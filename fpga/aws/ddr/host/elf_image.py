"""Strict ELF32 firmware geometry: ITCM boot, DTCM, reserved DDR code/data."""
import hashlib
import struct
from pathlib import Path

from transport import DDR_BASE, in_tcm

FIRMWARE_END = DDR_BASE + 0x100000


class ElfImage:
    def __init__(self, path, expected_sha256):
        self.path = Path(path)
        if self.path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('firmware ELF exceeds 8 MiB input limit')
        data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        if self.sha256 != expected_sha256:
            raise ValueError('firmware ELF SHA256 mismatch')
        if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01':
            raise ValueError('expected little-endian ELF32')
        fields = struct.unpack_from('<HHIIIIIHHHHHH', data, 16)
        kind, machine, version, self.entry, phoff, shoff, flags, ehsize, phsize, phnum, shsize, shnum, _ = fields
        if (kind, machine, version, flags, ehsize, phsize, shsize) != (2, 243, 1, 2, 52, 32, 40):
            raise ValueError('expected executable RV32 single-float ELF without compressed ABI')
        if self.entry % 4 or not 0 <= self.entry < 0x2000:
            raise ValueError('ELF must enter through aligned ITCM boot code')
        if phnum == 0 or phoff < 52 or shoff < 52 or not shnum or (
                phoff + phnum * phsize > len(data) or shoff + shnum * shsize > len(data)):
            raise ValueError('invalid/truncated ELF headers')
        self.segments = []
        for i in range(phnum):
            typ, offset, va, pa, filesz, memsz, perms, align = struct.unpack_from('<8I', data, phoff + i * 32)
            if typ != 1 or not memsz:
                continue
            valid = in_tcm(pa, memsz) or DDR_BASE <= pa < pa + memsz <= FIRMWARE_END
            if va != pa or filesz > memsz or offset + filesz > len(data) or not valid:
                raise ValueError('ELF load segment has invalid memory/file geometry')
            if align > 1 and (align & (align - 1) or (pa - offset) % align):
                raise ValueError('ELF segment alignment is invalid')
            if any(pa < s['address'] + s['size'] and s['address'] < pa + memsz for s in self.segments):
                raise ValueError('overlapping ELF load segments')
            if perms & 1 and not (pa < 0x2000 or DDR_BASE <= pa < FIRMWARE_END):
                raise ValueError('executable ELF segment outside ITCM/DDR firmware')
            self.segments.append(dict(address=pa, size=memsz, file_size=filesz, flags=perms,
                                      data=data[offset:offset + filesz] + bytes(memsz - filesz)))
        if not any(s['flags'] & 1 and s['address'] <= self.entry < s['address'] + s['file_size']
                   for s in self.segments):
            raise ValueError('entry is outside executable file-backed bytes')
        sections = [struct.unpack_from('<10I', data, shoff + i * 40) for i in range(shnum)]
        self.symbols = {}
        for section in sections:
            _, typ, _, _, off, size, link, _, _, entsize = section
            if typ != 2:
                continue
            if entsize != 16 or size % 16 or off + size > len(data) or link >= len(sections):
                raise ValueError('invalid ELF symbol table')
            strings = sections[link]
            if strings[1] != 3 or strings[4] + strings[5] > len(data):
                raise ValueError('invalid ELF symbol string table')
            names = data[strings[4]:strings[4] + strings[5]]
            for p in range(off, off + size, 16):
                name, value, symbol_size, _, _, shndx = struct.unpack_from('<IIIBBH', data, p)
                if not shndx or not name:
                    continue
                if name >= len(names) or b'\0' not in names[name:]:
                    raise ValueError('invalid ELF symbol name')
                key = names[name:].split(b'\0', 1)[0].decode('ascii')
                self.symbols[key] = dict(address=value, size=symbol_size)
        self.address('_ret', 4)
        if self.address('coral_mailbox', 128) != 0x10000:
            raise ValueError('ABI v2 mailbox must be 128 bytes at 0x10000')
        guard, bottom, top = (self.address(n) for n in
                             ('__stack_guard__', '__stack_start__', '__stack_end__'))
        if bottom - guard != 64 or top - bottom != 4096 or not 0x10080 <= guard < top <= 0x18000:
            raise ValueError('expected 64-byte stack guard and 4096-byte DTCM stack')
        ret = self.address('_ret', 4)
        if ret < 0x10080 or ret + 4 > guard:
            raise ValueError('_ret overlaps mailbox/stack or escapes DTCM')

    def address(self, name, size=0):
        if name not in self.symbols:
            raise ValueError(f'missing ELF symbol {name}')
        symbol = self.symbols[name]
        address = symbol['address']
        if not in_tcm(address, size) or address % 4 or (size and symbol['size'] not in (0, size)):
            raise ValueError(f'invalid TCM symbol {name}')
        if size and not any(s['address'] <= address and address + size <= s['address'] + s['size']
                            for s in self.segments):
            raise ValueError(f'ELF symbol {name} is outside PT_LOAD memory')
        return address

    def manifest(self):
        return dict(sha256=self.sha256, entry=self.entry,
                    segments=[{k: v for k, v in s.items() if k != 'data'} for s in self.segments],
                    symbols=self.symbols)
