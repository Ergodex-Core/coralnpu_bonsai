"""Exercise real Tcl control flow with mocked Vivado object/property queries."""
from pathlib import Path
import shutil
import subprocess
import unittest

import gates

HERE = Path(__file__).resolve().parent


class DDRCalibrationTest(unittest.TestCase):

    def test_aws_prefixed_errors_and_critical_warnings(self):
        for prefix in ('', '  ', 'AWS FPGA: (15:46:07): '):
            with self.subTest(prefix=prefix):
                text = prefix + 'CRITICAL WARNING: MiG BRAM is not populated\n'
                self.assertEqual(
                    len(gates._messages(text, 'CRITICAL WARNING')), 1
                )
                with self.assertRaisesRegex(ValueError, 'emitted ERROR'):
                    gates.validate_stage(
                        prefix + 'ERROR: failure\n'
                        'CORAL_STAGE_SYNTHESIS_PASSED\n', 'synthesis'
                    )
        self.assertFalse(
            gates._messages(
                '# CRITICAL WARNING: source echo\n', 'CRITICAL WARNING'
            )
        )

    def test_checkpoint_facts_require_one_populated_calibration_ram(self):
        good = {
            'ddr.calibration_bram_cells': '1',
            'ddr.calibration_init_2c': "256'h" + '0' * 63 + 'A'
        }
        self.assertEqual(
            gates._ddr_calibration(good), good['ddr.calibration_init_2c']
        )
        for update in ({'ddr.calibration_bram_cells':
                        '0'}, {'ddr.calibration_bram_cells':
                               '2'}, {'ddr.calibration_init_2c':
                                      "256'h" + '0' * 64},
                       {'ddr.calibration_init_2c':
                        "256'h" + 'x' * 64}, {'ddr.calibration_init_2c': ''}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                gates._ddr_calibration(dict(good, **update))
        with self.assertRaises(ValueError):
            gates._ddr_calibration({})

    def test_actual_tcl_rejects_missing_ambiguous_zero_and_unknown_properties(
        self
    ):
        tclsh = shutil.which('tclsh')
        self.assertIsNotNone(
            tclsh, 'tclsh is required; this gate must execute'
        )
        source = (HERE / 'ddr_calibration_gate.tcl').read_text()
        cases = [([], "256'h" + '1' * 64, False),
                 (['a', 'b'], "256'h" + '1' * 64, False),
                 (['a'], "256'h" + '0' * 64, False),
                 (['a'], "256'h" + 'x' * 64, False), (['a'], '', False),
                 (['a'], "256'h" + '0' * 63 + '1', True)]
        for cells, value, accepted in cases:
            script = (
                'set mock_cells {' + ' '.join(cells) + '}\n'
                'set mock_init {' + value + '}\n'
                'proc get_cells {args} {return $::mock_cells}\n'
                'proc get_property {property cell} {\n'
                '  if {$property ne "INIT_2C"} {error "wrong property"}\n'
                '  return $::mock_init\n}\n' + source + '\n'
                'if {[catch {coral_require_ddr_calibration} result]} {\n'
                '  puts stderr $result\n  exit 7\n}\n'
                'puts $result\nexit 0\n'
            )
            result = subprocess.run([tclsh],
                                    input=script,
                                    text=True,
                                    capture_output=True,
                                    timeout=5)
            with self.subTest(cells=cells, value=value):
                self.assertEqual(
                    result.returncode, 0 if accepted else 7,
                    result.stdout + result.stderr
                )
                if accepted:
                    self.assertIn('cells 1 init_2c', result.stdout)


if __name__ == '__main__':
    unittest.main()
