"""Audit the proposed encoding; this does not execute the Coral decoder."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re
import struct
import subprocess

PIN = 'b6e9dd7eb5c1765dabede62a07145d489ef39f9b'
OPS = {
    # name: (funct7, funct3, mask, first register index, rs2 register choices)
    'bt.submit': (0x40, 0, 0xfff0707f, 1, range(1)),
    'bt.poll': (0x40, 1, 0xfff0707f, 1, range(1)),
    'bt.reap': (0x40, 2, 0xfff0707f, 1, range(1)),
    'bt.lpq': (0x41, 0, 0xfe00707f, 0, range(32)),
    'bt.spq': (0x41, 1, 0xfe00707f, 0, range(32)),
}
ROOT = Path(__file__).resolve().parents[3]
FILES = {
    'scalar': 'hdl/chisel/src/coralnpu/scalar/Decode.scala',
    'float': 'hdl/chisel/src/coralnpu/float/FloatCoreInterface.scala',
    'rvv': 'hdl/chisel/src/coralnpu/rvv/RvvDecode.scala',
    'vme_words': 'tests/cocotb/vme_test/vme_test_utils.h',
}


def encode(name, rd, rs1, rs2=0):
    if name not in OPS:
        raise ValueError('Unknown instruction')
    funct7, funct3, _, minreg, rs2s = OPS[name]
    if not minreg <= rd <= 31 or not 0 <= rs1 <= 31 or rs2 not in rs2s:
        raise ValueError('Reserved encoding')
    return funct7 << 25 | rs2 << 20 | rs1 << 15 | funct3 << 12 | rd << 7 | 0x0b


def decode(word):
    rd, rs1 = (word >> 7) & 31, (word >> 15) & 31
    if not 0 <= word <= 0xffffffff:
        return None
    for name, (funct7, funct3, mask, minreg, _) in OPS.items():
        if rd >= minreg and word & mask == funct7 << 25 | funct3 << 12 | 0x0b:
            return name, rd, rs1, (word >> 20) & 31
    return None


def bitpats(source):
    result = []
    for pattern in re.findall(r'BitPat\("b([01?_]+)"\)', source):
        pattern = pattern.replace('_', '')
        if len(pattern) != 32:
            raise ValueError('Unexpected BitPat width')
        mask = int(''.join('0' if x == '?' else '1' for x in pattern), 2)
        value = int(pattern.replace('?', '0'), 2)
        result.append((value, mask))
    if not result:
        raise ValueError('No instruction BitPats found')
    return result


def overlap(value_a, mask_a, value_b, mask_b):
    return ((value_a ^ value_b) & mask_a & mask_b) == 0


def elf_text(path):
    data = path.read_bytes()
    if data[:7] != b'\x7fELF\x01\x01\x01':
        raise ValueError('Expected ELF32 little-endian object')
    if struct.unpack_from('<H', data, 18)[0] != 243:
        raise ValueError('Expected RISC-V ELF')
    shoff = struct.unpack_from('<I', data, 32)[0]
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 46)
    headers = [
        struct.unpack_from('<10I', data, shoff + i * shentsize)
        for i in range(shnum)
    ]
    strings = headers[shstrndx]
    names = data[strings[4]:strings[4] + strings[5]]
    for h in headers:
        if names[h[0]:].split(b'\x00', 1)[0] == b'.text':
            return data[h[4]:h[4] + h[5]]
    raise ValueError('No .text section')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--clang', required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    sources = {}
    hashes = {}
    for name, path in FILES.items():
        data = (ROOT / path).read_bytes()
        pinned = subprocess.check_output(['git', 'show', f'{PIN}:{path}'],
                                         cwd=ROOT)
        if data != pinned:
            raise ValueError(f'Decoder audit source changed: {path}')
        sources[name] = data.decode()
        hashes[path] = hashlib.sha256(data).hexdigest()

    scalar = bitpats(sources['scalar'])
    floating = bitpats(sources['float'])
    rvv_block = sources['rvv'].split('val new_opcode =',
                                     1)[1].split('// Fancy way', 1)[0]
    rvv = [int(x, 2) for x in re.findall(r'"b([01]{7})"\.U\s*->', rvv_block)]
    if set(rvv) != {0x07, 0x27, 0x57, 0x77}:
        raise ValueError('Unexpected RVV major dispatch')
    fp_block = sources['float'].split('val opcode              =',
                                      1)[1].split('val fcvt_s_bf16', 1)[0]
    fp = [int(x, 2) for x in re.findall(r'"b([01]{5})"\.U\s*->', fp_block)]
    if set(fp) != {1, 9, 20, 16, 17, 18, 19}:
        raise ValueError('Unexpected FP major dispatch')
    existing = scalar + floating + [(x, 0x7f) for x in rvv]
    # The actual FP decoder compares bits[6:2], so audit its broader mask.
    existing += [(x << 2, 0x7c) for x in fp]
    proposal_masks = []
    for name, (_, _, mask, _, _) in OPS.items():
        proposed = encode(name, 1, 0) & mask
        if any(overlap(proposed, mask, v, m) for v, m in existing):
            raise ValueError(f'Existing decoder collision: {name}')
        if any(overlap(proposed, mask, v, m) for v, m in proposal_masks):
            raise ValueError(f'Proposed instruction collision: {name}')
        proposal_masks.append((proposed, mask))

    words = []
    asm = ['.text', '.option norvc', '.option norelax']
    counts = {}
    for name, (funct7, funct3, _, minreg, rs2s) in OPS.items():
        counts[name] = 0
        for rd, rs1, rs2 in itertools.product(range(minreg, 32), range(32),
                                              rs2s):
            word = encode(name, rd, rs1, rs2)
            if decode(word) != (name, rd, rs1, rs2):
                raise ValueError('Round-trip failure')
            words.append(word)
            asm.append(
                f'.insn r 0x0b, {funct3}, {funct7}, x{rd}, x{rs1}, x{rs2}'
            )
            counts[name] += 1
    reserved_checks = 0
    for funct7, rs2, funct3 in itertools.product(range(128), range(32),
                                                 range(8)):
        word = (
            funct7 << 25 | rs2 << 20 | 1 << 15 | funct3 << 12 | 1 << 7 | 0x0b
        )
        legal = (funct7 == 0x40 and rs2 == 0 and funct3
                 in (0, 1, 2)) or (funct7 == 0x41 and funct3 in (0, 1))
        if (decode(word) is not None) != legal:
            raise ValueError('Reserved field accepted or legal word rejected')
        reserved_checks += 1
    for name, rs1 in itertools.product(OPS, range(32)):
        minreg = OPS[name][3]
        if (decode(encode(name, 1, rs1) & ~(31 << 7))
                is None) != (minreg == 1):
            raise ValueError('Wrong zero register legality')
        reserved_checks += 1
    for word in words:
        for opcode in range(128):
            if opcode != 0x0b and decode(word & ~127 | opcode) is not None:
                raise ValueError('Wrong major opcode accepted')
            reserved_checks += 1

    source = args.out / 'all-registers.s'
    obj = args.out / 'all-registers.o'
    source.write_text('\n'.join(asm) + '\n')
    command = [
        args.clang, '--target=riscv32-unknown-elf', '-march=rv32im',
        '-mabi=ilp32', '-c',
        str(source), '-o',
        str(obj)
    ]
    subprocess.run(command, check=True)
    emitted = elf_text(obj)
    expected = b''.join(struct.pack('<I', word) for word in words)
    if emitted != expected:
        raise ValueError('Assembler output does not match proposed encodings')
    report = {
        'status':
        'PASS',
        'scope':
        'Source mask audit and real assembler bytes only; no hardware execution',
        'core_source_commit':
        PIN,
        'core_source_sha256':
        hashes,
        'scalar_bitpats':
        len(scalar),
        'float_bitpats':
        len(floating),
        'rvv_major_opcodes': [hex(x) for x in rvv],
        'fp_major_bits_6_2':
        fp,
        'valid_register_combinations_assembled':
        len(words),
        'combinations_by_instruction':
        counts,
        'reserved_and_major_field_checks':
        reserved_checks,
        'assembler_command':
        command,
        'assembler_version':
        subprocess.check_output([args.clang, '--version'], text=True),
        'text_sha256':
        hashlib.sha256(emitted).hexdigest(),
        'checker_sha256':
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
