"""Fail-closed Vivado 2025.2 evidence gates and native AWS DCP packaging."""
import hashlib
import io
import math
import os
from pathlib import Path
import re
import tarfile
import tempfile


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class EvidenceError(ValueError):

    def __init__(self, message, metrics=None):
        super().__init__(message)
        self.metrics = metrics


def require(condition, message, metrics=None):
    if not condition:
        raise EvidenceError(message, metrics)


def _messages(log, severity):
    # The pinned HDK print procedure prefixes messages with AWS FPGA and %T.
    prefix = r'^\s*(?:AWS FPGA:\s*\(\d{2}:\d{2}:\d{2}\):\s*)?'
    return re.findall(prefix + re.escape(severity) + r':[^\n]*$', log, re.M)


def _ddr_calibration(facts):
    require(
        facts.get('ddr.calibration_bram_cells') == '1',
        'Expected exactly one DDR calibration BRAM'
    )
    value = facts.get('ddr.calibration_init_2c', '')
    match = re.fullmatch(r"256'h([0-9a-fA-F]{64})", value)
    require(
        match is not None and int(match[1], 16) != 0,
        'DDR calibration INIT_2C missing, malformed or zero'
    )
    return value


def validate_stage(log: str, stage: str):
    require(re.fullmatch(r'[A-Za-z_]+', stage), 'Invalid stage name')
    require(not _messages(log, 'ERROR'), f'{stage}: emitted ERROR')
    marker = f'CORAL_STAGE_{stage.upper()}_PASSED'
    require(
        len(re.findall(rf'^{marker}\s*$', log, re.M)) == 1,
        f'{stage}: missing or duplicate completion marker'
    )


def _one(pattern, text):
    matches = re.findall(pattern, text, re.M)
    require(len(matches) == 1, f'Missing/ambiguous field: {pattern}')
    return matches[0]


def _section(text, name):
    matches = list(re.finditer(rf'^\| {re.escape(name)}\s*$', text, re.M))
    require(len(matches) == 1, f'Missing/ambiguous section: {name}')
    return re.split(
        r'^\| [A-Za-z]', text[matches[0].end():], maxsplit=1, flags=re.M
    )[0]


def _route(text):
    counts = {
        key: int(_one(rf'^\s*# of {label}\.+\s*:\s*(\d+)\s*:', text))
        for key, label in [('routable',
                            'routable nets'), ('routed', 'fully routed nets'),
                           ('errors', 'nets with routing errors')]
    }
    require(
        counts['routable'] > 0 and counts['routed'] == counts['routable']
        and counts['errors'] == 0, f'Incomplete routing: {counts}'
    )
    return counts


def _timing(text):
    section = _section(text, 'Design Timing Summary')
    require(
        'WNS(ns)' in section and 'TPWS Total Endpoints' in section,
        'Unknown timing columns'
    )
    row = _one(
        r'^\s*((?:[+-]?\d+(?:\.\d+)?\s+){11}[+-]?\d+(?:\.\d+)?)\s*$', section
    )
    values = [float(x) for x in row.split()]
    require(all(math.isfinite(x) for x in values), 'Nonfinite timing')
    metrics = dict(
        zip((
            'wns', 'tns', 'setup_failing', 'setup_total', 'whs', 'ths',
            'hold_failing', 'hold_total', 'wpws', 'tpws', 'pulse_failing',
            'pulse_total'
        ), values)
    )
    require(
        all(values[i] >= 0 for i in (0, 4, 8))
        and all(values[i] == 0 for i in (1, 2, 5, 6, 9, 10))
        and all(values[i] > 0 and values[i].is_integer() for i in (3, 7, 11)),
        'Timing failed or coverage empty', metrics
    )
    unconstrained = _section(text, 'Unconstrained Path Table')
    rows = [
        line.strip()
        for line in unconstrained.splitlines()
        if line.strip() and set(line.strip()) -
        set('-| ') and not line.strip().startswith('Path Group')
    ]
    metrics['unconstrained_groups'] = rows
    require(
        not rows, f'Unconstrained path groups or unknown table format: {rows}',
        metrics
    )
    return metrics


def _drc(text):
    sections = re.findall(
        r'\n1\. REPORT SUMMARY\n-+\n(.*?)\n2\. REPORT DETAILS\n-+\n', text,
        re.S
    )
    require(len(sections) == 1, 'Unknown DRC summary format')
    total = int(_one(r'^\s*Checks found:\s*(\d+)\s*$', sections[0]))
    pattern = r'^\|\s*(\S+)\s*\|\s*(Error|Critical Warning|Warning|Advisory|Info)\s*\|[^\n]*\|\s*(\d+)\s*\|$'
    table_rows = [
        line for line in sections[0].splitlines()
        if line.startswith('|') and not re.match(r'\|\s*Rule\s*\|', line)
    ]
    require(
        all(re.fullmatch(pattern, line) for line in table_rows),
        'Unknown DRC row/severity'
    )
    rules = re.findall(pattern, sections[0], re.M)
    require(
        sum(int(row[2]) for row in rules) == total,
        'DRC summary counts do not reconcile'
    )
    metrics = {'violations': total, 'rules': rules}
    require(total == 0, f'Unreviewed DRC violations: {rules}', metrics)
    details = re.split(r'\n2\. REPORT DETAILS\n-+\n', text)[-1]
    require(
        not details.strip(), 'Unexpected details in zero-violation DRC report',
        metrics
    )
    return metrics


def _methodology(text):
    require(
        re.search(r'^Report Methodology\s*$', text, re.M),
        'Unknown methodology report'
    )
    return _drc(text)


def _cdc(text):
    # Vivado 2025.2 text table; reject unknown severities, columns and scoped reports.
    command = _one(r'^\| Command\s*:\s*(report_cdc[^\n]+)$', text)
    require(
        not re.search(r'-(?:cells|from|to|severity|waived)\b', command),
        'Scoped/filtered CDC report'
    )
    body = _one(
        r'(?s)\nCDC Report\s*\n\s*ID\s+Severity\s+Count\s+Description\s*\n[- ]+\n(.*)\Z',
        text
    )
    summary, _, details = body.partition('Source Clock:')
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    pattern = r'(CDC-\d+)\s+(Info|Warning|Critical)\s+(\d+)\s+(.+)'
    rows = [re.fullmatch(pattern, line) for line in lines]
    require(all(rows), 'Unknown CDC summary row')
    rules = [row.groups()[:3] for row in rows]
    require(
        len({rule[0]
             for rule in rules}) == len(rules), 'Duplicate CDC rule'
    )
    detailed = re.findall(
        r'^\s*\d+\s+(CDC-\d+)\s+(Info|Warning|Critical)\s+', details, re.M
    )
    require(
        all(
            sum(item == (rule, severity)
                for item in detailed) == int(count)
            for rule, severity, count in rules
        ) and len(detailed) == sum(int(row[2]) for row in rules),
        'CDC detail counts do not reconcile'
    )
    metrics = {'rules': rules, 'endpoints': len(detailed)}
    require(
        not any(
            severity != 'Info' and int(count) for _, severity, count in rules
        ), 'Unreviewed CDC issues', metrics
    )
    return metrics


def _exceptions(text):
    command = _one(r'^\| Command\s*:\s*(report_exceptions[^\n]+)$', text)
    require(
        '-coverage' in command and not re.search(
            r'-(?:from|to|through|rise_\w+|fall_\w+|ignored|summary)\b',
            command
        ), 'Missing coverage or scoped/filtered exceptions report'
    )
    body = _one(r'(?s)\nExceptions Report\s*\n(.*)\Z', text).splitlines()
    require(
        len(body) >= 2 and re.fullmatch(r'[- ]+', body[1]),
        'Unknown exceptions table'
    )
    starts = [match.start() for match in re.finditer('-+', body[1])]
    columns = lambda line: [
        line[a:b].strip() for a, b in zip(starts, starts[1:] + [None])
    ]
    require(
        columns(body[0]) == [
            'Position', 'Type', 'Setup', 'Hold', 'From', 'Through', 'To',
            'Endpoints', 'From (%)', 'Through (%)', 'To (%)'
        ], 'Unknown exceptions columns'
    )
    footer = 'Warning: the percentages reported indicate the number of pins covered by the exception relative to the number of pins specified implicitly or explicitly.'
    rows, issues = [], []
    for line in body[2:]:
        if not line.strip() or line == footer:
            continue
        row = columns(line)
        require(
            len(row) == 11 and row[0].isdigit() and row[7].isdigit(),
            'Malformed exception row'
        )
        require(
            row[1] in (
                'False Path', 'Max Delay', 'Min Delay',
                'Max Delay Datapath Only', 'Multicycle Path'
            ), f'Unknown exception type: {row[1]}'
        )
        percentages = []
        for objects, percentage in zip(row[4:7], row[8:11]):
            require(
                not objects
                or re.fullmatch(r'\d+ (pins|cells|ports|clocks)', objects),
                'Unknown exception object count'
            )
            require(
                bool(objects) == bool(percentage), 'Missing exception coverage'
            )
            if percentage:
                require(
                    re.fullmatch(r'\d+(?:\.\d+)?', percentage),
                    'Malformed exception percentage'
                )
                percentages.append(float(percentage))
        require(
            percentages and all(0 <= value <= 100 for value in percentages),
            'Invalid exception percentage'
        )
        rows.append({
            'position': int(row[0]),
            'type': row[1],
            'endpoints': int(row[7]),
            'coverage': percentages
        })
        if int(row[7]) == 0 or min(percentages) == 0:
            issues.append(int(row[0]))
    metrics = {'constraints': rows, 'invalid_positions': issues}
    require(
        len({row['position']
             for row in rows}) == len(rows), 'Duplicate exception position'
    )
    require(not issues, f'Uncovered timing exceptions: {issues}', metrics)
    require(
        not rows,
        'Unreviewed exception records require an approved disposition', metrics
    )
    return metrics


def _check_timing(text):
    found = re.findall(r'^\d+\. checking ([a-z_]+) \((\d+)\)$', text, re.M)
    names = (
        'no_clock constant_clock pulse_width_clock unconstrained_internal_endpoints no_input_delay '
        'no_output_delay multiple_clock generated_clocks loops partial_input_delay partial_output_delay latch_loops'
    ).split()
    require(
        len(found) == 24 and found[:12] == found[12:]
        and [x[0] for x in found[:12]] == names,
        'Unknown or inconsistent check_timing format'
    )
    issues = {key: int(value) for key, value in found[:12] if int(value)}
    require(not issues, f'Unreviewed check_timing exceptions: {issues}')
    return dict(found[:12])


def _bus_skew(text):
    ids = re.findall(r'^([1-9]\d*)\s+\d+\s+\[get_', text, re.M)
    slacks = re.findall(
        r'^Slack \((MET|VIOLATED)\)\s*:\s*([+-]?\d+(?:\.\d+)?)ns', text, re.M
    )
    require(
        ids == ['1', '2', '3', '4', '5'] and len(slacks) == 5,
        'Missing/unknown bus-skew constraints'
    )
    require(
        all(state == 'MET' and float(slack) >= 0 for state, slack in slacks),
        'Bus-skew violation'
    )
    return [float(slack) for _, slack in slacks]


def qualify(
    report_dir: Path, logs: dict[str, Path], checkpoint: Path, pins: dict
) -> dict:
    result = {
        'qualified': False,
        'blockers': [],
        'reports': {},
        'metrics': {},
        'physical_execution': 'not_run'
    }

    def inspect(name, action):
        try:
            return action()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['blockers'].append(f'{name}: {exc}')
            return getattr(exc, 'metrics', None)

    def read(name):
        path = Path(report_dir) / name
        text = path.read_text()
        require(bool(text.strip()), 'Empty report')
        result['reports'][name] = sha256(path)
        return text

    def checkpoint_check():
        require(
            '.VIOLATED' not in checkpoint.name.upper(), 'Violated checkpoint'
        )
        require(
            checkpoint.is_file() and checkpoint.stat().st_size > 0,
            'Missing/empty checkpoint'
        )
        return sha256(checkpoint)

    checkpoint = Path(checkpoint)
    result['checkpoint_sha256'] = inspect('checkpoint', checkpoint_check)
    for stage in ('synthesis', 'implementation', 'validation_reports'):

        def check(stage=stage):
            path = logs.get(
                stage,
                logs.get('validation')
                if stage == 'validation_reports' else None
            )
            require(path is not None, 'Missing stage log')
            text = Path(path).read_text()
            warnings = _messages(text, 'CRITICAL WARNING')
            if warnings:
                result['blockers'].append(
                    f'{stage}: Unreviewed critical warnings: {warnings}'
                )
            validate_stage(text, stage)
            if stage == 'synthesis' and pins.get('ddr_enabled'):
                validate_stage(text, 'ddr_calibration')
            if stage == 'implementation':
                for part in ('link', 'optimization', 'placement',
                             'physical_optimization', 'routing'):
                    validate_stage(text, part)

        inspect(stage, check)
    for name, parser in [('route_status', _route), ('timing_summary', _timing),
                         ('drc', _drc), ('check_timing', _check_timing),
                         ('bus_skew', _bus_skew),
                         ('methodology', _methodology), ('cdc', _cdc),
                         ('exceptions', _exceptions)]:
        result['metrics'][name] = inspect(
            name, lambda name=name, parser=parser: parser(read(name + '.rpt'))
        )

    def facts_check():
        rows = [line.split('\t') for line in read('facts.tsv').splitlines()]
        require(
            all(len(row) == 2 for row in rows)
            and len(dict(rows)) == len(rows), 'Malformed/duplicate facts'
        )
        facts = dict(rows)
        for key, expected in [
            ('fully_routed', 1), ('route_errors', 0), ('drc_errors', 0),
            ('changed_drc_severities', 0), ('existing_waivers', 0),
            ('clock.clk_main_a0.period_ns', 1e9 / pins['shell_clock_hz']),
            ('clock.npu_clk.period_ns', 1e9 / pins['core_clock_hz'])
        ]:
            require(
                float(facts[key]) == expected,
                f'Unexpected {key}: {facts[key]}'
            )
        if pins.get('ddr_enabled'):
            _ddr_calibration(facts)
        return facts

    result['metrics']['facts'] = inspect('facts', facts_check)

    def identity():
        for name in ('timing_summary.rpt', 'clocks.rpt', 'drc.rpt'):
            text = read(name)
            require(
                re.search(
                    r'Vivado v\.' + re.escape(pins['vivado_version']) + r'\s',
                    text
                ) and re.search(
                    r'Build ' + re.escape(pins['vivado_build']) + r'\b', text
                ), 'Unexpected Vivado version/build'
            )
        require(pins['device'] in read('drc.rpt'), 'Unexpected device')

    inspect('identity', identity)
    inspect('drc_check_properties', lambda: read('drc_check_properties.rpt'))
    result['qualified'] = not result['blockers']
    return result


def package(output_tar, checkpoint, probe_file, manifest: dict):
    """Package only after caller qualification; refuse existing output or changed inputs."""
    output_tar, checkpoint, probe_file = map(
        Path, (output_tar, checkpoint, probe_file)
    )
    require('.VIOLATED' not in checkpoint.name.upper(), 'Violated checkpoint')
    for path in (checkpoint, probe_file):
        require(
            path.is_file() and not path.is_symlink()
            and path.stat().st_size > 0, f'Invalid input: {path}'
        )
    required = {
        'pci_device_id', 'pci_vendor_id', 'pci_subsystem_id',
        'pci_subsystem_vendor_id', 'manifest_format_version', 'dcp_hash',
        'shell_version', 'hdk_version', 'tool_version', 'date',
        'clock_recipe_a', 'clock_recipe_b', 'clock_recipe_c',
        'clock_recipe_hbm', 'dcp_file_name'
    }
    require(required == set(manifest), 'Unexpected/missing manifest keys')
    tag = str(manifest['date'])
    require(
        re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,82}', tag), 'Unsafe tag'
    )
    require(
        manifest['dcp_file_name'] == f'{tag}.SH_CL_routed.dcp',
        'Unexpected DCP member name'
    )
    for key, value in manifest.items():
        require(
            isinstance(value,
                       (str, int)) and not re.search(r'[\r\n=]', str(value)),
            f'Unsafe manifest value: {key}'
        )
    require(
        str(manifest['manifest_format_version']) == '2',
        'Unsupported manifest version'
    )
    expected = {
        'pci_device_id': '0xf010',
        'pci_vendor_id': '0x1d0f',
        'pci_subsystem_id': '0x1d51',
        'pci_subsystem_vendor_id': '0xfedc'
    }
    require(
        all(
            str(manifest[key]).lower() == value
            for key, value in expected.items()
        ), 'Unexpected PCI identity'
    )
    hashes = [sha256(checkpoint), sha256(probe_file)]
    require(
        manifest['dcp_hash'] == hashes[0], 'Manifest checkpoint hash mismatch'
    )
    manifest_bytes = ''.join(
        f'{key}={manifest[key]}\n\n' for key in sorted(manifest)
    ).encode()
    names = [
        f'to_aws/{tag}.SH_CL_routed.dcp', f'to_aws/{tag}.debug_probes.ltx',
        f'to_aws/{tag}.manifest.txt'
    ]
    require(not output_tar.exists(), 'Refusing to overwrite output archive')
    with tempfile.NamedTemporaryFile(dir=output_tar.parent, suffix='.tar',
                                     delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with tarfile.open(temporary_path, 'w',
                          format=tarfile.USTAR_FORMAT) as archive:
            for name, path in zip(names[:2], (checkpoint, probe_file)):
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mode = 0o644
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            info = tarfile.TarInfo(names[2])
            info.size = len(manifest_bytes)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(manifest_bytes))
        with tarfile.open(temporary_path, 'r:') as archive:
            members = archive.getmembers()
            require([m.name for m in members] == names
                    and all(m.isfile() for m in members),
                    'Unexpected archive members')
            for member, expected_hash in zip(
                    members,
                    hashes + [hashlib.sha256(manifest_bytes).hexdigest()]):
                with archive.extractfile(member) as stream:
                    require(
                        hashlib.file_digest(stream, 'sha256').hexdigest() ==
                        expected_hash, 'Archive member hash mismatch'
                    )
        require(
            hashes == [sha256(checkpoint),
                       sha256(probe_file)], 'Inputs changed while packaging'
        )
        receipt = {
            'tar_sha256': sha256(temporary_path),
            'dcp_sha256': hashes[0],
            'probe_sha256': hashes[1],
            'members': names
        }
        os.link(
            temporary_path, output_tar
        )  # Atomic publication, without replacing another output.
        return receipt
    finally:
        temporary_path.unlink(missing_ok=True)
