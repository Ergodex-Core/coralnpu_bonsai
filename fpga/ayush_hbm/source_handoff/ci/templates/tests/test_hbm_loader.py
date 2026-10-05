import importlib.util
from pathlib import Path
import struct
import tempfile

r=Path('@@CORAL_ROOT@@')
spec=importlib.util.spec_from_file_location('hbm_host',r/'hbm_host.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
entry,segments=m.parse_elf(r/'tests/hbm_vector.elf')
assert entry==0x80000000 and len(segments)==1 and len(segments[0][1])==88
class FakeBar(m.Bar):
    def __init__(self):
        self.mem=bytearray([0xa5]*128)
        self.handle=0
        self.lib=self
    def read(self,a): return int.from_bytes(self.mem[a:a+4],'little')
    def write(self,a,v): self.mem[a:a+4]=int(v).to_bytes(4,'little')
    def fpga_pci_write_burst(self,handle,a,buf,count):
        for i in range(count): self.write(a+4*i,buf[i])
        return 0
for burst in (False,True):
    for address in range(8):
        for length in range(33):
            bar=FakeBar()
            data=bytes((i*17+3)&255 for i in range(length))
            expected=bytearray(bar.mem)
            expected[address:address+length]=data
            bar.write_bytes(address,data,burst=burst)
            assert bar.mem==expected,(burst,address,length)
good=(r/'tests/hbm_vector.elf').read_bytes()
phoff=struct.unpack_from('<I',good,28)[0]
for kind in ('bad-entry','outside-window','not-riscv','truncated'):
    bad=bytearray(good)
    if kind=='bad-entry': struct.pack_into('<I',bad,24,0x80000002)
    elif kind=='outside-window':
        struct.pack_into('<I',bad,phoff+8,0x70000000)
        struct.pack_into('<I',bad,phoff+12,0x70000000)
    elif kind=='not-riscv': struct.pack_into('<H',bad,18,3)
    else: bad=bad[:30]
    with tempfile.NamedTemporaryFile(dir=r/'tests',suffix='.elf') as f:
        f.write(bad);f.flush()
        try: m.parse_elf(f.name)
        except ValueError: pass
        else: raise AssertionError(kind)
print('PASS: HBM ELF parsing, invalid-image rejection, and 528 aligned/unaligned loader writes preserving neighboring bytes')
