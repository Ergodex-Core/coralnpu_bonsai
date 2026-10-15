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
// out[m][n] = sum_g sx[m][g] * ws[n][g] * (sum_{k in g} xq[m][k] * nib[n][k] - 8 * sum_{k in g} xq[m][k])
//
// Q4_0 int4 weights (nibble = code + 8), per-group f32 scales, int8
// activations with a per-token-per-group scale, on the Zvt matrix engine:
// tile0 += A(16 tokens x 4 k, signed int8) * B(4 k x 16 channels, nibbles).
// After each 32-wide group the tile is drained (one row per active token),
// converted to f32, offset-corrected, scaled and accumulated in DTCM.
//
// Args: m tokens (<=16), k, n; in0 = xq int8 k-major [k][16] (quant_act
// layout); in1 = sx f32 [16][k/32];
// in2 = xsum8 f32 [16][k/32] (8 * sum of the group's xq); u3 = row-major
// xq [16][k] (optional; used when m == 1); in3 = weight tiles
// (see kernel_args.h); out0 = out f32 rows of u0 floats (stride), u1 = tile
// offset (first output channel / 16, lets the host split large n across
// launches); u2 = scales base f32 [n/16][k/32][16]; flags bit0: accumulate
// into out instead of overwriting.
#include "models/common/kernel_args.h"
#include "models/common/zvt.h"

static float acc[MAX_TOKENS][TILE_N] __attribute__((aligned(16)));

// Decode staging (m == 1): the activation row and its per-group scales are
// read again for every 16-channel output tile, so they are copied to DTCM once
// per call. Only the weights and weight scales then stream from DRAM, which
// keeps them within the LSU read-prefetch bridge's streams (DBus2AxiStream).
#define MAX_STAGED_K 8192
static int8_t xq_staged[MAX_STAGED_K] __attribute__((aligned(16)));
static float sx_staged[MAX_STAGED_K / Q4_GROUP] __attribute__((aligned(16)));
static float xsum8_staged[MAX_STAGED_K / Q4_GROUP] __attribute__((aligned(16)));

// Copies n bytes (a multiple of 16) with up to 128-byte vector accesses.
static void vcopy(void* dst, const void* src, uint32_t n) {
  __asm__ volatile(
      "1:\n\t"
      "vsetvli t0, %[n], e8, m8, ta, ma\n\t"
      "vle8.v v8, (%[s])\n\t"
      "vse8.v v8, (%[d])\n\t"
      "add %[s], %[s], t0\n\t"
      "add %[d], %[d], t0\n\t"
      "sub %[n], %[n], t0\n\t"
      "bnez %[n], 1b\n\t"
      : [d] "+r"(dst), [s] "+r"(src), [n] "+r"(n)
      :
      : "t0", "memory", "v8", "v9", "v10", "v11", "v12", "v13", "v14", "v15");
}

#define MSETMTYPE ".insn r 0x57, 7, 0x41, x0, t5, t6\n\t"
#define MSETTM    ".insn r 0x57, 7, 0x42, t6, t5, x1\n\t"
#define MSETTN    ".insn r 0x57, 7, 0x42, t6, t5, x0\n\t"

// One Q4_0 group: configure, zero tile 0, 8 steps of 4 k each.
static inline __attribute__((always_inline)) void group_matmul(
    const int8_t* xq, const uint8_t* w, uint32_t tm) {
  __asm__ volatile(
      "li t5, %[mtype]\n\t"
      "li t6, %[vtype]\n\t"
      MSETMTYPE
      "mv t5, %[tm]\n\t"
      MSETTM
      "li t5, 16\n\t"
      MSETTN
      ".word %[vtzero]\n\t"
      "li t6, 8\n\t"
      "1:\n\t"
      // A rows j: xqT[k0 + j][0..15], 16 contiguous bytes each (k-major
      // layout written by quant_act), one bus transaction per row.
      "vle8.v v8, (%[xq])\n\t"
      "addi t4, %[xq], 16\n\t"
      "vle8.v v10, (t4)\n\t"
      "addi t4, %[xq], 32\n\t"
      "vle8.v v12, (t4)\n\t"
      "addi t4, %[xq], 48\n\t"
      "vle8.v v14, (t4)\n\t"
      // B rows: 32 packed bytes -> 4 nibble rows (unsigned 0..15).
      "vle8.v v24, (%[w])\n\t"
      "addi t4, %[w], 16\n\t"
      "vle8.v v25, (t4)\n\t"
      "vand.vi v16, v24, 15\n\t"
      "vsrl.vi v18, v24, 4\n\t"
      "vand.vi v20, v25, 15\n\t"
      "vsrl.vi v22, v25, 4\n\t"
      ".word %[vtmms]\n\t"
      "addi %[xq], %[xq], 64\n\t"
      "addi %[w], %[w], 32\n\t"
      "addi t6, t6, -1\n\t"
      "bnez t6, 1b\n\t"
      : [xq] "+r"(xq), [w] "+r"(w)
      : [tm] "r"(tm),
        [mtype] "i"(zvt_mtype(ZVT_TE, 4, 3)), [vtype] "i"(ZVT_VTYPE_SEW8_LMUL1),
        [vtzero] "i"(ZVT_VTZERO(0)), [vtmms] "i"(ZVT_VTMMS(0))
      : "t4", "t5", "t6", "memory", "v8", "v10", "v12", "v14", "v16", "v18",
        "v20", "v22", "v24", "v25");
}

// Decode fast path (tm == 1): only lane 0 of each A row matters, so the four
// rows of a step are the bytes xq1d[k0 .. k0+3] at lane 0. One 32-byte load
// covers four steps; rows come from register slides instead of bus reads.
static inline __attribute__((always_inline)) void group_matmul_1d(
    const int8_t* xq1d, const uint8_t* w) {
  __asm__ volatile(
      "li t5, %[mtype]\n\t"
      "li t6, %[vtype]\n\t"
      MSETMTYPE
      "li t5, 1\n\t"
      MSETTM
      "li t5, 16\n\t"
      MSETTN
      ".word %[vtzero]\n\t"
      "li t6, 2\n\t"
      "2:\n\t"
      "vle8.v v26, (%[xq])\n\t"          // 16 bytes: k0 .. k0+15 (16 k)
      "li t5, 4\n\t"
      "1:\n\t"
      "vmv.v.v v8, v26\n\t"               // row 0: lane 0 = xq[k]
      "vslidedown.vi v10, v26, 1\n\t"     // row 1: lane 0 = xq[k+1]
      "vslidedown.vi v12, v26, 2\n\t"
      "vslidedown.vi v14, v26, 3\n\t"
      "vle8.v v24, (%[w])\n\t"
      "addi t4, %[w], 16\n\t"
      "vle8.v v25, (t4)\n\t"
      "vand.vi v16, v24, 15\n\t"
      "vsrl.vi v18, v24, 4\n\t"
      "vand.vi v20, v25, 15\n\t"
      "vsrl.vi v22, v25, 4\n\t"
      ".word %[vtmms]\n\t"
      "vslidedown.vi v26, v26, 4\n\t"     // next 4 k
      "addi %[w], %[w], 32\n\t"
      "addi t5, t5, -1\n\t"
      "bnez t5, 1b\n\t"
      "addi %[xq], %[xq], 16\n\t"
      "addi t6, t6, -1\n\t"
      "bnez t6, 2b\n\t"
      : [xq] "+r"(xq1d), [w] "+r"(w)
      : [mtype] "i"(zvt_mtype(ZVT_TE, 4, 3)), [vtype] "i"(ZVT_VTYPE_SEW8_LMUL1),
        [vtzero] "i"(ZVT_VTZERO(0)), [vtmms] "i"(ZVT_VTMMS(0))
      : "t4", "t5", "t6", "memory", "v8", "v10", "v12", "v14", "v16", "v18",
        "v20", "v22", "v24", "v25", "v26");
}

// Drain row m of tile 0 and accumulate: acc[m] += (row - xsum8) * sx * ws.
static inline __attribute__((always_inline)) void drain_row(
    uint32_t m, const float* xsum8, const float* sx, const float* ws_g,
    float* acc_m) {
  __asm__ volatile(
      "mv a0, %[tss]\n\t"
      ".word %[vtmv]\n\t"
      "vfcvt.f.x.v v4, v4\n\t"
      "flw ft0, 0(%[xsum8])\n\t"
      "vfsub.vf v4, v4, ft0\n\t"
      "flw ft1, 0(%[sx])\n\t"
      "vfmul.vf v4, v4, ft1\n\t"
      "vle32.v v24, (%[ws])\n\t"
      "vle32.v v28, (%[acc])\n\t"
      "vfmacc.vv v28, v4, v24\n\t"
      "vse32.v v28, (%[acc])\n\t"
      :
      : [tss] "r"(ZVT_TSS(0, m)), [vtmv] "i"(ZVT_VTMV_V_T), [xsum8] "r"(xsum8),
        [sx] "r"(sx), [ws] "r"(ws_g), [acc] "r"(acc_m)
      : "a0", "ft0", "ft1", "memory", "v4", "v5", "v6", "v7", "v24", "v25",
        "v26", "v27", "v28", "v29", "v30", "v31");
}

static inline __attribute__((always_inline)) void config_acc(void) {
  __asm__ volatile(
      "li t5, %[mtype]\n\t"
      "li t6, %[vtype]\n\t"
      MSETMTYPE
      "li t5, 16\n\t"
      MSETTN
      :
      : [mtype] "i"(zvt_mtype(ZVT_TE, 1, 1)), [vtype] "i"(ZVT_VTYPE_SEW32_LMUL4)
      : "t5", "t6");
}

static int run(KernelArgs* a) {
  const uint32_t m = a->m, k = a->k, n = a->n;
  if (m == 0 || m > MAX_TOKENS || k % Q4_GROUP || n % TILE_N) return 1;
  const uint32_t groups = k / Q4_GROUP, tiles = n / TILE_N;
  const int8_t* xq = (const int8_t*)a->in0;
  const int8_t* xq1d = (const int8_t*)a->u3;  // row-major xq[0][k], decode path
  const float* sx = (const float*)a->in1;
  const float* xsum8 = (const float*)a->in2;
  const uint8_t* w = (const uint8_t*)a->in3;
  const float* ws = (const float*)a->u2;
  float* out = (float*)a->out0;
  const uint32_t out_stride = a->u0, tile0 = a->u1;
  const int accumulate = a->flags & 1;
  if (m == 1 && xq1d && k <= MAX_STAGED_K) {
    const uint32_t sbytes = (groups * 4 + 15) & ~15u;  // rows are [16][groups]
    vcopy(xq_staged, xq1d, k);
    vcopy(sx_staged, sx, sbytes);
    vcopy(xsum8_staged, xsum8, sbytes);
    xq1d = xq_staged;
    sx = sx_staged;
    xsum8 = xsum8_staged;
  }

  for (uint32_t t = 0; t < tiles; ++t) {
    for (uint32_t i = 0; i < m; ++i)
      for (uint32_t j = 0; j < TILE_N; ++j) acc[i][j] = 0.0f;
    const uint8_t* wt = w + (uint32_t)(tile0 + t) * TILE_BYTES(k);
    const float* wst = ws + (uint32_t)(tile0 + t) * groups * TILE_N;
    for (uint32_t g = 0; g < groups; ++g) {
      if (m == 1 && xq1d)
        group_matmul_1d(xq1d + g * Q4_GROUP, wt + g * TILE_GROUP_BYTES);
      else
        group_matmul(xq + g * Q4_GROUP * MAX_TOKENS, wt + g * TILE_GROUP_BYTES, m);
      config_acc();
      for (uint32_t i = 0; i < m; ++i)
        drain_row(i, xsum8 + i * groups + g, sx + i * groups + g,
                  wst + g * TILE_N, acc[i]);
    }
    for (uint32_t i = 0; i < m; ++i) {
      float* o = out + i * out_stride + (tile0 + t) * TILE_N;
      if (accumulate) {
        __asm__ volatile(
            "vle32.v v4, (%[acc])\n\t"
            "vle32.v v8, (%[o])\n\t"
            "vfadd.vv v4, v4, v8\n\t"
            "vse32.v v4, (%[o])\n\t"
            :
            : [acc] "r"(acc[i]), [o] "r"(o)
            : "memory", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11");
      } else {
        __asm__ volatile(
            "vle32.v v4, (%[acc])\n\t"
            "vse32.v v4, (%[o])\n\t"
            :
            : [acc] "r"(acc[i]), [o] "r"(o)
            : "memory", "v4", "v5", "v6", "v7");
      }
    }
  }
  return 0;
}

DEFINE_KERNEL(run)
