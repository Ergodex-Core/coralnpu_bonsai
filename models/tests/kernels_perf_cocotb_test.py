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
"""Per-operator cycle counts at Gemma decode/prefill shapes.

Runs each kernel that runtime.py launches per layer once, at the model's
shapes, on the Verilator model, and prints one `PERF {json}` line per launch
with the kernel's own mcycle count. Every decoder layer launches the same
kernels at the same shapes, so `estimate_e2e.py` rebuilds a token from these
numbers instead of simulating the whole model.

matvec cost is linear in output tiles, so it is measured at 2 and 4 tiles for
each reduction length and extrapolated to the real n (q, kv, o, gate_up,
down and the 262k-row logits).

Environment: PERF_MODEL (weights.MODELS key, default gemma3-1b), PERF_M
(tokens per launch: 1 = decode, 16 = prefill chunk), PERF_POS (KV position),
AXI_READ_LATENCY / AXI_WRITE_LATENCY (external memory latency, cycles).
"""

import json
import os

import cocotb
import numpy as np

from models.host import weights as W
from models.tests.kernels_cocotb_test import setup

MODEL = os.environ.get("PERF_MODEL", "gemma3-1b")
CFG = W.MODELS[MODEL]
M = int(os.environ.get("PERF_M", "1"))
POS = int(os.environ.get("PERF_POS", "127"))
D, H, NH, HD = CFG["dim"], CFG["hidden"], CFG["heads"], CFG["head_dim"]
RNG = np.random.default_rng(0)


def rand_f32(*shape):
  return RNG.standard_normal(shape).astype(np.float32)


async def timed(h, op, name, shape, **args):
  cycles = await h.launch(name, **args)
  print("PERF " + json.dumps(dict(
      op=op, kernel=name, model=MODEL, m=args.get("m", 1), shape=shape,
      cycles=int(cycles), read_latency=int(os.environ.get("AXI_READ_LATENCY", "0")))),
      flush=True)


async def matvec(h, k, tiles, m=M):
  n = 16 * tiles
  xq, xq1d = await h.put(np.zeros((k, 16), np.int8)), await h.put(np.zeros((16, k), np.int8))
  sx, xs = await h.put(np.ones((16, k // 32), np.float32)), await h.put(np.zeros((16, k // 32), np.float32))
  w = await h.put(RNG.integers(0, 256, n * k // 2, dtype=np.uint8))
  ws = await h.put(np.ones(n * k // 32, np.float32))
  out = h.alloc(16 * n * 4)
  await timed(h, f"matvec_k{k}_t{tiles}", "matvec_q4", dict(k=k, n=n), m=m, k=k, n=n,
              in0=xq, in1=sx, in2=xs, in3=w, u2=ws, u3=xq1d, out0=out, u0=n, u1=0)


async def quant(h, op, k, m=M):
  x = await h.put(rand_f32(16, k))
  await timed(h, op, "quant_act", dict(k=k), m=m, k=k, in0=x, out0=h.alloc(16 * k),
              out1=h.alloc(16 * k // 8), in1=h.alloc(16 * k // 8), in3=h.alloc(16 * k), u0=k)


async def rmsnorm(h, op, rows, k, stride=None, residual=False):
  x, s = await h.put(rand_f32(16 * NH, stride or k)), await h.put(rand_f32(k))
  r = await h.put(rand_f32(16, k)) if residual else 0
  await timed(h, op, "rmsnorm", dict(k=k), m=rows, k=k, in0=x, in1=s, out0=h.alloc(16 * NH * k * 4),
              in2=r, flags=1 if residual else 0, u0=stride or k, f0=1e-6)


@cocotb.test()
async def test_perf_matvec_dim(dut):
  """Reduction over the model width: q, kv, gate_up and the logits."""
  h = await setup(dut)
  for tiles in (2, 4):
    await matvec(h, D, tiles)
  if M != 1:  # the logits projection always runs on one token
    for tiles in (2, 4):
      await matvec(h, D, tiles, m=1)


@cocotb.test()
async def test_perf_matvec_attn_out(dut):
  h = await setup(dut)
  for tiles in (2, 4):
    await matvec(h, NH * HD, tiles)


@cocotb.test()
async def test_perf_matvec_down(dut):
  h = await setup(dut)
  for tiles in (2, 4):
    await matvec(h, H, tiles)


@cocotb.test()
async def test_perf_elementwise(dut):
  h = await setup(dut)
  await rmsnorm(h, "rmsnorm_dim", M, D)
  await rmsnorm(h, "rmsnorm_dim_residual", M, D, residual=True)
  await rmsnorm(h, "rmsnorm_q_norm", M * NH, HD)
  await rmsnorm(h, "rmsnorm_k_norm", M, HD, stride=2 * HD)
  await rmsnorm(h, "rmsnorm_final", 1, D)
  await quant(h, "quant_dim", D)
  await quant(h, "quant_attn", NH * HD)
  await quant(h, "quant_hidden", H)
  await quant(h, "quant_final", D, m=1)
  gu = await h.put(rand_f32(16, 2 * H))
  await timed(h, "gelu_mul", "gelu_mul", dict(k=H), m=M, k=H, in0=gu, out0=h.alloc(16 * H * 4))


@cocotb.test()
async def test_perf_attention(dut):
  h = await setup(dut)
  S = POS + M
  half = HD // 2
  cos, sin = await h.put(rand_f32(S, half)), await h.put(rand_f32(S, half))
  q, kv = await h.put(rand_f32(16, NH * HD)), await h.put(rand_f32(16, 2 * HD))
  kc, vc = await h.put(rand_f32(S, HD)), await h.put(rand_f32(S, HD))
  await timed(h, "rope_q", "rope", dict(pos=POS), m=M, n=NH * HD, in0=q, in1=cos, in2=sin,
              u0=POS, u1=NH, u2=HD)
  await timed(h, "rope_kv", "rope", dict(pos=POS), m=M, n=2 * HD, in0=kv, in1=cos, in2=sin,
              u0=POS, u1=1, u2=HD, flags=3, out0=kc, in3=kv + HD * 4, out1=vc)
  await timed(h, "attention", "attention", dict(pos=POS), m=M, n=NH * HD, in0=q, in1=kc, in2=vc,
              out0=h.alloc(16 * NH * HD * 4), u0=POS, u1=NH, u2=HD, u3=0, f0=HD ** -0.5)


@cocotb.test()
async def test_perf_topk(dut):
  """32k logits; the scan is linear, so estimate_e2e scales it to the vocab."""
  h = await setup(dut)
  n = 32768
  x = await h.put(rand_f32(n))
  await timed(h, "topk_32k", "topk", dict(n=n), n=n, in0=x, out0=h.alloc(64), u0=8)
