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

static int run(KernelArgs* a) {
  const uint32_t n = a->n, k = a->u0;
  if (n == 0 || k == 0 || k > 8) return 1;
  const float* x = (const float*)a->in0;
  uint32_t* idx = (uint32_t*)a->out0;
  float* val = (float*)a->out0 + 8;
  for (uint32_t j = 0; j < 8; ++j) { idx[j] = 0; val[j] = -3.0e38f; }
  for (uint32_t i = 0; i < n; ++i) {
    const float v = x[i];
    if (v <= val[k - 1]) continue;
    uint32_t j = k - 1;
    while (j > 0 && val[j - 1] < v) { val[j] = val[j - 1]; idx[j] = idx[j - 1]; --j; }
    val[j] = v;
    idx[j] = i;
  }
  return 0;
}

DEFINE_KERNEL(run)
