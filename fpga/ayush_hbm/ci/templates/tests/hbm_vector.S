.section .text
.global _start
.option norvc
.option norelax
_start:
  li t0, 0x600
  csrs mstatus, t0
  li a0, 0x80010000
  li a1, 0x80030000
  li a2, 0x80050000
  li t1, 16384
  vsetivli zero, 4, e32, m1, ta, ma
loop:
  vle32.v v1, (a0)
  vle32.v v2, (a1)
  vadd.vv v3, v1, v2
  vse32.v v3, (a2)
  addi a0, a0, 16
  addi a1, a1, 16
  addi a2, a2, 16
  addi t1, t1, -4
  bnez t1, loop
  fence
  li a0, 0x10000
  li t0, 0x51
  sw t0, 0(a0)
  fence
  .word 0x08000073
