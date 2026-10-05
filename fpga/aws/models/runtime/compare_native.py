#!/usr/bin/env python3
"""Compare complete native-C traces with independent NumPy traces.

The default gate is strictly less than 3e-5 normalized error, as in the tiny
fixtures. The separate 1e-4 logit diagnostics never change this gate. Legacy
reference reports without a return code remain readable; that omission is
explicitly recorded and an explicitly failing return code is always rejected.
Both exported logit files must be verified for PASS. Legacy reports without
exports retain trace diagnostics but cannot pass. A zero tolerance requires
exact numerical equality; positive tolerances use a strict upper bound.
"""
import argparse
import array
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

STAGES = {
    'attn_norm': 1,
    'q': 2,
    'k': 3,
    'v': 4,
    'attention': 5,
    'ffn_norm': 6,
    'gate': 7,
    'up': 8,
    'layer': 9,
    'logits': 10,
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f'invalid {label}')
    return value


def ids(value, label, nonempty=False):
    require(
        isinstance(value, list) and (value or not nonempty), f'invalid {label}'
    )
    require(
        all(type(x) is int and 0 <= x <= 0xffffffff for x in value),
        f'invalid {label}'
    )
    return value


def read(path, record):
    raw = path.read_bytes()
    if (len(raw) != record['count'] * 4
            or hashlib.sha256(raw).hexdigest() != record['sha256']):
        raise ValueError(f'trace integrity failure: {path.name}')
    values = array.array('f')
    values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    require(all(math.isfinite(x) for x in values), 'nonfinite trace')
    return values


def expected_inventory(config, prompt, generated):
    required = (
        'dim', 'hidden_dim', 'n_layers', 'n_heads', 'n_kv_heads', 'head_dim',
        'vocab', 'max_seq'
    )
    for field in required:
        integer(config[field], f'config {field}', 1)
    positions = len(prompt) + max(0, len(generated) - 1)
    require(
        positions <= config['max_seq'], 'trace positions exceed model context'
    )
    require(
        all(x < config['vocab'] for x in prompt + generated),
        'token ID exceeds model vocabulary'
    )
    counts = dict(
        attn_norm=config['dim'],
        q=config['n_heads'] * config['head_dim'],
        k=config['n_kv_heads'] * config['head_dim'],
        v=config['n_kv_heads'] * config['head_dim'],
        attention=config['n_heads'] * config['head_dim'],
        ffn_norm=config['dim'],
        gate=config['hidden_dim'],
        up=config['hidden_dim'],
        layer=config['dim']
    )
    expected = {(position, layer, stage): count
                for position in range(positions)
                for layer in range(config['n_layers'])
                for stage, count in counts.items()}
    expected.update({(position, config['n_layers'], 'logits'): config['vocab']
                     for position in range(positions)})
    return expected, positions


def inventory(records, expected, reference=False):
    require(isinstance(records, list) and records, 'trace inventory is empty')
    indexed = {}
    for record in records:
        position = integer(record['position'], 'trace position')
        layer = integer(record['layer'], 'trace layer')
        stage = record['name'] if reference else record['stage']
        require(stage in STAGES, 'unknown trace operator')
        if reference:
            require(
                record['stage'] == STAGES[stage], 'trace stage/name mismatch'
            )
        key = (position, layer, stage)
        require(key not in indexed, f'duplicate trace operator: {key}')
        require(key in expected, f'unexpected trace operator: {key}')
        require(
            integer(record['count'], 'trace count', 1) == expected[key],
            f'trace tensor shape differs: {key}'
        )
        require(
            isinstance(record.get('file'), str) and record['file'],
            'trace data file is missing; generate native traces with --trace'
        )
        require(isinstance(record.get('sha256'), str), 'trace hash is missing')
        indexed[key] = record
    require(
        set(indexed) == set(expected),
        'trace operator/key inventory is incomplete'
    )
    return indexed


def exported_logits(report, report_path, traces):
    fields = ('logits_file', 'logits_sha256')
    if not any(field in report for field in fields):
        return dict(
            status='NOT_RUN',
            reason='legacy report lacks exported logits metadata'
        )
    require(
        all(
            isinstance(report.get(field), str) and report[field]
            for field in fields
        ), 'incomplete exported logits metadata'
    )
    count = max(1, len(report['generated_ids']))
    if 'logits_rows' in report:
        require(
            integer(report['logits_rows'], 'exported logits rows', 1) == count,
            'exported logits row count disagrees with request'
        )
    row_bytes = report['config']['vocab'] * 4
    path = Path(report['logits_file'])
    if not path.is_absolute():
        path = report_path.parent / path
    first_position = len(report['input_ids']) - 1
    digest = hashlib.sha256()
    try:
        require(
            path.stat().st_size == count * row_bytes,
            'exported logits byte count disagrees with request'
        )
        with path.open('rb') as source:
            for row in range(count):
                raw = source.read(row_bytes)
                require(len(raw) == row_bytes, 'truncated exported logits row')
                digest.update(raw)
                key = (
                    first_position + row, report['config']['n_layers'],
                    'logits'
                )
                require(
                    hashlib.sha256(raw).hexdigest() == traces[key]['sha256'],
                    f'exported logits row {row} differs from traced logits'
                )
            require(not source.read(1), 'extra exported logits bytes')
    except OSError as error:
        raise ValueError('cannot read exported logits file') from error
    require(
        digest.hexdigest() == report['logits_sha256'],
        'exported logits hash mismatch'
    )
    return dict(
        status='PASS',
        rows=count,
        bytes=count * row_bytes,
        sha256=digest.hexdigest(),
        first_position=first_position,
        rows_match_trace=True
    )


def within_budget(error, tolerance):
    return error == 0 if tolerance == 0 else error < tolerance


def point(index, native, reference):
    actual, expected = native[index], reference[index]
    return dict(
        index=index,
        native=actual,
        reference=expected,
        native_bits=struct.pack('<f', actual).hex(),
        reference_bits=struct.pack('<f', expected).hex(),
        difference=actual - expected,
        absolute_error=abs(actual - expected),
        normalized_error=abs(actual - expected) / (1 + abs(expected))
    )


def compare(native_path, reference_path, tolerance):
    require(
        isinstance(tolerance, (int, float)) and math.isfinite(tolerance)
        and tolerance >= 0, 'invalid tolerance'
    )
    native_path, reference_path = Path(native_path), Path(reference_path)
    n = json.loads(native_path.read_text())
    r = json.loads(reference_path.read_text())
    require(
        type(n.get('return_code')) is int and n['return_code'] == 0,
        'native runtime did not report successful execution'
    )
    if 'status' in n:
        require(n['status'] == 'PASS', 'native report status is not PASS')
    if 'sources_unchanged' in n:
        require(
            n['sources_unchanged'] is True,
            'native sources changed during execution'
        )
    if 'trace_errors' in n:
        require(
            isinstance(n['trace_errors'], list) and not n['trace_errors'],
            'native trace capture reported errors'
        )
    require(
        'return_code' not in r
        or (type(r['return_code']) is int and r['return_code'] == 0),
        'reference runtime reported failure'
    )
    for field in ('model_id', 'package_sha256', 'input_ids', 'eos_ids',
                  'config'):
        require(n[field] == r[field], f'mismatched {field}')
    for field in ('max_new_tokens', 'numeric_profile', 'arithmetic_profile',
                  'tokenizer_sha256', 'prompt_sha256'):
        if field in n or field in r:
            require(
                field in n and field in r and n[field] == r[field],
                f'mismatched {field}'
            )
    prompt = ids(n['input_ids'], 'prompt', nonempty=True)
    eos = ids(n['eos_ids'], 'EOS IDs')
    native_ids = ids(n['generated_ids'], 'native generated IDs')
    reference_ids = ids(r['generated_ids'], 'reference generated IDs')
    expected, positions = expected_inventory(n['config'], prompt, native_ids)
    ref_expected, _ = expected_inventory(r['config'], prompt, reference_ids)
    require(
        expected == ref_expected,
        'native/reference executed position counts differ'
    )
    require(
        integer(n['completed_tokens'], 'completed tokens') == positions,
        'native completed-token count disagrees with trace request'
    )
    for report, generated in ((n, native_ids), (r, reference_ids)):
        reason = report['stop_reason']
        require(reason in ('prefill', 'length', 'eos'), 'invalid stop reason')
        require((reason == 'prefill') == (not generated),
                'stop reason disagrees with generated-token count')
        if generated:
            require(
                not any(token in eos for token in generated[:-1]),
                'generation continued after EOS'
            )
            require((generated[-1] in eos) == (reason == 'eos'),
                    'stop reason disagrees with EOS token')
        if 'max_new_tokens' in report:
            requested = integer(report['max_new_tokens'], 'max_new_tokens')
            require(
                len(generated) <= requested,
                'generated-token count exceeds request'
            )
            require(
                reason == 'eos' or len(generated) == requested,
                'generation stopped before requested length'
            )
    nt = inventory(n['trace'], expected)
    rt = inventory(r['trace_records'], ref_expected, reference=True)
    native_export = exported_logits(n, native_path, nt)
    reference_export = exported_logits(r, reference_path, rt)
    require(
        isinstance(r.get('trace_directory'), str) and r['trace_directory'],
        'reference trace directory is missing'
    )
    reference_dir = Path(r['trace_directory'])
    if not reference_dir.is_absolute():
        reference_dir = reference_path.parent / reference_dir
    trace, worst, layer_growth, logit_checks = [], {}, {}, []
    exact, bitwise_exact, total = 0, 0, 0
    first_divergence, first_failure = None, None
    for key in sorted(expected, key=lambda x: (x[0], x[1], STAGES[x[2]])):
        nr, rr = nt[key], rt[key]
        nv = read(native_path.parent / nr['file'], nr)
        rv = read(reference_dir / rr['file'], rr)
        errors = [abs(a - b) for a, b in zip(nv, rv)]
        normalized = [error / (1 + abs(b)) for error, b in zip(errors, rv)]
        worst_index = max(range(len(nv)), key=normalized.__getitem__)
        error, norm_error = max(errors), normalized[worst_index]
        record = dict(
            position=key[0],
            layer=key[1],
            stage=key[2],
            count=len(nv),
            max_absolute_error=error,
            max_normalized_error=norm_error,
            native_sha256=nr['sha256'],
            reference_sha256=rr['sha256'],
            status='PASS' if within_budget(norm_error, tolerance) else 'FAIL',
            worst_value=point(worst_index, nv, rv)
        )
        differing = [
            i for i, (a, b) in enumerate(zip(nv, rv))
            if struct.pack('<f', a) != struct.pack('<f', b)
        ]
        failing = [
            i for i, value in enumerate(normalized)
            if not within_budget(value, tolerance)
        ]
        record['bitwise_unequal_values'] = len(differing)
        record['values_outside_budget'] = len(failing)
        if differing:
            record['first_bitwise_difference'] = point(differing[0], nv, rv)
            if first_divergence is None:
                first_divergence = record
        if failing:
            record['first_tolerance_failure'] = point(failing[0], nv, rv)
            if first_failure is None:
                first_failure = record
        record['max_relative_error_reference_abs_ge_1e_6'] = max(
            (error / abs(b) for error, b in zip(errors, rv) if abs(b) >= 1e-6),
            default=0
        )
        record['max_absolute_error_reference_abs_lt_1e_6'] = max(
            (error for error, b in zip(errors, rv) if abs(b) < 1e-6),
            default=0
        )
        layer_key = f'{key[0]}:{key[1]}'
        layer_growth[layer_key] = max(
            layer_growth.get(layer_key, 0), norm_error
        )
        if key[2] == 'logits':
            top = sorted(range(len(rv)), key=rv.__getitem__, reverse=True)[:2]
            native_top = max(range(len(nv)), key=nv.__getitem__)
            margin = rv[top[0]] - rv[top[1]] if len(top) == 2 else None
            emitted_index = key[0] - len(prompt) + 1
            native_token_match = (
                emitted_index < 0 or not native_ids
                or native_ids[emitted_index] == native_top
            )
            reference_token_match = (
                emitted_index < 0 or not reference_ids
                or reference_ids[emitted_index] == top[0]
            )
            logit_checks.append(
                dict(
                    position=key[0],
                    max_absolute_error=error,
                    max_normalized_error=norm_error,
                    native_top_token=native_top,
                    reference_top_token=top[0],
                    reference_runner_up=top[1] if len(top) == 2 else None,
                    reference_top_token_margin=margin,
                    error_below_half_margin=2 *
                    error < margin if margin is not None else None,
                    native_generated_id_matches_trace=native_token_match,
                    reference_generated_id_matches_trace=reference_token_match,
                    selected_prediction=emitted_index >= 0,
                    selected_prediction_argmax_matches=(
                        emitted_index < 0 or native_top == top[0]
                    ),
                    all_values_within_atol_1e_4_rtol_1e_4=all(
                        error <= 1e-4 + 1e-4 * abs(b)
                        for error, b in zip(errors, rv)
                    )
                )
            )
        trace.append(record)
        total += len(nv)
        exact += sum(a == b for a, b in zip(nv, rv))
        bitwise_exact += len(nv) - len(differing)
        old = worst.get(key[2])
        if old is None or norm_error > old['max_normalized_error']:
            worst[key[2]] = record
    generated_match = native_ids == reference_ids
    stop_match = n['stop_reason'] == r['stop_reason']
    tokens_match_traces = all(
        x['native_generated_id_matches_trace']
        and x['reference_generated_id_matches_trace'] for x in logit_checks
    )
    prediction_argmax_matches = all(
        x['selected_prediction_argmax_matches'] for x in logit_checks
    )
    exports_pass = native_export['status'] == reference_export['status'
                                                               ] == 'PASS'
    all_pass = (
        generated_match and stop_match and tokens_match_traces
        and prediction_argmax_matches and exports_pass
        and all(t['status'] == 'PASS' for t in trace)
    )
    return dict(
        schema=1,
        status='PASS' if all_pass else 'FAIL',
        evidence_kind='host_native_vs_independent_cpu',
        model_id=n['model_id'],
        package_sha256=n['package_sha256'],
        input_ids=prompt,
        eos_ids=eos,
        generated_ids=native_ids,
        reference_generated_ids=reference_ids,
        generated_ids_match=generated_match,
        generated_ids_match_traces=tokens_match_traces,
        selected_prediction_argmax_matches=prediction_argmax_matches,
        exported_logits={
            'native': native_export,
            'reference': reference_export
        },
        exported_logits_verified=exports_pass,
        stop_reason_match=stop_match,
        normalized_error_tolerance=tolerance,
        normalized_error_comparison='exact_numerical_equality'
        if tolerance == 0 else 'strictly_less_than',
        native_return_code=n['return_code'],
        reference_return_code=r.get('return_code'),
        reference_return_code_provenance='explicit'
        if 'return_code' in r else 'legacy_unspecified',
        requested_new_tokens=n.get('max_new_tokens'),
        requested_new_tokens_provenance='explicit'
        if 'max_new_tokens' in n else 'legacy_unspecified',
        compared_values=total,
        exact_values=exact,
        bitwise_exact_values=bitwise_exact,
        trace_count=len(trace),
        expected_trace_count=len(expected),
        worst_by_stage=worst,
        trace=trace,
        first_bitwise_divergence=first_divergence,
        first_tolerance_failure=first_failure,
        normalized_error_definition='abs(native-reference)/(1+abs(reference))',
        relative_error_near_zero_policy=
        'relative error reported only for abs(reference)>=1e-6; otherwise absolute error',
        max_normalized_error_by_position_layer=layer_growth,
        logit_checks=logit_checks,
        separate_logit_gate_atol_1e_4_rtol_1e_4='PASS' if all(
            x['all_values_within_atol_1e_4_rtol_1e_4'] for x in logit_checks
        ) else 'FAIL',
        separate_logit_diagnostic_affects_status=False,
        native_elapsed_seconds=n['elapsed_seconds'],
        reference_metrics=r['metrics'],
        source_sha256=n['source_sha256'],
        physical='NOT_RUN',
        coral_simulation='NOT_RUN'
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('native', type=Path)
    p.add_argument('reference', type=Path)
    p.add_argument('--normalized-tolerance', type=float, default=3e-5)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = compare(a.native, a.reference, a.normalized_tolerance)
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    print(
        json.dumps({
            k: v
            for k, v in result.items()
            if k not in ('trace', 'source_sha256', 'worst_by_stage')
        },
                   indent=2)
    )
    print(
        json.dumps({
            k: v['max_normalized_error']
            for k, v in result['worst_by_stage'].items()
        },
                   indent=2)
    )
    if result['status'] != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
