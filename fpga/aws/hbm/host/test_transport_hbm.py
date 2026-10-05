"""HBM guard and BAR4 translation checks with no hardware access."""
import unittest
from unittest import mock
import transport_hbm as h
class Worker:
    def __init__(self,reads=None,fail=False):self.reads=reads or {};self.calls=[];self.fail=fail
    def request(self,operation,address,value,deadline):
        self.calls.append((operation,address,value))
        if self.fail:raise TimeoutError('test DMA timeout')
        if operation=='read':return self.reads.get(address,bytes(value) if type(value) is int else 0)
        self.reads[address]=value
class TransportTests(unittest.TestCase):
    def device(self):
        d=h.Device.__new__(h.Device);d.bank=None;d.host_drained=True;d.deadline=float('inf');d.timeout=1;d.slot=0;d.dma=Worker()
        d.ocl=Worker({h.CSR:1,h.CSR+8:1,h.HBM_CSR:h.SIGNATURE,h.HBM_CSR+4:7,h.HBM_CSR+8:0,h.HBM_CSR+12:0})
        return d
    def test_all_bank_dma_offsets(self):
        for bank in range(8):
            d=self.device();d.select_bank(bank);d.write_ddr(0xffffffc0,bytes(64))
            self.assertEqual(d.dma.calls[-1][1],bank*0x80000000+0x7fffffc0)
            self.assertTrue(d.host_drained)
    def test_reject_outstanding_host_or_npu(self):
        for host,counts,status,reset in [(False,0,7,1),(True,1,7,1),(True,256,7,1),(True,0,3,1),(True,0,5,0)]:
            d=self.device();d.host_drained=host;d.ocl.reads[h.HBM_CSR+12]=counts;d.ocl.reads[h.HBM_CSR+4]=status;d.ocl.reads[h.CSR]=reset
            with self.assertRaises(RuntimeError):d.select_bank(1)
            self.assertFalse(any(call[0]=='write' for call in d.ocl.calls))
    def test_timeout_cannot_claim_host_drained(self):
        d=self.device();d.select_bank(0);d.dma.fail=True
        with self.assertRaises(TimeoutError):d.write_ddr(0x80000000,bytes(64))
        self.assertFalse(d.host_drained)
        with self.assertRaises(RuntimeError):d.select_bank(1)
    def test_wait_requires_both_halt_and_idle(self):
        d=self.device();d.select_bank(0);d.wait_halt_and_drain()
        d.ocl.reads[h.HBM_CSR+4]=3;d.timeout=.001
        with self.assertRaises(TimeoutError):d.wait_halt_and_drain()
    def test_bank_crossing_rejected(self):
        d=self.device();d.select_bank(0)
        with self.assertRaises(ValueError):d.write_ddr(0xffffffc0,bytes(128))
if __name__=='__main__':unittest.main()
