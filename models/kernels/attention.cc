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
// Causal attention for one layer with a single KV head (Gemma 3 270M/1B):
//   scores[s] = q . K[s] * f0, s in [start, pos], softmax, out = sum p[s] V[s].
// Args: m tokens; in0 = q f32 [16][u1 heads][u2 head_dim] (row stride n);
// in1 = kcache f32 [S][head_dim]; in2 = vcache; out0 = out f32 [16][n];
// u0 = pos0; u3 = sliding window (0 = global); f0 = query scale.
#include "models/common/kernel_args.h"
#include "models/common/vec.h"

#define MAX_SEQ 512
static float scores[MAX_SEQ];

static float dot(const float* x, const float* y, uint32_t len) {
  float out;
  __asm__ volatile(
      "vsetvli t0, %[n], e32, m8, ta, ma\n\t"
      "vmv.v.i v24, 0\n\t"
      "1:\n\t"
      "vsetvli t0, %[n], e32, m8, ta, ma\n\t"
      "vle32.v v8, (%[x])\n\t"
      "vle32.v v16, (%[y])\n\t"
      "vfmacc.vv v24, v8, v16\n\t"
      "slli t1, t0, 2\n\t"
      "add %[x], %[x], t1\n\t"
      "add %[y], %[y], t1\n\t"
      "sub %[n], %[n], t0\n\t"
      "bnez %[n], 1b\n\t"
      "vsetvli t0, %[len], e32, m8, ta, ma\n\t"
      "vfmv.s.f v8, %[zero]\n\t"
      "vfredusum.vs v8, v24, v8\n\t"
      "vfmv.f.s %[out], v8\n\t"
      : [out] "=f"(out), [x] "+r"(x), [y] "+r"(y), [n] "+r"(len)
      : [len] "r"(len), [zero] "f"(0.0f)
      : "t0", "t1", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15",
        "v16", "v17", "v18", "v19", "v20", "v21", "v22", "v23", "v24", "v25", "v26",
        "v27", "v28", "v29", "v30", "v31");
  return out;
}

static int run(KernelArgs* a) {
  const uint32_t m = a->m, heads = a->u1, hd = a->u2, stride = a->n;
  const uint32_t window = a->u3;
  if (m == 0 || m > MAX_TOKENS || heads == 0 || hd == 0) return 1;
  const float* kc = (const float*)a->in1;
  const float* vc = (const float*)a->in2;
  for (uint32_t i = 0; i < m; ++i) {
    const uint32_t pos = a->u0 + i;
    const uint32_t start = (window && pos + 1 > window) ? pos + 1 - window : 0;
    if (pos - start + 1 > MAX_SEQ) return 1;
    for (uint32_t h = 0; h < heads; ++h) {
      const float* q = (const float*)a->in0 + i * stride + h * hd;
      float* o = (float*)a->out0 + i * stride + h * hd;
      float mx = -3.0e38f;
      for (uint32_t s = start; s <= pos; ++s) {
        scores[s - start] = dot(q, kc + s * hd, hd) * a->f0;
        if (scores[s - start] > mx) mx = scores[s - start];
      }
      float denom = 0.0f;
      for (uint32_t s = start; s <= pos; ++s) {
        scores[s - start] = fast_exp(scores[s - start] - mx);
        denom += scores[s - start];
      }
      const float inv = 1.0f / denom;
      for (uint32_t j = 0; j < hd;) {
        uint32_t vl;
        __asm__ volatile(
            "vsetvli %[vl], %[rem], e32, m8, ta, ma\n\t"
            "vmv.v.i v24, 0\n\t"
            : [vl] "=r"(vl)
            : [rem] "r"(hd - j)
            : "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31");
        for (uint32_t s = start; s <= pos; ++s) {
          __asm__ volatile(
              "vle32.v v8, (%[v])\n\t"
              "vfmacc.vf v24, %[p], v8\n\t"
              :
              : [v] "r"(vc + s * hd + j), [p] "f"(scores[s - start] * inv)
              : "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15",
                "v24", "v25", "v26", "v27", "v28", "v29", "v30", "v31");
        }
        __asm__ volatile("vse32.v v24, (%[o])\n\t" : : [o] "r"(o + j) : "memory");
        j += vl;
      }
    }
  }
  return 0;
}

DEFINE_KERNEL(run)
