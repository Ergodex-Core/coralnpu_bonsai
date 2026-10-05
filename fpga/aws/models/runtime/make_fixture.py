#!/usr/bin/env python3
"""Emit synthetic BF16/PQ2 inputs and independent FP64 golden checks.

The fixture is a real complete tiny decoder, never evidence for either full
checkpoint. Optional --elf expands the exact built ELF into load segments.
Defaults retain the two-token prompt, four generated tokens and capacity eight.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from test_runtime import fixture, reference_generation


def token_ids(text):
    """Parse explicit decimal token IDs without dropping empty fields."""
    if not text:
        return []
    try:
        return [int(value) for value in text.split(',')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            'expected comma-separated token IDs'
        ) from exc


def make_fixture(
    output,
    encoding='bf16',
    elf=None,
    prompt=(1, 2),
    max_new_tokens=4,
    capacity=8,
    eos=(),
    eos_first=False
):
    """Write a request and checks, rejecting overflow before creating output."""
    if encoding not in ('bf16', 'pq2'):
        raise ValueError('encoding must be bf16 or pq2')
    if type(capacity) is not int or not 1 <= capacity <= 2048:
        raise ValueError('capacity must be in 1..2048')
    if type(max_new_tokens) is not int or not 0 <= max_new_tokens <= capacity:
        raise ValueError('max_new_tokens must be in 0..capacity')
    prompt, eos = list(prompt), list(eos)
    if not prompt or len(prompt) + max(0, max_new_tokens - 1) > capacity:
        raise ValueError('prompt plus generation exceeds KV capacity')
    if len(eos) > 32 or len(set(eos)) != len(eos):
        raise ValueError('EOS IDs must be unique with at most 32 entries')
    if eos_first and (eos or not max_new_tokens):
        raise ValueError(
            'eos_first requires generation and no explicit EOS IDs'
        )
    native = encoding == 'pq2'
    image, cfg, weights = fixture(
        3 if native else 2, native, True, capacity=capacity
    )
    if any(type(token) is not int or not 0 <= token < cfg['vocab']
           for token in prompt + eos):
        raise ValueError('prompt and EOS token IDs must be inside vocabulary')
    if eos_first:
        first, _ = reference_generation(cfg, weights, prompt, 1)
        eos = [first[0]]
    generated, gold = reference_generation(
        cfg, weights, prompt, max_new_tokens, eos
    )
    stop_reason = (
        0 if not max_new_tokens else 2 if generated[-1] in eos else 1
    )
    completed_tokens = len(prompt) + max(0, len(generated) - 1)
    last_argmax = max(range(cfg['vocab']), key=gold[-1].__getitem__)
    align = lambda n: (n + 63) & ~63
    rows = max(1, max_new_tokens)
    base = 0x21000000
    tokens = align(base + len(image))
    logits = align(tokens + len(prompt) * 4)
    gen_addr = align(logits + rows * cfg['vocab'] * 4)
    eos_addr = align(gen_addr + max(1, max_new_tokens) * 4)
    workspace = align(eos_addr + max(1, len(eos)) * 4)
    d, ff, nh, nk, hd, layers = map(
        cfg.get,
        ('dim', 'hidden_dim', 'n_heads', 'n_kv_heads', 'head_dim', 'n_layers')
    )
    work_bytes = 4 * (
        3 * d + 2 * nh * hd + 2 * nk * hd + 2 * ff + capacity + hd // 2 +
        2 * layers * capacity * nk * hd
    )
    words = [
        0x434d5231, 2, 0, 0, base,
        len(image), workspace, work_bytes, tokens,
        len(prompt), logits, rows * cfg['vocab'], 0, 0, capacity,
        max_new_tokens,
        len(eos), eos_addr, gen_addr, max_new_tokens, 0, 0
    ] + [0] * 10
    parsed = None
    if elf is not None:
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / 'ddr/host')
        )
        from elf_image import ElfImage
        elf = Path(elf)
        parsed = ElfImage(elf, hashlib.sha256(elf.read_bytes()).hexdigest())

    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    segments, checks = [], []

    def save(name, address, data):
        (out / name).write_bytes(data)
        segments.append(
            dict(
                address=address,
                file=name,
                sha256=hashlib.sha256(data).hexdigest()
            )
        )

    if parsed is not None:
        for i, segment in enumerate(parsed.segments):
            save(f'elf-{i}.bin', segment['address'], segment['data'])
        checks.append(dict(address=parsed.address('_ret', 4), value=0))
    save('model.bin', base, image)
    save('tokens.bin', tokens, struct.pack(f'<{len(prompt)}I', *prompt))
    save('mailbox.bin', 0x10000, struct.pack('<32I', *words))
    if eos:
        save('eos.bin', eos_addr, struct.pack(f'<{len(eos)}I', *eos))
    for address, value in ((0x10008, 2), (0x1000c, 0),
                           (0x10030, completed_tokens), (0x10034, last_argmax),
                           (0x10050, len(generated)), (0x10054, stop_reason)):
        checks.append(dict(address=address, value=value))
    for i, token in enumerate(generated):
        checks.append(dict(address=gen_addr + 4 * i, value=token))
    for i, row in enumerate(gold):
        for j, value in enumerate(row):
            checks.append(
                dict(
                    address=logits + 4 * (i * cfg['vocab'] + j),
                    float=value,
                    atol=3e-5,
                    rtol=3e-5
                )
            )
    description = dict(
        schema=1,
        evidence_kind='synthetic_tiny_reference',
        encoding=encoding,
        segments=segments,
        checks=checks,
        prompt=prompt,
        generated=generated,
        workspace_bytes=work_bytes,
        firmware_sha256=parsed.sha256 if parsed is not None else None,
        capacity=capacity,
        max_new_tokens=max_new_tokens,
        eos=eos,
        completed_tokens=completed_tokens,
        stop_reason=stop_reason,
        metadata=dict(
            workload='synthetic tiny decoder; not a full checkpoint',
            reference='independent Python FP64 equations',
            execution='NOT_RUN'
        )
    )
    path = out / 'fixture.json'
    path.write_text(json.dumps(description, indent=2) + '\n')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--encoding', choices=['bf16', 'pq2'], default='bf16')
    parser.add_argument('--elf', type=Path)
    parser.add_argument(
        '--tokens',
        type=token_ids,
        default=[1, 2],
        help='comma-separated prompt token IDs; default 1,2'
    )
    parser.add_argument(
        '--max-new-tokens',
        type=int,
        default=4,
        help='generated tokens; zero evaluates the prompt only'
    )
    parser.add_argument(
        '--capacity',
        type=int,
        default=8,
        help='KV positions allocated, in 1..2048'
    )
    stopping = parser.add_mutually_exclusive_group()
    stopping.add_argument(
        '--eos',
        type=token_ids,
        default=[],
        help='comma-separated EOS IDs; default no stopping'
    )
    stopping.add_argument(
        '--eos-first',
        action='store_true',
        help='stop at the first independently predicted token'
    )
    args = parser.parse_args(argv)
    try:
        path = make_fixture(
            args.output, args.encoding, args.elf, args.tokens,
            args.max_new_tokens, args.capacity, args.eos, args.eos_first
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(path)


if __name__ == '__main__':
    main()
