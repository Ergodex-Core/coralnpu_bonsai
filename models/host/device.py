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
"""Backends that run one kernel ELF at a time on a CoralNPU core.

Every backend offers raw memory access by core address plus `launch`, which
loads a kernel image (ITCM/DTCM segments), writes its argument block, starts
the core at the entry point and waits for the halt.
"""

import asyncio
import fcntl
import inspect
import os
import struct
import time

import numpy as np

from models.host import f2_device

# KernelArgs (models/common/kernel_args.h), little-endian:
#   u32 m, k, n, flags; u32 in0..in3; u32 out0, out1; f32 f0, f1;
#   u32 u0..u3; u32 status, cycles.
ARGS_FIELDS = ("m", "k", "n", "flags", "in0", "in1", "in2", "in3", "out0",
               "out1", "f0", "f1", "u0", "u1", "u2", "u3")
ARGS_FORMAT = "<10Iff4I"
ARGS_SIZE = struct.calcsize(ARGS_FORMAT) + 8
STATUS_OK = 0x600D


def pack_args(**kw):
  unknown = set(kw) - set(ARGS_FIELDS)
  if unknown:
    raise ValueError(f"unknown kernel args {unknown}")
  values = [kw.get(f, 0) for f in ARGS_FIELDS]
  for i, f in enumerate(ARGS_FIELDS):
    if f not in ("f0", "f1"):
      values[i] = int(values[i]) & 0xFFFFFFFF
    else:
      values[i] = float(values[i])
  return struct.pack(ARGS_FORMAT, *values) + b"\0" * 8


class KernelImage:
  """A kernel ELF plus the addresses the host needs."""

  def __init__(self, path):
    self.name = os.path.splitext(os.path.basename(path))[0]
    self.path = path
    self.elf = f2_device.ElfImage(path)
    self.entry = self.elf.entry
    # Per-op kernels expose `kargs`; the fused assembly kernel exposes `dargs`.
    self.kargs = self.elf.symbols.get("kargs", self.elf.symbols.get("dargs", (None, 0)))[0]
    if self.kargs is None:
      raise KeyError("kernel has neither kargs nor dargs")
    self.ret = self.elf.symbols.get("_ret", (None, 0))[0]
    self.segments = self.elf.segments


class LaunchResult:
  def __init__(self, status, cycles, seconds):
    self.status, self.cycles, self.seconds = status, cycles, seconds


class Backend:
  """Base: memory regions and the launch protocol."""

  ddr = (0x80000000, 1 << 30)
  hbm = (0xC0000000, 1 << 30)  # None when the backend has no HBM window

  def write(self, addr: int, data: bytes):
    raise NotImplementedError

  def read(self, addr: int, size: int) -> bytes:
    raise NotImplementedError

  def write_array(self, addr: int, arr: np.ndarray):
    self.write(addr, np.ascontiguousarray(arr).tobytes())

  def read_array(self, addr: int, dtype, shape):
    n = int(np.prod(shape)) * np.dtype(dtype).itemsize
    return np.frombuffer(self.read(addr, n), dtype=dtype).reshape(shape)

  def launch(self, image: KernelImage, timeout: float = 60.0, **args) -> LaunchResult:
    raise NotImplementedError


class F2Backend(Backend):
  """AWS F2 image through fpga/aws/f2/f2_device.py."""

  def __init__(self, slot=0, check_image=True):
    # Advisory lock: one process drives a slot at a time. A second process
    # resetting the core mid-kernel shows up as a halt timeout in the first.
    self._lock = open(f"/tmp/coralnpu-f2-slot{slot}.lock", "w")
    try:
      fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      print(f"waiting for F2 slot {slot} (another process holds the lock)", flush=True)
      fcntl.flock(self._lock, fcntl.LOCK_EX)
    if check_image:
      f2_device.require_image(slot)
    self.dev = f2_device.F2Device(slot)
    self.dev.wait_memory_ready()
    self._loaded = None

  def write(self, addr, data):
    self.dev.write_bytes(addr, data)

  def read(self, addr, size):
    return self.dev.read_bytes(addr, size)

  def launch(self, image, timeout=60.0, **args):
    self.dev.hold_reset()
    if self._loaded != image.name:
      for seg in image.segments:
        self.dev.write_bytes(seg["address"], seg["data"])
      self._loaded = image.name
    self.dev.write_bytes(image.kargs, pack_args(**args))
    if image.ret is not None:
      self.dev.write32(image.ret, f2_device.SENTINEL)
    t0 = time.monotonic()
    self.dev.start(image.entry)
    self.dev.wait_halt(timeout)
    seconds = time.monotonic() - t0
    status = self.dev.read32(image.kargs + ARGS_SIZE - 8)
    cycles = self.dev.read32(image.kargs + ARGS_SIZE - 4)
    ret = self.dev.read32(image.ret) if image.ret is not None else 0
    if status != STATUS_OK or ret != 0:
      raise RuntimeError(f"{image.name}: status={status:#x} _ret={ret:#x} args={args}")
    return LaunchResult(status, cycles, seconds)

  def close(self):
    self.dev.close()
    fcntl.flock(self._lock, fcntl.LOCK_UN)
    self._lock.close()


def _run(coro_or_value):
  if inspect.isawaitable(coro_or_value):
    return asyncio.get_event_loop().run_until_complete(coro_or_value)
  return coro_or_value


class SimBackend(Backend):
  """A coralnpu_test_utils simulator fixture (MPACT or Verilator).

  The fixture must expose `load_elf_and_lookup_symbols`, `write`, `read`,
  `run_to_halt` and accept raw addresses. Simulators without an HBM window
  get `hbm = None`, so the runtime places everything in DDR.
  """

  def __init__(self, fixture, hbm=False, timeout_cycles=50_000_000):
    self.fx = fixture
    self.hbm = Backend.hbm if hbm else None
    self.timeout_cycles = timeout_cycles

  def write(self, addr, data):
    _run(self.fx.write(addr, np.frombuffer(data, dtype=np.uint8)))

  def read(self, addr, size):
    return bytes(np.asarray(_run(self.fx.read(addr, size)), dtype=np.uint8))

  def launch(self, image, timeout=60.0, **args):
    _run(self.fx.load_elf_and_lookup_symbols(image.path, ["kargs", "_ret"]))
    self.write(image.kargs, pack_args(**args))
    t0 = time.monotonic()
    _run(self.fx.run_to_halt(timeout_cycles=self.timeout_cycles))
    seconds = time.monotonic() - t0
    status = int(self.read_array(image.kargs + ARGS_SIZE - 8, np.uint32, (1,))[0])
    cycles = int(self.read_array(image.kargs + ARGS_SIZE - 4, np.uint32, (1,))[0])
    ret = int(_run(self.fx.read_word("_ret")))
    if status != STATUS_OK or ret != 0:
      raise RuntimeError(f"{image.name}: status={status:#x} _ret={ret:#x} args={args}")
    return LaunchResult(status, cycles, seconds)
