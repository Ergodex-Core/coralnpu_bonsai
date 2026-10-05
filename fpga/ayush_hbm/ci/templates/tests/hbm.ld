ENTRY(_start)
PHDRS { code PT_LOAD FLAGS(5); }
SECTIONS { . = 0x80000000; .text : { *(.text*) } :code /DISCARD/ : { *(.comment) *(.riscv.attributes) } }
