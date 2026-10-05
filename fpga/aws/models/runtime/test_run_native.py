"""Native validation driver integrity; no target or physical execution."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import run_native
from test_runtime import fixture, reference_generation


class NativeDriverTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        image, self.config, self.weights = fixture(capacity=5)
        (self.root / 'model.bin').write_bytes(image)
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(
            json.dumps({
                'model_id':
                'synthetic-only',
                'config':
                self.config,
                'segments': [{
                    'file': 'model.bin',
                    'bytes': len(image),
                    'sha256': hashlib.sha256(image).hexdigest()
                }]
            })
        )

    def args(self, *extra):
        return [
            str(self.manifest), '--tokens', '1,2', '--max-new-tokens', '3',
            '--eos', '', '--trace', '--compiler',
            os.environ.get('CC', 'cc'), '--output',
            str(self.root / 'native'), *extra
        ]

    def test_native_cli_records_all_predictions_and_source_identity(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run_native.main(self.args())
        report = json.loads((self.root / 'native/report.json').read_text())
        ids, expected = reference_generation(
            self.config, self.weights, [1, 2], 3
        )
        self.assertEqual(report['generated_ids'], ids)
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['sources_unchanged'])
        self.assertEqual(report['physical'], 'NOT_RUN')
        self.assertEqual(report['completed_tokens'], 4)
        self.assertEqual(len(report['trace']), 4 * 19)
        raw = (self.root / 'native/logits.f32').read_bytes()
        actual = struct.unpack(f'<{len(raw)//4}f', raw)
        for value, reference in zip(actual,
                                    [v for row in expected for v in row]):
            self.assertLess(
                abs(value - reference) / (1 + abs(reference)), 3e-5
            )
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), report['logits_sha256']
        )
        for item in report['trace']:
            data = (self.root / 'native' / item['file']).read_bytes()
            self.assertEqual(len(data), item['count'] * 4)
            self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])

    def test_token_ids_cannot_wrap_through_ctypes(self):
        for index, extra in enumerate((['--tokens', str(2**32 + 1)], ['--eos',
                                                                      '-1'])):
            with self.subTest(extra=extra), self.assertRaisesRegex(
                    ValueError, 'token outside vocabulary'):
                run_native.main(
                    self.args(
                        '--output', str(self.root / f'bad-{index}'), *extra
                    )
                )

    def test_callback_write_failure_cannot_report_success(self):
        original = Path.write_bytes

        def fail_trace(path, content):
            if path.parent.name == 'trace':
                raise OSError('injected trace write failure')
            return original(path, content)

        with mock.patch.object(Path, 'write_bytes',
                               fail_trace), contextlib.redirect_stdout(
                                   io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'trace capture'):
                run_native.main(self.args())
        report = json.loads((self.root / 'native/report.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['return_code'], 0)
        self.assertIn(
            'injected trace write failure', report['trace_errors'][0]
        )
        self.assertTrue((self.root / 'native/logits.f32').is_file())


if __name__ == '__main__':
    unittest.main()
