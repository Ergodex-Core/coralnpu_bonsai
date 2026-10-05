"""Parser/package tests; optional retained evidence is read only."""
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import gates

ROUTE = '''Design Route Status
# of routable nets........ : 42 :
# of fully routed nets.... : 42 :
# of nets with routing errors.... : 0 :
'''
TIMING = '''| Design Timing Summary
| ---------------------
WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints WPWS(ns) TPWS(ns) TPWS Failing Endpoints TPWS Total Endpoints
0.334 0.000 0 100 0.011 0.000 0 100 0.000 0.000 0 90
| Clock Summary
| Unconstrained Path Table
| ------------------------
Path Group   From Clock   To Clock
----------   ----------  --------
| Timing Details
'''
DRC = '''Report DRC
Table of Contents
1. REPORT SUMMARY
2. REPORT DETAILS

1. REPORT SUMMARY
-----------------
Checks found: 0
2. REPORT DETAILS
-----------------
'''
NAMES = (
    'no_clock constant_clock pulse_width_clock unconstrained_internal_endpoints no_input_delay '
    'no_output_delay multiple_clock generated_clocks loops partial_input_delay partial_output_delay latch_loops'
).split()
CHECK = '\n'.join(
    f'{i}. checking {name} (0)' for i, name in enumerate(NAMES, 1)
) + '\n'
BUS = '\n'.join(
    f'{i} {i+70} [get_cells x]\nSlack (MET) : 1.25ns' for i in range(1, 6)
)

CDC = "| Command : report_cdc -details -file cdc.rpt\n\nCDC Report\n\nID  Severity  Count  Description\n--  --------  -----  -----------\n"
EXCEPTIONS = """| Command : report_exceptions -coverage -file exceptions.rpt

Exceptions Report

Position  Type                     Setup          Hold   From      Through   To         Endpoints  From (%)  Through (%)  To (%)
--------  -----------------------  -------------  -----  --------  --------  ---------  ---------  --------  -----------  ------
"""
EXCEPTION_ROW = '3         False Path               false          false            1 pins               1                    100.00        \n'


class GatesTest(unittest.TestCase):

    def test_stage_markers(self):
        gates.validate_stage(
            '# ERROR: echo\nCORAL_STAGE_SYNTHESIS_PASSED\n', 'synthesis'
        )
        for log in (
                '# CORAL_STAGE_SYNTHESIS_PASSED',
                'ERROR: failure\nCORAL_STAGE_SYNTHESIS_PASSED',
                'CORAL_STAGE_SYNTHESIS_PASSED\nCORAL_STAGE_SYNTHESIS_PASSED'):
            with self.subTest(log=log), self.assertRaises(ValueError):
                gates.validate_stage(log, 'synthesis')

    def test_report_parsers_and_rejections(self):
        self.assertEqual(gates._route(ROUTE)['routed'], 42)
        self.assertEqual(gates._timing(TIMING)['wns'], .334)
        self.assertEqual(gates._drc(DRC), {'violations': 0, 'rules': []})
        self.assertEqual(len(gates._check_timing(CHECK + CHECK)), 12)
        self.assertEqual(gates._bus_skew(BUS), [1.25] * 5)
        bad = [
            (gates._route, ROUTE.replace(': 42 :', ': 0 :')),
            (gates._timing, TIMING.replace('0.334', '-0.001')),
            (gates._timing, TIMING.replace('0.334', 'NaN')),
            (
                gates._timing,
                TIMING.replace(
                    '| Timing Details', '(none) clk_main_a0\n| Timing Details'
                )
            ),
            (
                gates._drc,
                DRC.replace(
                    'Checks found: 0',
                    'Checks found: 1\n| REQP-1853 | Warning | Clock cascade | 1 |'
                )
            ), (gates._drc, DRC.replace('Checks found: 0', 'Checks found: 1')),
            (
                gates._drc,
                DRC.replace(
                    'Checks found: 0',
                    'Checks found: 0\n| BAD-1 | Unknown | Unknown issue | 1 |'
                )
            ), (gates._drc, DRC + 'Unexpected details'),
            (gates._check_timing, CHECK),
            (
                gates._check_timing,
                (CHECK + CHECK).replace('no_clock (0)', 'no_clock (1)')
            ), (gates._bus_skew, BUS.replace('MET', 'VIOLATED')),
            (gates._bus_skew, '')
        ]
        for parser, text in bad:
            with self.subTest(parser=parser.__name__,
                              text=text[:50]), self.assertRaises(ValueError):
                parser(text)

    def test_presence_never_qualifies_unknown_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cp = root / 'test.dcp'
            cp.write_bytes(b'checkpoint')
            header = 'Vivado v.2025.2 Build 6299465 xcvu47p-fsvh2892-2-e\n'
            reports = {
                'route_status': ROUTE,
                'timing_summary': TIMING,
                'drc': DRC,
                'check_timing': CHECK + CHECK,
                'bus_skew': BUS,
                'clocks': header,
                'methodology': 'present',
                'cdc': 'present',
                'exceptions': 'present',
                'drc_check_properties': 'present'
            }
            for name, text in reports.items():
                (root / f'{name}.rpt').write_text(header + text)
            (root / 'facts.tsv').write_text(
                'fully_routed\t1\nroute_errors\t0\ndrc_errors\t0\nchanged_drc_severities\t0\nexisting_waivers\t0\nclock.clk_main_a0.period_ns\t4\nclock.npu_clk.period_ns\t20\n'
            )
            logs = {}
            for stage in ('synthesis', 'implementation', 'validation_reports'):
                path = root / f'{stage}.log'
                logs[stage] = path
                parts = [stage] + ([
                    'link', 'optimization', 'placement',
                    'physical_optimization', 'routing'
                ] if stage == 'implementation' else [])
                path.write_text(
                    '\n'.join(
                        f'CORAL_STAGE_{part.upper()}_PASSED' for part in parts
                    )
                )
            pins = dict(
                shell_clock_hz=250000000,
                core_clock_hz=50000000,
                vivado_version='2025.2',
                vivado_build='6299465',
                device='xcvu47p-fsvh2892-2-e'
            )
            result = gates.qualify(root, logs, cp, pins)
            self.assertFalse(result['qualified'])
            self.assertEqual([
                item.split(':')[0] for item in result['blockers']
            ], ['methodology', 'cdc', 'exceptions'])
            for name, report in [('methodology',
                                  DRC.replace('Report DRC',
                                              'Report Methodology')),
                                 ('cdc', CDC), ('exceptions', EXCEPTIONS)]:
                (root / f'{name}.rpt').write_text(header + report)
            self.assertTrue(gates.qualify(root, logs, cp, pins)['qualified'])
            synthesis = logs['synthesis']
            original = synthesis.read_text()
            synthesis.write_text(
                original + '\nAWS FPGA: (15:46:07): '
                'CRITICAL WARNING: MiG BRAM is not populated\n'
            )
            self.assertTrue(
                any(
                    'Unreviewed critical warnings' in blocker for blocker in
                    gates.qualify(root, logs, cp, pins)['blockers']
                )
            )
            synthesis.write_text(original)
            ddr_pins = dict(pins, ddr_enabled=True)
            self.assertFalse(
                gates.qualify(root, logs, cp, ddr_pins)['qualified']
            )
            synthesis.write_text(
                original + '\nCORAL_STAGE_DDR_CALIBRATION_PASSED\n'
            )
            facts_path = root / 'facts.tsv'
            original_facts = facts_path.read_text()
            calibration = (
                "ddr.calibration_bram_cells\t1\n"
                "ddr.calibration_init_2c\t256'h" + '0' * 63 + '1\n'
            )
            facts_path.write_text(original_facts + calibration)
            self.assertTrue(
                gates.qualify(root, logs, cp, ddr_pins)['qualified']
            )
            facts_path.write_text(
                original_facts + calibration.replace('0' * 63 + '1', '0' * 64)
            )
            self.assertTrue(
                any(
                    'calibration INIT_2C' in blocker for blocker in
                    gates.qualify(root, logs, cp, ddr_pins)['blockers']
                )
            )
            facts_path.write_text(original_facts)
            synthesis.write_text(original)
            (root / 'facts.tsv').write_text(
                (root / 'facts.tsv').read_text().replace(
                    'changed_drc_severities\t0', 'changed_drc_severities\t1'
                )
            )
            self.assertTrue(
                any(
                    'changed_drc_severities' in item
                    for item in gates.qualify(root, logs, cp, pins)['blockers']
                )
            )

    def test_supplemental_formats_and_metrics(self):
        self.assertEqual(
            gates._methodology(
                DRC.replace('Report DRC', 'Report Methodology')
            )['violations'], 0
        )
        self.assertEqual(gates._cdc(CDC)['endpoints'], 0)
        self.assertEqual(gates._exceptions(EXCEPTIONS)['constraints'], [])
        detailed = 'Source Clock: a\nDestination Clock: b\n1 CDC-3 Info synchronized\n'
        self.assertEqual(
            gates._cdc(CDC + 'CDC-3 Info 1 synchronized\n' +
                       detailed)['endpoints'], 1
        )
        for text in [CDC.replace(' -details', ' -cells cell -details'),
                     CDC + 'CDC-1 Critical 1 unsafe\n', CDC + 'unknown']:
            with self.subTest(cdc=text), self.assertRaises(ValueError):
                gates._cdc(text)
        with self.assertRaises(gates.EvidenceError) as failed:
            gates._exceptions(EXCEPTIONS + EXCEPTION_ROW)
        self.assertIn('Unreviewed exception', str(failed.exception))
        self.assertEqual(len(failed.exception.metrics['constraints']), 1)
        with self.assertRaises(gates.EvidenceError) as failed:
            gates._timing(TIMING.replace('0.334', '-0.001'))
        self.assertEqual(failed.exception.metrics['wns'], -.001)

    def test_native_package_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cp = root / 'test.dcp'
            probe = root / 'probe.ltx'
            out = root / 'image.tar'
            cp.write_bytes(b'checkpoint' * 4096)
            probe.write_bytes(b'probe')
            manifest = dict(
                pci_device_id='0xF010',
                pci_vendor_id='0x1D0F',
                pci_subsystem_id='0x1D51',
                pci_subsystem_vendor_id='0xFEDC',
                manifest_format_version=2,
                dcp_hash=gates.sha256(cp),
                shell_version='0x10212415',
                hdk_version='2.3.0',
                tool_version='v2025.2',
                date='test',
                clock_recipe_a='A1',
                clock_recipe_b='B2',
                clock_recipe_c='C0',
                clock_recipe_hbm='H2',
                dcp_file_name='test.SH_CL_routed.dcp'
            )
            receipt = gates.package(out, cp, probe, manifest)
            self.assertEqual(receipt['tar_sha256'], gates.sha256(out))
            with tarfile.open(out) as archive:
                self.assertEqual(archive.getnames(), receipt['members'])
                self.assertEqual(
                    archive.extractfile('to_aws/test.SH_CL_routed.dcp').read(),
                    cp.read_bytes()
                )
                self.assertIn(
                    b'dcp_hash=' + gates.sha256(cp).encode() + b'\n\n',
                    archive.extractfile('to_aws/test.manifest.txt').read()
                )
            self.assertEqual(
                gates.package(root / 'repeat.tar', cp, probe,
                              manifest)['tar_sha256'], receipt['tar_sha256']
            )
            with self.assertRaises(ValueError):
                gates.package(out, cp, probe, manifest)
            violated = root / 'test.VIOLATED.dcp'
            violated.write_bytes(cp.read_bytes())
            with self.assertRaises(ValueError):
                gates.package(root / 'bad.tar', violated, probe, manifest)
            link = root / 'link.dcp'
            link.symlink_to(cp)
            with self.assertRaises(ValueError):
                gates.package(root / 'bad.tar', link, probe, manifest)
            for update in ({'dcp_hash':
                            '0' * 64}, {'date':
                                        '../escape'}, {'shell_version':
                                                       'bad\ninjection'}):
                with self.subTest(update=update
                                  ), self.assertRaises(ValueError):
                    gates.package(
                        root / 'bad.tar', cp, probe, dict(manifest, **update)
                    )
                self.assertFalse((root / 'bad.tar').exists())

    @unittest.skipUnless(
        os.environ.get('CORAL_RETAINED_REPORTS'),
        'Set CORAL_RETAINED_REPORTS for saved evidence'
    )
    def test_retained_reports_read_only(self):
        root = Path(os.environ['CORAL_RETAINED_REPORTS'])
        self.assertEqual(
            gates._route((root / 'route_status.rpt').read_text())['routed'],
            436380
        )
        self.assertEqual(
            min(gates._bus_skew((root / 'bus_skew.rpt').read_text())), 5.266
        )
        for name, parser in [('timing_summary', gates._timing),
                             ('drc', gates._drc),
                             ('check_timing', gates._check_timing)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                parser((root / f'{name}.rpt').read_text())

    @unittest.skipUnless(
        os.environ.get('CORAL_SUPPLEMENTAL_REPORTS'),
        'Set CORAL_SUPPLEMENTAL_REPORTS for saved CDC/coverage evidence'
    )
    def test_retained_supplemental_reports(self):
        root = Path(os.environ['CORAL_SUPPLEMENTAL_REPORTS'])
        with self.assertRaisesRegex(ValueError, 'Scoped/filtered'):
            gates._cdc((root / 'native_core_clock_cdc.rpt').read_text())
        with self.assertRaises(gates.EvidenceError) as failure:
            gates._exceptions((root / 'exception_coverage.rpt').read_text())
        self.assertEqual(len(failure.exception.metrics['constraints']), 146)
        self.assertEqual(
            failure.exception.metrics['invalid_positions'],
            [4, 5, 25, 26, 27, 28, 29, 33, 34, 35, 124, 125]
        )


if __name__ == '__main__':
    unittest.main()
