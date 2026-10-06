#!/usr/bin/env python3
"""Lossless, bounded-memory export of the two pinned official checkpoints.

No model code is imported/executed. Tensor payloads are streamed unchanged.
The only layout change is a named, aligned tensor directory with relative offsets.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, asdict
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

MAGIC = b"CORALM01"
HEADER = struct.Struct("<8s14I7f9I")
TENSOR = struct.Struct("<8I")
GLOBAL = 0xffffffff
F32, BF16, PQ2 = 1, 2, 3
Q8 = 4
Q8_REV = '23749fefcc72300e3a2ad315e1317431b06b590a'
Q8_SHA = '9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031'
TIED, QKNORM, YARN = 1, 2, 4
QWEN_REV = "c1899de289a04d12100db370d81485cdf75e47ca"
BONSAI_REV = "983b5dec2ff16aab79990711ba0f828a499a7e6a"
QWEN_SHA = "f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b"
BONSAI_SHA = "de68ba48a8dacb21979915991e7741b917869d71410a370df951c0c3a237ae50"
DDRB, DDRE, DEFAULT_ADDRESS = 0x20000000, 0xa0000000, 0x21000000
HF_ROLES = {
    "input_layernorm.weight": 10,
    "self_attn.q_proj.weight": 11,
    "self_attn.k_proj.weight": 12,
    "self_attn.v_proj.weight": 13,
    "self_attn.o_proj.weight": 14,
    "self_attn.q_norm.weight": 15,
    "self_attn.k_norm.weight": 16,
    "post_attention_layernorm.weight": 17,
    "mlp.gate_proj.weight": 18,
    "mlp.up_proj.weight": 19,
    "mlp.down_proj.weight": 20,
}
GG_ROLES = {
    "attn_norm.weight": 10,
    "attn_q.weight": 11,
    "attn_k.weight": 12,
    "attn_v.weight": 13,
    "attn_output.weight": 14,
    "attn_q_norm.weight": 15,
    "attn_k_norm.weight": 16,
    "ffn_norm.weight": 17,
    "ffn_gate.weight": 18,
    "ffn_up.weight": 19,
    "ffn_down.weight": 20
}


@dataclass
class Config:
    dim: int
    hidden_dim: int
    n_layers: int
    n_heads: int
    n_kv_heads: int
    head_dim: int
    vocab: int
    max_seq: int = 2048
    flags: int = TIED | QKNORM
    rope_theta: float = 1000000.0
    rms_eps: float = 1e-6
    rope_factor: float = 1.0
    rope_original_context: float = 40960.0
    yarn_beta_fast: float = 0.0
    yarn_beta_slow: float = 0.0
    yarn_attention_factor: float = 1.0

    def workspace_bytes(self):
        q = self.n_heads * self.head_dim
        kv = self.n_kv_heads * self.head_dim
        return 4 * (
            3 * self.dim + 2 * q + 2 * kv + 2 * self.hidden_dim + self.max_seq
            + self.head_dim // 2 + 2 * self.n_layers * self.max_seq * kv
        )

    def kv_bytes_per_token(self):
        return 8 * self.n_layers * self.n_kv_heads * self.head_dim


@dataclass
class Tensor:
    name: str
    role: int
    layer: int
    encoding: int
    rows: int
    cols: int
    source: Path
    source_offset: int
    bytes: int
    offset: int = 0


def align(value, n=64):
    return (value + n - 1) // n * n


def exact(f, n):
    b = f.read(n)
    if len(b) != n:
        raise ValueError("truncated input")
    return b


def sha256(path, offset=0, count=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(offset)
        while count is None or count:
            b = f.read(
                1024 * 1024 if count is None else min(count, 1024 * 1024)
            )
            if not b:
                if count: raise ValueError("truncated hash range")
                break
            h.update(b)
            if count is not None: count -= len(b)
    return h.hexdigest()


def require(condition, message):
    if not condition: raise ValueError(message)


def check_source(path, expected):
    actual = sha256(path)
    require(
        actual == expected,
        f"checkpoint SHA256 mismatch: {actual}; expected {expected}"
    )
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": actual}


def gguf_header(path, keep_tokenizer=False):
    """Strict GGUF v3 header reader; skips tokenizer payloads, never executes code."""
    with path.open("rb") as f:

        def read(fmt):
            return struct.unpack(
                "<" + fmt, exact(f, struct.calcsize("<" + fmt))
            )[0]

        def string():
            n = read("Q")
            require(n <= 64 * 1024 * 1024, "oversize GGUF string")
            return exact(f, n).decode("utf-8")

        def value(t, keep=True, depth=0):
            require(depth < 3, "nested GGUF metadata exceeds limit")
            if t == 8:
                n = read("Q")
                require(n <= 64 * 1024 * 1024, "oversize GGUF string")
                b = exact(f, n)
                return b.decode("utf-8") if keep else None
            if t == 9:
                kind, n = read("I"), read("Q")
                require(n <= 2_000_000, "oversize GGUF array")
                vals = []
                for _ in range(n):
                    v = value(kind, keep, depth + 1)
                    if keep: vals.append(v)
                return vals
            fmt = {
                0: "B",
                1: "b",
                2: "H",
                3: "h",
                4: "I",
                5: "i",
                6: "f",
                7: "?",
                10: "Q",
                11: "q",
                12: "d"
            }.get(t)
            require(fmt is not None, "unsupported GGUF metadata type")
            return read(fmt)

        require(exact(f, 4) == b"GGUF" and read("I") == 3, "expected GGUF v3")
        n, m = read("Q"), read("Q")
        require(n <= 4096 and m <= 4096, "oversize GGUF directory")
        meta = {}
        for _ in range(m):
            name = string()
            require(name not in meta, "duplicate GGUF key")
            meta[name] = value(
                read("I"), keep_tokenizer or not name.startswith("tokenizer.")
            )
        tensors = []
        for _ in range(n):
            name = string()
            nd = read("I")
            require(1 <= nd <= 2, "only vector/matrix tensors supported")
            dims = [read("Q") for _ in range(nd)]
            tensors.append((name, dims, read("I"), read("Q")))
        alignment = meta.get("general.alignment", 32)
        require(
            alignment > 0 and alignment <= 4096
            and alignment & (alignment - 1) == 0, "bad GGUF alignment"
        )
        start = align(f.tell(), alignment)
    return meta, tensors, start


def qwen_sources(directory, max_seq):
    path = directory / "model.safetensors"
    provenance = [check_source(path, QWEN_SHA)]
    cfgpath = directory / "config.json"
    cfg = json.loads(cfgpath.read_text())
    expected = {
        "model_type": "qwen3",
        "hidden_size": 1024,
        "intermediate_size": 3072,
        "num_hidden_layers": 28,
        "num_attention_heads": 16,
        "num_key_value_heads": 8,
        "head_dim": 128,
        "vocab_size": 151936,
        "tie_word_embeddings": True,
        "hidden_act": "silu",
        "attention_bias": False,
        "rope_theta": 1000000,
        "rope_scaling": None,
        "use_sliding_window": False,
        "rms_norm_eps": 1e-6
    }
    for k, v in expected.items():
        require(cfg.get(k) == v, f"unsupported Qwen config {k}")
    provenance.append({
        "file": cfgpath.name,
        "bytes": cfgpath.stat().st_size,
        "sha256": sha256(cfgpath)
    })
    c = Config(1024, 3072, 28, 16, 8, 128, 151936, max_seq)
    with path.open("rb") as f:
        n = struct.unpack("<Q", exact(f, 8))[0]
        require(2 <= n <= 16 * 1024 * 1024, "oversize safetensors header")

        def pairs(xs):
            d = {}
            for k, v in xs:
                require(k not in d, "duplicate safetensors key")
                d[k] = v
            return d

        header = json.loads(exact(f, n), object_pairs_hook=pairs)
    tensors = []
    extra_output = None
    ranges = []
    for name, item in header.items():
        if name == "__metadata__": continue
        shape = item["shape"]
        require(
            item["dtype"] == "BF16", f"expected original BF16 tensor: {name}"
        )
        require(
            len(shape) in (1, 2)
            and all(type(x) is int and x > 0 for x in shape),
            "bad tensor shape"
        )
        rows, cols = (1, shape[0]) if len(shape) == 1 else shape
        a, b = item["data_offsets"]
        require(
            type(a) is int and type(b) is int and 0 <= a < b
            and b - a == rows * cols * 2, "bad safetensors offsets"
        )
        require(
            8 + n + b <= path.stat().st_size,
            "safetensors payload outside file"
        )
        ranges.append((a, b))
        if name == "model.embed_tokens.weight": role, layer = 1, GLOBAL
        elif name == "model.norm.weight": role, layer = 2, GLOBAL
        elif name == "lm_head.weight": role, layer = 3, GLOBAL
        else:
            prefix, _, suffix = name.partition("model.layers.")
            require(not prefix and suffix, "unexpected tensor " + name)
            li, _, key = suffix.partition(".")
            require(
                li.isdigit() and key in HF_ROLES, "unexpected tensor " + name
            )
            role, layer = HF_ROLES[key], int(li)
        t = Tensor(name, role, layer, BF16, rows, cols, path, 8 + n + a, b - a)
        if role == 3: extra_output = t
        else: tensors.append(t)
    ranges.sort()
    require(
        all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))
        and ranges[0][0] == 0 and 8 + n + ranges[-1][1] == path.stat().st_size,
        "safetensors has gaps/overlap/trailing bytes"
    )
    if extra_output is not None:
        embedding = next(t for t in tensors if t.role == 1)
        require((extra_output.rows,
                 extra_output.cols) == (embedding.rows, embedding.cols),
                "tied output shape mismatch")
        require(
            sha256(path, extra_output.source_offset, extra_output.bytes
                   ) == sha256(path, embedding.source_offset, embedding.bytes),
            "lm_head differs from tied embedding"
        )
    validate_tensors(c, tensors)
    return c, tensors, provenance, "Qwen/Qwen3-0.6B", QWEN_REV


def qwen_q8_sources(path, max_seq):
    provenance = [check_source(path, Q8_SHA)]
    meta, raw, start = gguf_header(path)
    expected = {
        'general.architecture': 'qwen3',
        'general.file_type': 7,
        'general.quantization_version': 2,
        'qwen3.block_count': 28,
        'qwen3.embedding_length': 1024,
        'qwen3.feed_forward_length': 3072,
        'qwen3.attention.head_count': 16,
        'qwen3.attention.head_count_kv': 8,
        'qwen3.attention.key_length': 128,
        'qwen3.attention.value_length': 128,
        'qwen3.rope.freq_base': 1000000.,
    }
    for key, value in expected.items():
        require(meta.get(key) == value, 'unsupported Q8 metadata ' + key)
    c = Config(
        1024,
        3072,
        28,
        16,
        8,
        128,
        151936,
        max_seq,
        rms_eps=meta['qwen3.attention.layer_norm_rms_epsilon']
    )
    tensors = []
    for name, dims, typ, offset in raw:
        if name == 'token_embd.weight': role, layer = 1, GLOBAL
        elif name == 'output_norm.weight': role, layer = 2, GLOBAL
        else:
            fields = name.split('.', 2)
            require(
                len(fields) == 3 and fields[0] == 'blk' and fields[1].isdigit()
                and fields[2] in GG_ROLES, 'unexpected Q8 tensor ' + name
            )
            role, layer = GG_ROLES[fields[2]], int(fields[1])
        rows, cols = (1, dims[0]) if len(dims) == 1 else (dims[1], dims[0])
        norm = role in (2, 10, 15, 16, 17)
        require(
            typ == (0 if norm else 8),
            'Q8 profile expects F32 norms / Q8 matrices'
        )
        require(norm or cols % 32 == 0, 'unaligned Q8 columns')
        size = rows * cols * 4 if norm else rows * (cols // 32) * 34
        require(
            start + offset + size <= path.stat().st_size,
            'Q8 tensor outside file'
        )
        tensors.append(
            Tensor(
                name, role, layer, F32 if norm else Q8, rows, cols, path,
                start + offset, size
            )
        )
    validate_tensors(c, tensors)
    return c, tensors, provenance, 'Qwen/Qwen3-0.6B-GGUF', Q8_REV


def bonsai_sources(path, max_seq, yarn):
    provenance = [check_source(path, BONSAI_SHA)]
    meta, raw, start = gguf_header(path)
    expected = {
        "general.architecture": "qwen3",
        "general.license": "apache-2.0",
        "qwen3.embedding_length": 2048,
        "qwen3.feed_forward_length": 6144,
        "qwen3.block_count": 28,
        "qwen3.attention.head_count": 16,
        "qwen3.attention.head_count_kv": 8,
        "qwen3.attention.key_length": 128,
        "qwen3.attention.value_length": 128,
        "qwen3.rope.freq_base": 1000000.,
        "qwen3.rope.scaling.type": "yarn",
        "qwen3.rope.scaling.factor": 4.,
        "qwen3.rope.scaling.original_context_length": 8192
    }
    for k, v in expected.items():
        require(meta.get(k) == v, f"unsupported Bonsai metadata {k}")
    require(
        all(v is not None and math.isfinite(v) and v > 0 for v in yarn),
        "Bonsai requires explicit positive --yarn-beta-fast, --yarn-beta-slow, --yarn-attention-factor"
    )
    require(yarn[0] > yarn[1], "YaRN beta-fast must exceed beta-slow")
    c = Config(
        2048,
        6144,
        28,
        16,
        8,
        128,
        151669,
        max_seq,
        TIED | QKNORM | YARN,
        rms_eps=meta["qwen3.attention.layer_norm_rms_epsilon"],
        rope_factor=4.,
        rope_original_context=8192.,
        yarn_beta_fast=yarn[0],
        yarn_beta_slow=yarn[1],
        yarn_attention_factor=yarn[2]
    )
    tensors = []
    for name, dims, typ, offset in raw:
        if name == "token_embd.weight": role, layer = 1, GLOBAL
        elif name == "output_norm.weight": role, layer = 2, GLOBAL
        else:
            fields = name.split(".", 2)
            require(
                len(fields) == 3 and fields[0] == "blk" and fields[1].isdigit()
                and fields[2] in GG_ROLES, "unexpected GGUF tensor " + name
            )
            role, layer = GG_ROLES[fields[2]], int(fields[1])
        require(typ in (0, 142), "only original F32/PQ2_0 is supported")
        rows, cols = (1, dims[0]) if len(dims) == 1 else (dims[1], dims[0])
        require(typ == 0 or cols % 128 == 0, "unaligned PQ2_0 columns")
        size = rows * cols * 4 if typ == 0 else rows * (cols // 128) * 34
        require(
            start + offset + size <= path.stat().st_size,
            "GGUF tensor outside file"
        )
        tensors.append(
            Tensor(
                name, role, layer, F32 if typ == 0 else PQ2, rows, cols, path,
                start + offset, size
            )
        )
    validate_tensors(c, tensors)
    return c, tensors, provenance, "prism-ml/Ternary-Bonsai-1.7B-gguf", BONSAI_REV


def expected_shapes(c):
    shapes = {(1, GLOBAL): (c.vocab, c.dim), (2, GLOBAL): (1, c.dim)}
    if not c.flags & TIED: shapes[3, GLOBAL] = (c.vocab, c.dim)
    q, kv = c.n_heads * c.head_dim, c.n_kv_heads * c.head_dim
    per = {
        10: (1, c.dim),
        11: (q, c.dim),
        12: (kv, c.dim),
        13: (kv, c.dim),
        14: (c.dim, q),
        17: (1, c.dim),
        18: (c.hidden_dim, c.dim),
        19: (c.hidden_dim, c.dim),
        20: (c.dim, c.hidden_dim)
    }
    if c.flags & QKNORM: per.update({15: (1, c.head_dim), 16: (1, c.head_dim)})
    for layer in range(c.n_layers):
        for role, shape in per.items():
            shapes[role, layer] = shape
    return shapes


def validate_tensors(c, tensors):
    require(1 <= c.max_seq <= 2048, "runtime supports max_seq 1..2048")
    require(
        c.n_heads % c.n_kv_heads == 0 and c.head_dim % 2 == 0,
        "bad GQA/head geometry"
    )
    expected = expected_shapes(c)
    seen = set()
    ranges = []
    for t in tensors:
        key = t.role, t.layer
        require(
            key not in seen and key in expected,
            "duplicate/unexpected tensor role"
        )
        seen.add(key)
        require((t.rows, t.cols) == expected[key],
                "bad tensor shape: " + t.name)
        require(t.encoding in (F32, BF16, PQ2, Q8), "unsupported encoding")
        require(
            t.encoding not in (PQ2, Q8) or (
                t.cols % (128 if t.encoding == PQ2 else 32) == 0
                and t.role not in (2, 10, 15, 16, 17)
            ), "invalid packed tensor"
        )
        size = t.rows * t.cols * (
            4 if t.encoding == F32 else 2
        ) if t.encoding not in (
            PQ2, Q8
        ) else t.rows * (t.cols // (128 if t.encoding == PQ2 else 32)) * 34
        require(t.bytes == size, "bad tensor size")
        ranges.append(
            (str(t.source), t.source_offset, t.source_offset + t.bytes)
        )
    require(seen == set(expected), "missing tensor roles")
    ranges.sort()
    require(
        all(a[0] != b[0] or a[2] <= b[1] for a, b in zip(ranges, ranges[1:])),
        "overlapping source tensors"
    )


def write_package(
    out,
    c,
    tensors,
    provenance,
    model_id,
    revision,
    address=DEFAULT_ADDRESS,
    memory_profile='ddr'
):
    validate_tensors(c, tensors)
    tensors = sorted(tensors, key=lambda t: (t.layer, t.role))
    cursor = align(HEADER.size + TENSOR.size * len(tensors))
    for t in tensors:
        t.offset = cursor
        cursor = align(cursor + t.bytes)
    require(memory_profile in ('ddr', 'hbm'), 'unsupported memory profile')
    minimum, limit = (DEFAULT_ADDRESS, DDRE) if memory_profile == 'ddr' else (
        0x81000000, 0x100000000
    )
    require(
        minimum <= address < limit and address % 64 == 0,
        "model address outside reserved DDR map"
    )
    # Reserve full configured token input capacity, logits, and runtime workspace.
    end = align(align(align(address + cursor) + 4 * c.max_seq) +
                4 * c.vocab) + c.workspace_bytes()
    require(
        end <= limit,
        "model + tokens + logits + workspace exceed 2GiB DDR aperture; reduce --max-seq"
    )
    out.mkdir(parents=True, exist_ok=True)
    final = out / "model.bin"
    tmp = out / "model.bin.partial"
    require(
        not final.exists() and not (out / "manifest.json").exists(),
        "output package already exists"
    )
    ints = [
        2 if any(t.encoding == Q8 for t in tensors) else 1, 128, cursor,
        len(tensors), 128, c.dim, c.hidden_dim, c.n_layers, c.n_heads,
        c.n_kv_heads, c.head_dim, c.vocab, c.max_seq, c.flags
    ]
    floats = [
        c.rope_theta, c.rms_eps, c.rope_factor, c.rope_original_context,
        c.yarn_beta_fast, c.yarn_beta_slow, c.yarn_attention_factor
    ]
    records = []
    try:
        with tmp.open("wb") as f:
            f.write(HEADER.pack(MAGIC, *ints, *floats, *([0] * 9)))
            for t in tensors:
                f.write(
                    TENSOR.pack(
                        t.role, t.layer, t.encoding, t.rows, t.cols, t.offset,
                        t.bytes, 0
                    )
                )
            for t in tensors:
                f.write(bytes(t.offset - f.tell()))
                h = hashlib.sha256()
                with t.source.open("rb") as src:
                    src.seek(t.source_offset)
                    remaining = t.bytes
                    while remaining:
                        b = exact(src, min(remaining, 1024 * 1024))
                        f.write(b)
                        h.update(b)
                        remaining -= len(b)
                records.append({
                    "name": t.name,
                    "role": t.role,
                    "layer": t.layer,
                    "encoding": t.encoding,
                    "rows": t.rows,
                    "cols": t.cols,
                    "offset": t.offset,
                    "bytes": t.bytes,
                    "sha256": h.hexdigest()
                })
            f.write(bytes(cursor - f.tell()))
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    config = asdict(c)
    config["vocab_size"] = c.vocab
    manifest = {
        "schema":
        "coral-model-package-v1",
        "model_id":
        model_id,
        "source_revision":
        revision,
        "license":
        "Apache-2.0",
        "source_files":
        provenance,
        "config":
        config,
        "segments": [{
            "file": "model.bin",
            "address": address,
            "bytes": cursor,
            "sha256": sha256(final)
        }],
        "arithmetic":
        "q8_0-ref-activation-i32-block-fp32-ordered"
        if any(t.encoding == Q8 for t in tensors) else "fp32-ordered",
        "activations":
        "FP32; Q8_0 block32 matvec input"
        if any(t.encoding == Q8 for t in tensors) else "FP32",
        "kv":
        "FP32",
        "weights":
        "native Q8_0/F32 unchanged" if any(t.encoding == Q8 for t in tensors)
        else "BF16 unchanged" if all(t.encoding == BF16 for t in tensors
                                     ) else "native PQ2_0/F32 unchanged",
        "memory": {
            "profile": memory_profile,
            "npu_aperture_end_exclusive": limit,
            "workspace_bytes": c.workspace_bytes(),
            "max_seq": c.max_seq,
            "kv_bytes_per_token": c.kv_bytes_per_token(),
            "kv_bytes": c.kv_bytes_per_token() * c.max_seq,
            "planned_end_address": end
        },
        "tensors":
        records,
        "validation": {
            "package": "PASS",
            "full_model_reference": "NOT_RUN",
            "coral_simulation": "NOT_RUN",
            "physical": "NOT_RUN"
        }
    }
    (out / "manifest.json"
     ).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "kind", choices=["qwen3-0.6b", "qwen3-0.6b-q8_0", "bonsai-1.7b"]
    )
    p.add_argument('--memory-profile', choices=['ddr', 'hbm'], default='ddr')
    p.add_argument(
        "source",
        type=Path,
        help="Qwen snapshot directory or original Bonsai PQ2_0.gguf"
    )
    p.add_argument("output", type=Path)
    p.add_argument("--max-seq", type=int, default=2048)
    p.add_argument(
        "--address", type=lambda s: int(s, 0), default=DEFAULT_ADDRESS
    )
    p.add_argument("--yarn-beta-fast", type=float)
    p.add_argument("--yarn-beta-slow", type=float)
    p.add_argument("--yarn-attention-factor", type=float)
    a = p.parse_args()
    try:
        args = qwen_q8_sources(
            a.source, a.max_seq
        ) if a.kind == 'qwen3-0.6b-q8_0' else qwen_sources(
            a.source, a.max_seq
        ) if a.kind == "qwen3-0.6b" else bonsai_sources(
            a.source, a.max_seq,
            (a.yarn_beta_fast, a.yarn_beta_slow, a.yarn_attention_factor)
        )
        result = write_package(
            a.output,
            *args,
            address=a.address,
            memory_profile=a.memory_profile
        )
        print(
            json.dumps({
                "manifest": str(a.output / "manifest.json"),
                "model_id": result["model_id"],
                "model_bytes": result["segments"][0]["bytes"],
                "memory": result["memory"]
            },
                       indent=2)
        )
    except (ValueError, KeyError, OSError, struct.error) as e:
        p.exit(2, f"error: {e}\n")


if __name__ == "__main__": main()
