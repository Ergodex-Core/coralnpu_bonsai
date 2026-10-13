#!/usr/bin/env python3
"""Read-only GGUF inventory and code census; no model code or device access."""
import collections
import hashlib
import json
import pathlib
import struct
import random


def inspect(path):
    with path.open('rb') as f:

        def read(fmt):
            return struct.unpack(
                '<' + fmt, f.read(struct.calcsize('<' + fmt))
            )[0]

        def string():
            return f.read(read('Q')).decode('utf8')

        fmts = {
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

        def value(t):
            if t in fmts: return read(fmts[t])
            if t == 8: return string()
            if t == 9:
                element = read('I')
                count = read('Q')
                return [value(element) for _ in range(count)]
            raise ValueError(t)

        assert f.read(4) == b'GGUF'
        version = read('I')
        assert version in (2, 3)
        nt = read('Q')
        nm = read('Q')
        meta = {}
        for _ in range(nm):
            name = string()
            meta[name] = value(read('I'))
        tensors = []
        for _ in range(nt):
            name = string()
            nd = read('I')
            dims = [read('Q') for _ in range(nd)]
            tensors.append({
                'name': name,
                'dims': dims,
                'type': read('I'),
                'relative_offset': read('Q')
            })
        alignment = meta.get('general.alignment', 32)
        start = (f.tell() + alignment - 1) // alignment * alignment
        counts = [0] * 4
        groups = 0
        shape_counts = collections.Counter()
        checks = 0
        rng = random.Random(103)
        tables = [
            bytes(
                sum(((b >> (2 * j)) & 3) == c
                    for j in range(4))
                for b in range(256)
            )
            for c in range(4)
        ]
        for t in tensors:
            if t['type'] != 142: continue
            k = t['dims'][0]
            n = t['dims'][1]
            assert k % 128 == 0
            shape_counts[(n, k)] += 1
            g = n * k // 128
            groups += g
            f.seek(start + t['relative_offset'])
            data = f.read(g * 34)
            assert len(data) == g * 34
            packed = b''.join(data[2 + i::34] for i in range(32))
            for c in range(4):
                counts[c] += sum(packed.translate(tables[c]))
            for block in [data[:34], data[-34:]]:
                x = [-128, 127, -1, 0
                     ] + [rng.randrange(-128, 128) for _ in range(124)]
                w = [((block[2 + j // 4] >> (2 * (j % 4))) & 3) - 1
                     for j in range(128)]
                ref = sum(a * b for a, b in zip(x, w))
                for width in [32, 64, 128]:
                    partials = [
                        sum(
                            0 if w[j] == 0 else x[j] if w[j] ==
                            1 else -x[j] if w[j] == -1 else 2 * x[j]
                            for j in range(i, i + width)
                        )
                        for i in range(0, 128, width)
                    ]
                    assert sum(partials) == ref
                    checks += 1
        f.seek(0)
        h = hashlib.sha256()
        for data in iter(lambda: f.read(1 << 20), b''):
            h.update(data)
    return {
        'file':
        path.name,
        'sha256':
        h.hexdigest(),
        'bytes':
        path.stat().st_size,
        'GGUF_version':
        version,
        'tensor_count':
        nt,
        'formats':
        dict(collections.Counter(t['type'] for t in tensors)),
        'PQ2_shapes': [{
            'N': n,
            'K': k,
            'count': c
        } for (n, k), c in shape_counts.items()],
        'PQ2_groups':
        groups,
        'PQ2_payload_bytes':
        groups * 34,
        'code_counts':
        counts,
        'code_to_weight': [-1, 0, 1, 2],
        'actual_block_dot_checks':
        checks,
        'widths': [32, 64, 128],
        'scope':
        'Actual native weight inventory and host dot equality; not RTL/model inference or activation quantizer validation'
    }


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('weights', type=pathlib.Path)
    ap.add_argument(
        '--output',
        type=pathlib.Path,
        default=pathlib.Path('results/pq2-inventory.json')
    )
    a = ap.parse_args()
    d = inspect(a.weights)
    a.output.write_text(json.dumps(d, indent=2) + '\n')
    print(json.dumps(d, indent=2))
