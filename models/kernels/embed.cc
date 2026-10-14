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
// Embedding gather from the tiled Q4_0 table (the same tiles the logits
// matvec streams): x[i][:] = dequant(row token[i]) * sqrt(k).
// Args: m tokens, k = embed_dim; in0 = tokens u32 [16]; in1 = tiles;
// in2 = scales f32 [V/16][k/32][16]; out0 = x f32 [16][k]; f0 = sqrt(k).
#include "models/common/kernel_args.h"

static int run(KernelArgs* a) {
  const uint32_t m = a->m, k = a->k;
  if (m == 0 || m > MAX_TOKENS || k % Q4_GROUP) return 1;
  const uint32_t groups = k / Q4_GROUP;
  const uint32_t* tokens = (const uint32_t*)a->in0;
  for (uint32_t i = 0; i < m; ++i) {
    const uint32_t tok = tokens[i], tile = tok / TILE_N, lane = tok % TILE_N;
    const uint8_t* wt = (const uint8_t*)a->in1 + tile * TILE_BYTES(k);
    const float* ws = (const float*)a->in2 + tile * groups * TILE_N;
    float* x = (float*)a->out0 + i * k;
    for (uint32_t g = 0; g < groups; ++g) {
      const float s = ws[g * TILE_N + lane] * a->f0;
      const uint8_t* wg = wt + g * TILE_GROUP_BYTES;
      for (uint32_t k4 = 0; k4 < 8; ++k4) {
        const uint8_t b0 = wg[k4 * 32 + lane], b1 = wg[k4 * 32 + 16 + lane];
        float* xo = x + g * Q4_GROUP + k4 * 4;
        xo[0] = (float)((int)(b0 & 15) - 8) * s;
        xo[1] = (float)((int)(b0 >> 4) - 8) * s;
        xo[2] = (float)((int)(b1 & 15) - 8) * s;
        xo[3] = (float)((int)(b1 >> 4) - 8) * s;
      }
    }
  }
  return 0;
}

DEFINE_KERNEL(run)
