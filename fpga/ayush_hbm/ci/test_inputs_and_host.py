"""Dependency identity and hardware-independent HBM safety regression."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import inputs

spec = importlib.util.spec_from_file_location(
    'hbm_host',
    Path(__file__).parent / 'templates/hbm_host.py'
)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class InputTests(unittest.TestCase):

    def test_dependency_lock_rejects_changed_generated_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lock.json'
            path.write_text(json.dumps({'sha256': {'generated.xci': 'old'}}))
            with patch.object(inputs, 'dependency_inventory',
                              return_value={'sha256': {'generated.xci':
                                                       'new'}}):
                with self.assertRaises(RuntimeError):
                    inputs.verify_dependency_lock(Path(tmp), path)

    def test_hbm_configuration_rejects_density_and_calibration_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cl_hbm.xci'
            parameters = {
                k: [{
                    'value': v
                }]
                for k, v in inputs.PINS['hbm_xci_parameters'].items()
            }
            config = {
                'ip_inst': {
                    'component_reference': 'xilinx.com:ip:hbm:1.0',
                    'parameters': {
                        'component_parameters': parameters
                    }
                }
            }
            path.write_text(json.dumps(config))
            inputs.validate_hbm_xci(path)
            for key, value in [('USER_HBM_DENSITY', '8GB'),
                               ('USER_HBM_STACK', '1'),
                               ('USER_INIT_BYPASS', 'TRUE'),
                               ('USER_HBM_TCK_1', '800')]:
                old = parameters[key][0]['value']
                parameters[key][0]['value'] = value
                path.write_text(json.dumps(config))
                with self.subTest(key=key), self.assertRaises(RuntimeError):
                    inputs.validate_hbm_xci(path)
                parameters[key][0]['value'] = old

    def test_source_pin_and_dirty_checkout_rejected(self):
        with patch.object(inputs, 'git', return_value='wrong'):
            with self.assertRaisesRegex(RuntimeError, 'revision'):
                inputs.validate_coral(Path('/not-used'))
        with patch.object(inputs, 'git',
                          side_effect=[inputs.PINS['coral_commit'],
                                       ' M source.sv']):
            with self.assertRaisesRegex(RuntimeError, 'clean'):
                inputs.validate_coral(Path('/not-used'))

    def test_inventory_detects_unchanged_stat_edits(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'source.sv'
            path.write_bytes(b'old')
            stat = path.stat()
            before = inputs.tree_hashes(root)
            path.write_bytes(b'new')
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertNotEqual(before, inputs.tree_hashes(root))

    def test_input_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            (Path(tmp) / 'outside').write_text('input')
            (root / 'link').symlink_to(Path(tmp) / 'outside')
            with self.assertRaises(RuntimeError):
                inputs.tree_hashes(root)


class Memory:

    def __init__(self, alias=False):
        self.words = {}
        self.alias = alias

    def address(self, address):
        return address % host.WINDOW if self.alias else address

    def write(self, address, value):
        self.words[self.address(address)] = value

    def read(self, address):
        return self.words.get(self.address(address), 0)

    def write_bytes(self, address, data, burst=False):
        for offset in range(0, len(data), 4):
            self.write(
                address + offset,
                int.from_bytes(data[offset:offset + 4], 'little')
            )


class FakeDevice:

    def __init__(self, alias=False):
        self.control = Memory()
        self.memory = Memory(alias)
        self.bank = 0

    def load_elf(self, path, bank):
        self.bank = bank
        return 0x80000000

    def run(self, entry):
        base = self.bank * host.WINDOW
        for i in range(16384):
            value = self.memory.read(
                base + 0x10000 + 4 * i
            ) + self.memory.read(base + 0x30000 + 4 * i)
            self.memory.write(base + 0x50000 + 4 * i, value)
        self.control.write(0x10000, 0x51)


class HostTests(unittest.TestCase):

    def test_distinct_patterns_cover_all_eight_banks(self):
        with patch('builtins.print'):
            device = FakeDevice()
            host.smoke(device)
        self.assertEqual(device.bank, 7)
        self.assertNotEqual(
            device.memory.read(0x50000),
            device.memory.read(7 * host.WINDOW + 0x50000)
        )

    def test_complete_aliasing_cannot_pass_smoke(self):
        with patch('builtins.print'), self.assertRaisesRegex(RuntimeError,
                                                             'aliased'):
            host.smoke(FakeDevice(alias=True))

    def test_calibration_timeout_prevents_memory_bar_attachment(self):

        class Control:
            closed = False

            def read(self, address):
                return host.SIGNATURE if address == host.CONFIG else 0

            def close(self):
                self.closed = True

        control = Control()
        with patch.object(host, 'Bar', return_value=control) as attach, \
             patch.object(host.time, 'monotonic', side_effect=[0, 31]), \
             self.assertRaises(TimeoutError):
            host.Device(0)
        attach.assert_called_once_with(0, 0)
        self.assertTrue(control.closed)


if __name__ == '__main__':
    unittest.main()
