#!/usr/bin/env python3
"""Bounded Q8 probe on an already loaded HBM image; no image/lifecycle actions."""
import argparse,fcntl,json,os,struct,time
from pathlib import Path
from run_inference import ElfImage,Device,CSR,hold_reset,in_tcm,read_tcm,write_tcm,read_ddr,write_ddr,verify_loaded_image,cleanup
def execute(device,image,timeout):
    hold_reset(device);device.select_bank(0);device.deadline=time.monotonic()+30
    for s in image.segments:
        addr,data=s['address'],s['data']
        if in_tcm(addr,len(data)):
            write_tcm(device,addr,data);actual=read_tcm(device,addr,len(data))
        else:
            write_ddr(device,addr,data);actual=read_ddr(device,addr,len(data))
        if actual!=data:raise RuntimeError('probe ELF readback mismatch')
    guard=image.address('__stack_guard__');top=image.address('__stack_end__')
    write_tcm(device,guard,bytes([0xa5])*(top-guard))
    device.write(image.address('_ret',4),0x0badd00d)
    device.write(CSR+4,image.entry)
    if device.read(CSR+4)!=image.entry:raise RuntimeError('probe entry readback mismatch')
    device.deadline=time.monotonic()+timeout;begin=time.monotonic();device.write(CSR,0)
    while True:
        status=device.read(CSR+8)
        if status&2:raise RuntimeError('probe core fault')
        if status&1:break
        if time.monotonic()>=device.deadline:raise TimeoutError('probe exceeded deadline')
        time.sleep(.01)
    device.wait_halt_and_drain()
    result=list(struct.unpack('<8I',read_tcm(device,image.address('q8_probe_result',32),32)))
    ret=device.read(image.address('_ret',4))
    if read_tcm(device,guard,64)!=bytes([0xa5])*64:raise RuntimeError('probe stack guard overwritten')
    return {'physical_execution':'COMPLETED','wall_seconds':time.monotonic()-begin,'probe_words':result,
            'return_code':ret,'status':'PASSED' if ret==0 and result[0]==0x51385031 and result[1]==0x50415353 and result[4]==33 and result[6]==1 else 'FAILING'}
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--elf',required=True,type=Path);ap.add_argument('--elf-sha256',required=True)
    ap.add_argument('--report',required=True,type=Path);ap.add_argument('--slot',default=0,type=int)
    ap.add_argument('--expected-agfi');ap.add_argument('--expected-shell');ap.add_argument('--execute-hardware',action='store_true')
    ap.add_argument('--run-timeout',type=float,default=60)
    a=ap.parse_args()
    if not 0<a.run_timeout<=120 or a.slot<0:ap.error('probe timeout must be(0,120] and slot nonnegative')
    if a.execute_hardware and not(a.expected_agfi and a.expected_shell):ap.error('expected image and shell required')
    result={'status':'FAILING','physical_execution':'NOT_RUN','validation':'small Q8 probe only; not full-model evidence'};device=lock=None
    try:
        image=ElfImage(a.elf,a.elf_sha256);image.address('q8_probe_result',32)
        result['firmware']=image.manifest()
        if not a.execute_hardware:result['status']='PREFLIGHT_PASSED'
        else:
            lock=os.open(f'/run/lock/coralnpu-fpga-slot-{a.slot}.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result['image']=verify_loaded_image(a.slot,a.expected_agfi,a.expected_shell)
            device=Device(a.slot,5);result['physical_execution']='STARTED';result.update(execute(device,image,a.run_timeout))
    except Exception as e:result.update(status='FAILING',error=f'{type(e).__name__}: {e}')
    finally:
        if device is not None:cleanup(device,result)
        if lock is not None:os.close(lock)
        a.report.parent.mkdir(parents=True,exist_ok=True);a.report.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result));return 0 if result['status'] in ('PASSED','PREFLIGHT_PASSED') else 1
if __name__=='__main__':raise SystemExit(main())
