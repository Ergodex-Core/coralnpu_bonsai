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
// Rotary embedding (rotate-half, as gemma's apply_rope) over heads, with
// optional KV-cache writes.
// Args: m tokens; in0 = x f32 [16][u1 heads][u2 head_dim] (row stride n
// floats), rotated in place; in1 = cos table f32 [pos][head_dim/2];
// in2 = sin table; u0 = pos0 (row i has position pos0 + i).
// flags bit0: write rotated head 0 of each row to kcache (out0, f32
// [S][head_dim]) at position pos0 + i.
// flags bit1: copy in3 rows (head_dim floats, stride n) to vcache (out1).
#include "models/common/kernel_args.h"

static int run(KernelArgs* a) {
  const uint32_t m = a->m, heads = a->u1, hd = a->u2, stride = a->n;
  const uint32_t half = hd / 2;
  if (m == 0 || m > MAX_TOKENS || heads == 0 || hd % 64) return 1;
  for (uint32_t i = 0; i < m; ++i) {
    const uint32_t pos = a->u0 + i;
    const float* c = (const float*)a->in1 + pos * half;
    const float* s = (const float*)a->in2 + pos * half;
    for (uint32_t h = 0; h < heads; ++h) {
      float* x = (float*)a->in0 + i * stride + h * hd;
      for (uint32_t j = 0; j < half;) {
        uint32_t vl;
        __asm__ volatile(
            "vsetvli %[vl], %[rem], e32, m4, ta, ma\n\t"
            "vle32.v v8, (%[x1])\n\t"
            "vle32.v v12, (%[x2])\n\t"
            "vle32.v v16, (%[c])\n\t"
            "vle32.v v20, (%[s])\n\t"
            "vfmul.vv v24, v8, v16\n\t"     // x1*c
            "vfnmsac.vv v24, v12, v20\n\t"  // - x2*s
            "vfmul.vv v28, v12, v16\n\t"    // x2*c
            "vfmacc.vv v28, v8, v20\n\t"    // + x1*s
            "vse32.v v24, (%[x1])\n\t"
            "vse32.v v28, (%[x2])\n\t"
            : [vl] "=r"(vl)
            : [rem] "r"(half - j), [x1] "r"(x + j), [x2] "r"(x + half + j),
              [c] "r"(c + j), [s] "r"(s + j)
            : "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v16",
              "v17", "v18", "v19", "v20", "v21", "v22", "v23", "v24", "v25", "v26",
              "v27", "v28", "v29", "v30", "v31");
        j += vl;
      }
    }
    if (a->flags & 1) {
      const float* k0 = (const float*)a->in0 + i * stride;
      float* kc = (float*)a->out0 + pos * hd;
      for (uint32_t j = 0; j < hd; ++j) kc[j] = k0[j];
    }
    if (a->flags & 2) {
      const float* v0 = (const float*)a->in3 + i * stride;
      float* vc = (float*)a->out1 + pos * hd;
      for (uint32_t j = 0; j < hd; ++j) vc[j] = v0[j];
    }
  }
  return 0;
}

DEFINE_KERNEL(run)
