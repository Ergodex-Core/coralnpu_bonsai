#!/usr/bin/env python3
"""Prepare a fresh local image context; does not run Docker or mutate the host."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def prepare(output, base_image):
    if not re.fullmatch(r'ubuntu@sha256:[0-9a-f]{64}', base_image):
        raise ValueError('A resolved official Ubuntu image digest is required')
    root = Path(__file__).resolve().parent
    if output.exists():
        raise ValueError('Fresh context required')
    output.mkdir(parents=True)
    for name in ('Dockerfile', 'entrypoint.py', 'trusted-identity.json'):
        shutil.copyfile(root / name, output / name)
    for name in ('bin', 'trusted', 'lib'):
        shutil.copytree(root / name, output / name)
    # Owned by the host file-boundary implementation, copied only at preparation.
    shutil.copyfile(
        root.parent / 'host/secure_files.py', output / 'lib/secure_files.py'
    )
    hashes = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob('*'))
        if p.is_file()
    }
    receipt = {
        'base_image': base_image,
        'files': hashes,
        'network_none_license_qualified': False,
        'runtime_qualified': False,
        'public_dependency_snapshot_qualified': False
    }
    (output / 'context-identity.json'
     ).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
    print(
        json.dumps({
            'context':
            str(output),
            'base_image':
            base_image,
            'docker_build_command': [
                'docker', 'build', '--build-arg', 'BASE_IMAGE=' + base_image,
                '--tag', 'coralnpu-ci:qualification-candidate',
                str(output)
            ]
        },
                   indent=2)
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-image', required=True)
    args = parser.parse_args()
    prepare(args.output, args.base_image)
