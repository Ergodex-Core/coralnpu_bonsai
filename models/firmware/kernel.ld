/* Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * One kernel on the F2 image: 8 KiB ITCM at 0x0, 32 KiB DTCM at 0x10000.
 */
OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
  ITCM (rx)  : ORIGIN = 0x00000000, LENGTH = 8K
  DTCM (rw)  : ORIGIN = 0x00010000, LENGTH = 32K
}

STACK_SIZE = 2048;

SECTIONS {
  .text : {
    KEEP(*(.init))
    *(.text .text.*)
    *(.rodata .rodata.* .srodata .srodata.*)
    . = ALIGN(4);
  } > ITCM

  .data : {
    . = ALIGN(16);
    *(.data .data.* .sdata .sdata.*)
    . = ALIGN(4);
    _ret = .;
    LONG(0x0badd00d)
    . = ALIGN(4);
  } > DTCM

  .bss (NOLOAD) : {
    . = ALIGN(16);
    __bss_start__ = .;
    *(.bss .bss.* .sbss .sbss.* COMMON)
    . = ALIGN(4);
    __bss_end__ = .;
  } > DTCM

  .stack (NOLOAD) : {
    . = ALIGN(16);
    __stack_start__ = .;
    . += STACK_SIZE;
    __stack_end__ = .;
  } > DTCM

  /DISCARD/ : { *(.eh_frame .eh_frame_hdr .riscv.attributes .comment .note.*) }
}
