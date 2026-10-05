"""Strict HBM report gates. Unknown formats and unreviewed exceptions fail closed.

This profile accepts no DRC/methodology findings, CDC warnings/criticals, timing
exceptions, waivers or changed DRC severities. It does not grandfather the
retained checkpoint's findings. Hardware calibration is a separate NOT_RUN gate.
"""
import json
import math
from pathlib import Path
import re

from inputs import PINS, sha
from process import require

REPORTS = (
    'route_status.rpt', 'timing_summary.rpt', 'clocks.rpt',
    'clock_interaction.rpt', 'check_timing.rpt', 'cdc.rpt', 'bus_skew.rpt',
    'exceptions.rpt', 'drc.rpt', 'methodology.rpt', 'facts.tsv'
)


def one(pattern, text):
    values = re.findall(pattern, text, re.M)
    require(len(values) == 1, 'Missing or ambiguous report field: ' + pattern)
    return values[0]


def timing(text):
    section = re.split(r'^\| Design Timing Summary\s*$', text, flags=re.M)
    require(len(section) == 2, 'Missing timing summary')
    section = re.split(r'^\| [A-Za-z]', section[1], maxsplit=1, flags=re.M)[0]
    require(
        'WNS(ns)' in section and 'TPWS Total Endpoints' in section,
        'Unknown timing columns'
    )
    row = one(
        r'^\s*((?:[+-]?\d+(?:\.\d+)?\s+){11}[+-]?\d+(?:\.\d+)?)\s*$', section
    )
    values = [float(v) for v in row.split()]
    require(all(math.isfinite(v) for v in values), 'Nonfinite timing')
    require(
        all(values[i] >= 0 for i in (0, 4, 8))
        and all(values[i] == 0 for i in (1, 2, 5, 6, 9, 10))
        and all(values[i] > 0 and values[i].is_integer() for i in (3, 7, 11)),
        'Setup, hold, pulse width or endpoint coverage failed'
    )
    table = re.split(r'^\| Unconstrained Path Table\s*$', text, flags=re.M)
    require(len(table) == 2, 'Missing unconstrained path table')
    table = re.split(r'^\| [A-Za-z]', table[1], maxsplit=1, flags=re.M)[0]
    for line in table.splitlines():
        if line.strip() and set(line.strip()) - set('-| ') and not line.strip(
        ).startswith('Path Group'):
            raise RuntimeError(
                'Unconstrained paths or unsupported table format'
            )
    return values


def check_timing(text):
    expected = (
        'no_clock constant_clock pulse_width_clock unconstrained_internal_endpoints '
        'no_input_delay no_output_delay multiple_clock generated_clocks loops '
        'partial_input_delay partial_output_delay latch_loops'
    ).split()
    rows = re.findall(r'^\d+\. checking ([a-z_]+) \((\d+)\)$', text, re.M)
    require(len(rows) in (12, 24), 'Incomplete check_timing report')
    require([r[0] for r in rows[:12]] == expected,
            'Unknown check_timing categories')
    require(
        len(rows) == 12 or rows[:12] == rows[12:], 'Inconsistent timing checks'
    )
    require(
        all(int(n) == 0 for _, n in rows),
        'Unreviewed timing coverage findings'
    )


def cdc(text):
    command = one(r'^\| Command\s*:\s*(report_cdc[^\n]+)$', text)
    require(
        '-details' in command
        and not re.search(r'-(?:cells|from|to|severity|waived)\b', command),
        'Scoped or incomplete CDC command'
    )
    body = one(
        r'(?s)\nCDC Report\s*\n\s*ID\s+Severity\s+Count\s+Description\s*\n[- ]+\n(.*)\Z',
        text
    )
    summary, _, details = body.partition('Source Clock:')
    rows = []
    for line in summary.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(
            r'(CDC-\d+)\s+(Info|Warning|Critical)\s+(\d+)\s+(.+)', line.strip()
        )
        require(match, 'Unknown CDC summary format')
        rows.append(match.groups()[:3])
    require(rows, 'Empty CDC coverage is not qualified for this HBM design')
    require(len({r[0] for r in rows}) == len(rows), 'Duplicate CDC rule')
    endpoints = re.findall(
        r'^\s*\d+\s+(CDC-\d+)\s+(Info|Warning|Critical)\s+', details, re.M
    )
    require(
        len(endpoints) == sum(int(r[2]) for r in rows), 'CDC count mismatch'
    )
    for rule, severity, count in rows:
        require(
            endpoints.count((rule, severity)) == int(count),
            'CDC detail mismatch'
        )
        require(
            severity == 'Info' or int(count) == 0,
            'Unreviewed CDC finding: ' + rule
        )


def skew(text):
    ids = re.findall(r'^([1-9]\d*)\s+\d+\s+\[get_', text, re.M)
    slacks = re.findall(
        r'^Slack \((MET|VIOLATED)\)\s*:\s*([+-]?\d+(?:\.\d+)?)ns', text, re.M
    )
    count = PINS['bus_skew_constraints']
    require(
        ids == [str(i) for i in range(1, count + 1)] and len(slacks) == count,
        'Missing or unknown HBM bus-skew constraint coverage'
    )
    require(
        all(state == 'MET' and float(value) >= 0 for state, value in slacks),
        'Bus skew failed'
    )


def no_findings(text):
    require(
        int(one(r'^\s*Checks found:\s*(\d+)\s*$', text)) == 0,
        'Unreviewed DRC/methodology findings'
    )
    details = re.split(r'\n2\. REPORT DETAILS\n-+\n', text)
    require(
        len(details) == 2 and not details[1].strip(),
        'Unknown/nonempty DRC details'
    )


def exceptions(text):
    command = one(r'^\| Command\s*:\s*(report_exceptions[^\n]+)$', text)
    require(
        '-coverage' in command
        and not re.search(r'-(?:from|to|through|ignored|summary)\b', command),
        'Incomplete timing exception coverage'
    )
    require(
        'Exceptions Report' in text and 'Position' in text
        and 'Endpoints' in text, 'Unknown exceptions report'
    )
    require(
        not re.search(r'^\s*\d+\s+', text, re.M),
        'Timing exceptions require an independently reviewed policy; none are approved'
    )


def qualify(directory):
    directory = Path(directory)
    result = {
        'qualified': False,
        'blockers': [],
        'reports': {},
        'hardware_calibration': 'NOT_RUN',
        'physical_inference': 'NOT_RUN'
    }
    contents = {}
    for name in REPORTS:
        try:
            text = (directory / name).read_text()
            require(bool(text.strip()), 'Empty report')
            contents[name] = text
            result['reports'][name] = sha(directory / name)
        except (OSError, UnicodeError, RuntimeError) as exc:
            result['blockers'].append(f'{name}: {exc}')
    for name, checker in [
        ('timing_summary.rpt', timing), ('check_timing.rpt', check_timing),
        ('cdc.rpt', cdc), ('bus_skew.rpt', skew), ('drc.rpt', no_findings),
        ('methodology.rpt', no_findings), ('exceptions.rpt', exceptions)
    ]:
        try:
            checker(contents.get(name, ''))
        except (RuntimeError, ValueError) as exc:
            result['blockers'].append(f'{name}: {exc}')
    try:
        rows = [
            line.split('\t')
            for line in contents.get('facts.tsv', '').splitlines()
        ]
        require(all(len(r) == 2 for r in rows), 'Malformed facts')
        facts = dict(rows)
        require(len(facts) == len(rows), 'Duplicate facts')
        expected = {
            'fully_routed': '1',
            'route_errors': '0',
            'waivers': '0',
            'drc_violations': '0',
            'changed_drc_rules': '0'
        }
        require(
            all(facts.get(k) == v for k, v in expected.items()),
            'Routing/DRC/waiver facts failed'
        )
        for name, period in [('npu_clk', 20), ('clk_main_a0', 4),
                             ('clk_out1_cl_hbm_mmcm', 10 / 3)]:
            require(
                abs(float(facts['clock.' + name]) - period) < .002,
                'Clock fact failed'
            )
    except (KeyError, ValueError, RuntimeError) as exc:
        result['blockers'].append(f'facts.tsv: {exc}')
    result['qualified'] = not result['blockers']
    (directory /
     'qualification.json').write_text(json.dumps(result, indent=2) + '\n')
    return result
