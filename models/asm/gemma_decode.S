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
// Fused single-token Gemma 3 decode step for the CoralNPU F2 image, pure
// assembly. One launch per generated token: embed, all layers, final norm,
// logits with a fused top-8, no intermediate kernels and no logits buffer.
//
// Memory: weights (tiled Q4_0 nibbles, f16 group scales) in DDR; residual
// stream, q/kv/attention/MLP activations, KV cache and RoPE tables in HBM;
// int8 activations, their per-group scales and sums, attention scores and
// the top-8 list in DTCM.
//
// Argument block `dargs` (DTCM, written by the host, all u32 unless noted):
//   0  op          0 = full step; 1..8 = run one routine (unit tests)
//   4  layers      number of layers
//   8  dim         D (multiple of 32)
//  12  hidden      H (multiple of 32)
//  16  heads       nh
//  20  head_dim    hd (multiple of 64)
//  24  vocab       V (multiple of 16)
//  28  pos         position of this token
//  32  token       token id
//  36  emb_tiles   embedding tiles (DDR)
//  40  emb_scales  embedding f16 scales [V/16][D/32][16]
//  44  final_norm  f32 [D]
//  48  x           f32 [D] (HBM)
//  52  h           f32 [D]
//  56  xn          f32 [D]
//  60  q           f32 [nh*hd]
//  64  kv          f32 [2*hd]
//  68  attn        f32 [nh*hd]
//  72  ao          f32 [D]
//  76  gate_up     f32 [2*H]
//  80  act         f32 [H]
//  84  cos_local   f32 [S][hd/2]      88 sin_local
//  92  cos_global  f32 [S][hd/2]      96 sin_global
// 100  window      sliding window (local layers)
// 104  eps         f32
// 108  qscale      f32 head_dim^-0.5
// 112  embscale    f32 sqrt(D)
// 116  status      (written) 0x600D on success
// 120  cycles      (written)
// 124  topk_idx    u32 [8] (written)
// 156  topk_val    f32 [8] (written)
// 188  test_*      routine-test operands: 188 a, 192 b, 196 c, 200 d, 204 e,
//                  208 n, 212 k, 216 f (f32)
// 224  layers[]    per layer, 22 words:
//        0 q_tiles 1 q_scales 2 kv_tiles 3 kv_scales 4 o_tiles 5 o_scales
//        6 gu_tiles 7 gu_scales 8 dn_tiles 9 dn_scales 10 pre_attn
//        11 post_attn 12 pre_ffw 13 post_ffw 14 q_norm 15 k_norm
//        16 kcache 17 vcache 18 local(1/0) 19..21 reserved
//
// Register conventions inside routines: a0-a7 arguments, t0-t6 scratch,
// s0-s11 callee-saved loop state. Vector registers are scratch everywhere.

#define DARGS_LAYERS_OFF 224
#define LAYER_WORDS 22
#define GROUP 32
#define TILE 16
#define TILE_GROUP_BYTES 256

// mtype/vtype values (see models/common/zvt.h).
#define MTYPE_INT8 ((16 << 10) | (4 << 5) | 3)
#define MTYPE_ACC  ((16 << 10) | (1 << 5) | 1)
#define VTYPE_E8M1  0xC0
#define VTYPE_E32M4 0xD2
#define VTZERO0   0x43EF6057   // vtzero mt0        (ZVT_WORD 0x10,30,0,6,0)
#define VTMMS0    0xF2880077   // vtmms.tvv mt0, v8.., v16.. signed A (rd=1)
#define VTMV_V_T  0x43F56257   // vtmv.v.t v4.., a0  (tss in a0)

    .text
    .globl main
main:
    addi sp, sp, -64
    sw   ra, 60(sp)
    sw   s0, 56(sp)
    sw   s1, 52(sp)
    sw   s2, 48(sp)
    sw   s3, 44(sp)
    sw   s4, 40(sp)
    sw   s5, 36(sp)
    sw   s6, 32(sp)
    sw   s7, 28(sp)
    sw   s8, 24(sp)
    sw   s9, 20(sp)
    sw   s10, 16(sp)
    sw   s11, 12(sp)
    csrr s11, mcycle
    la   s0, dargs
    lw   t0, 0(s0)             // op
    beqz t0, .Lfull_step
    la   t1, .Lop_table
    slli t0, t0, 2
    add  t1, t1, t0
    lw   t1, 0(t1)
    jalr ra, t1, 0
    j    .Lok
.Lop_table:
    .word .Lbad, test_rmsnorm, test_quant, test_matvec, test_rope, test_attention
    .word test_gelu, test_embed, test_topk
.Lbad:
    li   a0, 1
    j    .Lexit

// ---------------------------------------------------------------------------
// Full step.
// ---------------------------------------------------------------------------
.Lfull_step:
    // embed: x = dequant(row token) * sqrt(D)
    lw   a0, 32(s0)            // token
    lw   a1, 36(s0)            // emb tiles
    lw   a2, 40(s0)            // emb scales (f16)
    lw   a3, 48(s0)            // x
    lw   a4, 8(s0)             // D
    flw  fa0, 112(s0)          // sqrt(D)
    jal  ra, embed_row
    li   s1, 0                 // layer index
.Llayer_loop:
    lw   t0, 4(s0)
    bgeu s1, t0, .Llayers_done
    li   t1, LAYER_WORDS * 4
    mul  t1, s1, t1
    addi s2, s0, DARGS_LAYERS_OFF
    add  s2, s2, t1            // s2 -> layer record
    // --- attention block ---
    lw   a0, 48(s0)            // x
    lw   a1, 40(s2)            // pre_attn scale
    lw   a2, 56(s0)            // xn
    lw   a3, 8(s0)             // D
    li   a4, 0                 // no residual
    jal  ra, rmsnorm
    lw   a0, 56(s0)            // xn
    lw   a1, 8(s0)             // D
    jal  ra, quant_act         // -> DTCM xq/sx/xsum8
    lw   a0, 0(s2)             // q tiles
    lw   a1, 4(s2)             // q scales
    lw   a2, 60(s0)            // q out
    lw   a3, 8(s0)             // K = D
    lw   a4, 16(s0)
    lw   t0, 20(s0)
    mul  a4, a4, t0            // N = nh*hd
    jal  ra, matvec
    lw   a0, 8(s2)             // kv tiles
    lw   a1, 12(s2)
    lw   a2, 64(s0)            // kv out
    lw   a3, 8(s0)
    lw   a4, 20(s0)
    slli a4, a4, 1             // N = 2*hd
    jal  ra, matvec
    // q norm per head, k norm
    lw   s3, 16(s0)            // nh
    lw   s4, 20(s0)            // hd
    lw   s5, 60(s0)            // q
    li   s6, 0
1:  bgeu s6, s3, 2f
    mv   a0, s5
    lw   a1, 56(s2)            // q_norm scale
    mv   a2, s5
    mv   a3, s4
    li   a4, 0
    jal  ra, rmsnorm
    slli t0, s4, 2
    add  s5, s5, t0
    addi s6, s6, 1
    j    1b
2:  lw   a0, 64(s0)            // kv (k part)
    lw   a1, 60(s2)            // k_norm
    lw   a2, 64(s0)
    mv   a3, s4
    li   a4, 0
    jal  ra, rmsnorm
    // rope tables for this layer
    lw   t0, 72(s2)            // local?
    lw   s7, 92(s0)            // cos_global
    lw   s8, 96(s0)
    beqz t0, 3f
    lw   s7, 84(s0)
    lw   s8, 88(s0)
3:  lw   t1, 28(s0)            // pos
    srli t2, s4, 1             // hd/2
    mul  t1, t1, t2
    slli t1, t1, 2
    add  s7, s7, t1            // cos row for pos
    add  s8, s8, t1
    // rope q heads
    lw   s5, 60(s0)
    li   s6, 0
4:  bgeu s6, s3, 5f
    mv   a0, s5
    mv   a1, s7
    mv   a2, s8
    mv   a3, s4
    jal  ra, rope_head
    slli t0, s4, 2
    add  s5, s5, t0
    addi s6, s6, 1
    j    4b
5:  lw   a0, 64(s0)            // k
    mv   a1, s7
    mv   a2, s8
    mv   a3, s4
    jal  ra, rope_head
    // cache writes: kcache[pos] = k, vcache[pos] = v
    lw   t0, 28(s0)
    mul  t0, t0, s4
    slli t0, t0, 2             // pos*hd*4
    lw   a0, 64(s2)            // kcache
    add  a0, a0, t0
    lw   a1, 64(s0)            // k
    mv   a2, s4
    jal  ra, copy_f32
    lw   t0, 28(s0)
    mul  t0, t0, s4
    slli t0, t0, 2
    lw   a0, 68(s2)            // vcache
    add  a0, a0, t0
    lw   a1, 64(s0)
    slli t1, s4, 2
    add  a1, a1, t1            // v
    mv   a2, s4
    jal  ra, copy_f32
    // attention
    lw   a0, 60(s0)            // q
    lw   a1, 64(s2)            // kcache
    lw   a2, 68(s2)            // vcache
    lw   a3, 68(s0)            // attn out
    lw   a4, 28(s0)            // pos
    lw   a5, 100(s0)           // window
    lw   t0, 72(s2)
    bnez t0, 6f
    li   a5, 0
6:  jal  ra, attention
    // o proj
    lw   a0, 68(s0)
    lw   a1, 16(s0)
    mul  a1, a1, s4            // nh*hd
    jal  ra, quant_act
    lw   a0, 16(s2)
    lw   a1, 20(s2)
    lw   a2, 72(s0)            // ao
    lw   a3, 16(s0)
    mul  a3, a3, s4            // K = nh*hd
    lw   a4, 8(s0)             // N = D
    jal  ra, matvec
    lw   a0, 72(s0)            // ao
    lw   a1, 44(s2)            // post_attn
    lw   a2, 52(s0)            // h
    lw   a3, 8(s0)
    lw   a4, 48(s0)            // residual x
    jal  ra, rmsnorm
    // --- MLP block ---
    lw   a0, 52(s0)
    lw   a1, 48(s2)            // pre_ffw
    lw   a2, 56(s0)            // xn
    lw   a3, 8(s0)
    li   a4, 0
    jal  ra, rmsnorm
    lw   a0, 56(s0)
    lw   a1, 8(s0)
    jal  ra, quant_act
    lw   a0, 24(s2)            // gu tiles
    lw   a1, 28(s2)
    lw   a2, 76(s0)            // gate_up
    lw   a3, 8(s0)
    lw   a4, 12(s0)
    slli a4, a4, 1             // N = 2H
    jal  ra, matvec
    lw   a0, 76(s0)            // gate_up
    lw   a1, 80(s0)            // act
    lw   a2, 12(s0)            // H
    jal  ra, gelu_mul
    lw   a0, 80(s0)
    lw   a1, 12(s0)
    jal  ra, quant_act
    lw   a0, 32(s2)            // down tiles
    lw   a1, 36(s2)
    lw   a2, 72(s0)            // ao
    lw   a3, 12(s0)            // K = H
    lw   a4, 8(s0)             // N = D
    jal  ra, matvec
    lw   a0, 72(s0)
    lw   a1, 52(s2)            // post_ffw
    lw   a2, 48(s0)            // x
    lw   a3, 8(s0)
    lw   a4, 52(s0)            // residual h
    jal  ra, rmsnorm
    addi s1, s1, 1
    j    .Llayer_loop
.Llayers_done:
    lw   a0, 48(s0)
    lw   a1, 44(s0)            // final norm
    lw   a2, 56(s0)
    lw   a3, 8(s0)
    li   a4, 0
    jal  ra, rmsnorm
    lw   a0, 56(s0)
    lw   a1, 8(s0)
    jal  ra, quant_act
    jal  ra, topk_init
    lw   a0, 36(s0)            // emb tiles
    lw   a1, 40(s0)            // emb scales
    li   a2, 0                 // out = 0: fused top-k drain
    lw   a3, 8(s0)             // K = D
    lw   a4, 24(s0)            // N = V
    jal  ra, matvec
.Lok:
    li   a0, 0
.Lexit:
    la   s0, dargs
    csrr t0, mcycle
    sub  t0, t0, s11
    sw   t0, 120(s0)
    li   t1, 0x600D
    bnez a0, 1f
    sw   t1, 116(s0)
    j    2f
1:  li   t1, 0xBAD0
    sw   t1, 116(s0)
2:  lw   ra, 60(sp)
    lw   s0, 56(sp)
    lw   s1, 52(sp)
    lw   s2, 48(sp)
    lw   s3, 44(sp)
    lw   s4, 40(sp)
    lw   s5, 36(sp)
    lw   s6, 32(sp)
    lw   s7, 28(sp)
    lw   s8, 24(sp)
    lw   s9, 20(sp)
    lw   s10, 16(sp)
    lw   s11, 12(sp)
    addi sp, sp, 64
    ret

// ---------------------------------------------------------------------------
// copy_f32(a0 dst, a1 src, a2 count)
// ---------------------------------------------------------------------------
copy_f32:
1:  vsetvli t0, a2, e32, m8, ta, ma
    vle32.v v8, (a1)
    vse32.v v8, (a0)
    slli t1, t0, 2
    add  a0, a0, t1
    add  a1, a1, t1
    sub  a2, a2, t0
    bnez a2, 1b
    ret

// ---------------------------------------------------------------------------
// rmsnorm(a0 x, a1 scale, a2 out, a3 n, a4 residual or 0)
//   out = x * rsqrt(mean(x^2) + eps) * (1 + scale) [+ residual]
// ---------------------------------------------------------------------------
rmsnorm:
    mv   t3, a0
    mv   t4, a3
    fmv.w.x ft2, zero          // ss
1:  vsetvli t0, t4, e32, m8, ta, ma
    vle32.v v8, (t3)
    vfmul.vv v8, v8, v8
    vfmv.s.f v16, ft2
    vfredusum.vs v16, v8, v16
    vfmv.f.s ft2, v16
    slli t1, t0, 2
    add  t3, t3, t1
    sub  t4, t4, t0
    bnez t4, 1b
    fcvt.s.wu ft3, a3
    fdiv.s ft2, ft2, ft3       // mean
    la   t0, dargs
    flw  ft3, 104(t0)          // eps
    fadd.s ft2, ft2, ft3
    fsqrt.s ft2, ft2
    li   t0, 0x3f800000
    fmv.w.x ft3, t0            // 1.0
    fdiv.s ft2, ft3, ft2       // inv
2:  vsetvli t0, a3, e32, m8, ta, ma
    vle32.v v8, (a0)
    vle32.v v16, (a1)
    vfmul.vf v8, v8, ft2
    vfadd.vf v16, v16, ft3
    beqz a4, 3f
    vle32.v v24, (a4)
    vfmacc.vv v24, v8, v16
    vse32.v v24, (a2)
    slli t1, t0, 2
    add  a4, a4, t1
    j    4f
3:  vfmul.vv v8, v8, v16
    vse32.v v8, (a2)
    slli t1, t0, 2
4:  add  a0, a0, t1
    add  a1, a1, t1
    add  a2, a2, t1
    sub  a3, a3, t0
    bnez a3, 2b
    ret

// ---------------------------------------------------------------------------
// quant_act(a0 x f32, a1 k): per group of 32: sx = max|x|/127,
//   xq = round(x/sx) -> DTCM xq1d[k], sx -> DTCM sxbuf[g], 8*sum -> xsum8[g]
// ---------------------------------------------------------------------------
quant_act:
    la   t3, xq1d
    la   t4, sxbuf
    la   t5, xsum8
    li   t6, GROUP
    vsetvli t0, t6, e32, m8, ta, ma
1:  vle32.v v8, (a0)
    vfsgnjx.vv v16, v8, v8
    fmv.w.x ft0, zero
    vfmv.s.f v24, ft0
    vfredmax.vs v24, v16, v24
    vfmv.f.s ft0, v24          // amax
    li   t0, 0x2b8cbccc        // 1e-12
    fmv.w.x ft1, t0
    fmax.s ft0, ft0, ft1
    li   t0, 0x42fe0000        // 127.0
    fmv.w.x ft1, t0
    fdiv.s ft2, ft0, ft1       // sx
    li   t0, 0x3f800000
    fmv.w.x ft1, t0
    fdiv.s ft3, ft1, ft2       // inv
    vfmul.vf v8, v8, ft3
    vfcvt.x.f.v v8, v8         // round to nearest even
    vmv.s.x v24, zero
    vredsum.vs v24, v8, v24
    vmv.x.s t0, v24
    slli t0, t0, 3             // 8 * sum
    fcvt.s.w ft4, t0
    fsw  ft2, 0(t4)
    fsw  ft4, 0(t5)
    vsetvli t0, t6, e16, m4, ta, ma
    vnclip.wi v16, v8, 0
    vsetvli t0, t6, e8, m2, ta, ma
    vnclip.wi v20, v16, 0
    vse8.v v20, (t3)
    vsetvli t0, t6, e32, m8, ta, ma
    addi a0, a0, GROUP * 4
    addi t3, t3, GROUP
    addi t4, t4, 4
    addi t5, t5, 4
    addi a1, a1, -GROUP
    bnez a1, 1b
    ret

// ---------------------------------------------------------------------------
// matvec(a0 tiles, a1 f16 scales [N/16][K/32][16], a2 out f32 [N] or 0 for
//        the fused top-k drain, a3 K, a4 N)
//   out[n] = sum_g sx[g] * ws[n][g] * (sum_k xq[k] nib[n][k] - xsum8[g])
//   Activations come from DTCM (xq1d, sxbuf, xsum8).
// ---------------------------------------------------------------------------
matvec:
    addi sp, sp, -16
    sw   ra, 12(sp)
    srli s3, a3, 5             // groups
    srli s4, a4, 4             // tiles
    mv   s5, a0                // tile pointer
    mv   s6, a1                // scale pointer
    mv   s7, a2                // out pointer (or 0)
    li   s8, 0                 // tile index
.Lmv_tile:
    // acc (16 f32 in DTCM) = 0
    la   t0, accbuf
    li   t1, 16
    vsetvli t2, t1, e32, m4, ta, ma
    vmv.v.i v28, 0
    vse32.v v28, (t0)
    la   s9, xq1d              // A pointer
    la   s10, sxbuf
    la   a7, xsum8
    mv   t3, s3                // groups left
.Lmv_group:
    // ---- int8 group matmul (8 steps of 4 k), A from DTCM ----
    li   t5, MTYPE_INT8
    li   t6, VTYPE_E8M1
    .insn r 0x57, 7, 0x41, x0, t5, t6     // msetmtype
    li   t5, 1
    .insn r 0x57, 7, 0x42, t6, t5, x1     // msettm 1
    li   t5, 16
    .insn r 0x57, 7, 0x42, t6, t5, x0     // msettn 16 (vl=16)
    .word VTZERO0
    li   t6, 2
2:  vle8.v v26, (s9)                      // 16 k of activations
    li   t5, 4
1:  vmv.v.v v8, v26
    vslidedown.vi v10, v26, 1
    vslidedown.vi v12, v26, 2
    vslidedown.vi v14, v26, 3
    vle8.v v24, (s5)
    addi t4, s5, 16
    vle8.v v25, (t4)
    vand.vi v16, v24, 15
    vsrl.vi v18, v24, 4
    vand.vi v20, v25, 15
    vsrl.vi v22, v25, 4
    .word VTMMS0
    vslidedown.vi v26, v26, 4
    addi s5, s5, 32
    addi t5, t5, -1
    bnez t5, 1b
    addi s9, s9, 16
    addi t6, t6, -1
    bnez t6, 2b
    // ---- f16 group scales -> f32 in v24..v27 (16 lanes) ----
    li   t1, 16
    vsetvli t2, t1, e32, m4, ta, ma
    vle16.v v20, (s6)                     // e16 m2 holds 16 halves (vl=16)
    vzext.vf2 v24, v20                    // u32 bits of the halves
    vsrl.vi v28, v24, 10
    li   t4, 31
    vand.vx v28, v28, t4                  // exponent
    li   t4, 0x3ff
    vand.vx v16, v24, t4                  // mantissa
    vsll.vi v16, v16, 13
    vadd.vx v28, v28, zero
    li   t4, 112
    vadd.vx v28, v28, t4
    vsll.vi v28, v28, 23
    vor.vv v16, v16, v28
    li   t4, 0x8000
    vand.vx v28, v24, t4
    vsll.vi v28, v28, 16
    vor.vv v16, v16, v28                  // f32 bits (exp != 0)
    li   t4, 0x7c00
    vand.vx v28, v24, t4
    vmseq.vi v0, v28, 0                   // exponent zero -> 0.0
    vmv.v.i v24, 0
    vmerge.vvm v24, v16, v24, v0          // v24 = exp==0 ? 0 : f32
    addi s6, s6, 32
    // ---- drain row 0, scale, accumulate into DTCM acc ----
    li   t5, MTYPE_ACC
    li   t6, VTYPE_E32M4
    .insn r 0x57, 7, 0x41, x0, t5, t6     // msetmtype (SEW32 LMUL4)
    li   t5, 16
    .insn r 0x57, 7, 0x42, t6, t5, x0     // msettn 16
    li   a0, 0                            // tss: tile 0 row 0
    .word VTMV_V_T                        // v4..v7 <- row 0
    vfcvt.f.x.v v4, v4
    flw  ft0, 0(a7)                       // xsum8[g]
    vfsub.vf v4, v4, ft0
    flw  ft1, 0(s10)                      // sx[g]
    vfmul.vf v4, v4, ft1
    la   t0, accbuf
    vle32.v v28, (t0)
    vfmacc.vv v28, v4, v24
    vse32.v v28, (t0)
    addi s10, s10, 4
    addi a7, a7, 4
    addi t3, t3, -1
    bnez t3, .Lmv_group
    // ---- tile done: write 16 outputs or feed the fused top-k ----
    la   t0, accbuf
    beqz s7, 3f
    li   t1, 16
    vsetvli t2, t1, e32, m4, ta, ma
    vle32.v v28, (t0)
    vse32.v v28, (s7)
    addi s7, s7, 64
    j    4f
3:  slli a0, s8, 4                        // base index = tile*16
    jal  ra, topk_tile
4:  addi s8, s8, 1
    bne  s8, s4, .Lmv_tile
    lw   ra, 12(sp)
    addi sp, sp, 16
    ret

// ---------------------------------------------------------------------------
// rope_head(a0 x [hd], a1 cos row [hd/2], a2 sin row, a3 hd): rotate-half.
// ---------------------------------------------------------------------------
rope_head:
    srli t3, a3, 1             // half
    slli t4, t3, 2
    add  t4, a0, t4            // x2
1:  vsetvli t0, t3, e32, m4, ta, ma
    vle32.v v8, (a0)
    vle32.v v12, (t4)
    vle32.v v16, (a1)
    vle32.v v20, (a2)
    vfmul.vv v24, v8, v16
    vfnmsac.vv v24, v12, v20
    vfmul.vv v28, v12, v16
    vfmacc.vv v28, v8, v20
    vse32.v v24, (a0)
    vse32.v v28, (t4)
    slli t1, t0, 2
    add  a0, a0, t1
    add  t4, t4, t1
    add  a1, a1, t1
    add  a2, a2, t1
    sub  t3, t3, t0
    bnez t3, 1b
    ret

// ---------------------------------------------------------------------------
// vexp: v8 = exp(v8) under the current e32 vtype (clamped to [-87, 88]).
//   Clobbers v16, v24, ft0-ft7, t0.
// ---------------------------------------------------------------------------
vexp:
    li   t0, 0x42b00000        // 88.0
    fmv.w.x ft0, t0
    vfmin.vf v8, v8, ft0
    li   t0, 0xc2ae0000        // -87.0
    fmv.w.x ft0, t0
    vfmax.vf v8, v8, ft0
    li   t0, 0x3fb8aa3b        // log2(e)
    fmv.w.x ft0, t0
    vfmul.vf v16, v8, ft0
    vfcvt.x.f.v v16, v16       // n = round(x*log2e)
    vfcvt.f.x.v v24, v16       // n as float
    li   t0, 0x3f317200        // ln2 hi
    fmv.w.x ft0, t0
    vfnmsac.vf v8, ft0, v24    // r = x - n*ln2hi
    li   t0, 0x35bfbe8e        // ln2 lo
    fmv.w.x ft0, t0
    vfnmsac.vf v8, ft0, v24
    // p = ((((( r/720 + 1/120) r + 1/24) r + 1/6) r + 1/2) r + 1) r + 1
    li   t0, 0x3ab60b61        // 1/720
    fmv.w.x ft1, t0
    li   t0, 0x3c088889        // 1/120
    fmv.w.x ft2, t0
    li   t0, 0x3d2aaaab        // 1/24
    fmv.w.x ft3, t0
    li   t0, 0x3e2aaaab        // 1/6
    fmv.w.x ft4, t0
    li   t0, 0x3f000000        // 0.5
    fmv.w.x ft5, t0
    li   t0, 0x3f800000        // 1.0
    fmv.w.x ft6, t0
    vfmv.v.f v24, ft1          // p = 1/720
    vfmul.vv v24, v24, v8
    vfadd.vf v24, v24, ft2
    vfmul.vv v24, v24, v8
    vfadd.vf v24, v24, ft3
    vfmul.vv v24, v24, v8
    vfadd.vf v24, v24, ft4
    vfmul.vv v24, v24, v8
    vfadd.vf v24, v24, ft5
    vfmul.vv v24, v24, v8
    vfadd.vf v24, v24, ft6
    vfmul.vv v24, v24, v8
    vfadd.vf v24, v24, ft6     // p
    li   t0, 127
    vadd.vx v16, v16, t0
    vsll.vi v16, v16, 23       // 2^n bits
    vfmul.vv v8, v24, v16      // reinterpret v16 as f32 (same bits)
    ret

// ---------------------------------------------------------------------------
// gelu_mul(a0 gate_up [2H], a1 act [H], a2 H): act = gelu_tanh(gate) * up
//   tanh(z) = 1 - 2/(1+exp(2z)); z = c*(x + 0.044715 x^3)
// ---------------------------------------------------------------------------
gelu_mul:
    addi sp, sp, -16
    sw   ra, 12(sp)
    slli t3, a2, 2
    add  t3, a0, t3            // up pointer
1:  vsetvli t4, a2, e32, m8, ta, ma
    vle32.v v0, (a0)           // x  (v0..v7)
    vfmul.vv v8, v0, v0        // x^2
    vfmul.vv v8, v8, v0        // x^3
    li   t0, 0x3d372713        // 0.044715
    fmv.w.x ft0, t0
    vfmacc.vf v0, ft0, v8      // v0 = x + 0.044715 x^3  (keep x in v24? we need x later)
    // recover x later from gate_up again (reload), keep inner in v0
    li   t0, 0x3f4c422a        // sqrt(2/pi)
    fmv.w.x ft0, t0
    vfmul.vf v8, v0, ft0       // z
    vfadd.vv v8, v8, v8        // 2z
    mv   t5, a0
    mv   t6, a2
    sw   t5, 8(sp)
    sw   t6, 4(sp)
    sw   t4, 0(sp)
    jal  ra, vexp              // v8 = exp(2z)
    lw   t4, 0(sp)
    lw   t6, 4(sp)
    lw   t5, 8(sp)
    li   t0, 0x3f800000
    fmv.w.x ft6, t0            // 1.0
    vfadd.vf v8, v8, ft6       // 1+e
    vfmv.v.f v16, ft6
    vfadd.vv v16, v16, v16     // 2.0
    vfdiv.vv v8, v16, v8       // 2/(1+e)
    vfrsub.vf v8, v8, ft6      // tanh = 1 - 2/(1+e)
    vfadd.vf v8, v8, ft6       // 1 + tanh
    vle32.v v0, (t5)           // x again
    vfmul.vv v8, v8, v0
    li   t0, 0x3f000000
    fmv.w.x ft0, t0
    vfmul.vf v8, v8, ft0       // gelu
    vle32.v v16, (t3)          // up
    vfmul.vv v8, v8, v16
    vse32.v v8, (a1)
    slli t1, t4, 2
    add  a0, t5, t1
    add  t3, t3, t1
    add  a1, a1, t1
    sub  a2, t6, t4
    bnez a2, 1b
    lw   ra, 12(sp)
    addi sp, sp, 16
    ret

// ---------------------------------------------------------------------------
// attention(a0 q [nh*hd], a1 kcache [S][hd], a2 vcache, a3 out [nh*hd],
//           a4 pos, a5 window): per head, causal softmax over [start, pos].
// ---------------------------------------------------------------------------
attention:
    addi sp, sp, -32
    sw   ra, 28(sp)
    la   t0, dargs
    lw   s3, 16(t0)            // nh
    lw   s4, 20(t0)            // hd
    flw  fs0, 108(t0)          // q scale
    // start
    li   s5, 0
    beqz a5, 1f
    addi t1, a4, 1
    bltu t1, a5, 1f
    sub  s5, t1, a5
1:  mv   s6, a0                // q head pointer
    mv   s7, a3                // out head pointer
    li   s8, 0                 // head index
    mv   s9, a4                // pos
    mv   s10, a5
    sw   a1, 24(sp)
    sw   a2, 20(sp)
.Lhead:
    bgeu s8, s3, .Lattn_done
    // scores[s - start] = dot(q, K[s]) * scale ; track max in fs1
    li   t0, 0xff7fffff
    fmv.w.x fs1, t0            // -FLT_MAX
    la   a7, scores
    mv   t3, s5                // s
2:  bgtu t3, s9, 3f
    lw   t4, 24(sp)
    mul  t5, t3, s4
    slli t5, t5, 2
    add  t4, t4, t5            // K[s]
    mv   a0, s6
    mv   a1, t4
    mv   a2, s4
    jal  ra, dot_f32           // fa0
    fmul.s fa0, fa0, fs0
    fsw  fa0, 0(a7)
    fmax.s fs1, fs1, fa0
    addi a7, a7, 4
    addi t3, t3, 1
    j    2b
3:  // softmax: p = exp(s - max); denom
    la   a7, scores
    sub  t3, s9, s5
    addi t3, t3, 1             // n = pos - start + 1
    fmv.w.x fs2, zero          // denom
    mv   t6, t3
    mv   t5, a7
4:  vsetvli t0, t6, e32, m8, ta, ma
    vle32.v v8, (t5)
    vfsub.vf v8, v8, fs1
    sw   t5, 16(sp)
    sw   t6, 12(sp)
    sw   t3, 8(sp)
    jal  ra, vexp
    lw   t3, 8(sp)
    lw   t6, 12(sp)
    lw   t5, 16(sp)
    vsetvli t0, t6, e32, m8, ta, ma
    vse32.v v8, (t5)
    vfmv.s.f v16, fs2
    vfredusum.vs v16, v8, v16
    vfmv.f.s fs2, v16
    slli t1, t0, 2
    add  t5, t5, t1
    sub  t6, t6, t0
    bnez t6, 4b
    li   t0, 0x3f800000
    fmv.w.x ft0, t0
    fdiv.s fs2, ft0, fs2       // 1/denom
    // out[j] = sum_s p[s] V[s][j], chunks of 32 over hd
    li   t6, 0                 // j
5:  bgeu t6, s4, .Lnext_head
    sub  t0, s4, t6
    vsetvli t4, t0, e32, m8, ta, ma
    vmv.v.i v24, 0
    la   a7, scores
    mv   t3, s5
6:  bgtu t3, s9, 7f
    lw   t5, 20(sp)
    mul  t1, t3, s4
    add  t1, t1, t6
    slli t1, t1, 2
    add  t5, t5, t1            // V[s][j]
    vle32.v v8, (t5)
    flw  ft0, 0(a7)
    fmul.s ft0, ft0, fs2
    vfmacc.vf v24, ft0, v8
    addi a7, a7, 4
    addi t3, t3, 1
    j    6b
7:  slli t1, t6, 2
    add  t1, s7, t1
    vse32.v v24, (t1)
    add  t6, t6, t4
    j    5b
.Lnext_head:
    slli t0, s4, 2
    add  s6, s6, t0
    add  s7, s7, t0
    addi s8, s8, 1
    j    .Lhead
.Lattn_done:
    lw   ra, 28(sp)
    addi sp, sp, 32
    ret

// dot_f32(a0 x, a1 y, a2 n) -> fa0
dot_f32:
    li   t0, 32
    vsetvli t0, t0, e32, m8, ta, ma
    vmv.v.i v24, 0
1:  vsetvli t0, a2, e32, m8, ta, ma
    vle32.v v8, (a0)
    vle32.v v16, (a1)
    vfmacc.vv v24, v8, v16
    slli t1, t0, 2
    add  a0, a0, t1
    add  a1, a1, t1
    sub  a2, a2, t0
    bnez a2, 1b
    li   t0, 32
    vsetvli t0, t0, e32, m8, ta, ma
    fmv.w.x ft0, zero
    vfmv.s.f v8, ft0
    vfredusum.vs v8, v24, v8
    vfmv.f.s fa0, v8
    ret

// ---------------------------------------------------------------------------
// embed_row(a0 token, a1 tiles, a2 f16 scales, a3 x out, a4 D, fa0 sqrt(D))
// ---------------------------------------------------------------------------
embed_row:
    srli t0, a0, 4             // tile
    andi t1, a0, 15            // lane
    srli t2, a4, 5             // groups
    slli t3, t2, 8             // tile bytes = groups*256
    mul  t3, t0, t3
    add  a1, a1, t3            // tile base
    slli t3, t2, 5             // groups*16*2 bytes of f16 scales per tile
    mul  t3, t0, t3
    add  a2, a2, t3
    slli t4, t1, 1
    add  a2, a2, t4            // &scale[tile][0][lane] (f16)
    li   t5, 0                 // g
1:  bgeu t5, t2, 9f
    lhu  t6, 0(a2)             // f16 bits
    jal  t0, f16_to_f32_scalar // ft0 = value (uses t0 as link? no: use ra-free helper via inline)
    fmul.s ft0, ft0, fa0       // scale * sqrt(D)
    li   t3, 0                 // k4
2:  slli t4, t3, 5
    add  t4, a1, t4            // wg + k4*32
    add  t4, t4, t1
    lbu  a5, 0(t4)             // b0
    lbu  a6, 16(t4)            // b1
    andi a7, a5, 15
    addi a7, a7, -8
    fcvt.s.w ft1, a7
    fmul.s ft1, ft1, ft0
    fsw  ft1, 0(a3)
    srli a7, a5, 4
    addi a7, a7, -8
    fcvt.s.w ft1, a7
    fmul.s ft1, ft1, ft0
    fsw  ft1, 4(a3)
    andi a7, a6, 15
    addi a7, a7, -8
    fcvt.s.w ft1, a7
    fmul.s ft1, ft1, ft0
    fsw  ft1, 8(a3)
    srli a7, a6, 4
    addi a7, a7, -8
    fcvt.s.w ft1, a7
    fmul.s ft1, ft1, ft0
    fsw  ft1, 12(a3)
    addi a3, a3, 16
    addi t3, t3, 1
    li   t4, 8
    bne  t3, t4, 2b
    addi a1, a1, TILE_GROUP_BYTES
    addi a2, a2, 32            // next group's scales (16 f16)
    addi t5, t5, 1
    j    1b
9:  ret

// f16 bits in t6 -> ft0 (f32). Scalar, normal numbers and zero only.
// Returns via t0 (called with jal t0).
f16_to_f32_scalar:
    srli a5, t6, 10
    andi a5, a5, 31            // exponent
    beqz a5, 1f
    addi a5, a5, 112
    slli a5, a5, 23
    andi a6, t6, 0x3ff
    slli a6, a6, 13
    or   a5, a5, a6
    li   a6, 0x8000
    and  a6, t6, a6
    slli a6, a6, 16
    or   a5, a5, a6
    fmv.w.x ft0, a5
    jr   t0
1:  fmv.w.x ft0, zero
    jr   t0

// ---------------------------------------------------------------------------
// Fused top-8: topk_init clears the list; topk_tile(a0 base index) folds the
// 16 values in accbuf into it. Results are copied to dargs at the end.
// ---------------------------------------------------------------------------
topk_init:
    la   t0, dargs
    addi t1, t0, 124           // idx
    addi t2, t0, 156           // val
    li   t3, 0xff7fffff        // -FLT_MAX
    li   t4, 8
1:  sw   zero, 0(t1)
    sw   t3, 0(t2)
    addi t1, t1, 4
    addi t2, t2, 4
    addi t4, t4, -1
    bnez t4, 1b
    ret

topk_tile:
    la   t0, dargs
    addi t1, t0, 124           // idx[0]
    addi t2, t0, 156           // val[0]
    flw  ft1, 28(t2)           // val[7] (current threshold)
    // quick reject: tile max <= threshold
    la   t3, accbuf
    li   t4, 16
    vsetvli t5, t4, e32, m4, ta, ma
    vle32.v v8, (t3)
    vfmv.s.f v16, ft1
    vfredmax.vs v16, v8, v16
    vfmv.f.s ft2, v16
    fle.s t5, ft2, ft1
    bnez t5, 9f
    li   t4, 0                 // i
1:  slli t5, t4, 2
    add  t5, t3, t5
    flw  ft2, 0(t5)            // v
    flw  ft1, 28(t2)
    fle.s t5, ft2, ft1
    bnez t5, 8f
    // insert: j from 7 down
    li   t6, 7
2:  beqz t6, 3f
    addi a7, t6, -1
    slli a5, a7, 2
    add  a5, t2, a5
    flw  ft3, 0(a5)            // val[j-1]
    flt.s t5, ft3, ft2
    beqz t5, 3f
    slli a6, t6, 2
    add  a6, t2, a6
    fsw  ft3, 0(a6)            // val[j] = val[j-1]
    add  a5, t1, a5
    lw   a7, 0(a5)
    add  a6, t1, a6
    sw   a7, 0(a6)             // idx[j] = idx[j-1]
    addi t6, t6, -1
    j    2b
3:  slli a6, t6, 2
    add  a5, t2, a6
    fsw  ft2, 0(a5)
    add  a5, t1, a6
    add  a7, a0, t4            // base + i
    sw   a7, 0(a5)
8:  addi t4, t4, 1
    li   t5, 16
    bne  t4, t5, 1b
9:  ret

// ---------------------------------------------------------------------------
// Routine tests (op 1..8): operands from dargs.test_* (offsets 188..216).
//   1 rmsnorm:   a=x b=scale c=out n=len d=residual(0)
//   2 quant:     a=x n=k; outputs copied: c=xq1d(out, n bytes) d=sx e=xsum8
//   3 matvec:    a=tiles b=scales(f16) c=out n=K k=N; activations from a
//                prior op 2 run (host runs op 2 then op 3)
//   4 rope:      a=x b=cos c=sin n=hd
//   5 attention: a=q b=kcache c=vcache d=out n=pos k=window
//   6 gelu:      a=gate_up c=out n=H
//   7 embed:     a=tiles b=scales c=out n=D, token in dargs.token, f=sqrt(D)
//   8 topk:      a=values (f32 [n]) n=count (multiple of 16); result in dargs
// ---------------------------------------------------------------------------
test_rmsnorm:
    lw   a0, 188(s0)
    lw   a1, 192(s0)
    lw   a2, 196(s0)
    lw   a3, 208(s0)
    lw   a4, 200(s0)
    j    rmsnorm
test_quant:
    addi sp, sp, -16
    sw   ra, 12(sp)
    lw   a0, 188(s0)
    lw   a1, 208(s0)
    jal  ra, quant_act
    lw   a0, 196(s0)
    la   a1, xq1d
    lw   a2, 208(s0)
    srli a2, a2, 2             // bytes -> words
    jal  ra, copy_f32
    lw   a0, 200(s0)
    la   a1, sxbuf
    lw   a2, 208(s0)
    srli a2, a2, 5
    jal  ra, copy_f32
    lw   a0, 204(s0)
    la   a1, xsum8
    lw   a2, 208(s0)
    srli a2, a2, 5
    jal  ra, copy_f32
    lw   ra, 12(sp)
    addi sp, sp, 16
    ret
test_matvec:
    lw   a0, 188(s0)
    lw   a1, 192(s0)
    lw   a2, 196(s0)
    lw   a3, 208(s0)
    lw   a4, 212(s0)
    j    matvec
test_rope:
    lw   a0, 188(s0)
    lw   a1, 192(s0)
    lw   a2, 196(s0)
    lw   a3, 208(s0)
    j    rope_head
test_attention:
    lw   a0, 188(s0)
    lw   a1, 192(s0)
    lw   a2, 196(s0)
    lw   a3, 200(s0)
    lw   a4, 208(s0)
    lw   a5, 212(s0)
    j    attention
test_gelu:
    lw   a0, 188(s0)
    lw   a1, 196(s0)
    lw   a2, 208(s0)
    j    gelu_mul
test_embed:
    lw   a0, 32(s0)
    lw   a1, 188(s0)
    lw   a2, 192(s0)
    lw   a3, 196(s0)
    lw   a4, 208(s0)
    flw  fa0, 216(s0)
    j    embed_row
test_topk:
    addi sp, sp, -16
    sw   ra, 12(sp)
    jal  ra, topk_init
    lw   s5, 188(s0)           // values
    lw   s6, 208(s0)           // n
    li   s8, 0
1:  bgeu s8, s6, 2f
    la   t0, accbuf
    li   t1, 16
    vsetvli t2, t1, e32, m4, ta, ma
    vle32.v v8, (s5)
    vse32.v v8, (t0)
    mv   a0, s8
    jal  ra, topk_tile
    addi s5, s5, 64
    addi s8, s8, 16
    j    1b
2:  lw   ra, 12(sp)
    addi sp, sp, 16
    ret

// ---------------------------------------------------------------------------
    .data
    .balign 16
    .globl dargs
dargs:
    .zero 224 + 32 * LAYER_WORDS * 4      // up to 32 layers

    .bss
    .balign 16
accbuf:  .zero 64
sxbuf:   .zero 1024                      // up to K = 32768
xsum8:   .zero 1024
scores:  .zero 2048                      // up to 512 positions
xq1d:    .zero 8192                      // up to K = 8192
