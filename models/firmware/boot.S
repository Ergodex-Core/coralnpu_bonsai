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
// Minimal startup for one kernel on the CoralNPU F2 image (mirrors
// toolchain/crt/coralnpu_start.S): stack, FS/VS enabled, bss zeroed, _ret
// sentinel, main, then mpause (halt) on 0 or ebreak (fault) otherwise.
    .section .init, "ax"
    .globl _start
_start:
    la sp, __stack_end__
    la t0, trap
    csrw mtvec, t0
    li t0, 0x6600          // mstatus.FS = VS = Dirty (MS resets to Initial)
    csrs mstatus, t0
    la a0, __bss_start__
    la a1, __bss_end__
1:  bgeu a0, a1, 2f
    sw zero, 0(a0)
    addi a0, a0, 4
    j 1b
2:  li t0, 0x0badd00d
    la t1, _ret
    sw t0, 0(t1)
    li a0, 0
    li a1, 0
    call main
    la t1, _ret
    sw a0, 0(t1)
    bnez a0, trap
    .word 0x08000073       // mpause: core halts
3:  j 3b
trap:
    ebreak                 // fault: status bit 1
    j trap
