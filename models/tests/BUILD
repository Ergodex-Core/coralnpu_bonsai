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
    "test_rmsnorm",
    "test_rope",
    "test_attention",
    "test_gelu_mul",
    "test_embed",
    "test_topk",
]

cocotb_test_suite(
    name = "kernels_cocotb_test",
    simulators = ["verilator"],
    testcases = KERNEL_TESTCASES,
    testcases_vname = "KERNEL_TESTCASES",
    tests_kwargs = {
        "waves": False,
        "hdl_toplevel": "VmeCoreMiniAxi",
        "seed": "1",
        "test_module": ["kernels_cocotb_test.py"],
        "deps": [
            ":reference",
            "//coralnpu_test_utils:core_mini_axi_sim_interface",
            "//coralnpu_test_utils/sim_backends:verilator_test_fixture",
            "//models/host:runtime_lib",
            requirement("numpy"),
            "@bazel_tools//tools/python/runfiles",
        ],
        "data": ["//models/kernels:all_kernels"],
        "size": "enormous",
        "tags": ["manual"],
    },
    verilator_model = "//tests/cocotb:vme_core_mini_axi_model",
)
