"""Executable proposal checks, not a Coral LSU/DMA/hardware simulation."""

import argparse
import hashlib
import json
from pathlib import Path
import random
import re

ROOT = Path(__file__).resolve().parents[3]


class Fault(Exception):

    def __init__(self, cause, address):
        self.cause, self.address = cause, address


class Memory:

    def __init__(self, base=0x10000, size=128):
        self.base = base
        self.data = bytearray([0xa5] * size)
        self.events = []
        self.enabled = True

    def bounds(self, address, size, cause):
        if address < self.base or address + size > self.base + len(self.data):
            bad = address if address < self.base or address >= self.base + len(
                self.data
            ) else self.base + len(self.data)
            raise Fault(cause, bad)
        if address + size > 2**32:
            raise Fault(cause, address)

    def arguments(self, address, count, cause, vstart):
        if not self.enabled or vstart != 0 or not 0 <= count <= 64:
            raise Fault(2, None)
        size = (count + 3) // 4
        if size:
            self.bounds(address, size, cause)
        return size

    def beats(self, address, size):
        end = address + size
        while address < end:
            n = min(end - address, 16 - address % 16)
            yield address, n
            address += n

    def load(self, old, address, count, fail=None, vstart=0):
        size = self.arguments(address, count, 5, vstart)
        if not size:
            return old
        payload = bytearray()
        for index, (addr, n) in enumerate(self.beats(address, size)):
            self.events.append(('read', addr, n))
            if index == fail:
                raise Fault(5, addr)
            offset = addr - self.base
            payload.extend(self.data[offset:offset + n])
        return bytes(payload) + bytes(16 - size)

    def store(
        self, vector, address, count, fail=None, vstart=0, fail_written=0
    ):
        size = self.arguments(address, count, 7, vstart)
        offset = 0
        for index, (addr, n) in enumerate(self.beats(address, size)):
            self.events.append(('write', addr, n))
            if index == fail:
                # Exercise permitted partial effects within the failing beat.
                changed = min(n, fail_written)
                dst = addr - self.base
                self.data[dst:dst + changed] = vector[offset:offset + changed]
                raise Fault(7, addr)
            dst = addr - self.base
            self.data[dst:dst + n] = vector[offset:offset + n]
            offset += n


def disjoint(a, an, b, bn):
    return a + an <= b or b + bn <= a


def stage_native(memory, src, dst, rows, groups):
    if not disjoint(src, 34 * rows * groups, dst, 1088 * groups):
        raise ValueError('Overlapping staging allocations')
    memory.bounds(src, 34 * rows * groups, 5)
    memory.bounds(dst, 1088 * groups, 7)
    for g in range(groups):
        for row in range(32):
            target = dst + 1088 * g + 32 * row
            if row < rows:
                origin = src + (row * groups + g) * 34
                for k in (0, 16):
                    vector = memory.load(bytes(16), origin + 2 + k, 64)
                    memory.store(vector, target + k, 64)
                scale = bytes(
                    memory.data[origin - memory.base:origin - memory.base + 2]
                )
            else:
                memory.store(bytes([0x55] * 16), target, 64)
                memory.store(bytes([0x55] * 16), target + 16, 64)
                scale = bytes(2)
            off = dst - memory.base + 1088 * g + 1024 + 2 * row
            memory.data[off:off +
                        2] = scale  # Existing LHU/SH fallback semantics.


def unpack(codes, count):
    result = []
    for i in range(count):
        code = (codes[i // 4] >> (2 * (i % 4))) & 3
        if code == 3:
            raise ValueError('Reserved active code')
        result.append(code - 1)
    return result


def pack(values):
    output = bytearray([0x55] * ((len(values) + 3) // 4))
    for i, value in enumerate(values):
        if value not in (-1, 0, 1):
            raise ValueError('Conversion requires an exact ternary value')
        shift = 2 * (i % 4)
        output[i // 4] = (output[i // 4]
                          & ~(3 << shift)) | ((int(value) + 1) << shift)
    return bytes(output)


class Slots:
    """Software ownership protocol model, not hardware locks or queue RTL."""

    def __init__(self, ranges):
        if not disjoint(*ranges[0], *ranges[1]):
            raise ValueError('Overlapping slots')
        self.generation = 1
        self.states = ['FREE', 'FREE']
        self.ticket = None
        self.next_ticket = 1

    def fill(self, slot):
        if self.states[slot] not in ('FREE', 'INVALID'):
            raise ValueError('Slot is not CPU writable')
        self.states[slot] = 'FILLING'

    def ready(self, slot, copy_ok):
        if self.states[slot] != 'FILLING':
            raise ValueError('No fill in progress')
        self.states[slot] = 'READY' if copy_ok else 'INVALID'

    def submit(self, slot):
        if self.ticket is not None or self.states[slot] != 'READY':
            raise ValueError('Queue busy or slot not ready')
        self.ticket = (slot, self.generation, self.next_ticket)
        self.next_ticket += 1
        self.states[slot] = 'INFLIGHT'
        return self.ticket

    def reap(self, ticket, terminal, success=True):
        if ticket != self.ticket:
            raise ValueError('Stale ticket/generation')
        if terminal:
            self.states[ticket[0]] = 'COMPLETE' if success else 'INVALID'
            self.ticket = None

    def consumed(self, slot):
        if self.states[slot] != 'COMPLETE':
            raise ValueError('Output not complete')
        self.states[slot] = 'FREE'

    def reset(self, drained):
        if not drained:
            raise ValueError('External epoch must drain/invalidate first')
        self.generation += 1
        self.ticket = None
        self.states = ['INVALID', 'INVALID']


class Checks:

    def __init__(self):
        self.count = 0

    def equal(self, actual, expected):
        self.count += 1
        if actual != expected:
            raise AssertionError((actual, expected))

    def fails(self, callback, kind, cause=None):
        self.count += 1
        try:
            callback()
        except kind as error:
            if cause is not None and error.cause != cause:
                raise AssertionError('Wrong fault cause') from error
            return error
        raise AssertionError('Expected rejection')


def run_checks():
    checks = Checks()
    rng = random.Random(103)
    # Independent expected bytes use a snapshot/slice, not load/store helpers.
    for count in range(65):
        for alignment in range(16):
            memory = Memory()
            memory.data[:] = bytes(rng.randrange(256) for _ in memory.data)
            before = bytes(memory.data)
            old = bytes([0xcc] * 16)
            size = (count + 3) // 4
            vector = memory.load(old, memory.base + alignment, count)
            expected = before[alignment:alignment +
                              size] + bytes(16 - size) if size else old
            checks.equal(vector, expected)
            memory.store(vector, memory.base + 64 + alignment, count)
            after = bytearray(before)
            after[64 + alignment:64 + alignment + size] = vector[:size]
            checks.equal(memory.data, after)
            checks.equal(sum(n for _, _, n in memory.events), 2 * size)

    # Every possible code byte survives all partial-byte tails, including 11.
    for byte in range(256):
        for count in range(1, 5):
            memory = Memory()
            memory.data[0] = byte
            memory.store(
                memory.load(bytes(16), memory.base, count), memory.base + 16,
                count
            )
            checks.equal(memory.data[16], byte)
    for count in range(65):
        values = [rng.choice((-1, 0, 1)) for _ in range(count)]
        checks.equal(unpack(pack(values), count), values)
    checks.fails(lambda: unpack(bytes([0xff]), 1), ValueError)
    checks.equal(
        unpack(bytes([0xfc]), 1), [-1]
    )  # Inactive 11 is not consumed.
    for value in (-2, 2, 0.5, float('nan')):
        checks.fails(lambda: pack([value]), ValueError)

    # Faults preserve load destination and never touch neighboring store bytes.
    for alignment in range(1, 16):
        for fail in (0, 1):
            memory = Memory()
            old = bytes([0xcc] * 16)
            vector = old

            def failed_load():
                nonlocal vector
                vector = memory.load(
                    old, memory.base + alignment, 64, fail=fail
                )

            fault = checks.fails(failed_load, Fault, 5)
            checks.equal(vector, old)
            checks.equal(
                fault.address, memory.base + (alignment if fail == 0 else 16)
            )
            checks.fails(
                lambda: memory.store(
                    bytes(range(16)), memory.base + alignment, 64, fail=fail
                ), Fault, 7
            )
            expected = bytearray([0xa5] * 128)
            if fail == 1:
                expected[alignment:16] = bytes(range(16 - alignment))
            checks.equal(memory.data, expected)
            # Full retry is safe for this exclusively owned normal-memory buffer.
            memory.store(bytes(range(16)), memory.base + alignment, 64)
            checks.equal(
                memory.data[alignment:alignment + 16], bytes(range(16))
            )

    for alignment in range(1, 16):
        for fail in (0, 1):
            beat_size = 16 - alignment if fail == 0 else alignment
            for changed in range(beat_size + 1):
                memory = Memory()
                checks.fails(
                    lambda: memory.store(
                        bytes(range(16)),
                        memory.base + alignment,
                        64,
                        fail=fail,
                        fail_written=changed
                    ), Fault, 7
                )
                completed = changed if fail == 0 else 16 - alignment + changed
                expected = bytearray([0xa5] * 128)
                expected[alignment:alignment +
                         completed] = bytes(range(completed))
                checks.equal(memory.data, expected)

    for address in (0xffff, 0x10080, 0x10079, 0xfffffff8):
        for store in (False, True):
            memory = Memory()
            operation = memory.store if store else memory.load
            checks.fails(
                lambda: operation(bytes(16), address, 64), Fault,
                7 if store else 5
            )
            checks.equal(memory.events, [])
    memory = Memory(base=0xfffffff0, size=32)
    checks.fails(lambda: memory.load(bytes(16), 0xfffffff8, 64), Fault, 5)
    checks.equal(memory.events, [])
    memory = Memory()
    for count in (65, 0xffffffff, -1):
        checks.fails(
            lambda: memory.load(bytes(16), memory.base, count), Fault, 2
        )
        checks.fails(
            lambda: memory.store(bytes(16), memory.base, count), Fault, 2
        )
    checks.equal(memory.load(bytes([7] * 16), 0xffffffff, 0), bytes([7] * 16))
    memory.store(bytes(16), 0xffffffff, 0)
    checks.fails(
        lambda: memory.load(bytes(16), memory.base, 0, vstart=1), Fault, 2
    )
    memory.enabled = False
    checks.fails(lambda: memory.store(bytes(16), memory.base, 0), Fault, 2)
    checks.equal(memory.events, [])

    # Independent native reconstruction checks actual slab offsets and stride.
    for rows, groups in ((1, 1), (3, 2), (32, 16)):
        native_size = rows * groups * 34
        src = 0x10002
        dst = (src + native_size + 31) & ~15
        memory = Memory(size=dst - 0x10000 + groups * 1088 + 16)
        raw = bytes(rng.randrange(256) for _ in range(native_size))
        memory.data[2:2 + native_size] = raw
        stage_native(memory, src, dst, rows, groups)
        rebuilt = bytearray()
        for row in range(rows):
            for g in range(groups):
                base = dst - memory.base + g * 1088
                rebuilt += memory.data[base + 1024 + row * 2:base + 1026 +
                                       row * 2]
                rebuilt += memory.data[base + row * 32:base + row * 32 + 32]
        checks.equal(rebuilt, raw)
        checks.equal(memory.data[-16:], bytes([0xa5] * 16))
        checks.fails(
            lambda: stage_native(memory, src, src + 16, rows, groups),
            ValueError
        )

    # Exhaustive raw FP16-bit preservation through the explicit LHU/SH scale path.
    for bits in range(65536):
        raw = bits.to_bytes(2, 'little')
        unsigned_halfword = raw[0] | raw[1] << 8
        checks.equal(
            bytes((unsigned_halfword & 255, unsigned_halfword >> 8)), raw
        )

    slots = Slots(((0x10000, 4096), (0x11000, 4096)))
    checks.fails(lambda: Slots(((0x10000, 4096), (0x10800, 4096))), ValueError)
    slots.fill(0)
    slots.ready(0, False)
    checks.fails(lambda: slots.submit(0), ValueError)
    slots.fill(0)
    slots.ready(0, True)
    ticket = slots.submit(0)
    checks.fails(lambda: slots.fill(0), ValueError)
    slots.fill(1)
    slots.ready(1, True)
    checks.fails(lambda: slots.submit(1), ValueError)
    slots.reap(ticket, False)
    checks.equal(slots.states[0], 'INFLIGHT')
    checks.fails(lambda: slots.consumed(0), ValueError)
    slots.reap(ticket, True)
    checks.fails(lambda: slots.fill(0), ValueError)
    slots.consumed(0)
    ticket2 = slots.submit(1)
    checks.fails(lambda: slots.reset(False), ValueError)
    slots.reset(True)
    checks.equal(slots.states, ['INVALID', 'INVALID'])
    checks.fails(lambda: slots.reap(ticket2, True), ValueError)
    slots.fill(1)
    slots.ready(1, True)
    ticket3 = slots.submit(1)
    checks.fails(lambda: slots.reap(ticket2, True), ValueError)
    slots.reap(ticket3, True, False)
    checks.equal(slots.states[1], 'INVALID')
    footprints = {
        f'M{m}_G{g}': g * (1088 + 128 * m + 32) + 160 * m + 64
        for m, g in ((1, 1), (1, 16), (8, 16), (1, 48), (8, 48))
    }
    checks.equal(
        list(footprints.values()), [1472, 20192, 35648, 60128, 104256]
    )
    return {'checks': checks.count, 'seed': 103, 'footprint_bytes': footprints}


def audit_sources(alex):
    paths = [
        'hdl/chisel/src/coralnpu/Parameters.scala',
        'hdl/chisel/src/coralnpu/CoreAxi.scala',
        'hdl/chisel/src/coralnpu/TCM.scala',
        'hdl/chisel/src/coralnpu/SRAM.scala',
        'hdl/chisel/src/coralnpu/Fabric.scala',
        'hdl/chisel/src/coralnpu/BankedDtcm.scala',
        'hdl/chisel/src/bus/TlulLocalXbar.scala',
        'hdl/chisel/src/coralnpu/scalar/Lsu.scala',
        'hdl/chisel/src/coralnpu/rvv/RvvInterface.scala',
    ]
    result = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in paths
    }
    for name in ('CoralF2Axi.sv', 'TCM128_1.sv', 'FabricMux.sv',
                 'FabricArbiter.sv'):
        data = (alex / name).read_bytes()
        result['alex/input/core/rtl/' +
               name] = hashlib.sha256(data).hexdigest()
    top = (alex / 'CoralF2Axi.sv').read_text()
    tcm = (alex / 'TCM128_1.sv').read_text()
    mux = (alex / 'FabricMux.sv').read_text()
    if 'TCM128_1 dtcm (' not in top or not re.search(
            r'input\s+\[10:0\]\s+io_addr', tcm):
        raise ValueError('Frozen DTCM instance/row count changed')
    if 'addr[31:16]' not in mux or "addr < 32'h18000" not in mux:
        raise ValueError('Frozen DTCM address decode changed')
    if 'BankedDtcm' in top:
        raise ValueError('Frozen AXI top now has a different banked path')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--alex-rtl', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    report = run_checks()
    report.update(
        status='PASS',
        scope=
        'Python specification model and read-only source inspection; no RTL execution',
        source_sha256=audit_sources(args.alex_rtl),
        checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        dtcm_base='0x00010000',
        dtcm_bytes=32768,
        service_bits=128,
        service_ports=1,
        independent_engine_port=False
    )
    args.out.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
