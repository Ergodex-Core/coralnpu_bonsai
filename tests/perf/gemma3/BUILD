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

# Gemma 3 kernel performance bench. Runs the RVV Gemma kernels at Gemma 3
# 270M decode shapes on the highmem RVV core and reports latency/throughput
# at 25 MHz and 1 GHz plus per-layer and per-token estimates.
#
#   bazel run //tests/perf/gemma3:gemma3_kernel_bench_verilator
#   bazel run --config=arcsim //tests/perf/gemma3:gemma3_kernel_bench
#
# Extra flags after `--`, e.g. --kv_len=512 --freq_mhz=25,250,1000.

load("@rules_python//python:defs.bzl", "py_binary")

package(default_visibility = ["//visibility:public"])

_KERNEL_PKG = "//tests/cocotb/rvv/ml_ops/gemma_kernels"

_RUNNERS = [
    "rvv_matmul",
    "rvv_bf16_matmul",
    "rvv_int8_matmul",
    "rvv_rms_norm",
    "rvv_bf16_rms_norm",
    "rvv_residual_add",
    "rvv_bf16_residual_add",
    "rvv_tanh_gelu_mul",
    "rvv_bf16_tanh_gelu_mul",
    "rvv_flashattention_test",
    "rvv_bf16_flashattention",
]

_ELFS = ["{}:{}.elf".format(_KERNEL_PKG, r) for r in _RUNNERS]

_ELF_ARGS = [
    "--elf={}=$(rootpath {}:{}.elf)".format(r, _KERNEL_PKG, r)
    for r in _RUNNERS
]

_VERILATOR_SIM = "//tests/verilator_sim:rvv_core_mini_highmem_axi_sim"

_ARCSIM_SIM = "//tests/arcsim_sim:rvv_core_mini_highmem_axi_sim"

# Verilator only; needs no ArcSim toolchain.
py_binary(
    name = "gemma3_kernel_bench_verilator",
    srcs = ["gemma3_kernel_bench.py"],
    args = ["--verilator=$(rootpath {})".format(_VERILATOR_SIM)] + _ELF_ARGS,
    data = [_VERILATOR_SIM] + _ELFS,
    main = "gemma3_kernel_bench.py",
)

# Verilator with the F2-calibrated external-memory latency (doc/arcsim.md).
py_binary(
    name = "gemma3_kernel_bench_verilator_f2",
    srcs = ["gemma3_kernel_bench.py"],
    args = [
        "--verilator=$(rootpath {})".format(_VERILATOR_SIM),
        "--axi_read_latency=18",
        "--axi_write_latency=18",
    ] + _ELF_ARGS,
    data = [_VERILATOR_SIM] + _ELFS,
    main = "gemma3_kernel_bench.py",
)

# Verilator and ArcSim side by side (cycle agreement and simulator speed).
py_binary(
    name = "gemma3_kernel_bench",
    srcs = ["gemma3_kernel_bench.py"],
    args = [
        "--verilator=$(rootpath {})".format(_VERILATOR_SIM),
        "--arcsim=$(rootpath {})".format(_ARCSIM_SIM),
    ] + _ELF_ARGS,
    data = [
        _ARCSIM_SIM,
        _VERILATOR_SIM,
    ] + _ELFS,
    main = "gemma3_kernel_bench.py",
    tags = ["arcsim"],
)

# ArcSim only (fastest).
py_binary(
    name = "gemma3_kernel_bench_arcsim",
    srcs = ["gemma3_kernel_bench.py"],
    args = ["--arcsim=$(rootpath {})".format(_ARCSIM_SIM)] + _ELF_ARGS,
    data = [_ARCSIM_SIM] + _ELFS,
    main = "gemma3_kernel_bench.py",
    tags = ["arcsim"],
)
