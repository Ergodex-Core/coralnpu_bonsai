#!/usr/bin/env python3
"""Build four unmodified upstream examples with a pinned Clang 18.1.3."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from elf_image import ElfImage

REVISION = '382b5c12030ad8eb74ab8deafb2529301faeab16'
SOURCES = {
    'math': 'tests/cocotb/math.cc',
    'fptr': 'tests/cocotb/fptr.cc',
    'float_add': 'examples/hello_world_add_floats.cc',
    'rvv_add': 'examples/rvv_add_intrinsic.cc',
}


def command(argv, log=None):
    proc = subprocess.run([str(x) for x in argv],
                          text=True,
                          capture_output=True,
                          timeout=120)
    if log is not None:
        log.write_text(proc.stdout + proc.stderr)
    if proc.returncode:
        raise RuntimeError(
            f'command failed: {argv}\n{proc.stdout}\n{proc.stderr}'
        )
    return proc.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--source-root',
        type=Path,
        default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--clang', default='clang++')
    parser.add_argument('--linker', default='ld.lld')
    parser.add_argument('--objdump', default='llvm-objdump-18')
    parser.add_argument('--readelf', default='llvm-readelf-18')
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    root = args.source_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    toolchain = {}
    for name in ('clang', 'linker', 'objdump', 'readelf'):
        selected = getattr(args, name)
        found = shutil.which(selected)
        if found is None:
            raise ValueError(f'missing {name}: {selected}')
        # LLVM multicall binaries select their behavior from argv[0].
        # Preserve ld.lld/llvm-readelf symlink names while hashing their targets.
        executable = Path(found).absolute()
        version = command([executable, '--version'])
        if not re.search(r'\b18\.1\.3\b', version):
            raise ValueError(f'{name} must be LLVM 18.1.3; got {version}')
        toolchain[name] = dict(
            path=str(executable),
            resolved_path=str(executable.resolve()),
            version=version,
            sha256=hashlib.sha256(executable.read_bytes()).hexdigest()
        )
        setattr(args, name, str(executable))
    source_paths = list(SOURCES.values()) + [
        'toolchain/crt/coralnpu_start.S', 'toolchain/crt/crt.S'
    ]
    sources = {}
    for relative in source_paths:
        data = (root / relative).read_bytes()
        pinned = subprocess.run([
            'git', '-C',
            str(root), 'show', f'{REVISION}:{relative}'
        ],
                                check=True,
                                capture_output=True,
                                timeout=30).stdout
        if data != pinned:
            raise ValueError(
                f'{relative} differs from pinned upstream revision {REVISION}'
            )
        sources[relative] = hashlib.sha256(data).hexdigest()
    flags = [
        '--target=riscv32-unknown-elf',
        '-march=rv32imf_zicsr_zifencei_zve32f_zvl128b', '-mabi=ilp32f',
        '-mno-relax', '-mcmodel=medany', '-O0', '-g', '-ffreestanding',
        '-fno-builtin', '-fno-exceptions', '-fno-rtti',
        '-fno-threadsafe-statics', '-fno-vectorize', '-fno-slp-vectorize',
        '-fdebug-compilation-dir=/coralnpu', '-nostdlib', '-fuse-ld=lld',
        '--ld-path=' + args.linker, '-std=c++17',
        '-ffile-prefix-map=' + str(root) + '=/coralnpu',
        '-ffile-prefix-map=' + str(here) + '=/fpga-tests',
        '-ffile-prefix-map=' + str(output) + '=/test-build',
        '-Wno-unknown-pragmas', '-I' + str(root), '-I' + str(here / 'include'),
        '-Wl,--no-relax', '-Wl,-T,' + str(here / 'tcm.ld')
    ]
    manifest = dict(
        schema=1,
        evidence_kind='build_only',
        upstream_revision=REVISION,
        toolchain=toolchain,
        isa='rv32imf_zicsr_zifencei_zve32f_zvl128b',
        flen=32,
        vlen=128,
        sources=sources,
        support_sources={},
        tests={}
    )
    for path in sorted(here.rglob('*')):
        if path.is_file() and '__pycache__' not in str(
                path) and path.suffix not in ('.pyc', ):
            manifest['support_sources'][str(path.relative_to(here))
                                        ] = hashlib.sha256(path.read_bytes()
                                                           ).hexdigest()
    for name, relative in SOURCES.items():
        elf = output / f'{name}.elf'
        # In freestanding C++, Clang does not give main implicit C linkage.
        # Declare that linkage without changing the pinned example.
        wrapper = output / f'{name}.entry.cc'
        parameters = 'int, char**' if name in ('math', 'fptr') else ''
        wrapper.write_text(
            f'extern \"C\" int main({parameters});\n#include \"{relative}\"\n'
        )
        argv = [
            args.clang, *flags, wrapper, here / 'start.S',
            root / 'toolchain/crt/crt.S', here / 'runtime.cc',
            '-Wl,-Map,' + str(output / f'{name}.map'), '-o', elf
        ]
        command(argv, log=output / f'{name}.build.log')
        image = ElfImage(elf)
        disasm = command([args.objdump, '-d', elf])
        (output / f'{name}.disasm').write_text(disasm)
        attributes = command([args.readelf, '-A', '-l', '-S', elf])
        (output / f'{name}.readelf').write_text(attributes)
        required = {
            'math': ['mul'],
            'fptr': ['jalr', '<memcpy>'],
            'float_add': ['fadd.s'],
            'rvv_add': ['vle8.v', 'vwadd.vv', 'vse16.v']
        }[name]
        if not all(op in disasm for op in required):
            raise ValueError(
                f'{name}: expected operations missing in disassembly: {required}'
            )
        if name == 'float_add':
            for symbol in ('input1', 'input2', 'output'):
                image.address(symbol, 32)
        if name == 'rvv_add':
            image.address('output', 2048)
        manifest['tests'][name] = dict(
            source=relative,
            elf=elf.name,
            command=[str(x) for x in argv],
            required_operations=required,
            entry_wrapper=wrapper.read_text(),
            **image.manifest()
        )
    (output /
     'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(
        json.dumps({
            name: {
                'sha256': item['sha256'],
                'segments': item['segments']
            }
            for name, item in manifest['tests'].items()
        },
                   indent=2)
    )


if __name__ == '__main__':
    main()
