/* SPDX-License-Identifier: Apache-2.0 */
/* TCM-only AWS F2 configuration. No external-memory region exists. */
MEMORY {
  ITCM(rx) : ORIGIN = 0, LENGTH = 8K
  DTCM(rw) : ORIGIN = 0x10000, LENGTH = 32K
}
ENTRY(_start)
SECTIONS {
  .text : ALIGN(16) {
    KEEP(*(._init)) *(.text .text.*) *(.crt) . = ALIGN(16);
  } > ITCM
  .rodata : ALIGN(16) {
    *(.srodata .srodata.* .rodata .rodata.*) . = ALIGN(16);
  } > ITCM
  .init_array : ALIGN(4) {
    __init_array_start__ = .; KEEP(*(.init_array .init_array.*))
    __init_array_end__ = .;
  } > ITCM
  .fini_array : ALIGN(4) {
    __fini_array_start__ = .; KEEP(*(.fini_array .fini_array.*))
    __fini_array_end__ = .;
  } > ITCM
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
ASSERT(SIZEOF(.init_array) == 0, "examples must not need constructors")
ASSERT(SIZEOF(.fini_array) == 0, "examples must not need destructors")
ASSERT(__stack_end__ <= ORIGIN(DTCM) + LENGTH(DTCM), "DTCM stack overflow")
