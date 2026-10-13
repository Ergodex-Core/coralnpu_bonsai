"""Run reproducible scaled projections; report simulation cycles, never FPGA TPS."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

p = argparse.ArgumentParser()
p.add_argument('--deps', type=Path, required=True)
p.add_argument('--fixtures', type=Path, required=True)
p.add_argument('--out', type=Path, required=True)
a = p.parse_args()
root = Path(__file__).resolve().parents[2]
a.out.mkdir(parents=True, exist_ok=True)
fixture_manifest = json.loads((a.fixtures / 'manifest.json').read_text())
reference = json.loads((root / 'results/layer-fixtures.json').read_text())
for record in reference['records']:
    paths = {record['role'] + '/weights.pq2': record['weight_sha256']}
    paths.update({
        name: meta['sha256']
        for name, meta in record['files'].items()
    })
    for name, digest in paths.items():
        assert hashlib.sha256((a.fixtures / name).read_bytes()
                              ).hexdigest() == digest, name

report = {
    'scope':
    'Verilator composed scaled-projection RTL simulation; synthetic activations; no FPGA measurements',
    'memory_service':
    '16 bytes aggregate/cycle; latency8; max16 outstanding; request/output ready cycle%16<12; highest eligible ID responses; held payload under backpressure',
    'timing_boundary':
    'Sum of observed accepted-job-to-done counters; command_issue_cycles separately counted. Reset, host packing, descriptor/HBM output writes, production AXI/DMA and complete model work excluded.',
    'fp_arithmetic':
    'Pinned FPnew FP32, 4 distributed pipeline registers; RNE MUL, MUL, ADD separately; one shared serial reducer; flags sticky per output across groups',
    'verilator':
    subprocess.check_output(['verilator', '--version'], text=True).strip(),
    'model_sha256':
    reference['model_sha256'],
    'sources': {
        str(f.relative_to(root)): hashlib.sha256(f.read_bytes()).hexdigest()
        for folder in ['rtl', 'tests/scaled']
        for f in (root / folder).iterdir()
        if f.is_file() and f.suffix in ['.sv', '.cc', '.py']
    },
    'dependency_manifest':
    json.loads((root / 'results/fpnew-dependencies.json').read_text()),
    'physical_FPGA':
    'NOT_RUN',
    'Arcsim_scaled_projection':
    'NOT_RUN',
    'records': [],
}


def save():
    (a.out /
     'scaled-simulation.json').write_text(json.dumps(report, indent=2) + '\n')


for engines in [2, 4, 6, 8]:
    for lookup in [1, 0]:
        build = a.out / f'e{engines}l{lookup}'
        subprocess.run([
            'python3',
            str(Path(__file__).with_name('build.py')), '--deps',
            str(a.deps), '--out',
            str(build), '--engines',
            str(engines), '--lookup',
            str(lookup)
        ],
                       check=True)
        binary = build / 'Vbonsai_scaled_tile'
        for ref in reference['records']:
            # Complete all 14 workloads for primary E2 lookup. Compare full Q
            # decode/prefill at every other engine/datapath variant.
            if (engines, lookup) != (2, 1) and ref['role'] != 'q':
                continue
            cmd = [
                str(binary),
                str(a.fixtures), ref['role'], ref['mode'],
                str(ref['N']),
                str(ref['K']),
                str(ref['M']), '0'
            ]
            begin = time.monotonic()
            result = json.loads(subprocess.check_output(cmd, text=True))
            result.update({
                'engines': engines,
                'lookup': lookup,
                'role': ref['role'],
                'mode': ref['mode'],
                'M': ref['M'],
                'N': ref['N'],
                'K': ref['K'],
                'coverage':
                'complete projection, every output checked bit-for-bit',
                'simulator_wall_seconds': time.monotonic() - begin
            })
            report['records'].append(result)
            save()
            print(
                engines,
                lookup,
                ref['role'],
                ref['mode'],
                result['target_cycles'],
                flush=True
            )
        if (engines, lookup) == (2, 1):
            report['protocol_checks'] = []
            for fault, name in enumerate(
                ['scale_response_error', 'wrong_scale_ID',
                 'tile_response_error', 'reserved_PQ2_code', 'invalid_shape',
                 'reset_epoch_flush_restart', 'N31_tail',
                 'zero_activation_scale', 'address_overflow'], 1):
                result = json.loads(
                    subprocess.check_output([
                        str(binary),
                        str(a.fixtures), 'q', 'decode', '2048', '2048', '1',
                        '1',
                        str(fault)
                    ],
                                            text=True)
                )
                result['case'] = name
                report['protocol_checks'].append(result)
                save()
print(a.out / 'scaled-simulation.json')
