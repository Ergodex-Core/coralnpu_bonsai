"""Bounded OCL control and PCIS XDMA transports; importing never opens hardware."""
import ctypes as C
import multiprocessing
import os
import time

DDR_BASE = 0x20000000
DDR_BYTES = 0x80000000
CSR = 0x30000
DDR_CSR = 0x40000
CHUNK_BYTES = 1 << 20
TCM_REGIONS = ((0, 0x2000), (0x10000, 0x18000))


def in_tcm(address, size):
    return size >= 0 and any(
        lo <= address and address + size <= hi for lo, hi in TCM_REGIONS
    )


def ddr_offset(address, size):
    if not isinstance(address, int) or not isinstance(
            size, int) or size < 0 or not (DDR_BASE <= address and address +
                                           size <= DDR_BASE + DDR_BYTES):
        raise ValueError('DDR address/length outside the mapped 2 GiB window')
    return address - DDR_BASE


def _sdk_worker(pipe, slot, mode, buffer):
    """A blocked SDK/DMA call cannot block the supervising Python process.

    OCL and DMA have independent workers, so a DMA timeout still permits reset.
    DMA signatures match AWS HDK 2.3.0 fpga_dma.h. SDK performs short-I/O loops.
    """
    handle, readfd, writefd = C.c_int(-1), -1, -1
    lib = None
    try:
        lib = C.CDLL('/usr/local/lib/libfpga_mgmt.so')

        def check(rc):
            if rc:
                raise RuntimeError(f'AWS FPGA SDK returned {rc}')

        lib.fpga_pci_init.argtypes = []
        check(lib.fpga_pci_init())
        if mode == 'ocl':
            lib.fpga_pci_attach.argtypes = [
                C.c_int, C.c_int, C.c_int, C.c_uint32,
                C.POINTER(C.c_int)
            ]
            lib.fpga_pci_detach.argtypes = [C.c_int]
            lib.fpga_pci_peek.argtypes = [
                C.c_int, C.c_uint64,
                C.POINTER(C.c_uint32)
            ]
            lib.fpga_pci_poke.argtypes = [C.c_int, C.c_uint64, C.c_uint32]
            check(lib.fpga_pci_attach(slot, 0, 0, 0, C.byref(handle)))
        else:
            lib.fpga_dma_open_queue.argtypes = [
                C.c_int, C.c_int, C.c_int, C.c_bool
            ]
            lib.fpga_dma_burst_read.argtypes = [
                C.c_int, C.c_void_p, C.c_size_t, C.c_size_t
            ]
            lib.fpga_dma_burst_write.argtypes = [
                C.c_int, C.c_void_p, C.c_size_t, C.c_size_t
            ]
            # FPGA_DMA_XDMA = 1; resolve slot through SDK, never guess /dev IDs.
            readfd = lib.fpga_dma_open_queue(1, slot, 0, True)
            writefd = lib.fpga_dma_open_queue(1, slot, 0, False)
            if readfd < 0 or writefd < 0:
                raise RuntimeError('cannot open the installed XDMA channel 0')
            aligned = (C.addressof(buffer) + 4095) & ~4095
        pipe.send((True, None))
        while True:
            operation, address, value = pipe.recv()
            if operation == 'close':
                break
            if mode == 'ocl' and operation == 'read':
                result = C.c_uint32()
                check(lib.fpga_pci_peek(handle, address, C.byref(result)))
                pipe.send((True, result.value))
            elif mode == 'ocl' and operation == 'write':
                check(lib.fpga_pci_poke(handle, address, value))
                pipe.send((True, None))
            elif mode == 'dma' and operation == 'read':
                check(lib.fpga_dma_burst_read(readfd, aligned, value, address))
                pipe.send((True, value))
            elif mode == 'dma' and operation == 'write':
                check(
                    lib.fpga_dma_burst_write(writefd, aligned, value, address)
                )
                pipe.send((True, None))
            else:
                raise ValueError('invalid worker operation')
        if handle.value != -1:
            check(lib.fpga_pci_detach(handle))
            handle.value = -1
        for fd in (readfd, writefd):
            if fd >= 0:
                os.close(fd)
        readfd = writefd = -1
        pipe.send((True, None))
    except Exception as exc:
        try:
            pipe.send((False, f'{type(exc).__name__}: {exc}'))
        except (OSError, EOFError):
            pass
    finally:
        if lib is not None and handle.value != -1:
            lib.fpga_pci_detach(handle)
        for fd in (readfd, writefd):
            if fd >= 0:
                os.close(fd)
        pipe.close()


class Worker:

    def __init__(self, slot, mode, timeout=5, target=_sdk_worker):
        self.timeout = timeout
        self.mode = mode
        context = multiprocessing.get_context('spawn')
        # Only small control messages use Pipe. A large pipe.send(payload) can
        # otherwise block indefinitely if its child dies halfway through I/O.
        self.buffer = context.RawArray('B', CHUNK_BYTES + 4095)
        self.aligned = (C.addressof(self.buffer) + 4095) & ~4095
        self.pipe, child = context.Pipe()
        self.process = context.Process(
            target=target, args=(child, slot, mode, self.buffer), daemon=True
        )
        self.process.start()
        child.close()
        self.receive('attach', float('inf'))

    def receive(self, operation, deadline):
        wait = min(self.timeout, deadline - time.monotonic())
        if wait <= 0 or not self.pipe.poll(wait):
            self.stop()
            raise TimeoutError(f'SDK {operation} timed out; worker stopped')
        try:
            ok, value = self.pipe.recv()
        except (EOFError, OSError) as exc:
            self.stop()
            raise RuntimeError(
                f'SDK worker exited during {operation}'
            ) from exc
        if not ok:
            self.stop()
            raise RuntimeError(value)
        return value

    def request(self, operation, address, value, deadline):
        if not self.process.is_alive():
            raise RuntimeError('SDK worker is no longer alive')
        if time.monotonic() >= deadline:
            raise TimeoutError('operation deadline expired')
        if self.mode == 'dma' and operation == 'write':
            C.memmove(self.aligned, value, len(value))
            value = len(value)
        self.pipe.send((operation, address, value))
        result = self.receive(operation, deadline)
        if self.mode == 'dma' and operation == 'read':
            if result != value:
                raise RuntimeError('DMA worker returned an incorrect length')
            return C.string_at(self.aligned, value)
        return result

    def stop(self):
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=0.5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=0.5)
        self.pipe.close()

    def close(self):
        try:
            if self.process.is_alive():
                self.request('close', 0, None, time.monotonic() + self.timeout)
        finally:
            self.stop()


class Device:

    def __init__(self, slot, timeout=5):
        self.slot, self.timeout = slot, timeout
        self.deadline = float('inf')
        self.ocl = Worker(slot, 'ocl', timeout)
        self.dma = None  # Open DMA only after checking image, reset, and DDR ABI.

    def read(self, address):
        if address % 4 or not (in_tcm(address, 4) or address in
                               (CSR, CSR + 4, CSR + 8, DDR_CSR, DDR_CSR + 4,
                                DDR_CSR + 8, DDR_CSR + 12, DDR_CSR + 16)):
            raise ValueError('MMIO read outside TCM/control/DDR status')
        return self.ocl.request('read', address, None, self.deadline)

    def write(self, address, value):
        if address % 4 or not (in_tcm(address, 4)
                               or address in (CSR, CSR + 4)):
            raise ValueError('MMIO write outside TCM/control')
        if not isinstance(value, int) or not 0 <= value <= 0xffffffff:
            raise ValueError('MMIO value is not uint32')
        return self.ocl.request('write', address, value, self.deadline)

    def _transfer(self, operation, address, value):
        size = value if operation == 'read' else len(value)
        offset = ddr_offset(address, size)
        if address % 64 or not 0 < size <= CHUNK_BYTES or size % 64:
            raise ValueError(
                'DMA accesses must be 64-byte aligned, 64 bytes..1 MiB'
            )
        if self.dma is None:
            self.dma = Worker(self.slot, 'dma', self.timeout)
        return self.dma.request(operation, offset, value, self.deadline)

    def read_ddr(self, address, size):
        return self._transfer('read', address, size)

    def write_ddr(self, address, data):
        return self._transfer('write', address, bytes(data))

    def emergency_reset(self):
        """Try a fresh bounded OCL worker after a failed control operation."""
        self.ocl.stop()
        self.ocl = Worker(self.slot, 'ocl', self.timeout)
        self.deadline = time.monotonic() + self.timeout
        self.write(CSR, 1)
        if self.read(CSR) != 1:
            raise RuntimeError('emergency reset readback failed')

    def close(self):
        errors = []
        for worker in (self.dma, self.ocl):
            if worker is not None:
                try:
                    worker.close()
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise RuntimeError('; '.join(errors))


def read_tcm(device, address, size):
    if not in_tcm(address, size):
        raise ValueError('read outside TCM')
    raw = b''.join(
        device.read(a).to_bytes(4, 'little')
        for a in range(address & ~3, (address + size + 3) & ~3, 4)
    )
    return raw[address % 4:address % 4 + size]


def write_tcm(device, address, data):
    if not in_tcm(address, len(data)):
        raise ValueError('write outside TCM')
    for a in range(address & ~3, (address + len(data) + 3) & ~3, 4):
        lo, hi = max(a, address), min(a + 4, address + len(data))
        word = bytearray(device.read(a).to_bytes(4, 'little')
                         ) if hi - lo < 4 else bytearray(4)
        word[lo - a:hi - a] = data[lo - address:hi - address]
        device.write(a, int.from_bytes(word, 'little'))


def hold_reset(device, timeout=5):
    device.deadline = time.monotonic() + timeout
    device.write(CSR, 1)
    while device.read(CSR) != 1 or device.read(CSR + 8) & 3:
        if time.monotonic() >= device.deadline:
            raise TimeoutError('core reset/status failed to clear')
        time.sleep(0.001)


def check_ddr(device):
    expected = ((0, 0x43444452), (8, DDR_BYTES), (12, DDR_BASE), (16, 1))
    for offset, value in expected:
        if device.read(DDR_CSR + offset) != value:
            raise RuntimeError(f'DDR ABI mismatch at {DDR_CSR + offset:#x}')
    status = device.read(DDR_CSR + 4)
    if status & 7 != 3:
        raise RuntimeError(
            f'DDR is not calibrated/present or has a sticky fault: {status:#x}'
        )
