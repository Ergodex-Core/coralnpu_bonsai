#!/usr/bin/env python3
"""Full actual Bonsai projection weights and independent host numerical references."""
import argparse
import ctypes
import hashlib
import json
import math
import pathlib
import random
import struct
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
MODEL_SHA = 'de68ba48a8dacb21979915991e7741b917869d71410a370df951c0c3a237ae50'
ROLES = [('q', 'attn_q'), ('k', 'attn_k'), ('v', 'attn_v'),
         ('output', 'attn_output'), ('gate', 'ffn_gate'), ('up', 'ffn_up'),
         ('down', 'ffn_down')]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_inventory(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
        if h.hexdigest() != MODEL_SHA: raise ValueError('Model SHA mismatch')
        f.seek(0)

        def read(fmt):
            return struct.unpack(
                '<' + fmt, f.read(struct.calcsize('<' + fmt))
            )[0]

        def string():
            return f.read(read('Q')).decode('utf8')

        types = {
            0: 'B',
            1: 'b',
            2: 'H',
            3: 'h',
            4: 'I',
            5: 'i',
            6: 'f',
            7: '?',
            10: 'Q',
            11: 'q',
            12: 'd'
        }

        def value(kind):
            if kind in types: return read(types[kind])
            if kind == 8: return string()
            if kind == 9:
                subtype, count = read('I'), read('Q')
                return [value(subtype) for _ in range(count)]
            raise ValueError(kind)

        if f.read(4) != b'GGUF' or read('I') != 3:
            raise ValueError('Expected GGUFv3')
        nt, nm = read('Q'), read('Q')
        meta = {}
        for _ in range(nm):
            key = string()
            meta[key] = value(read('I'))
        tensors = {}
        for _ in range(nt):
            name = string()
            dims = [read('Q') for _ in range(read('I'))]
            tensors[name] = {
                'dims': dims,
                'type': read('I'),
                'offset': read('Q')
            }
        alignment = meta.get('general.alignment', 32)
        start = (f.tell() + alignment - 1) // alignment * alignment
    return tensors, start


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def independent_check(raw, acts, scales, dots, outputs, m, n, k):
    groups = k // 128
    checks = 0
    for token in sorted({0, m - 1}):
        for row in sorted({0, 1, n // 2, n - 1}):
            total = 0.0
            for group in range(groups):
                offset = (row * groups + group) * 34
                block = raw[offset:offset + 34]
                scale = struct.unpack('<e', block[:2])[0]
                if not math.isfinite(scale):
                    raise ValueError('Nonfinite native scale')
                dot = 0
                for lane in range(128):
                    x = acts[token * k + group * 128 + lane]
                    if x >= 128: x -= 256
                    w = ((block[2 + lane // 4] >> (2 * (lane % 4))) & 3) - 1
                    dot += x * w
                if dot != dots[(token * n + row) * groups + group]:
                    raise AssertionError('Integer oracle mismatch')
                total = f32(
                    total + f32(
                        f32(float(dot) * scale) *
                        scales[token * groups + group]
                    )
                )
                checks += 1
            if struct.pack('<f',
                           total) != struct.pack('<f',
                                                 outputs[token * n + row]):
                raise AssertionError('Scaled oracle mismatch')
    return checks


def main():
    if sys.byteorder != 'little':
        raise RuntimeError(
            'This binary fixture writer requires a little-endian host'
        )
    ap = argparse.ArgumentParser()
    ap.add_argument('--weights', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    ap.add_argument('--summary', type=pathlib.Path, required=True)
    ap.add_argument('--cc', default='cc')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    tensors, start = read_inventory(a.weights)
    records = []
    compiler = subprocess.check_output([a.cc, '--version'],
                                       text=True).splitlines()[0]
    with tempfile.TemporaryDirectory() as temp:
        library = pathlib.Path(temp) / 'oracle.so'
        command = [
            a.cc, '-std=c11', '-O2', '-fno-fast-math', '-ffp-contract=off',
            '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
            str(ROOT / 'oracle.c'), '-o',
            str(library)
        ]
        subprocess.run(command, check=True)
        oracle = ctypes.CDLL(str(library)).bonsai_layer_oracle
        oracle.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
        ]
        for role_index, (role, suffix) in enumerate(ROLES):
            name = 'blk.0.' + suffix + '.weight'
            tensor = tensors[name]
            if tensor['type'] != 142 or len(tensor['dims']) != 2:
                raise ValueError(name)
            k, n = tensor['dims']
            groups = k // 128
            if k % 128 or n % 32:
                raise ValueError('Unexpected projection geometry')
            with a.weights.open('rb') as f:
                f.seek(start + tensor['offset'])
                raw = f.read(n * groups * 34)
            folder = a.out / role
            folder.mkdir(exist_ok=True)
            (folder / 'weights.pq2').write_bytes(raw)
            native = ctypes.create_string_buffer(raw)
            for m in [1, 32]:
                mode = 'decode' if m == 1 else 'prefill32'
                rng = random.Random(103 + role_index)
                acts = bytes(rng.randrange(256) for _ in range(m * k))
                scales = [
                    2.0**(-7 + (token + group) % 4)
                    for token in range(m)
                    for group in range(groups)
                ]
                av = ctypes.create_string_buffer(acts)
                sv = (ctypes.c_float * len(scales))(*scales)
                dots = (ctypes.c_int32 * (m * n * groups))()
                outputs = (ctypes.c_float * (m * n))()
                oracle(native, av, sv, m, n, k, dots, outputs)
                checks = independent_check(
                    raw, acts, scales, dots, outputs, m, n, k
                )
                files = {
                    f'{mode}.activations.i8': acts,
                    f'{mode}.activation_scales.f32': bytes(sv),
                    f'{mode}.group_dots.i32': bytes(dots),
                    f'{mode}.scaled_outputs.f32': bytes(outputs)
                }
                for filename, data in files.items():
                    (folder / filename).write_bytes(data)
                record = {
                    'role':
                    role,
                    'tensor':
                    name,
                    'M':
                    m,
                    'N':
                    n,
                    'K':
                    k,
                    'groups_per_row':
                    groups,
                    'native_weight_bytes':
                    len(raw),
                    'weight_sha256':
                    sha(raw),
                    'gguf_payload_offset':
                    start + tensor['offset'],
                    'mode':
                    mode,
                    'activation_scope':
                    'Deterministic synthetic signed-int8 operands and explicit power-of-two FP32 scales; NOT captured model activations and NOT a validated Bonsai quantizer',
                    'seed':
                    103 + role_index,
                    'host_status':
                    'PASS',
                    'independent_python_group_checks':
                    checks,
                    'files': {
                        role + '/' + filename: {
                            'bytes': len(data),
                            'sha256': sha(data)
                        }
                        for filename, data in files.items()
                    },
                    'useful_mac_equivalents':
                    m * n * k,
                    'existing_tile_jobs':
                    math.ceil(m / 8) * (n // 32) * groups,
                    'existing_tile_read_bytes':
                    (n // 32) * groups * (1088 * math.ceil(m / 8) + m * 128),
                    'weight_reloads_in_existing_cold_tile_schedule':
                    math.ceil(m / 8),
                    'read_bytes_scope':
                    'Exact cold tile schedule input payload. Includes repeated activations across N tiles, repeated weights across token tiles; excludes output writes, descriptor, scale/reduce and layer sequencer costs.',
                    'Arcsim_integer_groups':
                    'NOT_RUN',
                    'Arcsim_full_scaled_layer':
                    'NOT_IMPLEMENTED_IN_RTL',
                    'FPGA_integer_groups':
                    'NOT_RUN',
                    'FPGA_full_scaled_layer':
                    'NOT_RUN',
                    'cycles':
                    None,
                    'measured_useful_bandwidth_GB_per_s':
                    None,
                    'layer_seconds':
                    None
                }
                records.append(record)
                print(role, mode, (m, n, k), 'PASS', checks, flush=True)
    result = {
        'scope':
        'Full projection-shape actual weight fixtures and independent HOST numerical references; no performance measurements',
        'model_sha256': MODEL_SHA,
        'block_index': 0,
        'endianness': 'little',
        'arithmetic':
        'dot_i32=sum(int8_activation*(PQ2_code-1)); per group p=RN32(dot*FP16_weight_scale); q=RN32(p*FP32_activation_scale); y=RN32(y+q), increasing K-group order, y initialized+0. No bias, activation function or fused FMA.',
        'tensor_orders': {
            'weights': 'N,G,34 bytes',
            'activations': 'M,K',
            'activation_scales': 'M,G',
            'group_dots': 'M,N,G int32',
            'scaled_outputs': 'M,N float32'
        },
        'oracle_sources': {
            p.name: sha(p.read_bytes())
            for p in [ROOT / 'oracle.c', ROOT / 'prepare_layers.py']
        },
        'host_compiler': compiler,
        'host_flags': ['-O2', '-fno-fast-math', '-ffp-contract=off'],
        'records': records
    }
    (a.out / 'manifest.json').write_text(json.dumps(result, indent=2) + '\n')
    a.summary.parent.mkdir(parents=True, exist_ok=True)
    a.summary.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__': main()
