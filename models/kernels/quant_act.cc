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
// Per-token, per-group (32) symmetric int8 activation quantization:
//   sx = max|x| / 127, xq = round(x / sx) in [-127, 127], xsum8 = 8 * sum(xq).
// Args: m, k; in0 = x f32 [16][u0 stride floats]; out0 = xq int8 in k-major
// layout [k][16] (row i of token m at byte k*16 + m, so the matvec loads one
// contiguous 16-byte A row per k); out1 = sx f32 [16][k/32]; in1 = xsum8 f32
// [16][k/32] (output); in3 = row-major xq int8 [16][k] (output, decode path).
#include "models/common/kernel_args.h"

static int run(KernelArgs* a) {
  const uint32_t m = a->m, k = a->k, stride = a->u0 ? a->u0 : k;
  if (m == 0 || m > MAX_TOKENS || k % Q4_GROUP) return 1;
  const uint32_t groups = k / Q4_GROUP;
  for (uint32_t i = 0; i < m; ++i) {
    const float* x = (const float*)a->in0 + i * stride;
    int8_t* xq = (int8_t*)a->out0 + i;  // + k * 16 per element
    int8_t* xq1d = (int8_t*)a->in3 + i * k;
    float* sx = (float*)a->out1 + i * groups;
    float* xsum8 = (float*)a->in1 + i * groups;
    for (uint32_t g = 0; g < groups; ++g) {
      float amax, inv;
      int32_t sum;
      __asm__ volatile(
          "vsetvli t0, %[n], e32, m8, ta, ma\n\t"
          "vle32.v v8, (%[x])\n\t"
          "vfsgnjx.vv v16, v8, v8\n\t"
          "vfmv.s.f v24, %[zero]\n\t"
          "vfredmax.vs v24, v16, v24\n\t"
          "vfmv.f.s %[amax], v24\n\t"
          : [amax] "=f"(amax)
          : [n] "r"(Q4_GROUP), [x] "r"(x + g * Q4_GROUP), [zero] "f"(0.0f)
          : "t0", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15",
            "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23", "v24");
      if (amax < 1e-12f) amax = 1e-12f;
      const float s = amax / 127.0f;
      inv = 1.0f / s;
      __asm__ volatile(
          "vfmul.vf v8, v8, %[inv]\n\t"
          "vfcvt.x.f.v v8, v8\n\t"           // round to nearest even
          "vmv.s.x v24, zero\n\t"
          "vredsum.vs v24, v8, v24\n\t"
          "vmv.x.s %[sum], v24\n\t"
          "vsetvli t0, %[n], e16, m4, ta, ma\n\t"
          "vnclip.wi v16, v8, 0\n\t"
          "vsetvli t0, %[n], e8, m2, ta, ma\n\t"
          "vnclip.wi v20, v16, 0\n\t"
          "vsse8.v v20, (%[xq]), %[stride]\n\t"   // k-major: stride 16
          "vse8.v v20, (%[xq1d])\n\t"             // row-major copy
          : [sum] "=r"(sum)
          : [inv] "f"(inv), [n] "r"(Q4_GROUP), [xq] "r"(xq + g * Q4_GROUP * MAX_TOKENS),
            [stride] "r"(MAX_TOKENS), [xq1d] "r"(xq1d + g * Q4_GROUP)
          : "t0", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15",
            "v16", "v17", "v18", "v19", "v20", "v21", "v24");
      sx[g] = s;
      xsum8[g] = 8.0f * (float)sum;
    }
  }
  return 0;
}

DEFINE_KERNEL(run)
