"""Independent BOOTTIME cutoff, outside the workload's memory/task limits.

The guard receives a duplicate of the launcher's locked open-file-description.
It never releases that lease until the pinned payload is permanently frozen and
empty. Broken clients are protocol failures, not reasons to stop guarding.
"""
import array
import fcntl
import math
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import time

from container_argv import run_key, slice_name
from linux_adapter import Cgroups, CGROOT

TERMINAL = frozenset({'success', 'failed', 'timed_out', 'cancelled'})
LOCK_PATH = '/run/lock/coralnpu-fpga-slot-0.lock'


def validate_boundary(key, boundary, boot_id):
    if boundary['run_key'] != run_key(key) or boundary['boot_id'] != boot_id:
        raise RuntimeError('stale boot/run boundary')
    for name in ('aggregate', 'payload', 'launcher'):
        record = boundary[name]
        if set(record) != {'path', 'device', 'inode'}:
            raise RuntimeError('unexpected cgroup identity fields')
        if any(type(record[field]) is not int or record[field] < 0
               for field in ('device', 'inode')):
            raise RuntimeError('invalid cgroup identity')
        path = record['path']
        if not isinstance(path, str) or any(part in ('', '.', '..')
                                            for part in path.split('/')[1:]):
            raise RuntimeError('noncanonical cgroup identity')
    aggregate = boundary['aggregate']['path']
    if not aggregate.endswith('/coralnpu-ci.slice'
                              ) or not aggregate.startswith(CGROOT + '/'):
        raise RuntimeError('unexpected aggregate identity')
    if boundary['payload']['path'] != aggregate + '/' + slice_name(key):
        raise RuntimeError('payload is not the exact dedicated run slice')
    if boundary['launcher'][
            'path'] != aggregate + '/coralnpu-ci@' + key + '.service':
        raise RuntimeError('launcher is not the exact request unit')
    deadline = boundary['deadline_boottime']
    if type(deadline) not in (
            float, int) or not math.isfinite(deadline) or deadline <= 0:
        raise RuntimeError('invalid absolute deadline')


def expire(boundary, cgroups, *, now=time.monotonic, sleep=time.sleep):
    """Attempt every cutoff even if another fails; never call Docker or systemd."""
    failures = []
    for record, field in ((boundary['payload'], 'cgroup.freeze'),
                          (boundary['payload'], 'cgroup.kill'),
                          (boundary['launcher'], 'cgroup.kill')):
        try:
            # Cgroups reopens no-follow and verifies device/inode on every write.
            cgroups.write(record, field, '1')
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise RuntimeError(
            'direct cgroup cutoff failed; keep lease and quarantine'
        ) from failures[0]
    end = now() + 5
    while True:
        frozen = cgroups.read(boundary['payload'], 'cgroup.freeze') == '1'
        if frozen and cgroups.empty(boundary['payload']):
            return
        if now() >= end:
            raise RuntimeError(
                'payload stop unproven; keep lease and quarantine'
            )
        sleep(.05)


class Guard:

    def __init__(
        self,
        store,
        *,
        cgroups=None,
        boottime=None,
        boot_id=None,
        monotonic=time.monotonic,
        sleep=time.sleep,
        socket_factory=socket.socket,
        lock_path=LOCK_PATH,
        expected_owner_uid=0,
        manage_signals=True
    ):
        self.store = store
        self.cg = cgroups or Cgroups()
        self.boottime = boottime or (
            lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
        )
        self.boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id'
                                       ).read_text().strip()
        self.monotonic, self.sleep = monotonic, sleep
        self.socket_factory, self.lock_path = socket_factory, lock_path
        self.owner, self.manage_signals = expected_owner_uid, manage_signals
        self.interrupted = False

    def _quarantine(self, key, error):
        try:
            self.store.quarantine(key, str(error))
        except Exception:
            # State storage failure is not permission to drop the in-memory lease.
            pass

    def _finish_cutoff(self, key, boundary, outcome, error):
        expire(boundary, self.cg, now=self.monotonic, sleep=self.sleep)
        # A deadline/cleanup race must not overwrite an already published result.
        if self.store.read_status(key) not in TERMINAL:
            self.store.finish(
                self.store.read_request(key), outcome, error=error
            )

    def _retain_until_stopped(self, key, boundary, outcome, error):
        """Fail closed indefinitely if the exact cgroup cannot be proven stopped."""
        while True:
            try:
                self._finish_cutoff(key, boundary, outcome, error)
                return
            except BaseException as exc:
                self._quarantine(key, exc)
                # No finally closes the lock in this loop. Signals also only set
                # an interrupt flag; they do not bypass containment proof.
                try:
                    self.sleep(.1)
                except BaseException:
                    pass

    @staticmethod
    def _reply(connection, message):
        try:
            connection.send(message)
        except (OSError, TimeoutError):
            pass

    def _receive(self, connection, key, boundary, lease_fd):
        """Return (lease_fd, complete); own/close every received ancillary FD."""
        received = []
        try:
            connection.settimeout(.1)
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12
            )
            if struct.unpack('3i', credentials)[1] != self.owner:
                raise RuntimeError('guard peer is not root')
            message, control, flags, _ = connection.recvmsg(
                64, socket.CMSG_SPACE(16 * array.array('i').itemsize)
            )
            for level, kind, payload in control:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array('i')
                    values.frombytes(
                        payload[:len(payload) - len(payload) % values.itemsize]
                    )
                    received.extend(values)
            if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                raise RuntimeError('truncated guard protocol')
            if message == b'lease' and lease_fd is None and len(received) == 1:
                if self.interrupted or self.boottime() >= boundary[
                        'deadline_boottime'] or self.store.cancel_requested(key
                                                                            ):
                    raise RuntimeError('request may no longer arm')
                fd = received[0]
                info = os.fstat(fd)
                expected = os.stat(self.lock_path, follow_symlinks=False)
                for entry in (info, expected):
                    if not stat.S_ISREG(
                            entry.st_mode
                    ) or entry.st_uid != self.owner or entry.st_nlink != 1:
                        raise RuntimeError('untrusted hardware lease FD')
                if (info.st_dev, info.st_ino) != (expected.st_dev,
                                                  expected.st_ino):
                    raise RuntimeError('wrong hardware lease FD')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Own the descriptor before sending: a disconnect after SCM_RIGHTS
                # cannot discard the only surviving copy of the hardware lease.
                lease_fd = received.pop()
                self._reply(connection, b'armed')
                return lease_fd, False
            if message == b'complete' and not received and lease_fd is not None:
                if self.store.read_status(key) not in TERMINAL:
                    raise RuntimeError('request lacks a terminal receipt')
                if not self.cg.empty(boundary['payload']) or self.cg.read(
                        boundary['payload'], 'cgroup.freeze') != '1':
                    raise RuntimeError('payload is not sealed and stopped')
                self._reply(connection, b'complete')
                return lease_fd, True
            raise RuntimeError('unexpected guard message')
        except Exception:
            self._reply(connection, b'rejected')
            return lease_fd, False
        finally:
            for fd in received:
                os.close(fd)

    def run(self, key):
        key = run_key(key)
        boundary = self.store.read_boundary(key)
        validate_boundary(key, boundary, self.boot_id)
        # RootStore has already verified this root-private directory. Tests use
        # the same interface with an isolated temporary directory.
        socket_path = str(Path(self.store.root) / (key + '.lease.sock'))
        listener = self.socket_factory(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lease_fd = None
        release_proven = False
        old_handlers = {}
        try:
            if self.manage_signals:

                def interrupt(signum, frame):
                    self.interrupted = True

                for signum in (signal.SIGTERM, signal.SIGINT):
                    old_handlers[signum] = signal.signal(signum, interrupt)
            # Never unlink an existing run's socket: it is a durable tombstone.
            listener.bind(socket_path)
            os.chmod(socket_path, 0o600)
            listener.listen(2)
            listener.settimeout(.1)
            while True:
                cancelled = self.store.cancel_requested(key)
                expired = self.boottime() >= boundary['deadline_boottime']
                if cancelled or expired or self.interrupted:
                    outcome = 'cancelled' if cancelled else (
                        'timed_out' if expired else 'failed'
                    )
                    reason = 'exact request cancelled' if cancelled else (
                        'absolute BOOTTIME cutoff'
                        if expired else 'guard interrupted'
                    )
                    self._retain_until_stopped(key, boundary, outcome, reason)
                    release_proven = True
                    return
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    lease_fd, release_proven = self._receive(
                        connection, key, boundary, lease_fd
                    )
                if release_proven:
                    return
        except BaseException as exc:
            # Unexpected listener/clock/storage errors must stop the pinned
            # workload, including a stuck launcher, before releasing its lease.
            self._quarantine(key, exc)
            self._retain_until_stopped(
                key, boundary, 'failed', 'guard failed: ' + str(exc)
            )
            release_proven = True
            raise
        finally:
            # A failure in resource cleanup must not skip the proof/lease rule.
            try:
                listener.close()
            finally:
                if lease_fd is not None and release_proven:
                    os.close(lease_fd)
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
            # The socket remains as a non-reusable tombstone for this run.
