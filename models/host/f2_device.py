#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Host access to the CoralNPU AWS F2 image (agfi-075a98d106eb1b6bf).

Address map seen by the core and by this host library:

  0x00000000  ITCM   8 KiB   BAR0 (OCL, 32-bit words)
  0x00010000  DTCM  32 KiB   BAR0
  0x00030000  CSR            BAR0: +0 reset, +4 start PC, +8 status
  0x80000000  DDR    1 GiB   BAR4 (PCIS) offset 0x0
  0xC0000000  HBM    1 GiB   BAR4 offset 0x10_0000_0000

The AWS SDK calls run in a child process so a wedged MMIO access cannot hang
the caller: every operation has a host-side timeout.
"""

import ctypes as C
import multiprocessing
import struct
import subprocess
import time

AGFI = "agfi-075a98d106eb1b6bf"
SHELL = "0x10212415"

ITCM = (0x00000000, 8 * 1024)
DTCM = (0x00010000, 32 * 1024)
CSR = 0x00030000
CSR_RESET, CSR_PC, CSR_STATUS = CSR, CSR + 4, CSR + 8
STATUS_HALTED, STATUS_FAULT = 1, 2
DDR_WINDOW = (0x80000000, 1 << 30)
HBM_WINDOW = (0xC0000000, 1 << 30)
HBM_BAR4_BASE = 0x10_0000_0000
SENTINEL = 0x0BADD00D


def region(address, size=0):
  """Returns 'tcm', 'csr', 'ddr' or 'hbm' for a range, else None."""
  for name, (base, length) in (("tcm", ITCM), ("tcm", DTCM), ("csr", (CSR, 0x1000)),
                               ("ddr", DDR_WINDOW), ("hbm", HBM_WINDOW)):
    if base <= address and address + size <= base + length:
      return name
  return None


def bar4_offset(address):
  if region(address) == "ddr":
    return address - DDR_WINDOW[0]
  if region(address) == "hbm":
    return HBM_BAR4_BASE + (address - HBM_WINDOW[0])
  raise ValueError(f"{address:#x} is not in the DDR or HBM window")


def _sdk_worker(pipe, slot):
  bar0 = C.c_int(-1)
  bar4 = C.c_int(-1)
  lib = None
  try:
    lib = C.CDLL("/usr/local/lib/libfpga_mgmt.so")
    lib.fpga_pci_init.argtypes = []
    lib.fpga_pci_attach.argtypes = [C.c_int, C.c_int, C.c_int, C.c_uint32,
                                    C.POINTER(C.c_int)]
    lib.fpga_pci_detach.argtypes = [C.c_int]
    lib.fpga_pci_peek.argtypes = [C.c_int, C.c_uint64, C.POINTER(C.c_uint32)]
    lib.fpga_pci_poke.argtypes = [C.c_int, C.c_uint64, C.c_uint32]
    lib.fpga_pci_peek8.argtypes = [C.c_int, C.c_uint64, C.POINTER(C.c_uint8)]
    lib.fpga_pci_poke8.argtypes = [C.c_int, C.c_uint64, C.c_uint8]
    lib.fpga_pci_write_burst.argtypes = [C.c_int, C.c_uint64,
                                         C.POINTER(C.c_uint32), C.c_uint64]
    lib.fpga_pci_get_address.argtypes = [C.c_int, C.c_uint64, C.c_uint64,
                                         C.POINTER(C.c_void_p)]
    lib.fpga_mgmt_init.argtypes = []
    lib.fpga_mgmt_get_vLED_status.argtypes = [C.c_int, C.POINTER(C.c_uint16)]

    def check(rc, what):
      if rc:
        raise RuntimeError(f"AWS FPGA SDK {what} returned {rc}")

    check(lib.fpga_mgmt_init(), "fpga_mgmt_init")
    check(lib.fpga_pci_init(), "fpga_pci_init")
    check(lib.fpga_pci_attach(slot, 0, 0, 0, C.byref(bar0)), "attach BAR0")
    check(lib.fpga_pci_attach(slot, 0, 4, 1, C.byref(bar4)), "attach BAR4")
    pipe.send((True, None))
    while True:
      op, address, payload = pipe.recv()
      if op == "close":
        lib.fpga_pci_detach(bar4)
        lib.fpga_pci_detach(bar0)
        bar0.value = bar4.value = -1
        pipe.send((True, None))
        return
      if op == "read32":
        value = C.c_uint32()
        check(lib.fpga_pci_peek(bar0, address, C.byref(value)), "peek")
        pipe.send((True, value.value))
      elif op == "write32":
        check(lib.fpga_pci_poke(bar0, address, payload), "poke")
        pipe.send((True, None))
      elif op == "vled":
        value = C.c_uint16()
        check(lib.fpga_mgmt_get_vLED_status(slot, C.byref(value)), "vLED")
        pipe.send((True, value.value))
      elif op == "mem_write":
        offset, data = bar4_offset(address), payload
        head = (-offset) % 4
        for i in range(min(head, len(data))):
          check(lib.fpga_pci_poke8(bar4, offset + i, data[i]), "poke8")
        body = (len(data) - head) // 4 * 4
        if body > 0:
          words = (C.c_uint32 * (body // 4)).from_buffer_copy(data[head:head + body])
          check(lib.fpga_pci_write_burst(bar4, offset + head, words, body // 4),
                "write_burst")
        for i in range(head + body, len(data)):
          check(lib.fpga_pci_poke8(bar4, offset + i, data[i]), "poke8")
        pipe.send((True, None))
      elif op == "mem_read":
        offset, size = bar4_offset(address), payload
        ptr = C.c_void_p()
        check(lib.fpga_pci_get_address(bar4, offset, (size + 3) // 4,
                                       C.byref(ptr)), "get_address")
        pipe.send((True, C.string_at(ptr.value, size)))
      else:
        raise ValueError(f"unknown SDK operation {op}")
  except Exception as exc:  # pylint: disable=broad-except
    try:
      pipe.send((False, f"{type(exc).__name__}: {exc}"))
    except (OSError, EOFError):
      pass
  finally:
    if lib is not None:
      if bar4.value != -1:
        lib.fpga_pci_detach(bar4)
      if bar0.value != -1:
        lib.fpga_pci_detach(bar0)
    pipe.close()


class F2Device:
  """Process-isolated access to BAR0 (TCM/CSR) and BAR4 (DDR/HBM)."""

  def __init__(self, slot=0, timeout=5.0):
    self.slot = slot
    self.timeout = timeout
    self.deadline = float("inf")
    context = multiprocessing.get_context("spawn")
    self.pipe, child = context.Pipe()
    self.worker = context.Process(target=_sdk_worker, args=(child, slot),
                                  daemon=True)
    self.worker.start()
    child.close()
    self._receive("attach")

  def _receive(self, what):
    remaining = min(self.timeout, self.deadline - time.monotonic())
    if remaining <= 0 or not self.pipe.poll(remaining):
      self.stop()
      raise TimeoutError(f"SDK {what} exceeded {self.timeout}s; worker stopped")
    try:
      ok, result = self.pipe.recv()
    except (EOFError, OSError):
      self.stop()
      raise RuntimeError(f"SDK worker exited during {what}")
    if not ok:
      self.stop()
      raise RuntimeError(result)
    return result

  def _call(self, op, address=0, payload=None):
    self.pipe.send((op, address, payload))
    return self._receive(f"{op} at {address:#x}")

  # 32-bit control-plane accesses (BAR0).
  def read32(self, address):
    if address % 4 or region(address, 4) not in ("tcm", "csr"):
      raise ValueError(f"not a TCM/CSR word: {address:#x}")
    return self._call("read32", address)

  def write32(self, address, value):
    if address % 4 or region(address, 4) not in ("tcm", "csr"):
      raise ValueError(f"not a TCM/CSR word: {address:#x}")
    if address == CSR_STATUS or not 0 <= value <= 0xFFFFFFFF:
      raise ValueError("invalid write")
    self._call("write32", address, value)

  # Byte-granular accesses routed by region.
  def read_bytes(self, address, size):
    kind = region(address, size)
    if kind in ("ddr", "hbm"):
      return self._call("mem_read", address, size)
    if kind != "tcm":
      raise ValueError(f"unmapped read {address:#x}+{size}")
    out = b""
    for word in range(address & ~3, (address + size + 3) & ~3, 4):
      out += self.read32(word).to_bytes(4, "little")
    return out[address % 4:address % 4 + size]

  def write_bytes(self, address, data):
    kind = region(address, len(data))
    if kind in ("ddr", "hbm"):
      self._call("mem_write", address, bytes(data))
      return
    if kind != "tcm":
      raise ValueError(f"unmapped write {address:#x}+{len(data)}")
    end = address + len(data)
    for word in range(address & ~3, (end + 3) & ~3, 4):
      lo, hi = max(word, address), min(word + 4, end)
      value = (bytearray(4) if hi - lo == 4 else
               bytearray(self.read32(word).to_bytes(4, "little")))
      value[lo - word:hi - word] = data[lo - address:hi - address]
      self.write32(word, int.from_bytes(value, "little"))

  def vled(self):
    return self._call("vled")

  def wait_memory_ready(self, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
      leds = self.vled()
      if leds & 0b11 == 0b11:  # ddr_ready, hbm_ready
        return leds
      if time.monotonic() > deadline:
        raise TimeoutError(f"DDR/HBM not ready (vLED={leds:#06x}); "
                           f"is {AGFI} loaded in slot {self.slot}?")
      time.sleep(0.01)

  # Core control.
  def hold_reset(self, timeout=2.0):
    self.write32(CSR_RESET, 1)  # reset asserted, clock enabled
    deadline = time.monotonic() + timeout
    while self.read32(CSR_RESET) != 1 or self.read32(CSR_STATUS) & 3:
      if time.monotonic() > deadline:
        raise TimeoutError("reset/status did not clear; stale halt?")
      time.sleep(0.001)

  def start(self, entry):
    self.write32(CSR_PC, entry)
    if self.read32(CSR_PC) != entry:
      raise RuntimeError("start PC readback failed")
    self.write32(CSR_RESET, 0)

  def wait_halt(self, timeout):
    deadline = time.monotonic() + timeout
    sleep = 1e-4
    while True:
      status = self.read32(CSR_STATUS)
      if status & STATUS_FAULT:
        raise RuntimeError(f"core fault, status={status:#x}")
      if status & STATUS_HALTED:
        return status
      if time.monotonic() > deadline:
        raise TimeoutError("core did not halt before timeout")
      time.sleep(sleep)
      sleep = min(sleep * 2, 0.01)

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
        self.pipe.send(("close", 0, None))
        self._receive("detach")
    finally:
      self.stop()


def describe_loaded_image(slot):
  """Returns (agfi, status, shell) from fpga-describe-local-image."""
  proc = subprocess.run(["fpga-describe-local-image", "-S", str(slot)],
                        check=True, capture_output=True, text=True, timeout=15)
  rows = [line.split() for line in proc.stdout.splitlines()
          if line.startswith("AFI ")]
  if len(rows) != 1 or len(rows[0]) < 4:
    raise ValueError(f"unrecognized image status:\n{proc.stdout}")
  row = rows[0]
  return row[2], row[3], row[-1]


def require_image(slot, agfi=AGFI, shell=SHELL):
  loaded, status, loaded_shell = describe_loaded_image(slot)
  if loaded != agfi or status != "loaded" or loaded_shell.lower() != shell.lower():
    raise RuntimeError(f"expected {agfi} loaded on shell {shell} in slot {slot}; "
                       f"found {loaded} {status} {loaded_shell}")


class ElfImage:
  """Dependency-free ELF32 reader for RV32 images targeting this device."""

  def __init__(self, path):
    with open(path, "rb") as f:
      data = f.read()
    self.data = data
    if len(data) < 52 or data[:7] != b"\x7fELF\x01\x01\x01":
      raise ValueError("expected little-endian ELF32")
    (kind, machine, _, self.entry, phoff, shoff, _, _, phsize, phnum, shsize,
     shnum, _) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    if (kind, machine) != (2, 243):
      raise ValueError("expected an executable RISC-V ELF")
    if self.entry % 4 or region(self.entry, 4) != "tcm" or self.entry >= ITCM[1]:
      raise ValueError(f"entry {self.entry:#x} must be inside ITCM")
    self.segments = []
    for i in range(phnum):
      typ, offset, _, paddr, filesz, memsz, flags, _ = struct.unpack_from(
          "<8I", data, phoff + i * phsize)
      if typ != 1 or not memsz:
        continue
      if region(paddr, memsz) is None or region(paddr) == "csr":
        raise ValueError(f"segment {paddr:#x}+{memsz:#x} is outside TCM/DDR/HBM")
      self.segments.append(dict(address=paddr, size=memsz, file_size=filesz,
                                flags=flags,
                                data=data[offset:offset + filesz] +
                                bytes(memsz - filesz)))
    self.symbols = {}
    sections = [struct.unpack_from("<10I", data, shoff + i * shsize)
                for i in range(shnum)]
    for section in sections:
      _, typ, _, _, off, size, link, _, _, entsize = section
      if typ != 2 or entsize != 16:
        continue
      strtab = sections[link]
      names = data[strtab[4]:strtab[4] + strtab[5]]
      for p in range(off, off + size, 16):
        name, value, sym_size, _, _, shndx = struct.unpack_from("<IIIBBH", data, p)
        if shndx and name:
          self.symbols[names[name:].split(b"\0", 1)[0].decode()] = (value, sym_size)

  def address(self, name):
    if name not in self.symbols:
      raise KeyError(f"missing symbol {name}")
    return self.symbols[name][0]
