/* HBM ABI profile: pair only with -DCM_HBM_PROFILE and an HBM-aware loader.
 * CPU preparation only; strict LLVM18.1.3 target link NOT RUN here. */
/* SPDX-License-Identifier: Apache-2.0 */
/* Pinned CoreAxi enables external instruction fetch. Boot remains in ITCM;
 * no runtime instruction fetches use DDR until the host has loaded all data. */
MEMORY {
  ITCM(rx) : ORIGIN = 0, LENGTH = 8K
  DTCM(rw) : ORIGIN = 0x10000, LENGTH = 32K
  DDR(rx) : ORIGIN = 0x80000000, LENGTH = 64K
}
ENTRY(_start)
SECTIONS {
  .boot : ALIGN(16) { KEEP(*(._init)) . = ALIGN(16); } > ITCM
  .text : ALIGN(16) { *(.text .text.*) *(.crt) . = ALIGN(16); } > DDR
  .rodata : ALIGN(16) { *(.srodata .srodata.* .rodata .rodata.*) . = ALIGN(16); } > DDR
  .q8_probe_data : ALIGN(64) { *(.q8_probe_data) . = ALIGN(64); } > DDR
  .init_array : ALIGN(4) {
    __init_array_start__ = .; KEEP(*(.init_array .init_array.*)) __init_array_end__ = .;
  } > DDR
  .fini_array : ALIGN(4) {
    __fini_array_start__ = .; KEEP(*(.fini_array .fini_array.*)) __fini_array_end__ = .;
  } > DDR
  .mailbox (NOLOAD) : ALIGN(64) { KEEP(*(.mailbox)) } > DTCM
  .data : ALIGN(16) {
    _global_pointer = . + 0x800;
    *(.sdata .sdata.* .data .data.*)
    . = ALIGN(4); _ret = .; LONG(0x0badd00d); . = ALIGN(16);
  } > DTCM
  .bss (NOLOAD) : ALIGN(16) {
    __bss_start__ = .; *(.sbss .sbss.* .bss .bss.*) *(COMMON)
    . = ALIGN(16); __bss_end__ = .;
  } > DTCM
  .stack (NOLOAD) : ALIGN(16) {
    __stack_guard__ = .; . += 64; __stack_start__ = .;
    . += 4096; __stack_end__ = .;
  } > DTCM
  /DISCARD/ : { *(.comment .eh_frame .eh_frame_hdr) }
}
ASSERT(ADDR(.mailbox) == 0x10000, "mailbox ABI address")
ASSERT(SIZEOF(.mailbox) == 128, "mailbox ABI size")
ASSERT(SIZEOF(.init_array) == 0, "no constructors")
ASSERT(SIZEOF(.fini_array) == 0, "no destructors")
ASSERT(__stack_end__ <= ORIGIN(DTCM)+LENGTH(DTCM), "DTCM overflow")
