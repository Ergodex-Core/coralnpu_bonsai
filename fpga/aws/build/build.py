#!/usr/bin/env python3
"""Build and qualify a Coral NPU F2 image using pre-provisioned licensed tools.

No downloads of proprietary tools, cloud lifecycle operations, credentials,
licenses, AFIs, or hardware loading are performed by this entrypoint.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
AWS = HERE.parent
REPO = AWS.parent.parent
PINS = json.loads((HERE / "pins.json").read_text())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capture(argv, cwd=None):
    return subprocess.check_output(
        argv, cwd=cwd, text=True, stderr=subprocess.STDOUT, timeout=60
    ).strip()


def git(root, *args):
    return capture(["git", "--no-optional-locks", "-C", str(root), *args])


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def run(argv, log, *, cwd, env=None, timeout=60):
    """Kill the complete compiler/Vivado process group on timeout; retain output."""
    print(f"Running {log.stem}; timeout {timeout}s", flush=True)
    with log.open("w") as f:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            start_new_session=True
        )
        try:
            code = proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise RuntimeError(
                f"{log.name} timed out or interrupted"
            ) from None
    require(code == 0, f"{log.name} exited {code}; inspect retained log")
    return log.read_text(errors="replace")


def source_check():
    require((REPO /
             ".bazelversion").read_text().strip() == PINS["bazel_version"],
            "Bazel version changed; update and qualify pins")
    wrapper = (AWS / "cl_coralnpu/design/cl_coralnpu.sv").read_text()
    require(".BUFGCE_DIVIDE(5)" in wrapper, "Unexpected NPU divider")
    require(
        "parameter EN_DDR = 1" in wrapper
        and "parameter EN_HBM = 0" in wrapper, "Expected DDR enabled / HBM disabled configuration"
    )
    require(
        "cl_axi_clock_converter_light i_host_cdc" in wrapper,
        "Expected OCL clock converter missing"
    )
    require(
        "FPGA_XILINX" in (REPO / "hdl/verilog/ClockGate.sv").read_text(),
        "Expected FPGA clock gate missing"
    )
    require(
        "`ifdef SYNTHESIS" in (REPO / "hdl/verilog/Sram.v").read_text(),
        "Synthesizable SRAM branch missing"
    )
    require(PINS["ddr_enabled"] and not PINS["hbm_enabled"], "Memory enable pins differ")
    require(PINS["ddr_cpu_base"] == 0x20000000 and
            PINS["ddr_aperture_bytes"] == 0x80000000 and
            PINS["ddr_axi_data_bits"] == 512 and
            PINS["ddr_axi_clock_hz"] == PINS["shell_clock_hz"],
            "DDR address/width/clock pins differ")
    subprocess.run([
        sys.executable, "-m", "unittest", "discover", "-s",
        str(HERE), "-p", "test_*.py"
    ],
                   check=True)
    print("CORAL_SOURCE_CHECK_PASSED")


def preflight(hdk, out):
    require(
        platform.system() == "Linux" and platform.machine() == "x86_64",
        "Build runner must be Linux x86_64"
    )
    require(
        hdk.is_dir(),
        "Provision a pinned AWS HDK checkout; no automatic installation is performed"
    )
    require(
        git(hdk, "rev-parse", "HEAD") == PINS["hdk_commit"],
        "Wrong HDK revision"
    )
    require(
        git(hdk / "hdk/common/ip", "rev-parse", "HEAD") == PINS["ip_commit"],
        "Wrong HDK IP revision"
    )
    # This IP gitlink intentionally differs from the HDK superproject default.
    changed = git(hdk, "diff", "HEAD", "--name-only").splitlines()
    require(set(changed) <= {"hdk/common/ip"}, "HDK tracked files modified")
    require(
        not git(
            hdk / "hdk/common/ip", "status", "--porcelain",
            "--untracked-files=no"
        ), "HDK IP tracked files modified"
    )
    shell = hdk / "hdk/common/shell_stable"
    require(
        f"small_shell={PINS['shell_version']}"
        in (shell / "shell_version.txt").read_text(), "Wrong shell version"
    )
    require(
        f"RELEASE_VERSION={PINS['hdk_version']}"
        in (hdk / "release_version.txt").read_text(),
        "Wrong HDK release version"
    )
    dependencies = {
        "shell_dcp":
        shell / "build/checkpoints/from_aws/cl_bb_routed.small_shell.dcp",
        "clock_converter_xci": hdk /
        "hdk/common/ip/cl_ip/cl_ip.srcs/sources_1/ip/cl_axi_clock_converter_light/cl_axi_clock_converter_light.xci",
        "ddr4_xci": hdk /
        "hdk/common/ip/cl_ip/cl_ip.srcs/sources_1/ip/cl_ddr4_32g/cl_ddr4_32g.xci",
        "build_all_tcl": shell / "build/scripts/build_all.tcl",
        "encrypt_tcl": shell / "build/scripts/encrypt.tcl",
    }
    for name, path in dependencies.items():
        require(path.is_file(), f"Pre-provisioned dependency missing: {name}")
        require(
            sha256(path) == PINS[name + "_sha256"],
            f"Wrong dependency hash: {name}"
        )
    version = capture(["vivado", "-version"])
    require(
        f"Vivado v{PINS['vivado_version']}" in version
        and PINS["vivado_build"] in version, "Wrong Vivado version/build"
    )
    require(
        capture(["bazel", "--version"]) == "bazel " + PINS["bazel_version"],
        "Wrong Bazel version"
    )
    verilator = os.environ.get("VERILATOR", "verilator")
    require(
        re.search(
            r"\bVerilator " + re.escape(PINS["verilator_version"]) + r"\b",
            capture([verilator, "--version"])
        ), "Wrong Verilator version"
    )
    cpus = len(os.sched_getaffinity(0))
    cpu_limit = Path("/sys/fs/cgroup/cpu.max")
    if cpu_limit.is_file():
        quota, period = cpu_limit.read_text().split()
        if quota != "max":
            cpus = min(cpus, int(quota) / int(period))
    memory = int(
        re.search(r"MemAvailable:\s+(\d+)",
                  Path("/proc/meminfo").read_text())[1]
    ) * 1024
    cgroup_limit = Path("/sys/fs/cgroup/memory.max")
    if cgroup_limit.is_file() and cgroup_limit.read_text().strip().isdigit():
        usage = int(Path("/sys/fs/cgroup/memory.current").read_text())
        memory = min(memory, max(0, int(cgroup_limit.read_text()) - usage))
    free = shutil.disk_usage(out).free
    require(
        cpus >= PINS["minimum_cpus"], "Runner needs at least 16 available CPUs"
    )
    require(
        memory >= PINS["minimum_memory_gib"] * 2**30,
        "Runner needs at least 32 GiB available RAM"
    )
    require(
        free >= PINS["minimum_free_disk_gib"] * 2**30,
        "Runner needs at least 100 GiB free disk"
    )
    log = out / "logs/license.log"
    probe = out / "license-probe"
    probe.mkdir()
    run([
        "vivado", "-mode", "batch", "-source",
        str(HERE / "license_probe.tcl"), "-log",
        str(probe / "vivado.log"), "-journal",
        str(probe / "vivado.jou"), "-tclargs",
        str(probe)
    ],
        log,
        cwd=probe,
        timeout=300)
    from gates import validate_stage
    validate_stage(log.read_text(), "license")
    result = {
        "passed": True,
        "cpus": cpus,
        "available_ram_bytes": memory,
        "free_disk_bytes": free,
        "license_probe_scope":
        "synthesis for xcvu47p only; full shell implementation licensing is verified by the build stages",
        "tool_versions": {
            "vivado": PINS["vivado_version"],
            "vivado_build": PINS["vivado_build"],
            "bazel": PINS["bazel_version"],
            "verilator": PINS["verilator_version"]
        },
        "dependency_hashes": {
            k: sha256(v)
            for k, v in dependencies.items()
        }
    }
    write_json(out / "preflight.json", result)
    return result


def prepare_rtl(emitted, archive, dest):
    """Retain the emitted design and include archive; no SRAM rewrite is needed."""
    dest.mkdir(parents=True)
    shutil.copy2(emitted, dest / "RvvCoreMiniAxi.sv")
    include = dest / "include"
    include.mkdir()
    seen = set()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            path = Path(member.filename)
            require(
                not path.is_absolute() and ".." not in path.parts,
                "Unsafe RTL archive path"
            )
            if member.is_dir():
                continue
            require(path.name not in seen, "Duplicate RTL include basename")
            require((member.external_attr >> 16) & 0o170000 != 0o120000,
                    "RTL archive symlink rejected")
            seen.add(path.name)
            (include / path.name).write_bytes(z.read(member))
    require(
        "rvv_backend.svh" in seen and "Sram.v" in seen,
        "Missing RTL include/SRAM sources"
    )


def validate_parameters(text):
    rows = re.findall(r"^#define KP_(\w+)\s+(\S+)\s*$", text, re.M)
    parameters = dict(rows)
    require(
        len(rows) == len(parameters), "Duplicate generated hardware parameter"
    )
    expected = dict(
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
    require(
        all(parameters.get(k) == v for k, v in expected.items()),
        "Generated ISA/memory/bus configuration differs from pinned F2 configuration"
    )
    return parameters


def build(args, out):
    from gates import qualify, package, validate_stage
    hdk = args.hdk_root.resolve()
    preflight_result = preflight(hdk, out)
    require(
        re.fullmatch(r"[A-Za-z0-9_-]{1,83}", args.tag),
        "Tag must be 1-83 letters, digits, underscores or hyphens"
    )
    # AWS's Tcl file paths are not universally list-quoted.
    require(
        not re.search(r"[\s\[\]{};$]",
                      str(out) + str(hdk)),
        "Use HDK/output paths without Tcl metacharacters or spaces"
    )
    cl = out / "cl_coralnpu"
    shutil.copytree(AWS / "cl_coralnpu", cl)
    shell = hdk / "hdk/common/shell_stable"
    scripts = cl / "build/scripts"
    for name in ("build_all.tcl", "encrypt.tcl"):
        shutil.copy2(shell / "build/scripts" / name, scripts / name)
    env = os.environ.copy()
    env.update(
        AWS_FPGA_REPO_DIR=str(hdk),
        HDK_DIR=str(hdk / "hdk"),
        HDK_COMMON_DIR=str(hdk / "hdk/common"),
        HDK_SHELL_DIR=str(shell),
        HDK_SHELL_DESIGN_DIR=str(shell / "design"),
        CL_DIR=str(cl),
        HDK_IP_SRC_DIR=str(
            hdk / "hdk/common/ip/cl_ip/cl_ip.srcs/sources_1/ip"
        ),
        HDK_BD_SRC_DIR=str(
            hdk / "hdk/common/ip/cl_ip/cl_ip.srcs/sources_1/bd"
        ),
        HDK_BD_GEN_DIR=str(hdk / "hdk/common/ip/cl_ip/cl_ip.gen/sources_1/bd"),
        VIVADO_TOOL_VERSION=PINS["vivado_version"],
        CL="cl_coralnpu",
        SHELL_MODE=PINS["shell_mode"],
        ENCRYPT="0",
        BUILD_TAG=args.tag
    )
    source_files = git(REPO, "ls-files", "-z").split("\0")
    hashes = {
        p: sha256(REPO / p)
        for p in source_files
        if p and (REPO / p).is_file()
    }
    # Include newly added FPGA sources for a local rehearsal before committing.
    hashes.update({
        str(p.relative_to(REPO)): sha256(p)
        for p in AWS.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    })
    manifest = {
        "schema_version": 1,
        "source_commit": git(REPO, "rev-parse", "HEAD"),
        "github_merge_sha": os.environ.get("GITHUB_SHA"),
        "pr_head_sha": os.environ.get("PR_HEAD_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID", args.tag),
        "pins": PINS,
        "source_hashes": hashes,
        "preflight": preflight_result,
        "stages": {},
        "physical_execution": "not_run",
        "build_qualification": "incomplete"
    }

    def stage(name, action):
        manifest["stages"][name] = {
            "status": "running",
            "start_unix": time.time()
        }
        write_json(out / "provenance.json", manifest)
        action()
        manifest["stages"][name].update(status="passed", end_unix=time.time())
        write_json(out / "provenance.json", manifest)

    stage(
        "rtl", lambda: run(["bazel", "build", PINS["rtl_target"]],
                           out / "logs/rtl.log",
                           cwd=REPO,
                           timeout=PINS["timeouts_seconds"]["rtl"])
    )
    emitted = REPO / "bazel-bin/hdl/chisel/src/coralnpu/RvvCoreMiniAxi.sv"
    archive = emitted.with_suffix(".zip")
    prepare_rtl(emitted, archive, cl / "rtl")
    manifest["generated_hashes"] = {
        "top": sha256(emitted),
        "archive": sha256(archive)
    }
    reference_diff = git(
        REPO, "diff", PINS["coral_reference_commit"], "--", "hdl", "rules",
        "third_party", "MODULE.bazel", "MODULE.bazel.lock", ".bazelrc",
        ".bazelversion"
    )
    if not reference_diff:
        require(
            sha256(emitted) == PINS["reference_emitted_rtl_sha256"],
            "Unmodified reference source emitted unexpected RTL; generator/tool provenance differs"
        )
    for path in sorted((cl / "rtl/include").iterdir()):
        manifest["generated_hashes"]["include/" + path.name] = sha256(path)
    includes = {p.name: sha256(p) for p in (cl / "rtl/include").iterdir()}
    inventory_hash = hashlib.sha256(
        json.dumps(includes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["include_inventory_sha256"] = inventory_hash
    if not reference_diff:
        require(
            inventory_hash == PINS["reference_include_inventory_sha256"],
            "Unmodified source emitted unexpected black-box/SRAM includes"
        )
    header = emitted.parent / "VRvvCoreMiniAxi_parameters.h"
    manifest["generated_parameters"] = validate_parameters(header.read_text())
    manifest["generated_hashes"]["parameters"] = sha256(header)
    shutil.copy2(header, out / header.name)

    def simulate():
        obj = out / "simulation"
        command = [
            os.environ.get("VERILATOR", "verilator"), "--binary", "--timing",
            "-j", "16", "--top-module", "tb_host", "--Mdir",
            str(obj), "-Wno-fatal", "-Wno-BLKANDNBLK", "-Wno-WIDTH",
            "-Wno-UNOPTFLAT",
            "+define+USE_GENERIC+SYNTHESIS+VLEN_128+ZVE32F_ON+TB_SUPPORT",
            "+incdir+" + str(cl / "rtl/include"),
            str(cl / "rtl/RvvCoreMiniAxi.sv"),
            str(cl / "design/coral_host.sv"),
            str(HERE / "sim/tb_host.sv")
        ]
        run(
            command,
            out / "logs/simulation-compile.log",
            cwd=out,
            timeout=PINS["timeouts_seconds"]["simulation"]
        )
        text = run([str(obj / "Vtb_host")],
                   out / "logs/simulation.log",
                   cwd=out,
                   timeout=120)
        for marker in (
                "RUN 0 PASS result=42", "RUN 1 PASS result=43",
                "PASS: RVV vector addition",
                "PASS: TCM lanes, byte strobes, independent AW/W, response backpressure, execution and restart"
        ):
            require(marker in text, f"Simulation missing result: {marker}")

    stage("simulation", simulate)

    def simulate_ddr():
        # A missing simulator must not silently turn unittest SKIP into proof.
        require(shutil.which("iverilog") and shutil.which("vvp"),
                "DDR protocol qualification requires iverilog and vvp")
        manifest["ddr_simulation_tools"] = {
            name: capture([name, "-V"]) for name in ("iverilog", "vvp")
        }
        run([sys.executable, "-m", "unittest", "discover", "-s",
             str(AWS / "ddr/sim"), "-p", "test_*.py", "-v"],
            out / "logs/ddr-simulation.log", cwd=REPO, timeout=300)

    stage("ddr_simulation", simulate_ddr)
    for name, flow in (("synthesis", "SynthCL"), ("implementation", "ImplCL")):

        def vivado_stage(name=name, flow=flow):
            env["BUILD_FLOW"] = flow
            log = out / "logs" / (name + ".log")
            run([
                "vivado", "-mode", "batch", "-source", "build_all.tcl", "-log",
                str(out / "logs" / (name + ".vivado.log")), "-journal",
                str(out / "logs" / (name + ".jou")), "-tclargs",
                *PINS["directives"], *PINS["clock_recipes"]
            ],
                log,
                cwd=scripts,
                env=env,
                timeout=PINS["timeouts_seconds"][name])
            validate_stage(log.read_text(), name)
            require(
                not list((cl / "build/checkpoints").glob("*.VIOLATED.dcp")),
                "Timing-violated checkpoint rejected"
            )

        stage(name, vivado_stage)
    checkpoint = cl / "build/checkpoints" / f"cl_coralnpu.{args.tag}.post_route.dcp"
    require(
        checkpoint.is_file() and checkpoint.stat().st_size > 1_000_000,
        "Routed checkpoint absent/incomplete"
    )
    checkpoint_hash = sha256(checkpoint)
    validation = out / "validation"
    validation.mkdir()

    def validate():
        log = out / "logs/validation.log"
        run([
            "vivado", "-mode", "batch", "-source",
            str(HERE / "validate_checkpoint.tcl"), "-log",
            str(validation / "vivado.log"), "-journal",
            str(validation / "vivado.jou"), "-tclargs",
            str(checkpoint),
            str(validation)
        ],
            log,
            cwd=validation,
            env=env,
            timeout=PINS["timeouts_seconds"]["validation"])
        validate_stage(log.read_text(), "validation_reports")
        require(
            sha256(checkpoint) == checkpoint_hash,
            "Checkpoint changed while validating"
        )

    stage("validation", validate)
    require(
        all((REPO / name).is_file() and sha256(REPO / name) == digest
            for name, digest in hashes.items()),
        "Source changed during the build; refusing to bind this checkpoint to stale provenance"
    )
    qualification = qualify(
        validation, {
            n: out / "logs" / (n + ".log")
            for n in ("synthesis", "implementation", "validation")
        }, checkpoint, PINS
    )
    write_json(out / "qualification.json", qualification)
    manifest.update(
        checkpoint_sha256=checkpoint_hash,
        build_qualification="passed"
        if qualification["qualified"] else "failed",
        report_hashes={
            p.name: sha256(p)
            for p in validation.iterdir()
            if p.is_file()
        },
        log_hashes={
            p.name: sha256(p)
            for p in (out / "logs").iterdir()
            if p.is_file()
        }
    )
    write_json(out / "provenance.json", manifest)
    require(
        qualification["qualified"],
        "Qualification blocked: " + "; ".join(qualification["blockers"])
    )
    native = dict(
        pci_device_id="0xF010",
        pci_vendor_id="0x1D0F",
        pci_subsystem_id="0x1D51",
        pci_subsystem_vendor_id="0xFEDC",
        manifest_format_version=2,
        dcp_hash=checkpoint_hash,
        shell_version=PINS["shell_version"],
        hdk_version=PINS["hdk_version"],
        tool_version="v" + PINS["vivado_version"],
        date=args.tag,
        dcp_file_name=args.tag + ".SH_CL_routed.dcp"
    )
    native.update(
        zip((
            "clock_recipe_a", "clock_recipe_b", "clock_recipe_c",
            "clock_recipe_hbm"
        ), PINS["clock_recipes"])
    )
    receipt = package(
        checkpoint.parent / (args.tag + ".Developer_CL.tar"), checkpoint,
        validation / "debug_probes.ltx", native
    )
    write_json(out / "package-receipt.json", receipt)
    print("CORAL_PACKAGE_PASSED")


def main():

    def cancelled(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-check", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument(
        "--hdk-root",
        type=Path,
        default=Path(os.environ.get("AWS_FPGA_REPO_DIR", "/nonexistent/hdk"))
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    if args.source_check:
        source_check()
        return
    require(args.output is not None, "--output is required")
    require(args.preflight or args.tag, "--tag is required")
    out = args.output.resolve()
    require(
        not out.exists(),
        "Output directory must be fresh; refusing to reuse stale build products"
    )
    out.mkdir(parents=True)
    (out / "logs").mkdir()
    try:
        if args.preflight:
            preflight(args.hdk_root.resolve(), out)
            print("CORAL_PREFLIGHT_PASSED")
        else:
            build(args, out)
    except Exception as exc:
        write_json(
            out / "failure.json", {
                "status": "failed",
                "reason": str(exc),
                "physical_execution": "not_run"
            }
        )
        raise


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError,
            ValueError) as exc:
        print(f"CORAL_BUILD_FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
