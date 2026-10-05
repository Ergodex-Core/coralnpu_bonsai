"""Source, version and complete local HDK dependency-lock validation."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess

from process import require

PINS = json.loads(Path(__file__).with_name('pins.json').read_text())


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def capture(command, cwd=None):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    try:
        output, _ = process.communicate(timeout=60)
        require(process.returncode == 0, f'Command failed: {command[0]}')
        return output.strip()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def git(root, *args):
    return capture(['git', '-C', str(root), *args])


def tree_hashes(root):
    """Inventory every file, including ignored/generated inputs, excluding Git internals."""
    root = Path(root).resolve()
    files = {}
    seen = set()
    for directory, dirs, names in os.walk(root, followlinks=True):
        parent = Path(directory)
        real = parent.resolve()
        require(
            real.is_relative_to(root),
            'Input directory symlink escapes its root'
        )
        require(
            real not in seen, 'Input directory aliases/cycles are unsupported'
        )
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if d not in {'.git', '__pycache__'})
        for name in sorted(names):
            if name == '.git' or name.endswith('.pyc'):
                continue
            path = parent / name
            require(
                path.resolve().is_relative_to(root),
                'Input file symlink escapes its root'
            )
            require(
                path.is_file(), f'Input is not a regular file: {path.name}'
            )
            files[str(path.relative_to(root))] = sha(path)
    return files


def validate_coral(root):
    require(
        git(root, 'rev-parse', 'HEAD') == PINS['coral_commit'],
        'Coral revision differs from pins.json; review templates and pins together'
    )
    require(
        not git(root, 'status', '--porcelain', '--untracked-files=all'),
        'Coral source checkout must be clean'
    )
    require((root / 'hdl/chisel/src/coralnpu/BUILD').is_file(),
            'Missing Coral RTL target')


def validate_hbm_xci(path):
    try:
        component = json.loads(path.read_text())['ip_inst']
        require(
            component['component_reference'] == 'xilinx.com:ip:hbm:1.0',
            'Wrong HBM IP'
        )
        parameters = component['parameters']['component_parameters']
        for name, expected in PINS['hbm_xci_parameters'].items():
            require(
                len(parameters[name]) == 1
                and parameters[name][0]['value'] == expected,
                'Unsupported HBM configuration: ' + name
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('Unknown HBM XCI configuration format') from exc


def dependency_inventory(hdk):
    require(
        git(hdk, 'rev-parse', 'HEAD') == PINS['hdk_commit'],
        'Wrong HDK revision'
    )
    ip = hdk / 'hdk/common/ip'
    ip_commit = git(ip, 'rev-parse', 'HEAD')
    require(
        re.fullmatch('[0-9a-f]{40}', ip_commit)
        and ip_commit != PINS['hdk_commit'],
        'Missing initialized HDK IP repository'
    )
    require(ip_commit == PINS['ip_commit'], 'Wrong HDK IP revision')
    validate_hbm_xci(ip / 'cl_ip/cl_ip.srcs/sources_1/ip/cl_hbm/cl_hbm.xci')
    changed = git(hdk, 'diff', 'HEAD', '--name-only').splitlines()
    require(set(changed) <= {'hdk/common/ip'}, 'HDK tracked files modified')
    require(
        not git(ip, 'status', '--porcelain', '--untracked-files=no'),
        'HDK IP tracked files modified'
    )
    require(
        f"RELEASE_VERSION={PINS['hdk_version']}"
        in (hdk / 'release_version.txt').read_text(), 'Wrong HDK release'
    )
    # shell_stable is an official in-tree alias; inventory the canonical hdk tree
    # once, skipping that alias, rather than following aliases multiple times.
    files = {}
    aliases = {}
    base = hdk / 'hdk'
    for directory, dirs, names in os.walk(base, followlinks=False):
        parent = Path(directory)
        for name in dirs:
            path = parent / name
            if path.is_symlink():
                require(
                    path.resolve().is_relative_to(base),
                    'HDK directory alias escapes inventoried tree'
                )
                aliases[str(path.relative_to(hdk))
                        ] = str(path.resolve().relative_to(hdk))
        dirs[:] = sorted(
            d for d in dirs if d != '.git' and not (parent / d).is_symlink()
        )
        for name in sorted(names):
            if name == '.git':
                continue
            path = parent / name
            require(
                path.resolve().is_relative_to(hdk),
                'HDK dependency escapes checkout'
            )
            require(path.is_file(), 'HDK dependency is not a regular file')
            files[str(path.relative_to(hdk))] = sha(path)
    shared = hdk / 'shared'
    if shared.is_dir():
        for name, digest in tree_hashes(shared).items():
            files['shared/' + name] = digest
    for name in ('hdk_setup.sh', 'release_version.txt'):
        path = hdk / name
        if path.is_file():
            files[name] = sha(path)
    shell = hdk / 'hdk/common/shell_stable'
    require(shell.resolve().is_relative_to(base), 'Shell alias escapes HDK')
    require(
        sha(shell / 'build/checkpoints/from_aws/cl_bb_routed.small_shell.dcp'
            ) == PINS['shell_dcp_sha256'], 'Wrong shell checkpoint'
    )
    return {
        'schema_version': 1,
        'hdk_commit': PINS['hdk_commit'],
        'ip_commit': ip_commit,
        'shell_relative_path': str(shell.resolve().relative_to(hdk)),
        'aliases': aliases,
        'sha256': files
    }


def verify_dependency_lock(hdk, lock_path):
    expected = json.loads(lock_path.read_text())
    actual = dependency_inventory(hdk)
    require(
        actual == expected,
        'HDK/IP dependency inventory differs from reviewed lock'
    )
    return actual


def tool_versions(bazel, verilator, clang, linker, vivado=False):
    commands = {
        'bazel': [bazel, '--version'],
        'verilator': [verilator, '--version'],
        'clang': [clang, '--version'],
        'linker': [linker, '--version']
    }
    if vivado:
        commands['vivado'] = ['vivado', '-version']
    versions = {name: capture(command) for name, command in commands.items()}
    require(
        versions['bazel'] == 'bazel ' + PINS['bazel_version'],
        'Wrong Bazel version'
    )
    for name, prefix, pin in [('verilator', 'Verilator ', 'verilator_version'),
                              ('clang', 'clang version ', 'llvm_version'),
                              ('linker', 'LLD ', 'llvm_version')]:
        require(
            re.search(re.escape(prefix + PINS[pin]) + r'\b', versions[name]),
            f'Wrong {name} version'
        )
    if vivado:
        require(
            'Vivado v' + PINS['vivado_version'] in versions['vivado']
            and PINS['vivado_build'] in versions['vivado'],
            'Wrong Vivado version/build'
        )
    return versions
