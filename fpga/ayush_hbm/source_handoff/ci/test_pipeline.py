"""Fast tests of failure gates and opt-in AFI flow; never calls AWS."""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).with_name('pipeline.py'))
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class PipelineTests(unittest.TestCase):
    def test_log_rejects_errors_even_with_pass_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test.log'
            for error in ('ERROR: timing failed', 'FATAL: simulation', '%Error: assertion'):
                path.write_text(f'{error}\nPASS\n')
                with self.assertRaises(RuntimeError):
                    ci.check_log(path, ['PASS'])

    def test_log_requires_success_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'test.log'; path.write_text('No error, but simulation never finished\n')
            with self.assertRaises(RuntimeError):
                ci.check_log(path, ['PASS'])
            path.write_text('PASS\n'); ci.check_log(path, ['PASS'])

    def test_source_edits_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'tests').mkdir()
            path = root/'tests/design.sv'; path.write_text('before')
            before = ci.source_hashes(root)
            path.write_text('after')
            self.assertNotEqual(before, ci.source_hashes(root))

    def test_upload_does_not_create_afi_unless_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package = root/'image.tar'; package.write_bytes(b'test')
            args = argparse.Namespace(prefix='ci', bucket='example', region='us-east-1', create_afi=False)
            with patch.object(ci.subprocess, 'run') as upload, patch.object(ci, 'aws_json') as api:
                ci.publish(package, root, args, 'tag')
                upload.assert_called_once(); api.assert_not_called()

    def test_failed_afi_is_ci_failure_and_preserves_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); package = root/'image.tar'; package.write_bytes(b'test')
            args = argparse.Namespace(prefix='ci', bucket='example', region='us-east-1', create_afi=True, afi_timeout=10)
            with patch.object(ci.subprocess, 'run'), patch.object(ci, 'aws_json', side_effect=[
                {'FpgaImageId':'afi-test','FpgaImageGlobalId':'agfi-test'},
                {'FpgaImages':[{'State':{'Code':'failed','Message':'test failure'}}]}]):
                with self.assertRaises(RuntimeError):
                    ci.publish(package, root, args, 'tag')
            self.assertEqual(json.loads((root/'afi.json').read_text())['FpgaImageId'], 'afi-test')
            self.assertTrue((root/'afi-request.json').exists())


if __name__ == '__main__':
    unittest.main()
