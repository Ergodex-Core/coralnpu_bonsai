"""Host protocol tests only: fake device results are never model evidence."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest import mock

import run_inference as run
from transport import CSR, DDR_BASE, DDR_CSR, Worker, ddr_offset


def make_elf(path):
    names = b'\0coral_mailbox\0_ret\0__stack_guard__\0__stack_start__\0__stack_end__\0'
    values = [('coral_mailbox', 0x10000, 128), ('_ret', 0x10080, 4),
              ('__stack_guard__', 0x10100, 0), ('__stack_start__', 0x10140, 0),
              ('__stack_end__', 0x11140, 0)]
    data = bytearray(0x200)
    data[:16] = b'\x7fELF\x01\x01\x01' + bytes(9)
    strings = len(data)
    data.extend(names)
    data.extend(bytes((-len(data)) % 4))
    symoff = len(data)
    data.extend(bytes(16))
    for name, value, size in values:
        data.extend(
            struct.pack(
                '<IIIBBH', names.index(name.encode()), value, size, 0x10, 0, 1
            )
        )
    shoff = len(data)
    data.extend(bytes(40))
    data.extend(struct.pack('<10I', 0, 1, 3, 0x10000, 0x104, 132, 0, 0, 4, 0))
    data.extend(
        struct.pack('<10I', 0, 3, 0, 0, strings, len(names), 0, 0, 1, 0)
    )
    data.extend(struct.pack('<10I', 0, 2, 0, 0, symoff, 96, 2, 0, 4, 16))
    struct.pack_into(
        '<HHIIIIIHHHHHH', data, 16, 2, 243, 1, 0, 52, shoff, 2, 52, 32, 3, 40,
        4, 0
    )
    struct.pack_into('<8I', data, 52, 1, 0x100, 0, 0, 4, 4, 5, 4)
    struct.pack_into(
        '<8I', data, 84, 1, 0x104, 0x10000, 0x10000, 132, 0x1140, 6, 4
    )
    struct.pack_into(
        '<8I', data, 116, 1, 0x188, DDR_BASE, DDR_BASE, 4, 64, 5, 4
    )
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class FakeDevice:

    def __init__(self, plan, corrupt=False, never_halt=False, bad_token=False):
        self.plan, self.memory = plan, {}
        self.control, self.status = 1, 0
        self.ddr_status = 3
        self.corrupt, self.never_halt, self.bad_token = corrupt, never_halt, bad_token
        self.deadline = float('inf')
        self.closed = False

    def get(self, address, size):
        return bytes(self.memory.get(address + i, 0) for i in range(size))

    def put(self, address, data):
        self.memory.update((address + i, b) for i, b in enumerate(data))

    def read(self, address):
        if time.monotonic() > self.deadline:
            raise TimeoutError('fake deadline')
        constants = {
            CSR: self.control,
            CSR + 8: self.status,
            DDR_CSR: 0x43444452,
            DDR_CSR + 4: self.ddr_status,
            DDR_CSR + 8: 0x80000000,
            DDR_CSR + 12: DDR_BASE,
            DDR_CSR + 16: 1
        }
        return constants[address] if address in constants else int.from_bytes(
            self.get(address, 4), 'little'
        )

    def write(self, address, value):
        self.put(address, value.to_bytes(4, 'little'))
        if address == CSR:
            self.control = value
            if value == 1:
                self.status = 0
            elif not self.never_halt:
                self.complete()

    def read_ddr(self, address, size):
        ddr_offset(address, size)
        data = self.get(address, size)
        return bytes([data[0] ^ 1]) + data[1:] if self.corrupt else data

    def write_ddr(self, address, data):
        ddr_offset(address, len(data))
        self.put(address, data)

    def complete(self):
        m = list(struct.unpack('<32I', self.get(0x10000, 128)))
        generated = []
        rows = []
        for i in range(max(1, self.plan.max_new_tokens)):
            token = 1 + (i % 2)
            rows.extend([
                0., 3. if token == 1 else 1., 4. if token == 2 else 1.
            ])
            if self.plan.max_new_tokens:
                generated.append(token)
                if token in self.plan.eos_tokens:
                    break
        self.put(
            self.plan.logits_address, struct.pack(f'<{len(rows)}f', *rows)
        )
        self.put(
            self.plan.generated_address,
            struct.pack(
                f'<{len(generated)}I',
                *([0] * len(generated) if self.bad_token else generated)
            )
        )
        m[2], m[3], m[12], m[13] = 2, 0, len(
            self.plan.tokens
        ) + max(0,
                len(generated) - 1), 1 + (max(1, len(generated)) - 1) % 2
        m[20] = len(generated)
        m[21] = (
            2 if generated[-1] in self.plan.eos_tokens else 1
        ) if generated else 0
        m[22], m[24], m[26], m[28] = 100, 50 if len(
            generated
        ) > 1 else 0, 200, 110 if generated else 0
        self.put(0x10000, struct.pack('<32I', *m))
        self.put(self.plan.image.address('_ret'), bytes(4))
        self.status = 1

    def close(self):
        self.closed = True


def unresponsive_worker(pipe, slot, mode, buffer):
    pipe.send((True, None))
    pipe.recv()
    time.sleep(30)


class HostTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.elf = self.root / 'firmware.elf'
        self.elf_sha = make_elf(self.elf)
        config = dict(
            dim=4,
            hidden_dim=8,
            n_layers=1,
            n_heads=1,
            n_kv_heads=1,
            head_dim=4,
            vocab=3,
            max_seq=4,
            flags=3
        )
        data = bytearray(192)
        data[:8] = b'CORALM01'
        struct.pack_into(
            '<14I', data, 8, 1, 128, 192, 1, 128, 4, 8, 1, 1, 1, 4, 3, 4, 3
        )
        (self.root / 'model.bin').write_bytes(data)
        self.manifest = dict(
            schema='coral-model-package-v1',
            model_id='fake-only',
            source_revision='fixture',
            config=config,
            memory=dict(max_seq=4, workspace_bytes=328),
            segments=[
                dict(
                    file='model.bin',
                    address=0x21000000,
                    bytes=192,
                    sha256=hashlib.sha256(data).hexdigest()
                )
            ]
        )
        self.save_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def save_manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def plan(self, **kwargs):
        return run.Plan(self.root, self.elf, self.elf_sha, [0], **kwargs)

    def test_generation_full_rows_and_metrics(self):
        reference = self.root / 'reference.f32'
        reference.write_bytes(struct.pack('<6f', 0, 3, 1, 0, 1, 4))
        plan = self.plan(
            max_new_tokens=2,
            reference=reference,
            reference_sha256=run.digest_file(reference)
        )
        device, result = FakeDevice(plan), {}
        actual = run.execute(device, plan, result)
        self.assertEqual(actual, reference.read_bytes())
        self.assertEqual(result['status'], 'PASSED')
        self.assertEqual(result['generated_tokens'], [1, 2])
        self.assertEqual(result['metrics']['decode_tokens'], 1)
        run.cleanup(device, result)
        self.assertEqual(device.control, 1)
        self.assertTrue(device.closed)

    def test_prompt_only(self):
        plan = self.plan(max_new_tokens=0)
        result = {}
        run.execute(FakeDevice(plan), plan, result)
        self.assertEqual(result['generated_tokens'], [])
        self.assertEqual(result['status'], 'EXECUTED_UNVERIFIED')

    def test_eos_stops_early(self):
        plan = self.plan(max_new_tokens=3, eos_tokens=[1])
        result = {}
        run.execute(FakeDevice(plan), plan, result)
        self.assertEqual(result['generated_tokens'], [1])
        self.assertEqual(result['stop_reason'], 'eos')

    def test_bad_generated_token_rejected(self):
        plan = self.plan()
        with self.assertRaisesRegex(AssertionError, 'differs'):
            run.execute(FakeDevice(plan, bad_token=True), plan, {})

    def test_readback_corruption(self):
        plan = self.plan()
        with self.assertRaisesRegex(AssertionError, 'readback'):
            run.execute(FakeDevice(plan, corrupt=True), plan, {})

    def test_calibration_and_sticky_fault(self):
        plan = self.plan()
        for status in (0, 2, 7):
            device = FakeDevice(plan)
            device.ddr_status = status
            with self.assertRaisesRegex(RuntimeError, 'calibrated'):
                run.execute(device, plan, {})

    def test_execution_timeout_cleanup(self):
        plan = self.plan()
        device, result = FakeDevice(plan, never_halt=True), {}
        with self.assertRaises(TimeoutError):
            run.execute(device, plan, result, run_timeout=0.001)
        run.cleanup(device, result)
        self.assertEqual(device.control, 1)

    def test_sdk_timeout_is_bounded(self):
        worker = Worker(0, 'ocl', timeout=0.2, target=unresponsive_worker)
        begin = time.monotonic()
        with self.assertRaises(TimeoutError):
            worker.request('read', 0, None, begin + 1)
        self.assertLess(time.monotonic() - begin, 3)
        self.assertFalse(worker.process.is_alive())

    def test_model_corruption(self):
        (self.root / 'model.bin').write_bytes(bytes(192))
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            self.plan()

    def test_model_mutation_after_preflight(self):
        plan = self.plan()
        (self.root / 'model.bin').write_bytes(bytes(192))
        with self.assertRaisesRegex(ValueError, 'changed'):
            run.load_model(FakeDevice(plan), plan)

    def test_bad_layout_and_tokens(self):
        for address in (0x20000000, 0x21000001, 0xa0000000):
            self.manifest['segments'][0]['address'] = address
            self.save_manifest()
            with self.assertRaises(ValueError):
                self.plan()
        self.manifest['segments'][0]['address'] = 0x21000000
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'KV capacity'):
            run.Plan(
                self.root, self.elf, self.elf_sha, [0, 1], max_new_tokens=4
            )

    def test_manifest_header_mismatch(self):
        self.manifest['config']['vocab'] = 4
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'configuration differ'):
            self.plan()

    def test_elf_hash_and_abi(self):
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            run.Plan(self.root, self.elf, '0' * 64, [0])
        data = bytearray(self.elf.read_bytes())
        struct.pack_into('<I', data, 36, 3)
        self.elf.write_bytes(data)
        self.elf_sha = run.digest_file(self.elf)
        with self.assertRaisesRegex(ValueError, 'executable RV32'):
            self.plan()

    def test_ddr_partial_write_preserves_neighbors(self):
        device = FakeDevice(self.plan())
        device.put(DDR_BASE, b'Z' * 128)
        run.write_ddr(device, DDR_BASE + 3, b'hello')
        self.assertEqual(device.get(DDR_BASE, 10), b'ZZZhelloZZ')

    def test_reference_every_logit_and_token(self):
        result = run.compare_logits(
            struct.pack('<3f', 0, 3, 1), struct.pack('<3f', 0, 3, 2), 0, 0
        )
        self.assertEqual(result['reference_check'], 'FAILING')
        with self.assertRaisesRegex(AssertionError, 'NaN'):
            run.compare_logits(bytes([255]) * 12, None, 0, 0)

    def test_preflight_never_attaches(self):
        tokens = self.root / 'tokens.json'
        tokens.write_text('[0]')
        with mock.patch.object(run, 'Device',
                               side_effect=AssertionError('hardware touched')):
            rc = run.main([
                '--package',
                str(self.root), '--elf',
                str(self.elf), '--elf-sha256', self.elf_sha, '--tokens-json',
                str(tokens), '--report',
                str(self.root / 'report.json')
            ])
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
