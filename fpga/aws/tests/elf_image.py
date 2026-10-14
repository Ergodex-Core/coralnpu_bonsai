#!/usr/bin/env python3
"""Strict, dependency-free ELF32 reader for the 8 KiB/32 KiB F2 image."""
import hashlib
import struct
from pathlib import Path

REGIONS = ((0, 0x2000), (0x10000, 0x18000))


def in_tcm(address, size):
    return size >= 0 and any(
        lo <= address and address + size <= hi for lo, hi in REGIONS
    )


class ElfImage:

    def __init__(self, path):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        data = self.data
        if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01':
            raise ValueError('expected little-endian ELF32')
        fields = struct.unpack_from('<HHIIIIIHHHHHH', data, 16)
        kind, machine, version, self.entry, phoff, shoff, flags, ehsize, phsize, phnum, shsize, shnum, _ = fields
        if (kind, machine, version, ehsize, phsize, shsize) != (2, 243, 1, 52,
                                                                32, 40):
            raise ValueError(
                'expected executable RISC-V ELF with section and program headers'
            )
        if flags != 2:  # EF_RISCV_FLOAT_ABI_SINGLE; no compressed instructions.
            raise ValueError(
                f'expected RV32 single-float ABI without RVC, flags={flags:#x}'
            )
        if self.entry % 4 or not 0 <= self.entry < 8192:
            raise ValueError('entry must be aligned within ITCM')
        if phoff + phnum * phsize > len(data) or shoff + shnum * shsize > len(
                data):
            raise ValueError('truncated ELF headers')
        self.segments = []
        for i in range(phnum):
            typ, offset, va, pa, filesz, memsz, perms, align = struct.unpack_from(
                '<8I', data, phoff + i * 32
            )
            if typ != 1 or not memsz:
                continue
            if va != pa or filesz > memsz or offset + filesz > len(
                    data) or not in_tcm(pa, memsz):
                raise ValueError(
                    'load segment escapes TCM or has invalid file geometry'
                )
            if any(pa < prev['address'] +
                   prev['size'] and prev['address'] < pa + memsz
                   for prev in self.segments):
                raise ValueError('overlapping load segments')
            self.segments.append(
                dict(
                    address=pa,
                    size=memsz,
                    file_size=filesz,
                    flags=perms,
                    data=data[offset:offset + filesz] + bytes(memsz - filesz)
                )
            )
        if not any(
                s['flags'] & 1 and s['address'] <= self.entry < s['address'] +
                s['file_size'] for s in self.segments):
            raise ValueError('entry is outside executable file-backed bytes')
        self.symbols = {}
        sections = [
            struct.unpack_from('<10I', data, shoff + i * 40)
            for i in range(shnum)
        ]
        for section in sections:
            _, typ, _, addr, off, size, link, _, _, entsize = section
            if typ != 2:
                continue
            if entsize != 16 or size % 16 or off + size > len(
                    data) or link >= len(sections):
                raise ValueError('invalid symbol table')
            strings = sections[link]
            if strings[4] + strings[5] > len(data):
                raise ValueError('invalid string table')
            names = data[strings[4]:strings[4] + strings[5]]
            for p in range(off, off + size, 16):
                name, value, sym_size, _, _, shndx = struct.unpack_from(
                    '<IIIBBH', data, p
                )
                if not shndx or not name:
                    continue
                if name >= len(names) or b'\0' not in names[name:]:
                    raise ValueError('invalid symbol name')
                self.symbols[names[name:].split(b'\0', 1)[0].decode()] = dict(
                    address=value, size=sym_size
                )
        for name in ('_ret', '__stack_guard__', '__stack_start__',
                     '__stack_end__'):
            self.address(name)
        guard, bottom, top = [
            self.address(n)
            for n in ('__stack_guard__', '__stack_start__', '__stack_end__')
        ]
        if bottom - guard != 64 or top - bottom != 4096 or not in_tcm(
                guard, top - guard):
            raise ValueError(
                'expected 64-byte guard plus 4096-byte DTCM stack'
            )
        self.address('_ret', 4)

    def address(self, name, size=0):
        if name not in self.symbols:
            raise ValueError(f'missing symbol {name}')
        symbol = self.symbols[name]
        address = symbol['address']
        if not in_tcm(address, size) or (size
                                         and symbol['size'] not in (0, size)):
            raise ValueError(f'invalid range or size for symbol {name}')
        if size and not any(s['address'] <= address and address +
                            size <= s['address'] + s['size']
                            for s in self.segments):
            raise ValueError(f'symbol {name} is outside load segments')
        return address

    def manifest(self):
        return dict(
            sha256=self.sha256,
            entry=self.entry,
            segments=[{
                k: v
                for k, v in s.items()
                if k != 'data'
            }
                      for s in self.segments],
            symbols=self.symbols
        )
