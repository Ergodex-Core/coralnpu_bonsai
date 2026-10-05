"""Independent CPU oracle compared to separate FP64 scalar fixture equations."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
try:
    import numpy as np
    from cpu_reference import Reference
except ImportError:
    np = None
from pack_model import sha256


@unittest.skipIf(np is None, 'NumPy is an optional CPU-reference dependency')
class ReferenceTests(unittest.TestCase):

    def test_cli_exports_prediction_rows_and_portable_trace_paths(self):
        from runtime.test_runtime import fixture, reference_generation
        image, cfg, weights = fixture(capacity=5)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model.bin').write_bytes(image)
            manifest = root / 'manifest.json'
            manifest.write_text(
                json.dumps({
                    'model_id':
                    'fixture',
                    'config':
                    cfg,
                    'segments': [{
                        'file': 'model.bin',
                        'bytes': len(image),
                        'sha256': sha256(root / 'model.bin')
                    }]
                })
            )
            for count in (0, 3):
                output = root / f'case-{count}' / 'report.json'
                trace = output.parent / 'trace'
                subprocess.run([
                    sys.executable,
                    str(Path(__file__).with_name('cpu_reference.py')),
                    str(manifest), '--tokens', '1,2', '--max-new-tokens',
                    str(count), '--eos', '', '--output',
                    str(output), '--trace-dir',
                    str(trace)
                ],
                               check=True,
                               capture_output=True,
                               text=True)
                report = json.loads(output.read_text())
                raw = output.parent / report['logits_file']
                self.assertFalse(Path(report['trace_directory']).is_absolute())
                self.assertEqual(report['logits_sha256'], sha256(raw))
                self.assertEqual(
                    raw.stat().st_size,
                    max(1, count) * cfg['vocab'] * 4
                )
                expected_ids, rows = reference_generation(
                    cfg, weights, [1, 2], count
                )
                self.assertEqual(report['generated_ids'], expected_ids)
                np.testing.assert_allclose(
                    np.fromfile(raw, dtype='<f4').reshape(-1, cfg['vocab']),
                    rows,
                    rtol=3e-5,
                    atol=3e-5
                )
            with self.assertRaisesRegex(ValueError, 'invalid prompt token'):
                Reference(manifest).generate([2**32], 1)
            with self.assertRaisesRegex(ValueError, 'invalid EOS token'):
                Reference(manifest).generate([1], 1, [-1])

    def test_three_formats_decode_and_repeatability(self):
        from runtime.test_runtime import fixture, reference
        for encoding, yarn, tied in [(1, False, False), (2, False, True),
                                     (3, True, True)]:
            image, cfg, weights = fixture(encoding, yarn, tied)
            with self.subTest(encoding=encoding
                              ), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / 'model.bin').write_bytes(image)
                (root / 'manifest.json').write_text(
                    json.dumps({
                        'model_id':
                        'fixture',
                        'segments': [{
                            'file': 'model.bin',
                            'bytes': len(image),
                            'sha256': sha256(root / 'model.bin')
                        }]
                    })
                )
                gold = reference(cfg, weights, [1, 2, 3])
                r = Reference(root / 'manifest.json', root / 'traces')
                for pos, token in enumerate([1, 2, 3]):
                    actual = r.step(token)
                    expected = gold[10, cfg['n_layers'], pos]
                    np.testing.assert_allclose(
                        actual, expected, rtol=3e-5, atol=3e-5
                    )
                    self.assertEqual(
                        int(np.argmax(actual)), int(np.argmax(expected))
                    )
                self.assertEqual(len(r.trace_records), 57)
                logits_records = [
                    v for v in r.trace_records if v['stage'] == 10
                ]
                self.assertEqual(len(logits_records), 3)
                for item in r.trace_records:
                    path = root / 'traces' / item['file']
                    self.assertEqual(path.stat().st_size, 4 * item['count'])
                    self.assertEqual(sha256(path), item['sha256'])
                np.testing.assert_array_equal(
                    np.fromfile(
                        root / 'traces' / logits_records[-1]['file'],
                        dtype='<f4'
                    ), actual
                )
                first = Reference(root / 'manifest.json').generate([1], 3, [])
                self.assertEqual(
                    first,
                    Reference(root / 'manifest.json').generate([1], 3, [])
                )
                self.assertEqual(first[2], 'length')
                stopped = Reference(root / 'manifest.json'
                                    ).generate([1], 3, [first[0][0]])
                self.assertEqual(stopped[0], [first[0][0]])
                self.assertEqual(stopped[2], 'eos')


if __name__ == '__main__': unittest.main()
