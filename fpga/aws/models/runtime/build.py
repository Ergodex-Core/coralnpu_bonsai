#!/usr/bin/env python3
"""Build freestanding DDR inference with strict LLVM 18.1.3 provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
HOST = ROOT / 'fpga/aws/ddr/host'


def run(argv):
    p = subprocess.run([str(x) for x in argv],
                       capture_output=True,
                       text=True,
                       timeout=180)
    if p.returncode:
        raise RuntimeError(f'command failed: {argv}\n{p.stdout}\n{p.stderr}')
    return p.stdout + p.stderr


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--clang', default='clang')
    ap.add_argument('--linker', default='ld.lld')
    ap.add_argument('--objdump', default='llvm-objdump')
    ap.add_argument('--readelf', default='llvm-readelf')
    ap.add_argument('--memory-profile', choices=['ddr', 'hbm'], default='ddr')
    ap.add_argument('--q8-rvv', action='store_true')
    ap.add_argument('--probe', action='store_true', help='build the bounded Q8 probe instead of the decoder')
    ap.add_argument(
        '--allow-unpinned-toolchain',
        action='store_true',
        help='exploratory build ONLY; manifest is UNQUALIFIED'
    )
    a = ap.parse_args()
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    toolchain = {}
    pinned = True
    for name in ('clang', 'linker', 'objdump', 'readelf'):
        selected = shutil.which(getattr(a, name))
        if not selected:
            raise ValueError(f'missing tool {name}: {getattr(a,name)}')
        path = Path(selected).absolute()
        version = run([path, '--version'])
        match = bool(re.search(r'\b18\.1\.3\b', version))
        pinned &= match
        if not match and not a.allow_unpinned_toolchain:
            raise ValueError(f'{name} must be LLVM18.1.3: {version}')
        toolchain[name] = dict(
            version=version,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        )
        setattr(a, name, str(path))
    if a.probe and a.memory_profile != 'hbm':
        raise ValueError('external-memory Q8 probe requires the HBM linker profile')
    sources = [
        HERE / n for n in
        (('q8_target_probe.c', 'start.S') if a.probe else
         ('decoder.c', 'math.c', 'generate.c', 'firmware.c', 'start.S'))
    ] + [ROOT / 'toolchain/crt/crt.S']
    flags = [
        '--target=riscv32-unknown-elf',
        '-march=rv32imf_zicsr_zifencei_zve32f_zvl128b', '-mabi=ilp32f',
        '-mno-relax', '-mcmodel=medany', '-msmall-data-limit=0', '-Oz', '-g',
        '-std=c11', '-ffreestanding', '-fno-builtin', '-ffp-contract=off',
        '-fno-fast-math', '-fno-vectorize', '-fno-slp-vectorize',
        '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
        '-fuse-ld=lld', '--ld-path=' + a.linker, '-I' + str(ROOT), '-Wall',
        '-Wextra', '-Werror', '-ffile-prefix-map=' + str(ROOT) + '=/coralnpu',
        '-Wl,--no-relax', '-Wl,-T,' + str(HERE / (a.memory_profile + '.ld')),
        '-Wl,-Map,' + str(output / 'decoder.map')
    ]
    if a.memory_profile == 'hbm': flags.append('-DCM_HBM_PROFILE')
    if a.q8_rvv: flags.append('-DCM_Q8_RVV')
    elf = output / 'decoder.elf'
    command = [a.clang, *flags, *sources, '-o', elf]
    (output / 'build.log').write_text(run(command))
    disassembly = run([a.objdump, '-d', elf])
    (output / 'decoder.disasm').write_text(disassembly)
    attrs = run([a.readelf, '-A', '-l', '-S', elf])
    (output / 'decoder.readelf').write_text(attrs)
    operations = [] if a.probe else ['fadd.s', 'fmul.s', 'fdiv.s', 'fsqrt.s', '<cm_generate>']
    if a.q8_rvv: operations += ['vle8.v', 'vwmul.vv', 'vwredsum.vs']
    for operation in operations:
        if operation not in disassembly:
            raise ValueError(f'missing expected operation {operation}')
    if re.search(r'\b(?:fmadd|fmsub|fnmadd|fnmsub)\.s\b', disassembly):
        raise ValueError('FMA violates ordered FP32 contract')
    # Share the loader geometry checks used before any physical transfer.
    if a.memory_profile == 'hbm':
        sys.path.insert(0, str(ROOT / 'fpga/aws/hbm/host'))
        from elf_image_hbm import ElfImage
    else:
        sys.path.insert(0, str(HOST))
        from elf_image import ElfImage
    digest = hashlib.sha256(elf.read_bytes()).hexdigest()
    image = ElfImage(elf, digest)
    tracked = sources + [
        HERE / n for n in (
            'decoder.h', 'math.h', 'q8_0.h', 'address_range.h', 'generate.h', 'mailbox.h', a.memory_profile + '.ld',
            'build.py'
        )
    ] + [
        HERE.parent / 'model_format.h', ROOT / 'toolchain/crt/coralnpu_start.S'
    ]
    manifest = dict(
        schema=1,
        evidence_kind='build_only',
        runtime_kind='q8_probe' if a.probe else 'decoder',
        memory_profile=a.memory_profile,
        q8_rvv=a.q8_rvv,
        toolchain_qualification='PINNED_18.1.3' if pinned else 'UNQUALIFIED',
        full_model_physical='NOTRUN',
        isa='rv32imf_zicsr_zifencei_zve32f_zvl128b',
        flen=32,
        vlen=128,
        flags=flags,
        toolchain=toolchain,
        elf='decoder.elf',
        sha256=digest,
        entry=image.entry,
        segments=[{
            k: v
            for k, v in s.items()
            if k != 'data'
        }
                  for s in image.segments],
        symbols=image.symbols,
        sources={
            str(p.relative_to(ROOT)):
            hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tracked
        }
    )
    (output /
     'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(
        json.dumps({
            k: manifest[k]
            for k in
            ('evidence_kind', 'toolchain_qualification', 'sha256', 'segments')
        },
                   indent=2)
    )


if __name__ == '__main__': main()
