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
// Tile-load bandwidth microbenchmark (pure assembly). Streams kargs.n bytes
// from kargs.in0 in 256-byte steps:
//   flags = 0: 16 x vle8.v (16 B each) into v8 (baseline, same as membench)
//   flags = 1: 16 x vtle8 (one 16-byte tile slice each) per 256 B
//   flags = 2: 16 x vle8 with LMUL1 into 16 different registers (v0..v15)
//              back to back (tests load overlap)
// kargs layout: see models/common/kernel_args.h (m,k,n,flags,in0,...).
    .text
    .globl main
main:
    csrr s1, mcycle
    la   t0, kargs
    lw   a1, 12(t0)          // flags
    lw   a2, 16(t0)          // in0
    lw   a3, 8(t0)           // n (bytes, multiple of 256)
    // SEW8 LMUL1, vl = 16 for the vector loads.
    li   t1, 16
    vsetvli t2, t1, e8, m1, ta, ma
    beqz a1, .Lvle
    li   t3, 2
    beq  a1, t3, .Lunrolled
.Ltile:
    // mtype: tm=16, tk=1, mtwiden=1; vtype SEW8 LMUL1. TSS = tile 0, row 0,
    // pattern 0: vtle8 loads 16 rows of 16 bytes from rs1 contiguous.
    li   t5, (16 << 10) | (1 << 5) | 1
    li   t6, 0xC0
    .insn r 0x57, 7, 0x41, x0, t5, t6     // msetmtype
    li   t5, 16
    .insn r 0x57, 7, 0x42, t6, t5, x0     // msettn 16
    .insn r 0x57, 7, 0x42, t6, t5, x1     // msettm 16
    // A tile load moves one 16-byte slice (row tss[3:0]) per instruction.
1:  li   t4, 0                            // tss: tile 0, row 0
2:  .insn r 0x07, 7, 0x09, zero, a2, t4   // vtle8 zero, (a2), t4
    addi a2, a2, 16
    addi t4, t4, 1
    andi t5, t4, 15
    bnez t5, 2b
    addi a3, a3, -256
    bnez a3, 1b
    j    .Ldone
.Lvle:
1:  vle8.v v8, (a2)
    addi t4, a2, 16
    vle8.v v8, (t4)
    addi t4, a2, 32
    vle8.v v8, (t4)
    addi t4, a2, 48
    vle8.v v8, (t4)
    addi t4, a2, 64
    vle8.v v8, (t4)
    addi t4, a2, 80
    vle8.v v8, (t4)
    addi t4, a2, 96
    vle8.v v8, (t4)
    addi t4, a2, 112
    vle8.v v8, (t4)
    addi t4, a2, 128
    vle8.v v8, (t4)
    addi t4, a2, 144
    vle8.v v8, (t4)
    addi t4, a2, 160
    vle8.v v8, (t4)
    addi t4, a2, 176
    vle8.v v8, (t4)
    addi t4, a2, 192
    vle8.v v8, (t4)
    addi t4, a2, 208
    vle8.v v8, (t4)
    addi t4, a2, 224
    vle8.v v8, (t4)
    addi t4, a2, 240
    vle8.v v8, (t4)
    addi a2, a2, 256
    addi a3, a3, -256
    bnez a3, 1b
    j    .Ldone
.Lunrolled:
1:  vle8.v v0, (a2)
    addi t4, a2, 16
    vle8.v v1, (t4)
    addi t4, a2, 32
    vle8.v v2, (t4)
    addi t4, a2, 48
    vle8.v v3, (t4)
    addi t4, a2, 64
    vle8.v v4, (t4)
    addi t4, a2, 80
    vle8.v v5, (t4)
    addi t4, a2, 96
    vle8.v v6, (t4)
    addi t4, a2, 112
    vle8.v v7, (t4)
    addi t4, a2, 128
    vle8.v v8, (t4)
    addi t4, a2, 144
    vle8.v v9, (t4)
    addi t4, a2, 160
    vle8.v v10, (t4)
    addi t4, a2, 176
    vle8.v v11, (t4)
    addi t4, a2, 192
    vle8.v v12, (t4)
    addi t4, a2, 208
    vle8.v v13, (t4)
    addi t4, a2, 224
    vle8.v v14, (t4)
    addi t4, a2, 240
    vle8.v v15, (t4)
    addi a2, a2, 256
    addi a3, a3, -256
    bnez a3, 1b
.Ldone:
    la   t0, kargs
    csrr t2, mcycle
    sub  t2, t2, s1
    sw   t2, 68(t0)          // cycles
    li   t1, 0x600D
    sw   t1, 64(t0)          // status
    li   a0, 0
    ret

    .data
    .balign 16
    .globl kargs
kargs:
    .zero 72
