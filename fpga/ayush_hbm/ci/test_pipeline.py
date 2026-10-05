"""Local failure/packaging tests. No vendor, AWS or FPGA access."""
import io
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import pipeline as ci
from process import run
import reports


class PipelineTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_errors_and_stale_markers_fail(self):
        path = self.root / 'test.log'
        for text in (' ERROR: bad\nPASS\n', '[ERROR] bad\nPASS\n',
                     'FATAL: bad\nPASS\n', '%Error: bad\nPASS\n',
                     'Not a PASS\n', 'PASS\nPASS\n', ''):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                ci.check_log(path, ['PASS'])
        path.write_text('PASS: expected details\n')
        ci.check_log(path, ['PASS: expected'])

    def test_nonzero_stage_is_failure_even_with_marker(self):
        with self.assertRaises(RuntimeError):
            run([sys.executable, '-c', 'print("PASS");raise SystemExit(3)'],
                self.root / 'stage.log',
                self.root, ['PASS'],
                timeout=5)

    def test_stage_deadline_is_enforced(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, '-c', 'import time;time.sleep(10)'],
                self.root / 'timeout.log',
                self.root,
                timeout=.05)

    def test_output_refuses_existing_and_source_descendants(self):
        for output in (self.root, self.root / 'coral/out',
                       self.root / 'hdk/out'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                ci.validate_output(
                    output, self.root / 'coral', self.root / 'hdk'
                )

    def test_dry_run_has_no_external_command_or_output(self):
        with patch.object(ci, 'source_check', return_value={'source_check': 'PASS'}), \
             patch('subprocess.Popen', side_effect=AssertionError('External command')), \
             patch('sys.stdout', new_callable=io.StringIO) as output:
            ci.main([
                '--dry-run', '--output',
                str(self.root / 'must-not-exist')
            ])
        self.assertFalse((self.root / 'must-not-exist').exists())
        self.assertFalse(
            json.loads(output.getvalue())['external_commands_executed']
        )

    def test_cloud_and_raw_rtl_flags_are_not_exposed(self):
        for flag in ('--create-afi', '--bucket', '--rtl-dir'):
            with self.subTest(flag=flag), patch('sys.stderr', new_callable=io.StringIO), \
                 self.assertRaises(SystemExit):
                ci.main([flag])

    def package(self, entries):
        path = self.root / 'image.tar'
        with tarfile.open(path, 'w') as archive:
            for name, data in entries:
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        return path

    def test_package_rejects_wrong_checkpoint_and_clock_metadata(self):
        import hashlib
        fingerprint = hashlib.sha256(b'dcp').hexdigest()
        good = [('to_aws/design.dcp', b'dcp'),
                ('to_aws/manifest.txt', b'clock_recipe_hbm=H3\n')]
        ci.verify_package(self.package(good), fingerprint)
        cases = [
            good + good, [('a.dcp', b'wrong'), good[1]],
            [good[0], ('manifest.txt', b'clock_recipe_hbm=H2\n')],
            good + [('../escape', b'bad')]
        ]
        for entries in cases:
            with self.subTest(entries=entries
                              ), self.assertRaises(RuntimeError):
                ci.verify_package(self.package(entries), fingerprint)

    def test_missing_reports_cannot_qualify(self):
        result = reports.qualify(self.root)
        self.assertFalse(result['qualified'])
        self.assertEqual(result['hardware_calibration'], 'NOT_RUN')
        self.assertTrue((self.root / 'qualification.json').is_file())

    def test_generation_failure_writes_failed_receipt(self):
        out = self.root / 'new-output'
        lock = self.root / 'lock.json'
        lock.write_text('{}')
        with patch.object(ci, 'source_check'), patch.object(ci, 'validate_coral'), \
             patch.object(ci, 'verify_dependency_lock', return_value={}), \
             patch.object(ci, 'tool_versions', return_value={}), \
             patch.object(ci, 'generate', side_effect=RuntimeError('fixture generation failed')):
            with self.assertRaisesRegex(RuntimeError,
                                        'fixture generation failed'):
                ci.main([
                    '--generate-only', '--coral-repo',
                    str(self.root / 'coral'), '--hdk-root',
                    str(self.root / 'hdk'), '--dependency-lock',
                    str(lock), '--dependency-lock-sha256',
                    ci.sha(lock), '--output',
                    str(out)
                ])
        result = json.loads((out / 'result.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['hardware_calibration'], 'NOT_RUN')
        self.assertFalse(list(out.glob('*.tar')))

    def test_full_synthetic_report_set_and_missing_report(self):
        for name in reports.REPORTS:
            (self.root / name).write_text('synthetic report fixture')
        (self.root / 'timing_summary.rpt').write_text(ReportTests().timing())
        (self.root / 'facts.tsv').write_text(
            'fully_routed\t1\nroute_errors\t0\nwaivers\t0\ndrc_violations\t0\n'
            'changed_drc_rules\t0\nclock.npu_clk\t20\nclock.clk_main_a0\t4\n'
            'clock.clk_out1_cl_hbm_mmcm\t3.333333\n'
        )
        names = (
            'no_clock constant_clock pulse_width_clock unconstrained_internal_endpoints '
            'no_input_delay no_output_delay multiple_clock generated_clocks loops '
            'partial_input_delay partial_output_delay latch_loops'
        ).split()
        (self.root / 'check_timing.rpt').write_text(
            ''.join(f'{i}. checking {n} (0)\n' for i, n in enumerate(names))
        )
        (self.root / 'cdc.rpt').write_text(
            '| Command : report_cdc -details -file cdc.rpt\nCDC Report\n'
            'ID Severity Count Description\n----------\nCDC-3 Info 1 Synchronizer\n'
            'Source Clock: clk_core\n1 CDC-3 Info endpoint\n'
        )
        (self.root / 'bus_skew.rpt').write_text(
            ''.join(
                f'{i} 1 [get_pins source]\nSlack (MET) : 0.010ns\n'
                for i in range(1, 44)
            )
        )
        for name in ('drc.rpt', 'methodology.rpt'):
            (self.root / name
             ).write_text('\nChecks found: 0\n2. REPORT DETAILS\n------\n')
        (self.root / 'exceptions.rpt').write_text(
            '| Command : report_exceptions -coverage -file exceptions.rpt\n'
            'Exceptions Report\nPosition Type Setup Hold Endpoints\n'
        )
        self.assertTrue(reports.qualify(self.root)['qualified'])
        (self.root / 'clock_interaction.rpt').unlink()
        self.assertFalse(reports.qualify(self.root)['qualified'])


class ReportTests(unittest.TestCase):

    def timing(self, row='0.009 0 0 10 0.010 0 0 10 0.2 0 0 10'):
        return (
            '| Design Timing Summary\nWNS(ns) TPWS Total Endpoints\n' + row +
            '\n| Unconstrained Path Table\nPath Group\n----------\n'
        )

    def test_timing_checks_all_metrics_and_coverage(self):
        reports.timing(self.timing())
        for row in ('-0.1 0 0 10 0.01 0 0 10 0.2 0 0 10',
                    '0.1 0 0 10 -0.01 0 0 10 0.2 0 0 10',
                    '0.1 0 0 10 0.01 0 0 10 -0.2 0 0 10',
                    '0.1 0 0 0 0.01 0 0 10 0.2 0 0 10',
                    '0.1 -1 1 10 0.01 0 0 10 0.2 0 0 10'):
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                reports.timing(self.timing(row))
        with self.assertRaises(RuntimeError):
            reports.timing(self.timing() + 'unconstrained 1\n')

    def test_skew_requires_all_43_constraints(self):
        text = ''.join(
            f'{i} 1 [get_pins source]\nSlack (MET) : 0.010ns\n'
            for i in range(1, 44)
        )
        reports.skew(text)
        for bad in (text.replace('43 1', '44 1'), text.replace('0.010ns',
                                                               '-0.010ns'),
                    text.replace('Slack (MET)', 'Slack (VIOLATED)'), ''):
            with self.assertRaises(RuntimeError):
                reports.skew(bad)

    def test_cdc_counts_and_no_clock_name_allowlist(self):
        text = (
            '| Command : report_cdc -details -file cdc.rpt\nCDC Report\n'
            'ID Severity Count Description\n----------\nCDC-3 Info 1 Synchronizer\n'
            'Source Clock: clk_core\n1 CDC-3 Info endpoint\n'
        )
        reports.cdc(text)
        for bad in (text.replace('Info', 'Critical'), text.replace('Info',
                                                                   'Warning'),
                    text.replace('Info 1',
                                 'Info 2'), text.replace('-details',
                                                         '-from clk_core'),
                    text.replace('CDC-3 Info 1 Synchronizer\n',
                                 ''), text + '2 CDC-3 Info extra\n'):
            with self.assertRaises(RuntimeError):
                reports.cdc(bad)

    def test_drc_warnings_and_unknown_formats_rejected(self):
        clean = '\nChecks found: 0\n2. REPORT DETAILS\n------\n'
        reports.no_findings(clean)
        for bad in (clean.replace(': 0',
                                  ': 1477'), clean + 'hidden violation', ''):
            with self.assertRaises(RuntimeError):
                reports.no_findings(bad)


if __name__ == '__main__':
    unittest.main()
