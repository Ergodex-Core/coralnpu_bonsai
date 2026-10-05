"""Adversarial file/identity tests; these do not assert licensed runtime readiness."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    'ci_entrypoint', HERE / 'entrypoint.py'
)
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class EntryTests(unittest.TestCase):

    def args(self, **overrides):
        result = dict(
            repository=ci.REPOSITORY,
            source_sha='a' * 40,
            source_ref='refs/pull/123/head',
            run_key='100-1',
            phase='build',
            evidence_only=False
        )
        result.update(overrides)
        return argparse.Namespace(**result)

    def test_identity_disallows_arbitrary_repository_ref_and_shell(self):
        ci.validate_identity(self.args())
        for field, value in [
            ('repository', 'evil/repo'), ('source_ref', 'main'),
            ('source_ref', 'refs/pull/1/head;sh'), ('source_sha', '../x'),
            ('run_key', '1-1/../../'), ('run_key', '01-1')
        ]:
            with self.subTest(field=field,
                              value=value), self.assertRaises(RuntimeError):
                ci.validate_identity(self.args(**{field: value}))

    def test_env_and_git_do_not_inherit_credentials_or_execution_config(self):
        with patch.dict(os.environ,
                        {'AWS_SECRET_ACCESS_KEY': 'do-not-inherit',
                         'PYTHONPATH': '/evil', 'GIT_CONFIG_COUNT': '1'}):
            env = ci.fixed_environment(Path('/job/tmp/home'))
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('GIT_CONFIG_COUNT', env)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')
        command = ci.git_command('fetch', 'origin', 'refs/pull/1/head')
        for value in ('core.hooksPath=/dev/null', 'protocol.allow=never',
                      'http.followRedirects=false', 'credential.helper=',
                      'fetch.recurseSubmodules=false'):
            self.assertIn(value, command)

    def test_special_hardlink_symlink_and_ancestor_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            p = root / 'file'
            p.write_bytes(b'abc')
            self.assertEqual(ci.read_regular(p, 4), b'abc')
            self.assertEqual(
                ci.hash_regular(p, 4)[0],
                hashlib.sha256(b'abc').hexdigest()
            )
            link = root / 'link'
            link.symlink_to(p)
            with self.assertRaises((OSError, RuntimeError)):
                ci.read_regular(link, 10)
            directory = root / 'directory'
            directory.mkdir()
            (directory / 'file').write_text('data')
            (root / 'ancestor').symlink_to(directory, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                ci.read_regular(root / 'ancestor/file', 10)
            hard = root / 'hard'
            os.link(p, hard)
            with self.assertRaises(RuntimeError):
                ci.read_regular(p, 10)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(RuntimeError):
                ci.read_regular(fifo, 10)

    def test_path_rejection(self):
        for name in ('../x', '/x', 'a/../b', '.git/config', 'a\nb', 'a//b',
                     './a'):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                ci.safe_relative(name)

    def test_genuine_git_inventory_and_postbuild_tampering(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source, metadata = root / 'source', root / 'metadata'
            source.mkdir()
            metadata.mkdir()
            (source / 'hello').write_text('hello\n')
            (source / 'alias').symlink_to('hello')
            env = dict(
                os.environ,
                GIT_CONFIG_NOSYSTEM='1',
                GIT_CONFIG_GLOBAL='/dev/null'
            )
            subprocess.run(['git', 'init', '-q'],
                           cwd=source,
                           env=env,
                           check=True)
            subprocess.run(['git', 'add', 'hello', 'alias'],
                           cwd=source,
                           env=env,
                           check=True)
            tree_id = subprocess.check_output(['git', 'write-tree'],
                                              cwd=source,
                                              env=env).decode().strip()
            tree = subprocess.check_output([
                'git', 'ls-tree', '-r', '-z', tree_id
            ],
                                           cwd=source,
                                           env=env)
            with patch.object(ci, 'SOURCE',
                              source), patch.object(ci, 'METADATA', metadata):
                entries = ci.staged_inventory(tree)
                receipt = dict(
                    repository=ci.REPOSITORY,
                    source_sha='a' * 40,
                    source_ref='refs/pull/123/head',
                    run_key='100-1',
                    files=entries
                )
                (metadata /
                 'staged-source.json').write_text(json.dumps(receipt))
                ci.read_staging(self.args())
                (source / 'hello').write_text('different\n')
                with self.assertRaises(RuntimeError):
                    ci.read_staging(self.args())

    def test_escaping_symlink_or_gitlink_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw).resolve()
            p = source / 'alias'
            p.symlink_to('/etc/passwd')
            data = b'/etc/passwd'
            oid = hashlib.sha1(
                b'blob ' + str(len(data)).encode() + b'\0' + data
            ).hexdigest()
            with patch.object(ci, 'SOURCE', source):
                with self.assertRaises(RuntimeError):
                    ci.staged_inventory(f'120000 blob {oid}\talias\0'.encode())
                with self.assertRaises(RuntimeError):
                    ci.staged_inventory(
                        ('160000 commit ' + 'a' * 40 + '\tmodule\0').encode()
                    )

    def test_evidence_is_bounded_regular_fixed_names(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / 'report'
            source.write_bytes(b'report\n')
            out = root / 'evidence.tar'
            ci.evidence_archive(out, [('reports/report.txt', source, 20)])
            with tarfile.open(out) as archive:
                self.assertEqual(
                    archive.getnames(),
                    ['reports/report.txt', 'evidence-omissions.json']
                )
                self.assertTrue(archive.getmembers()[0].isfile())
            ci.evidence_archive(
                root / 'oversized.tar', [('report', source, 2)]
            )
            with tarfile.open(root / 'oversized.tar') as archive:
                omissions = json.load(
                    archive.extractfile('evidence-omissions.json')
                )
                self.assertEqual(omissions[0]['name'], 'report')
                self.assertNotIn('report', archive.getnames())
            with self.assertRaises(RuntimeError):
                ci.evidence_archive(
                    root / 'escape.tar', [('../escape', source, 20)]
                )
            self.assertFalse((root / 'escape.tar').exists())

    def test_image_context_refuses_mutable_base(self):
        spec = importlib.util.spec_from_file_location(
            'prepare', HERE / 'prepare_context.py'
        )
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):
                prepare.prepare(Path(raw) / 'context', 'ubuntu:24.04')

    def test_missing_checkpoint_cannot_be_overridden_by_pr_pass_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            for name in ('qualification', 'build', 'metadata'):
                (root / name).mkdir()
            (root /
             'build/qualification.json').write_text('{"qualified":true}')
            fake_driver = argparse.Namespace(PINS={})
            with patch.object(ci, 'QUALIFICATION', root / 'qualification'), \
                    patch.object(ci, 'BUILD', root / 'build'), \
                    patch.object(ci, 'METADATA', root / 'metadata'), \
                    patch.object(ci, 'trusted_modules', return_value=(fake_driver, None, {})), \
                    patch.object(ci, 'read_staging', return_value={'tree_sha': 'b' * 40}):
                with self.assertRaises(RuntimeError):
                    ci.qualify(self.args())
            result = json.loads(
                (root / 'qualification/trusted-result.json').read_text()
            )
            self.assertFalse(result['qualified'])
            self.assertFalse(
                (root / 'qualification/output/checkpoint.tar').exists()
            )
            self.assertTrue(
                (root / 'qualification/output/evidence.tar').is_file()
            )

    def test_earlier_failure_still_yields_evidence_without_tool_execution(
        self
    ):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            for name in ('qualification', 'build', 'metadata'):
                (root / name).mkdir()
            (root / 'metadata/fetch.log').write_text('Git fetch failed\n')
            with patch.object(ci, 'QUALIFICATION', root / 'qualification'), \
                    patch.object(ci, 'BUILD', root / 'build'), \
                    patch.object(ci, 'METADATA', root / 'metadata'), \
                    patch.object(ci, 'trusted_modules', return_value=(None, None, {})), \
                    patch.object(ci, 'command') as command:
                with self.assertRaises(RuntimeError):
                    ci.qualify(self.args(evidence_only=True))
                command.assert_not_called()
            with tarfile.open(root / 'qualification/output/evidence.tar'
                              ) as archive:
                self.assertIn('staging-fetch.log', archive.getnames())
                self.assertIn('trusted-result.json', archive.getnames())


if __name__ == '__main__':
    unittest.main()
