#!/usr/bin/env python3
"""Qualify an already loaded DDR image; default preflight never opens hardware.

Host memory patterns test PCIS transport. Optional compiler-independent RV32
instructions exercise core DDR fetch, loads, stores, and byte preservation.
These are memory tests, not model execution or throughput measurements.
"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time

from run_inference import cleanup, read_ddr, verify_loaded_image, write_ddr
from transport import (
    CSR, DDR_BASE, DDR_BYTES, DDR_CSR, Device, check_ddr, hold_reset, in_tcm,
    read_tcm, write_tcm
)

WINDOWS = ((DDR_BASE, 64), (DDR_BASE + DDR_BYTES // 2, 128),
           (DDR_BASE + DDR_BYTES - 64, 64))


def core_fixture():
    path = Path(__file__).resolve().parents[1] / 'core_sim/run.py'
    spec = importlib.util.spec_from_file_location('coral_ddr_core_smoke', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.smoke()


def execute(device, report, core=False, timeout=30):
    report['stage'] = 'reset_and_ddr_qualification'
    hold_reset(device)
    device.deadline = time.monotonic() + timeout
    check_ddr(device)
    report['ddr_registers'] = {
        hex(DDR_CSR + offset): device.read(DDR_CSR + offset)
        for offset in (0, 4, 8, 12, 16)
    }
    report['stage'] = 'pcis_patterns'
    begin = time.monotonic()
    patterns = {
        address:
        hashlib.shake_256(f'coral-ddr-{address:08x}'.encode()).digest(size)
        for address, size in WINDOWS
    }
    # Write every distinct window before reading any back to detect aliasing.
    for address, data in patterns.items():
        device.write_ddr(address, data)
    report['pattern_checks'] = []
    for address, data in patterns.items():
        actual = device.read_ddr(address, len(data))
        report['pattern_checks'].append(
            dict(
                address=address,
                bytes=len(data),
                expected_sha256=hashlib.sha256(data).hexdigest(),
                actual_sha256=hashlib.sha256(actual).hexdigest()
            )
        )
        if actual != data:
            raise AssertionError(f'PCIS pattern mismatch at {address:#x}')
    middle = WINDOWS[1][0]
    patch = b'\x12\x34\x56\x78\x9a\xbc'
    write_ddr(device, middle + 61, patch)
    expected = patterns[middle][:61] + patch + patterns[middle][67:]
    if device.read_ddr(middle, len(expected)) != expected:
        raise AssertionError('host partial-write neighbor preservation failed')
    report['host_partial_write_preservation'] = 'PASS'
    report['client_bounds'] = []
    # These requests must be rejected before opening or using the DMA worker.
    for address in (DDR_BASE - 64, DDR_BASE + DDR_BYTES):
        try:
            device.read_ddr(address, 64)
        except ValueError:
            report['client_bounds'].append(
                dict(address=address, result='REJECTED_BEFORE_DMA')
            )
        else:
            raise AssertionError('out-of-window client request was accepted')
    report['client_bounds_evidence'
           ] = 'software-only; no invalid AXI request issued'
    check_ddr(device)
    report['pcis_wall_seconds'] = time.monotonic() - begin
    if core:
        report['stage'] = 'core_load_and_readback'
        regions, checks = core_fixture()
        report['core_inputs'] = []
        for address, data in regions:
            tcm = in_tcm(address, len(data))
            (write_tcm if tcm else write_ddr)(device, address, data)
            actual = (read_tcm
                      if tcm else read_ddr)(device, address, len(data))
            if actual != data:
                raise AssertionError(
                    f'core fixture readback failed at {address:#x}'
                )
            report['core_inputs'].append(
                dict(
                    address=address,
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest()
                )
            )
        device.write(CSR + 4, 0)
        if device.read(CSR + 4) != 0 or device.read(CSR + 8) & 3:
            raise AssertionError('invalid core start PC/status')
        check_ddr(device)
        report['stage'] = 'core_execute'
        begin = time.monotonic()
        device.deadline = begin + timeout
        device.write(CSR, 0)
        if device.read(CSR) != 0:
            raise AssertionError('core reset release readback failed')
        while True:
            status = device.read(CSR + 8)
            if status & 2:
                raise AssertionError('core fault during DDR smoke')
            check_ddr(device)
            if status & 1:
                break
            if time.monotonic() >= device.deadline:
                raise TimeoutError(
                    'core DDR smoke exceeded execution deadline'
                )
            time.sleep(0.01)
        report['core_wall_seconds'] = time.monotonic() - begin
        report['stage'] = 'core_validate'
        device.deadline = time.monotonic() + timeout
        report['core_checks'] = []
        for check in checks:
            address = check['address']
            actual = device.read(address) if in_tcm(
                address, 4
            ) else int.from_bytes(read_ddr(device, address, 4), 'little')
            report['core_checks'].append(
                dict(address=address, expected=check['value'], actual=actual)
            )
            if actual != check['value']:
                raise AssertionError(
                    f'core DDR result mismatch at {address:#x}'
                )
        check_ddr(device)
    report.update(status='PASS', stage='complete')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--execute-hardware', action='store_true')
    parser.add_argument('--core-smoke', action='store_true')
    parser.add_argument('--expected-agfi')
    parser.add_argument('--expected-shell')
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--sdk-timeout', type=float, default=5)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    if args.slot < 0 or not 0 < args.sdk_timeout <= 60 or not 0 < args.timeout <= 120:
        parser.error(
            'slot must be nonnegative; SDK timeout (0,60]; timeout (0,120]'
        )
    if args.execute_hardware and not (args.expected_agfi
                                      and args.expected_shell):
        parser.error(
            'hardware execution requires explicit expected image and shell'
        )
    report = dict(
        schema=1,
        status='FAILING',
        stage='preflight',
        evidence_kind='physical_memory_smoke'
        if args.execute_hardware else 'host_preflight',
        physical_execution='NOT_RUN',
        core_smoke_requested=args.core_smoke,
        backend=
        'PCIS/OCL transport and optional Coral RV32 memory instructions; no model compute',
        nominal_core_clock_hz=50_000_000,
        timing='host wall time only; not TTFT/TPS',
        windows=[dict(address=a, bytes=n) for a, n in WINDOWS],
        runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )
    device, lockfd = None, None
    try:
        if not args.execute_hardware:
            report.update(status='PREFLIGHT_PASSED', stage='complete')
        else:
            report['stage'] = 'slot_ownership'
            lockfd = os.open(
                f'/run/lock/coralnpu-fpga-slot-{args.slot}.lock',
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report['image'] = verify_loaded_image(
                args.slot, args.expected_agfi, args.expected_shell
            )
            device = Device(args.slot, args.sdk_timeout)
            report['physical_execution'] = 'STARTED'
            execute(device, report, args.core_smoke, args.timeout)
            report['physical_execution'] = 'COMPLETED'
    except Exception as exc:
        report.update(status='FAILING', error=f'{type(exc).__name__}: {exc}')
    finally:
        if device is not None:
            cleanup(device, report)
        if lockfd is not None:
            os.close(lockfd)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, allow_nan=False) + '\n'
        )
    print(json.dumps({k: report[k] for k in ('status', 'stage')}))
    return 0 if report['status'] in ('PASS', 'PREFLIGHT_PASSED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
