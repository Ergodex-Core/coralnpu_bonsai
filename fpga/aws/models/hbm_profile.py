"""Pure address/guard helpers for the supplied Ayush2026_10_05-161524 ABI.

No PCI, DMA, AWS or lifecycle operations. Callers must read back staged bytes
before releasing reset and must wait for halt/drain before collecting results.
The caller supplies host_drained from its own host-traffic accounting: the NPU
read/write count CSR cannot establish completion of host DMA.
"""
NPU_BASE = 0x80000000
BANK_BYTES = 0x80000000
NPU_END = 0x100000000
BAR4_BYTES = 0x400000000
BANKS = 8
SIGNATURE = 0x48424D31
CSR = {'signature':0x40000,'status':0x40004,'bank':0x40008,'counts':0x4000c,
       'control':0x30000,'start_pc':0x30004,'core_status':0x30008}

def transfer_offset(bank, address, size):
    if any(type(x) is not int for x in (bank,address,size)):
        raise ValueError('integer bank/address/size required')
    if not 0 <= bank < BANKS or not NPU_BASE <= address < NPU_END or size <= 0:
        raise ValueError('invalid HBM range')
    # Python integers deliberately preserve end == 2**32 and never wrap.
    if address + size > NPU_END:
        raise ValueError('transfer crosses the selected NPU bank')
    offset = bank * BANK_BYTES + address - NPU_BASE
    if offset + size > BAR4_BYTES:
        raise ValueError('transfer outside BAR4')
    return offset

def bank_change_guard(signature, status, counts, *, reset_asserted, host_drained):
    if signature != SIGNATURE: raise ValueError('HBM ABI signature mismatch')
    if not status & 1: raise ValueError('HBM not ready')
    if reset_asserted is not True and not status & 2: raise ValueError('NPU must be reset or halted')
    if not status & 4 or counts & 0xffff: raise ValueError('NPU traffic not drained')
    if host_drained is not True: raise ValueError('host traffic not independently drained')
    return True
