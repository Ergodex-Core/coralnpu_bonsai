"""Strict intake of the preserved pinned reference emission; never restamp RTL.

The caller supplies a separately reviewed manifest SHA256. This deliberately
does not accept CI merge-stamped artifacts or a new arbitrary cache identity.
"""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile

FILES = (
    'RvvCoreMiniAxi.sv', 'RvvCoreMiniAxi.zip', 'VRvvCoreMiniAxi_parameters.h'
)
POLICY = 'all-tracked-except-fpga-and-github-v1'
STAMP = re.compile(rb"\(kscm([0-4])En \? 32'h([0-9A-F]{1,8}) : 32'h0\)")


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def git(repo, *args, binary=False):
    result = subprocess.check_output([
        'git', '--no-optional-locks', '-C',
        str(repo), *args
    ],
                                     timeout=60,
                                     stderr=subprocess.PIPE)
    return result if binary else result.decode().strip()


def closure(repo, revision):
    """Bind all source/config/toolchain/utils, including symlink target bytes."""
    require(re.fullmatch(r'[0-9a-f]{40}', revision), 'Full revision required')
    entries = {}
    for row in git(repo, 'ls-tree', '-rz', revision, binary=True).split(b'\0'):
        if not row:
            continue
        metadata, name = row.split(b'\t', 1)
        name = name.decode()
        if name.startswith(('fpga/', '.github/')):
            continue
        mode, kind, oid = metadata.decode().split()
        require(
            kind == 'blob' and mode in ('100644', '100755', '120000'),
            'Unsupported generator source object'
        )
        entries[name] = {'mode': mode, 'git_blob': oid}
    require(entries, 'Empty source closure')
    # One bounded batch avoids per-file Git processes while hashing blob bytes.
    names = sorted(entries)
    objects = ''.join(entries[name]['git_blob'] + '\n'
                      for name in names).encode()
    result = subprocess.run([
        'git', '--no-optional-locks', '-C',
        str(repo), 'cat-file', '--batch'
    ],
                            input=objects,
                            capture_output=True,
                            timeout=60,
                            check=True)
    data, offset = result.stdout, 0
    require(len(data) < 256 * 1024 * 1024, 'Source closure too large')
    for name in names:
        end = data.index(b'\n', offset)
        oid, kind, size = data[offset:end].decode().split()
        require(
            oid == entries[name]['git_blob'] and kind == 'blob',
            'Wrong source blob'
        )
        offset = end + 1
        size = int(size)
        blob = data[offset:offset + size]
        require(
            len(blob) == size
            and data[offset + size:offset + size + 1] == b'\n',
            'Incomplete source blob'
        )
        offset += size + 1
        entries[name]['sha256'] = digest(blob)
        if entries[name]['mode'] == '120000':
            target = blob.decode()
            resolved = os.path.normpath(
                str(PurePosixPath(name).parent / target)
            )
            require(
                not target.startswith('/') and resolved in entries,
                'Source symlink escapes verified closure'
            )
    require(offset == len(data), 'Unexpected source blob data')
    return {
        'policy': POLICY,
        'files': len(entries),
        'sha256': digest(canonical(entries))
    }


def read_regular(path, maximum):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
            'Artifact must be a single-linked regular file: ' + path.name
        )
        require(
            0 < info.st_size <= maximum,
            'Artifact size outside bound: ' + path.name
        )
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require(
        len(data) == info.st_size and (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns
        ) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            after.st_ctime_ns
        ), 'Artifact changed during intake'
    )
    return data


def validate_stamp(data, revision):
    matches = STAMP.findall(data)
    require(
        len(matches) == 5
        and sorted(int(i) for i, _ in matches) == list(range(5)),
        'Missing or duplicate cached SCM stamp'
    )
    for index, word in matches:
        expected = format((int(revision, 16) >>
                           (32 * int(index))) & 0xffffffff, 'X').encode()
        require(
            word == expected,
            'Cached stamp differs from actual emitter revision'
        )


def validate_cache(repo, cache, pins, manifest_sha256, expected_candidate_sha):
    repo, cache = Path(repo), Path(cache)
    require(
        re.fullmatch(r'[0-9a-f]{64}', manifest_sha256),
        'Reviewed manifest SHA required'
    )
    require(
        re.fullmatch(r'[0-9a-f]{40}', expected_candidate_sha),
        'Candidate SHA required'
    )
    require(
        git(repo, 'rev-parse', 'HEAD') == expected_candidate_sha,
        'Candidate HEAD differs'
    )
    require(
        not git(
            repo, 'status', '--porcelain=v1', '--untracked-files=all',
            '--ignored=matching'
        ), 'Candidate checkout is not clean'
    )
    require(
        cache.is_dir() and not cache.is_symlink(), 'Invalid cache directory'
    )
    require(
        sorted(p.name for p in cache.iterdir()) == sorted(
            (*FILES, 'cache-manifest.json')
        ), 'Unexpected cache files'
    )
    raw_manifest = read_regular(cache / 'cache-manifest.json', 256 * 1024)
    require(
        digest(raw_manifest) == manifest_sha256,
        'Cache manifest differs from reviewed SHA'
    )
    manifest = json.loads(raw_manifest)
    require(
        manifest['schema_version'] == 1
        and manifest['kind'] == 'reference-rtl-cache-v1',
        'Unsupported cache schema'
    )
    emitter = manifest['artifact_emission_sha']
    require(
        emitter == pins['coral_reference_commit'],
        'Only exact pinned reference cache accepted'
    )
    require(manifest['include_count'] == 230, 'Expected exactly 230 includes')
    require(
        set(manifest['files']) == set(FILES), 'Wrong artifact file manifest'
    )
    blobs = {}
    for name, maximum in zip(FILES, (32 * 1024**2, 8 * 1024**2, 64 * 1024)):
        data = read_regular(cache / name, maximum)
        require(
            manifest['files'][name] == {
                'bytes': len(data),
                'sha256': digest(data)
            }, 'Artifact hash/size differs: ' + name
        )
        blobs[name] = data
    require(
        digest(blobs[FILES[0]]) == pins['reference_emitted_rtl_sha256'],
        'Cached top differs from unchanged reference pin'
    )
    validate_stamp(blobs[FILES[0]], emitter)
    inventory = {}
    with zipfile.ZipFile(io.BytesIO(blobs[FILES[1]])) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        require(
            len(members) == 230
            and sum(m.file_size for m in members) <= 32 * 1024**2,
            'Include count/size outside bound'
        )
        for member in members:
            path = PurePosixPath(member.filename)
            require(
                not path.is_absolute() and '..' not in path.parts
                and '\\' not in member.filename and path.name not in inventory,
                'Unsafe/duplicate include path'
            )
            require(
                stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG),
                'Nonregular include'
            )
            data = archive.read(member)
            inventory[path.name] = digest(data)
            if path.name == 'Csr.sv':
                validate_stamp(data, emitter)
            else:
                require(
                    not STAMP.search(data),
                    'SCM stamp outside expected CSR include'
                )
    require(
        'Csr.sv' in inventory and 'Sram.v' in inventory,
        'Missing required include'
    )
    inventory_hash = digest(canonical(inventory))
    require(
        inventory_hash == pins['reference_include_inventory_sha256'] ==
        manifest['include_inventory_sha256'],
        'Include inventory differs from reference'
    )
    parameter_rows = re.findall(
        r'^#define KP_(\w+)\s+(\S+)\s*$', blobs[FILES[2]].decode(), re.M
    )
    parameters = dict(parameter_rows)
    require(
        len(parameters) == len(parameter_rows), 'Duplicate hardware parameter'
    )
    require(
        parameters == manifest['hardware_parameters'],
        'Parameter header differs'
    )
    for name, value in dict(xlen='32', rvvVlen='128', enableRvv='true',
                            enableFloat='true', enableVerification='false',
                            itcmSizeKBytes='8', dtcmSizeKBytes='32',
                            fetchDataBits='128', lsuDataBits='128').items():
        require(
            parameters.get(name) == value,
            'Cached hardware configuration differs'
        )
    reference_closure = closure(repo, emitter)
    candidate_closure = closure(repo, expected_candidate_sha)
    require(
        reference_closure == candidate_closure == manifest['generator_inputs'],
        'Generator/source closure differs from reference emission'
    )
    require(
        git(repo, 'rev-parse', 'HEAD') == expected_candidate_sha and not git(
            repo, 'status', '--porcelain=v1', '--untracked-files=all',
            '--ignored=matching'
        ), 'Candidate changed during intake'
    )
    return {
        'schema_version': 1,
        'rtl_generation': 'cached_pinned_output_verified_not_regenerated',
        'candidate_source_sha': expected_candidate_sha,
        'artifact_emission_sha': emitter,
        'manifest_sha256': manifest_sha256,
        'files': manifest['files'],
        'include_inventory_sha256': inventory_hash,
        'generator_inputs': reference_closure,
        'hardware_parameters': parameters,
        'synthesis_input_bytes': 'unchanged original artifacts',
        'simulation': 'required_not_run_by_intake',
        'physical_execution': 'not_run'
    }
