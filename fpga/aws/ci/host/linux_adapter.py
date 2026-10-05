"""Concrete Linux adapter. Every command is fixed and bounded; none runs PR code.

This module is only installed after qualification. Offline tests inject Runner,
filesystem/cgroup adapters and clocks; importing it has no operating-system effects.
"""
import array
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import subprocess
import time

from container_argv import stage_argv, slice_name, run_key, absolute_path
from secure_files import open_trusted_directory, read_private_json, seal_inbox, verify_sealed_inbox

CGROOT = '/sys/fs/cgroup'
EXECROOT = '/usr/local/libexec/coralnpu-ci'
PYTHON = '/opt/coralnpu-ci/venv/bin/python'
ENV = {
    'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
    'LANG': 'C',
    'LC_ALL': 'C',
    'HOME': '/var/lib/coralnpu-ci'
}


class Runner:
    """Bound stdout/stderr and client lifetime without relaying guest output."""

    def run(self, argv, *, timeout=30, pass_fds=()):
        if not argv or not all(isinstance(arg, str) and '\x00' not in arg
                               for arg in argv):
            raise ValueError('literal argv required')
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=ENV,
            start_new_session=True,
            pass_fds=pass_fds
        )
        output = [bytearray(), bytearray()]
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, 0)
        selector.register(proc.stderr, selectors.EVENT_READ, 1)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError('trusted command timed out: ' + argv[0])
                for event, _ in selector.select(min(.2, deadline -
                                                    time.monotonic())):
                    data = os.read(event.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(event.fileobj)
                    else:
                        output[event.data].extend(data)
                        if sum(map(len, output)) > 1048576:
                            raise RuntimeError(
                                'trusted command output exceeded 1 MiB'
                            )
            proc.wait(timeout=max(.001, deadline - time.monotonic()))
            if proc.returncode:
                raise RuntimeError(
                    f'trusted command failed: {argv[0]} ({proc.returncode})'
                )
            return output[0].decode('utf-8', errors='strict').strip()
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
            raise
        finally:
            selector.close()
            proc.stdout.close()
            proc.stderr.close()


def identity(path):
    fd = open_trusted_directory(path)
    try:
        info = os.fstat(fd)
        return {'path': path, 'device': info.st_dev, 'inode': info.st_ino}
    finally:
        os.close(fd)


class Cgroups:
    """Only pinned run/launcher cgroups; never enumerates or kills other slices."""

    def open(self, record):
        path = record['path']
        if not isinstance(path, str) or not path.startswith(CGROOT + '/'):
            raise ValueError('invalid cgroup root')
        fd = open_trusted_directory(path)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (record['device'], record['inode']):
            os.close(fd)
            raise RuntimeError('cgroup identity changed')
        return fd

    def read(self, record, name):
        if name not in {'cpu.max', 'memory.max', 'memory.swap.max', 'pids.max',
                        'cgroup.events', 'cgroup.freeze'}:
            raise ValueError('unapproved cgroup property')
        fd = self.open(record)
        try:
            value_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                return os.read(value_fd, 4096).decode().strip()
            finally:
                os.close(value_fd)
        finally:
            os.close(fd)

    def write(self, record, name, value):
        if (name, value) not in {('cgroup.freeze', '1'), ('cgroup.kill', '1')}:
            raise ValueError('only permanent freeze and kill are supported')
        fd = self.open(record)
        try:
            target = os.open(name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if os.write(target, value.encode()) != len(value):
                    raise RuntimeError('short cgroup control write')
            finally:
                os.close(target)
        finally:
            os.close(fd)

    def empty(self, record):
        values = dict(
            line.split()
            for line in self.read(record, 'cgroup.events').splitlines()
        )
        return values.get('populated') == '0'

    def verify_caps(self, record):
        cpu = self.read(record, 'cpu.max').split()
        if len(cpu) != 2 or cpu[0] == 'max' or int(cpu[0]) / int(cpu[1]) > 16:
            raise RuntimeError('CPU boundary not enforced')
        for name, maximum in [('memory.max', 68719476736), ('pids.max', 4096)]:
            value = self.read(record, name)
            if value == 'max' or not 0 < int(value) <= maximum:
                raise RuntimeError(name + ' boundary not enforced')
        if self.read(record, 'memory.swap.max') != '0':
            raise RuntimeError('swap boundary not enforced')


def canonical_rules(value):
    if isinstance(value, dict):
        return {
            key: canonical_rules(item)
            for key, item in value.items()
            if key not in {'handle', 'packets', 'bytes'}
        }
    if isinstance(value, list):
        return [canonical_rules(item) for item in value]
    return value


def publish_gate(directory_fd, content):
    """Publish a complete readable gate despite the root service's 0077 umask."""
    fd = os.open(
        '.go-pending',
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd
    )
    try:
        data = content.encode('ascii')
        if os.write(fd, data) != len(data):
            raise RuntimeError('short gate write')
        os.fchmod(fd, 0o444)
        os.fsync(fd)
        # Only this root launcher can write this directory, under the shared
        # lease; no second writer or guest can race the existing-gate check.
        try:
            os.stat('go', dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError('containment gate already published')
        os.rename(
            '.go-pending',
            'go',
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd
        )
        os.fsync(directory_fd)
    finally:
        os.close(fd)


class LinuxAdapter:

    def __init__(
        self,
        config,
        *,
        runner=None,
        cgroups=None,
        wall=time.time,
        boot=None,
        volume=None
    ):
        self.config = config
        self.runner = runner or Runner()
        self.cg = cgroups or Cgroups()
        self.wall = wall
        self.boot = boot or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self.volume = volume
        self.lock_fd = None
        self.containers = {}
        self.workspace_identities = {}
        self.deadline_boot = None

    def command(self, argv, deadline=None, timeout=30):
        remaining = timeout if deadline is None else min(
            timeout, deadline - self.wall()
        )
        if remaining <= 0:
            raise TimeoutError('absolute run deadline reached')
        return self.runner.run(argv, timeout=remaining)

    def docker(self, args, deadline=None, timeout=30):
        if self.config.get('docker_socket'
                           ) != 'unix:///run/coralnpu-ci/docker.sock':
            raise RuntimeError('fixed private Docker socket required')
        return self.command([
            '/usr/bin/docker', '--host', self.config['docker_socket'], *args
        ], deadline, timeout)

    def acquire_shared_lock(self):
        if os.geteuid() != 0:
            raise PermissionError('root launcher required')
        # /run/lock is conventionally root-owned sticky-writable. The existing
        # root-owned single-link lock inode is opened no-follow and never replaced.
        parent = os.open(
            '/run/lock', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            p = os.fstat(parent)
            if p.st_uid != 0 or (p.st_mode & 0o022
                                 and not p.st_mode & stat.S_ISVTX):
                raise RuntimeError('untrusted shared-lock directory')
            fd = os.open(
                'coralnpu-fpga-slot-0.lock',
                os.O_RDWR | os.O_NOFOLLOW,
                dir_fd=parent
            )
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(
                    info.st_mode
            ) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
                raise RuntimeError('shared lock inode is not trusted')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.lock_fd = fd
        return fd

    def release_shared_lock(self, fd):
        # Do not LOCK_UN: watcher holds the same open-file-description lease.
        os.close(fd)
        self.lock_fd = None

    def private_json(self, path):
        path = Path(absolute_path(path))
        parent = open_trusted_directory(str(path.parent), private=True)
        try:
            return read_private_json(parent, path.name)
        finally:
            os.close(parent)

    def validate_permit(self, request, now):
        permit = self.private_json(self.config['scheduling_permit_path'])
        expected = {
            'run_id', 'attempt', 'source_sha', 'not_before', 'expires_at',
            'operator', 'exclusive_window', 'colleague_handoff'
        }
        if set(permit) != expected:
            raise RuntimeError('scheduling permit schema differs')
        for key in ('run_id', 'attempt', 'source_sha'):
            if permit[key] != request[key]:
                raise RuntimeError(
                    'scheduling permit belongs to another request'
                )
        if permit['exclusive_window'] is not True or permit['colleague_handoff'
                                                            ] is not True:
            raise RuntimeError('explicit colleague scheduling handoff missing')
        if not isinstance(permit['operator'], str) or not re.fullmatch(
                r'[A-Za-z0-9_.@-]{1,100}', permit['operator']):
            raise RuntimeError('missing operator identity')
        if any(type(permit[key]) not in (int, float)
               for key in ('not_before', 'expires_at')):
            raise RuntimeError('permit timestamps invalid')
        if not now - 300 <= permit[
                'not_before'] <= now or not now + 21600 <= permit[
                    'expires_at'] <= permit['not_before'] + 21900:
            raise RuntimeError(
                'permit does not cover one current six-hour window'
            )

    def assert_no_unmanaged_builds(self):
        # A scan is supplementary to the explicit permit, never authorization.
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():
                continue
            try:
                command = (path / 'comm').read_text().strip().lower()
                uid = (path / 'status').read_text().split('Uid:')[1].split()[0]
            except FileNotFoundError:
                continue
            if command in {'vivado', 'bazel', 'bazelisk', 'verilator', 'v++',
                           'vitis', 'gcc', 'g++', 'clang', 'clang++'}:
                raise RuntimeError('active unmanaged build prevents admission')
            if int(uid) == self.config['host_uid']:
                raise RuntimeError('dedicated CI uid already owns a process')

    def verify_network(self):
        proof = self.private_json(
            self.config['staging_network_qualification_path']
        )
        if set(proof) != {'network_id', 'name', 'bridge', 'rules_sha256',
                          'allowed_https_hostnames', 'ipv6_disabled',
                          'negative_probes_passed'}:
            raise RuntimeError('network qualification schema differs')
        if proof['name'] != self.config['fetch_network'] or proof[
                'allowed_https_hostnames'] != ['github.com']:
            raise RuntimeError(
                'staging destination differs from fixed GitHub remote'
            )
        if proof['ipv6_disabled'] is not True or proof['negative_probes_passed'
                                                       ] is not True:
            raise RuntimeError('network probes/IPv6 boundary unqualified')
        network = json.loads(
            self.docker(['network', 'inspect', proof['name']])
        )
        if len(network) != 1:
            raise RuntimeError('network identity ambiguous')
        network = network[0]
        if network['Id'] != proof['network_id'] or network[
                'Driver'] != 'bridge' or network['EnableIPv6'] is not False:
            raise RuntimeError('staging network identity changed')
        if network['Options'].get('com.docker.network.bridge.name') != proof[
                'bridge'] or network.get('Containers'):
            raise RuntimeError('unqualified bridge or existing network peers')
        actual = json.loads(
            self.command([
                '/usr/sbin/nft', '--json', 'list', 'table', 'inet',
                'coralnpu_ci'
            ])
        )
        digest = hashlib.sha256(
            json.dumps(
                canonical_rules(actual), sort_keys=True, separators=(',', ':')
            ).encode()
        ).hexdigest()
        if digest != proof['rules_sha256']:
            raise RuntimeError(
                'dedicated INPUT/FORWARD rules differ from live-qualified policy'
            )

    def validate_host_prerequisites(self):
        from sdk_runtime import verify_sdk_runtime
        verify_sdk_runtime()
        if self.config.get('private_engine_verified') is not True:
            raise RuntimeError(
                'private engine and storage have not been qualified'
            )
        if self.config.get('containerd_socket'
                           ) != '/run/coralnpu-ci/containerd.sock':
            raise RuntimeError('fixed private containerd socket required')
        info = json.loads(self.docker(['info', '--format', '{{json .}}']))
        if info.get('CgroupVersion') != '2' or info.get('CgroupDriver'
                                                        ) != 'systemd':
            raise RuntimeError('qualified cgroup-v2/systemd Docker required')
        if info.get('DockerRootDir') != '/srv/coralnpu-ci/engine/docker':
            raise RuntimeError(
                'Docker writes outside bounded dedicated storage'
            )
        aggregate = self._systemd_cgroup('coralnpu-ci.slice')
        self.cg.verify_caps(aggregate)
        for unit in ('coralnpu-ci-docker.service',
                     'coralnpu-ci-containerd.service'):
            record = self._systemd_cgroup(unit)
            if not record['path'].startswith(aggregate['path'] + '/'):
                raise RuntimeError(
                    'private engine escaped aggregate resource caps'
                )
        if not any('apparmor' in value
                   for value in info.get('SecurityOptions', [])):
            raise RuntimeError('AppArmor protection unavailable')
        image = json.loads(
            self.docker([
                'image', 'inspect', self.config['container_image_digest']
            ])
        )
        pin = self.config['container_image_digest']
        if len(image) != 1:
            raise RuntimeError('immutable helper image ambiguous')
        if pin.startswith('sha256:'):
            if image[0].get('Id') != pin:
                raise RuntimeError('local helper image content ID differs')
        elif pin not in image[0].get('RepoDigests', []):
            raise RuntimeError(
                'immutable helper image repository digest unavailable'
            )
        image_config = image[0]['Config']
        if image_config.get('Volumes') or image_config.get(
                'OnBuild') or image_config.get('Healthcheck', {}).get(
                    'Test', ['NONE']) != ['NONE']:
            raise RuntimeError(
                'image has unbounded automatic actions or volumes'
            )
        for item in image_config.get('Env', []):
            key, _, value = item.partition('=')
            if key not in {'PATH', 'LANG', 'LC_ALL', 'HOME', 'TMPDIR',
                           'AWS_EC2_METADATA_DISABLED',
                           'PYTHONDONTWRITEBYTECODE'}:
                raise RuntimeError(
                    'image environment is not on the qualified nonsecret allowlist'
                )
        self.verify_network()
        from mount_inventory import verify_mount
        for mount in self.config['licensed_mounts']:
            verify_mount(mount, deadline=time.monotonic() + 300)

    def ensure_bounded_filesystem(self):
        if self.volume is None:
            from volume import BoundedVolume
            self.volume = BoundedVolume(self.config, self.runner)
        return self.volume.ensure()

    def prepare_workspace(self, key):
        run_key(key)
        base = open_trusted_directory('/srv/coralnpu-ci', private=True)
        try:
            try:
                os.mkdir('runs', 0o700, dir_fd=base)
            except FileExistsError:
                pass
            runs = open_trusted_directory('runs', dir_fd=base, private=True)
            try:
                os.mkdir(
                    key, 0o700, dir_fd=runs
                )  # Never reuse an admitted directory.
                parent = open_trusted_directory(key, dir_fd=runs, private=True)
            finally:
                os.close(runs)
        finally:
            os.close(base)
        paths = {}
        try:
            for name in ('source', 'metadata', 'build', 'qualification',
                         'inbox', 'tmp', 'run', 'home', 'control'):
                os.mkdir(name, 0o700, dir_fd=parent)
                if name not in {'control', 'tmp', 'run', 'home'}:
                    os.chown(
                        name,
                        self.config['host_uid'],
                        self.config['host_gid'],
                        dir_fd=parent,
                        follow_symlinks=False
                    )
                path = f'/srv/coralnpu-ci/runs/{key}/{name}'
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                self.workspace_identities[path] = (info.st_dev, info.st_ino)
                paths[name] = path
            for scratch in ('tmp', 'run', 'home'):
                scratch_fd = os.open(
                    scratch,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent
                )
                try:
                    for phase in ('stage', 'build', 'qualify', 'collect'):
                        os.mkdir(phase, 0o700, dir_fd=scratch_fd)
                        os.chown(
                            phase,
                            self.config['host_uid'],
                            self.config['host_gid'],
                            dir_fd=scratch_fd,
                            follow_symlinks=False
                        )
                finally:
                    os.close(scratch_fd)
            control = os.open(
                'control',
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent
            )
            try:
                for phase in ('stage', 'build', 'qualify', 'collect'):
                    os.mkdir(phase, 0o555, dir_fd=control)
                    child = os.open(
                        phase,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=control
                    )
                    try:
                        os.fchmod(child, 0o555)
                    finally:
                        os.close(child)
            finally:
                os.close(control)
        finally:
            os.close(parent)
        return paths

    def _systemd_cgroup(self, unit):
        value = self.command([
            '/usr/bin/systemctl', 'show', '--property=ControlGroup', '--value',
            unit
        ])
        if not value.startswith('/') or '..' in value.split(
                '/') or not value.endswith('/' + unit):
            raise RuntimeError('unexpected systemd ControlGroup')
        return identity(CGROOT + value)

    def mark_admission(self):
        # Capture once immediately before durable admission. Wall-clock changes
        # never lengthen this independent elapsed-time bound.
        self.deadline_boot = self.boot() + 21600

    def create_boundary(self, key, deadline):
        if self.deadline_boot is None or not 0 < self.deadline_boot - self.boot(
        ) <= 21600:
            raise RuntimeError('missing or expired admission BOOTTIME anchor')
        unit = slice_name(key)
        fd = open_trusted_directory('/run/systemd/system')
        body = (
            '[Unit]\nDescription=CoralNPU immutable per-run containment\nStopWhenUnneeded=no\n'
            'RefuseManualStop=yes\n[Slice]\nCPUQuota=1600%\nMemoryMax=68719476736\n'
            'MemorySwapMax=0\nTasksMax=4096\n'
        )
        try:
            out = os.open(
                unit,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd
            )
            with os.fdopen(out, 'w') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        self.command(['/usr/bin/systemctl', 'daemon-reload'], deadline)
        self.command(['/usr/bin/systemctl', 'start', unit], deadline)
        aggregate = self._systemd_cgroup('coralnpu-ci.slice')
        payload = self._systemd_cgroup(unit)
        launcher = self._systemd_cgroup('coralnpu-ci@' + key + '.service')
        if not payload['path'].startswith(
                aggregate['path'] +
                '/') or not launcher['path'].startswith(aggregate['path'] +
                                                        '/'):
            raise RuntimeError(
                'run or trusted launcher escaped aggregate cgroup'
            )
        self.cg.verify_caps(aggregate)
        self.cg.verify_caps(payload)
        boundary = {
            'run_key': key,
            'payload': payload,
            'launcher': launcher,
            'aggregate': aggregate,
            'boot_id':
            Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'deadline_boottime': self.deadline_boot,
            'deadline_wall': deadline
        }
        return boundary

    def start_launcher(self, key):
        self.command([
            '/usr/bin/systemctl', 'start',
            'coralnpu-ci@' + run_key(key) + '.service'
        ])

    def assert_deadline(self, deadline):
        if self.wall() >= deadline or (self.deadline_boot is not None
                                       and self.boot() >= self.deadline_boot):
            raise TimeoutError('absolute run deadline reached')

    def assert_empty(self, boundary):
        if not self.cg.empty(boundary['payload']):
            raise RuntimeError('run cgroup remains populated')

    def _guard_socket(self, key):
        return '/var/lib/coralnpu-ci/' + run_key(key) + '.lease.sock'

    def arm_deadline(self, key, boundary, deadline):
        if self.lock_fd is None:
            raise RuntimeError('shared lease missing')
        unit = 'coralnpu-ci-guard-' + key + '.service'
        self.command([
            '/usr/bin/systemd-run', '--unit=' + unit, '--service-type=exec',
            '--property=Slice=system.slice', '--property=OOMScoreAdjust=-1000',
            '--property=MemoryMax=67108864', '--property=TasksMax=32',
            '--property=Restart=no', '--property=UMask=0077', PYTHON, '-I',
            EXECROOT + '/entrypoint.py', 'watch', key
        ], deadline)
        # No guest may start until independent guard owns a duplicate lock FD.
        end = min(self.wall() + 10, deadline)
        while True:
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                client.settimeout(1)
                client.connect(self._guard_socket(key))
                peer = client.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 12
                )
                import struct
                if struct.unpack('3i', peer)[1] != 0:
                    raise RuntimeError('deadline guard peer is not root')
                client.sendmsg([b'lease'], [(
                    socket.SOL_SOCKET, socket.SCM_RIGHTS,
                    array.array('i', [self.lock_fd])
                )])
                if client.recv(32) != b'armed':
                    raise RuntimeError('deadline guard refused lease')
                client.close()
                return
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                client.close()
                if self.wall() >= end:
                    raise RuntimeError(
                        'independent deadline guard did not arm'
                    )
                time.sleep(.05)

    def disarm_deadline(self, key):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            client.settimeout(5)
            client.connect(self._guard_socket(key))
            import struct
            if struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET,
                                                     socket.SO_PEERCRED,
                                                     12))[1] != 0:
                raise RuntimeError('deadline guard peer is not root')
            client.send(b'complete')
            if client.recv(32) != b'complete':
                raise RuntimeError('guard refused completed lease release')
        finally:
            client.close()

    def _pin_workspace(self, paths):
        for path in paths.values():
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or (
                    info.st_dev,
                    info.st_ino) != self.workspace_identities[path]:
                raise RuntimeError('workspace mount identity changed')

    def run_phase(
        self, phase, request, paths, boundary, deadline, *, qualified=False
    ):
        self.assert_deadline(deadline)
        self.assert_empty(boundary)
        self.cg.verify_caps(boundary['aggregate'])
        self.cg.verify_caps(boundary['payload'])
        self._pin_workspace(paths)
        args = stage_argv(
            self.config, request, phase, paths, qualified=qualified
        )
        if phase == 'collect':
            args += [
                '--deadline',
                str(time.monotonic() + max(0, deadline - self.wall()))
            ]
        cid = self.command(args, deadline)
        if not re.fullmatch(r'[0-9a-f]{64}', cid):
            raise RuntimeError('unexpected Docker container ID')
        self.containers[cid] = phase
        self._verify_container(
            cid, phase, request, boundary, paths, running=False
        )
        self.docker(['start', cid], deadline)
        data = self._verify_container(
            cid, phase, request, boundary, paths, running=True
        )
        pid = data['State']['Pid']
        if type(pid) is not int or pid <= 1:
            raise RuntimeError('container has no live init PID')
        actual_cg = Path(f'/proc/{pid}/cgroup').read_text().strip()
        expected = boundary['payload']['path'][len(CGROOT):] + '/'
        if not actual_cg.startswith('0::' + expected):
            raise RuntimeError(
                'container process escaped pinned payload cgroup'
            )
        for namespace in ('pid', 'mnt', 'net', 'user'):
            if namespace == 'user':  # Dedicated nonroot UID is enforced; daemon userns-remap is not assumed.
                continue
            if os.readlink(f'/proc/{pid}/ns/{namespace}') == os.readlink(
                    f'/proc/self/ns/{namespace}'):
                raise RuntimeError('container shares a host namespace')
        control = open_trusted_directory(paths['control'] + '/' + phase)
        try:
            publish_gate(
                control, request['run_id'] + '-' + request['attempt'] + ' ' +
                phase + ' ' + request['source_sha'] + '\n'
            )
        finally:
            os.close(control)
        while True:
            self.assert_deadline(deadline)
            data = json.loads(self.docker(['inspect', cid], deadline))[0]
            if data['State']['Running'] is False:
                exit_code = data['State']['ExitCode']
                if type(exit_code) is not int:
                    raise RuntimeError('invalid container exit status')
                self.assert_empty(boundary)
                return exit_code
            time.sleep(.2)

    def _verify_container(
        self, cid, phase, request, boundary, paths, *, running
    ):
        data = json.loads(self.docker(['inspect', cid]))
        if len(data) != 1 or data[0]['Id'] != cid:
            raise RuntimeError('container identity changed')
        data = data[0]
        config, host = data['Config'], data['HostConfig']
        key = request['run_id'] + '-' + request['attempt']
        if config.get('Labels',
                      {}).get('coralnpu.ci.run') != key or config.get(
                          'Labels', {}).get('coralnpu.ci.phase') != phase:
            raise RuntimeError('container label mismatch')
        if config.get('Image'
                      ) != self.config['container_image_digest'] or config.get(
                          'Entrypoint') != ['/opt/coral-ci/bin/' + phase]:
            raise RuntimeError('untrusted image or entrypoint')
        if config.get(
                'User'
        ) != f'{self.config["host_uid"]}:{self.config["host_gid"]}':
            raise RuntimeError('unexpected guest UID')
        expected_network = self.config['fetch_network'
                                       ] if phase == 'stage' else 'none'
        if host.get('Privileged') or host.get(
                'ReadonlyRootfs') is not True or host.get(
                    'NetworkMode') != expected_network:
            raise RuntimeError('container isolation configuration differs')
        if (host.get('CgroupParent') != slice_name(key) or host.get('PidMode')
                or host.get('IpcMode') != 'none'
                or host.get('CgroupnsMode') != 'private' or host.get('Devices')
                or host.get('DeviceRequests')):
            raise RuntimeError('container has unapproved host interfaces')
        if host.get('LogConfig', {}).get('Type') != 'none' or host.get(
                'CapAdd') or host.get('CapDrop') != ['ALL']:
            raise RuntimeError('container logs/capabilities differ')
        # Docker may normalize the no-new-privileges separator; accept only this fixed policy plus seccomp/AppArmor.
        security = host.get('SecurityOpt', [])
        if not any(item in ('no-new-privileges', 'no-new-privileges=true',
                            'no-new-privileges:true') for item in security):
            raise RuntimeError('no-new-privileges missing')
        seccomp = [
            item[len('seccomp='):]
            for item in security
            if item.startswith('seccomp=')
        ]
        apparmor = [item for item in security if item.startswith('apparmor=')]
        expected_profile = self.private_json(
            self.config['container_seccomp_profile']
        )
        if len(seccomp) != 1 or json.loads(seccomp[0]) != expected_profile:
            raise RuntimeError(
                'actual seccomp policy differs from reviewed profile'
            )
        if apparmor != ['apparmor=docker-default'
                        ] or data.get('AppArmorProfile') != 'docker-default':
            raise RuntimeError('actual AppArmor policy differs')
        if len(security) != 3:
            raise RuntimeError('unapproved extra security options')
        # Compare every actual bind destination, readonly flag and source to the
        # fixed constructor, rejecting omitted or duplicate destinations too.
        expected_mounts = {}
        argv = stage_argv(self.config, request, phase, paths)
        for index, argument in enumerate(argv):
            if argument != '--mount':
                continue
            fields = dict(
                item.split('=', 1) if '=' in item else (item, True)
                for item in argv[index + 1].split(',')
            )
            expected_mounts[fields['dst']
                            ] = (fields['src'], 'readonly' not in fields)
        actual_mounts = {}
        for mount in data.get('Mounts', []):
            destination = mount.get('Destination')
            if mount.get('Type') != 'bind' or destination in actual_mounts:
                raise RuntimeError(
                    'unapproved extra/anonymous/duplicate mount'
                )
            actual_mounts[destination] = (mount.get('Source'), mount.get('RW'))
        if actual_mounts != expected_mounts:
            raise RuntimeError(
                'actual mount access differs from fixed phase policy'
            )
        if running and data['State']['Running'] is not True:
            raise RuntimeError(
                'immutable helper exited before containment gate'
            )
        return data

    def retire_guests(self, key, boundary, deadline):
        self.assert_empty(boundary)
        self.cg.write(boundary['payload'], 'cgroup.freeze', '1')
        for cid in list(self.containers):
            self.docker(['rm', cid], deadline, timeout=10)
        self.assert_empty(boundary)

    def emergency_stop(self, key, boundary):
        if boundary['run_key'] != run_key(key):
            raise RuntimeError('cleanup run identity mismatch')
        # Direct cgroup controls always precede daemon calls and remain frozen.
        self.cg.write(boundary['payload'], 'cgroup.freeze', '1')
        self.cg.write(boundary['payload'], 'cgroup.kill', '1')
        end = time.monotonic() + 5
        while not self.cg.empty(boundary['payload']):
            if time.monotonic() >= end:
                raise RuntimeError(
                    'could not prove stopped payload; quarantine required'
                )
            time.sleep(.05)
        # Never remove a cgroup/tombstone, thaw it, kill the daemon, or touch another run.
        for cid in list(self.containers):
            try:
                self.docker(['rm', '--force', cid], timeout=5)
            except Exception:
                pass

    def cancel_run(self, key, boundary):
        from deadline_guard import validate_boundary
        validate_boundary(
            key, boundary,
            Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        )
        self.emergency_stop(key, boundary)
        # Caller is the SSM cancel process outside the dedicated launcher cgroup.
        self.cg.write(boundary['launcher'], 'cgroup.kill', '1')

    def seal_inbox(self, paths, *, qualified):
        self._pin_workspace(paths)
        fd = os.open(
            paths['inbox'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            info = os.fstat(fd)
            if (info.st_dev,
                    info.st_ino) != self.workspace_identities[paths['inbox']]:
                raise RuntimeError('inbox identity changed')
            seal_inbox(fd, self.config['host_uid'], qualified=qualified)
            verify_sealed_inbox(fd, qualified=qualified)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def close_inbox(self, fd):
        os.close(fd)
