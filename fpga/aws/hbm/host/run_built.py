#!/usr/bin/env python3
"""Launch only a source-matched strict HBM build; hardware remains opt-in."""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[3]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build',type=Path,required=True);ap.add_argument('--report',type=Path,required=True)
    ap.add_argument('--package',type=Path);ap.add_argument('--inputs',type=Path);ap.add_argument('--model',choices=['q8','pq2'])
    ap.add_argument('--count',type=int,choices=[1,4],default=1);ap.add_argument('--slot',type=int,default=0)
    ap.add_argument('--execute-hardware',action='store_true');ap.add_argument('--expected-agfi');ap.add_argument('--expected-shell')
    ap.add_argument('--run-timeout',type=float,default=900);ap.add_argument('--load-timeout',type=float,default=600)
    a=ap.parse_args();meta=json.loads((a.build/'manifest.json').read_text())
    if meta.get('toolchain_qualification')!='PINNED_18.1.3' or meta.get('memory_profile')!='hbm':raise ValueError('strict LLVM18.1.3 HBM build required')
    for name,digest in meta['sources'].items():
        path=(ROOT/name).resolve()
        if not path.is_relative_to(ROOT) or sha(path)!=digest:raise ValueError('build source mismatch: '+name)
    if a.execute_hardware and not(a.expected_agfi and a.expected_shell):ap.error('expected AGFI/shell required')
    elf=a.build/meta['elf']
    if sha(elf)!=meta['sha256']:raise ValueError('built ELF hash mismatch')
    args=['--elf',str(elf),'--elf-sha256',meta['sha256'],'--report',str(a.report),'--slot',str(a.slot)]
    if a.execute_hardware:args+=['--execute-hardware','--expected-agfi',a.expected_agfi,'--expected-shell',a.expected_shell]
    if meta['runtime_kind']=='q8_probe':
        if not meta.get('q8_rvv'):raise ValueError('probe must exercise explicit RVV')
        entry=HERE/'run_probe.py';args+=['--run-timeout',str(min(a.run_timeout,120))]
    else:
        if not(a.package and a.inputs and a.model):ap.error('decoder requires package, inputs and model')
        if not 0<a.run_timeout<=1800 or not 0<a.load_timeout<=900:ap.error('model run<=1800s, load<=900s required')
        if a.model=='q8' and not meta.get('q8_rvv'):raise ValueError('Q8 physical request requires explicit RVV build')
        receipt=json.loads((a.inputs/'input-receipt.json').read_text())[a.model]
        package=json.loads((a.package/'manifest.json').read_text())
        if package['segments'][0]!=receipt['package']:raise ValueError('package differs from pinned reference input')
        name=f'{a.model}-hello-{a.count}.logits.f32';ref=a.inputs/name
        args+=['--package',str(a.package),'--tokens-json',str(a.inputs/'tokens-hello.json'),
               '--eos-tokens-json',str(a.inputs/'eos.json'),'--max-new-tokens',str(a.count),
               '--reference-logits',str(ref),'--reference-sha256',receipt['files'][name]['sha256'],
               '--run-timeout',str(a.run_timeout),'--load-timeout',str(a.load_timeout)]
        entry=HERE/'run_inference.py'
    return subprocess.call([sys.executable,str(entry),*args])
if __name__=='__main__':raise SystemExit(main())
