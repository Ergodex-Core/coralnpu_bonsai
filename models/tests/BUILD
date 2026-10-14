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

# Kernel unit tests on the Verilator VmeCoreMiniAxi model (8K/32K TCM with
# the matrix engine, external memory at 0x80000000), following
# tests/cocotb/vme_test/BUILD.

load("@coralnpu_hw//third_party/python:requirements.bzl", "requirement")
load("//rules:coco_tb.bzl", "cocotb_test_suite")

package(default_visibility = ["//visibility:public"])

py_library(
    name = "reference",
    srcs = ["reference.py"],
    deps = ["//models/host:runtime_lib"],
)

py_test(
    name = "reference_test",
    srcs = ["reference_test.py"],
    deps = [
        ":reference",
        "//models/host:runtime_lib",
        requirement("numpy"),
    ],
)

KERNEL_TESTCASES = [
    "test_quant_act",
    "test_matvec_q4",
    "test_matvec_q4_decode",
    "test_rmsnorm",
    "test_rope",
    "test_attention",
    "test_gelu_mul",
    "test_embed",
    "test_topk",
]

PERF_TESTCASES = [
    "test_perf_matvec_dim",
    "test_perf_matvec_attn_out",
    "test_perf_matvec_down",
    "test_perf_elementwise",
    "test_perf_attention",
    "test_perf_topk",
]

# Replays fpga_kernels_test.py for memory-latency calibration against the F2.
F2_CALIBRATION_TESTCASES = [
    "test_calib_matvec_decode",
    "test_calib_matvec_q4",
    "test_calib_quant_act",
    "test_calib_rmsnorm",
    "test_calib_rope",
    "test_calib_attention",
    "test_calib_gelu_mul",
    "test_calib_embed",
    "test_calib_topk",
    "test_calib_topk_big",
]

FUSED_PERF_TESTCASES = [
    "test_fused_matvec_dim",
    "test_fused_matvec_attn_out",
    "test_fused_matvec_down",
    "test_fused_elementwise",
    "test_fused_attention",
]

# Each suite runs on the baseline core and on the core with the LSU read
# prefetch bridge ("_pf"). The perf suites print per-kernel cycle counts; set
# the memory latency with --test_env=AXI_READ_LATENCY=N (see estimate_e2e.py).
[
    cocotb_test_suite(
        name = name + suffix,
        simulators = ["verilator"],
        testcases = cases,
        testcases_vname = vname,
        tests_kwargs = {
            "waves": False,
            "hdl_toplevel": toplevel,
            "seed": "1",
            "test_module": [name + ".py"],
            "deps": [
                ":reference",
                "//coralnpu_test_utils:core_mini_axi_sim_interface",
                "//coralnpu_test_utils/sim_backends:verilator_test_fixture",
                "//models/host:runtime_lib",
                "@bazel_tools//tools/python/runfiles",
            ] + ([":kernels_cocotb_test_lib", ":fused_perf_ops"] if name != "kernels_cocotb_test" else []),
            "data": ["//models/kernels:all_kernels"],
            "size": "enormous",
            "tags": ["manual"],
        },
        verilator_model = model,
    )
    for name, cases, vname in [
        ("kernels_cocotb_test", KERNEL_TESTCASES, "KERNEL_TESTCASES"),
        ("kernels_perf_cocotb_test", PERF_TESTCASES, "PERF_TESTCASES"),
        ("kernels_fused_perf_cocotb_test", FUSED_PERF_TESTCASES, "FUSED_PERF_TESTCASES"),
        ("f2_calibration_cocotb_test", F2_CALIBRATION_TESTCASES, "F2_CALIBRATION_TESTCASES"),
    ]
    for suffix, toplevel, model in [
        ("", "VmeCoreMiniAxi", "//tests/cocotb:vme_core_mini_axi_model"),
        ("_pf", "VmeCoreMiniPfAxi", "//tests/cocotb:vme_core_mini_pf_axi_model"),
    ]
]

py_library(
    name = "fused_perf_ops",
    srcs = ["fused_perf_ops.py"],
    deps = ["//models/host:runtime_lib"],
)

py_library(
    name = "kernels_cocotb_test_lib",
    srcs = ["kernels_cocotb_test.py"],
)
