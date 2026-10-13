#!/usr/bin/env python3
"""Host packing checks, scalar C validation and FPGA handoff fixtures; no FPGA I/O."""
import argparse
import collections
import ctypes
import hashlib
import json
import pathlib
import random
import struct
import subprocess
import tempfile

from layouts import HEADER, LAYOUTS, align, codes, decode, encode, shape_bytes

ROOT = pathlib.Path(__file__).resolve().parent
MODEL_SHA = 'de68ba48a8dacb21979915991e7741b917869d71410a370df951c0c3a237ae50'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gguf_tensor(path):
    """Read GGUF metadata, then one actual 2048x2048 PQ2 matrix, with full file SHA."""
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA:
            raise ValueError('Unexpected model SHA')
        f.seek(0)

        def read(fmt):
            return struct.unpack(
                '<' + fmt, f.read(struct.calcsize('<' + fmt))
            )[0]

        def string():
            return f.read(read('Q')).decode('utf8')

        formats = {
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
            if t in formats: return read(formats[t])
            if t == 8: return string()
            if t == 9:
                kind, count = read('I'), read('Q')
                return [value(kind) for _ in range(count)]
            raise ValueError(t)

        if f.read(4) != b'GGUF' or read('I') != 3:
            raise ValueError('Expected GGUFv3')
        nt, nm = read('Q'), read('Q')
        meta = {}
        for _ in range(nm):
            key = string()
            meta[key] = value(read('I'))
        tensors = []
        for _ in range(nt):
            name = string()
            dims = [read('Q') for _ in range(read('I'))]
            tensors.append((name, dims, read('I'), read('Q')))
        start = align(f.tell(), meta.get('general.alignment', 32))
        chosen = next(
            t for t in tensors if t[1] == [2048, 2048] and t[2] == 142
        )
        f.seek(start + chosen[3])
        raw = f.read(2048 * 2048 // 128 * 34)
    return chosen[0], raw, 2048, 2048


def expanded(raw):
    return b''.join(
        bytes((c - 1) & 255
              for c in codes(raw[i:i + 34])) + raw[i:i + 2]
        for i in range(0, len(raw), 34)
    )


def ccheck(lib, packed, expected, n, k, layout, order):
    source = ctypes.create_string_buffer(packed[HEADER:])
    output = ctypes.create_string_buffer(len(expected))
    rc = lib.bonsai_unpack(source, output, n, k, LAYOUTS[layout][0], order)
    if rc != 0 or output.raw != expected:
        raise AssertionError((layout, order, rc))


def fixtures():
    rng = random.Random(103)
    checks = 0
    for n, k in [(1, 128), (16, 256), (31, 128), (32, 256), (33, 256)]:
        raw = b''.join(
            struct.pack('<H', rng.randrange(65536)) + bytes(
                sum(rng.randrange(3) << (j * 2)
                    for j in range(4))
                for _ in range(32)
            )
            for _ in range(n * k // 128)
        )
        for layout in LAYOUTS:
            packed = encode(raw, n, k, layout)
            assert decode(packed) == raw
            broken = bytearray(packed)
            broken[HEADER] ^= 1
            try:
                decode(bytes(broken))
            except ValueError:
                pass
            else:
                raise AssertionError('Corruption accepted')
            checks += 1
    four = struct.pack('<H', 0x3c00) + bytes([0xe4]) * 32
    for layout in LAYOUTS:
        if layout == 'slab32_radix3':
            try:
                encode(four, 1, 128, layout)
            except ValueError:
                pass
            else:
                raise AssertionError('code3 silently changed')
        else:
            assert decode(encode(four, 1, 128, layout)) == four
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weights', type=pathlib.Path)
    ap.add_argument('--fixtures-out', type=pathlib.Path, required=True)
    ap.add_argument('--report', type=pathlib.Path, required=True)
    ap.add_argument('--cc', default='cc')
    a = ap.parse_args()
    a.fixtures_out.mkdir(parents=True, exist_ok=True)
    checks = fixtures()
    if a.weights:
        tensor, raw, n, k = gguf_tensor(a.weights)
        input_status = 'PASS_NATIVE_SHA'
    else:
        tensor, n, k = 'synthetic_ternary', 33, 256
        raw = b''.join(
            struct.pack('<H', 0x3800 + g % 2048) + bytes(
                sum(((i * 4 + j + g) % 3) << (j * 2)
                    for j in range(4))
                for i in range(32)
            )
            for g in range(n * k // 128)
        )
        input_status = 'SYNTHETIC_ONLY'
    expected = expanded(raw)
    (a.fixtures_out / 'native.bin').write_bytes(raw)
    (a.fixtures_out / 'expected-expanded.bin').write_bytes(expected)
    inventory = json.loads(
        (ROOT.parent / 'results/pq2-inventory.json').read_text()
    )
    model = []
    vectors = []
    with tempfile.TemporaryDirectory() as temp:
        library = pathlib.Path(temp) / 'kernels.so'
        subprocess.run([
            a.cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-shared',
            '-fPIC',
            str(ROOT / 'kernels.c'), '-o',
            str(library)
        ],
                       check=True)
        lib = ctypes.CDLL(str(library))
        lib.bonsai_unpack.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32
        ]
        for layout in LAYOUTS:
            packed = encode(raw, n, k, layout)
            assert decode(packed) == raw
            for order in (0, 1):
                ccheck(lib, packed, expected, n, k, layout, order)
            (a.fixtures_out / (layout + '.bin')).write_bytes(packed)
            sizes = shape_bytes(n, k, layout)
            vectors.append(
                dict(
                    layout=layout,
                    **sizes,
                    packed_sha256=sha(packed),
                    body_sha256=sha(
                        packed[HEADER:HEADER + sizes['body_bytes']]
                    ),
                    host_exact_output='PASS',
                    fpga_status='NOT_RUN',
                    core_hz=25000000,
                    fpga_cycles=None,
                    seconds=None,
                    useful_native_GB_per_s=None,
                    physical_read_GB_per_s=None,
                    output_sha256=sha(expected),
                    output_bytes=len(expected)
                )
            )
            total = collections.Counter()
            for shape in inventory['PQ2_shapes']:
                for key, v in shape_bytes(shape['N'], shape['K'],
                                          layout).items():
                    total[key] += v * shape['count']
            # Bundle header64 + directory of197 offset/length pairs16B, padded64.
            total['bundle_directory_bytes'] = 64 + align(197 * 16)
            total['bundle_bytes'
                  ] = total['file_bytes'] + total['bundle_directory_bytes']
            model.append(
                dict(
                    layout=layout,
                    **total,
                    ratio_to_native_payload=total['bundle_bytes'] /
                    inventory['PQ2_payload_bytes'],
                    scope=
                    'Exact shape-derived serialized size, not FPGA traffic or timing'
                )
            )
        # Independent copy/checksum validation, including a non-multiple-of-four word count.
        words = (ctypes.c_uint32 *
                 19)(*[((i + 1) * 0x1234567) & 0xffffffff for i in range(19)])
        copied = (ctypes.c_uint32 * 19)()
        lib.bonsai_copy_words.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t
        ]
        lib.bonsai_copy_words(words, copied, 19)
        assert list(words) == list(copied)
        lib.bonsai_read_words.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        lib.bonsai_read_words.restype = ctypes.c_uint32
        assert lib.bonsai_read_words(words, 19) == sum(words) & 0xffffffff
    result = {
        'scope':
        'Host lossless packing and scalar C correctness only. No FPGA or simulator execution.',
        'input_status': input_status,
        'model_sha256': MODEL_SHA if a.weights else None,
        'tensor': tensor,
        'N': n,
        'K': k,
        'native_tensor_sha256': sha(raw),
        'expected_expanded_sha256': sha(expected),
        'synthetic_tail_roundtrips': checks,
        'c_orders_per_layout': 2,
        'copy_and_checksum_host': 'PASS',
        'source_hashes': {
            p.name: sha(p.read_bytes())
            for p in
            (ROOT / 'layouts.py', ROOT / 'kernels.c', ROOT / 'experiment.py')
        },
        'vectors': vectors,
        'whole_model_sizes': model
    }
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(result, indent=2) + '\n')
    print(
        json.dumps({
            'input_status': input_status,
            'tensor': tensor,
            'host_checks': 'PASS',
            'FPGA': 'NOT_RUN',
            'sizes': model
        },
                   indent=2)
    )


if __name__ == '__main__':
    main()
