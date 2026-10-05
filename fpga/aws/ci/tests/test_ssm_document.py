"""SSM parameter constraints must work in both service and agent validators."""
import json
from pathlib import Path
import re
import unittest


class SsmDocumentTests(unittest.TestCase):

    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.document = json.loads(
            (root / 'ssm/CoralNpuFpgaCiBuild.json').read_text()
        )

    def test_request_bound_uses_document_length_constraint(self):
        parameter = self.document['parameters']['RequestBase64']
        self.assertEqual(parameter['minChars'], 1)
        self.assertEqual(parameter['maxChars'], 12000)
        self.assertEqual(parameter['interpolationType'], 'ENV_VAR')

        def accepted(value):
            return (
                parameter['minChars'] <= len(value) <= parameter['maxChars']
                and re.fullmatch(parameter['allowedPattern'],
                                 value) is not None
            )

        self.assertTrue(accepted('A' * 12000))
        self.assertTrue(accepted('YQ=='))
        for value in ('', 'A' * 12001, 'A===', ';id', 'A\nB', '$(id)'):
            with self.subTest(value_length=len(value)):
                self.assertFalse(accepted(value))

    def test_regex_counted_repetitions_fit_go_limit(self):
        # CreateDocument and SSM Agent reject counted repeats greater than 1000.
        # maxChars preserves the larger payload bound without such a regex.
        for parameter in self.document['parameters'].values():
            for count in re.findall(r'\{(\d+)(?:,(\d*))?\}',
                                    parameter['allowedPattern']):
                self.assertTrue(all(int(n) <= 1000 for n in count if n))

    def test_document_still_invokes_only_fixed_launcher(self):
        steps = self.document['mainSteps']
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['action'], 'aws:runShellScript')
        self.assertEqual(
            steps[0]['inputs']['runCommand'][-1],
            'exec /usr/local/libexec/coralnpu-ci-submit'
        )
        self.assertNotIn('{{', '\n'.join(steps[0]['inputs']['runCommand']))


if __name__ == '__main__':
    unittest.main()
