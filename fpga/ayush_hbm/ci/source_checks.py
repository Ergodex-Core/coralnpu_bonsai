#!/usr/bin/env python3
"""Unprivileged source checks; receipts cannot authorize a licensed build."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from inputs import sha
from process import require, run

CI = Path(__file__).resolve().parent
REPO = CI.parents[2]
WORKFLOW = '.github/workflows/ayush-hbm.yml'


def validate_checkout(repo, expected):
    require(
        re.fullmatch(r'[0-9a-f]{40}', expected), 'Expected full commit SHA'
    )
    actual = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                     cwd=repo,
                                     text=True,
                                     timeout=10).strip()
    require(actual == expected, 'Checkout does not match expected source SHA')
    status = subprocess.check_output([
        'git', 'status', '--porcelain', '--untracked-files=all'
    ],
                                     cwd=repo,
                                     text=True,
                                     timeout=10)
    require(not status, 'Source checkout must be clean')
    return actual


def check(expected, output):
    output = output.resolve()
    require(
        not output.is_relative_to(REPO), 'Evidence must be outside checkout'
    )
    output.mkdir(exist_ok=False, parents=True)
    result = {
        'status': 'failed',
        'expected_source_sha': expected,
        'scope': 'source checks only; untrusted PR diagnostics',
        'licensed_build': 'NOT_RUN',
        'hardware_calibration': 'NOT_RUN',
        'physical_inference': 'NOT_RUN',
        'qualified_checkpoint': False,
        'checks': []
    }
    try:
        result['checked_out_sha'] = validate_checkout(REPO, expected)
        result['workflow_sha256'] = sha(REPO / WORKFLOW)
        result['source_manifest_sha256'] = sha(CI.parent / 'SHA256SUMS')
        result['pins_sha256'] = sha(CI / 'pins.json')
        result['tools_lock_sha256'] = sha(CI / 'requirements-source.lock')
        python = sys.executable
        shells = sorted(str(p) for p in CI.rglob('*.sh'))
        commands = [
            (
                'dry-run',
                ['bash', str(CI / 'build_from_coral.sh'), '--dry-run']
            ),
            (
                'unit-tests', [
                    python, '-m', 'unittest', 'discover', '-s',
                    str(CI), '-p', 'test_*.py', '-v'
                ]
            ),
            (
                'python-format', [python, '-m', 'yapf', '--diff'] +
                sorted(str(p) for p in CI.rglob('*.py'))
            ),
            ('shellcheck', [str(Path(python).parent / 'shellcheck')] + shells),
            ('macro-signatures', [python, 'utils/check_macro_signatures.py'])
        ]
        commands.extend((f'bash-{i}', ['bash', '-n', p])
                        for i, p in enumerate(shells))
        for name, command in commands:
            record = {'name': name, 'status': 'failed'}
            result['checks'].append(record)
            run(command, output / (name + '.log'), REPO, timeout=120)
            record['status'] = 'passed'
        validate_checkout(REPO, expected)
        result['status'] = 'passed'
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        (output /
         'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    check(args.expected_sha, args.output)
