"""Offline supervisor tests: no Docker, mounts, systemd, AWS, or network."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'host'), str(ROOT / 'tests')]
from runtime import Runtime, Blocked, Cancelled
from store import RootStore
from protocol import encode_request, make_request
from test_transport import config_fixture, proof_fixture
from container_argv import stage_argv
from linux_adapter import LinuxAdapter, publish_gate


class Adapter:

    def __init__(self):
        self.events = []
        self.codes = {}
        self.fail = {}

    def action(self, name):
        self.events.append(name)
        if name in self.fail:
            raise self.fail[name]

    def start_launcher(self, key):
        self.action('start')

    def acquire_shared_lock(self):
        self.action('lock')
        return 5

    def release_shared_lock(self, fd):
        self.action('release')

    def validate_permit(self, request, now):
        self.action('permit')

    def assert_no_unmanaged_builds(self):
        self.action('colleagues')

    def validate_host_prerequisites(self):
        self.action('prerequisites')

    def ensure_bounded_filesystem(self):
        self.action('volume')

    def mark_admission(self):
        self.action('anchor')

    def prepare_workspace(self, key):
        self.action('workspace')
        return {}

    def create_boundary(self, key, deadline):
        self.action('boundary')
        return {'run_key': key}

    def arm_deadline(self, key, boundary, deadline):
        self.action('guard')

    def disarm_deadline(self, key):
        self.action('disarm')

    def assert_deadline(self, deadline):
        self.action('deadline')

    def assert_empty(self, boundary):
        self.action('empty')

    def run_phase(
        self, phase, request, paths, boundary, deadline, *, qualified=False
    ):
        self.action(phase)
        self.events.append((phase, qualified))
        return self.codes.get(phase, 0)

    def retire_guests(self, key, boundary, deadline):
        self.action('retire')

    def seal_inbox(self, paths, *, qualified):
        self.action('seal')
        return 8

    def close_inbox(self, fd):
        self.action('close')

    def emergency_stop(self, key, boundary):
        self.action('stop')

    def cancel_run(self, key, boundary):
        self.action('cancel_stop')


class RuntimeTests(unittest.TestCase):

    def setUp(self):
        self.config = config_fixture()
        self.config.update(fetch_network='coralnpu-ci-fetch-12345678')
        self.request = make_request(
            proof_fixture(self.config), self.config, '123', '1'
        )
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        # Secure traversal itself is covered by secure_files tests. Pin a local
        # fixture anchor because macOS /tmp is a symlink and tests are nonroot.
        with patch('store.open_trusted_directory',
                   side_effect=lambda path, **kw: os.open(path, os.O_RDONLY)):
            self.store = RootStore(
                str(self.directory),
                'unit-test',
                expected_owner_uid=os.getuid()
            )
        self.adapter = Adapter()
        self.published = []

        def publish(config, request, fd, trusted):
            self.adapter.action('publish')
            self.published.append(trusted)
            return {'test_receipt': True}

        self.runtime = Runtime(self.config, self.store, self.adapter, publish)

    def tearDown(self):
        os.close(self.store.fd)
        self.temp.cleanup()

    def env(self, operation='Build', request=None):
        return {
            'SSM_' + k: v[0]
            for k, v in encode_request(
                request or self.request, self.config, operation
            ).items()
        }

    def submit(self):
        return self.runtime.submit(self.env())

    def launch(self):
        self.submit()
        return self.runtime.launch('123-1')

    def test_disabled_build_has_no_store_or_adapter_effect(self):
        self.config['enabled'] = False
        with self.assertRaises(Blocked):
            self.submit()
        self.assertEqual(self.adapter.events, [])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cancel_before_admission_tombstone_prevents_late_build(self):
        self.runtime.submit(self.env('Cancel'))
        self.assertEqual(self.store.read_status('123-1'), 'cancelled')
        self.submit()
        self.assertEqual(self.adapter.events, [])
        self.assertFalse((self.directory / 'unit-test.json').exists())

    def test_cancel_remains_available_when_disabled(self):
        self.config['enabled'] = False
        self.runtime.submit(self.env('Cancel'))
        self.assertEqual(self.store.read_status('123-1'), 'cancelled')

    def test_changed_request_same_identity_rejected(self):
        self.submit()
        altered = copy.deepcopy(self.request)
        altered['eligibility']['exact_sha_approvers'] = ['someone-else']
        with self.assertRaises(Exception):
            self.runtime.submit(self.env('Cancel', altered))
        self.assertFalse(self.store.cancel_requested('123-1'))
        self.assertNotIn('cancel_stop', self.adapter.events)

    def test_wrong_cancel_key_rejected(self):
        with self.assertRaises(Blocked):
            self.runtime.cancel('124-1', self.request)
        self.assertEqual(self.adapter.events, [])

    def test_repeated_delivery_does_not_start_twice(self):
        self.submit()
        self.submit()
        self.assertEqual(self.adapter.events, ['start'])

    def test_launcher_start_failure_is_terminal_without_admission(self):
        self.adapter.fail['start'] = RuntimeError('systemd unavailable')
        with self.assertRaises(RuntimeError):
            self.submit()
        self.assertEqual(self.store.read_status('123-1'), 'failed')
        self.assertFalse((self.directory / 'unit-test.json').exists())

    def test_delayed_launcher_after_start_failure_cannot_admit(self):
        self.adapter.fail['start'] = RuntimeError('uncertain start')
        with self.assertRaises(RuntimeError):
            self.submit()
        result = self.runtime.launch('123-1')
        self.assertFalse(result['launched'])
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('lock', self.adapter.events)
        self.assertNotIn('stage', self.adapter.events)
        self.assertFalse((self.directory / 'unit-test.json').exists())

    def test_terminal_race_during_preflight_cannot_admit(self):
        self.submit()
        self.adapter.ensure_bounded_filesystem = lambda: self.store.finish(
            self.request, 'failed', error='uncertain start'
        )
        result = self.runtime.launch('123-1')
        self.assertFalse(result['launched'])
        self.assertNotIn('workspace', self.adapter.events)
        self.assertFalse((self.directory / 'unit-test.json').exists())

    def test_busy_lock_finishes_without_admission(self):
        self.adapter.fail['lock'] = BlockingIOError('held')
        with self.assertRaises(BlockingIOError):
            self.launch()
        self.assertEqual(self.store.read_status('123-1'), 'failed')
        self.assertFalse((self.directory / 'unit-test.json').exists())
        self.runtime.cleanup('123-1')

    def test_permit_denial_consumes_no_admission(self):
        self.adapter.fail['permit'] = RuntimeError('no handoff')
        with self.assertRaises(RuntimeError):
            self.launch()
        self.assertEqual(self.store.read_status('123-1'), 'failed')
        self.assertNotIn('workspace', self.adapter.events)
        self.assertFalse((self.directory / 'unit-test.json').exists())

    def test_success_publishes_only_after_retire_and_seal(self):
        result = self.launch()
        self.assertEqual(result['state'], 'success')
        self.assertTrue(self.published[0]['qualified'])
        events = self.adapter.events
        self.assertLess(events.index('guard'), events.index('stage'))
        self.assertLess(events.index('retire'), events.index('seal'))
        self.assertLess(events.index('seal'), events.index('publish'))
        self.runtime.cleanup('123-1')
        self.assertEqual(self.store.read_status('123-1'), 'success')

    def test_stage_failure_generates_evidence_without_build_or_vivado(self):
        self.adapter.codes['stage'] = 1
        result = self.launch()
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('build', self.adapter.events)
        self.assertIn(('qualify', False), self.adapter.events)
        self.assertIn(('collect', False), self.adapter.events)
        self.assertFalse(self.published[0]['qualified'])

    def test_failed_build_generates_evidence_only(self):
        self.adapter.codes['build'] = 1
        self.launch()
        self.assertIn(('qualify', False), self.adapter.events)
        self.assertFalse(self.published[0]['qualified'])

    def test_collector_nonzero_cannot_publish(self):
        self.adapter.codes['collect'] = 2
        with self.assertRaisesRegex(RuntimeError, 'collector'):
            self.launch()
        self.assertEqual(self.published, [])
        self.assertNotIn('seal', self.adapter.events)

    def test_guard_failure_starts_no_guest(self):
        self.adapter.fail['guard'] = RuntimeError('not armed')
        with self.assertRaises(RuntimeError):
            self.launch()
        self.assertNotIn('stage', self.adapter.events)
        self.assertEqual(self.store.read_status('123-1'), 'failed')

    def test_timeout_does_not_launch_collection(self):
        self.adapter.fail['build'] = TimeoutError('cutoff')
        with self.assertRaises(TimeoutError):
            self.launch()
        self.assertNotIn('qualify', self.adapter.events)
        self.assertNotIn('collect', self.adapter.events)
        self.assertEqual(self.published, [])
        self.assertEqual(self.store.read_status('123-1'), 'timed_out')

    def test_unproven_cleanup_quarantines_and_blocks_new_admission(self):
        self.adapter.fail.update(
            build=RuntimeError('failure'), stop=RuntimeError('populated')
        )
        with self.assertRaises(RuntimeError):
            self.launch()
        self.assertEqual(self.store.read_status('123-1'), 'quarantined')
        self.store.quarantine('123-1', 'second concurrent error')
        other = make_request(
            proof_fixture(self.config), self.config, '124', '1'
        )
        with self.assertRaisesRegex(RuntimeError, 'quarantined'):
            self.store.admit(other)

    def test_cancel_finished_success_preserves_outcome_and_does_not_stop(self):
        self.launch()
        self.runtime.submit(self.env('Cancel'))
        self.assertEqual(self.store.read_status('123-1'), 'success')
        self.assertNotIn('cancel_stop', self.adapter.events)

    def test_active_cancel_stops_exact_recorded_boundary(self):
        self.submit()
        self.store.admit(self.request)
        self.store.set_status('123-1', 'running')
        self.store.record_boundary('123-1', {'run_key': '123-1'})
        self.runtime.submit(self.env('Cancel'))
        self.assertIn('cancel_stop', self.adapter.events)
        self.assertEqual(self.store.read_status('123-1'), 'cancelled')

    def test_durable_terminal_survives_status_write_crash(self):
        self.submit()
        self.store.admit(self.request)
        self.store.set_status('123-1', 'running')
        with patch.object(self.store, 'set_status',
                          side_effect=OSError('crash point')):
            with self.assertRaises(OSError):
                self.store.finish(
                    self.request, 'success', receipt={'verified': True}
                )
        self.assertEqual(self.store.read_status('123-1'), 'success')
        self.assertEqual(
            self.store.finish(self.request, 'timed_out'), 'success'
        )
        self.assertEqual(
            json.loads((self.directory / '123-1.terminal.json').read_text()
                       )['receipt'], {'verified': True}
        )

    def test_quarantine_preserves_terminal_receipt(self):
        self.launch()
        self.store.quarantine('123-1', 'late cleanup uncertainty')
        self.assertEqual(self.store.read_status('123-1'), 'success')
        self.assertTrue((self.directory / '123-1.quarantine.json').exists())


class ContainerTests(unittest.TestCase):

    def setUp(self):
        self.config = config_fixture()
        self.config.update(fetch_network='coralnpu-ci-fetch-12345678')
        self.request = make_request(
            proof_fixture(self.config), self.config, '123', '1'
        )
        self.paths = {
            n: '/srv/coralnpu-ci/runs/123-1/' + n
            for n in (
                'source', 'metadata', 'build', 'qualification', 'inbox', 'tmp',
                'run', 'home', 'control'
            )
        }

    def inspected(self):
        args = stage_argv(self.config, self.request, 'build', self.paths)
        mounts = []
        for i, arg in enumerate(args):
            if arg != '--mount': continue
            fields = dict(
                x.split('=', 1) if '=' in x else (x, True)
                for x in args[i + 1].split(',')
            )
            mounts.append({
                'Type': 'bind',
                'Source': fields['src'],
                'Destination': fields['dst'],
                'RW': 'readonly' not in fields
            })
        return {
            'Id': 'f' * 64,
            'Config': {
                'Labels': {
                    'coralnpu.ci.run': '123-1',
                    'coralnpu.ci.phase': 'build'
                },
                'Image': self.config['container_image_digest'],
                'Entrypoint': ['/opt/coral-ci/bin/build'],
                'User': f'{self.config["host_uid"]}:{self.config["host_gid"]}'
            },
            'HostConfig': {
                'ReadonlyRootfs':
                True,
                'NetworkMode':
                'none',
                'CgroupParent':
                'coralnpu-ci-r123a1.slice',
                'IpcMode':
                'none',
                'CgroupnsMode':
                'private',
                'LogConfig': {
                    'Type': 'none'
                },
                'CapDrop': ['ALL'],
                'SecurityOpt': [
                    'no-new-privileges:true', 'apparmor=docker-default',
                    'seccomp={"defaultAction":"SCMP_ACT_ERRNO"}'
                ]
            },
            'AppArmorProfile': 'docker-default',
            'Mounts': mounts,
            'State': {
                'Running': True
            }
        }

    def verify_inspected(self, data):
        adapter = LinuxAdapter(self.config)
        adapter.docker = lambda *args, **kw: json.dumps([data])
        adapter.private_json = lambda path: {'defaultAction': 'SCMP_ACT_ERRNO'}
        return adapter._verify_container(
            'f' * 64, 'build', self.request, {}, self.paths, running=True
        )

    def test_actual_containment_and_mounts_match(self):
        self.verify_inspected(self.inspected())

    def test_readwrite_metadata_actual_mount_is_rejected(self):
        data = self.inspected()
        next(m for m in data['Mounts']
             if m['Destination'] == '/job/metadata')['RW'] = True
        with self.assertRaisesRegex(RuntimeError, 'mount'):
            self.verify_inspected(data)

    def test_missing_mount_is_rejected(self):
        data = self.inspected()
        data['Mounts'].pop()
        with self.assertRaisesRegex(RuntimeError, 'mount'):
            self.verify_inspected(data)

    def test_changed_security_or_namespace_is_rejected(self):
        for name, value in [('IpcMode', 'host'), ('CgroupnsMode', 'host'),
                            ('SecurityOpt', ['no-new-privileges:true'])]:
            with self.subTest(name=name):
                data = self.inspected()
                data['HostConfig'][name] = value
                with self.assertRaises(RuntimeError):
                    self.verify_inspected(data)
        data = self.inspected()
        data['AppArmorProfile'] = 'unconfined'
        with self.assertRaises(RuntimeError):
            self.verify_inspected(data)

    def test_shared_engine_rejected_before_command(self):
        self.config['docker_socket'] = 'unix:///var/run/docker.sock'
        with self.assertRaises(ValueError):
            stage_argv(self.config, self.request, 'build', self.paths)

        class NoRunner:

            def run(self, *args, **kw):
                raise AssertionError('must not execute')

        with self.assertRaises(RuntimeError):
            LinuxAdapter(self.config, runner=NoRunner()).docker(['info'])

    def test_phase_scratch_is_private_and_network_disabled_after_stage(self):
        for phase in ('stage', 'build', 'qualify', 'collect'):
            args = stage_argv(self.config, self.request, phase, self.paths)
            self.assertIn('unix:///run/coralnpu-ci/docker.sock', args)
            self.assertIn(
                'type=bind,src=' + self.paths['tmp'] + '/' + phase +
                ',dst=/job/tmp', args
            )
            self.assertIn(
                '--network=' +
                ('coralnpu-ci-fetch-12345678' if phase == 'stage' else 'none'),
                args
            )
            self.assertIn('--log-driver=none', args)
        args = stage_argv(
            self.config, self.request, 'qualify', self.paths, qualified=False
        )
        self.assertIn('--evidence-only', args)

    def test_local_content_id_is_accepted_and_mutable_tag_rejected(self):
        self.config['container_image_digest'] = 'sha256:' + '1' * 64
        self.assertIn(
            self.config['container_image_digest'],
            stage_argv(self.config, self.request, 'build', self.paths)
        )
        self.verify_inspected(self.inspected())
        self.config['container_image_digest'] = 'coralnpu:latest'
        with self.assertRaises(ValueError):
            stage_argv(self.config, self.request, 'build', self.paths)

    def test_local_image_id_preflight_rejects_mismatch(self):
        self.config['container_image_digest'] = 'sha256:' + '1' * 64
        adapter = LinuxAdapter(self.config)
        info = {
            'CgroupVersion': '2',
            'CgroupDriver': 'systemd',
            'DockerRootDir': '/srv/coralnpu-ci/engine/docker',
            'SecurityOptions': ['name=apparmor']
        }
        adapter.docker = lambda args: json.dumps(
            info if args[0] == 'info' else [{
                'Id': 'sha256:' + '2' * 64
            }]
        )
        adapter._systemd_cgroup = lambda unit: {
            'path':
            '/sys/fs/cgroup/coralnpu-ci.slice' if unit.endswith('.slice') else
            '/sys/fs/cgroup/coralnpu-ci.slice/' + unit
        }
        adapter.cg.verify_caps = lambda record: None
        with self.assertRaisesRegex(RuntimeError, 'content ID differs'):
            adapter.validate_host_prerequisites()

    def test_mutable_metadata_not_available_to_build(self):
        args = stage_argv(self.config, self.request, 'build', self.paths)
        self.assertIn(
            'type=bind,src=' + self.paths['metadata'] +
            ',dst=/job/metadata,readonly', args
        )
        self.assertNotIn('--privileged', args)


class GateAndClockTests(unittest.TestCase):

    def test_gate_is_readable_and_exclusive_under_service_umask(self):
        with tempfile.TemporaryDirectory() as temp:
            fd = os.open(temp, os.O_RDONLY)
            previous = os.umask(0o077)
            try:
                publish_gate(fd, '123-1 build ' + 'a' * 40 + '\n')
                gate = Path(temp) / 'go'
                self.assertEqual(gate.stat().st_mode & 0o777, 0o444)
                self.assertEqual(
                    gate.read_text(), '123-1 build ' + 'a' * 40 + '\n'
                )
                self.assertFalse((Path(temp) / '.go-pending').exists())
                with self.assertRaises(FileExistsError):
                    publish_gate(fd, 'replacement')
                self.assertTrue(gate.read_text().startswith('123-1 build '))
            finally:
                os.umask(previous)
                os.close(fd)

    def test_boottime_anchor_does_not_move_with_wall_clock(self):
        boot = [100.0]
        wall = [1000.0]
        adapter = LinuxAdapter({}, boot=lambda: boot[0], wall=lambda: wall[0])
        adapter.mark_admission()
        self.assertEqual(adapter.deadline_boot, 21700.0)
        wall[0] = -1000000.0
        boot[0] = 21700.0
        with self.assertRaises(TimeoutError):
            adapter.assert_deadline(22600.0)


if __name__ == '__main__': unittest.main()
