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
// Top-k over a logits row: out0 = {u32 idx[8], f32 val[8]} sorted descending.
// Args: n = vocab; in0 = logits f32 [n]; u0 = k (1..8).
#include "models/common/kernel_args.h"

#define CHUNK 32

static float chunk_max(const float* x) {
  float m;
  __asm__ volatile(
      "vsetvli t0, %[n], e32, m8, ta, ma\n\t"
      "vle32.v v8, (%[x])\n\t"
      "vfmv.s.f v16, %[lo]\n\t"
      "vfredmax.vs v16, v8, v16\n\t"
      "vfmv.f.s %[m], v16\n\t"
      : [m] "=f"(m)
      : [n] "r"(CHUNK), [x] "r"(x), [lo] "f"(-3.0e38f)
      : "t0", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15", "v16");
  return m;
}

static void insert(uint32_t* idx, float* val, uint32_t k, uint32_t i, float v) {
  if (v <= val[k - 1]) return;
  uint32_t j = k - 1;
  while (j > 0 && val[j - 1] < v) { val[j] = val[j - 1]; idx[j] = idx[j - 1]; --j; }
  val[j] = v;
  idx[j] = i;
}

// Pass 1: the top-k chunk maxima (32-wide vector reductions). Every element of
// the overall top-k lives in one of those chunks. Pass 2: exact scalar scan of
// just those chunks.
static int run(KernelArgs* a) {
  const uint32_t n = a->n, k = a->u0;
  if (n == 0 || n % CHUNK || k == 0 || k > 8) return 1;
  const float* x = (const float*)a->in0;
  uint32_t* idx = (uint32_t*)a->out0;
  float* val = (float*)a->out0 + 8;
  uint32_t cidx[8];
  float cval[8];
  for (uint32_t j = 0; j < 8; ++j) { cidx[j] = 0; cval[j] = -3.0e38f; idx[j] = 0; val[j] = -3.0e38f; }
  for (uint32_t c = 0; c < n / CHUNK; ++c) insert(cidx, cval, k, c, chunk_max(x + c * CHUNK));
  for (uint32_t j = 0; j < k; ++j) {
    const uint32_t base = cidx[j] * CHUNK;
    for (uint32_t i = 0; i < CHUNK; ++i) insert(idx, val, k, base + i, x[base + i]);
  }
  return 0;
}

DEFINE_KERNEL(run)
