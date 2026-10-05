"""Focused acceptance and rejection tests for the proposed identity helper."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import stamp_identity as s

REFERENCE = '382b5c12030ad8eb74ab8deafb2529301faeab16'


def rtl(revision):
    words = [
        f"(kscm{i}En ? 32'h{(int(revision, 16) >> (32 * i)) & 0xffffffff:X} : 32'h0)"
        for i in range(5)
    ]
    return (
        'module Csr;\nassign value = ' + ' | '.join(words) + ';\nendmodule\n'
    ).encode()


class StampIdentityTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run([
            'git', '-C',
            str(self.repo), '-c', 'user.name=Stamp Test', '-c',
            'user.email=stamp@example.invalid', 'commit', '-q',
            '--allow-empty', '-m', 'fixture checkout'
        ],
                       check=True)
        self.head = subprocess.check_output([
            'git', '-C', str(self.repo), 'rev-parse', 'HEAD'
        ],
                                            text=True).strip()
        self.top = self.root / 'RvvCoreMiniAxi.sv'
        self.includes = self.root / 'include'
        self.includes.mkdir()
        self.csr = self.includes / 'Csr.sv'
        self.top.write_bytes(rtl(self.head))
        self.csr.write_bytes(rtl(self.head))
        (self.includes / 'Sram.v').write_bytes(b'module Sram; endmodule\n')
        inventory = {
            'Csr.sv':
            hashlib.sha256(rtl(REFERENCE)).hexdigest(),
            'Sram.v':
            hashlib.sha256((self.includes / 'Sram.v').read_bytes()).hexdigest()
        }
        self.pins = {
            'coral_reference_commit':
            REFERENCE,
            'reference_emitted_rtl_sha256':
            hashlib.sha256(rtl(REFERENCE)).hexdigest(),
            'reference_include_inventory_sha256':
            hashlib.sha256(
                json.dumps(inventory, sort_keys=True,
                           separators=(',', ':')).encode()
            ).hexdigest()
        }

    def check(self, **kwargs):
        return s.validate_emission(
            self.repo, self.top, self.includes, self.pins, **kwargs
        )

    def test_checkout_stamp_passes_and_artifacts_remain_byte_identical(self):
        before = {
            p: p.read_bytes()
            for p in [self.top, *self.includes.iterdir()]
        }
        result = self.check(
            pr_head_sha='a' * 40, github_checkout_sha=self.head
        )
        self.assertEqual(result['checkout_sha'], self.head)
        self.assertEqual(result['pr_head_sha'], 'a' * 40)
        self.assertEqual(
            result['raw_top_sha256'],
            hashlib.sha256(before[self.top]).hexdigest()
        )
        self.assertEqual(
            result['reference_stamp_top_sha256'],
            self.pins['reference_emitted_rtl_sha256']
        )
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_pinned_reference_stamp_itself_passes(self):
        self.pins['coral_reference_commit'] = self.head
        self.pins['reference_emitted_rtl_sha256'] = hashlib.sha256(
            rtl(self.head)
        ).hexdigest()
        inventory = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.includes.iterdir()
        }
        self.pins['reference_include_inventory_sha256'] = s._inventory_hash(
            inventory
        )
        result = self.check()
        self.assertEqual(
            result['raw_top_sha256'], result['reference_stamp_top_sha256']
        )

    def test_pr_head_is_never_accepted_in_place_of_checkout_stamp(self):
        self.top.write_bytes(rtl('a' * 40))
        with self.assertRaisesRegex(s.StampIdentityError, 'checked-out'):
            self.check(pr_head_sha='a' * 40)

    def test_wrong_github_checkout_context_is_fatal(self):
        with self.assertRaisesRegex(s.StampIdentityError, 'actual Git HEAD'):
            self.check(github_checkout_sha='a' * 40)

    def test_each_wrong_word_is_fatal_in_both_artifacts(self):
        for path in (self.top, self.csr):
            for index in range(5):
                with self.subTest(path=path.name, index=index):
                    wrong = int(self.head, 16) ^ (1 << (32 * index))
                    path.write_bytes(rtl(f'{wrong:040x}'))
                    with self.assertRaisesRegex(s.StampIdentityError,
                                                'checked-out'):
                        self.check()
                    path.write_bytes(rtl(self.head))

    def test_each_missing_or_duplicate_word_is_fatal(self):
        original = rtl(self.head)
        for path in (self.top, self.csr):
            for match in s.STAMP.finditer(original):
                for mode in ('missing', 'duplicate'):
                    with self.subTest(path=path.name, index=match['index'],
                                      mode=mode):
                        changed = original.replace(
                            match[0], b'0', 1
                        ) if mode == 'missing' else original + match[0]
                        path.write_bytes(changed)
                        with self.assertRaisesRegex(s.StampIdentityError,
                                                    'exactly one literal'):
                            self.check()
                        path.write_bytes(original)

    def test_any_other_top_byte_change_is_fatal(self):
        self.top.write_bytes(rtl(self.head) + b'// additional byte\n')
        with self.assertRaisesRegex(s.StampIdentityError, 'RTL differs'):
            self.check()

    def test_any_other_csr_byte_change_is_fatal(self):
        self.csr.write_bytes(rtl(self.head) + b'// additional byte\n')
        with self.assertRaisesRegex(s.StampIdentityError,
                                    'Include inventory differs'):
            self.check()

    def test_other_include_changed_added_or_removed_is_fatal(self):
        path = self.includes / 'Sram.v'
        original = path.read_bytes()
        for mode in ('changed', 'added', 'removed'):
            with self.subTest(mode=mode):
                if mode == 'changed':
                    path.write_bytes(original + b' ')
                elif mode == 'added':
                    (self.includes / 'extra.v').write_bytes(original)
                else:
                    path.unlink()
                with self.assertRaisesRegex(s.StampIdentityError,
                                            'Include inventory differs'):
                    self.check()
                path.write_bytes(original)
                (self.includes / 'extra.v').unlink(missing_ok=True)

    def test_malformed_or_noncanonical_literal_is_fatal(self):
        original = rtl(self.head)
        match = next(s.STAMP.finditer(original))
        for token in (b'123456789', b'x', match['word'].lower()):
            if token == match['word']:
                continue
            self.top.write_bytes(
                original[:match.start('word')] + token +
                original[match.end('word'):]
            )
            with self.assertRaises(s.StampIdentityError):
                self.check()

    def test_missing_csr_and_extra_stamp_are_fatal(self):
        self.csr.unlink()
        with self.assertRaisesRegex(s.StampIdentityError,
                                    'Missing include/Csr'):
            self.check()
        self.csr.write_bytes(rtl(self.head))
        (self.includes / 'Sram.v').write_bytes(rtl(self.head))
        with self.assertRaisesRegex(s.StampIdentityError,
                                    'Unexpected stamp outside'):
            self.check()


if __name__ == '__main__':
    unittest.main(verbosity=2)
