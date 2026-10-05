#!/usr/bin/env python3
"""Host loader for the standalone Coral NPU HBM image. Requires the AWS FPGA SDK."""
import argparse
import ctypes as C
import json
from pathlib import Path
import struct
import time

HBM_SIZE = 16 << 30
WINDOW = 2 << 30
CSR = 0x30000
CONFIG = 0x40000
SIGNATURE = 0x48424D31


def check(rc):
    if rc:
        raise RuntimeError(f'AWS FPGA SDK error {rc}')


class Bar:
    def __init__(self, slot, bar):
        self.lib = C.CDLL('/usr/local/lib/libfpga_mgmt.so')
        self.lib.fpga_pci_init.argtypes = []
        self.lib.fpga_pci_attach.argtypes = [C.c_int,C.c_int,C.c_int,C.c_uint32,C.POINTER(C.c_int)]
        self.lib.fpga_pci_detach.argtypes = [C.c_int]
        self.lib.fpga_pci_peek.argtypes = [C.c_int,C.c_uint64,C.POINTER(C.c_uint32)]
        self.lib.fpga_pci_poke.argtypes = [C.c_int,C.c_uint64,C.c_uint32]
        self.lib.fpga_pci_write_burst.argtypes = [C.c_int,C.c_uint64,C.POINTER(C.c_uint32),C.c_uint64]
        check(self.lib.fpga_pci_init())
        self.handle = C.c_int(-1)
        # Uncached mapping keeps register and memory accesses ordered.
        check(self.lib.fpga_pci_attach(slot,0,bar,0,C.byref(self.handle)))

    def close(self):
        if self.handle.value != -1:
            check(self.lib.fpga_pci_detach(self.handle))
            self.handle.value = -1

    def read(self, address):
        value = C.c_uint32()
        check(self.lib.fpga_pci_peek(self.handle,address,C.byref(value)))
        return value.value

    def write(self, address, value):
        check(self.lib.fpga_pci_poke(self.handle,address,value))

    def write_bytes(self, address, data, burst=False):
        if not data:
            return
        # Preserve bytes outside an unaligned segment or file boundary.
        while data and address % 4:
            aligned = address & ~3
            word = bytearray(self.read(aligned).to_bytes(4,'little'))
            count = min(4-address%4,len(data))
            word[address%4:address%4+count] = data[:count]
            self.write(aligned,int.from_bytes(word,'little'))
            address += count
            data = data[count:]
        whole = len(data) & ~3
        if whole and burst:
            buffer = (C.c_uint32*(whole//4)).from_buffer_copy(data[:whole])
            check(self.lib.fpga_pci_write_burst(self.handle,address,buffer,whole//4))
        else:
            for offset in range(0,whole,4):
                self.write(address+offset,int.from_bytes(data[offset:offset+4],'little'))
        if whole < len(data):
            word = bytearray(self.read(address+whole).to_bytes(4,'little'))
            word[:len(data)-whole] = data[whole:]
            self.write(address+whole,int.from_bytes(word,'little'))


def parse_elf(path):
    data = Path(path).read_bytes()
    if len(data)<52 or data[:7]!=b'\x7fELF\x01\x01\x01':
        raise ValueError('Expected little-endian ELF32')
    kind,machine,version,entry,phoff,_,_,ehsize,phentsize,phnum = struct.unpack_from('<HHIIIIIHHH',data,16)
    if (kind,machine,version,ehsize,phentsize)!=(2,243,1,52,32) or phoff+32*phnum>len(data):
        raise ValueError('Expected a valid executable RISC-V ELF32')
    regions = ((0,8192),(0x10000,0x18000),(0x80000000,0x100000000))
    segments = []
    executable = []
    for i in range(phnum):
        typ,off,va,pa,fs,ms,flags,align = struct.unpack_from('<8I',data,phoff+32*i)
        if typ!=1 or ms==0:
            continue
        if va!=pa or fs>ms or off+fs>len(data) or not any(lo<=pa and pa+ms<=hi for lo,hi in regions):
            raise ValueError('ELF segment is outside TCM or the selected 2 GiB HBM window')
        if any(pa < a+n and a < pa+ms for a,_,n in segments):
            raise ValueError('Overlapping ELF load segments are unsupported')
        segments.append((pa,data[off:off+fs],ms))
        if flags&1:
            executable.append((pa,pa+fs))
    if entry%4 or not any(lo<=entry<hi for lo,hi in executable):
        raise ValueError('Entry point must be aligned and in a loaded executable segment')
    return entry,segments


class Device:
    def __init__(self, slot):
        self.control = Bar(slot,0)
        self.memory = None
        if self.control.read(CONFIG)!=SIGNATURE:
            self.control.close()
            raise RuntimeError('Slot does not contain the Coral HBM image')
        deadline = time.monotonic()+30
        while not self.control.read(CONFIG+4)&1:
            if time.monotonic()>deadline:
                self.control.close()
                raise TimeoutError('HBM initialization did not complete')
            time.sleep(.01)
        try:
            self.memory = Bar(slot,4)
        except Exception:
            self.control.close()
            raise

    def close(self):
        if self.memory:
            self.memory.close()
        self.control.close()

    def prepare(self, bank=None):
        c = self.control
        if not (c.read(CSR)&1 or c.read(CSR+8)&1):
            raise RuntimeError('NPU is running; wait for it to halt before changing memory or banks')
        c.write(CSR,1)
        deadline = time.monotonic()+5
        while not c.read(CONFIG+4)&4:
            if time.monotonic()>deadline:
                raise TimeoutError('External memory requests have not drained')
            time.sleep(.001)
        if bank is not None:
            if not 0<=bank<8:
                raise ValueError('HBM bank must be between 0 and 7')
            c.write(CONFIG+8,bank)
            if c.read(CONFIG+8)!=bank:
                raise RuntimeError('HBM bank selection failed')

    def run(self, entry, timeout=30):
        c = self.control
        c.write(CSR+4,entry)
        c.write(CSR,0)
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            status = c.read(CSR+8)
            if status&2:
                raise RuntimeError(f'NPU fault, status={status:#x}')
            if status&1:
                return
            time.sleep(.001)
        raise TimeoutError('NPU did not halt before the timeout')

    def load_elf(self, path, bank):
        entry,segments = parse_elf(path)
        self.prepare(bank)
        for virtual,payload,size in segments:
            external = virtual>=0x80000000
            bar = self.memory if external else self.control
            address = bank*WINDOW+virtual-0x80000000 if external else virtual
            for off in range(0,size,65536):
                count = min(65536,size-off)
                chunk = payload[off:off+count]
                chunk += bytes(count-len(chunk))
                bar.write_bytes(address+off,chunk,burst=external)
            # Readback orders posted PCIe writes before execution starts.
            for off in range(0,len(payload),4):
                count = min(4,len(payload)-off)
                a = address+off
                got = bytes(bar.read(a&~3).to_bytes(4,'little')[a%4:])
                if len(got)<count:
                    got += bar.read((a&~3)+4).to_bytes(4,'little')
                if got[:count]!=payload[off:off+count]:
                    raise RuntimeError(f'ELF readback mismatch at {virtual+off:#x}')
            if size:
                bar.read((address+size-1)&~3)
        return entry


def smoke(device):
    program = Path(__file__).parent/'tests/hbm_vector.elf'
    for bank in (0,7):
        entry = device.load_elf(program,bank)
        base = bank*WINDOW
        for offset,values in ((0x10000,range(16384)),(0x30000,(3*i+7 for i in range(16384))),(0x50000,[0]*16384)):
            device.memory.write_bytes(base+offset,b''.join(struct.pack('<I',v) for v in values),burst=True)
        device.memory.read(base+0x5fffc)
        device.control.write(0x10000,0)
        device.run(entry)
        for i in range(16384):
            got = device.memory.read(base+0x50000+4*i)
            if got!=4*i+7:
                raise RuntimeError(f'Bank {bank}, vector element {i}: {got}, expected {4*i+7}')
        if device.control.read(0x10000)!=0x51:
            raise RuntimeError('Completion marker missing')
        print(f'PASS: bank {bank}, HBM instruction fetch and 16384-element vector addition',flush=True)
    for i in range(16384):
        if device.memory.read(0x50000+4*i)!=4*i+7:
            raise RuntimeError('High HBM bank aliased bank zero')
    print('PASS: HBM bank isolation',flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slot',type=int,default=0)
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    sub.add_parser('smoke')
    e = sub.add_parser('run-elf')
    e.add_argument('file');e.add_argument('--bank',type=int,choices=range(8),default=0)
    e.add_argument('--timeout',type=float,default=30)
    load = sub.add_parser('load-data')
    load.add_argument('file');load.add_argument('--offset',type=lambda s:int(s,0),required=True)
    load.add_argument('--verify',action='store_true')
    read = sub.add_parser('read')
    read.add_argument('offset',type=lambda s:int(s,0))
    args = parser.parse_args()
    if args.command=='run-elf':
        parse_elf(args.file)
    if args.command=='load-data' and not 0<=args.offset<=HBM_SIZE-Path(args.file).stat().st_size:
        parser.error('File does not fit within 16 GiB HBM')
    if args.command=='read' and (args.offset%4 or not 0<=args.offset<=HBM_SIZE-4):
        parser.error('Read offset must be word-aligned within HBM')
    device = Device(args.slot)
    try:
        if args.command=='status':
            c=device.control
            print(json.dumps(dict(hbm_ready=bool(c.read(CONFIG+4)&1),bank=c.read(CONFIG+8),reset=c.read(CSR),pc=c.read(CSR+4),status=c.read(CSR+8),outstanding=c.read(CONFIG+12)),indent=2))
        elif args.command=='smoke':
            smoke(device)
        elif args.command=='run-elf':
            entry=device.load_elf(args.file,args.bank)
            device.run(entry,args.timeout)
            print('NPU halted successfully')
        elif args.command=='read':
            print(f'{device.memory.read(args.offset):#010x}')
        else:
            device.prepare()
            address=args.offset
            with open(args.file,'rb') as stream:
                while chunk:=stream.read(65536):
                    device.memory.write_bytes(address,chunk,burst=True)
                    if args.verify:
                        for i,b in enumerate(chunk):
                            if i==0 or (address+i)%4==0:
                                word=device.memory.read((address+i)&~3)
                            if ((word>>(8*((address+i)%4)))&255)!=b:
                                raise RuntimeError(f'HBM readback mismatch at {address+i:#x}')
                    address+=len(chunk)
            if address>args.offset:
                device.memory.read((address-1)&~3)
            print(f'Loaded {address-args.offset} bytes into HBM at {args.offset:#x}')
    finally:
        device.close()


if __name__=='__main__':
    main()
