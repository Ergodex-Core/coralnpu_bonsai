"""Protect the compiler-output boundary used by FPGA and simulation builds."""
from pathlib import Path
import tempfile
import unittest
import zipfile

from build import prepare_rtl, validate_parameters


class PreparationTest(unittest.TestCase):

    def test_hardware_configuration_is_bound_to_the_generated_parameters(self):
        values = dict(
            xlen="32",
            rvvVlen="128",
            enableRvv="true",
            enableFloat="true",
            enableVerification="false",
            itcmSizeKBytes="8",
            dtcmSizeKBytes="32",
            fetchDataBits="128",
            lsuDataBits="128"
        )
        text = "\n".join(
            f"#define KP_{key} {value}" for key, value in values.items()
        )
        self.assertEqual(validate_parameters(text), values)
        for changed in (text.replace("KP_xlen 32", "KP_xlen 64"),
                        text.replace("KP_dtcmSizeKBytes 32",
                                     "KP_dtcmSizeKBytes 64"),
                        text + "\n#define KP_xlen 32"):
            with self.assertRaises(RuntimeError):
                validate_parameters(changed)

    def test_emitted_rtl_and_sram_are_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            top = root / "emitted.sv"
            top.write_bytes(b"module RvvCoreMiniAxi; endmodule\n")
            archive = root / "includes.zip"
            sram = b"`ifdef SYNTHESIS\n// byte-masked generic SRAM\n`endif\n"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("rvv_backend.svh", b"// definitions\n")
                z.writestr("Sram.v", sram)
            prepare_rtl(top, archive, root / "rtl")
            self.assertEqual((root / "rtl/RvvCoreMiniAxi.sv").read_bytes(),
                             top.read_bytes())
            self.assertEqual((root / "rtl/include/Sram.v").read_bytes(), sram)

    def test_untrusted_archive_paths_and_basename_collisions_fail(self):
        for members in (("../escape.sv", ), ("/absolute.sv", ), ("a/Sram.v",
                                                                 "b/Sram.v")):
            with self.subTest(members=members
                              ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                top = root / "top.sv"
                top.write_text("module top; endmodule\n")
                archive = root / "includes.zip"
                with zipfile.ZipFile(archive, "w") as z:
                    for member in members:
                        z.writestr(member, "// source\n")
                with self.assertRaises(RuntimeError):
                    prepare_rtl(top, archive, root / "rtl")
                self.assertFalse((root / "escape.sv").exists())

    def test_missing_sram_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            top = root / "top.sv"
            top.write_text("module top; endmodule\n")
            archive = root / "includes.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("rvv_backend.svh", "// header\n")
            with self.assertRaisesRegex(RuntimeError,
                                        "Missing RTL include/SRAM"):
                prepare_rtl(top, archive, root / "rtl")


if __name__ == "__main__":
    unittest.main()
