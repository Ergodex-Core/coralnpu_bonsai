#!/usr/bin/env python3
"""Run the pinned Coral core with production DDR RTL and functional memory/FIFO.

No AWS, synthesis, physical device, vendor IP download or checkpoint download.
The default fixture is hand-encoded RV32I and needs no cross compiler. Optional
ELF/JSON fixtures exercise the same path and retain exact input/source hashes.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time

HERE = Path(__file__).resolve().parent
AWS = HERE.parents[1]
DESIGN = AWS / "cl_coralnpu/design"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def elf_regions(path):
    """Read little-endian RV32 ELF PT_LOAD with no third-party dependencies."""
    data = path.read_bytes()
    if data[:7] != b"\x7fELF\x01\x01\x01":
        raise ValueError("Firmware must be ELF32 little endian")
    fields = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    _, machine, _, entry, phoff, _, _, _, phsize, phnum, *_ = fields
    if machine != 243 or entry != 0 or phsize != 32:
        raise ValueError("Firmware must be RISC-V, entry 0, ELF32 program headers")
    regions = []
    for i in range(phnum):
        kind, offset, va, pa, size, memsize, flags, align = struct.unpack_from(
            "<8I", data, phoff + i * phsize)
        if kind != 1 or not memsize:
            continue
        if size > memsize or offset + size > len(data) or va != pa:
            raise ValueError("Invalid/relocated ELF load segment")
        regions.append((va, data[offset:offset + size] + bytes(memsize - size)))
    return regions


def smoke():
    # Boot jumps from ITCM to DDR. DDR code loads 40, adds 2, stores both DDR
    # and DTCM, then performs one byte store and halts with mpause.
    instructions = [
        0x20010137,  # lui x2,0x20010
        0x00012083,  # lw x1,0(x2)
        0x00208093,  # addi x1,x1,2
        0x00112223,  # sw x1,4(x2)
        0x000101B7,  # lui x3,0x10
        0x0011A023,  # sw x1,0(x3)
        0x001104A3,  # sb x1,9(x2)
        0x0FF0000F,  # fence
        0x08000073,  # mpause
    ]
    regions = [(0, struct.pack("<2I", 0x200002B7, 0x00028067)),
               (0x20000000, struct.pack("<%dI" % len(instructions), *instructions)),
               (0x20010000, struct.pack("<4I", 40, 0, 0xAABBCCDD, 0))]
    checks = [{"address": 0x10000, "value": 42},
              {"address": 0x20010004, "value": 42},
              {"address": 0x20010008, "value": 0xAABB2ADD}]
    return regions, checks


def write_fixture(out, regions, checks):
    """Normalize images into host transactions, preserving partial final lines."""
    memory = {}
    for address, content in regions:
        end = address + len(content)
        if not ((0 <= address < end <= 0x2000) or
                (0x10000 <= address < end <= 0x18000) or
                (0x20000000 <= address < end <= 0xA0000000)):
            raise ValueError(f"Load outside ITCM/DTCM/DDR: {address:#x}+{len(content):#x}")
        for offset, byte in enumerate(content):
            # Later regions may initialize a NOLOAD mailbox from the ELF.
            memory[address + offset] = byte
    tcm = sorted({address & ~3 for address in memory if address < 0x20000000})
    ddr = sorted({address & ~63 for address in memory if address >= 0x20000000})
    def read(address, size):
        return int.from_bytes(bytes(memory.get(address + i, 0) for i in range(size)), "little")
    (out / "tcm.hex").write_text("".join(f"{a:08x} {read(a, 4):08x}\n" for a in tcm))
    (out / "ddr.hex").write_text("".join(f"{a-0x20000000:08x} {read(a, 64):0128x}\n" for a in ddr))
    addresses = [number(check["address"]) for check in checks]
    if any(a & 3 for a in addresses):
        raise ValueError("Readback checks must be 32-bit aligned")
    (out / "reads.hex").write_text("".join(f"{a:08x}\n" for a in sorted(set(addresses))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emitted-dir", type=Path, required=True)
    parser.add_argument("--verilator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--fixture", type=Path,
                        help="JSON segments[{address,file}], checks[{address,value|float,atol,rtol}]")
    parser.add_argument("--reuse-build", type=Path,
                        help="Previous output directory; exact RTL/source hashes must match")
    parser.add_argument("--max-polls", type=int, default=2000000)
    parser.add_argument("--run-timeout", type=int, default=600)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    pins = json.loads((AWS / "build/pins.json").read_text())
    top = args.emitted_dir / "RvvCoreMiniAxi.sv"
    archive = top.with_suffix(".zip")
    header = args.emitted_dir / "VRvvCoreMiniAxi_parameters.h"
    if digest(top) != pins["reference_emitted_rtl_sha256"]:
        raise ValueError("Emitted core differs from the qualified pin")
    version = subprocess.check_output([str(args.verilator), "--version"], text=True).strip()
    if not version.startswith("Verilator " + pins["verilator_version"] + " "):
        raise ValueError("Wrong Verilator version: " + version)
    spec = importlib.util.spec_from_file_location("coral_build", AWS / "build/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parameters = module.validate_parameters(header.read_text())
    module.prepare_rtl(top, archive, out / "rtl")
    includes = {p.name: digest(p) for p in (out / "rtl/include").iterdir()}
    inventory = hashlib.sha256(json.dumps(includes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if inventory != pins["reference_include_inventory_sha256"]:
        raise ValueError("Core include inventory differs from pin")
    sources = [DESIGN / f"coral_ddr_{name}.sv" for name in ("frontend", "backend", "cdc", "subsystem")]
    sources += [DESIGN / "coral_host.sv", HERE / "sim_xpm.sv", HERE / "tb_core_ddr.sv"]
    model = HERE.parent / "sim/xpm_fifo_async_model.sv"
    identity = {str(p.relative_to(AWS)): digest(p) for p in sources + [model]}
    identity["emitted_core"] = digest(top)
    identity["include_inventory"] = inventory
    # Compile an immutable copy, so another workstream editing shared sources
    # cannot silently change the binary after this receipt's hashes were read.
    staged = out / "sources"
    staged.mkdir()
    for path in sources + [model]:
        shutil.copy2(path, staged / path.name)
        if digest(staged / path.name) != identity[str(path.relative_to(AWS))]:
            raise RuntimeError("Source changed while snapshotting: " + str(path))
    receipt = {"schema_version": 1, "status": "RUNNING", "source_sha256": identity,
               "verilator": version, "generated_parameters": parameters,
               "core_clock_hz": 50000000, "shell_clock_hz": 250000000,
               "physical_ddr": "NOTRUN", "vendor_xpm_cdc": "NOTRUN",
               "fifo": "portable_functional_gray_pointer_model",
               "full_checkpoint_inference": "NOTRUN"}
    receipt["runner_sha256"] = digest(Path(__file__))
    receipt["pins_sha256"] = digest(AWS / "build/pins.json")
    receipt["archive_sha256"] = digest(archive)
    receipt["parameters_header_sha256"] = digest(header)
    regions, checks = smoke()
    if args.firmware or args.fixture:
        regions, checks = [], []
        if args.firmware:
            regions += elf_regions(args.firmware)
            receipt["firmware_sha256"] = digest(args.firmware)
        if args.fixture:
            fixture = json.loads(args.fixture.read_text())
            receipt["fixture_sha256"] = digest(args.fixture)
            receipt["fixture_metadata"] = fixture.get("metadata", {})
            receipt["fixture_files"] = {}
            for region in fixture.get("segments", []):
                file = args.fixture.parent / region["file"]
                regions.append((number(region["address"]), file.read_bytes()))
                receipt["fixture_files"][region["file"]] = digest(file)
            checks = fixture["checks"]
        if not checks:
            raise ValueError("Inference runs require explicit expected readback checks")
    write_fixture(out, regions, checks)
    receipt["normalized_fixture_sha256"] = {name: digest(out / name) for name in ("tcm.hex", "ddr.hex", "reads.hex")}
    started = time.monotonic()
    try:
        binary = out / "obj/Vtb_core_ddr"
        if args.reuse_build:
            old = json.loads((args.reuse_build / "receipt.json").read_text())
            if old["source_sha256"] != identity or old["verilator"] != version:
                raise ValueError("Cannot reuse binary after source/tool changes")
            binary = args.reuse_build / "obj/Vtb_core_ddr"
            if digest(binary) != old["binary_sha256"]:
                raise ValueError("Reused binary digest mismatch")
        else:
            command = [str(args.verilator), "--binary", "--timing", "-j", "4", "--top-module", "tb_core_ddr",
                       "--Mdir", str(out / "obj"), "-Wno-fatal", "-Wno-BLKANDNBLK", "-Wno-WIDTH", "-Wno-UNOPTFLAT",
                       "+define+USE_GENERIC+SYNTHESIS+VLEN_128+ZVE32F_ON+TB_SUPPORT",
                       "+incdir+" + str(out / "rtl/include"), "+incdir+" + str(staged),
                       str(out / "rtl/RvvCoreMiniAxi.sv"), *[str(staged / path.name) for path in sources]]
            receipt["compile_command"] = command
            env = dict(os.environ, CCACHE_DISABLE="1", PYTHONDONTWRITEBYTECODE="1")
            module.run(command, out / "compile.log", cwd=out, env=env, timeout=1800)
        receipt["binary_sha256"] = digest(binary)
        text = module.run([str(binary), "+tcm=" + str(out / "tcm.hex"), "+ddr=" + str(out / "ddr.hex"),
                           "+reads=" + str(out / "reads.hex"), "+limit=" + str(args.max_polls)],
                          out / "simulation.log", cwd=out, timeout=args.run_timeout)
        values = {int(a, 16): int(v, 16) for a, v in re.findall(r"READBACK ([0-9a-fA-F]{8}) ([0-9a-fA-F]{8})", text)}
        results = []
        for check in checks:
            a = number(check["address"])
            if a not in values:
                raise AssertionError(f"Missing readback {a:#x}")
            actual = values[a]
            if "float" in check:
                actual = struct.unpack("<f", struct.pack("<I", actual))[0]
                expected = check["float"]
                ok = math.isfinite(actual) and math.isclose(actual, expected, rel_tol=check.get("rtol", 0), abs_tol=check.get("atol", 0))
            else:
                expected = number(check["value"])
                ok = actual == expected
            results.append(dict(address=f"0x{a:08x}", actual=actual, expected=expected, passed=ok))
        receipt["readback"] = results
        if not all(result["passed"] for result in results):
            raise AssertionError("Readback mismatch: " + json.dumps(results))
        if "PASS: real Coral core boot" not in text:
            raise AssertionError("Completion marker absent")
        receipt["status"] = "PASS"
        print("PASS: pinned real core DDR inference/readback checks")
    except BaseException as exc:
        receipt["status"] = "FAIL"
        receipt["failure"] = str(exc)
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
