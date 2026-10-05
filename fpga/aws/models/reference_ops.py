"""Independent, deliberately scalar host reference for validation only.

These functions are never called by the hardware loader or target inference path.
Python struct defines storage rounding; FP32 multiply/add rounds separately.
"""
import math
import struct


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def bf16(data):
    if len(data) % 2: raise ValueError("partial BF16")
    return [
        struct.unpack("<f", b"\0\0" + data[i:i + 2])[0]
        for i in range(0, len(data), 2)
    ]


def pq2(data):
    if len(data) % 34: raise ValueError("partial PQ2_0 group")
    values = []
    for group in range(0, len(data), 34):
        scale = struct.unpack_from("<e", data, group)[0]
        for byte in data[group + 2:group + 34]:
            for shift in (0, 2, 4, 6):
                values.append(f32(scale * (((byte >> shift) & 3) - 1)))
    return values


def dot(weights, x):
    if len(weights) != len(x): raise ValueError("dimension mismatch")
    acc = 0.0
    for w, a in zip(weights, x):
        acc = f32(acc + f32(w * a))
    return acc


def rmsnorm(x, weights, eps):
    squares = 0.0
    for a in x:
        squares = f32(squares + f32(a * a))
    inverse = f32(1.0 / f32(math.sqrt(f32(f32(squares / len(x)) + eps))))
    return [f32(f32(a * inverse) * w) for a, w in zip(x, weights)]


def softmax(x):
    peak = max(x)
    values = [f32(math.exp(f32(a - peak))) for a in x]
    denom = 0.0
    for a in values:
        denom = f32(denom + a)
    return [f32(a / denom) for a in values]


def silu(x):
    return [f32(a / f32(1 + f32(math.exp(-a)))) for a in x]
