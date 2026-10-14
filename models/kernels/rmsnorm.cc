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
// RMSNorm (Gemma): y = x * rsqrt(mean(x^2) + eps) * (1 + scale), optionally
// added to a residual: out = resid + y.
// Args: m rows, k = row length; in0 = x f32 [16][u0 stride]; in1 = scale
// f32 [k]; in2 = residual f32 [16][u0] (flags bit0); out0 = out f32 [16][u0];
// f0 = eps. Rows are independent, so q/k norms pass m = tokens * heads with
// k = head_dim and stride = head_dim.
#include "models/common/kernel_args.h"
#include "models/common/vec.h"

static int run(KernelArgs* a) {
  const uint32_t m = a->m, k = a->k, stride = a->u0 ? a->u0 : k;
  if (m == 0 || k == 0) return 1;
  const float* scale = (const float*)a->in1;
  const int with_resid = a->flags & 1;
  for (uint32_t i = 0; i < m; ++i) {
    const float* x = (const float*)a->in0 + i * stride;
    const float* r = (const float*)a->in2 + i * stride;
    float* o = (float*)a->out0 + i * stride;
    float ss = 0.0f;
    for (uint32_t j = 0; j < k;) {
      uint32_t vl;
      float part;
      __asm__ volatile(
          "vsetvli %[vl], %[rem], e32, m8, ta, ma\n\t"
          "vle32.v v8, (%[x])\n\t"
          "vfmul.vv v8, v8, v8\n\t"
          "vfmv.s.f v16, %[zero]\n\t"
          "vfredusum.vs v16, v8, v16\n\t"
          "vfmv.f.s %[part], v16\n\t"
          : [vl] "=r"(vl), [part] "=f"(part)
          : [rem] "r"(k - j), [x] "r"(x + j), [zero] "f"(0.0f)
          : "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v16");
      ss += part;
      j += vl;
    }
    const float inv = 1.0f / fsqrt(ss / (float)k + a->f0);
    for (uint32_t j = 0; j < k;) {
      uint32_t vl;
      if (with_resid) {
        __asm__ volatile(
            "vsetvli %[vl], %[rem], e32, m8, ta, ma\n\t"
            "vle32.v v8, (%[x])\n\t"
            "vle32.v v16, (%[s])\n\t"
            "vle32.v v24, (%[r])\n\t"
            "vfmul.vf v8, v8, %[inv]\n\t"
            "vfadd.vf v16, v16, %[one]\n\t"
            "vfmacc.vv v24, v8, v16\n\t"
            "vse32.v v24, (%[o])\n\t"
            : [vl] "=r"(vl)
            : [rem] "r"(k - j), [x] "r"(x + j), [s] "r"(scale + j), [r] "r"(r + j),
              [o] "r"(o + j), [inv] "f"(inv), [one] "f"(1.0f)
            : "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v16",
              "v17", "v18", "v19", "v20", "v21", "v22", "v23", "v24", "v25", "v26",
              "v27", "v28", "v29", "v30", "v31");
      } else {
        __asm__ volatile(
            "vsetvli %[vl], %[rem], e32, m8, ta, ma\n\t"
            "vle32.v v8, (%[x])\n\t"
            "vle32.v v16, (%[s])\n\t"
            "vfmul.vf v8, v8, %[inv]\n\t"
            "vfadd.vf v16, v16, %[one]\n\t"
            "vfmul.vv v8, v8, v16\n\t"
            "vse32.v v8, (%[o])\n\t"
            : [vl] "=r"(vl)
            : [rem] "r"(k - j), [x] "r"(x + j), [s] "r"(scale + j), [o] "r"(o + j),
              [inv] "f"(inv), [one] "f"(1.0f)
            : "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v16",
              "v17", "v18", "v19", "v20", "v21", "v22", "v23");
      }
      j += vl;
    }
  }
  return 0;
}

DEFINE_KERNEL(run)
