#!/usr/bin/env python3
"""Destructive TCM tests for an already approved and loaded AWS F2 image.

Never creates, uploads, or loads an FPGA image. Requires an explicit expected AGFI.
"""
import argparse
import ctypes as C
import datetime
import fcntl
import hashlib
import json
import multiprocessing
import os
import struct
import subprocess
import time
from pathlib import Path
from elf_image import ElfImage, in_tcm

CSR = 0x30000
SENTINEL = 0x0BADD00D
STACK_PATTERN = 0xA5
CASE_NAMES = ('math', 'fptr', 'float_add', 'rvv_add')


def sdk_worker(pipe, slot):
    """Isolate SDK calls so even a stuck MMIO access has a host-side timeout."""
    handle = C.c_int(-1)
    lib = None
    try:
        lib = C.CDLL('/usr/local/lib/libfpga_mgmt.so')
        lib.fpga_pci_init.argtypes = []
        lib.fpga_pci_attach.argtypes = [
            C.c_int, C.c_int, C.c_int, C.c_uint32,
            C.POINTER(C.c_int)
        ]
        lib.fpga_pci_peek.argtypes = [
            C.c_int, C.c_uint64, C.POINTER(C.c_uint32)
        ]
        lib.fpga_pci_poke.argtypes = [C.c_int, C.c_uint64, C.c_uint32]
        lib.fpga_pci_detach.argtypes = [C.c_int]

        def check(rc):
            if rc:
                raise RuntimeError(f'AWS FPGA SDK returned {rc}')

        check(lib.fpga_pci_init())
        check(lib.fpga_pci_attach(slot, 0, 0, 0, C.byref(handle)))
        pipe.send((True, None))
        while True:
            operation, address, value = pipe.recv()
            if operation == 'close':
                check(lib.fpga_pci_detach(handle))
                handle.value = -1
                pipe.send((True, None))
                return
            if operation == 'read':
                got = C.c_uint32()
                check(lib.fpga_pci_peek(handle, address, C.byref(got)))
                pipe.send((True, got.value))
            elif operation == 'write':
                check(lib.fpga_pci_poke(handle, address, value))
                pipe.send((True, None))
            else:
                raise ValueError('unknown SDK operation')
    except Exception as exc:
        try:
            pipe.send((False, f'{type(exc).__name__}: {exc}'))
        except (OSError, EOFError):
            pass
    finally:
        if lib is not None and handle.value != -1:
            lib.fpga_pci_detach(handle)
        pipe.close()


class Device:

    def __init__(self, slot, timeout=2):
        self.timeout = timeout
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe()
        self.worker = context.Process(
            target=sdk_worker, args=(child, slot), daemon=True
        )
        self.worker.start()
        child.close()
        self.receive('attach')

    def receive(self, operation):
        remaining = min(
            self.timeout,
            getattr(self, 'deadline', float('inf')) - time.monotonic()
        )
        if remaining <= 0 or not self.pipe.poll(remaining):
            self.stop()
            raise TimeoutError(
                f'SDK {operation} exceeded {self.timeout}s; MMIO worker stopped'
            )
        try:
            ok, result = self.pipe.recv()
        except (EOFError, OSError):
            self.stop()
            raise RuntimeError(f'SDK worker exited during {operation}')
        if not ok:
            self.stop()
            raise RuntimeError(result)
        return result

    def access(self, operation, address, value=None):
        if address % 4 or not (in_tcm(address, 4)
                               or address in (CSR, CSR + 4, CSR + 8)):
            raise ValueError(
                f'address outside TCM/control words: {address:#x}'
            )
        if operation == 'write' and (address == CSR + 8
                                     or not 0 <= value <= 0xFFFFFFFF):
            raise ValueError('invalid write')
        self.pipe.send((operation, address, value))
        return self.receive(f'{operation} at {address:#x}')

    def read(self, address):
        return self.access('read', address)

    def write(self, address, value):
        self.access('write', address, value)

    def stop(self):
        if self.worker.is_alive():
            self.worker.terminate()
            self.worker.join(timeout=1)
        if self.worker.is_alive():
            self.worker.kill()
            self.worker.join(timeout=1)
        self.pipe.close()

    def close(self):
        try:
            if self.worker.is_alive():
                self.pipe.send(('close', 0, 0))
                self.receive('detach')
        finally:
            self.stop()


def read_bytes(device, address, size):
    if not in_tcm(address, size):
        raise ValueError('read outside TCM')
    words = b''.join(
        device.read(a).to_bytes(4, 'little')
        for a in range(address & ~3, (address + size + 3) & ~3, 4)
    )
    return words[address % 4:address % 4 + size]


def write_bytes(device, address, data):
    if not in_tcm(address, len(data)):
        raise ValueError('write outside TCM')
    for a in range(address & ~3, (address + len(data) + 3) & ~3, 4):
        lo, hi = max(a, address), min(a + 4, address + len(data))
        word = bytearray(device.read(a).to_bytes(4, 'little')
                         ) if hi - lo != 4 else bytearray(4)
        word[lo - a:hi - a] = data[lo - address:hi - address]
        device.write(a, int.from_bytes(word, 'little'))


def poll(device, predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.001)


def hold_reset(device, timeout):
    device.deadline = time.monotonic() + timeout
    device.write(CSR, 1)  # Core reset asserted, core clock enabled.

    def reset_ready():
        return True if device.read(
            CSR
        ) == 1 and device.read(CSR + 8) & 3 == 0 else None

    poll(
        device, reset_ready, timeout,
        'reset/status did not clear; possible stale halt'
    )


def execute_case(device, image, name, repeat, timeout, result):
    started = time.monotonic()
    result.update(
        test=name,
        repeat=repeat,
        elf_sha256=image.sha256,
        expected_ret=0,
        status='running',
        stage='reset'
    )
    hold_reset(device, timeout)
    result['stage'] = 'load_and_readback'
    deadline = time.monotonic() + 60
    device.deadline = deadline
    for segment in image.segments:
        write_bytes(device, segment['address'], segment['data'])
        if read_bytes(device, segment['address'],
                      segment['size']) != segment['data']:
            raise AssertionError(
                f'ELF load/readback mismatch at {segment["address"]:#x}'
            )
        if time.monotonic() > deadline:
            raise TimeoutError('ELF loading/readback exceeded 60 seconds')
    ret_address = image.address('_ret', 4)
    device.write(ret_address, SENTINEL)
    guard = image.address('__stack_guard__')
    stack_top = image.address('__stack_end__')
    stack_data = bytes([STACK_PATTERN]) * (stack_top - guard)
    write_bytes(device, guard, stack_data)
    expected = b''
    output_address = None
    if name == 'float_add':
        # Alternate inputs make replay of a previous successful result fail.
        first = [float(i + repeat * 8) for i in range(8)]
        second = [10.0] * 8
        expected = struct.pack('<8f', *[a + b for a, b in zip(first, second)])
        write_bytes(
            device, image.address('input1', 32), struct.pack('<8f', *first)
        )
        write_bytes(
            device, image.address('input2', 32), struct.pack('<8f', *second)
        )
        output_address = image.address('output', 32)
        result.update(
            inputs=[first, second],
            expected_outputs=[a + b for a, b in zip(first, second)]
        )
    elif name == 'rvv_add':
        output_address = image.address('output', 2048)
        expected = struct.pack('<1024h', *([7] * 1024))
        result['expected_outputs'] = [7] * 1024
    elif name == 'math':
        result['internal_assertion'] = 'sum(2*i + 3*i*i, i=0..1) == 5'
    elif name == 'fptr':
        result['internal_assertion'
               ] = 'indirect memcpy copies 0xdeadbeef; src == dst'
    else:
        raise ValueError(f'unknown case {name}')
    if output_address is not None:
        poison = bytes([0xCD ^ (repeat & 0xFF)]) * len(expected)
        write_bytes(device, output_address, poison)
        if read_bytes(device, output_address, len(poison)) != poison:
            raise AssertionError('output poison readback failed')
    if device.read(ret_address) != SENTINEL or read_bytes(
            device, guard, len(stack_data)) != stack_data:
        raise AssertionError(
            'return sentinel or stack pattern readback failed'
        )
    device.write(CSR + 4, image.entry)
    if device.read(CSR + 4) != image.entry:
        raise AssertionError('start PC readback failed')
    if device.read(CSR + 8) & 3:
        raise AssertionError('status became stale before reset release')
    result['stage'] = 'execute'
    device.deadline = time.monotonic() + timeout
    device.write(CSR, 0)
    if device.read(CSR) != 0:
        raise AssertionError('reset release readback failed')

    def finished():
        status = device.read(CSR + 8)
        result['core_status'] = status
        if status & 2:
            raise AssertionError(f'core fault, status={status:#x}')
        return status if status & 1 else None

    poll(
        device, finished, timeout, 'core did not halt before execution timeout'
    )
    result['stage'] = 'validate'
    device.deadline = time.monotonic() + 60
    actual_ret = device.read(ret_address)
    result['actual_ret'] = actual_ret
    if actual_ret != 0:
        raise AssertionError(
            f'_ret={actual_ret:#x}, expected 0; halt is not success'
        )
    if output_address is not None:
        actual = read_bytes(device, output_address, len(expected))
        result['actual_outputs_hex'] = actual.hex()
        result['expected_outputs_hex'] = expected.hex()
        if actual != expected:
            raise AssertionError(
                'output bytes do not match every expected element'
            )
        result['actual_outputs'] = list(
            struct.unpack('<8f' if name == 'float_add' else '<1024h', actual)
        )
    stack = read_bytes(device, guard, stack_top - guard)
    if stack[:64] != bytes([STACK_PATTERN]) * 64:
        raise AssertionError('stack guard overwritten')
    first_written = next(
        (i for i, byte in enumerate(stack[64:]) if byte != STACK_PATTERN), 4096
    )
    result.update(
        stack_reserved_bytes=4096,
        stack_observed_used_bytes=4096 - first_written,
        elapsed_seconds=time.monotonic() - started,
        stage='complete',
        status='passed'
    )


def verify_loaded_image(slot, agfi, shell):
    if not agfi.startswith('agfi-'):
        raise ValueError('--expected-agfi must identify an approved image')
    proc = subprocess.run(['fpga-describe-local-image', '-S',
                           str(slot)],
                          check=True,
                          capture_output=True,
                          text=True,
                          timeout=15)
    lines = [
        line.split()
        for line in proc.stdout.splitlines()
        if line.startswith('AFI ')
    ]
    if len(lines) != 1 or len(lines[0]) < 8:
        raise ValueError('unrecognized FPGA image status')
    info = lines[0]
    if info[1] != str(slot) or info[2] != agfi or info[3] != 'loaded' or info[
            -1].lower() != shell.lower():
        raise ValueError(
            f'expected loaded {agfi} on shell {shell}; observed: {proc.stdout.strip()}'
        )
    return proc.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--expected-agfi', required=True)
    parser.add_argument('--expected-shell', default='0x10212415')
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--timeout', type=float, default=5)
    args = parser.parse_args()
    if args.repeat < 2 or args.repeat > 100 or not 0 < args.timeout <= 60 or args.slot < 0:
        parser.error(
            'repeat must be 2..100; timeout must be (0,60]; slot must be nonnegative'
        )
    report = dict(
        schema=1,
        evidence_kind='physical_fpga',
        status='failed',
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        slot=args.slot,
        expected_agfi=args.expected_agfi,
        expected_shell=args.expected_shell,
        runs=[],
        stage='preflight'
    )
    device = None
    lockfd = None
    try:
        manifest_bytes = (args.bundle / 'manifest.json').read_bytes()
        manifest = json.loads(manifest_bytes)
        report['build_manifest_sha256'] = hashlib.sha256(manifest_bytes
                                                         ).hexdigest()
        report['build_manifest'] = manifest
        if set(manifest['tests']) != set(CASE_NAMES):
            raise ValueError(
                'bundle must contain exactly the four upstream cases'
            )
        images = {}
        for name in CASE_NAMES:
            image = ElfImage(args.bundle / f'{name}.elf')
            if image.sha256 != manifest['tests'][name]['sha256']:
                raise ValueError(
                    f'{name}: ELF hash differs from build manifest'
                )
            images[name] = image
        lockfd = os.open(
            f'/run/lock/coralnpu-fpga-slot-{args.slot}.lock',
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report['image_status'] = verify_loaded_image(
            args.slot, args.expected_agfi, args.expected_shell
        )
        device = Device(args.slot)
        report['stage'] = 'running'
        for repeat in range(args.repeat):
            for name in CASE_NAMES:
                result = {}
                report['runs'].append(result)
                try:
                    execute_case(
                        device, images[name], name, repeat, args.timeout,
                        result
                    )
                except Exception as exc:
                    result.update(
                        status='failed', error=f'{type(exc).__name__}: {exc}'
                    )
                    raise
        report.update(status='passed', stage='complete')
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if device is not None:
            try:
                hold_reset(device, args.timeout)
                report['cleanup_reset'] = 'passed'
            except Exception as exc:
                report.update(status='failed', cleanup_reset=f'failed: {exc}')
            try:
                device.deadline = time.monotonic() + 5
                device.close()
            except Exception as exc:
                report.update(status='failed', cleanup_detach=f'failed: {exc}')
        if lockfd is not None:
            os.close(lockfd)
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc
                                                       ).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, allow_nan=False) + '\n'
        )
    print(json.dumps({k: report[k] for k in ('status', 'stage')}))
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
