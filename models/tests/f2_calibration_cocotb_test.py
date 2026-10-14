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
"""Replays the F2 kernel test (fpga_kernels_test.py) in Verilator for memory
latency calibration.

Each case launches the kernel with the same shapes and buffer placement (all
operands in external memory) as the F2 run and prints

  CALIB {"op": ..., "cycles": <sim mcycle>, "f2_cycles": <F2 mcycle>, ...}

Sweep AXI_READ_LATENCY / AXI_WRITE_LATENCY and pick the latency whose cycles
match the F2 column. Run with KERNEL_DIR set to the clang-18 ELFs of the
commit that produced F2_CYCLES (models/firmware/build.sh). Correctness is
covered by kernels_cocotb_test; this module only measures.
"""

import json
import os

import cocotb
import numpy as np

from models.host import weights as W
from models.tests import reference as R
from models.tests.kernels_cocotb_test import kmajor, pad16, setup

# mcycle per kernel on the F2 image (CoralF2Axi @ 25 MHz), from
# report/results/kernels_test.txt at 82f37317.
F2_CYCLES = {
    "topk": 41360,
    "topk_big": 1817142,
    "matvec_decode": 92914,
    "quant_act": 10855,
    "rmsnorm": 8634,
    "gelu_mul": 34068,
    "embed": 7206,
    "rope": 11075,
    "attention": 27634,
    "matvec_q4": 7724,
}


async def calib(h, op, name, **args):
  cycles = int(await h.launch(name, **args))
  print("CALIB " + json.dumps(dict(
      op=op, cycles=cycles, f2_cycles=F2_CYCLES[op], ratio=round(cycles / F2_CYCLES[op], 3),
      read_latency=int(os.environ.get("AXI_READ_LATENCY", "0")),
      write_latency=int(os.environ.get("AXI_WRITE_LATENCY", "0")))), flush=True)


@cocotb.test()
async def test_calib_matvec_decode(dut):
  h = await setup(dut)
  rng = np.random.default_rng(11)
  k, n = 1152, 64
  x = rng.standard_normal((1, k)).astype(np.float32)
  nib, sc = W.quantize_q4_0(rng.standard_normal((n, k)).astype(np.float32) * 0.1)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  await calib(h, "matvec_decode", "matvec_q4", m=1, k=k, n=n, in0=await h.put(kmajor(xq)),
              in1=await h.put(pad16(sx)), in2=await h.put(pad16(xsum8)),
              in3=await h.put(tiles), u2=await h.put(scales), u3=await h.put(pad16(xq)),
              out0=h.alloc(16 * n * 4), u0=n, u1=0)


@cocotb.test()
async def test_calib_matvec_q4(dut):
  h = await setup(dut)
  rng = np.random.default_rng(1)
  m, k, n = 3, 64, 32
  x = rng.standard_normal((m, k)).astype(np.float32)
  nib, sc = W.quantize_q4_0(rng.standard_normal((n, k)).astype(np.float32) * 0.1)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  await calib(h, "matvec_q4", "matvec_q4", m=m, k=k, n=n, in0=await h.put(kmajor(xq)),
              in1=await h.put(pad16(sx)), in2=await h.put(pad16(xsum8)),
              in3=await h.put(tiles), u2=await h.put(scales), out0=h.alloc(16 * n * 4),
              u0=n, u1=0)


@cocotb.test()
async def test_calib_quant_act(dut):
  h = await setup(dut)
  m, k = 3, 96
  x = np.random.default_rng(0).standard_normal((m, k)).astype(np.float32)
  await calib(h, "quant_act", "quant_act", m=m, k=k, in0=await h.put(pad16(x)),
              out0=h.alloc(16 * k), out1=h.alloc(16 * 3 * 4), in1=h.alloc(16 * 3 * 4),
              in3=h.alloc(16 * k), u0=k)


@cocotb.test()
async def test_calib_rmsnorm(dut):
  h = await setup(dut)
  rng = np.random.default_rng(2)
  m, k = 4, 64
  x = rng.standard_normal((m, k)).astype(np.float32)
  s = rng.standard_normal(k).astype(np.float32) * 0.1
  r = rng.standard_normal((m, k)).astype(np.float32)
  await calib(h, "rmsnorm", "rmsnorm", m=m, k=k, in0=await h.put(pad16(x)),
              in1=await h.put(s), in2=await h.put(pad16(r)), out0=h.alloc(16 * k * 4),
              flags=1, u0=k, f0=1e-6)


@cocotb.test()
async def test_calib_rope(dut):
  h = await setup(dut)
  rng = np.random.default_rng(3)
  m, heads, hd, pos0, S = 2, 2, 64, 5, 16
  x = rng.standard_normal((m, heads, hd)).astype(np.float32)
  ang = np.arange(S)[:, None] * (10000.0 ** (-np.arange(0, hd // 2) * 2 / hd))[None, :]
  await calib(h, "rope", "rope", m=m, n=heads * hd,
              in0=await h.put(pad16(x.reshape(m, heads * hd))),
              in1=await h.put(np.cos(ang).astype(np.float32)),
              in2=await h.put(np.sin(ang).astype(np.float32)),
              u0=pos0, u1=heads, u2=hd, flags=1, out0=h.alloc(S * hd * 4))


@cocotb.test()
async def test_calib_attention(dut):
  h = await setup(dut)
  rng = np.random.default_rng(4)
  m, heads, hd, pos0, S = 2, 2, 64, 3, 8
  q = rng.standard_normal((m, heads, hd)).astype(np.float32)
  kc = rng.standard_normal((S, hd)).astype(np.float32)
  vc = rng.standard_normal((S, hd)).astype(np.float32)
  await calib(h, "attention", "attention", m=m, n=heads * hd,
              in0=await h.put(pad16(q.reshape(m, -1))), in1=await h.put(kc),
              in2=await h.put(vc), out0=h.alloc(16 * heads * hd * 4),
              u0=pos0, u1=heads, u2=hd, u3=0, f0=hd ** -0.5)


@cocotb.test()
async def test_calib_gelu_mul(dut):
  h = await setup(dut)
  m, k = 2, 48
  gu = np.random.default_rng(5).standard_normal((m, 2 * k)).astype(np.float32)
  await calib(h, "gelu_mul", "gelu_mul", m=m, k=k, in0=await h.put(pad16(gu)),
              out0=h.alloc(16 * k * 4))


@cocotb.test()
async def test_calib_embed(dut):
  h = await setup(dut)
  V, D = 32, 64
  nib, sc = W.quantize_q4_0(np.random.default_rng(6).standard_normal((V, D)).astype(np.float32))
  tiles, scales = W.pack_tiles(nib, sc)
  await calib(h, "embed", "embed", m=3, k=D,
              in0=await h.put(pad16(np.array([3, 17, 31], np.uint32))),
              in1=await h.put(tiles), in2=await h.put(scales), out0=h.alloc(16 * D * 4),
              f0=float(np.sqrt(D)))


@cocotb.test()
async def test_calib_topk(dut):
  h = await setup(dut)
  x = np.random.default_rng(7).standard_normal(320).astype(np.float32)
  await calib(h, "topk", "topk", n=320, in0=await h.put(x), out0=h.alloc(64), u0=8)


@cocotb.test()
async def test_calib_topk_big(dut):
  h = await setup(dut)
  x = np.random.default_rng(8).standard_normal(262144).astype(np.float32)
  await calib(h, "topk_big", "topk", n=262144, in0=await h.put(x), out0=h.alloc(64), u0=8)
