"""Validate core-simulation requests and complete independent readback checks."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from make_fixture import main, make_fixture
from test_runtime import fixture, reference


class FixtureTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='coral-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def create(self, **kwargs):
        out = self.root / str(len(list(self.root.iterdir())))
        path = make_fixture(out, **kwargs)
        result = json.loads(path.read_text())
        mailbox = struct.unpack('<32I', (out / 'mailbox.bin').read_bytes())
        for segment in result['segments']:
            self.assertEqual(
                hashlib.sha256((out / segment['file']).read_bytes()
                               ).hexdigest(), segment['sha256']
            )
        return out, result, mailbox

    def check_predictions(self, result, mailbox):
        """Check every row/address against independently evaluated prefixes."""
        encoding = 3 if result['encoding'] == 'pq2' else 2
        _, cfg, weights = fixture(
            encoding, encoding == 3, True, capacity=result['capacity']
        )
        checks = {item['address']: item for item in result['checks']}
        self.assertEqual(len(checks), len(result['checks']))
        floats = [item for item in checks.values() if 'float' in item]
        predictions = max(1, len(result['generated']))
        self.assertEqual(len(floats), cfg['vocab'] * predictions)
        for i in range(predictions):
            prefix = result['prompt'] + result['generated'][:i]
            golden = reference(cfg, weights, prefix)[10, cfg['n_layers'],
                                                     len(prefix) - 1]
            for j, expected in enumerate(golden):
                check = checks[mailbox[10] + (i * cfg['vocab'] + j) * 4]
                self.assertEqual(check['float'], expected)
                self.assertEqual((check['atol'], check['rtol']), (3e-5, 3e-5))
            if result['generated']:
                self.assertEqual(
                    checks[mailbox[18] + i * 4]['value'],
                    result['generated'][i]
                )
        self.assertEqual(checks[0x10030]['value'], result['completed_tokens'])
        self.assertEqual(checks[0x10050]['value'], len(result['generated']))
        self.assertEqual(checks[0x10054]['value'], result['stop_reason'])
        self.assertEqual(
            checks[0x10034]['value'],
            max(range(cfg['vocab']), key=golden.__getitem__)
        )

    def test_default_bf16_and_pq2_requests(self):
        for encoding, tokens in (('bf16', [7, 7, 7, 7]), ('pq2', [2, 2, 2,
                                                                  2])):
            with self.subTest(encoding=encoding):
                out, result, mailbox = self.create(encoding=encoding)
                self.assertEqual(result['prompt'], [1, 2])
                self.assertEqual(result['generated'], tokens)
                self.assertEqual(mailbox[:4], (0x434d5231, 2, 0, 0))
                self.assertEqual((
                    mailbox[9], mailbox[11], mailbox[14], mailbox[15],
                    mailbox[19]
                ), (2, 36, 8, 4, 4))
                self.assertEqual((out / 'tokens.bin').read_bytes(),
                                 struct.pack('<2I', 1, 2))
                self.assertEqual(
                    (result['completed_tokens'], result['stop_reason']),
                    (5, 1)
                )
                self.check_predictions(result, mailbox)

    def test_prompt_variation_and_repeatability(self):
        _, first, mailbox = self.create(prompt=[3])
        _, repeat, _ = self.create(prompt=[3])
        self.assertEqual(first, repeat)
        self.assertEqual(first['generated'], [2, 7, 7, 7])
        self.check_predictions(first, mailbox)

    def test_prompt_only_exact_capacity_keeps_one_prediction(self):
        for encoding in ('bf16', 'pq2'):
            with self.subTest(encoding=encoding):
                _, result, mailbox = self.create(
                    encoding=encoding,
                    prompt=[3, 1],
                    max_new_tokens=0,
                    capacity=2
                )
                self.assertEqual(result['generated'], [])
                self.assertEqual(
                    (result['completed_tokens'], result['stop_reason']),
                    (2, 0)
                )
                self.assertEqual((mailbox[11], mailbox[15], mailbox[19]),
                                 (9, 0, 0))
                self.assertFalse(
                    any(
                        item['address'] == mailbox[18]
                        for item in result['checks']
                    )
                )
                self.check_predictions(result, mailbox)

    def test_first_later_and_absent_eos(self):
        for kwargs, expected, reason in (({'eos_first':
                                           True}, [2], 2), ({'eos':
                                                             [7]}, [2, 7], 2),
                                         ({'eos':
                                           [2, 7]}, [2], 2), ({'eos':
                                                               [8]}, [2, 7, 7,
                                                                      7], 1)):
            with self.subTest(kwargs=kwargs):
                out, result, mailbox = self.create(prompt=[3], **kwargs)
                self.assertEqual(result['generated'], expected)
                self.assertEqual(result['stop_reason'], reason)
                self.assertEqual(
                    (out / 'eos.bin').read_bytes(),
                    struct.pack(f'<{len(result["eos"])}I', *result['eos'])
                )
                self.assertEqual(mailbox[16], len(result['eos']))
                self.check_predictions(result, mailbox)

    def test_exact_generation_capacity_and_supported_cap(self):
        _, result, mailbox = self.create(
            prompt=[3, 1, 2], max_new_tokens=4, capacity=6
        )
        self.assertEqual(result['completed_tokens'], 6)
        self.check_predictions(result, mailbox)
        _, maximum, mailbox = self.create(
            prompt=[1], max_new_tokens=1, capacity=2048
        )
        self.assertEqual(mailbox[14], 2048)
        self.assertEqual(maximum['completed_tokens'], 1)
        self.check_predictions(maximum, mailbox)

    def test_invalid_requests_leave_no_output(self):
        for kwargs in ({'capacity': 0}, {'capacity':
                                         2049}, {'max_new_tokens':
                                                 -1}, {'max_new_tokens':
                                                       9}, {'capacity': 4},
                       {'prompt': [1, 2, 3], 'capacity': 2, 'max_new_tokens':
                        0}, {'prompt': []}, {'prompt': [-1]}, {'prompt': [9]},
                       {'eos': [-1]}, {'eos': [9]}, {'eos': [2, 2]},
                       {'eos': [2] * 33}, {'eos_first': True, 'max_new_tokens':
                                           0}, {'eos_first': True, 'eos':
                                                [2]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                make_fixture(self.root / 'invalid', **kwargs)
            self.assertFalse((self.root / 'invalid').exists())

    def test_cli_variation_and_invalid_token_syntax(self):
        with contextlib.redirect_stdout(io.StringIO()):
            main([
                '--output',
                str(self.root / 'cli'), '--tokens', '3', '--capacity', '4',
                '--max-new-tokens', '4', '--eos', '7'
            ])
        result = json.loads((self.root / 'cli/fixture.json').read_text())
        self.assertEqual(result['generated'], [2, 7])
        with contextlib.redirect_stderr(
                io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(['--output', str(self.root / 'invalid'), '--tokens', '1,,2'])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse((self.root / 'invalid').exists())


if __name__ == '__main__':
    unittest.main()
