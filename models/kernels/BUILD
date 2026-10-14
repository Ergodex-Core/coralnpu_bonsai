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

# Per-dispatch kernels for the CoralNPU F2 image: 8 KiB ITCM, 32 KiB DTCM,
# data in DDR (0x80000000) and HBM (0xC0000000). One ELF per kernel.

load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")

package(default_visibility = ["//visibility:public"])

KERNELS = [
    "matvec_q4",
    "quant_act",
    "rmsnorm",
    "rope",
    "attention",
    "gelu_mul",
    "embed",
    "topk",
]

# No compiler-generated vector code: a stray vsetvli would clear the matrix
# engine configuration between the explicit asm blocks.
KERNEL_COPTS = [
    "-O2",
    "-fno-tree-vectorize",
    "-fno-tree-loop-vectorize",
    "-fno-tree-slp-vectorize",
]

[
    coralnpu_v2_binary(
        name = kernel,
        srcs = [kernel + ".cc"],
        hdrs = [
            "//models/common:kernel_args.h",
            "//models/common:vec.h",
            "//models/common:zvt.h",
        ],
        copts = KERNEL_COPTS,
        enable_vmem = False,
        itcm_size_kbytes = 8,
        dtcm_size_kbytes = 32,
        stack_size_bytes = 2048,
    )
    for kernel in KERNELS
]

filegroup(
    name = "all_kernels",
    srcs = [kernel + ".elf" for kernel in KERNELS],
)
