#!/usr/bin/env python3
"""Emit synthetic BF16/PQ2 generation inputs and independent golden checks.

The fixture is a real complete tiny decoder, never evidence for either full
checkpoint. Optional --elf expands the exact built ELF into load segments.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from test_runtime import fixture, reference_generation


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--encoding', choices=['bf16', 'pq2'], default='bf16')
    p.add_argument('--elf', type=Path)
    p.add_argument('--eos-first', action='store_true')
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    native = a.encoding == 'pq2'
    image, cfg, weights = fixture(3 if native else 2, native, True, capacity=8)
    prompt = [1, 2]
    generated, gold = reference_generation(cfg, weights, prompt, 4)
    eos = [generated[0]] if a.eos_first else []
    generated, gold = reference_generation(cfg, weights, prompt, 4, eos)
    align = lambda n: (n + 63) & ~63
    base = 0x21000000
    tokens = align(base + len(image))
    logits = align(tokens + len(prompt) * 4)
    gen_addr = align(logits + 4 * cfg['vocab'] * 4)
    eos_addr = align(gen_addr + 4 * 4)
    workspace = align(eos_addr + max(1, len(eos)) * 4)
    d, ff, nh, nk, hd, L = map(
        cfg.get,
        ('dim', 'hidden_dim', 'n_heads', 'n_kv_heads', 'head_dim', 'n_layers')
    )
    work_bytes = 4 * (
        3 * d + 2 * nh * hd + 2 * nk * hd + 2 * ff + 8 + hd // 2 +
        2 * L * 8 * nk * hd
    )
    words = [
        0x434d5231, 2, 0, 0, base,
        len(image), workspace, work_bytes, tokens,
        len(prompt), logits, 4 * cfg['vocab'], 0, 0, 8, 4,
        len(eos), eos_addr, gen_addr, 4, 0, 0
    ] + [0] * 10
    segments = []
    checks = []

    def save(name, address, data):
        (out / name).write_bytes(data)
        segments.append(
            dict(
                address=address,
                file=name,
                sha256=hashlib.sha256(data).hexdigest()
            )
        )

    if a.elf:
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / 'ddr/host')
        )
        from elf_image import ElfImage
        parsed = ElfImage(
            a.elf,
            hashlib.sha256(a.elf.read_bytes()).hexdigest()
        )
        for i, s in enumerate(parsed.segments):
            save(f'elf-{i}.bin', s['address'], s['data'])
        checks.append(dict(address=parsed.address('_ret', 4), value=0))
    save('model.bin', base, image)
    save('tokens.bin', tokens, struct.pack('<2I', *prompt))
    save('mailbox.bin', 0x10000, struct.pack('<32I', *words))
    if eos: save('eos.bin', eos_addr, struct.pack('<I', *eos))
    for addr, value in ((0x10008, 2), (0x1000c, 0),
                        (0x10030, len(prompt) + len(generated) - 1),
                        (0x10034, generated[-1]), (0x10050, len(generated)),
                        (0x10054, 2 if eos else 1)):
        checks.append(dict(address=addr, value=value))
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
    (out / 'fixture.json').write_text(
        json.dumps(
            dict(
                schema=1,
                evidence_kind='synthetic_tiny_reference',
                encoding=a.encoding,
                segments=segments,
                checks=checks,
                prompt=prompt,
                generated=generated,
                workspace_bytes=work_bytes,
                firmware_sha256=hashlib.sha256(a.elf.read_bytes()).hexdigest(
                ) if a.elf else None
            ),
            indent=2
        ) + '\n'
    )
    print(out / 'fixture.json')


if __name__ == '__main__': main()
