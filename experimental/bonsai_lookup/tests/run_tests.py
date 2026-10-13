#!/usr/bin/env python3
"""Local event-driven RTL simulation; never FPGA or Arcsim performance."""
import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixtures', type=pathlib.Path, required=True)
    ap.add_argument('--report', type=pathlib.Path, required=True)
    a = ap.parse_args()
    manifest = json.loads((a.fixtures / 'manifest.json').read_text())
    sources = [
        ROOT / 'rtl/bonsai_dot_cluster.sv', ROOT / 'rtl/bonsai_matmul_tile.sv',
        ROOT / 'tests/tb.sv'
    ]
    rows = []
    with tempfile.TemporaryDirectory() as temp:
        for lookup in (0, 1):
            for engines in (2, 4, 6, 8):
                binary = pathlib.Path(temp) / f'tb-{lookup}-{engines}'
                command = [
                    'iverilog', '-g2012', '-s', 'tb',
                    f'-Ptb.ENGINES={engines}', f'-Ptb.LOOKUP={lookup}', '-o',
                    str(binary), *[str(p) for p in sources]
                ]
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                for case in manifest['cases']:
                    for restart in ([0, 1]
                                    if case['name'] == 'decode' else [0]):
                        folder = a.fixtures / case['name']
                        args = [
                            'vvp',
                            str(binary), f'+MEM={folder}/memory.hex',
                            f'+EXPECTED={folder}/expected.hex',
                            f'+M={case["M"]}', f'+N={case["N"]}',
                            f'+LANES={case["K_valid"]}',
                            f'+INJECT={int(case["name"]=="memory_error")}',
                            f'+JOB_ERROR={case["job_error_expected"]}',
                            f'+RESET_TEST={restart}'
                        ]
                        result = subprocess.run(
                            args,
                            check=True,
                            capture_output=True,
                            text=True,
                            timeout=60
                        )
                        line = next(
                            line for line in result.stdout.splitlines()
                            if line.startswith('RESULT ')
                        )
                        row = {
                            key: int(value)
                            for key, value in re.findall(r'(\w+)=(\d+)', line)
                        }
                        row.update(
                            case=case['name'],
                            kind=case['kind'],
                            status='PASS_RTL_SIMULATION',
                            simulator='Icarus Verilog',
                            FPGA='NOT_RUN',
                            Arcsim='NOT_RUN',
                            useful_mac_equivalents=case[
                                'useful_mac_equivalents'],
                            target_clock_hz=25000000,
                            modeled_seconds=row['cycles'] / 25000000,
                            modeled_read_GB_per_s=row['bytes'] * 25000000 /
                            row['cycles'] / 1e9,
                            lane_capacity_utilization=case[
                                'useful_mac_equivalents'] /
                            (engines * 128 * row['cycles']),
                            compute_and_output_cycles=row['cycles'] -
                            row['load_cycles'],
                            fixture_memory_sha256=case['memory_sha256'],
                            oracle_sha256=case['oracle_json_sha256']
                        )
                        rows.append(row)
    report = {
        'scope':
        'Measured event-driven RTL simulation cycles. Modeled25MHz conversions; NOT FPGA or Arcsim measurements; NOT model inference.',
        'sources': {
            str(p.relative_to(ROOT)):
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
        },
        'oracle_source_sha256': manifest['oracle_source_sha256'],
        'memory_contract': {
            'beat_bytes': 16,
            'max_beats_per_cycle': 1,
            'response_latency_cycles': 8,
            'outstanding_max': 16,
            'request_ready': 'cycle%16<12',
            'output_ready': 'cycle%16<12',
            'response_order': 'highest eligible ID first',
            'overlap':
            'load whole one-group tile before compute; no DMA/compute overlap',
            'output_stores': 'Not modeled; output ready/valid only'
        },
        'passed_cases': len(rows),
        'rows': rows
    }
    a.report.write_text(json.dumps(report, indent=2) + '\n')
    print(
        f'PASS {len(rows)} cases with all values checked; FPGA and Arcsim NOT_RUN'
    )
    for r in rows:
        if r['case'] in ('decode', 'prefill') and not r['reset_test']:
            print(
                r['lookup'], r['engines'], r['case'], r['cycles'],
                r['load_cycles'], r['bytes']
            )


if __name__ == '__main__':
    main()
