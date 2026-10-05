"""Saved-trace qualification must reject incomplete evidence and identify drift."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from compare_native import STAGES, compare, expected_inventory


class ComparisonTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='coral-trace-compare-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native_path = self.root / 'native' / 'report.json'
        self.reference_path = self.root / 'reference' / 'report.json'
        (self.native_path.parent / 'trace').mkdir(parents=True)
        (self.reference_path.parent / 'trace').mkdir(parents=True)
        config = dict(
            dim=2,
            hidden_dim=4,
            n_layers=1,
            n_heads=2,
            n_kv_heads=1,
            head_dim=2,
            vocab=4,
            max_seq=8,
            flags=2,
            rms_eps=1e-6,
            rope_theta=10000.0
        )
        common = dict(
            model_id='synthetic-saved-traces',
            package_sha256='a' * 64,
            config=config,
            input_ids=[1, 2],
            generated_ids=[3, 3],
            eos_ids=[],
            stop_reason='length',
            return_code=0,
            max_new_tokens=2
        )
        self.native = dict(
            copy.deepcopy(common),
            completed_tokens=3,
            elapsed_seconds=1.0,
            source_sha256={},
            trace=[]
        )
        self.reference = dict(
            copy.deepcopy(common),
            metrics={},
            trace_directory='trace',
            trace_records=[]
        )
        expected, _ = expected_inventory(config, [1, 2], [3, 3])
        for (position, layer, stage), count in expected.items():
            values = list(range(count)) if stage == 'logits' else [0.0] * count
            filename = f'{position}-{layer}-{stage}.f32'
            raw = struct.pack(f'<{count}f', *values)
            digest = hashlib.sha256(raw).hexdigest()
            (self.native_path.parent / 'trace' / filename).write_bytes(raw)
            (self.reference_path.parent / 'trace' / filename).write_bytes(raw)
            record = dict(
                position=position, layer=layer, count=count, sha256=digest
            )
            self.native['trace'].append(
                dict(record, stage=stage, file='trace/' + filename)
            )
            self.reference['trace_records'].append(
                dict(record, stage=STAGES[stage], name=stage, file=filename)
            )
        self.refresh_exports()
        self.write_reports()

    def write_reports(self):
        self.native_path.write_text(json.dumps(self.native))
        self.reference_path.write_text(json.dumps(self.reference))

    def refresh_exports(self):
        for report, report_path, trace_field, native in (
            (self.native, self.native_path, 'trace', True),
            (self.reference, self.reference_path, 'trace_records', False),
        ):
            first = len(report['input_ids']) - 1
            count = max(1, len(report['generated_ids']))
            records = {
                x['position']: x
                for x in report[trace_field]
                if (x['stage'] if native else x['name']) == 'logits'
            }
            base = report_path.parent if native else report_path.parent / report[
                'trace_directory']
            raw = b''.join((base / records[position]['file']).read_bytes()
                           for position in range(first, first + count))
            (report_path.parent / 'logits.f32').write_bytes(raw)
            report.update(
                logits_file='logits.f32',
                logits_rows=count,
                logits_sha256=hashlib.sha256(raw).hexdigest()
            )

    def set_logits(self, which, position, values):
        report = getattr(self, which)
        native = which == 'native'
        records = report['trace'] if native else report['trace_records']
        record = next(
            x for x in records if x['position'] == position and
            (x['stage'] if native else x['name']) == 'logits'
        )
        base = self.native_path.parent if native else self.reference_path.parent / report[
            'trace_directory']
        raw = struct.pack(f'<{len(values)}f', *values)
        (base / record['file']).write_bytes(raw)
        record['sha256'] = hashlib.sha256(raw).hexdigest()

    def run_comparison(self, tolerance=3e-5):
        self.write_reports()
        return compare(self.native_path, self.reference_path, tolerance)

    def change_native(self, stage, index, value, position=0):
        record = next(
            x for x in self.native['trace']
            if x['stage'] == stage and x['position'] == position
        )
        path = self.native_path.parent / record['file']
        raw = bytearray(path.read_bytes())
        struct.pack_into('<f', raw, index * 4, value)
        path.write_bytes(raw)
        record['sha256'] = hashlib.sha256(raw).hexdigest()

    def test_complete_relative_trace_paths_and_execution_order(self):
        self.native['trace'].reverse()
        self.reference['trace_records'].reverse()
        result = self.run_comparison()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['trace_count'], 30)
        self.assertEqual(result['compared_values'], 90)
        self.assertEqual([x['stage'] for x in result['trace'][:10]],
                         list(STAGES))
        self.assertIsNone(result['first_bitwise_divergence'])
        self.assertIsNone(result['first_tolerance_failure'])
        self.assertTrue(result['generated_ids_match_traces'])

    def test_empty_duplicate_missing_and_unexpected_operator_evidence(self):
        original_n, original_r = copy.deepcopy(self.native), copy.deepcopy(
            self.reference
        )
        for mutation in ('empty', 'duplicate', 'missing', 'unexpected',
                         'count'):
            with self.subTest(mutation=mutation):
                self.native, self.reference = copy.deepcopy(
                    original_n
                ), copy.deepcopy(original_r)
                if mutation == 'empty':
                    self.native['trace'] = []
                    self.reference['trace_records'] = []
                elif mutation == 'duplicate':
                    self.native['trace'].append(
                        copy.deepcopy(self.native['trace'][0])
                    )
                    self.reference['trace_records'].append(
                        copy.deepcopy(self.reference['trace_records'][0])
                    )
                elif mutation == 'missing':
                    self.native['trace'].pop(0)
                    self.reference['trace_records'].pop(0)
                elif mutation == 'unexpected':
                    self.native['trace'][0]['position'] = 99
                    self.reference['trace_records'][0]['position'] = 99
                else:
                    self.native['trace'][0]['count'] += 1
                    self.reference['trace_records'][0]['count'] += 1
                with self.assertRaises(ValueError):
                    self.run_comparison()

    def test_first_divergence_and_budget_failure_are_distinct(self):
        self.change_native('q', 1, 1e-6)
        self.change_native('gate', 2, 5e-5)
        self.native['trace'].reverse()
        result = self.run_comparison()
        self.assertEqual(result['status'], 'FAIL')
        first = result['first_bitwise_divergence']
        failure = result['first_tolerance_failure']
        self.assertEqual((first['position'], first['layer'], first['stage']),
                         (0, 0, 'q'))
        self.assertEqual(first['first_bitwise_difference']['index'], 1)
        self.assertEqual(
            (failure['position'], failure['layer'], failure['stage']),
            (0, 0, 'gate')
        )
        self.assertEqual(failure['first_tolerance_failure']['index'], 2)
        self.assertEqual(failure['first_tolerance_failure']['reference'], 0.0)
        self.assertEqual(
            result['separate_logit_gate_atol_1e_4_rtol_1e_4'], 'PASS'
        )
        self.assertFalse(result['separate_logit_diagnostic_affects_status'])

    def test_signed_zero_is_bitwise_divergence_without_numerical_failure(self):
        self.change_native('attn_norm', 0, -0.0)
        result = self.run_comparison()
        self.assertEqual(result['status'], 'PASS')
        first = result['first_bitwise_divergence']['first_bitwise_difference']
        self.assertNotEqual(first['native_bits'], first['reference_bits'])
        self.assertEqual(first['absolute_error'], 0.0)
        self.assertEqual(
            result['exact_values'] - result['bitwise_exact_values'], 1
        )

    def test_strict_boundary_and_invalid_tolerance(self):
        self.change_native('attn_norm', 0, 2**-13)
        self.assertEqual(self.run_comparison(2**-13)['status'], 'FAIL')
        for tolerance in (-1, float('nan'), float('inf')):
            with self.subTest(tolerance=tolerance
                              ), self.assertRaises(ValueError):
                self.run_comparison(tolerance)

    def test_identity_return_code_and_request_mismatches(self):
        original_n, original_r = copy.deepcopy(self.native), copy.deepcopy(
            self.reference
        )
        changes = [('package_sha256', 'b' * 64), ('input_ids', [2, 1]),
                   ('eos_ids', [0]), ('model_id', 'other'),
                   ('max_new_tokens', 3), ('numeric_profile', 'different')]
        for field, value in changes:
            with self.subTest(field=field):
                self.native, self.reference = copy.deepcopy(
                    original_n
                ), copy.deepcopy(original_r)
                self.reference[field] = value
                with self.assertRaises(ValueError):
                    self.run_comparison()
        for which in ('native', 'reference'):
            with self.subTest(return_code=which):
                self.native, self.reference = copy.deepcopy(
                    original_n
                ), copy.deepcopy(original_r)
                getattr(self, which)['return_code'] = 7
                with self.assertRaises(ValueError):
                    self.run_comparison()
        self.native, self.reference = copy.deepcopy(original_n), copy.deepcopy(
            original_r
        )
        self.reference['config']['rope_theta'] = 1000000.0
        with self.assertRaises(ValueError):
            self.run_comparison()

    def test_native_capture_and_source_failures_override_successful_return_code(
        self
    ):
        for field, value in (('status', 'FAIL'), ('sources_unchanged', False),
                             ('trace_errors', ['disk write failed'])):
            with self.subTest(field=field):
                self.native[field] = value
                with self.assertRaises(ValueError):
                    self.run_comparison()
                self.native.pop(field)

    def test_legacy_metadata_is_explicitly_identified(self):
        self.reference.pop('return_code')
        self.reference.pop('max_new_tokens')
        self.native.pop('max_new_tokens')
        result = self.run_comparison()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(
            result['reference_return_code_provenance'], 'legacy_unspecified'
        )
        self.assertIsNone(result['reference_return_code'])
        self.assertEqual(
            result['requested_new_tokens_provenance'], 'legacy_unspecified'
        )

    def test_stop_count_and_declared_tokens_must_match_trace(self):
        self.native['generated_ids'] = [2, 2]
        self.reference['generated_ids'] = [2, 2]
        result = self.run_comparison()
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['generated_ids_match_traces'])
        self.native['completed_tokens'] = 2
        with self.assertRaises(ValueError):
            self.run_comparison()

    def test_exported_logits_missing_corrupt_or_wrong_shape_fail(self):
        for which in ('native', 'reference'):
            report = getattr(self, which)
            report_path = getattr(self, which + '_path')
            path = report_path.parent / 'logits.f32'
            original = path.read_bytes()
            for mutation in ('missing_file', 'hash', 'length', 'rows',
                             'partial_metadata'):
                with self.subTest(which=which, mutation=mutation):
                    if mutation == 'missing_file':
                        report['logits_file'] = 'MISSING.f32'
                    elif mutation == 'hash':
                        report['logits_sha256'] = '0' * 64
                    elif mutation == 'length':
                        path.write_bytes(original[:-4])
                        report['logits_sha256'] = hashlib.sha256(
                            original[:-4]
                        ).hexdigest()
                    elif mutation == 'rows':
                        report['logits_rows'] += 1
                    else:
                        report.pop('logits_sha256')
                    with self.assertRaises(ValueError):
                        self.run_comparison()
                    self.refresh_exports()

    def test_exported_rows_must_match_corresponding_trace_bytes(self):
        self.set_logits('native', 2, [0, 1, 2, 4])
        self.refresh_exports()
        path = self.native_path.parent / 'logits.f32'
        raw = path.read_bytes()
        swapped = raw[16:] + raw[:16]
        path.write_bytes(swapped)
        self.native['logits_sha256'] = hashlib.sha256(swapped).hexdigest()
        with self.assertRaisesRegex(ValueError, 'differs from traced logits'):
            self.run_comparison()

    def test_legacy_without_export_has_diagnostics_but_cannot_pass(self):
        for field in ('logits_file', 'logits_rows', 'logits_sha256'):
            self.reference.pop(field)
        result = self.run_comparison()
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['exported_logits']['native']['status'], 'PASS')
        self.assertEqual(
            result['exported_logits']['reference']['status'], 'NOT_RUN'
        )
        self.assertFalse(result['exported_logits_verified'])
        self.assertTrue(all(x['status'] == 'PASS' for x in result['trace']))

    def test_prompt_only_near_tie_argmax_difference_fails_within_budget(self):
        for report, field in ((self.native, 'trace'), (self.reference,
                                                       'trace_records')):
            report.update(
                generated_ids=[], max_new_tokens=0, stop_reason='prefill'
            )
            report[field] = [x for x in report[field] if x['position'] < 2]
        self.native['completed_tokens'] = 2
        self.set_logits('native', 1, [0, 1e-6, 0, 0])
        self.set_logits('reference', 1, [1e-6, 0, 0, 0])
        self.refresh_exports()
        result = self.run_comparison()
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(result['exported_logits_verified'])
        self.assertTrue(all(x['status'] == 'PASS' for x in result['trace']))
        self.assertFalse(result['selected_prediction_argmax_matches'])
        self.set_logits('native', 1, [1e-6, 0, 0, 0])
        self.refresh_exports()
        self.assertEqual(self.run_comparison()['status'], 'PASS')

    def test_zero_tolerance_requires_exact_numerical_equality(self):
        self.assertEqual(self.run_comparison(0)['status'], 'PASS')
        self.change_native('attn_norm', 0, -0.0)
        result = self.run_comparison(0)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(
            result['normalized_error_comparison'], 'exact_numerical_equality'
        )
        self.change_native('attn_norm', 0, 1e-8)
        self.assertEqual(self.run_comparison(0)['status'], 'FAIL')

    def test_trace_corruption_and_nonfinite_values_fail(self):
        record = self.native['trace'][0]
        path = self.native_path.parent / record['file']
        original = path.read_bytes()
        path.write_bytes(original[:-1])
        with self.assertRaises(ValueError):
            self.run_comparison()
        path.write_bytes(original)
        self.change_native('attn_norm', 0, float('nan'))
        with self.assertRaises(ValueError):
            self.run_comparison()


if __name__ == '__main__':
    unittest.main()
