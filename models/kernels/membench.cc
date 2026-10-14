// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// Memory microbenchmark: streams n bytes from in0 with vector loads of
// u0 bytes each (16, 32, 64 or 128), or stores to out0 when flags bit0 is
// set; cycles are in kargs.cycles. Reports the sum of the first byte of each
// load in u1 so the loads are not dead.
#include "models/common/kernel_args.h"

static int run(KernelArgs* a) {
  uint32_t n = a->n;
  const uint32_t chunk = a->u0 ? a->u0 : 16;
  if (n == 0 || n % chunk || (chunk != 16 && chunk != 32 && chunk != 64 && chunk != 128)) return 1;
  const uint8_t* p = (const uint8_t*)a->in0;
  uint8_t* o = (uint8_t*)a->out0;
  uint32_t acc = 0;
  if (a->flags & 1) {
    __asm__ volatile(
        "vsetvli t0, %[chunk], e8, m8, ta, ma\n\t"
        "vmv.v.i v8, 1\n\t"
        "1:\n\t"
        "vse8.v v8, (%[o])\n\t"
        "add %[o], %[o], %[chunk]\n\t"
        "sub %[n], %[n], %[chunk]\n\t"
        "bnez %[n], 1b\n\t"
        : [o] "+r"(o), [n] "+r"(n)
        : [chunk] "r"(chunk)
        : "t0", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15");
  } else {
    __asm__ volatile(
        "vsetvli t0, %[chunk], e8, m8, ta, ma\n\t"
        "vmv.v.i v16, 0\n\t"
        "1:\n\t"
        "vle8.v v8, (%[p])\n\t"
        "vadd.vv v16, v16, v8\n\t"
        "add %[p], %[p], %[chunk]\n\t"
        "sub %[n], %[n], %[chunk]\n\t"
        "bnez %[n], 1b\n\t"
        "vmv.x.s %[acc], v16\n\t"
        : [p] "+r"(p), [n] "+r"(n), [acc] "=r"(acc)
        : [chunk] "r"(chunk)
        : "t0", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15",
          "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23");
  }
  a->u1 = acc;
  return 0;
}

DEFINE_KERNEL(run)
