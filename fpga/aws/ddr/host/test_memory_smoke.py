"""Memory-smoke protocol tests with an independent RV32I subset interpreter.

Fake memory and interpreted instructions establish no physical FPGA evidence.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import run_memory_smoke as smoke
from transport import CSR, DDR_BASE, DDR_BYTES, DDR_CSR, ddr_offset


class FakeDevice:

    def __init__(
        self,
        alias=False,
        bad_byte_store=False,
        never_halt=False,
        fault=False
    ):
        self.memory, self.control, self.status, self.pc = {}, 1, 0, 0
        self.ddr_status, self.deadline, self.closed = 3, float('inf'), False
        self.alias, self.bad_byte_store = alias, bad_byte_store
        self.never_halt, self.fault = never_halt, fault
        self.dma_calls = []

    def get(self, address, size):
        return bytes(self.memory.get(address + i, 0) for i in range(size))

    def put(self, address, data):
        self.memory.update((address + i, value)
                           for i, value in enumerate(data))

    def read(self, address):
        if time.monotonic() > self.deadline:
            raise TimeoutError('fake device deadline')
        values = {
            CSR: self.control,
            CSR + 4: self.pc,
            CSR + 8: self.status,
            DDR_CSR: 0x43444452,
            DDR_CSR + 4: self.ddr_status,
            DDR_CSR + 8: DDR_BYTES,
            DDR_CSR + 12: DDR_BASE,
            DDR_CSR + 16: 1
        }
        return values[address] if address in values else int.from_bytes(
            self.get(address, 4), 'little'
        )

    def write(self, address, value):
        if address == CSR:
            self.control = value
            if value:
                self.status = 0
            elif self.fault:
                self.status = 2
            elif not self.never_halt:
                self.interpret()
        elif address == CSR + 4:
            self.pc = value
        else:
            self.put(address, value.to_bytes(4, 'little'))

    def dma_address(self, address, size):
        ddr_offset(address, size)
        if address % 64 or size % 64 or not 0 < size <= 1 << 20:
            raise ValueError('unaligned fake DMA request')
        self.dma_calls.append((address, size))
        return DDR_BASE if self.alias and address == DDR_BASE + DDR_BYTES - 64 else address

    def read_ddr(self, address, size):
        return self.get(self.dma_address(address, size), size)

    def write_ddr(self, address, data):
        self.put(self.dma_address(address, len(data)), data)

    def close(self):
        self.closed = True

    def interpret(self):
        regs, pc = [0] * 32, self.pc

        def signed(value, bits):
            return value - (1 << bits) if value & (1 << (bits - 1)) else value

        for _ in range(32):
            word = int.from_bytes(self.get(pc, 4), 'little')
            op, rd, rs1, rs2, kind = word & 127, word >> 7 & 31, word >> 15 & 31, word >> 20 & 31, word >> 12 & 7
            next_pc = pc + 4
            if word == 0x08000073:
                self.status = 1
                return
            if op == 0x37:  # LUI
                regs[rd] = word & 0xfffff000
            elif op == 0x67 and kind == 0:  # JALR
                next_pc = (regs[rs1] + signed(word >> 20, 12)) & ~1
                regs[rd] = pc + 4
            elif op == 0x03 and kind == 2:  # LW
                address = regs[rs1] + signed(word >> 20, 12)
                regs[rd] = int.from_bytes(self.get(address, 4), 'little')
            elif op == 0x13 and kind == 0:  # ADDI
                regs[rd] = (regs[rs1] + signed(word >> 20, 12)) & 0xffffffff
            elif op == 0x23 and kind in (0, 2):  # SB/SW
                address = regs[rs1] + signed((word >> 25 << 5) |
                                             (word >> 7 & 31), 12)
                size = 4 if kind == 2 or self.bad_byte_store else 1
                self.put(address, regs[rs2].to_bytes(4, 'little')[:size])
            elif op != 0x0f:  # FENCE
                raise AssertionError(f'unexpected instruction {word:#x}')
            regs[0], pc = 0, next_pc
        raise AssertionError('fake instruction limit exceeded')


class MemorySmokeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.report = Path(self.tmp.name) / 'report.json'

    def test_distinct_patterns_boundaries_and_core_instructions(self):
        device, report = FakeDevice(), {}
        smoke.execute(device, report, core=True)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual([row['actual'] for row in report['core_checks']],
                         [42, 42, 0xaabb2add])
        self.assertEqual(len(report['core_inputs']), 3)
        self.assertEqual(report['host_partial_write_preservation'], 'PASS')
        self.assertEqual(len(report['client_bounds']), 2)
        self.assertTrue(
            all(
                DDR_BASE <= address and address + size <= DDR_BASE + DDR_BYTES
                for address, size in device.dma_calls
            )
        )
        self.assertNotIn('core_ttft_seconds', report)

    def test_address_aliasing_cannot_pass(self):
        with self.assertRaisesRegex(AssertionError, 'pattern mismatch'):
            smoke.execute(FakeDevice(alias=True), {})

    def test_core_byte_strobes_must_preserve_neighbors(self):
        report = {}
        with self.assertRaisesRegex(AssertionError,
                                    'core DDR result mismatch'):
            smoke.execute(FakeDevice(bad_byte_store=True), report, core=True)
        self.assertEqual(report['core_checks'][-1]['expected'], 0xaabb2add)
        self.assertNotEqual(report['core_checks'][-1]['actual'], 0xaabb2add)

    def test_ddr_fault_core_fault_and_timeout_fail(self):
        device = FakeDevice()
        device.ddr_status = 7
        with self.assertRaisesRegex(RuntimeError, 'sticky fault'):
            smoke.execute(device, {})
        with self.assertRaisesRegex(AssertionError, 'core fault'):
            smoke.execute(FakeDevice(fault=True), {}, core=True)
        with self.assertRaises(TimeoutError):
            smoke.execute(
                FakeDevice(never_halt=True), {}, core=True, timeout=0.02
            )

    def test_preflight_never_attaches_or_checks_image(self):
        with mock.patch.object(smoke, 'Device', side_effect=AssertionError(
                'hardware touched')), mock.patch.object(
                    smoke, 'verify_loaded_image',
                    side_effect=AssertionError('image tool called')):
            rc = smoke.main(['--report', str(self.report), '--core-smoke'])
        report = json.loads(self.report.read_text())
        self.assertEqual(rc, 0)
        self.assertEqual(report['physical_execution'], 'NOT_RUN')
        self.assertEqual(report['status'], 'PREFLIGHT_PASSED')

    def test_cli_failure_preserves_reset_and_closes_slot(self):
        device = FakeDevice(alias=True)
        with mock.patch.object(
                smoke, 'Device', return_value=device), mock.patch.object(
                    smoke, 'verify_loaded_image',
                    return_value={'image': 'fake-only'}), mock.patch.object(
                        smoke.os, 'open', return_value=123), mock.patch.object(
                            smoke.os, 'close') as close, mock.patch.object(
                                smoke.fcntl, 'flock') as lock:
            rc = smoke.main([
                '--report',
                str(self.report), '--execute-hardware', '--expected-agfi',
                'agfi-1234', '--expected-shell', '0x1'
            ])
        report = json.loads(self.report.read_text())
        self.assertEqual(rc, 1)
        self.assertEqual(report['status'], 'FAILING')
        self.assertEqual(report['cleanup_reset'], 'PASSED')
        self.assertEqual(device.control, 1)
        self.assertTrue(device.closed)
        lock.assert_called_once()
        close.assert_called_once_with(123)

    def test_wrong_image_prevents_device_attachment(self):
        with mock.patch.object(smoke, 'Device', side_effect=AssertionError(
                'hardware touched')), mock.patch.object(
                    smoke, 'verify_loaded_image',
                    side_effect=ValueError('wrong image')), mock.patch.object(
                        smoke.os, 'open', return_value=123), mock.patch.object(
                            smoke.os, 'close'), mock.patch.object(smoke.fcntl,
                                                                  'flock'):
            rc = smoke.main([
                '--report',
                str(self.report), '--execute-hardware', '--expected-agfi',
                'agfi-1234', '--expected-shell', '0x1'
            ])
        self.assertEqual(rc, 1)
        self.assertEqual(
            json.loads(self.report.read_text())['physical_execution'],
            'NOT_RUN'
        )


if __name__ == '__main__':
    unittest.main()
