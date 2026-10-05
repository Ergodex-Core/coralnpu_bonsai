#!/usr/bin/env python3
"""Fixed, credential-free CI phases baked into the reviewed container image.

The root launcher owns all paths, mounts, phase ordering and the deadline. This
program accepts identities, never arbitrary paths, commands, repositories or
qualification policies. Only stage has a separately qualified egress network.
"""
import argparse
import hashlib
import importlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

ROOT = Path('/opt/coral-ci')
TRUSTED = ROOT / 'trusted/fpga/aws/build'
SOURCE = Path('/job/source')
METADATA = Path('/job/metadata')
BUILD = Path('/job/build')
QUALIFICATION = Path('/job/qualification')
TEMP = Path('/job/tmp')
REPOSITORY = 'Ergodex-Core/coralnpu_bonsai'
REMOTE = 'https://github.com/' + REPOSITORY + '.git'
REFERENCE = '382b5c12030ad8eb74ab8deafb2529301faeab16'
MAX_SOURCE_FILES = 200_000
MAX_SOURCE_BYTES = 4 * 1024**3
MAX_DCP_BYTES = 1_450_000_000
MAX_EVIDENCE_BYTES = 450_000_000
MAX_LOG_BYTES = 16_000_000
REPORTS = (
    'route_status.rpt', 'timing_summary.rpt', 'clocks.rpt', 'bus_skew.rpt',
    'check_timing.rpt', 'drc.rpt', 'methodology.rpt', 'cdc.rpt',
    'exceptions.rpt', 'facts.tsv', 'drc_check_properties.rpt'
)


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def safe_relative(name):
    require(isinstance(name, str) and len(name) <= 4096, 'Invalid source path')
    p = PurePosixPath(name)
    require(
        name and not p.is_absolute() and name == p.as_posix()
        and not any(x in ('', '.', '..', '.git') for x in p.parts)
        and not any(ord(x) < 32 for x in name), 'Unsafe source path'
    )
    return p


def read_regular(path, limit):
    """Never follow a leaf/ancestor symlink or accept special/hardlinked files."""
    path = Path(path)
    require(path.is_absolute(), 'Absolute internal path required')
    parent = Path('/')
    for part in path.parts[1:-1]:
        parent /= part
        require(
            stat.S_ISDIR(parent.lstat().st_mode),
            'Symlink/non-directory ancestor'
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and 0 <= before.st_size <= limit, 'Unsafe/oversized file'
        )
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        require(
            len(data) == before.st_size and len(data) <= limit and (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns
            ) == (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns
            ), 'File changed while reading'
        )
    return data


def hash_regular(path, limit):
    # Checkpoints are intentionally bounded, but hash them without a large buffer.
    path = Path(path)
    parent = Path('/')
    for part in path.parts[1:-1]:
        parent /= part
        require(
            stat.S_ISDIR(parent.lstat().st_mode),
            'Symlink/non-directory ancestor'
        )
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(before.st_mode) and before.st_nlink == 1
            and 0 < before.st_size <= limit, 'Unsafe/oversized file'
        )
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
        require((
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns
        ) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns
        ), 'File changed while hashing')
        return digest, before.st_size


def fixed_environment(home):
    """No host/user environment, credentials, license values, Git config or proxy."""
    return {
        'PATH':
        '/opt/coral-ci/bin:/deps/tools/verilator/bin:/opt/Xilinx/2025.2/Vivado/bin:/usr/local/bin:/usr/bin:/bin',
        'HOME': str(home),
        'TMPDIR': str(TEMP),
        'LANG': 'C.UTF-8',
        'LC_ALL': 'C.UTF-8',
        'PYTHONDONTWRITEBYTECODE': '1',
        'AWS_EC2_METADATA_DISABLED': 'true',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': '/dev/null',
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_ASKPASS': '/bin/false',
        'SSH_ASKPASS': '/bin/false',
        'GIT_ALLOW_PROTOCOL': 'https',
        'GIT_ATTR_NOSYSTEM': '1',
        'GIT_LFS_SKIP_SMUDGE': '1',
        'VERILATOR': '/deps/tools/verilator/bin/verilator',
        'VERILATOR_ROOT': '/deps/tools/verilator',
        'CCACHE_DISABLE': '1',
        'XILINX_VIVADO': '/opt/Xilinx/2025.2/Vivado'
    }


def command(args, *, cwd, env, timeout=120, log=None):
    """A root/systemd deadline also covers descendants if this process is killed."""
    if log:
        output = open(log, 'xb')
    else:
        output = subprocess.PIPE
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        try:
            data, _ = proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise RuntimeError('Phase command timed out') from None
        require(proc.returncode == 0, 'Phase command failed: ' + args[0])
        return data or b''
    finally:
        if log:
            output.close()


def git_command(*args):
    return [
        'git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
        '-c', 'core.sshCommand=/bin/false', '-c', 'protocol.allow=never', '-c',
        'protocol.https.allow=always', '-c', 'protocol.file.allow=never', '-c',
        'protocol.ext.allow=never', '-c', 'credential.helper=', '-c',
        'http.followRedirects=false', '-c', 'fetch.recurseSubmodules=false',
        '-c', 'submodule.recurse=false', '-c', 'core.askPass=/bin/false', *args
    ]


def validate_identity(args):
    require(args.repository == REPOSITORY, 'Repository is fixed')
    require(
        re.fullmatch(r'[0-9a-f]{40}', args.source_sha), 'Invalid source SHA'
    )
    require(
        re.fullmatch(r'refs/pull/[1-9][0-9]{0,9}/head', args.source_ref),
        'Invalid PR ref'
    )
    require(
        re.fullmatch(r'[1-9][0-9]{0,19}-[1-9][0-9]{0,5}', args.run_key),
        'Invalid run key'
    )


def await_host_gate(args):
    """Do not touch submitted inputs until root has inspected actual containment."""
    deadline = time.monotonic() + 30
    path = Path('/control/go')
    expected = f'{args.run_key} {args.phase} {args.source_sha}\n'.encode()
    while time.monotonic() < deadline:
        try:
            info = path.lstat()
        except FileNotFoundError:
            time.sleep(0.1)
            continue
        require(
            info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o444,
            'Host gate must be root-owned and read-only'
        )
        require(
            read_regular(path, 256) == expected, 'Host gate identity mismatch'
        )
        return
    raise RuntimeError('Host containment gate was not granted')


def staged_inventory(tree):
    rows = tree.split(b'\0')
    entries = {}
    total = 0
    for row in rows:
        if not row:
            continue
        header, raw_name = row.split(b'\t', 1)
        mode, kind, oid = header.decode('ascii').split()
        name = raw_name.decode('utf-8', errors='strict')
        safe_relative(name)
        require(
            name not in entries and len(entries) < MAX_SOURCE_FILES,
            'Duplicate/excess source paths'
        )
        path = SOURCE / name
        require(
            kind == 'blob' and mode in ('100644', '100755', '120000'),
            'Submodule/special source rejected'
        )
        if mode == '120000':
            require(path.is_symlink(), 'Expected symlink')
            target = os.readlink(path)
            require(
                not os.path.isabs(target) and '\x00' not in target,
                'Unsafe symlink target'
            )
            require(
                path.resolve().is_relative_to(SOURCE),
                'Source symlink escapes checkout'
            )
            data = target.encode()
        else:
            data = read_regular(path, MAX_SOURCE_BYTES)
        # Verify working bytes against the Git object, not a mutable PR manifest.
        blob_hash = hashlib.sha1(
            b'blob ' + str(len(data)).encode() + b'\0' + data
        ).hexdigest()
        require(blob_hash == oid, 'Checkout differs from Git tree')
        total += len(data)
        require(total <= MAX_SOURCE_BYTES, 'Source byte limit exceeded')
        entries[name] = {
            'mode': mode,
            'git_blob': oid,
            'bytes': len(data),
            'sha256': digest_bytes(data)
        }
    require(entries, 'Empty source tree')
    return entries


def stage(args):
    require(
        not any(SOURCE.iterdir()) and not any(METADATA.iterdir()),
        'Stage requires fresh directories'
    )
    home = TEMP / 'stage-home'
    home.mkdir(mode=0o700)
    env = fixed_environment(home)
    # The host must already attest this named network's deny rules and DNS policy.
    # No proxy value supplied by the PR or inherited host environment is used.
    command(git_command('init', '--template=', '.'), cwd=SOURCE, env=env)
    command(
        git_command('remote', 'add', 'origin', REMOTE), cwd=SOURCE, env=env
    )
    command(
        git_command(
            'fetch', '--no-tags', '--no-recurse-submodules', 'origin',
            args.source_ref
        ),
        cwd=SOURCE,
        env=env,
        timeout=900,
        log=METADATA / 'fetch.log'
    )
    observed = command(
        git_command('rev-parse', 'FETCH_HEAD^{commit}'), cwd=SOURCE, env=env
    ).decode().strip()
    require(observed == args.source_sha, 'PR ref moved or SHA differs')
    # Require the pinned comparison commit in genuine Git history; never forge HEAD.
    command(
        git_command('cat-file', '-e', REFERENCE + '^{commit}'),
        cwd=SOURCE,
        env=env
    )
    command(
        git_command(
            'checkout', '--detach', '--no-recurse-submodules', args.source_sha
        ),
        cwd=SOURCE,
        env=env
    )
    tree = command(
        git_command('ls-tree', '-r', '-z', 'HEAD'), cwd=SOURCE, env=env
    )
    entries = staged_inventory(tree)
    tree_sha = command(
        git_command('rev-parse', 'HEAD^{tree}'), cwd=SOURCE, env=env
    ).decode().strip()
    command(
        git_command('fsck', '--no-reflogs', '--strict'),
        cwd=SOURCE,
        env=env,
        timeout=300,
        log=METADATA / 'git-fsck.log'
    )
    write_json(
        METADATA / 'staged-source.json', {
            'schema_version': 1,
            'repository': REPOSITORY,
            'source_sha': observed,
            'source_ref': args.source_ref,
            'run_key': args.run_key,
            'tree_sha': tree_sha,
            'reference_commit': REFERENCE,
            'files': entries,
            'source_bytes': sum(row['bytes'] for row in entries.values())
        }
    )


def read_staging(args, verify_source=True):
    receipt = json.loads(
        read_regular(METADATA / 'staged-source.json', 64_000_000)
    )
    require(
        receipt['repository'] == REPOSITORY
        and receipt['source_sha'] == args.source_sha
        and receipt['source_ref'] == args.source_ref
        and receipt['run_key'] == args.run_key,
        'Root-separated staged identity mismatch'
    )
    require(
        isinstance(receipt['files'], dict)
        and 0 < len(receipt['files']) <= MAX_SOURCE_FILES,
        'Invalid staged inventory'
    )
    if verify_source:
        for name, expected in receipt['files'].items():
            safe_relative(name)
            path = SOURCE / name
            if expected['mode'] == '120000':
                require(
                    path.is_symlink()
                    and path.resolve().is_relative_to(SOURCE),
                    'Source symlink changed'
                )
                actual = os.readlink(path).encode()
            else:
                actual = read_regular(path, MAX_SOURCE_BYTES)
            require(
                len(actual) == expected['bytes']
                and digest_bytes(actual) == expected['sha256'],
                'Source changed: ' + name
            )
    return receipt


def trusted_modules():
    identity = json.loads(
        read_regular(ROOT / 'trusted-identity.json', 1_000_000)
    )
    for name, expected in identity['files'].items():
        require(
            digest_bytes(read_regular(ROOT / 'trusted' / name,
                                      32_000_000)) == expected,
            'Baked trusted policy changed'
        )
    sys.path.insert(0, str(TRUSTED))
    driver = importlib.import_module('build')
    gates = importlib.import_module('gates')
    return driver, gates, identity


def build(args):
    read_staging(args)
    driver, _, _ = trusted_modules()
    result = BUILD / 'result'
    require(not result.exists(), 'Fresh build result directory required')
    result.mkdir(mode=0o700)
    (result / 'logs').mkdir(mode=0o700)
    home = BUILD / 'home'
    home.mkdir(mode=0o700)
    # This snapshot has already been scanned/sealed by the operator. Each run
    # receives its own mutable copy; no colleague cache or shared cache is mounted.
    shutil.copytree(
        '/deps/bazel-repository-cache',
        BUILD / 'bazel-repository-cache',
        symlinks=True
    )
    os.environ.clear()
    os.environ.update(fixed_environment(home))
    os.environ.update(
        PR_HEAD_SHA=args.source_sha,
        GITHUB_SHA=args.source_sha,
        GITHUB_RUN_ID=args.run_key
    )
    # Baked driver, pins, testbench and qualification Tcl; submitted HDL/build
    # rules are intentionally untrusted code inside the credential-free sandbox.
    driver.REPO = SOURCE
    driver.AWS = SOURCE / 'fpga/aws'
    try:
        driver.build(
            SimpleNamespace(
                hdk_root=Path('/deps/aws-fpga'), tag='ci-' + args.run_key
            ), result
        )
    except Exception as exc:
        write_json(
            result / 'trusted-driver-failure.json', {
                'status': 'failed',
                'reason': str(exc)[:8192]
            }
        )
        raise


def native_manifest(pins, tag, checkpoint_hash):
    result = dict(
        pci_device_id='0xF010',
        pci_vendor_id='0x1D0F',
        pci_subsystem_id='0x1D51',
        pci_subsystem_vendor_id='0xFEDC',
        manifest_format_version=2,
        dcp_hash=checkpoint_hash,
        shell_version=pins['shell_version'],
        hdk_version=pins['hdk_version'],
        tool_version='v' + pins['vivado_version'],
        date=tag,
        dcp_file_name=tag + '.SH_CL_routed.dcp'
    )
    result.update(
        zip((
            'clock_recipe_a', 'clock_recipe_b', 'clock_recipe_c',
            'clock_recipe_hbm'
        ), pins['clock_recipes'])
    )
    return result


def evidence_archive(output, candidates):
    """Fixed logical names, bounded regular bytes only; never archive a PR tree."""
    total = 0
    seen = set()
    omissions = []
    try:
        with tarfile.open(output, 'x', format=tarfile.USTAR_FORMAT) as archive:
            for name, path, limit in candidates:
                safe_relative(name)
                require(name not in seen, 'Duplicate evidence name')
                seen.add(name)
                try:
                    data = read_regular(path, limit)
                    require(
                        total + len(data) <= MAX_EVIDENCE_BYTES,
                        'Evidence aggregate byte cap'
                    )
                except (OSError, RuntimeError) as exc:
                    # A malicious oversized/special diagnostic file must not
                    # suppress the trusted failure receipt or produce a partial tar.
                    omissions.append({'name': name, 'reason': str(exc)[:1024]})
                    continue
                total += len(data)
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
            data = json.dumps(omissions, sort_keys=True).encode() + b'\n'
            info = tarfile.TarInfo('evidence-omissions.json')
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    except Exception:
        output.unlink(missing_ok=True)
        raise
    require(
        0 < output.stat().st_size < 499_000_000,
        'Evidence artifact exceeds wire cap'
    )


def qualify(args):
    require(
        not any(QUALIFICATION.iterdir()),
        'Qualification requires a fresh isolated directory'
    )
    reports = QUALIFICATION / 'reports'
    reports.mkdir(mode=0o700)
    output = QUALIFICATION / 'output'
    output.mkdir(mode=0o700)
    home = QUALIFICATION / 'home'
    home.mkdir(mode=0o700)
    driver, gates, identity = trusted_modules()
    result = {
        'qualified': False,
        'source_sha': args.source_sha,
        'source_ref': args.source_ref,
        'run_key': args.run_key,
        'trusted_policy': identity,
        'physical_execution': 'not_run'
    }
    success = False
    try:
        require(
            not args.evidence_only,
            'Earlier phase failed; evidence-only qualification requested'
        )
        staged = read_staging(args)
        result['tree_sha'] = staged['tree_sha']
        tag = 'ci-' + args.run_key
        checkpoint = BUILD / 'result/cl_coralnpu/build/checkpoints' / (
            'cl_coralnpu.' + tag + '.post_route.dcp'
        )
        before, size = hash_regular(checkpoint, MAX_DCP_BYTES)
        require(
            size > 1_000_000 and '.VIOLATED' not in checkpoint.name.upper(),
            'Invalid routed checkpoint'
        )
        # Copy NO PR Tcl/report/gate module into this phase. Only the DCP is
        # opened by a fresh bounded, offline Vivado invocation.
        log = QUALIFICATION / 'validation.log'
        env = fixed_environment(home)
        command([
            '/opt/Xilinx/2025.2/Vivado/bin/vivado', '-mode', 'batch',
            '-source',
            str(TRUSTED / 'validate_checkpoint.tcl'), '-log',
            str(reports / 'vivado.log'), '-journal',
            str(reports / 'vivado.jou'), '-tclargs',
            str(checkpoint),
            str(reports)
        ],
                cwd=reports,
                env=env,
                timeout=1800,
                log=log)
        require(
            hash_regular(checkpoint, MAX_DCP_BYTES)[0] == before,
            'Checkpoint changed during qualification'
        )
        logs = {
            n: BUILD / 'result/logs' / (n + '.log')
            for n in ('synthesis', 'implementation')
        }
        for path in logs.values():
            read_regular(path, MAX_LOG_BYTES)
        logs['validation'] = log
        for name in REPORTS:
            read_regular(reports / name, MAX_LOG_BYTES)
        verdict = gates.qualify(reports, logs, checkpoint, driver.PINS)
        result['checkpoint_sha256'] = before
        result['qualification'] = verdict
        require(
            verdict['qualified'], 'Independent checkpoint qualification failed'
        )
        # Ignore PR-produced archives/receipts entirely; create native AWS tar
        # from the independently reopened checkpoint and regenerated debug probes.
        receipt = gates.package(
            output / 'checkpoint.tar', checkpoint,
            reports / 'debug_probes.ltx',
            native_manifest(driver.PINS, tag, before)
        )
        require((output / 'checkpoint.tar').stat().st_size <= 1_500_000_000,
                'Checkpoint tar exceeds wire cap')
        result['package'] = receipt
        result['qualified'] = success = True
    except Exception as exc:
        result['failure'] = str(exc)[:8192]
    finally:
        write_json(QUALIFICATION / 'trusted-result.json', result)
        candidates = [
            (
                'trusted-result.json', QUALIFICATION / 'trusted-result.json',
                8_000_000
            ),
            (
                'staged-source.json', METADATA / 'staged-source.json',
                64_000_000
            ), ('staging-fetch.log', METADATA / 'fetch.log', MAX_LOG_BYTES),
            ('staging-fsck.log', METADATA / 'git-fsck.log', MAX_LOG_BYTES),
            (
                'validation.log', QUALIFICATION / 'validation.log',
                MAX_LOG_BYTES
            )
        ]
        candidates += [('reports/' + n, reports / n, MAX_LOG_BYTES)
                       for n in REPORTS]
        # Untrusted build output is diagnostic only; identify it as such.
        candidates += [(
            'untrusted-build-logs/' + n + '.log',
            BUILD / 'result/logs' / (n + '.log'), MAX_LOG_BYTES
        ) for n in (
            'rtl', 'simulation-compile', 'simulation', 'synthesis',
            'implementation'
        )]
        evidence_archive(output / 'evidence.tar', candidates)
    require(
        success, 'Checkpoint rejected; evidence-only collection is allowed'
    )


def collect(args):
    require(
        args.deadline is not None and args.deadline > time.monotonic(),
        'Collector deadline required'
    )
    sys.path.insert(0, str(ROOT / 'lib'))
    collector = importlib.import_module('secure_files')
    argv = ['--deadline', str(args.deadline)]
    if args.evidence_only:
        argv.append('--evidence-only')
    collector.main(argv)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'phase', choices=('stage', 'build', 'qualify', 'collect')
    )
    parser.add_argument('--repository', default=REPOSITORY)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--source-ref', required=True)
    parser.add_argument('--run-key', required=True)
    parser.add_argument('--deadline', type=float)
    parser.add_argument('--evidence-only', action='store_true')
    args = parser.parse_args(argv)
    require(os.geteuid() != 0, 'Container entrypoints must run without root')
    os.umask(0o077)
    validate_identity(args)
    await_host_gate(args)
    globals()[args.phase](args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(
            'CORAL_TRUSTED_PHASE_FAILED: ' + str(exc)[:8192], file=sys.stderr
        )
        sys.exit(1)
