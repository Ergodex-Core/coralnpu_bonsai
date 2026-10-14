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
"""Routine cycle counts of the fused decode kernel on the Verilator model.

Runs gemma_decode.elf once per routine (dargs.op 1..8) at one model's decode
shapes (see fused_perf_ops.py) and prints `PERF {json}` lines; estimate_e2e.py
--fused composes them into a token. External memory is zero-filled: the
routines' control flow depends on shapes only.

Environment: PERF_MODEL, PERF_POS, AXI_READ_LATENCY / AXI_WRITE_LATENCY,
KERNEL_DIR (must hold gemma_decode.elf from models/firmware/build.sh).
"""

import json
import os

import cocotb
import numpy as np

from models.host import device as dev
from models.host import fused as F
from models.tests import fused_perf_ops as P
from models.tests.kernels_cocotb_test import setup

MODEL = os.environ.get("PERF_MODEL", "gemma3-1b")
POS = int(os.environ.get("PERF_POS", "127"))


async def run(h, names):
  path = os.path.join(os.environ["KERNEL_DIR"], "gemma_decode.elf")
  image = dev.KernelImage(path)
  todo = [(n, f) for n, f in P.launches(MODEL, lambda: h.alloc(1 << 20), POS)
          if any(n.startswith(p) for p in names)]
  for name, fields in todo:
    await h.fx.load_elf_and_lookup_symbols(path, ["dargs", "_ret"])
    dargs = image.elf.address("dargs")
    await h.fx.write(dargs, np.frombuffer(F.pack_dargs(fields), np.uint8))
    await h.fx.run_to_halt(timeout_cycles=500_000_000)
    status, cycles = (await h.get(dargs + F.OFF["status"], np.uint32, (2,)))
    assert status == F.STATUS_OK, f"{name}: status={status:#x}"
    print("PERF " + json.dumps(dict(
        op=name, kernel="gemma_decode", model=MODEL, m=1, cycles=int(cycles),
        read_latency=int(os.environ.get("AXI_READ_LATENCY", "0")))), flush=True)


@cocotb.test()
async def test_fused_matvec_dim(dut):
  """q, kv, gate_up and the logits (reduction over the model width)."""
  c = P.W.MODELS[MODEL]
  await run(await setup(dut), [f"f_matvec_k{c['dim']}_", "f_logits"])


@cocotb.test()
async def test_fused_matvec_attn_out(dut):
  c = P.W.MODELS[MODEL]
  await run(await setup(dut), [f"f_matvec_k{c['heads'] * c['head_dim']}_"])


@cocotb.test()
async def test_fused_matvec_down(dut):
  c = P.W.MODELS[MODEL]
  await run(await setup(dut), [f"f_matvec_k{c['hidden']}_"])


@cocotb.test()
async def test_fused_elementwise(dut):
  await run(await setup(dut), ["f_rmsnorm", "f_quant", "f_rope", "f_gelu", "f_embed"])


@cocotb.test()
async def test_fused_attention(dut):
  await run(await setup(dut), ["f_attention"])
