#!/usr/bin/env python3
"""Host logic fault-injection tests. These are not RTL or hardware evidence."""
import multiprocessing
import struct
import tempfile
import time
import unittest
from pathlib import Path
from elf_image import ElfImage
from run_physical import (
    CSR, Device, SENTINEL, execute_case, read_bytes, write_bytes
)


class FixtureImage:
    entry = 0
    sha256 = 'fixture-only'
    symbols = {
        '_ret': 0x10000,
        'input1': 0x10010,
        'input2': 0x10030,
        'output': 0x10050,
        '__stack_guard__': 0x12000,
        '__stack_start__': 0x12040,
        '__stack_end__': 0x13040
    }
    segments = [
        dict(address=0, data=bytes(16), size=16),
        dict(address=0x10000, data=bytes(0x3040), size=0x3040)
    ]

    def address(self, name, size=0):
        return self.symbols[name]


class FakeDevice:
    """Emulates expected host-side memory effects, not RISC-V execution."""

    def __init__(self, case='math', failure=None):
        self.memory = {}
        self.case = case
        self.failure = failure
        self.started = False

    def read(self, address):
        value = self.memory.get(address, 0)
        if self.failure == 'readback' and address == 0:
            return value ^ 1
        if self.failure == 'pc' and address == CSR + 4:
            return value ^ 4
        return value

    def write(self, address, value):
        self.memory[address] = value
        if address != CSR:
            return
        if value == 1:
            self.memory[CSR + 8] = 1 if self.failure == 'stale_status' else 0
            return
        self.started = True
        self.memory[CSR + 8] = 2 if self.failure == 'fault' else 1
        if self.failure == 'timeout':
            self.memory[CSR + 8] = 0
        self.memory[0x10000] = SENTINEL if self.failure == 'sentinel' else 0
        if self.failure == 'ret_failure':
            self.memory[0x10000] = 1
        if self.case == 'float_add' and self.failure != 'stale_output':
            first = struct.unpack('<8f', read_bytes(self, 0x10010, 32))
            second = struct.unpack('<8f', read_bytes(self, 0x10030, 32))
            write_bytes(
                self, 0x10050,
                struct.pack('<8f', *[a + b for a, b in zip(first, second)])
            )
        if self.case == 'rvv_add':
            data = [7] * 1024
            if self.failure == 'last_element':
                data[-1] = 6
            write_bytes(self, 0x10050, struct.pack('<1024h', *data))
        if self.failure == 'stack':
            self.memory[0x12000] = 0


def elf_fixture():
    data = bytearray(768)
    data[:16] = b'\x7fELF\x01\x01\x01' + bytes(9)
    struct.pack_into(
        '<HHIIIIIHHHHHH', data, 16, 2, 243, 1, 0, 52, 512, 2, 52, 32, 2, 40, 3,
        0
    )
    struct.pack_into('<8I', data, 52, 1, 128, 0, 0, 16, 16, 5, 4)
    struct.pack_into(
        '<8I', data, 84, 1, 160, 0x10000, 0x10000, 16, 0x1050, 6, 4
    )
    names = b'\0'
    for i, (name, address) in enumerate(
        (('_ret', 0x10000), ('__stack_guard__', 0x10010),
         ('__stack_start__', 0x10050), ('__stack_end__', 0x11050)), 1):
        struct.pack_into(
            '<IIIBBH', data, 192 + i * 16, len(names), address, 0, 0x10, 0, 1
        )
        names += name.encode() + b'\0'
    data[320:320 + len(names)] = names
    struct.pack_into(
        '<10I', data, 552, 0, 3, 0, 0, 320, len(names), 0, 0, 1, 0
    )
    struct.pack_into('<10I', data, 592, 0, 2, 0, 0, 192, 80, 1, 0, 4, 16)
    return data


class HarnessTests(unittest.TestCase):

    def run_case(self, name='math', failure=None, repeat=0):
        result = {}
        execute_case(
            FakeDevice(name, failure), FixtureImage(), name, repeat, 0.005,
            result
        )
        return result

    def test_all_cases_and_repeats(self):
        for name in ('math', 'fptr', 'float_add', 'rvv_add'):
            for repeat in range(3):
                with self.subTest(name=name, repeat=repeat):
                    self.assertEqual(
                        self.run_case(name, repeat=repeat)['status'], 'passed'
                    )

    def test_float_repeat_changes_expected(self):
        self.assertEqual(
            self.run_case('float_add', repeat=0)['actual_outputs'],
            list(range(10, 18))
        )
        self.assertEqual(
            self.run_case('float_add', repeat=1)['actual_outputs'],
            list(range(18, 26))
        )

    def test_all_vector_elements_checked(self):
        with self.assertRaisesRegex(AssertionError, 'every expected'):
            self.run_case('rvv_add', 'last_element')

    def test_stale_output_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'every expected'):
            self.run_case('float_add', 'stale_output')

    def test_halt_is_not_pass(self):
        for failure in ('sentinel', 'ret_failure'):
            with self.subTest(failure=failure
                              ), self.assertRaisesRegex(AssertionError,
                                                        'halt is not success'):
                self.run_case(failure=failure)

    def test_fault_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'core fault'):
            self.run_case(failure='fault')

    def test_loader_readback_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'readback mismatch'):
            self.run_case(failure='readback')

    def test_start_pc_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'start PC'):
            self.run_case(failure='pc')

    def test_stack_guard_rejected(self):
        with self.assertRaisesRegex(AssertionError, 'stack guard'):
            self.run_case(failure='stack')

    def test_reset_and_execution_timeouts(self):
        for failure in ('stale_status', 'timeout'):
            with self.subTest(failure=failure
                              ), self.assertRaises(TimeoutError):
                self.run_case(failure=failure)

    def test_unaligned_writes_preserve_adjacent_bytes(self):
        device = FakeDevice()
        write_bytes(device, 0x10100, bytes([9] * 8))
        write_bytes(device, 0x10101, b'abcde')
        self.assertEqual(read_bytes(device, 0x10100, 8), b'\x09abcde\x09\x09')

    def test_external_memory_rejected(self):
        device = FakeDevice()
        with self.assertRaises(ValueError):
            write_bytes(device, 0x20000000, b'1234')
        with self.assertRaises(ValueError):
            read_bytes(device, 0x20000000, 4)

    def test_sdk_timeout_is_bounded(self):
        # A pipe with a live silent peer reproduces a stuck SDK response.
        device = Device.__new__(Device)
        device.pipe, peer = multiprocessing.Pipe()
        device.timeout = 0.01
        stopped = []
        device.stop = lambda: stopped.append(True)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            device.receive('injected blocked MMIO')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(stopped, [True])
        device.pipe.close()
        peer.close()

    def test_elf_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.elf'
            data = elf_fixture()
            path.write_bytes(data)
            self.assertEqual(ElfImage(path).address('_ret'), 0x10000)
            mutations = [(84 + 8, 0x20000000), (84 + 12, 0x20000000),
                         (84 + 20, 0x8001), (52 + 24, 4), (36, 3), (24, 64)]
            for offset, value in mutations:
                bad = bytearray(data)
                struct.pack_into('<I', bad, offset, value)
                path.write_bytes(bad)
                with self.subTest(offset=offset,
                                  value=value), self.assertRaises(ValueError):
                    ElfImage(path)


if __name__ == '__main__':
    unittest.main()
