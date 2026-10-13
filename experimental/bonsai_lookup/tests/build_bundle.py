#!/usr/bin/env python3
"""Create a small source/fixture handoff; no upload, remote work, or model execution."""
import argparse
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixtures', type=pathlib.Path, required=True)
    ap.add_argument('--output', type=pathlib.Path, required=True)
    a = ap.parse_args()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                       cwd=REPO,
                                       text=True).strip()
    if subprocess.check_output(
        ['git', 'status', '--porcelain', '--', 'experimental/bonsai_lookup'],
            cwd=REPO, text=True).strip():
        raise SystemExit('Commit source changes before packaging')
    with tempfile.TemporaryDirectory() as temp:
        bundle = pathlib.Path(temp) / 'bonsai-standalone'
        bundle.mkdir()
        for name in ('README.md', 'NOTICE.md', 'UPSTREAM_LICENSE.txt'):
            shutil.copy2(ROOT / name, bundle / name)
        for directory in ('rtl', 'tests', 'results'):
            shutil.copytree(
                ROOT / directory,
                bundle / directory,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc')
            )
        shutil.copytree(a.fixtures, bundle / 'fixtures')
        fixture = json.loads((bundle / 'fixtures/manifest.json').read_text())
        for case in fixture['cases']:
            folder = bundle / 'fixtures' / case['name']
            for filename, field in [('memory.hex', 'memory_hex_sha256'),
                                    ('expected.hex', 'expected_hex_sha256'),
                                    ('oracle.json', 'oracle_json_sha256')]:
                if sha(folder / filename) != case[field]:
                    raise ValueError('Fixture checksum mismatch')
        cluster = (ROOT / 'rtl/bonsai_dot_cluster.sv').read_text()
        feeder = (ROOT / 'rtl/bonsai_matmul_tile.sv').read_text()
        ports = feeder.split(') (', 1)[1].split(');', 1)[0]
        names = re.findall(
            r'\b(?:input|output)\s+(?:wire|reg)\s+(?:\[[^]]+\]\s+)?(\w+)',
            ports
        )
        variants = []
        for lookup in (0, 1):
            for count in (2, 4, 6, 8):
                top = f'bonsai_{"lookup" if lookup else "direct"}_e{count}'
                header = ports.replace('ENGINES', str(count)
                                       ).replace('output reg', 'output wire')
                wrapper = f'module {top} (\n{header}\n);\n'
                wrapper += f'bonsai_matmul_tile #(.ENGINES({count}),.LOOKUP({lookup})) dut (\n'
                wrapper += ',\n'.join(
                    f'.{name}({name})' for name in names
                ) + '\n);\nendmodule\n'
                path = bundle / 'variants' / (top + '.sv')
                path.parent.mkdir(exist_ok=True)
                path.write_text(cluster + '\n' + feeder + '\n' + wrapper)
                # Fixed wrapper elaboration check. This is not an Arcsim build.
                subprocess.run([
                    'iverilog', '-g2012', '-s', top, '-o',
                    str(pathlib.Path(temp) / (top + '.vvp')),
                    str(path)
                ],
                               check=True,
                               capture_output=True,
                               timeout=60)
                variants.append({
                    'top':
                    top,
                    'engines':
                    count,
                    'lookup':
                    lookup,
                    'file':
                    str(path.relative_to(bundle)),
                    'sha256':
                    sha(path),
                    'Arcsim':
                    'NOT_RUN',
                    'FPGA':
                    'NOT_RUN',
                    'command':
                    f'arcsim build variants/{top}.sv --top {top} --harness OWNER_ADAPTER.cpp --profile default --cpu x86-64 -o outputs/{top}',
                    'generated_model_requirement':
                    'Generate own matching model.hpp/state.json; inspect actual wide-port API before implementing owner adapter'
                })
        protocol = {
            'schema_version':
            1,
            'scope':
            'Standalone single-native-K-group integer matmul tile; no full Coral or model inference',
            'reset': {
                'active_low':
                True,
                'synchronous':
                True,
                'cycles':
                16,
                'restart':
                'Flush external memory epoch and pending IDs when reset cancels a job'
            },
            'target_clock_hz':
            25000000,
            'memory': {
                'beat_bytes': 16,
                'max_beats_per_cycle': 1,
                'latency_cycles': 8,
                'max_outstanding': 16,
                'request_ready': 'cycle%16<12',
                'response_order': 'highest eligible ID, held until accepted',
                'weight_base': 0,
                'activation_base': 1088
            },
            'output_ready':
            'cycle%16<12',
            'cycle_limit':
            10000,
            'edge_sequence':
            'Drive on low; settle; sample pre-rising handshakes; rising edge/eval; update queues once; lower edge/eval. Record reset separately.',
            'required_scoreboard':
            'Compare every signed dot, FP16 scale bits, mask, error, tag, token/row and final counts. Verify stable requests/results under stalls. Check all pending IDs drained.',
            'ports':
            names,
            'fixture_manifest_sha256':
            sha(bundle / 'fixtures/manifest.json'),
            'source_commit':
            revision,
            'source_repository':
            'Ergodex-Core/coralnpu_bonsai',
            'variants':
            variants,
            'owner_coordination':
            'Parent authorizes window; use existing Arcsim owner lock/resource limits and installed licensed compiler; no S3 transfer or new licensing changes',
            'physical_gate':
            'New qualified FPGA image required for added RTL. Kernel bandwidth/correctness first; full-model FPGA TPS/TTFT/latency only with complete runtime.'
        }
        (bundle /
         'handoff.json').write_text(json.dumps(protocol, indent=2) + '\n')
        files = {
            str(p.relative_to(bundle)): sha(p)
            for p in sorted(bundle.rglob('*'))
            if p.is_file()
        }
        (bundle / 'MANIFEST.json').write_text(
            json.dumps({
                'source_commit': revision,
                'files': files
            }, indent=2) + '\n'
        )
        a.output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(a.output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    archive.write(path, str(path.relative_to(bundle.parent)))
        print(
            json.dumps({
                'file': a.output.name,
                'bytes': a.output.stat().st_size,
                'sha256': sha(a.output),
                'source_commit': revision,
                'variants': len(variants),
                'files': len(files) + 1
            },
                       indent=2)
        )


if __name__ == '__main__':
    main()
