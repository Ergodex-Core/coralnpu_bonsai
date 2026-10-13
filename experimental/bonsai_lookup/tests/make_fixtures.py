#!/usr/bin/env python3
"""Independent scalar oracle. No imports from packing or RTL algorithms."""
import argparse
import hashlib
import json
import pathlib
import random
import struct

ROOT = pathlib.Path(__file__).resolve().parents[3]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--native-tensor', type=pathlib.Path, required=True)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    a = ap.parse_args()
    receipt = json.loads(
        (ROOT /
         'analysis/ternary_simd/results/packing-native.json').read_text()
    )
    raw = a.native_tensor.read_bytes()
    assert digest(raw) == receipt['native_tensor_sha256']
    assert receipt['N'] == receipt['K'] == 2048
    # Actual first128 weights of each of the first32 rows, retaining scale bits.
    original = [raw[row * 16 * 34:(row * 16 + 1) * 34] for row in range(32)]
    cases = []
    for name, m, n, lanes, kind in [
        ('decode', 1, 32, 128, 'native'), ('prefill', 8, 32, 128, 'native'),
        ('tails', 2, 31, 127, 'native'), ('bounds', 2, 32, 128, 'synthetic'),
        ('zero', 1, 17, 128, 'synthetic'), ('empty_k', 1, 31, 0, 'native'),
        ('reserved', 1, 31, 127, 'synthetic'), ('memory_error', 1, 32, 128,
                                                'fault')
    ]:
        blocks = list(original)
        rng = random.Random(103)
        activations = [[-128, 127, -1, 0] +
                       [rng.randrange(-128, 128)
                        for _ in range(124)]
                       for _ in range(m)]
        if name == 'bounds':
            blocks = [
                struct.pack('<H', 0x3c00) +
                bytes([0 if row % 2 == 0 else 0xaa]) * 32 for row in range(32)
            ]
            activations = [[-128] * 128, [127] * 128]
        if name == 'zero':
            blocks = [
                struct.pack('<H', row) + bytes([0x55]) * 32
                for row in range(32)
            ]
        if name == 'reserved':
            b = bytearray(blocks[0])
            b[2] |= 3
            blocks[0] = bytes(b)
            b = bytearray(blocks[1])
            b[33] |= 0xc0
            blocks[1] = bytes(b)  # Masked lane127.
            blocks[31] = bytes(2) + bytes([0xff]
                                          ) * 32  # Entire masked output row.
        body = b''.join(b[2:]
                        for b in blocks) + b''.join(b[:2] for b in blocks)
        memory = body + bytes(v & 255 for row in activations for v in row)
        values = []
        for token in range(m):
            for row in range(n):
                block = blocks[row]
                weights = [((block[2 + j // 4] >> ((j % 4) * 2)) & 3) - 1
                           for j in range(lanes)]
                invalid = any(w == 2 for w in weights)
                dot = 0 if invalid else sum(
                    activations[token][j] * weights[j] for j in range(lanes)
                )
                scale = int.from_bytes(block[:2], 'little')
                error = int(invalid or name == 'memory_error')
                values.append({
                    'token': token,
                    'row': row,
                    'dot': dot,
                    'scale_bits': scale,
                    'error': error
                })
        folder = a.out / name
        folder.mkdir(parents=True, exist_ok=True)
        memory_hex = ''.join(
            f'{int.from_bytes(memory[i:i+16],"little"):032x}\n'
            for i in range(0, len(memory), 16)
        )
        expected_hex = ''.join(
            f'{(v["error"]<<48)|(v["scale_bits"]<<32)|(v["dot"] & 0xffffffff):016x}\n'
            for v in values
        )
        (folder / 'memory.hex').write_text(memory_hex)
        (folder / 'expected.hex').write_text(expected_hex)
        (folder /
         'oracle.json').write_text(json.dumps(values, indent=2) + '\n')
        case = {
            'name': name,
            'M': m,
            'N': n,
            'K_valid': lanes,
            'K_storage': 128,
            'kind': kind,
            'activation_scope':
            'Seeded synthetic int8 operands; no model activation quantization claim',
            'model_sha256': receipt['model_sha256'],
            'tensor': receipt['tensor'],
            'native_tensor_sha256': digest(raw),
            'native_rows': list(range(32)),
            'native_K_group': 0,
            'memory_bytes': len(memory),
            'weight_bytes': 1088,
            'activation_bytes': m * 128,
            'useful_mac_equivalents': m * n * lanes,
            'memory_sha256': digest(memory),
            'memory_hex_sha256': digest(memory_hex.encode()),
            'expected_hex_sha256': digest(expected_hex.encode()),
            'oracle_json_sha256':
            digest((folder / 'oracle.json').read_bytes()),
            'job_error_expected': int(name in ('reserved', 'memory_error'))
        }
        (folder / 'case.json').write_text(json.dumps(case, indent=2) + '\n')
        cases.append(case)
    (a.out / 'manifest.json').write_text(
        json.dumps({
            'oracle_source_sha256':
            digest(pathlib.Path(__file__).read_bytes()),
            'cases':
            cases
        },
                   indent=2) + '\n'
    )
    print(
        'Generated exact-value fixtures; actual trained weights, synthetic activations, separate edge/fault cases.'
    )


if __name__ == '__main__':
    main()
