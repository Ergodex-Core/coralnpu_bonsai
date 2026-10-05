"""Fixed argv for immutable helpers. No operation is performed here."""
import re

STAGES = ('stage', 'build', 'qualify', 'collect')


def run_key(value):
    if not isinstance(value, str) or not re.fullmatch(
            r'[1-9][0-9]{0,19}-[1-9][0-9]{0,5}', value):
        raise ValueError('invalid run key')
    return value


def slice_name(key):
    run, attempt = run_key(key).split('-')
    return f'coralnpu-ci-r{run}a{attempt}.slice'


def absolute_path(value):
    if not isinstance(value, str) or not value.startswith('/') or any(
            c in value for c in ',\n\r\x00'):
        raise ValueError('invalid absolute mount path')
    if any(part in ('.', '..', '') for part in value.split('/')[1:]):
        raise ValueError('non-canonical mount path')
    return value


def stage_argv(config, request, phase, paths, *, qualified=False):
    """Only validated fixed phases/paths. Callers pin every source before Docker."""
    if phase not in STAGES or type(qualified) is not bool:
        raise ValueError('invalid stage')
    key = run_key(request['run_id'] + '-' + request['attempt'])
    uid, gid = config['host_uid'], config['host_gid']
    if type(uid) is not int or type(gid) is not int or min(uid, gid) < 1001:
        raise ValueError('dedicated nonroot uid/gid required')
    image = config['container_image_digest']
    if not isinstance(image, str) or not re.fullmatch(
            r'(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}', image):
        raise ValueError('content-addressed image required')
    parent = slice_name(key)
    network = config['fetch_network'] if phase == 'stage' else 'none'
    if phase == 'stage' and not re.fullmatch(
            r'coralnpu-ci-fetch-[a-z0-9]{8,32}', network):
        raise ValueError('qualified dedicated staging network required')
    root = f'/srv/coralnpu-ci/runs/{key}'
    expected_paths = {
        'source', 'metadata', 'build', 'qualification', 'inbox', 'tmp', 'run',
        'home', 'control'
    }
    if set(paths) != expected_paths:
        raise ValueError('unexpected workspace mounts')
    for name, path in paths.items():
        if absolute_path(path) != root + '/' + name:
            raise ValueError('mount escaped fixed per-run directory')
    if config.get('docker_socket') != 'unix:///run/coralnpu-ci/docker.sock':
        raise ValueError('fixed private engine socket required')
    args = [
        '/usr/bin/docker', '--host', config['docker_socket'], 'create',
        '--name', f'coralnpu-ci-{key}-{phase}', '--label',
        'coralnpu.ci.run=' + key, '--label', 'coralnpu.ci.phase=' + phase,
        '--pull=never', '--user', f'{uid}:{gid}', '--read-only',
        '--network=' + network, '--cap-drop=ALL',
        '--security-opt=no-new-privileges:true', '--security-opt=seccomp=' +
        absolute_path(config['container_seccomp_profile']),
        '--security-opt=apparmor=docker-default', '--pids-limit=4096',
        '--cpus=16', '--memory=64g', '--memory-swap=64g', '--ipc=none',
        '--cgroupns=private', '--cgroup-parent=' + parent, '--init',
        '--log-driver=none', '--ulimit=core=0:0', '--ulimit=nofile=4096:4096',
        '--stop-timeout=1', '--workdir=/job/tmp', '--env=HOME=/job/home',
        '--env=TMPDIR=/job/tmp', '--env=AWS_EC2_METADATA_DISABLED=true',
        '--env=GIT_CONFIG_NOSYSTEM=1', '--env=GIT_CONFIG_GLOBAL=/dev/null',
        '--env=GIT_TERMINAL_PROMPT=0'
    ]
    # Writable state and logs are on the one hard-bounded ext4 filesystem;
    # no anonymous volumes, tmpfs, Docker logs, socket, credentials, or devices.
    rw = {
        'stage': {'source', 'metadata', 'tmp', 'run', 'home'},
        'build': {'source', 'build', 'tmp', 'run', 'home'},
        'qualify': {'qualification', 'tmp', 'run', 'home'},
        'collect': {'inbox', 'tmp', 'run', 'home'}
    }[phase]
    visible = {
        'stage': {'source', 'metadata', 'tmp', 'run', 'home'},
        'build': {'source', 'metadata', 'build', 'tmp', 'run', 'home'},
        'qualify':
        {'source', 'metadata', 'build', 'qualification', 'tmp', 'run', 'home'},
        'collect': {'build', 'qualification', 'inbox', 'tmp', 'run', 'home'}
    }[phase]
    for name in sorted(visible):
        source = paths[name] + '/' + phase if name in {'tmp', 'run', 'home'
                                                       } else paths[name]
        target = '/inbox' if name == 'inbox' else '/job/' + name
        suffix = '' if name in rw else ',readonly'
        args += ['--mount', f'type=bind,src={source},dst={target}{suffix}']
    args += [
        '--mount',
        f'type=bind,src={paths["control"]}/{phase},dst=/control,readonly'
    ]
    if phase == 'collect':
        args += [
            '--mount',
            f'type=bind,src={paths["qualification"]}/output,dst=/work/output,readonly'
        ]
    # /tmp and /run are writable bind aliases inside the same disk/inode budget.
    args += [
        '--mount', f'type=bind,src={paths["tmp"]}/{phase},dst=/tmp', '--mount',
        f'type=bind,src={paths["run"]}/{phase},dst=/run'
    ]
    for mount in config['licensed_mounts']:
        if set(mount) != {'source', 'target', 'qualification_sha256'}:
            raise ValueError('mount lacks reviewed transitive inventory')
        source, target = absolute_path(mount['source']
                                       ), absolute_path(mount['target'])
        if target not in {'/opt/amd', '/opt/Xilinx', '/opt/coral-tools',
                          '/opt/aws-fpga', '/deps'}:
            raise ValueError('unapproved tool/dependency destination')
        if not re.fullmatch(r'[0-9a-f]{64}', mount['qualification_sha256']):
            raise ValueError('unqualified tool mount')
        if phase != 'stage':
            args += [
                '--mount', f'type=bind,src={source},dst={target},readonly'
            ]
    args += [
        '--entrypoint=/opt/coral-ci/bin/' + phase, image, '--repository',
        config['repository'], '--source-sha', request['source_sha'],
        '--source-ref', request['eligibility']['source_ref'], '--run-key', key
    ]
    if phase in {'collect', 'qualify'} and not qualified:
        args += ['--evidence-only']
    return args


def build_argv(image, uid, gid, run_key, workspace, licensed_mounts):
    """Legacy pure constructor retained for review-bundle compatibility tests."""
    if not re.fullmatch(r'(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}', image):
        raise ValueError('image must have an immutable SHA256 digest')
    if not re.fullmatch(r'[1-9][0-9]{0,19}-[1-9][0-9]{0,5}', run_key):
        raise ValueError('invalid run key')
    if type(uid) is not int or type(gid) is not int or min(uid, gid) < 1001:
        raise ValueError('dedicated nonroot uid/gid required')
    if absolute_path(workspace) != f'/srv/coralnpu-ci/runs/{run_key}/work':
        raise ValueError('workspace outside dedicated mount')
    if licensed_mounts:
        raise ValueError(
            'use phase constructor with reviewed mount inventories'
        )
    return [
        '/usr/bin/docker', 'create', '--read-only', '--network=none',
        '--cap-drop=ALL', '--cgroup-parent=coralnpu-ci.slice', '--user',
        f'{uid}:{gid}', image
    ]
