#!/usr/bin/env python3
"""Strict LLVM18.1.3 compile/disassembly check; never executes board operations.

The relocatable object is NOT a qualified runnable ELF. The owner must link it
with the qualified CRT/profile, inspect complete ELF/stack and execute the probe.
"""
import argparse,hashlib,json,re,shutil,subprocess
from pathlib import Path
HERE=Path(__file__).resolve().parent
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def run(argv):return subprocess.check_output(list(map(str,argv)),stderr=subprocess.STDOUT,text=True)
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--clang',default='clang');ap.add_argument('--objdump',default='llvm-objdump')
    ap.add_argument('--readelf',default='llvm-readelf');ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    tools={}
    for key in ['clang','objdump','readelf']:
        path=shutil.which(getattr(args,key))
        if not path:raise ValueError('Missing strict tool: '+key)
        version=run([path,'--version'])
        if not re.search(r'\b18\.1\.3\b',version):raise ValueError('Strict LLVM18.1.3 required: '+key)
        tools[key]={'path':path,'sha256':digest(path),'version':version}
    flags=['--target=riscv32-unknown-elf','-march=rv32imf_zicsr_zifencei_zve32f_zvl128b',
           '-mabi=ilp32f','-mno-relax','-mcmodel=medany','-msmall-data-limit=0','-Oz','-std=c11',
           '-ffreestanding','-fno-builtin','-ffp-contract=off','-fno-fast-math',
           '-fno-vectorize','-fno-slp-vectorize','-fno-unwind-tables','-fno-asynchronous-unwind-tables',
           '-Wall','-Wextra','-Werror','-DCM_Q8_RVV','-fstack-usage']
    obj=out/'q8-probe.o';command=[tools['clang']['path'],*flags,'-c',HERE/'q8_target_probe.c','-o',obj]
    (out/'compile.log').write_text(run(command))
    asm=run([tools['objdump']['path'],'-d',obj]);(out/'probe.disasm').write_text(asm)
    attrs=run([tools['readelf']['path'],'-A','-s',obj]);(out/'probe.readelf').write_text(attrs)
    for op in ['vle8.v','vwmul.vv','vwredsum.vs']:
        if op not in asm:raise ValueError('Expected vector operation absent: '+op)
    if re.search(r'\b(?:fmadd|fmsub|fnmadd|fnmsub)\.s\b|\bvfmacc\.',asm):
        raise ValueError('FMA violates this profile')
    undefined=[line for line in attrs.splitlines() if re.search(r'\bUND\b',line) and line.split()[-1]!='UND']
    receipt={'evidence':'strict compile/disassembly only; target execution NOT_RUN',
             'tools':tools,'command':list(map(str,command)),'object_sha256':digest(obj),
             'sources':{p.name:digest(p) for p in [HERE/'q8_target_probe.c',HERE/'q8_0.h']},
             'undefined_symbol_lines':undefined,'owner_must_review':'final ELF ISA/helper closure, stack usage, result after actual halt/drain'}
    (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))
if __name__=='__main__':main()
