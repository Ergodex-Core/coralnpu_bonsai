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
// Gated GELU: act[i][j] = gelu_tanh(gate[i][j]) * up[i][j], where the
// gating matvec wrote gate and up side by side: in0 = [16][2 * k] with gate
// in columns [0, k) and up in [k, 2k). Args: m, k; out0 = act f32 [16][k].
#include "models/common/kernel_args.h"
#include "models/common/vec.h"

static int run(KernelArgs* a) {
  const uint32_t m = a->m, k = a->k;
  if (m == 0 || m > MAX_TOKENS || k == 0) return 1;
  for (uint32_t i = 0; i < m; ++i) {
    const float* gate = (const float*)a->in0 + i * 2 * k;
    const float* up = gate + k;
    float* o = (float*)a->out0 + i * k;
    for (uint32_t j = 0; j < k; ++j) o[j] = gelu_tanh(gate[j]) * up[j];
  }
  return 0;
}

DEFINE_KERNEL(run)
