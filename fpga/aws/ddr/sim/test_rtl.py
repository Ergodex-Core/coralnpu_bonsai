"""Local DDR protocol tests. No FPGA, Vivado, checkpoint, or AWS access needed."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
DESIGN = HERE.parents[1] / "cl_coralnpu" / "design"


class RTLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("iverilog") or not shutil.which("vvp"):
            raise unittest.SkipTest("iverilog and vvp are required")

    def simulate(self, top, sources, parameters=(), defines=()):
        with tempfile.TemporaryDirectory(prefix="coral-ddr-test-") as tmp:
            exe = Path(tmp) / "test.vvp"
            command = ["iverilog", "-g2012", "-s", top, "-o", str(exe)]
            command += [f"-D{value}" for value in defines]
            command += [f"-P{top}.{key}={value}" for key, value in parameters]
            command += [str(path) for path in sources]
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(["vvp", str(exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS:", result.stdout)
            return result.stdout

    def test_core_frontend_128(self):
        self.simulate("tb_ddr_frontend", [DESIGN / "coral_ddr_frontend.sv", HERE / "tb_ddr_frontend.sv"])

    def test_pcis_frontend_512(self):
        self.simulate("tb_ddr_frontend", [DESIGN / "coral_ddr_frontend.sv", HERE / "tb_ddr_frontend.sv"], [("DATA_WIDTH", 512), ("ADDR_WIDTH", 64), ("ID_WIDTH", 16), ("BASE_ADDR", 0)])

    def test_backend(self):
        self.simulate("tb_ddr_backend", [DESIGN / "coral_ddr_backend.sv", HERE / "tb_ddr_backend.sv"])

    def test_cdc(self):
        self.simulate("tb_ddr_cdc", [HERE / "xpm_fifo_async_model.sv", DESIGN / "coral_ddr_cdc.sv", HERE / "tb_ddr_cdc.sv"])


if __name__ == "__main__":
    unittest.main()
