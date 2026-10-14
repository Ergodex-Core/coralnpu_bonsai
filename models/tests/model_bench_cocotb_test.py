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
"""Correctness and performance bench: the fused decode step over real layers.

Runs gemma_decode.elf's full step (dargs.op = 0, the code F2 runs) on a subset
of a model's real layers with real weights, a truncated vocabulary, and a KV
cache pre-filled for `BENCH_POS` prompt tokens. Then:

  correctness  the residual stream after the last layer and the top-8 logits
               must match models/tests/reference.py on the same layers
               (group scales rounded through f16, as the kernel stores them);
  performance  prints `BENCH {json}` with the kernel's mcycle count and the
               13-stage breakdown (stage_cycles, see gemma_decode.S).

A token is rebuilt from a few of these runs (bench_estimate.py): every layer
of one attention type runs the same code at the same shapes, so
run(0,1) - run(0) is the exact cost of a local layer, adding the first global
layer gives a global one, and two vocab sizes give the logits cost per row.

Environment:
  BENCH_WEIGHTS  directory with weights.npz and config.json (models/host/weights.py)
  BENCH_POS      prompt tokens already in the KV cache (default 16)
  KERNEL_DIR     must hold gemma_decode.elf (models/firmware/build.sh)
  AXI_READ_LATENCY / AXI_WRITE_LATENCY  external memory latency in cycles
"""

import json
import os

import cocotb
import numpy as np

from coralnpu_test_utils.sim_backends.verilator_test_fixture import VerilatorTestFixture
from models.host import device as dev
from models.host import fused as F
from models.tests import reference as R
from models.tests.kernels_cocotb_test import Harness

DDR = 0x80000000
GROUP, TILE = 32, 16


def subset(cfg, arrays, layers, vocab):
  """Config and arrays for model layers `layers` (renumbered 0..N-1) and the
  first `vocab` rows of the tied embedding; scales rounded through f16."""
  D = cfg["dim"]
  out = {}
  for j, i in enumerate(layers):
    for name in arrays.files:
      if name.startswith(f"l{i}."):
        out[f"l{j}." + name.split(".", 1)[1]] = arrays[name]
  out["embed.tiles"] = arrays["embed.tiles"][:vocab // TILE * (D // GROUP) * 256]
  out["embed.scales"] = arrays["embed.scales"][:vocab // TILE]
  out["final_norm"] = arrays["final_norm"]
  for name in out:
    if name.endswith(".scales"):
      out[name] = out[name].astype(np.float16).astype(np.float32)
  pattern = cfg["pattern"]
  sub = dict(cfg, layers=len(layers), vocab=vocab,
             pattern=[pattern[i % len(pattern)] for i in layers])
  return sub, out


async def run_bench(dut, layers, vocab):
  cfg = json.load(open(os.path.join(os.environ["BENCH_WEIGHTS"], "config.json")))
  arrays = np.load(os.path.join(os.environ["BENCH_WEIGHTS"], "weights.npz"))
  pos = int(os.environ.get("BENCH_POS", "16"))
  S = (pos + 1 + 15) // 16 * 16
  sub, a = subset(cfg, arrays, layers, vocab)
  sub["cache_length"] = S
  D, H, nh, hd = sub["dim"], sub["hidden"], sub["heads"], sub["head_dim"]

  # Reference: prefill `pos` tokens, then the measured token.
  tokens = np.random.default_rng(0).integers(2, vocab, pos + 1)
  ref = R.ModelRef(sub, a)
  ref.step(tokens[:pos])
  (ref_idx, ref_val), _ = ref.step(tokens[pos:])

  # Device memory: weights, tables, activations, KV cache pre-filled by the reference.
  fx = await VerilatorTestFixture.Create(dut, highmem=False, ext_mem_base_addr=DDR,
                                         ext_mem_size=256 << 20)
  h = Harness(fx)
  put = lambda arr: h.put(np.ascontiguousarray(arr))
  f16 = lambda name: put(a[name].astype(np.float16))
  half = hd // 2
  tables = {}
  for kind in ("local", "global"):
    inv = sub[kind + "_base"] ** (-np.arange(0, half, dtype=np.float64) * 2 / hd)
    ang = np.arange(S, dtype=np.float64)[:, None] * inv[None, :]
    tables[kind] = (await put(np.cos(ang).astype(np.float32)),
                    await put(np.sin(ang).astype(np.float32)))
  table = []
  for j in range(len(layers)):
    p = f"l{j}."
    rec = []
    for m in ("q", "kv", "o", "gate_up", "down"):
      rec += [await put(a[p + m + ".tiles"]), await f16(p + m + ".scales")]
    for n in ("pre_attn", "post_attn", "pre_ffw", "post_ffw", "q_norm", "k_norm"):
      rec.append(await put(a[p + n]))
    rec += [await put(ref.kc[j]), await put(ref.vc[j]),
            1 if sub["pattern"][j] == "local" else 0, 0, 0, 0]
    table.append(rec)
  buf = lambda n: h.alloc(n * 4)
  x = buf(D)
  fields = dict(
      op=0, layers=len(layers), dim=D, hidden=H, heads=nh, head_dim=hd, vocab=vocab,
      pos=pos, token=int(tokens[pos]), emb_tiles=await put(a["embed.tiles"]),
      emb_scales=await f16("embed.scales"), final_norm=await put(a["final_norm"]),
      x=x, h=buf(D), xn=buf(D), q=buf(nh * hd), kv=buf(2 * hd), attn=buf(nh * hd),
      ao=buf(D), gate_up=buf(2 * H), act=buf(H),
      cos_local=tables["local"][0], sin_local=tables["local"][1],
      cos_global=tables["global"][0], sin_global=tables["global"][1],
      window=sub["window"], eps=1e-6, qscale=hd ** -0.5, embscale=float(np.sqrt(D)))

  # Run the full step.
  path = os.path.join(os.environ["KERNEL_DIR"], "gemma_decode.elf")
  image = dev.KernelImage(path)
  await fx.load_elf_and_lookup_symbols(path, ["dargs", "_ret", "stage_cycles"])
  dargs = image.elf.address("dargs")
  await fx.write(dargs, np.frombuffer(F.pack_dargs(fields, table), np.uint8))
  await fx.run_to_halt(timeout_cycles=2_000_000_000)
  status, cycles = await h.get(dargs + F.OFF["status"], np.uint32, (2,))
  idx = await h.get(dargs + F.OFF["topk_idx"], np.uint32, (8,))
  val = await h.get(dargs + F.OFF["topk_val"], np.float32, (8,))
  stages = await h.get(image.elf.address("stage_cycles"), np.uint32, (13,))
  got_x = await h.get(x, np.float32, (D,))

  x_err = float(np.abs(got_x - ref.last_x[-1]).max() / np.abs(ref.last_x[-1]).max())
  result = dict(model=sub["model"] if "model" in sub else os.path.basename(os.environ["BENCH_WEIGHTS"]),
                layers=list(layers), pattern=sub["pattern"], vocab=vocab, pos=pos,
                read_latency=int(os.environ.get("AXI_READ_LATENCY", "0")),
                status=int(status), cycles=int(cycles), stages=[int(s) for s in stages],
                top1_match=bool(idx[0] == ref_idx[0]),
                top8_overlap=len(set(idx.tolist()) & set(ref_idx.tolist())),
                top8_val_err=float(np.abs(val - ref_val).max()), x_rel_err=x_err)
  print("BENCH " + json.dumps(result), flush=True)
  assert status == F.STATUS_OK, f"status={status:#x}"
  assert result["top1_match"] and result["top8_overlap"] >= 7, result
  assert x_err < 2e-2, result


def first_global(cfg_path):
  pattern = json.load(open(cfg_path))["pattern"]
  return pattern.index("global")


@cocotb.test()
async def test_bench_layer0(dut):
  await run_bench(dut, [0], 256)


@cocotb.test()
async def test_bench_layers01(dut):
  await run_bench(dut, [0, 1], 256)


@cocotb.test()
async def test_bench_layers01_global(dut):
  g = first_global(os.path.join(os.environ["BENCH_WEIGHTS"], "config.json"))
  await run_bench(dut, [0, 1, g], 256)


@cocotb.test()
async def test_bench_layer0_vocab512(dut):
  await run_bench(dut, [0], 512)
