"""Independent intake tests: synthetic pinned artifacts and real Git histories."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

import cached_rtl

TOP = 'RvvCoreMiniAxi.sv'
ZIP = 'RvvCoreMiniAxi.zip'
HEADER = 'VRvvCoreMiniAxi_parameters.h'
PARAMETERS = dict(
    xlen='32',
    rvvVlen='128',
    enableRvv='true',
    enableFloat='true',
    enableVerification='false',
    itcmSizeKBytes='8',
    dtcmSizeKBytes='32',
    fetchDataBits='128',
    lsuDataBits='128'
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def stamped_rtl(revision):
    # Deliberately independent of the validator's regexp and stamp helper.
    words = [revision[i:i + 8] for i in range(0, 40, 8)][::-1]
    expressions = [
        f"(kscm{i}En ? 32'h{int(word, 16):X} : 32'h0)"
        for i, word in enumerate(words)
    ]
    return (
        'module Csr;\nassign result = ' + ' | '.join(expressions) +
        ';\nendmodule\n'
    ).encode()


class CachedRTLTest(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='cached-rtl-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo, self.cache = self.root / 'repo', self.root / 'cache'
        self.repo.mkdir()
        self.cache.mkdir()
        self.git('init', '-q')
        sources = {
            'utils/scm_info.py': 'generator-v1\n',
            'utils/BUILD': 'tool-v1\n',
            'utils/get_workspace_status.sh': 'git rev-parse HEAD\n',
            'hdl/core.scala': 'core-v1\n',
            'rules/emit.bzl': 'rule-v1\n',
            '.bazelrc': 'build --stamp\n',
            'MODULE.bazel': 'module-v1\n',
            '.gitignore': 'ignored-output/\n'
        }
        for name, data in sources.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        self.emitter = self.commit('reference generation inputs')
        self.write_source('fpga/aws/candidate.txt', 'DDR wrapper candidate\n')
        self.candidate = self.commit('candidate wrapper only')
        (self.cache / TOP).write_bytes(stamped_rtl(self.emitter))
        self.includes = {
            'Csr.sv': stamped_rtl(self.emitter),
            'Sram.v': b'module Sram; endmodule\n'
        }
        self.includes.update({
            f'Part{i:03d}.sv': f'// part {i}\n'.encode()
            for i in range(228)
        })
        self.write_zip()
        (self.cache / HEADER).write_text(
            ''.join(f'#define KP_{k} {v}\n' for k, v in PARAMETERS.items())
        )
        self.pins = {
            'coral_reference_commit': self.emitter,
            'reference_emitted_rtl_sha256':
            sha((self.cache / TOP).read_bytes()),
            'reference_include_inventory_sha256': self.inventory_hash()
        }
        self.manifest = {
            'schema_version': 1,
            'kind': 'reference-rtl-cache-v1',
            'artifact_emission_sha': self.emitter,
            'include_count': 230,
            'files': {},
            'include_inventory_sha256': self.inventory_hash(),
            'hardware_parameters': dict(PARAMETERS),
            'generator_inputs': cached_rtl.closure(self.repo, self.emitter)
        }
        self.refresh_manifest()

    def git(self, *args):
        return subprocess.check_output([
            'git', '--no-optional-locks', '-C',
            str(self.repo), *args
        ],
                                       text=True,
                                       stderr=subprocess.PIPE).strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git(
            '-c', 'user.name=Cache Test', '-c',
            'user.email=cache@example.invalid', 'commit', '-q', '-m', message
        )
        return self.git('rev-parse', 'HEAD')

    def write_source(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)

    def write_zip(self, entries=None):
        with zipfile.ZipFile(self.cache / ZIP, 'w',
                             compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in (self.includes.items()
                               if entries is None else entries):
                archive.writestr(name, data)

    def inventory_hash(self):
        return sha(
            canonical({
                name: sha(data)
                for name, data in self.includes.items()
            })
        )

    def refresh_manifest(self):
        for name in (TOP, ZIP, HEADER):
            data = (self.cache / name).read_bytes()
            self.manifest['files'][name] = {
                'bytes': len(data),
                'sha256': sha(data)
            }
        raw = canonical(self.manifest)
        (self.cache / 'cache-manifest.json').write_bytes(raw)
        self.manifest_sha = sha(raw)

    def check(self, **overrides):
        args = dict(
            repo=self.repo,
            cache=self.cache,
            pins=self.pins,
            manifest_sha256=self.manifest_sha,
            expected_candidate_sha=self.candidate
        )
        args.update(overrides)
        return cached_rtl.validate_cache(**args)

    def test_accepts_reference_bytes_without_relabeling_or_mutation(self):
        before = {p.name: p.read_bytes() for p in self.cache.iterdir()}
        pins = dict(self.pins)
        receipt = self.check()
        self.assertNotEqual(self.emitter, self.candidate)
        self.assertEqual(receipt['artifact_emission_sha'], self.emitter)
        self.assertEqual(receipt['candidate_source_sha'], self.candidate)
        self.assertEqual(
            receipt['rtl_generation'],
            'cached_pinned_output_verified_not_regenerated'
        )
        self.assertEqual(receipt['physical_execution'], 'not_run')
        self.assertEqual(receipt['manifest_sha256'], self.manifest_sha)
        self.assertEqual(
            before, {p.name: p.read_bytes()
                     for p in self.cache.iterdir()}
        )
        self.assertEqual(self.pins, pins)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_manifest_tampering_rejected_by_separate_reviewed_digest(self):
        path = self.cache / 'cache-manifest.json'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'reviewed SHA'):
            self.check()

    def test_raw_artifact_tampering_rejected(self):
        for name in (TOP, ZIP, HEADER):
            with self.subTest(name=name):
                path = self.cache / name
                before = path.read_bytes()
                path.write_bytes(before + b'changed')
                with self.assertRaisesRegex(ValueError,
                                            'Artifact hash/size differs'):
                    self.check()
                path.write_bytes(before)

    def test_rehashed_top_cannot_replace_unchanged_reference_pin(self):
        path = self.cache / TOP
        path.write_bytes(path.read_bytes() + b'// changed RTL\n')
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'unchanged reference pin'):
            self.check()

    def test_rehashed_include_cannot_replace_unchanged_inventory_pin(self):
        self.includes['Sram.v'] += b'// changed SRAM\n'
        self.write_zip()
        self.manifest['include_inventory_sha256'] = self.inventory_hash()
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'Include inventory differs'):
            self.check()

    def test_header_hardware_configuration_is_checked_after_hashes(self):
        self.manifest['hardware_parameters']['itcmSizeKBytes'] = '64'
        path = self.cache / HEADER
        path.write_text(
            path.read_text().replace(
                'KP_itcmSizeKBytes 8', 'KP_itcmSizeKBytes 64'
            )
        )
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError,
                                    'hardware configuration differs'):
            self.check()

    def test_duplicate_parameter_definitions_are_rejected(self):
        path = self.cache / HEADER
        path.write_text('#define KP_xlen 64\n' + path.read_text())
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, '[Dd]uplicate|[Pp]arameter'):
            self.check()

    def test_manifest_cannot_relabel_artifact_as_candidate(self):
        self.manifest['artifact_emission_sha'] = self.candidate
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'exact pinned reference'):
            self.check()

    def test_wrong_candidate_head_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Candidate HEAD differs'):
            self.check(expected_candidate_sha=self.emitter)

    def test_stamps_are_checked_independently_of_raw_hashes(self):
        # Fixture pins are adjusted only to reach the stamp check. The emitter stays fixed.
        for target in ('top', 'csr'):
            with self.subTest(target=target):
                if target == 'top':
                    (self.cache / TOP).write_bytes(stamped_rtl(self.candidate))
                    self.pins['reference_emitted_rtl_sha256'] = sha(
                        stamped_rtl(self.candidate)
                    )
                else:
                    (self.cache / TOP).write_bytes(stamped_rtl(self.emitter))
                    self.pins['reference_emitted_rtl_sha256'] = sha(
                        stamped_rtl(self.emitter)
                    )
                    self.includes['Csr.sv'] = stamped_rtl(self.candidate)
                    self.write_zip()
                    self.pins['reference_include_inventory_sha256'
                              ] = self.inventory_hash()
                    self.manifest['include_inventory_sha256'
                                  ] = self.inventory_hash()
                self.refresh_manifest()
                with self.assertRaisesRegex(ValueError, 'stamp differs'):
                    self.check()

    def test_extra_cache_file_or_directory_is_rejected(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                extra = self.cache / 'unreviewed'
                extra.mkdir() if directory else extra.write_bytes(b'extra')
                with self.assertRaisesRegex(ValueError,
                                            'Unexpected cache files'):
                    self.check()
                extra.rmdir() if directory else extra.unlink()

    def test_include_count_and_unsafe_paths_are_rejected(self):
        entries = list(self.includes.items())
        for changed in (entries[:-1], entries + [('extra.sv', b'extra')],
                        entries[:-1] + [('../escape.sv', b'escape')],
                        entries[:-1] +
                        [('nested/Csr.sv', b'duplicate basename')]):
            with self.subTest(last=changed[-1][0], count=len(changed)):
                self.write_zip(changed)
                self.refresh_manifest()
                with self.assertRaisesRegex(ValueError,
                                            'Include count|Unsafe/duplicate'):
                    self.check()

    def test_committed_generator_changes_including_utils_are_rejected(self):
        for name in ('utils/scm_info.py', 'utils/BUILD',
                     'utils/get_workspace_status.sh', 'hdl/core.scala',
                     'rules/emit.bzl', '.bazelrc', 'MODULE.bazel'):
            with self.subTest(name=name):
                self.write_source(name, 'changed generator input\n')
                self.candidate = self.commit('change ' + name)
                with self.assertRaisesRegex(
                        ValueError, 'Generator/source closure differs'):
                    self.check()
                self.git('checkout', '-q', self.emitter, '--', name)
                self.candidate = self.commit('restore ' + name)

    def test_committed_fpga_and_github_changes_are_permitted(self):
        self.write_source('fpga/aws/candidate.txt', 'updated wrapper\n')
        self.write_source('.github/workflows/test.yml', 'workflow fixture\n')
        self.candidate = self.commit('allowed non-generator changes')
        self.assertEqual(self.check()['artifact_emission_sha'], self.emitter)

    def test_dirty_tracked_untracked_and_ignored_files_are_rejected(self):
        for name in ('utils/scm_info.py', 'untracked.txt',
                     'ignored-output/cache.bin'):
            with self.subTest(name=name):
                path = self.repo / name
                before = path.read_bytes() if path.exists() else None
                self.write_source(name, 'dirty input\n')
                with self.assertRaisesRegex(ValueError,
                                            'checkout is not clean'):
                    self.check()
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
