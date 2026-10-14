#!/usr/bin/env python3
"""Analytical bounds, NOT an RTL simulation or a measured accelerator benchmark.

Run: python3 sizing.py [--weights /path/to/canonical/weights.bin]
Only Python standard library is required. All rates count one signed product
and accumulation as one MAC-equivalent; a ternary add/skip is not a multiplier.
"""
import argparse
import collections
import hashlib
import json
import math
import pathlib
import random
import struct

ROOT = pathlib.Path(__file__).resolve().parent
F = 25_000_000


def emit(name, value):
    (ROOT / 'results' / name).write_text(json.dumps(value, indent=2) + '\n')


def amdahl(p, kernel_speedup):
    return 1 / (1 - p + p / kernel_speedup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--weights', type=pathlib.Path)
    args = ap.parse_args()
    (ROOT / 'results').mkdir(exist_ok=True)
    manifest = json.loads((ROOT / 'sources/bonsai-manifest.json').read_text())
    ts = [t for t in manifest['tensors'] if t['format'] == 'Q1_0']
    head = next(t for t in ts if t['name'] == 'token_embd.weight')
    shapes = collections.Counter(tuple(t['shape']) for t in ts)
    work = {}
    for m in [1, 8, 32, 128]:
        # Head only at last prompt position. Embedding is a lookup, not a second GEMM.
        macs = sum(t['elements'] * (1 if t is head else m) for t in ts)
        work[str(m)] = {
            'MACs':
            macs,
            'head_last_position_only':
            True,
            'ideal_weights_bytes_one_scan':
            sum(t['payload_bytes'] for t in ts),
            'weights_bytes_token_tile8':
            sum(
                t['payload_bytes'] * (1 if t is head else math.ceil(m / 8))
                for t in ts
            )
        }
    emit(
        'workload.json', {
            'shapes': [{
                'N': n,
                'K': k,
                'count': c
            } for (n, k), c in shapes.items()],
            'workloads':
            work,
            'model_image_bytes':
            manifest['image_bytes'],
            'expanded_int8_weights_only_bytes':
            sum(t['elements'] for t in ts)
        }
    )
    candidates = []
    # Payload bandwidth sensitivity, not measured hardware bandwidth.
    bws = [0.013, .05, .2, .4, .9, 1.6, 1.8, 3.6]
    for width in [32, 64, 128]:
        for engines in [1, 2, 4, 8]:
            lanes = width * engines
            peak = F * lanes
            q1_bw = peak * 18 / 128 / 1e9
            candidates.append({
                'engines':
                engines,
                'width':
                width,
                'lanes':
                lanes,
                'ideal_GMAC_s':
                peak / 1e9,
                'group_results_per_cycle':
                lanes / 128,
                'Q1_GB_s_to_feed_peak':
                q1_bw,
                'PTQ1_GB_s_to_feed_peak':
                peak * 28 / 128 / 1e9,
                'PQ2_GB_s_to_feed_peak':
                peak * 34 / 128 / 1e9,
                'shared_activation_bytes_per_cycle':
                width,
                'Q1_weight_bytes_per_cycle':
                lanes * 18 / 128,
                'group_output_bytes_per_cycle_int32_plus_fp16':
                lanes * 6 / 128,
                'compute_only_decode_seconds':
                int(work['1']['MACs']) / peak,
                'roofline_decode_seconds': {
                    str(b):
                    max(
                        work['1']['MACs'] / peak,
                        work['1']['ideal_weights_bytes_one_scan'] / (b * 1e9)
                    )
                    for b in bws
                },
                'PQ2_roofline_decode_seconds': {
                    str(b):
                    max(
                        work['1']['MACs'] / peak,
                        work['1']['ideal_weights_bytes_one_scan'] * 34 / 18 /
                        (b * 1e9)
                    )
                    for b in bws
                },
                'PQ2_prefill32_tile8_seconds': {
                    str(b):
                    max(
                        work['32']['MACs'] / peak,
                        work['32']['weights_bytes_token_tile8'] * 34 / 18 /
                        (b * 1e9)
                    )
                    for b in bws
                },
                'prefill32_tile8_seconds': {
                    str(b):
                    max(
                        work['32']['MACs'] / peak,
                        work['32']['weights_bytes_token_tile8'] / (b * 1e9)
                    )
                    for b in bws
                },
                'head_row_occupancy':
                head['shape'][0] /
                (math.ceil(head['shape'][0] / engines) * engines)
            })
    emit('candidates.json', candidates)
    # Common 25% combined issue/scale/tail loss is an illustrative sensitivity.
    targets = []
    for p in [.5, .6, .7, .8, .9, .95]:
        for target in [2, 5]:
            denom = 1 / target - (1 - p)
            required = p / denom if denom > 1e-12 else None
            targets.append({
                'accelerable_fraction': p,
                'e2e_target': target,
                'required_kernel_speedup': required,
                'limit_infinite_kernel': 1 / (1 - p)
            })
    emit('targets.json', targets)
    emit(
        'amdahl.json', [{
            'p': p,
            's': s,
            'speedup': amdahl(p, s)
        } for p in [.6, .8, .9, .95] for s in [2, 4, 8, 16]]
    )
    receipts = json.loads((ROOT / 'sources/matrix-receipts.json').read_text())
    cases = receipts['files']['physical-result.json']['content']['cases']
    for c in cases:
        cycles = c['operand_setup_cycles'] + c['issue_to_readback_cycles']
        c['setup_issue_drain_cycles'] = cycles
        c['useful_MACs_per_total_cycle'] = c['useful_MACs'] / cycles
        c['useful_GMAC_s_total'] = c['useful_MACs'] / cycles * F / 1e9
        c['utilization_vs_full_INT8_peak_total'
          ] = None if c['float'] else c['useful_MACs'] / cycles / 256
    emit('measured-matrix.json', cases)
    q = json.loads((ROOT / 'sources/qwen-comparison.json').read_text())
    total = q['candidate']['device_seconds']
    phases = {x['phase']: x['candidate_seconds'] for x in q['phases']}
    emit(
        'qwen-scope-only.json', {
            'scope':
            'Qwen W8/FP32 only, never a Bonsai speedup forecast',
            'matrix_only_zero_time_ceiling':
            total / (total - phases['matrix_cycles']),
            'conversion_matrix_combine_zero_time_ceiling':
            total / q['candidate']['other_seconds'],
            'phases':
            phases,
            'total_seconds':
            total
        }
    )
    # Compare algebraically identical signed INT8 and partitioned add/sub dots.
    # Numerical harness does not model Coral issue, memory timing, or new RTL.
    rng = random.Random(103)
    tests = 0
    actual = 0
    digest = None
    blocks = []
    if args.weights:
        h = hashlib.sha256()
        with args.weights.open('rb') as f:
            for data in iter(lambda: f.read(1 << 20), b''):
                h.update(data)
        digest = h.hexdigest()
        assert digest == manifest['image_sha256']
        with args.weights.open('rb') as f:
            for t in ts:
                for off in [t['offset'],
                            t['offset'] + t['payload_bytes'] - 18]:
                    f.seek(off)
                    b = f.read(18)
                    blocks.append(b)
                    actual += 1
    else:
        for _ in range(394):
            blocks.append(struct.pack('<e', .125) + rng.randbytes(16))
    scale_results = []
    for b in blocks:
        scale = struct.unpack('<e', b[:2])[0]
        assert math.isfinite(scale)
        x = [-128, 127, -1, 0] + [rng.randrange(-128, 128) for _ in range(124)]
        w = [1 if b[2 + i // 8] & (1 << (i % 8)) else -1 for i in range(128)]
        ref = sum(a * v for a, v in zip(x, w))
        for width in [32, 64, 128]:
            partials = [
                sum(x[j] if w[j] > 0 else -x[j]
                    for j in range(i, i + width))
                for i in range(0, 128, width)
            ]
            assert sum(partials) == ref and -16384 <= ref <= 16384
            tests += 1
        scale_results.append(struct.pack('<f', scale * ref).hex())
    emit(
        'numerical-checks.json', {
            'status':
            'PASS',
            'actual_native_blocks':
            actual,
            'partitioned_dot_checks':
            tests,
            'widths': [32, 64, 128],
            'seed':
            103,
            'weights_sha256':
            digest,
            'scaled_results_sha256':
            hashlib.sha256(''.join(scale_results).encode()).hexdigest(),
            'scope':
            'Host algebra check on native Q1 bytes; no RTL/FPGA/model activation quantizer validation'
        }
    )
    print(
        'PASS: generated workload, candidate rooflines, Amdahl targets, physical matrix extraction and numerical checks.'
    )


if __name__ == '__main__': main()
