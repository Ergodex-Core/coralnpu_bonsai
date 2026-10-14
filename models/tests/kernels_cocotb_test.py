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
"""Runs each kernel on the Verilator VmeCoreMiniAxi model (matrix engine,
8K/32K TCM, external memory at 0x80000000) against the NumPy references.

Not yet run: the toolchain and Verilator model build only on Linux x86_64.
"""

import os

import cocotb
import numpy as np
from bazel_tools.tools.python.runfiles import runfiles

from coralnpu_test_utils.sim_backends.verilator_test_fixture import VerilatorTestFixture
from models.host import device as dev
from models.host import weights as W
from models.tests import reference as R

KERNELS = "coralnpu_hw/models/kernels"
DDR = 0x80000000


class Harness:
  """Bump-allocates DDR and launches kernels through the fixture."""

  def __init__(self, fixture):
    self.fx = fixture
    self.cursor = DDR + 0x1000

  def alloc(self, nbytes):
    addr = (self.cursor + 63) & ~63
    self.cursor = addr + nbytes
    return addr

  async def put(self, arr):
    addr = self.alloc(arr.nbytes)
    await self.fx.write(addr, np.frombuffer(np.ascontiguousarray(arr).tobytes(), np.uint8))
    return addr

  async def get(self, addr, dtype, shape):
    n = int(np.prod(shape)) * np.dtype(dtype).itemsize
    raw = await self.fx.read(addr, n)
    return np.frombuffer(bytes(np.asarray(raw, np.uint8)), dtype=dtype).reshape(shape)

  async def launch(self, name, **args):
    # KERNEL_DIR selects the clang-18 ELFs from models/firmware/build.sh (the
    # ones the F2 image runs) instead of the Bazel-built kernels.
    if os.environ.get("KERNEL_DIR"):
      path = os.path.join(os.environ["KERNEL_DIR"], name + ".elf")
    else:
      path = runfiles.Create().Rlocation(os.path.join(KERNELS, name + ".elf"))
    image = dev.KernelImage(path)
    await self.fx.load_elf_and_lookup_symbols(path, ["kargs", "_ret"])
    await self.fx.write(image.kargs, np.frombuffer(dev.pack_args(**args), np.uint8))
    await self.fx.run_to_halt(timeout_cycles=50_000_000)
    status = (await self.get(image.kargs + dev.ARGS_SIZE - 8, np.uint32, (1,)))[0]
    ret = await self.fx.read_word("_ret")
    assert status == dev.STATUS_OK and ret == 0, f"{name}: status={status:#x} ret={ret:#x}"
    return (await self.get(image.kargs + dev.ARGS_SIZE - 4, np.uint32, (1,)))[0]  # mcycle


async def setup(dut):
  fx = await VerilatorTestFixture.Create(dut, highmem=False, ext_mem_base_addr=DDR,
                                         ext_mem_size=64 << 20)
  return Harness(fx)


def kmajor(xq):
  """[M,K] int8 -> device layout [K][16]."""
  out = np.zeros((xq.shape[1], 16), np.int8)
  out[:, :xq.shape[0]] = xq.T
  return out


def pad16(x):
  out = np.zeros((16,) + x.shape[1:], x.dtype)
  out[:x.shape[0]] = x
  return out


@cocotb.test()
async def test_quant_act(dut):
  h = await setup(dut)
  rng = np.random.default_rng(0)
  m, k = 3, 96
  x = rng.standard_normal((m, k)).astype(np.float32)
  xin = await h.put(pad16(x))
  xq, sx, xs = h.alloc(16 * k), h.alloc(16 * k // 32 * 4), h.alloc(16 * k // 32 * 4)
  xq1d = h.alloc(16 * k)
  await h.launch("quant_act", m=m, k=k, in0=xin, out0=xq, out1=sx, in1=xs, in3=xq1d, u0=k)
  rq, rs, rsum = R.quant_act(x)
  np.testing.assert_array_equal((await h.get(xq, np.int8, (k, 16)))[:, :m].T, rq)
  np.testing.assert_allclose(await h.get(sx, np.float32, (m, k // 32)), rs, rtol=1e-6)
  np.testing.assert_array_equal(await h.get(xs, np.float32, (m, k // 32)), rsum)


@cocotb.test()
async def test_matvec_q4(dut):
  h = await setup(dut)
  rng = np.random.default_rng(1)
  m, k, n = 3, 64, 32
  x = rng.standard_normal((m, k)).astype(np.float32)
  w = rng.standard_normal((n, k)).astype(np.float32) * 0.1
  nib, sc = W.quantize_q4_0(w)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  a_xq, a_sx, a_xs = await h.put(kmajor(xq)), await h.put(pad16(sx)), await h.put(pad16(xsum8))
  a_w, a_ws = await h.put(tiles), await h.put(scales)
  out = h.alloc(16 * n * 4)
  await h.launch("matvec_q4", m=m, k=k, n=n, in0=a_xq, in1=a_sx, in2=a_xs, in3=a_w,
                 u2=a_ws, out0=out, u0=n, u1=0)
  got = await h.get(out, np.float32, (m, n))
  np.testing.assert_allclose(got, R.matvec_q4(xq, sx, xsum8, nib, sc), rtol=1e-4, atol=1e-4)


@cocotb.test()
async def test_matvec_q4_decode(dut):
  """m == 1 fast path (row-major activations in u3), as in decode."""
  h = await setup(dut)
  rng = np.random.default_rng(11)
  k, n = 1152, 32
  x = rng.standard_normal((1, k)).astype(np.float32)
  w = rng.standard_normal((n, k)).astype(np.float32) * 0.1
  nib, sc = W.quantize_q4_0(w)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  out = h.alloc(16 * n * 4)
  await h.launch("matvec_q4", m=1, k=k, n=n, in0=await h.put(kmajor(xq)),
                 in1=await h.put(pad16(sx)), in2=await h.put(pad16(xsum8)),
                 in3=await h.put(tiles), u2=await h.put(scales), u3=await h.put(pad16(xq)),
                 out0=out, u0=n, u1=0)
  np.testing.assert_allclose(await h.get(out, np.float32, (1, n)),
                             R.matvec_q4(xq, sx, xsum8, nib, sc), rtol=1e-4, atol=1e-4)


@cocotb.test()
async def test_rmsnorm(dut):
  h = await setup(dut)
  rng = np.random.default_rng(2)
  m, k = 4, 64
  x = rng.standard_normal((m, k)).astype(np.float32)
  s = rng.standard_normal(k).astype(np.float32) * 0.1
  r = rng.standard_normal((m, k)).astype(np.float32)
  xin, sin, rin = await h.put(pad16(x)), await h.put(s), await h.put(pad16(r))
  out = h.alloc(16 * k * 4)
  await h.launch("rmsnorm", m=m, k=k, in0=xin, in1=sin, in2=rin, out0=out, flags=1, u0=k, f0=1e-6)
  np.testing.assert_allclose(await h.get(out, np.float32, (m, k)),
                             R.rmsnorm(x, s, residual=r), rtol=1e-4, atol=1e-5)


@cocotb.test()
async def test_rope(dut):
  h = await setup(dut)
  rng = np.random.default_rng(3)
  m, heads, hd, pos0, S = 2, 2, 64, 5, 16
  x = rng.standard_normal((m, heads, hd)).astype(np.float32)
  half = hd // 2
  inv = 10000.0 ** (-np.arange(0, half) * 2 / hd)
  ang = np.arange(S)[:, None] * inv[None, :]
  cos, sin = await h.put(np.cos(ang).astype(np.float32)), await h.put(np.sin(ang).astype(np.float32))
  xin = await h.put(pad16(x.reshape(m, heads * hd)))
  kc = h.alloc(S * hd * 4)
  await h.launch("rope", m=m, n=heads * hd, in0=xin, in1=cos, in2=sin, u0=pos0, u1=heads,
                 u2=hd, flags=1, out0=kc)
  ref = R.rope(x, np.arange(pos0, pos0 + m), 10000.0, hd)
  np.testing.assert_allclose(await h.get(xin, np.float32, (m, heads, hd)), ref, rtol=1e-5, atol=1e-5)
  np.testing.assert_allclose(await h.get(kc + pos0 * hd * 4, np.float32, (m, hd)), ref[:, 0], rtol=1e-5, atol=1e-5)


@cocotb.test()
async def test_attention(dut):
  h = await setup(dut)
  rng = np.random.default_rng(4)
  m, heads, hd, pos0, S = 2, 2, 64, 3, 8
  q = rng.standard_normal((m, heads, hd)).astype(np.float32)
  kc = rng.standard_normal((S, hd)).astype(np.float32)
  vc = rng.standard_normal((S, hd)).astype(np.float32)
  qin, kin, vin = await h.put(pad16(q.reshape(m, -1))), await h.put(kc), await h.put(vc)
  out = h.alloc(16 * heads * hd * 4)
  await h.launch("attention", m=m, n=heads * hd, in0=qin, in1=kin, in2=vin, out0=out,
                 u0=pos0, u1=heads, u2=hd, u3=0, f0=hd ** -0.5)
  ref = R.attention(q, kc, vc, pos0, hd ** -0.5)
  np.testing.assert_allclose(await h.get(out, np.float32, (m, heads, hd)), ref, rtol=1e-4, atol=1e-4)


@cocotb.test()
async def test_gelu_mul(dut):
  h = await setup(dut)
  rng = np.random.default_rng(5)
  m, k = 2, 48
  gu = rng.standard_normal((m, 2 * k)).astype(np.float32)
  gin = await h.put(pad16(gu))
  out = h.alloc(16 * k * 4)
  await h.launch("gelu_mul", m=m, k=k, in0=gin, out0=out)
  np.testing.assert_allclose(await h.get(out, np.float32, (m, k)),
                             R.gelu_tanh(gu[:, :k]) * gu[:, k:], rtol=1e-4, atol=1e-5)


@cocotb.test()
async def test_embed(dut):
  h = await setup(dut)
  rng = np.random.default_rng(6)
  V, D = 32, 64
  nib, sc = W.quantize_q4_0(rng.standard_normal((V, D)).astype(np.float32))
  tiles, scales = W.pack_tiles(nib, sc)
  tok = np.array([3, 17, 31], np.uint32)
  tin, win, sin = await h.put(pad16(tok)), await h.put(tiles), await h.put(scales)
  out = h.alloc(16 * D * 4)
  await h.launch("embed", m=3, k=D, in0=tin, in1=win, in2=sin, out0=out, f0=float(np.sqrt(D)))
  ref = W.unpack_tiles(tiles, scales, V, D)[tok] * np.float32(np.sqrt(D))
  np.testing.assert_allclose(await h.get(out, np.float32, (3, D)), ref, rtol=1e-6)


@cocotb.test()
async def test_topk(dut):
  h = await setup(dut)
  rng = np.random.default_rng(7)
  x = rng.standard_normal(320).astype(np.float32)
  xin = await h.put(x)
  out = h.alloc(64)
  await h.launch("topk", n=320, in0=xin, out0=out, u0=8)
  idx = await h.get(out, np.uint32, (8,))
  val = await h.get(out + 32, np.float32, (8,))
  ridx, rval = R.topk(x, 8)
  np.testing.assert_array_equal(idx, ridx)
  np.testing.assert_array_equal(val, rval)
