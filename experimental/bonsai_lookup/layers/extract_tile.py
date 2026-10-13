#!/usr/bin/env python3
"""Extract one full-shape fixture tile for the unchanged PR6 RTL interface."""
import argparse
import hashlib
import json
import pathlib
import struct


def extract(root, role, mode, token0, row0, group, out):
    manifest = json.loads((root / 'manifest.json').read_text())
    record = next(
        r for r in manifest['records']
        if r['role'] == role and r['mode'] == mode
    )
    m, n, k, g = record['M'], record['N'], record['K'], record['groups_per_row'
                                                               ]
    if token0 % 8 or row0 % 32 or not (0 <= token0 < m and 0 <= row0 < n
                                       and 0 <= group < g):
        raise ValueError('Tile origin outside fixture or not aligned')
    tokens = min(8, m - token0)
    rows = min(32, n - row0)
    folder = root / role
    with (folder / 'weights.pq2').open('rb') as f:
        blocks = []
        for row in range(rows):
            f.seek(((row0 + row) * g + group) * 34)
            blocks.append(f.read(34))
    blocks += [bytes(2) + bytes([0x55]) * 32] * (32 - rows)
    memory = b''.join(b[2:] for b in blocks) + b''.join(b[:2] for b in blocks)
    with (folder / (mode + '.activations.i8')).open('rb') as f:
        for token in range(tokens):
            f.seek((token0 + token) * k + group * 128)
            memory += f.read(128)
    scales = []
    with (folder / (mode + '.activation_scales.f32')).open('rb') as f:
        for token in range(tokens):
            f.seek(((token0 + token) * g + group) * 4)
            scales.append(f.read(4))
    expected = []
    with (folder / (mode + '.group_dots.i32')).open('rb') as f:
        for token in range(tokens):
            for row in range(rows):
                f.seek((((token0 + token) * n + row0 + row) * g + group) * 4)
                dot = struct.unpack('<i', f.read(4))[0]
                expected.append({
                    'token':
                    token,
                    'row':
                    row,
                    'dot':
                    dot,
                    'scale_bits':
                    int.from_bytes(blocks[row][:2], 'little'),
                    'error':
                    0
                })
    out.mkdir(parents=True, exist_ok=True)
    (out / 'memory.hex').write_text(
        ''.join(
            f'{int.from_bytes(memory[i:i+16],"little"):032x}\n'
            for i in range(0, len(memory), 16)
        )
    )
    (out / 'expected.hex').write_text(
        ''.join(
            f'{(v["scale_bits"]<<32)|(v["dot"]&0xffffffff):016x}\n'
            for v in expected
        )
    )
    (out / 'oracle.json').write_text(json.dumps(expected, indent=2) + '\n')
    (out / 'activation_scales.f32').write_bytes(b''.join(scales))
    case = {
        'role': role,
        'mode': mode,
        'M': tokens,
        'N': rows,
        'K_valid': 128,
        'source_token_base': token0,
        'source_row_base': row0,
        'source_K_group': group,
        'model_sha256': manifest['model_sha256'],
        'weight_sha256': record['weight_sha256'],
        'scope':
        'One integer group tile. Activation scales are a SIDE INPUT for later software/scaled-layer reference, not consumed by current RTL.',
        'files': {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out.iterdir())
            if p.is_file() and p.name != 'case.json'
        }
    }
    (out / 'case.json').write_text(json.dumps(case, indent=2) + '\n')
    return case


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixtures', type=pathlib.Path, required=True)
    ap.add_argument(
        '--role',
        required=True,
        choices=['q', 'k', 'v', 'output', 'gate', 'up', 'down']
    )
    ap.add_argument('--mode', required=True, choices=['decode', 'prefill32'])
    ap.add_argument('--token-base', type=int, default=0)
    ap.add_argument('--row-base', type=int, default=0)
    ap.add_argument('--group', type=int, default=0)
    ap.add_argument('--out', type=pathlib.Path, required=True)
    args = ap.parse_args()
    print(
        json.dumps(
            extract(
                args.fixtures, args.role, args.mode, args.token_base,
                args.row_base, args.group, args.out
            ),
            indent=2
        )
    )
