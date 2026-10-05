"""Rebuild the omitted RVV test ELF with explicit LLVM 18.1.3 tools."""
import argparse
from pathlib import Path

from inputs import PINS, capture, sha
from process import require, run


def build(directory, clang, linker):
    directory = Path(directory)
    require(
        'clang version ' + PINS['llvm_version']
        in capture([clang, '--version']), 'Fixture requires clang 18.1.3'
    )
    require(
        'LLD ' + PINS['llvm_version'] in capture([linker, '--version']),
        'Fixture requires LLD 18.1.3'
    )
    obj, elf = directory / 'hbm_vector.o', directory / 'hbm_vector.elf'
    require(
        not obj.exists() and not elf.exists(), 'Fixture output already exists'
    )
    run([
        clang, '--target=riscv32-unknown-elf', '-march=rv32im_zve32x_zicsr',
        '-mabi=ilp32', '-mno-relax', '-c',
        str(directory / 'hbm_vector.S'), '-o',
        str(obj)
    ],
        directory / 'fixture-compile.log',
        directory,
        timeout=60)
    run([
        linker, '-m', 'elf32lriscv', '--no-relax', '-T',
        str(directory / 'hbm.ld'),
        str(obj), '-o',
        str(elf)
    ],
        directory / 'fixture-link.log',
        directory,
        timeout=60)
    return {
        'elf_sha256': sha(elf),
        'assembly_sha256': sha(directory / 'hbm_vector.S'),
        'linker_script_sha256': sha(directory / 'hbm.ld'),
        'compiler': capture([clang, '--version']),
        'linker': capture([linker, '--version'])
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--clang', default='clang-18')
    parser.add_argument('--linker', default='ld.lld-18')
    args = parser.parse_args()
    print(build(args.directory.resolve(), args.clang, args.linker))
