# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ArcSim cycle-accurate simulators for the CoralNPU AXI cores. These mirror
# //tests/verilator_sim:*_axi_sim and take the same command line. All targets
# are tagged "arcsim" and need `--config=arcsim` (see doc/arcsim.md).

load("@rules_python//python:defs.bzl", "py_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load(
    "//hdl/chisel/src/coralnpu:flags.bzl",
    "CORE_MINI_AXI_VOPTS",
    "RVV_CORE_MINI_AXI_VOPTS",
    "VME_CORE_MINI_AXI_VOPTS",
)
load(
    "//rules:arcsim.bzl",
    "arcsim_model",
    "arcsim_sim_binary",
    "verilator_vopts_to_arcsim_frontend_args",
)

package(default_visibility = ["//visibility:public"])

exports_files(["arcsim_sim.cc"])

# The emitted RTL inlines these headers behind include guards; they are also
# passed on the include path so a different emit order still resolves.
_RVV_HDRS = [
    "//hdl/verilog/rvv/inc:rvv_backend.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_alu.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_define.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_dispatch.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_div.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_falu.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_opcode.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_pmtrdt.svh",
    "//hdl/verilog/rvv/inc:rvv_backend_sva.svh",
]

# core -> (top module, Verilator vopts, RTL include headers, harness defines).
# Highmem cores (1 MiB ITCM/DTCM, CSR at 0x200000) run the Gemma kernels.
_CORES = {
    "core_mini_axi": ("CoreMiniAxi", CORE_MINI_AXI_VOPTS, [], []),
    "rvv_core_mini_axi": ("RvvCoreMiniAxi", RVV_CORE_MINI_AXI_VOPTS, _RVV_HDRS, []),
    "rvv_core_mini_highmem_axi": (
        "RvvCoreMiniHighmemAxi",
        RVV_CORE_MINI_AXI_VOPTS,
        _RVV_HDRS,
        ["HIGHMEM_RTL"],
    ),
    "vme_core_mini_axi": ("VmeCoreMiniAxi", VME_CORE_MINI_AXI_VOPTS, _RVV_HDRS, []),
    "vme_core_mini_highmem_axi": (
        "VmeCoreMiniHighmemAxi",
        VME_CORE_MINI_AXI_VOPTS,
        _RVV_HDRS,
        ["HIGHMEM_RTL"],
    ),
}

[
    arcsim_model(
        name = core + "_model",
        srcs = ["//hdl/chisel/src/coralnpu:{}_cc_library_emit_verilog_single_sv".format(core)],
        hdrs = hdrs,
        frontend_args = verilator_vopts_to_arcsim_frontend_args(vopts) + (
            ["-Ihdl/verilog/rvv/inc"] if hdrs else []
        ),
        top = top,
    )
    for core, (top, vopts, hdrs, _) in _CORES.items()
]

[
    arcsim_sim_binary(
        name = core + "_sim",
        defines = defines,
        model = ":{}_model".format(core),
    )
    for core, (_, _, _, defines) in _CORES.items()
]

# Runs the same ELFs on ArcSim and Verilator and requires the same pass/fail
# result. Cycle counts are reported, not compared: the AXI testbench models
# differ in latency. There is no Verilator VME simulator, so the VME target
# only checks that ArcSim passes.
_DIFF_TESTS = {
    "core_mini_axi": (
        "//tests/verilator_sim:core_mini_axi_sim",
        ["//examples:coralnpu_v2_hello_world_add_floats"],
    ),
    "rvv_core_mini_axi": (
        "//tests/verilator_sim:rvv_core_mini_axi_sim",
        [
            "//examples:coralnpu_v2_hello_world_add_floats",
            "//examples:coralnpu_v2_rvv_add_intrinsic",
        ],
    ),
    "vme_core_mini_axi": (
        None,
        [
            "//examples:coralnpu_v2_hello_world_add_floats",
            "//examples:coralnpu_v2_rvv_add_intrinsic",
        ],
    ),
}

[
    sh_test(
        name = core + "_vs_verilator_test",
        size = "enormous",
        srcs = ["arcsim_vs_verilator_test.sh"],
        args = [
            "$(rootpath :{}_sim)".format(core),
            "$(rootpath {})".format(verilator) if verilator else "none",
        ] + ["$(rootpaths {})".format(elf) for elf in elfs],
        data = [":{}_sim".format(core)] + ([verilator] if verilator else []) + elfs,
        tags = ["arcsim"],
    )
    for core, (verilator, elfs) in _DIFF_TESTS.items()
]

# Performance comparison: `bazel run --config=arcsim :<core>_perf -- [--cpu N]`
# prints a Verilator-vs-ArcSim cycles/s table for a few workloads.
py_binary(
    name = "compare_perf",
    srcs = ["compare_perf.py"],
)

_RVV_PERF_ELFS = [
    "//examples:coralnpu_v2_hello_world_add_floats",
    "//tests/cocotb/rvv/ml_ops:rvv_matmul.elf",
    "//tests/cocotb/rvv/ml_ops:rvv_float_matmul_optimized.elf",
]

_PERF = {
    "core_mini_axi": (
        "//tests/verilator_sim:core_mini_axi_sim",
        ["//examples:coralnpu_v2_hello_world_add_floats"],
    ),
    "rvv_core_mini_axi": ("//tests/verilator_sim:rvv_core_mini_axi_sim", _RVV_PERF_ELFS),
    "vme_core_mini_axi": (None, _RVV_PERF_ELFS),
}

[
    py_binary(
        name = core + "_perf",
        srcs = ["compare_perf.py"],
        args = [
            "--label=" + core,
            "--arcsim=$(rootpath :{}_sim)".format(core),
            "--verilator=" + ("$(rootpath {})".format(verilator) if verilator else "none"),
        ] + ["$(rootpaths {})".format(elf) for elf in elfs],
        data = [":{}_sim".format(core)] + ([verilator] if verilator else []) + elfs,
        main = "compare_perf.py",
        tags = ["arcsim"],
    )
    for core, (verilator, elfs) in _PERF.items()
]
