"""Offline fault tests: no systemd, Docker, mounts, or real cgroup operations."""
import array
import fcntl
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'host')]
from deadline_guard import Guard, expire, validate_boundary

KEY = '123-1'
AGGREGATE = '/sys/fs/cgroup/coralnpu.slice/coralnpu-ci.slice'


def boundary():

    def record(path, inode):
        return {'path': path, 'device': 5, 'inode': inode}

    return {
        'run_key': KEY,
        'boot_id': 'test-boot',
        'deadline_boottime': 20,
        'aggregate': record(AGGREGATE, 1),
        'payload': record(AGGREGATE + '/coralnpu-ci-r123a1.slice', 2),
        'launcher': record(AGGREGATE + '/coralnpu-ci@123-1.service', 3)
    }


class Clock:

    def __init__(self):
        self.value = 10
        self.after_sleep = None

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        if self.after_sleep:
            self.after_sleep()


class Cgroups:

    def __init__(self):
        self.events = []
        self.frozen = False
        self.stopped = False
        self.failure_count = 0
        self.identity_changed = False
        self.keep_populated = False

    def write(self, record, name, value):
        self.events.append((record['inode'], name, value))
        if self.identity_changed:
            raise RuntimeError('pinned inode changed')
        if self.failure_count:
            self.failure_count -= 1
            raise OSError('injected cutoff write failure')
        if record['inode'] == 2:
            if name == 'cgroup.freeze':
                self.frozen = value == '1'
            if name == 'cgroup.kill' and not self.keep_populated:
                self.stopped = True

    def read(self, record, name):
        if self.identity_changed:
            raise RuntimeError('pinned inode changed')
        return '1' if self.frozen else '0'

    def empty(self, record):
        if self.identity_changed:
            raise RuntimeError('pinned inode changed')
        return self.stopped


class Store:

    def __init__(self, root):
        self.root = root
        self.state = 'running'
        self.cancel = False
        self.finishes = []
        self.quarantines = []

    def read_boundary(self, key):
        return boundary()

    def read_request(self, key):
        return {'run_id': '123', 'attempt': '1'}

    def read_status(self, key):
        return self.state

    def cancel_requested(self, key):
        return self.cancel

    def finish(self, request, outcome, **fields):
        self.state = outcome
        self.finishes.append((outcome, fields))

    def quarantine(self, key, error):
        self.quarantines.append(error)


class Connection:

    def __init__(
        self,
        message=b'bad',
        *,
        fd=None,
        uid=None,
        error=None,
        flags=0,
        disconnected=False
    ):
        self.message, self.fd, self.error, self.flags = message, fd, error, flags
        self.uid = os.getuid() if uid is None else uid
        self.disconnected = disconnected
        self.replies = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, value):
        pass

    def getsockopt(self, *args):
        return struct.pack('3i', os.getpid(), self.uid, os.getgid())

    def recvmsg(self, *args):
        if self.error:
            raise self.error
        ancillary = [] if self.fd is None else [(
            socket.SOL_SOCKET, socket.SCM_RIGHTS,
            array.array('i', [self.fd]).tobytes()
        )]
        return self.message, ancillary, self.flags, None

    def send(self, message):
        if self.disconnected:
            raise BrokenPipeError('client disconnected')
        self.replies.append(message)
        return len(message)


class Listener:

    def __init__(self, clock, events):
        self.clock, self.events = clock, events
        self.closed = False

    def bind(self, path):
        # Test-only stand-in so run() can apply its real private socket mode.
        Path(path).touch(exist_ok=False)
        self.path = path

    def listen(self, backlog):
        pass

    def settimeout(self, value):
        pass

    def accept(self):
        if not self.events:
            self.clock.sleep(.1)
            raise socket.timeout()
        event = self.events.pop(0)
        if callable(event):
            event = event()
        if isinstance(event, BaseException):
            raise event
        return event, None

    def close(self):
        self.closed = True


class BoundaryTests(unittest.TestCase):

    def test_exact_cgroup_paths_and_boot_are_required(self):
        validate_boundary(KEY, boundary(), 'test-boot')
        for mutator in (
                lambda b: b.update(boot_id='old-boot'),
                lambda b: b['payload'].update(path=AGGREGATE +
                                              '/someone-else.slice'),
                lambda b: b['launcher'].update(path=AGGREGATE +
                                               '/coralnpu-ci@124-1.service'),
                lambda b: b['payload'].update(inode=True),
                lambda b: b.update(deadline_boottime=float('nan')),
                lambda b: b.update(deadline_boottime=float('inf')),
                lambda b: b['aggregate'].update(
                    path='/sys/fs/cgroup/../coralnpu-ci.slice'),
        ):
            value = boundary()
            mutator(value)
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_boundary(KEY, value, 'test-boot')

    def test_cutoff_freezes_then_kills_payload_and_stuck_launcher(self):
        cg, clock = Cgroups(), Clock()
        expire(boundary(), cg, now=clock.now, sleep=clock.sleep)
        self.assertEqual(
            cg.events, [(2, 'cgroup.freeze', '1'), (2, 'cgroup.kill', '1'),
                        (3, 'cgroup.kill', '1')]
        )
        self.assertTrue(cg.frozen and cg.stopped)

    def test_one_failed_write_does_not_skip_other_cutoffs(self):
        cg = Cgroups()
        cg.failure_count = 1
        with self.assertRaisesRegex(RuntimeError, 'cutoff failed'):
            expire(boundary(), cg)
        self.assertEqual(len(cg.events), 3)

    def test_changed_inode_never_qualifies_as_stopped(self):
        cg = Cgroups()
        cg.identity_changed = True
        with self.assertRaisesRegex(RuntimeError, 'cutoff failed'):
            expire(boundary(), cg)
        self.assertFalse(cg.frozen or cg.stopped)

    def test_nonempty_payload_times_out_stop_proof(self):
        cg, clock = Cgroups(), Clock()
        cg.keep_populated = True
        with self.assertRaisesRegex(RuntimeError, 'stop unproven'):
            expire(boundary(), cg, now=clock.now, sleep=clock.sleep)
        self.assertGreaterEqual(clock.now(), 15)


class GuardTests(unittest.TestCase):

    def setUp(self):
        self.peer_option = patch(
            'deadline_guard.socket.SO_PEERCRED', 17, create=True
        )
        self.peer_option.start()
        self.addCleanup(self.peer_option.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.clock, self.cg = Clock(), Cgroups()
        self.lock_path = str(Path(self.temp.name) / 'hardware.lock')
        self.lock_fd = os.open(
            self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
        )
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lease = os.dup(self.lock_fd)
        self.listener = None
        self.guard = None

    def tearDown(self):
        for fd in (self.lease, self.lock_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        self.temp.cleanup()

    def run_guard(self, events):
        self.listener = Listener(self.clock, events)
        self.guard = Guard(
            self.store,
            cgroups=self.cg,
            boottime=self.clock.now,
            boot_id='test-boot',
            monotonic=self.clock.now,
            sleep=self.clock.sleep,
            socket_factory=lambda *args: self.listener,
            lock_path=self.lock_path,
            expected_owner_uid=os.getuid(),
            manage_signals=False
        )
        self.guard.run(KEY)

    def armed(self, **kwargs):
        return Connection(b'lease', fd=self.lease, **kwargs)

    def deadline(self):
        self.clock.value = 20
        return socket.timeout()

    def assert_closed(self):
        with self.assertRaises(OSError):
            os.fstat(self.lease)
        self.assertTrue(self.listener.closed)

    def test_deadline_returns_after_stop_proof_and_releases_lease(self):
        conn = self.armed()
        self.run_guard([conn, self.deadline])
        self.assertEqual(conn.replies, [b'armed'])
        self.assertEqual(self.store.state, 'timed_out')
        self.assertTrue(self.cg.frozen and self.cg.stopped)
        self.assertEqual(
            Path(self.listener.path).stat().st_mode & 0o777, 0o600
        )
        self.assert_closed()

    def test_cancellation_uses_same_direct_cutoff(self):

        def cancel():
            self.store.cancel = True
            return socket.timeout()

        self.run_guard([self.armed(), cancel])
        self.assertEqual(self.store.state, 'cancelled')
        self.assertEqual(self.cg.events[-1], (3, 'cgroup.kill', '1'))
        self.assert_closed()

    def test_receipt_survives_deadline_race(self):
        self.store.state = 'success'
        self.run_guard([self.armed(), self.deadline])
        self.assertEqual(self.store.state, 'success')
        self.assertEqual(self.store.finishes, [])
        self.assert_closed()

    def test_malformed_disconnected_and_timed_out_peers_do_not_drop_lease(
        self
    ):
        invalid = Connection(b'bad', disconnected=True)
        short = Connection(error=ConnectionResetError())
        timeout = Connection(error=socket.timeout())
        wrong_uid = Connection(uid=os.getuid() + 1)
        self.run_guard([
            self.armed(), invalid, short, timeout, wrong_uid, self.deadline
        ])
        self.assertEqual(self.store.state, 'timed_out')
        self.assertEqual(self.store.quarantines, [])
        self.assert_closed()

    def test_disconnect_after_fd_transfer_keeps_lease(self):

        def check_held_then_expire():
            os.fstat(self.lease)
            return self.deadline()

        self.run_guard([self.armed(disconnected=True), check_held_then_expire])
        self.assertEqual(self.store.state, 'timed_out')
        self.assert_closed()

    def test_unexpected_listener_failure_stops_before_releasing(self):
        self.run_guard_failure = RuntimeError('listener failed')
        with self.assertRaisesRegex(RuntimeError, 'listener failed'):
            self.run_guard([self.armed(), self.run_guard_failure])
        self.assertTrue(self.cg.frozen and self.cg.stopped)
        self.assertEqual(self.store.state, 'failed')
        self.assertTrue(self.store.quarantines)
        self.assert_closed()

    def test_failed_cleanup_keeps_fd_until_later_empty_proof(self):
        self.cg.failure_count = 1
        observed = []

        def verify_lease_still_held():
            os.fstat(self.lease)
            observed.append('held')
            self.assertTrue(self.store.quarantines)

        self.clock.after_sleep = verify_lease_still_held
        self.run_guard([self.armed(), self.deadline])
        self.assertTrue(observed)
        self.assertEqual(len(self.cg.events), 6)
        self.assert_closed()

    def test_complete_requires_terminal_frozen_empty(self):
        bad = Connection(b'complete')
        complete = Connection(b'complete')

        def make_terminal():
            self.store.state = 'success'
            self.cg.frozen = self.cg.stopped = True
            return complete

        self.run_guard([self.armed(), bad, make_terminal])
        self.assertEqual(bad.replies, [b'rejected'])
        self.assertEqual(complete.replies, [b'complete'])
        self.assertEqual(self.cg.events, [])
        self.assert_closed()

    def test_terminal_but_unfrozen_complete_is_rejected(self):
        self.store.state, self.cg.stopped = 'success', True
        bad = Connection(b'complete')
        self.run_guard([self.armed(), bad, self.deadline])
        self.assertEqual(bad.replies, [b'rejected'])
        self.assertTrue(self.cg.frozen)
        self.assert_closed()

    def test_signal_interrupt_stops_work_before_release(self):

        def interrupted():
            self.guard.interrupted = True
            return socket.timeout()

        self.run_guard([self.armed(), interrupted])
        self.assertEqual(self.store.state, 'failed')
        self.assertTrue(self.cg.frozen and self.cg.stopped)
        self.assert_closed()

    def test_lease_arriving_after_deadline_cannot_arm(self):
        conn = self.armed()

        def late():
            self.clock.value = 20
            return conn

        self.run_guard([late])
        self.assertEqual(conn.replies, [b'rejected'])
        self.assertEqual(self.store.state, 'timed_out')
        self.assert_closed()

    def test_wrong_fd_is_closed_and_cannot_arm(self):
        other_path = str(Path(self.temp.name) / 'other.lock')
        other_fd = os.open(
            other_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
        )
        bad = Connection(b'lease', fd=other_fd)
        self.run_guard([bad, self.armed(), self.deadline])
        self.assertEqual(bad.replies, [b'rejected'])
        with self.assertRaises(OSError):
            os.fstat(other_fd)
        self.assert_closed()

    def test_truncated_protocol_closes_ancillary_descriptors(self):
        extra = os.dup(self.lock_fd)
        bad = Connection(b'lease', fd=extra, flags=socket.MSG_CTRUNC)
        self.run_guard([bad, self.armed(), self.deadline])
        with self.assertRaises(OSError):
            os.fstat(extra)
        self.assertEqual(bad.replies, [b'rejected'])
        self.assert_closed()


if __name__ == '__main__':
    unittest.main()
