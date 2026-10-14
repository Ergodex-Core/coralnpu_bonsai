"""Root-private durable requests, receipts and three-admission ledger."""
from contextlib import contextmanager
import fcntl
import stat
import json
import os
import uuid
from container_argv import run_key
from pilot_state import PilotState
from secure_files import open_trusted_directory, read_private_json, create_private_json


class RootStore:

    def __init__(self, root, pilot_id, *, expected_owner_uid=0):
        self.root, self.owner = root, expected_owner_uid
        self.fd = open_trusted_directory(
            root, expected_owner_uid=expected_owner_uid, private=True
        )
        self.ledger = PilotState(
            root, pilot_id, require_root=expected_owner_uid == 0
        )

    def _name(self, key, kind):
        return run_key(key) + '.' + kind + '.json'

    @contextmanager
    def request_lock(self, key):
        fd = os.open(
            self._name(key, 'mutex'),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(
                    info.st_mode
            ) or info.st_uid != self.owner or info.st_nlink != 1 or info.st_mode & 0o077:
                raise RuntimeError('untrusted request mutex')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def cancel_requested(self, key):
        try:
            marker = read_private_json(
                self.fd,
                self._name(key, 'cancel'),
                expected_owner_uid=self.owner,
                max_bytes=9000
            )
        except FileNotFoundError:
            return False
        if marker != self.read_request(key):
            raise RuntimeError('cancel tombstone differs from exact request')
        return True

    def request_cancel(self, key, request):
        self.create_request(key, request)
        create_private_json(
            self.fd,
            self._name(key, 'cancel'),
            request,
            expected_owner_uid=self.owner,
            max_bytes=9000
        )

    def create_request(self, key, request):
        return create_private_json(
            self.fd,
            self._name(key, 'request'),
            request,
            expected_owner_uid=self.owner,
            max_bytes=9000
        )

    def read_request(self, key):
        return read_private_json(
            self.fd,
            self._name(key, 'request'),
            expected_owner_uid=self.owner,
            max_bytes=9000
        )

    def _replace(self, key, kind, value):
        name = self._name(key, kind)
        temp = '.state-' + uuid.uuid4().hex
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        if len(raw) > 1000000:
            raise ValueError('trusted state exceeds limit')
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd
        )
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def read_status(self, key):
        try:
            terminal = read_private_json(
                self.fd,
                self._name(key, 'terminal'),
                expected_owner_uid=self.owner,
                max_bytes=1000000
            )
            if terminal['request'] != self.read_request(
                    key) or terminal['status'] not in {
                        'success', 'failed', 'timed_out', 'cancelled'
                    }:
                raise RuntimeError('invalid durable terminal identity')
            return terminal['status']
        except FileNotFoundError:
            pass
        try:
            return read_private_json(
                self.fd,
                self._name(key, 'status'),
                expected_owner_uid=self.owner
            )['status']
        except FileNotFoundError:
            return 'submitted'

    def set_status(self, key, status):
        if status not in {'running', 'success', 'failed', 'timed_out',
                          'cancelled', 'quarantined'}:
            raise ValueError('invalid state')
        self._replace(key, 'status', {'status': status})

    def admit(self, request):
        if any(name.endswith('.quarantine.json')
               for name in os.listdir(self.fd)):
            raise RuntimeError('operator must reconcile quarantined state')
        return self.ledger.admit(
            request['run_id'], request['attempt'], request['source_sha']
        )

    def record_boundary(self, key, boundary):
        create_private_json(
            self.fd,
            self._name(key, 'boundary'),
            boundary,
            expected_owner_uid=self.owner,
            max_bytes=16384
        )

    def read_boundary(self, key):
        return read_private_json(
            self.fd,
            self._name(key, 'boundary'),
            expected_owner_uid=self.owner,
            max_bytes=16384
        )

    def finish(self, request, outcome, *, receipt=None, error=None):
        key = request['run_id'] + '-' + request['attempt']
        if outcome not in {'success', 'failed', 'timed_out', 'cancelled'}:
            raise ValueError('invalid terminal outcome')
        with self.request_lock(key):
            if self.read_request(key) != request:
                raise RuntimeError('terminal request identity mismatch')
            status = self.read_status(key)
            if status in {'success', 'failed', 'timed_out', 'cancelled',
                          'quarantined'}:
                return status
            if status != 'running' and outcome not in {'failed', 'cancelled'}:
                raise RuntimeError('cannot succeed an unadmitted request')
            record = {
                'status': outcome,
                'request': request,
                'receipt': receipt,
                'error': (error or '')[:4096]
            }
            # The durable terminal receipt is authoritative. A crash afterward
            # may leave a stale running ledger, which safely blocks admission
            # until reconciliation; it cannot rewrite success as timeout.
            self._replace(key, 'terminal', record)
            try:
                self.ledger.finish(
                    request['run_id'], request['attempt'], outcome
                )
            except KeyError:
                if status == 'running' or outcome not in {'cancelled', 'failed'
                                                          }:
                    raise
                if outcome == 'cancelled' and not self.cancel_requested(key):
                    raise
            self.set_status(key, outcome)
            return outcome

    def quarantine(self, key, error):
        # First failure is retained; concurrent cleanup cannot turn this into an exception.
        with self.request_lock(key):
            try:
                read_private_json(
                    self.fd,
                    self._name(key, 'quarantine'),
                    expected_owner_uid=self.owner
                )
            except FileNotFoundError:
                create_private_json(
                    self.fd,
                    self._name(key, 'quarantine'),
                    {'error': str(error)[:4096]},
                    expected_owner_uid=self.owner
                )
            if self.read_status(key) not in {'success', 'failed', 'timed_out',
                                             'cancelled'}:
                self.set_status(key, 'quarantined')
