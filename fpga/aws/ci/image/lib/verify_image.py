#!/usr/bin/env python3
"""Verify baked policy identity at image construction; no submitted code runs."""
import hashlib
import json
from pathlib import Path

root = Path('/opt/coral-ci')
identity = json.loads((root / 'trusted-identity.json').read_text())
for name, digest in identity['files'].items():
    actual = hashlib.sha256((root / 'trusted' / name).read_bytes()).hexdigest()
    if actual != digest:
        raise RuntimeError('Trusted source identity mismatch: ' + name)
for phase in ('stage', 'build', 'qualify', 'collect'):
    if not (root / 'bin' / phase).is_file():
        raise RuntimeError('Missing phase helper')
if not (root / 'lib/secure_files.py').is_file():
    raise RuntimeError('Missing bounded collector')
print('CORAL_IMAGE_BAKED_POLICY_VERIFIED')
