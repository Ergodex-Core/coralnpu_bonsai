#!/usr/bin/env python3
"""Preflight or execute full CoralNPU inference through an already loaded HBM image.

No image management, driver installation, host attention, or host projection.
Hardware access requires --execute-hardware and explicit expected image identity.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

from elf_image_hbm import ElfImage
from transport_hbm import (
    CHUNK_BYTES, CSR, DDR_BASE, DDR_BYTES, Device, check_ddr, ddr_offset,
    hold_reset, in_tcm, read_tcm, write_tcm
)

MAILBOX = 0x10000
MAILBOX_MAGIC = 0x434d5231
RETURN_SENTINEL = 0x0badd00d
CORE_CLOCK_HZ = 50_000_000  # Supplied HBM ABI nominal clock; wall time also retained.


def align64(value):
    return (value + 63) & ~63


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


def integer(value, name, minimum=1, maximum=0xffffffff):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in [{minimum},{maximum}]')
    return value


class Plan:
    """All file hashes and address geometry are checked before touching a slot."""

    def __init__(
        self,
        package,
        elf,
        elf_sha256,
        tokens,
        reference=None,
        reference_sha256=None,
        max_new_tokens=1,
        eos_tokens=None
    ):
        self.package = Path(package).resolve()
        manifest_path = self.package / 'manifest.json'
        if manifest_path.stat().st_size > 1024 * 1024:
            raise ValueError('manifest exceeds 1 MiB')
        raw = manifest_path.read_bytes()
        self.manifest_sha256 = hashlib.sha256(raw).hexdigest()
        self.manifest = json.loads(raw)
        if self.manifest.get('schema') != 'coral-model-package-v1':
            raise ValueError('unsupported model package schema')
        segments = self.manifest.get('segments', [])
        if len(segments) != 1:
            raise ValueError(
                'runtime requires exactly one packed model segment'
            )
        self.model = segments[0]
        filename = self.model.get('file')
        if not isinstance(filename, str) or Path(filename).is_absolute():
            raise ValueError('model file must be relative to package')
        self.model_path = (self.package / filename).resolve()
        if not self.model_path.is_relative_to(self.package):
            raise ValueError('model file escapes package directory')
        self.model_address = integer(
            self.model.get('address'), 'model address'
        )
        self.model_bytes = integer(self.model.get('bytes'), 'model bytes')
        if self.model_address % 64 or self.model_address < DDR_BASE + 0x1000000:
            raise ValueError(
                'model must be 64-byte aligned after reserved firmware memory'
            )
        ddr_offset(self.model_address, align64(self.model_bytes))
        if self.model_path.stat().st_size != self.model_bytes:
            raise ValueError('model file size mismatch')
        expected = self.model.get('sha256', '')
        if not re.fullmatch('[0-9a-f]{64}', expected) or digest_file(
                self.model_path) != expected:
            raise ValueError('model file SHA256 mismatch')
        with self.model_path.open('rb') as stream:
            header = stream.read(128)
        if len(header) != 128 or header[:8] != b'CORALM01':
            raise ValueError('model binary magic/header mismatch')
        header_words = struct.unpack_from('<14I', header, 8)
        if (header_words[0] not in (1, 2)
                or header_words[1:3] != (128, self.model_bytes)
            ) or not 0 < header_words[3] <= 4096 or header_words[4] != 128:
            raise ValueError('invalid model header geometry')
        if 128 + 32 * header_words[3] > self.model_bytes or any(header[92:128]
                                                                ):
            raise ValueError('invalid model directory/reserved bytes')
        config = self.manifest['config']
        dims = {
            n: integer(config[n], n, maximum=1 << 20)
            for n in (
                'dim', 'hidden_dim', 'n_layers', 'n_heads', 'n_kv_heads',
                'head_dim', 'vocab'
            )
        }
        self.vocab = dims['vocab']
        self.max_seq = integer(
            self.manifest['memory']['max_seq'], 'max_seq', maximum=2048
        )
        if tuple(dims.values()
                 ) != header_words[5:12] or self.max_seq != header_words[
                     12] or config.get('flags') != header_words[13]:
            raise ValueError('model header and manifest configuration differ')
        if dims['head_dim'] % 2 or dims['n_heads'] % dims['n_kv_heads']:
            raise ValueError('invalid head dimensions')
        kv_dim = dims['n_kv_heads'] * dims['head_dim']
        self.workspace_bytes = 4 * (
            3 * dims['dim'] + 2 * dims['n_heads'] * dims['head_dim'] +
            2 * kv_dim + 2 * dims['hidden_dim'] + self.max_seq +
            dims['head_dim'] // 2 +
            2 * dims['n_layers'] * self.max_seq * kv_dim
        )
        if self.manifest['memory']['workspace_bytes'] != self.workspace_bytes:
            raise ValueError('workspace size differs from runtime formula')
        if not isinstance(tokens,
                          list) or not 1 <= len(tokens) <= self.max_seq:
            raise ValueError('provide 1..max_seq token IDs')
        self.tokens = [
            integer(token, 'token ID', 0, self.vocab - 1) for token in tokens
        ]
        self.max_new_tokens = integer(
            max_new_tokens, 'max_new_tokens', 0, self.max_seq
        )
        if len(tokens) + max(0, self.max_new_tokens - 1) > self.max_seq:
            raise ValueError('prompt plus generation exceeds KV capacity')
        self.eos_tokens = [] if eos_tokens is None else eos_tokens
        if not isinstance(self.eos_tokens, list) or len(self.eos_tokens) > 32:
            raise ValueError(
                'EOS tokens must be an array with at most 32 entries'
            )
        self.eos_tokens = [
            integer(token, 'EOS token ID', 0, self.vocab - 1)
            for token in self.eos_tokens
        ]
        if len(set(self.eos_tokens)) != len(self.eos_tokens):
            raise ValueError('duplicate EOS token ID')
        self.logits_capacity = self.vocab * max(1, self.max_new_tokens)
        self.tokens_address = align64(self.model_address + self.model_bytes)
        self.logits_address = self.tokens_address + align64(4 * len(tokens))
        self.eos_address = self.logits_address + align64(
            4 * self.logits_capacity
        )
        self.generated_address = self.eos_address + align64(
            4 * max(1, len(self.eos_tokens))
        )
        self.workspace_address = self.generated_address + align64(
            4 * max(1, self.max_new_tokens)
        )
        ddr_offset(self.workspace_address, align64(self.workspace_bytes))
        if not re.fullmatch('[0-9a-f]{64}', elf_sha256):
            raise ValueError('firmware requires an explicit lowercase SHA256')
        self.image = ElfImage(elf, elf_sha256)
        self.reference = None
        self.reference_sha256 = None
        self.reference_rows = 0
        if reference is not None:
            path = Path(reference)
            self.reference_rows, remainder = divmod(
                path.stat().st_size, 4 * self.vocab
            )
            if remainder or not 1 <= self.reference_rows <= max(
                    1, self.max_new_tokens):
                raise ValueError(
                    'reference must contain one vocabulary row per emitted token'
                )
            if self.reference_rows < max(
                    1, self.max_new_tokens) and not self.eos_tokens:
                raise ValueError(
                    'short reference requires an EOS stopping condition'
                )
            self.reference_sha256 = digest_file(path)
            if not reference_sha256 or self.reference_sha256 != reference_sha256:
                raise ValueError(
                    'reference logits require a matching explicit SHA256'
                )
            self.reference = path.read_bytes()
            if not all(math.isfinite(x[0])
                       for x in struct.iter_unpack('<f', self.reference)):
                raise ValueError('reference logits contain a non-finite value')
        elif reference_sha256:
            raise ValueError(
                'reference SHA256 provided without reference file'
            )

    def describe(self):
        return dict(
            model_id=self.manifest.get('model_id'),
            source_revision=self.manifest.get('source_revision'),
            manifest_sha256=self.manifest_sha256,
            model_sha256=self.model['sha256'],
            model_address=self.model_address,
            model_bytes=self.model_bytes,
            token_count=len(self.tokens),
            tokens=self.tokens,
            max_seq=self.max_seq,
            max_new_tokens=self.max_new_tokens,
            eos_tokens=self.eos_tokens,
            tokens_address=self.tokens_address,
            logits_address=self.logits_address,
            logits_bytes=4 * self.logits_capacity,
            eos_address=self.eos_address,
            generated_address=self.generated_address,
            workspace_address=self.workspace_address,
            workspace_bytes=self.workspace_bytes,
            ddr_high_watermark=self.workspace_address +
            align64(self.workspace_bytes),
            reference_sha256=self.reference_sha256,
            firmware=self.image.manifest()
        )


def read_ddr(device, address, size):
    ddr_offset(address, size)
    result = bytearray()
    start, end = address & ~63, align64(address + size)
    for block in range(start, end, CHUNK_BYTES):
        raw = device.read_ddr(block, min(CHUNK_BYTES, end - block))
        lo, hi = max(address, block), min(address + size, block + len(raw))
        result.extend(raw[lo - block:hi - block])
    if len(result) != size:
        raise RuntimeError('short DDR read')
    return bytes(result)


def write_ddr(device, address, data):
    """Preserve adjacent bytes for ELF segments whose ends are not DMA aligned."""
    ddr_offset(address, len(data))
    start, end = address & ~63, align64(address + len(data))
    for block in range(start, end, CHUNK_BYTES):
        size = min(CHUNK_BYTES, end - block)
        lo, hi = max(address, block), min(address + len(data), block + size)
        raw = bytearray(device.read_ddr(block, size)
                        ) if hi - lo < size else bytearray(size)
        raw[lo - block:hi - block] = data[lo - address:hi - address]
        device.write_ddr(block, raw)


def load_model(device, plan):
    digest, readback = hashlib.sha256(), hashlib.sha256()
    with plan.model_path.open('rb') as stream:
        remaining, offset = plan.model_bytes, 0
        while remaining:
            data = stream.read(min(CHUNK_BYTES, remaining))
            if not data:
                raise ValueError('model file truncated during loading')
            digest.update(data)
            padded = data + bytes(align64(len(data)) - len(data))
            device.write_ddr(plan.model_address + offset, padded)
            actual = device.read_ddr(plan.model_address + offset, len(padded))
            if len(actual) != len(
                    padded) or actual[len(data):] != padded[len(data):]:
                raise AssertionError('model padding/length readback mismatch')
            readback.update(actual[:len(data)])
            offset += len(data)
            remaining -= len(data)
        if stream.read(1):
            raise ValueError('model file grew during loading')
    if digest.hexdigest() != plan.model['sha256']:
        raise ValueError('model file changed after preflight')
    if readback.hexdigest() != plan.model['sha256']:
        raise AssertionError('DDR model readback SHA256 mismatch')
    check_ddr(device)
    return readback.hexdigest()


def fill_verify(device, address, size, byte):
    for offset in range(0, align64(size), CHUNK_BYTES):
        data = bytes([byte]) * min(CHUNK_BYTES, align64(size) - offset)
        device.write_ddr(address + offset, data)
        if device.read_ddr(address + offset, len(data)) != data:
            raise AssertionError(
                f'DDR initialization/readback mismatch at {address + offset:#x}'
            )


def compare_logits(actual_bytes, reference_bytes, atol, rtol):
    actual = struct.unpack(f'<{len(actual_bytes) // 4}f', actual_bytes)
    if not all(math.isfinite(x) for x in actual):
        raise AssertionError(
            'CoralNPU logits contain NaN/Inf or unwritten poison'
        )
    argmax = max(range(len(actual)), key=lambda i: actual[i])
    result = dict(
        argmax=argmax,
        logit_count=len(actual),
        logits_sha256=hashlib.sha256(actual_bytes).hexdigest(),
        reference_check='NOT_RUN'
    )
    if reference_bytes is not None:
        if len(reference_bytes) != len(actual_bytes):
            raise ValueError('reference and result lengths differ')
        expected = struct.unpack(f'<{len(actual)}f', reference_bytes)
        if not all(math.isfinite(x) for x in expected):
            raise ValueError('reference logits contain NaN/Inf')
        mismatches = [
            i for i, (a, b) in enumerate(zip(actual, expected))
            if abs(a - b) >= atol + rtol * abs(b)
        ]
        expected_argmax = max(range(len(expected)), key=lambda i: expected[i])
        result.update(
            reference_check='PASSED'
            if not mismatches and argmax == expected_argmax else 'FAILING',
            expected_argmax=expected_argmax,
            max_abs_error=max(abs(a - b) for a, b in zip(actual, expected)),
            mismatched_logits=len(mismatches),
            first_mismatch=mismatches[0] if mismatches else None,
            atol=atol,
            rtol=rtol
        )
    return result


def execute(
    device,
    plan,
    result,
    load_timeout=600,
    run_timeout=900,
    atol=3e-5,
    rtol=3e-5,
    logits_output=None
):
    load_started = time.monotonic()
    result['stage'] = 'reset_and_ddr_qualification'
    hold_reset(device)
    device.select_bank(0)
    check_ddr(device)
    device.deadline = time.monotonic() + load_timeout
    result['stage'] = 'load_and_readback'
    for segment in plan.image.segments:
        address, data = segment['address'], segment['data']
        if in_tcm(address, len(data)):
            write_tcm(device, address, data)
            actual = read_tcm(device, address, len(data))
        else:
            write_ddr(device, address, data)
            actual = read_ddr(device, address, len(data))
        if actual != data:
            raise AssertionError(f'firmware readback mismatch at {address:#x}')
    result['model_readback_sha256'] = load_model(device, plan)
    fill_verify(device, plan.workspace_address, plan.workspace_bytes, 0)
    # All-ones IEEE float is NaN, so a partial/stale output cannot pass validation.
    fill_verify(device, plan.logits_address, 4 * plan.logits_capacity, 0xff)
    fill_verify(
        device, plan.generated_address, 4 * max(1, plan.max_new_tokens), 0xff
    )
    tokens_data = struct.pack(f'<{len(plan.tokens)}I', *plan.tokens)
    tokens_data += bytes(align64(len(tokens_data)) - len(tokens_data))
    device.write_ddr(plan.tokens_address, tokens_data)
    if device.read_ddr(plan.tokens_address, len(tokens_data)) != tokens_data:
        raise AssertionError('token input readback mismatch')
    eos_data = struct.pack(f'<{len(plan.eos_tokens)}I', *plan.eos_tokens)
    eos_data += bytes(align64(max(1, len(eos_data))) - len(eos_data))
    device.write_ddr(plan.eos_address, eos_data)
    if device.read_ddr(plan.eos_address, len(eos_data)) != eos_data:
        raise AssertionError('EOS input readback mismatch')
    mailbox = [
        MAILBOX_MAGIC, 2, 0, 0, plan.model_address, plan.model_bytes,
        plan.workspace_address, plan.workspace_bytes, plan.tokens_address,
        len(plan.tokens), plan.logits_address, plan.logits_capacity, 0,
        0xffffffff, plan.max_seq, plan.max_new_tokens,
        len(plan.eos_tokens
            ), plan.eos_address, plan.generated_address, plan.max_new_tokens
    ] + [0] * 12
    data = struct.pack('<32I', *mailbox)
    write_tcm(device, MAILBOX, data)
    if read_tcm(device, MAILBOX, 128) != data:
        raise AssertionError('mailbox readback mismatch')
    device.write(plan.image.address('_ret', 4), RETURN_SENTINEL)
    guard, top = (
        plan.image.address(n) for n in ('__stack_guard__', '__stack_end__')
    )
    write_tcm(device, guard, bytes([0xa5]) * (top - guard))
    if read_tcm(device, guard, top - guard) != bytes([0xa5]) * (top - guard):
        raise AssertionError('stack initialization/readback mismatch')
    if device.read(plan.image.address('_ret', 4)) != RETURN_SENTINEL:
        raise AssertionError('return sentinel readback mismatch')
    device.write(CSR + 4, plan.image.entry)
    if device.read(CSR + 4) != plan.image.entry or device.read(CSR + 8) & 3:
        raise AssertionError('start PC/status invalid before execution')
    check_ddr(device)
    result['load_and_readback_seconds'] = time.monotonic() - load_started
    result['stage'] = 'execute'
    device.deadline = time.monotonic() + run_timeout
    run_started = time.monotonic()
    device.write(CSR, 0)
    if device.read(CSR) != 0:
        raise AssertionError('core reset release readback mismatch')
    while True:
        status = device.read(CSR + 8)
        if status & 2:
            raise AssertionError(
                'CoralNPU core fault; mailbox read deferred until reset/halt and drain'
            )
        check_ddr(device)
        if status & 1:
            break
        if time.monotonic() >= device.deadline:
            raise TimeoutError(
                'CoralNPU inference exceeded execution deadline'
            )
        time.sleep(0.01)
    device.wait_halt_and_drain()
    result['stage'] = 'validate_output'
    result['execution_wall_seconds'] = time.monotonic() - run_started
    if result.get('physical_execution') == 'STARTED':
        result['physical_execution'] = 'COMPLETED'
    device.deadline = time.monotonic() + 60
    output_mailbox = list(
        struct.unpack('<32I', read_tcm(device, MAILBOX, 128))
    )
    result['mailbox'] = output_mailbox
    cycle_names = ('prefill', 'decode', 'total', 'first_token')
    cycles = {
        name: output_mailbox[22 + 2 * i] | (output_mailbox[23 + 2 * i] << 32)
        for i, name in enumerate(cycle_names)
    }
    # Preserve raw counters even when malformed output prevents validation.
    # Derived timing is emitted only after the protocol and counters pass.
    result['firmware_cycles'] = cycles
    if device.read(plan.image.address('_ret', 4)) != 0:
        raise AssertionError(
            'firmware did not return zero; halt alone is not success'
        )
    if any(output_mailbox[i] != mailbox[i]
           for i in (0, 1, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 16, 17, 18, 19,
                     30, 31)):
        raise AssertionError('firmware corrupted immutable mailbox fields')
    generated_count = output_mailbox[20]
    if (plan.max_new_tokens == 0 and generated_count
            != 0) or (plan.max_new_tokens
                      and not 1 <= generated_count <= plan.max_new_tokens):
        raise AssertionError('invalid generated token count')
    completed_tokens = len(plan.tokens) + max(0, generated_count - 1)
    if output_mailbox[2:4] != [2, 0] or output_mailbox[12] != completed_tokens:
        raise AssertionError(
            'firmware mailbox does not report every token completed successfully'
        )
    if read_tcm(device, guard, 64) != bytes([0xa5]) * 64:
        raise AssertionError('firmware stack guard overwritten')
    if device.read_ddr(plan.tokens_address, len(tokens_data)) != tokens_data:
        raise AssertionError('firmware modified input tokens')
    if device.read_ddr(plan.eos_address, len(eos_data)) != eos_data:
        raise AssertionError('firmware modified EOS input')
    generated = list(
        struct.unpack(
            f'<{generated_count}I',
            read_ddr(device, plan.generated_address, 4 * generated_count)
        )
    ) if generated_count else []
    stop_reason = output_mailbox[21]
    result.update(
        generated_tokens=generated,
        generated_count=generated_count,
        stop_reason_code=stop_reason
    )
    rows = max(1, generated_count)
    actual = read_ddr(device, plan.logits_address, 4 * plan.vocab * rows)
    result.update(
        logits_sha256=hashlib.sha256(actual).hexdigest(),
        logits_bytes=len(actual),
        logits_rows=rows
    )
    if logits_output is not None:
        output = Path(logits_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(actual)
        result['logits_file'] = str(output)
    if any(token >= plan.vocab for token in generated):
        raise AssertionError('generated token is outside vocabulary')
    if plan.max_new_tokens:
        if any(token in plan.eos_tokens for token in generated[:-1]):
            raise AssertionError('firmware continued after an EOS token')
        expected_stop = 2 if generated[-1] in plan.eos_tokens else 1
        if stop_reason != expected_stop or (
                expected_stop == 1 and generated_count != plan.max_new_tokens):
            raise AssertionError(
                'generation stop reason/count is inconsistent'
            )
    elif stop_reason != 0:
        raise AssertionError('prompt-only request must have stop reason zero')
    result['stop_reason'] = {
        0: 'prompt_only',
        1: 'max_new_tokens',
        2: 'eos'
    }[stop_reason]
    if not cycles['prefill'] or not cycles['total'] or cycles[
            'total'] < cycles['prefill'] + cycles['decode']:
        raise AssertionError('invalid firmware cycle counters')
    if generated and not cycles['prefill'] <= cycles['first_token'] <= cycles[
            'total']:
        raise AssertionError('invalid firmware first-token cycle counter')
    if generated_count > 1 and not cycles['decode']:
        raise AssertionError(
            'missing decode cycles for generated continuation'
        )
    seconds = {name: value / CORE_CLOCK_HZ for name, value in cycles.items()}
    result['metrics'] = dict(
        cycles=cycles,
        nominal_core_clock_hz=CORE_CLOCK_HZ,
        cycle_clock_basis=
        'supplied HBM ABI nominal50MHz; wall clock separately reported',
        prefill_seconds=seconds['prefill'],
        decode_seconds=seconds['decode'],
        core_total_seconds=seconds['total'],
        core_ttft_seconds=seconds['first_token'] if generated else None,
        decode_tokens=max(0, generated_count - 1),
        decode_tokens_per_second=(generated_count - 1) /
        seconds['decode'] if generated_count > 1 else None,
        load_and_readback_seconds=result['load_and_readback_seconds'],
        execution_wall_seconds=result['execution_wall_seconds']
    )
    if plan.reference is not None and rows != plan.reference_rows:
        raise AssertionError(
            'generated token count differs from reference rows'
        )
    comparisons = []
    result['predictions'] = comparisons
    for i in range(rows):
        lo, hi = i * plan.vocab * 4, (i + 1) * plan.vocab * 4
        comparison = dict(prediction_index=i)
        comparisons.append(comparison)
        try:
            comparison.update(
                compare_logits(
                    actual[lo:hi],
                    None if plan.reference is None else plan.reference[lo:hi],
                    atol, rtol
                )
            )
        except (AssertionError, ValueError) as exc:
            comparison['error'] = str(exc)
            raise
        if generated and generated[i] != comparison['argmax']:
            comparison['generated_token'] = generated[i]
            comparison['error'] = 'generated token differs from logits argmax'
            raise AssertionError(
                f'generated token {i} differs from returned logits argmax'
            )
    if comparisons[-1]['argmax'] != output_mailbox[13]:
        raise AssertionError('firmware argmax differs from returned logits')
    result['reference_check'] = (
        'NOT_RUN' if plan.reference is None else 'FAILING'
        if any(x['reference_check'] != 'PASSED'
               for x in comparisons) else 'PASSED'
    )
    if result['reference_check'] == 'FAILING':
        raise AssertionError('full-logit reference comparison failed')
    result.update(
        stage='complete',
        status='PASSED'
        if plan.reference is not None else 'EXECUTED_UNVERIFIED'
    )
    return actual


def verify_loaded_image(slot, expected_image, expected_shell):
    if not re.fullmatch(r'agfi-[0-9a-f]+', expected_image):
        raise ValueError('expected image must be an approved agfi identifier')
    if not re.fullmatch(r'0x[0-9a-fA-F]+', expected_shell):
        raise ValueError('expected shell must be hexadecimal')
    process = subprocess.run(['fpga-describe-local-image', '-S',
                              str(slot)],
                             check=True,
                             capture_output=True,
                             text=True,
                             timeout=15)
    lines = [
        line.split()
        for line in process.stdout.splitlines()
        if line.startswith('AFI ')
    ]
    if len(lines) != 1 or len(lines[0]) < 8:
        raise ValueError('unrecognized loaded FPGA image status')
    line = lines[0]
    if line[1:4] != [str(slot), expected_image, 'loaded'
                     ] or line[-1].lower() != expected_shell.lower():
        raise ValueError(
            'loaded image/slot/shell does not match explicit expected identity'
        )
    return dict(
        image=expected_image, shell=expected_shell, slot=slot, state='loaded'
    )


def cleanup(device, report):
    try:
        hold_reset(device)
        report['cleanup_reset'] = 'PASSED'
    except Exception as first:
        try:
            device.emergency_reset()
            report['cleanup_reset'] = 'PASSED_AFTER_RECONNECT'
        except Exception as second:
            report.update(
                status='FAILING',
                cleanup_reset=f'FAILING: {first}; reconnect: {second}'
            )
    try:
        device.close()
        report['cleanup_detach'] = 'PASSED'
    except Exception as exc:
        report.update(status='FAILING', cleanup_detach=f'FAILING: {exc}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--elf', required=True, type=Path)
    parser.add_argument('--elf-sha256', required=True)
    parser.add_argument(
        '--tokens-json',
        required=True,
        type=Path,
        help='JSON array of tokenizer-produced IDs'
    )
    parser.add_argument(
        '--max-new-tokens',
        type=int,
        default=1,
        help='greedy tokens generated on CoralNPU; zero checks prompt only'
    )
    parser.add_argument(
        '--eos-tokens-json',
        type=Path,
        help='JSON array of explicit EOS token IDs; default no EOS stopping'
    )
    parser.add_argument(
        '--reference-logits',
        type=Path,
        help='little-endian FP32 rows, one full vocabulary per emitted token'
    )
    parser.add_argument('--reference-sha256')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--logits-output', type=Path)
    parser.add_argument('--execute-hardware', action='store_true')
    parser.add_argument('--expected-agfi')
    parser.add_argument('--expected-shell')
    parser.add_argument('--slot', type=int, default=0)
    parser.add_argument('--sdk-timeout', type=float, default=5)
    parser.add_argument('--load-timeout', type=float, default=600)
    parser.add_argument('--run-timeout', type=float, default=900)
    parser.add_argument('--atol', type=float, default=3e-5)
    parser.add_argument('--rtol', type=float, default=3e-5)
    args = parser.parse_args(argv)
    if args.slot < 0 or not 0 < args.sdk_timeout <= 60 or not 0 < args.load_timeout <= 86400 or not 0 < args.run_timeout <= 86400:
        parser.error(
            'slot must be nonnegative; SDK timeout (0,60]; load/run timeout (0,86400]'
        )
    if args.atol != 3e-5 or args.rtol != 3e-5:
        parser.error(
            'HBM diagnostic retains the strict3e-5 gate; tolerances cannot be changed'
        )
    if args.execute_hardware and args.reference_logits is None:
        parser.error(
            'first model execution requires saved pinned-reference logits'
        )
    if args.execute_hardware and not (args.expected_agfi
                                      and args.expected_shell):
        parser.error(
            'hardware execution requires --expected-agfi and --expected-shell'
        )
    report = dict(
        schema='coral-hbm-run-v2',
        status='FAILING',
        stage='preflight',
        compute_backend=
        'CoralNPU firmware; host loading/token IDs/output validation only',
        evidence_kind='physical_fpga'
        if args.execute_hardware else 'host_preflight',
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        physical_execution='NOT_RUN',
        reference_check='NOT_RUN'
    )
    report[
        'validation_scope'
    ] = 'final logits/tokens and protocol; internal operator traces NOT_CAPTURED'
    device, lockfd = None, None
    try:
        if args.tokens_json.stat().st_size > 64 * 1024:
            raise ValueError('token JSON exceeds 64 KiB')
        eos = []
        if args.eos_tokens_json:
            if args.eos_tokens_json.stat().st_size > 4096:
                raise ValueError('EOS JSON exceeds 4096 bytes')
            eos = json.loads(args.eos_tokens_json.read_text())
        plan = Plan(
            args.package, args.elf, args.elf_sha256,
            json.loads(args.tokens_json.read_text()), args.reference_logits,
            args.reference_sha256, args.max_new_tokens, eos
        )
        report['plan'] = plan.describe()
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
            output = args.logits_output or args.report.with_suffix(
                '.logits.f32'
            )
            execute(
                device,
                plan,
                report,
                args.load_timeout,
                args.run_timeout,
                args.atol,
                args.rtol,
                logits_output=output
            )
    except Exception as exc:
        report.update(status='FAILING', error=f'{type(exc).__name__}: {exc}')
    finally:
        if device is not None:
            cleanup(device, report)
        if lockfd is not None:
            os.close(lockfd)
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc
                                                       ).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, allow_nan=False) + '\n'
        )
    print(json.dumps({k: report[k] for k in ('status', 'stage')}))
    return 0 if report['status'] in (
        'PREFLIGHT_PASSED', 'PASSED', 'EXECUTED_UNVERIFIED'
    ) else 1


if __name__ == '__main__':
    sys.exit(main())
