"""Prepare fixed standalone tops with owner-supplied, hash-verified FPnew files.

This only writes local compiler inputs; it never starts a remote compile or run.
"""
import argparse
import hashlib
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--deps', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
a = p.parse_args()
root = Path(__file__).resolve().parents[2]
a.out.mkdir(parents=True, exist_ok=True)
manifest = json.loads((root / 'results/fpnew-dependencies.json').read_text())
parts = []
for name, digest in manifest['files'].items():
    data = (a.deps / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, name
    parts.append(data.decode())
for name in ['bonsai_dot_cluster', 'bonsai_matmul_tile',
             'bonsai_group_reduce']:
    parts.append((root / 'rtl' / (name + '.sv')).read_text())
source = (root / 'rtl/bonsai_scaled_tile.sv').read_text()
variants = []
for engines in [2, 4, 6, 8]:
    for lookup in [0, 1]:
        top = f'bonsai_scaled_e{engines}_l{lookup}'
        variant = source.replace('module bonsai_scaled_tile', 'module ' + top)
        # The exact declaration spelling is checked to avoid silently building
        # a default configuration after a source refactor.
        import re
        variant, e_count = re.subn(
            r'(parameter integer ENGINES\s*=\s*)2',
            lambda m: m[1] + str(engines), variant
        )
        variant, l_count = re.subn(
            r'(parameter integer LOOKUP\s*=\s*)1',
            lambda m: m[1] + str(lookup), variant
        )
        assert e_count == l_count == 1
        data = ('\n'.join(parts + [variant]) + '\n').encode()
        path = a.out / (top + '.sv')
        path.write_bytes(data)
        variants.append({
            'top': top,
            'engines': engines,
            'lookup': lookup,
            'file': path.name,
            'sha256': hashlib.sha256(data).hexdigest()
        })
(a.out / 'variants.json').write_text(json.dumps(variants, indent=2) + '\n')
print(a.out / 'variants.json')
