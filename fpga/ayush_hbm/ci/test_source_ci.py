"""Check exact checkout identity and the unprivileged workflow contract."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

import source_checks as checks


class SourceCITests(unittest.TestCase):

    def test_exact_sha_required(self):
        for value in ('main', 'a' * 39, 'A' * 40, 'a' * 40 + ';false'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                checks.validate_checkout(Path('/unused'), value)

    def test_wrong_or_dirty_checkout_rejected(self):
        for outputs in (['b' * 40 + '\n'], ['a' * 40 + '\n',
                                            ' M source\n'], ['a' * 40 + '\n',
                                                             '?? extra\n']):
            with self.subTest(outputs=outputs), \
                 patch('subprocess.check_output', side_effect=outputs), \
                 self.assertRaises(RuntimeError):
                checks.validate_checkout(Path('/unused'), 'a' * 40)

    def test_clean_exact_checkout(self):
        with patch('subprocess.check_output', side_effect=['a' * 40, '']):
            self.assertEqual(
                checks.validate_checkout(Path('/unused'), 'a' * 40), 'a' * 40
            )

    def test_failed_stage_leaves_failed_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'evidence'
            with patch.object(checks, 'validate_checkout', return_value='a' * 40), \
                 patch.object(checks, 'run', side_effect=subprocess.TimeoutExpired('test', 120)), \
                 self.assertRaises(subprocess.TimeoutExpired):
                checks.check('a' * 40, out)
            result = json.loads((out / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['checks'][0]['status'], 'failed')
            self.assertFalse(result['qualified_checkpoint'])
            self.assertEqual(result['hardware_calibration'], 'NOT_RUN')

    def test_existing_evidence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(
                FileExistsError):
            checks.check('a' * 40, Path(tmp))

    def test_workflow_has_no_privileged_or_licensed_execution(self):
        # BaseLoader preserves the YAML 'on' key and scalar spelling.
        data = yaml.load((checks.REPO / checks.WORKFLOW).read_text(),
                         Loader=yaml.BaseLoader)
        self.assertEqual(
            set(data['on']), {'pull_request', 'workflow_dispatch'}
        )
        self.assertEqual(data['permissions'], {'contents': 'read'})
        self.assertEqual(
            set(data['jobs']), {'source-checks', 'licensed-readiness'}
        )
        for job in data['jobs'].values():
            self.assertEqual(job['runs-on'], 'blacksmith-2vcpu-ubuntu-2404')
            self.assertNotIn('permissions', job)
            self.assertLessEqual(int(job['timeout-minutes']), 10)
            for step in job['steps']:
                if 'uses' in step:
                    self.assertRegex(
                        step['uses'], r'^actions/[a-z-]+@[0-9a-f]{40}$'
                    )
        checkout = data['jobs']['source-checks']['steps'][0]
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        self.assertEqual(
            checkout['with']['ref'],
            '${{ github.event.pull_request.head.sha || github.sha }}'
        )
        gate = data['jobs']['licensed-readiness']
        self.assertEqual(gate['needs'], 'source-checks')
        self.assertEqual(len(gate['steps']), 1)
        self.assertIn('HBM_TRUSTED_PROFILE_MISSING', gate['steps'][0]['run'])
        self.assertTrue(gate['steps'][0]['run'].rstrip().endswith('exit 1'))


if __name__ == '__main__':
    unittest.main()
