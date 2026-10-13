#!/usr/bin/env python3
"""Check native packing interpretation and immutable recovered source identity."""
import hashlib
import json
import pathlib
import struct
import tempfile
import unittest

from inspect_pq2 import inspect

ROOT = pathlib.Path(__file__).resolve().parents[2]


class AnalysisChecks(unittest.TestCase):

    def test_all_native_pq2_codes(self):
        # Independent GGUF v3 fixture: one row, 128 elements; each code 32 times.
        name = b'probe.weight'
        header = b'GGUF' + struct.pack('<IQQ', 3, 1, 0)
        header += struct.pack('<Q', len(name)) + name
        header += struct.pack('<IQQIQ', 2, 128, 1, 142, 0)
        header += bytes((-len(header)) % 32)
        # LSB-first pairs 00,01,10,11. Scale is metadata, never code bytes.
        block = struct.pack('<e', 0.5) + bytes([0xe4] * 32)
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / 'probe.gguf'
            path.write_bytes(header + block)
            result = inspect(path)
        self.assertEqual(result['code_counts'], [32, 32, 32, 32])
        self.assertEqual(result['code_to_weight'], [-1, 0, 1, 2])
        self.assertEqual(result['PQ2_payload_bytes'], 34)
        self.assertEqual(result['actual_block_dot_checks'], 6)

    def test_recovered_design_hashes(self):
        folder = ROOT / 'doc/microarch/ternary/recovered'
        for record in json.loads((folder / 'provenance.json').read_text()):
            with self.subTest(file=record['file']):
                digest = hashlib.sha256((folder / record['file']).read_bytes()
                                        ).hexdigest()
                self.assertEqual(digest, record['sha256'])


if __name__ == '__main__':
    unittest.main()
