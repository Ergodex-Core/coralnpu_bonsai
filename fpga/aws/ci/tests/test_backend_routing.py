"""Exercise the actual credential-free workflow routing shell with fake outputs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

CI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))
WORKFLOW = Path(__file__).resolve().parents[4] / '.github/workflows/fpga.yml'


def readiness_script():
    text = WORKFLOW.read_text()
    section = text.split('  readiness:\n', 1)[1].split('  build:\n', 1)[0]
    return textwrap.dedent(section.split('        run: |\n', 1)[1])


class RoutingTests(unittest.TestCase):

    def run_route(self, backend, **values):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            summary = Path(directory) / 'summary'
            environment = {
                'PATH': os.environ['PATH'],
                'BACKEND': backend,
                'DIRECT_ENABLED': '',
                'SSM_ENABLED': '',
                'RUNNER': '',
                'HDK_ROOT': '',
                'VIVADO_SETTINGS': '',
                'IS_FORK': 'false',
                'GITHUB_OUTPUT': str(output),
                'GITHUB_STEP_SUMMARY': str(summary)
            }
            result = subprocess.run(['bash', '-c',
                                     readiness_script()],
                                    env=environment | values,
                                    capture_output=True,
                                    text=True,
                                    timeout=10)
            return result, output.read_text() if output.exists() else ''

    def test_default_source_only_is_explicitly_disabled(self):
        result, output = self.run_route('source-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no routed artifact', result.stdout)
        self.assertEqual(output, '')

    def test_unknown_and_unactivated_ssm_are_rejected(self):
        for backend in ('unexpected', 'ssm'):
            result, output = self.run_route(backend)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output, '')

    def test_ssm_handoff_does_not_select_direct_builder(self):
        result, output = self.run_route(
            'ssm', SSM_ENABLED='true', IS_FORK='true'
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output, 'backend=ssm\nenabled=true\n')

    def test_direct_requires_all_inputs_and_rejects_forks(self):
        values = {
            'DIRECT_ENABLED': 'true',
            'RUNNER': 'example-runner',
            'HDK_ROOT': '/example/hdk',
            'VIVADO_SETTINGS': '/example/settings64.sh'
        }
        for missing in values:
            result, _ = self.run_route('direct', **(values | {missing: ''}))
            self.assertNotEqual(result.returncode, 0)
        result, _ = self.run_route('direct', **(values | {'IS_FORK': 'true'}))
        self.assertNotEqual(result.returncode, 0)
        result, output = self.run_route('direct', **values)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(output, 'backend=direct\nenabled=true\n')

    def test_local_and_registry_image_pins_reject_mutable_tags(self):
        from test_transport import config_fixture
        from validate_bundle import activation_errors
        config = config_fixture()
        for image in ('sha256:' + 'b' * 64,
                      'registry.example/image@sha256:' + 'b' * 64):
            self.assertEqual(
                activation_errors(config | {'container_image_digest': image}),
                []
            )
        self.assertTrue(
            any(
                'container_image_digest' in item for item in activation_errors(
                    config | {'container_image_digest': 'image:latest'}
                )
            )
        )

    def test_example_never_claims_shared_physical_lock_integration(self):
        from validate_bundle import activation_errors
        config = json.loads((CI / 'config.example.json').read_text())
        self.assertFalse(config['shared_lock_integrated_with_physical_tests'])
        self.assertEqual(
            config['shared_lock'], '/run/lock/coralnpu-fpga-slot-0.lock'
        )
        self.assertTrue(
            any(
                'shared_lock_integrated_with_physical_tests' in value
                for value in activation_errors(config)
            )
        )


if __name__ == '__main__':
    unittest.main()
