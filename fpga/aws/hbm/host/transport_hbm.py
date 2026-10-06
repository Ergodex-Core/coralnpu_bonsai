"""Supplied HBM ABI adapter; no device is opened by importing this module."""
import sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'ddr/host'))
from transport import Device as DDRDevice, Worker, CHUNK_BYTES, CSR, in_tcm, read_tcm, write_tcm, hold_reset

DDR_BASE = 0x80000000
DDR_BYTES = 0x80000000
HBM_CSR = 0x40000
SIGNATURE = 0x48424d31


def ddr_offset(address, size):
    if type(address) is not int or type(
            size
    ) is not int or size < 0 or not DDR_BASE <= address < 0x100000000 or address + size > 0x100000000:
        raise ValueError('HBM NPU range exceeds one2GiB bank')
    return address - DDR_BASE


def check_ddr(device):
    if device.read(HBM_CSR) != SIGNATURE:
        raise RuntimeError('HBM ABI signature mismatch')
    if not device.read(HBM_CSR + 4) & 1: raise RuntimeError('HBM is not ready')
    if device.read(HBM_CSR + 8) != device.bank:
        raise RuntimeError('HBM bank changed unexpectedly')


class Device(DDRDevice):

    def __init__(self, slot, timeout=5):
        super().__init__(slot, timeout)
        self.bank = None
        self.host_drained = True

    def select_bank(self, bank):
        if type(bank) is not int or not 0 <= bank < 8:
            raise ValueError('HBM bank0..7 required')
        if self.read(HBM_CSR) != SIGNATURE:
            raise RuntimeError('HBM ABI signature mismatch')
        status = self.read(HBM_CSR + 4)
        if not status & 1 or not status & 4:
            raise RuntimeError('HBM must be ready and idle')
        if not (self.read(CSR) & 1 or status & 2):
            raise RuntimeError('bank change requires reset/halt')
        if self.read(HBM_CSR + 12) & 0xffff or not self.host_drained:
            raise RuntimeError('NPU and host traffic must both be drained')
        self.ocl.request('write', HBM_CSR + 8, bank, self.deadline)
        if self.read(HBM_CSR + 8) != bank:
            raise RuntimeError('HBM bank readback failed')
        self.bank = bank

    def _transfer(self, operation, address, value):
        size = value if operation == 'read' else len(value)
        if self.bank is None:
            raise RuntimeError('HBM bank has not been selected')
        offset = self.bank * DDR_BYTES + ddr_offset(address, size)
        if address % 64 or not 0 < size <= CHUNK_BYTES or size % 64:
            raise ValueError('DMA requires64-byte alignment/size and <=1MiB')
        if self.dma is None: self.dma = Worker(self.slot, 'dma', self.timeout)
        self.host_drained = False
        result = self.dma.request(operation, offset, value, self.deadline)
        # A failed/timed-out request deliberately leaves this false. NPU counts
        # cannot prove completion of outstanding host DMA after such a failure.
        self.host_drained = True
        return result

    def wait_halt_and_drain(self):
        deadline = min(self.deadline, time.monotonic() + self.timeout)
        while True:
            status = self.read(HBM_CSR + 4)
            if self.read(CSR + 8) & 1 and status & 6 == 6 and self.read(
                    HBM_CSR + 12) & 0xffff == 0 and self.host_drained:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('halt/drain deadline exceeded')
            time.sleep(.001)
