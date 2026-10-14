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
"""Estimates Gemma decode time per token from per-operator cycle counts.

Input: `PERF {json}` lines from kernels_perf_cocotb_test (one file per model,
core and memory latency). Every decoder layer launches the same kernels at the
same shapes as runtime.Runtime._layer, so

  token = layers * layer + final norm + quant + logits matvec + top-k.

matvec cycles are linear in output tiles: fixed + per_tile * n / 16, from the
2- and 4-tile runs. top-k is measured on 32k logits and scaled to the vocab.
Device cycles only: host launch overhead over PCIe and `embed` (one row) are
not included.

  python3 -m models.tests.estimate_e2e perf/gemma3-1b_m1_L24_base.jsonl \
      perf/gemma3-1b_m1_L24_pf.jsonl+perf/gemma3-1b_m1_L24_pf_opt.jsonl
"""

import argparse
import json
import os

from models.host import weights as W
from models.tests import fused_perf_ops


def load(paths):
  """`a.jsonl+b.jsonl`: later files override ops of earlier ones."""
  ops = {}
  for path in paths.split("+"):
    for line in open(path):
      if line.startswith("PERF "):
        d = json.loads(line[5:])
        ops[(d["op"], d["m"])] = d["cycles"]
        model = d["model"]
  return model, ops


def matvec(ops, k, n, m):
  c2, c4 = ops[(f"matvec_k{k}_t2", m)], ops[(f"matvec_k{k}_t4", m)]
  per_tile = (c4 - c2) / 2
  return c2 - 2 * per_tile + per_tile * n / 16


def token(model, ops, m):
  """Returns {stage: cycles} for one step of m tokens."""
  c = W.MODELS[model]
  L, D, H, NH, HD, V = c["layers"], c["dim"], c["hidden"], c["heads"], c["head_dim"], c["vocab"]
  g = lambda op, mm=m: ops[(op, mm)]
  layer = {
      "matvec": (matvec(ops, D, NH * HD, m) + matvec(ops, D, 2 * HD, m) +
                 matvec(ops, NH * HD, D, m) + matvec(ops, D, 2 * H, m) +
                 matvec(ops, H, D, m)),
      "norm+quant": (2 * g("rmsnorm_dim") + 2 * g("rmsnorm_dim_residual") +
                     g("rmsnorm_q_norm", m * NH) + g("rmsnorm_k_norm") +
                     2 * g("quant_dim") + g("quant_attn") + g("quant_hidden")),
      "rope+attention": g("rope_q") + g("rope_kv") + g("attention"),
      "gelu": g("gelu_mul"),
  }
  out = {k: L * v for k, v in layer.items()}
  out["logits"] = matvec(ops, D, V, 1)
  out["final+topk"] = g("rmsnorm_final", 1) + g("quant_final", 1) + g("topk_32k", 1) * V / 32768
  return out


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("files", nargs="+")
  ap.add_argument("--mhz", default="25,1000")
  ap.add_argument("--fused", action="store_true",
                  help="inputs are fused-kernel routine timings (fused_perf_ops)")
  args = ap.parse_args()
  mhz = [float(x) for x in args.mhz.split(",")]
  rows = []
  for path in args.files:
    model, ops = load(path)
    if args.fused:
      m, stages = 1, fused_perf_ops.compose(model, {op: c for (op, _), c in ops.items()})
    else:
      m = max(mm for (op, mm) in ops if op.startswith("matvec"))
      stages = token(model, ops, m)
    total = sum(stages.values())
    name = "+".join(os.path.basename(p).replace(".jsonl", "") for p in path.split("+"))
    rows.append((name, m, stages, total))
  head = "| run | m | " + " | ".join(rows[0][2]) + " | total Mcycles | " + \
      " | ".join(f"s/step @{f:g} MHz" for f in mhz) + " |"
  print(head)
  print("|" + "---|" * (head.count("|") - 1))
  for name, m, stages, total in rows:
    cells = [f"{v / 1e6:.1f}" for v in stages.values()]
    times = [f"{total / (f * 1e6):.3g}" for f in mhz]
    print(f"| {name} | {m} | " + " | ".join(cells) + f" | {total / 1e6:.1f} | " +
          " | ".join(times) + " |")


if __name__ == "__main__":
  main()
