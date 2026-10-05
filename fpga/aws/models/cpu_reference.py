#!/usr/bin/env python3
"""Independent NumPy CPU oracle. Never called by hardware inference.

Streams FP32-decoded rows; ordered reductions round each add to FP32.
Requires numpy==2.4.2. Tokenization is a separate explicitly pinned input.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import struct
import sys
import time
import numpy as np
from pack_model import HEADER, TENSOR, Config, GLOBAL, F32, BF16, PQ2, Q8, TIED, QKNORM, YARN, sha256, require


class Reference:

    def __init__(self, manifest_path, trace_dir=None):
        load_started = time.monotonic()
        self.manifest = json.loads(Path(manifest_path).read_text())
        seg = self.manifest['segments'][0]
        path = Path(manifest_path).parent / seg['file']
        require(
            path.stat().st_size == seg['bytes']
            and sha256(path) == seg['sha256'], 'package size/hash mismatch'
        )
        with path.open('rb') as source:
            self.data = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)
        h = HEADER.unpack_from(self.data)
        require(h[0] == b'CORALM01' and h[1] in (1, 2)
                and h[2:4] == (128, len(self.data)), 'bad image')
        self.c = Config(*h[6:15], *h[15:22])
        require(1 <= self.c.max_seq <= 2048, 'unsupported context')
        self.ts = {}
        for i in range(h[4]):
            t = TENSOR.unpack_from(self.data, 128 + 32 * i)
            require(t[2] != Q8 or h[1] == 2, 'Q8 requires version2')
            require(h[1] != 2 or t[2] in (F32, Q8), 'unsupported version2 encoding')
            require(t[5] + t[6] <= len(self.data), 'tensor outside image')
            self.ts[t[:2]] = t
        self.position = 0
        self.keys = np.zeros((
            self.c.n_layers, self.c.max_seq, self.c.n_kv_heads, self.c.head_dim
        ), np.float32)
        self.values = np.zeros_like(self.keys)
        c = self.c
        freq = np.power(
            c.rope_theta,
            -2 * np.arange(c.head_dim // 2, dtype=np.float64) / c.head_dim
        )
        if c.flags & YARN:
            low = max(
                0,
                math.floor(
                    c.head_dim * math.log(
                        c.rope_original_context /
                        (c.yarn_beta_fast * 2 * math.pi)
                    ) / (2 * math.log(c.rope_theta))
                )
            )
            high = min(
                c.head_dim - 1,
                math.ceil(
                    c.head_dim * math.log(
                        c.rope_original_context /
                        (c.yarn_beta_slow * 2 * math.pi)
                    ) / (2 * math.log(c.rope_theta))
                )
            )
            ramp = 1 - np.clip((np.arange(c.head_dim // 2) - low) /
                               max(.001, high - low), 0, 1)
            freq *= ramp + (1 - ramp) / c.rope_factor
        self.freq = freq.astype(np.float32)
        self.trace = {}
        self.trace_records = []
        self.trace_dir = Path(trace_dir) if trace_dir is not None else None
        if self.trace_dir is not None:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            require(
                not any(self.trace_dir.iterdir()),
                'trace directory must be empty'
            )
        self.load_seconds = time.monotonic() - load_started
        self.metrics = {}

    def rows(self, role, layer, first=0, count=None):
        t = self.ts[role, layer]
        _, _, enc, rows, cols, offset, _, _ = t
        if count is None: count = rows - first
        require(
            0 <= first <= rows and 0 <= count <= rows - first, 'row bounds'
        )
        if enc == F32:
            return np.frombuffer(
                self.data,
                dtype='<f4',
                count=count * cols,
                offset=offset + first * cols * 4
            ).reshape(count, cols)
        if enc == BF16:
            x = np.frombuffer(
                self.data,
                dtype='<u2',
                count=count * cols,
                offset=offset + first * cols * 2
            )
            return np.left_shift(x.astype(np.uint32),
                                 16).view(np.float32).reshape(count, cols)
        require(enc in (PQ2, Q8) and cols % (128 if enc == PQ2 else 32) == 0, 'encoding')
        group = 128 if enc == PQ2 else 32
        blocks = count * cols // group
        x = np.frombuffer(
            self.data,
            dtype=np.uint8,
            count=blocks * 34,
            offset=offset + first * (cols // group) * 34
        ).reshape(blocks, 34)
        scales = x[:, :2].copy().view('<f2').astype(np.float32
                                                    ).reshape(blocks, 1)
        if enc == Q8:
            return (x[:, 2:].copy().view(np.int8).astype(np.float32) * scales).reshape(count, cols)
        codes = ((x[:, 2:, None] >> np.array([0, 2, 4, 6], np.uint8))
                 & 3).reshape(blocks, 128).astype(np.float32) - 1
        return (codes * scales).reshape(count, cols)

    @staticmethod
    def total(x, axis=-1):
        return np.add.accumulate(
            x, axis=axis, dtype=np.float32
        ).take(
            -1, axis=axis
        )

    def mat(self, role, layer, x):
        t = self.ts[role, layer]
        if t[2] == Q8:
            return self.mat_q8(t, x)
        result = np.empty(t[3], np.float32)
        for row in range(0, t[3], 128):
            n = min(128, t[3] - row)
            result[row:row +
                   n] = self.total(self.rows(role, layer, row, n) * x)
        return result

    @staticmethod
    def quant_q8(x):
        blocks = np.asarray(x, np.float32).reshape(-1, 32)
        require(np.isfinite(blocks).all(), 'Q8 activation must be finite')
        d = np.max(np.abs(blocks), axis=1) / np.float32(127)
        inverse = np.zeros_like(d)
        np.divide(np.float32(1), d, out=inverse, where=d != 0)
        scale = d.astype('<f2').astype(np.float32)
        require(np.isfinite(inverse).all() and np.isfinite(scale).all(), 'Q8 activation scale range')
        product = blocks * inverse[:, None]
        # roundf ties away; avoid np.rint (ties even) and float32 +0.5 double rounding.
        codes = np.copysign(np.floor(np.abs(product).astype(np.float64) + 0.5), product).astype(np.int32)
        require(np.max(np.abs(codes)) <= 127, 'Q8 activation code range')
        return scale, codes

    def mat_q8(self, t, x):
        _, _, _, rows, cols, offset, _, _ = t
        scale, codes = self.quant_q8(x)
        result = np.empty(rows, np.float32)
        blocks = cols // 32
        for first in range(0, rows, 128):
            n = min(128, rows - first)
            raw = np.frombuffer(self.data, dtype=np.uint8,
                                count=n * blocks * 34, offset=offset + first * blocks * 34).reshape(n, blocks, 34)
            ws = raw[:, :, :2].copy().view('<f2').reshape(n, blocks).astype(np.float32)
            wc = raw[:, :, 2:].copy().view(np.int8).astype(np.int32)
            sums = np.sum(wc * codes, axis=2, dtype=np.int32)
            result[first:first+n] = self.total(sums.astype(np.float32) * (ws * scale))
        return result

    def norm(self, x, role, layer):
        w = self.rows(role, layer).reshape(-1)
        inv = np.float32(1) / np.sqrt(
            self.total(x * x) / np.float32(x.shape[-1]) +
            np.float32(self.c.rms_eps)
        )
        return (x * inv[..., None]) * w if x.ndim > 1 else (x * inv) * w

    def rope(self, x):
        c = self.c
        d = c.head_dim // 2
        a = np.float32(self.position) * self.freq
        magnitude = np.float32(
            c.yarn_attention_factor if c.flags & YARN else 1
        )
        cs = np.cos(a) * magnitude
        sn = np.sin(a) * magnitude
        return np.concatenate(
            (x[:, :d] * cs - x[:, d:] * sn, x[:, :d] * sn + x[:, d:] * cs),
            axis=1
        )

    def record(self, name, layer, x):
        stage = {
            'attn_norm': 1,
            'q': 2,
            'k': 3,
            'v': 4,
            'attention': 5,
            'ffn_norm': 6,
            'gate': 7,
            'up': 8,
            'layer': 9,
            'logits': 10
        }[name]
        data = x.astype('<f4').tobytes()
        digest = hashlib.sha256(data).hexdigest()
        self.trace[f'{self.position}:{layer}:{name}'] = digest
        if self.trace_dir is not None:
            filename = f'{self.position:04d}-{layer:03d}-{stage:02d}-{name}.f32'
            (self.trace_dir / filename).write_bytes(data)
            self.trace_records.append({
                'position': self.position,
                'layer': layer,
                'stage': stage,
                'name': name,
                'shape': list(x.shape),
                'count': int(x.size),
                'file': filename,
                'sha256': digest
            })

    def step(self, token):
        c = self.c
        p = self.position
        require(0 <= token < c.vocab and p < c.max_seq, 'token/context bounds')
        x = self.rows(1, GLOBAL, token, 1).reshape(-1).copy()
        for layer in range(c.n_layers):
            n = self.norm(x, 10, layer)
            self.record('attn_norm', layer, n)
            q = self.mat(11, layer, n).reshape(c.n_heads, c.head_dim)
            k = self.mat(12, layer, n).reshape(c.n_kv_heads, c.head_dim)
            v = self.mat(13, layer, n).reshape(c.n_kv_heads, c.head_dim)
            if c.flags & QKNORM:
                q = self.norm(q, 15, layer)
                k = self.norm(k, 16, layer)
            q = self.rope(q)
            k = self.rope(k)
            for name, a in [('q', q), ('k', k), ('v', v)]:
                self.record(name, layer, a)
            self.keys[layer, p] = k
            self.values[layer, p] = v
            att = np.empty_like(q)
            for head in range(c.n_heads):
                kh = head // (c.n_heads // c.n_kv_heads)
                score = self.total(self.keys[layer, :p + 1, kh] * q[head]
                                   ) / np.float32(math.sqrt(c.head_dim))
                score = np.exp(score - np.max(score))
                score *= np.float32(1) / self.total(score)
                att[head] = self.total(
                    self.values[layer, :p + 1, kh] * score[:, None], axis=0
                )
            self.record('attention', layer, att)
            residual = x + self.mat(14, layer, att.reshape(-1))
            n = self.norm(residual, 17, layer)
            self.record('ffn_norm', layer, n)
            gate = self.mat(18, layer, n)
            up = self.mat(19, layer, n)
            self.record('gate', layer, gate)
            self.record('up', layer, up)
            with np.errstate(over='ignore'):
                activated = gate / (np.float32(1) + np.exp(-gate))
            x = residual + self.mat(20, layer, up * activated)
            self.record('layer', layer, x)
        logits = self.mat(
            1 if c.flags & TIED else 3, GLOBAL, self.norm(x, 2, GLOBAL)
        )
        require(np.all(np.isfinite(logits)), 'nonfinite logits')
        self.record('logits', c.n_layers, logits)
        self.position += 1
        return logits

    def generate(self, tokens, count, eos=(), logits_path=None):
        require(
            tokens and count >= 0 and self.position == 0
            and len(tokens) + max(0, count - 1) <= self.c.max_seq,
            'invalid prompt/decode length'
        )
        require(
            all(0 <= t < self.c.vocab for t in tokens), 'invalid prompt token'
        )
        require(
            len(eos) <= 32 and all(0 <= t < self.c.vocab for t in eos),
            'invalid EOS token'
        )
        begin = time.monotonic()
        prefill = []
        decode = []
        for t in tokens:
            started = time.monotonic()
            logits = self.step(t)
            prefill.append(time.monotonic() - started)
        self.metrics = {
            'load_seconds': self.load_seconds,
            'prefill_seconds': time.monotonic() - begin,
            'prefill_token_seconds': prefill,
            'decode_token_seconds': decode,
            'decode_seconds': 0.0
        }
        output = []
        steps = []
        stream = Path(logits_path).open(
            'wb'
        ) if logits_path is not None else nullcontext()
        with stream as output_file:
            for i in range(max(1, count)):
                token = int(np.argmax(logits))
                raw = logits.astype('<f4').tobytes()
                if output_file is not None:
                    output_file.write(raw)
                if count:
                    output.append(token)
                top = np.argsort(logits)[-5:][::-1]
                steps.append({
                    'position':
                    self.position - 1,
                    'token':
                    token,
                    'logits_sha256':
                    hashlib.sha256(raw).hexdigest(),
                    'top5': [{
                        'token': int(t),
                        'logit': float(logits[t])
                    } for t in top]
                })
                if count and token in eos:
                    self.metrics['decode_seconds'] = sum(decode)
                    return output, steps, 'eos'
                if i + 1 < count:
                    started = time.monotonic()
                    logits = self.step(token)
                    decode.append(time.monotonic() - started)
        self.metrics['decode_seconds'] = sum(decode)
        return output, steps, 'length' if count else 'prefill'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest', type=Path)
    p.add_argument(
        '--tokens',
        required=True,
        help='comma-separated IDs from independently pinned tokenizer'
    )
    p.add_argument('--max-new-tokens', type=int, default=4)
    p.add_argument('--eos', default='151645,151643')
    p.add_argument(
        '--trace-dir',
        type=Path,
        help=
        'empty output directory for named operator and per-step logits FP32 vectors'
    )
    p.add_argument('--output', type=Path, required=True)
    p.add_argument(
        '--logits-output',
        type=Path,
        help=
        'raw little-endian FP32 prediction rows; defaults to OUTPUT.logits.f32'
    )
    a = p.parse_args()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    logits_path = a.logits_output or a.output.with_suffix('.logits.f32')
    logits_path.parent.mkdir(parents=True, exist_ok=True)
    require(
        logits_path.resolve() != a.output.resolve(),
        'logits and report paths must differ'
    )
    t = time.monotonic()
    r = Reference(a.manifest, a.trace_dir)
    tokens = [int(v) for v in a.tokens.split(',')]
    eos = [int(v) for v in a.eos.split(',') if v]
    generated, steps, reason = r.generate(
        tokens, a.max_new_tokens, eos, logits_path
    )
    report = {
        'backend':
        'independent NumPy FP32 ordered reference',
        'model_id':
        r.manifest['model_id'],
        'package_sha256':
        r.manifest['segments'][0]['sha256'],
        'config':
        r.manifest['config'],
        'input_ids':
        tokens,
        'generated_ids':
        generated,
        'max_new_tokens':
        a.max_new_tokens,
        'return_code':
        0,
        'stop_reason':
        reason,
        'eos_ids':
        eos,
        'steps':
        steps,
        'operator_trace_sha256':
        r.trace,
        'trace_records':
        r.trace_records,
        'metrics':
        r.metrics,
        'elapsed_seconds':
        time.monotonic() - t,
        'trace_directory':
        os.path.relpath(a.trace_dir.resolve(), a.output.parent.resolve())
        if a.trace_dir else None,
        'logits_file':
        os.path.relpath(logits_path.resolve(), a.output.parent.resolve()),
        'logits_sha256':
        sha256(logits_path),
        'logits_rows':
        len(steps),
        'numpy_version':
        np.__version__,
        'reference_source_sha256':
        sha256(Path(__file__)),
        'physical':
        'NOT_RUN',
        'coral_simulation':
        'NOT_RUN'
    }
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2) + '\n')
    print(
        json.dumps({
            'generated_ids': generated,
            'stop_reason': reason,
            'metrics': r.metrics
        })
    )


if __name__ == '__main__': main()
