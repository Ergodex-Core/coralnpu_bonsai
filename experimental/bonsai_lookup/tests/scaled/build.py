"""Build bounded scaled-tile tests against hash-pinned external Alex FPnew RTL."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--deps', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
p.add_argument('--engines', type=int, default=2, choices=[2, 4, 6, 8])
p.add_argument('--lookup', type=int, default=1, choices=[0, 1])
p.add_argument('--reducer', action='store_true')
a = p.parse_args()
root = Path(__file__).resolve().parents[2]
manifest = json.loads((root / 'results/fpnew-dependencies.json').read_text())
for name, expected in manifest['files'].items():
    assert hashlib.sha256((a.deps / name).read_bytes()
                          ).hexdigest() == expected, name
a.out.mkdir(parents=True, exist_ok=True)
# Suppress only known diagnostics in pinned third-party source, never new RTL.
config = a.out / 'external.vlt'
config.write_text(
    '`verilator_config\n' + ''.join(
        f'lint_off -rule {rule} -file "{a.deps.resolve()}/*"\n'
        for rule in ['ASCRANGE', 'WIDTHEXPAND', 'WIDTHTRUNC']
    )
)
top = 'bonsai_group_reduce' if a.reducer else 'bonsai_scaled_tile'
files = [str(a.deps.resolve() / name) for name in manifest['files']]
files += [str(root / 'rtl/bonsai_group_reduce.sv')]
if not a.reducer:
    files += [
        str(root / 'rtl' / (n + '.sv')) for n in
        ['bonsai_dot_cluster', 'bonsai_matmul_tile', 'bonsai_scaled_tile']
    ]
command = [
    'verilator', '--cc', '--exe', '--build', '-j', '2', '--top-module', top,
    '--Mdir',
    str(a.out.resolve()), '-CFLAGS', '-O2 -fno-fast-math -ffp-contract=off',
    str(config.resolve()), *files,
    str(
        Path(__file__
             ).with_name('reducer_tb.cc' if a.reducer else 'projection_tb.cc')
    )
]
if not a.reducer:
    command += [f'-GENGINES={a.engines}', f'-GLOOKUP={a.lookup}']
with (a.out / 'build.log').open('w') as log:
    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
print(a.out.resolve() / ('V' + top))
