"""Lossless native PQ2 repacking. No device access or activation quantization."""
import hashlib
import struct

MAGIC = b'BPQ2TILE'
HEADER = 128
LAYOUTS = {
    'native34': (0, 1, 32),
    'padded64': (1, 1, 32),
    'slab16_pq2': (2, 16, 32),
    'slab32_pq2': (3, 32, 32),
    'slab32_radix3': (4, 32, 26)
}


def align(value, boundary=64):
    return (value + boundary - 1) // boundary * boundary


def shape_bytes(n, k, layout):
    if n < 1 or k < 128 or k % 128:
        raise ValueError('Requires positive rows and K divisible by 128')
    _, rows, code_bytes = LAYOUTS[layout]
    groups = n * (k // 128)
    if layout == 'native34':
        body = groups * 34
    elif layout == 'padded64':
        body = groups * 64
    else:
        body = ((n + rows - 1) // rows) * (k // 128) * (rows * code_bytes + 64)
    return {
        'native_payload_bytes': groups * 34,
        'body_bytes': body,
        'tensor_header_bytes': HEADER,
        'tensor_end_padding_bytes': align(body) - body,
        'file_bytes': HEADER + align(body)
    }


def codes(block):
    return [((block[2 + j // 4] >> (2 * (j % 4))) & 3) for j in range(128)]


def to_radix3(block):
    values = codes(block)
    if 3 in values:
        raise ValueError(
            'radix3 is lossless only for admitted ternary weights; code3=+2'
        )
    values += [
        1
    ] * 2  # Last byte has three real trits and two zero-weight pads.
    return bytes(
        sum(values[i + j] * (3**j)
            for j in range(5))
        for i in range(0, 130, 5)
    )


def from_radix3(packed):
    values = []
    for byte in packed:
        if byte >= 243:
            raise ValueError('Noncanonical radix3 byte')
        for _ in range(5):
            values.append(byte % 3)
            byte //= 3
    if values[128:] != [1, 1]:
        raise ValueError('Noncanonical trailing trits')
    return bytes(
        sum(values[i + j] << (2 * j)
            for j in range(4))
        for i in range(0, 128, 4)
    )


def encode(raw, n, k, layout):
    sizes = shape_bytes(n, k, layout)
    if len(raw) != sizes['native_payload_bytes']:
        raise ValueError('Native length mismatch')
    layout_id, rows, code_bytes = LAYOUTS[layout]
    if layout == 'native34':
        body = raw
    elif layout == 'padded64':
        body = b''.join(
            raw[i:i + 34] + bytes(30) for i in range(0, len(raw), 34)
        )
    else:
        pieces = []
        groups = k // 128
        zero = bytes(2) + bytes([0x55]) * 32
        for row0 in range(0, n, rows):
            for group in range(groups):
                blocks = [
                    raw[((row0 + r) * groups + group) *
                        34:((row0 + r) * groups + group + 1) * 34] if row0 +
                    r < n else zero for r in range(rows)
                ]
                pieces.append(
                    b''.join(
                        to_radix3(b) if code_bytes == 26 else b[2:]
                        for b in blocks
                    )
                )
                pieces.append(
                    b''.join(b[:2] for b in blocks) + bytes(64 - rows * 2)
                )
        body = b''.join(pieces)
    if len(body) != sizes['body_bytes']:
        raise ValueError('Internal body size mismatch')
    # 0..47 fields; 48..63 reserved zero; 64..95 native SHA; 96..127 body SHA.
    fields = struct.pack(
        '<8sIIIIIIQQ', MAGIC, 1, layout_id, n, k, rows, 128, len(raw),
        len(body)
    )
    header = fields + bytes(64 - len(fields)) + hashlib.sha256(raw).digest(
    ) + hashlib.sha256(body).digest()
    return header + body + bytes(align(len(body)) - len(body))


def decode(packed):
    if len(packed) < HEADER:
        raise ValueError('Truncated header')
    magic, version, layout_id, n, k, rows, group_size, native_size, body_size = struct.unpack(
        '<8sIIIIIIQQ', packed[:48]
    )
    name = next(
        (key for key, value in LAYOUTS.items() if value[0] == layout_id), None
    )
    if magic != MAGIC or version != 1 or name is None or group_size != 128:
        raise ValueError('Unsupported header')
    sizes = shape_bytes(n, k, name)
    if rows != LAYOUTS[name][1] or native_size != sizes[
            'native_payload_bytes'] or body_size != sizes['body_bytes'] or len(
                packed) != sizes['file_bytes']:
        raise ValueError('Inconsistent shape or length')
    body = packed[HEADER:HEADER + body_size]
    if any(packed[48:64]
           ) or any(packed[HEADER + body_size:]
                    ) or hashlib.sha256(body).digest() != packed[96:128]:
        raise ValueError('Header/padding/hash mismatch')
    out = bytearray(native_size)
    groups = k // 128
    if name == 'native34':
        out[:] = body
    elif name == 'padded64':
        for group in range(n * groups):
            pos = group * 64
            if any(body[pos + 34:pos + 64]):
                raise ValueError('Noncanonical group padding')
            out[group * 34:(group + 1) * 34] = body[pos:pos + 34]
    else:
        code_bytes = LAYOUTS[name][2]
        slab_bytes = rows * code_bytes + 64
        for row in range(n):
            for group in range(groups):
                slab = ((row // rows) * groups + group) * slab_bytes
                source = slab + (row % rows) * code_bytes
                scales = slab + rows * code_bytes + (row % rows) * 2
                code = body[source:source + code_bytes]
                block = body[scales:scales + 2] + (
                    from_radix3(code) if code_bytes == 26 else code
                )
                offset = (row * groups + group) * 34
                out[offset:offset + 34] = block
    if hashlib.sha256(out).digest() != packed[64:96]:
        raise ValueError('Native checksum mismatch')
    # Canonical encode also checks masked-row and scale-padding bytes.
    if encode(bytes(out), n, k, name) != packed:
        raise ValueError('Noncanonical layout')
    return bytes(out)
