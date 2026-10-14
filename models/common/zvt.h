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
// Zvt (VME) matrix-engine helpers, VLEN=128: 16x16 tiles of 32-bit
// accumulators. Encodings follow tests/cocotb/vme_test/vme_test_utils.h and
// the compiler's CoralNPUMatrixCodegen.cpp.
//
// int8 matmul (vtmms.tvv): tile += A[16 x tk] * B[tk x 16], tk <= 4.
//   A row j (A[:, j]) in v8 + 2j, B row j (B[j, :]) in v16 + 2j, 16 int8 each.
//   A is signed; B is unsigned unless vtype.altfmt is set. We keep B unsigned
//   so raw Q4_0 nibbles (0..15) are used as is and the -8 offset is removed
//   with the activation sums afterwards.
// Tile rows move through v4..v7 (vtmv.v.t / vtmv.t.v) under SEW32/LMUL4.
#ifndef MODELS_COMMON_ZVT_H_
#define MODELS_COMMON_ZVT_H_

#include <stdint.h>

#define ZVT_TE 16

static inline uint32_t zvt_mtype(uint32_t tm, uint32_t tk, uint32_t mtwiden) {
  return ((tm & 0x3FFF) << 10) | ((tk & 0x7) << 5) | (mtwiden & 0x3);
}
#define ZVT_VTYPE_SEW8_LMUL1 0xC0u    // ta/ma, SEW8, LMUL1
#define ZVT_VTYPE_SEW32_LMUL4 0xD2u   // ta/ma, SEW32, LMUL4

// msetmtype rs1=mtype rs2=vtype (clears vl); msettn sets tn (and vl).
static inline __attribute__((always_inline)) void zvt_msetmtype(uint32_t mtype,
                                                                uint32_t vtype) {
  register uint32_t a0 __asm__("a0") = mtype;
  register uint32_t a1 __asm__("a1") = vtype;
  __asm__ volatile(".insn r 0b1010111, 0b111, 0b1000001, x0, a0, a1" : : "r"(a0), "r"(a1));
}
static inline __attribute__((always_inline)) void zvt_msettn(uint32_t tn) {
  register uint32_t a0 __asm__("a0") = tn;
  register uint32_t t0 __asm__("t0");
  __asm__ volatile(".insn r 0b1010111, 0b111, 0b1000010, t0, a0, x0" : "=r"(t0) : "r"(a0));
}
static inline __attribute__((always_inline)) void zvt_msettm(uint32_t tm) {
  register uint32_t a0 __asm__("a0") = tm;
  register uint32_t t0 __asm__("t0");
  __asm__ volatile(".insn r 0b1010111, 0b111, 0b1000010, t0, a0, x1" : "=r"(t0) : "r"(a0));
}

// Instruction words (funct6<<26 | vm=1<<25 | vs2<<20 | rs1<<15 | funct3<<12 | rd<<7 | op).
#define ZVT_WORD(f6, vs2, rs1, f3, rd, op) \
  (((f6) << 26) | (1u << 25) | ((vs2) << 20) | ((rs1) << 15) | ((f3) << 12) | ((rd) << 7) | (op))
#define ZVT_OP_V 0x57u
#define ZVT_OP_VE 0x77u
// vtmms.tvv mt<tile>, v8.., v16..  (signed A x unsigned B -> int32)
#define ZVT_VTMMS(tile) ZVT_WORD(0x3C, 8, 16, 0, ((tile) << 1) | 1, ZVT_OP_VE)
#define ZVT_VTZERO(tile) ZVT_WORD(0x10, 30, 0, 6, (tile) << 1, ZVT_OP_V)
#define ZVT_VTMV_V_T ZVT_WORD(0x10, 31, 10, 6, 4, ZVT_OP_V)  // v4..v7 <- tile row (tss in a0)
// Same move into an arbitrary destination register (e8: one 16-byte row).
#define ZVT_VTMV_V_T_RD(rd) ZVT_WORD(0x10, 31, 10, 6, (rd), ZVT_OP_V)
// Tile load (vtle8): .insn r 0x07, 7, 0x09, zero, rs1(addr), rs2(tss) loads
// 16 rows of 16 bytes from a contiguous 256-byte block (one bus burst).
#define ZVT_VTMV_T_V ZVT_WORD(0x17, 4, 10, 6, 0, ZVT_OP_V)   // tile row <- v4..v7

#define ZVT_TSS(tile, row) (((tile) << 27) | (row))

// Configure the 32-bit accumulator view (tile moves, vtzero, f32 math on
// 16-lane LMUL4 vectors) with vl = 16.
static inline __attribute__((always_inline)) void zvt_config_acc(void) {
  zvt_msetmtype(zvt_mtype(ZVT_TE, 1, 1), ZVT_VTYPE_SEW32_LMUL4);
  zvt_msettn(ZVT_TE);
}
// Configure the int8 matmul: tm active rows, tk = 4 k per instruction, vl = 16.
static inline __attribute__((always_inline)) void zvt_config_int8(uint32_t tm) {
  zvt_msetmtype(zvt_mtype(ZVT_TE, 4, 3), ZVT_VTYPE_SEW8_LMUL1);
  zvt_msettm(tm);
  zvt_msettn(ZVT_TE);
}
static inline __attribute__((always_inline)) void zvt_vtzero0(void) {
  __asm__ volatile(".word %0" : : "i"(ZVT_VTZERO(0)) : "memory");
}
static inline __attribute__((always_inline)) void zvt_vtmms0(void) {
  __asm__ volatile(".word %0" : : "i"(ZVT_VTMMS(0)) : "memory");
}
// v4..v7 <- row `row` of tile 0.
static inline __attribute__((always_inline)) void zvt_row_to_v4(uint32_t row) {
  register uint32_t a0 __asm__("a0") = ZVT_TSS(0, row);
  __asm__ volatile(".word %0" : : "i"(ZVT_VTMV_V_T), "r"(a0) : "v4", "v5", "v6", "v7", "memory");
}

#endif  // MODELS_COMMON_ZVT_H_
