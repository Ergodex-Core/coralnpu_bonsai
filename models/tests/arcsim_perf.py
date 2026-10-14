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
"""Per-operator cycle counts on ArcSim, same ops and output as
kernels_perf_cocotb_test.py (`PERF {json}` lines for estimate_e2e.py).

Each launch runs one kernel ELF on an ArcSim simulator binary, fills `kargs`
with --set (operands are DDR addresses; their contents do not change the
kernels' control flow) and reads kargs.cycles (the kernel's own mcycle count)
with --dump.

  python3 -m models.tests.arcsim_perf --sim bazel-bin/tests/arcsim_sim/vme_core_mini_pf_axi_sim \\
      --kernels ~/rachit/fw --model gemma3-1b --latency 24 [--ops matvec,attention] > out.jsonl
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess

from models.host import fused as F
from models.host import weights as W
from models.tests import fused_perf_ops

# KernelArgs field offsets (models/common/kernel_args.h).
FIELDS = dict(m=0, k=4, n=8, flags=12, in0=16, in1=20, in2=24, in3=28, out0=32,
              out1=36, f0=40, f1=44, u0=48, u1=52, u2=56, u3=60)
CYCLES = 68


def ops(model, m=1, pos=127):
  """(op, kernel, rows, args) for one decode layer, as in runtime._layer."""
  c = W.MODELS[model]
  D, H, NH, HD = c["dim"], c["hidden"], c["heads"], c["head_dim"]
  ddr = iter(range(0x80100000, 0xC0000000, 0x01000000))  # 16 MiB per operand
  a = lambda: next(ddr)
  out = []
  for k in (D, NH * HD, H):
    for tiles in (2, 4):
      n = 16 * tiles
      out.append((f"matvec_k{k}_t{tiles}", "matvec_q4", m, dict(
          m=m, k=k, n=n, in0=a(), in1=a(), in2=a(), in3=a(), u2=a(), u3=a(), out0=a(),
          u0=n, u1=0)))
  for op, rows, k, extra in (("rmsnorm_dim", m, D, {}),
                             ("rmsnorm_dim_residual", m, D, dict(in2=a(), flags=1)),
                             ("rmsnorm_q_norm", m * NH, HD, {}),
                             ("rmsnorm_k_norm", m, HD, dict(u0=2 * HD)),
                             ("rmsnorm_final", 1, D, {})):
    out.append((op, "rmsnorm", rows, dict(dict(m=rows, k=k, in0=a(), in1=a(), out0=a(),
                                               u0=k, f0=1e-6), **extra)))
  for op, rows, k in (("quant_dim", m, D), ("quant_attn", m, NH * HD), ("quant_hidden", m, H),
                      ("quant_final", 1, D)):
    out.append((op, "quant_act", rows, dict(m=rows, k=k, in0=a(), out0=a(), out1=a(), in1=a(),
                                            in3=a(), u0=k)))
  out.append(("gelu_mul", "gelu_mul", m, dict(m=m, k=H, in0=a(), out0=a())))
  q, kv, cos, sin, kc, vc = a(), a(), a(), a(), a(), a()
  out.append(("rope_q", "rope", m, dict(m=m, n=NH * HD, in0=q, in1=cos, in2=sin, u0=pos,
                                        u1=NH, u2=HD)))
  out.append(("rope_kv", "rope", m, dict(m=m, n=2 * HD, in0=kv, in1=cos, in2=sin, u0=pos,
                                         u1=1, u2=HD, flags=3, out0=kc, in3=kv + HD * 4,
                                         out1=vc)))
  out.append(("attention", "attention", m, dict(m=m, n=NH * HD, in0=q, in1=kc, in2=vc,
                                                out0=a(), u0=pos, u1=NH, u2=HD, u3=0,
                                                f0=HD ** -0.5)))
  out.append(("topk_32k", "topk", 1, dict(n=32768, in0=a(), out0=a(), u0=8)))
  return out


def run(sim, elf, args, latency, timeout, fused=False):
  sym, fields, cycles = ("dargs", F.OFF, F.OFF["cycles"]) if fused else ("kargs", FIELDS, CYCLES)
  sets = ",".join(f"{sym}+{fields[k]}=" + (f"f32:{v}" if isinstance(v, float) else str(v))
                  for k, v in args.items())
  cmd = [sim, f"--binary={elf}", f"--set={sets}", f"--dump={sym}+{cycles}",
         f"--axi_read_latency={latency}", f"--axi_write_latency={latency}",
         "--verify_load=false", "--cycles=2000000000"]
  p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
  got = re.search(rf"SYMBOL {sym}\+{cycles}=(\d+)", p.stdout + p.stderr)
  if not got:
    raise RuntimeError(f"{os.path.basename(elf)}: no cycle count\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
  return int(got.group(1))


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--sim", required=True)
  ap.add_argument("--kernels", required=True, help="directory with the kernel ELFs")
  ap.add_argument("--model", default="gemma3-1b")
  ap.add_argument("--latency", type=int, default=24)
  ap.add_argument("--ops", default="", help="comma-separated op name prefixes")
  ap.add_argument("--jobs", type=int, default=os.cpu_count())
  ap.add_argument("--timeout", type=int, default=6 * 3600)
  ap.add_argument("--fused", action="store_true",
                  help="time the fused kernel's routines (gemma_decode.elf, dargs.op 1..8)")
  a = ap.parse_args()
  if a.fused:
    ddr = iter(range(0x80100000, 0xC0000000, 0x00400000))
    todo = [(n, "gemma_decode", 1, f) for n, f in fused_perf_ops.launches(a.model, lambda: next(ddr))]
  else:
    todo = ops(a.model)
  todo = [o for o in todo if not a.ops or any(o[0].startswith(p) for p in a.ops.split(","))]
  with concurrent.futures.ThreadPoolExecutor(a.jobs) as ex:
    futs = {ex.submit(run, a.sim, os.path.join(a.kernels, kern + ".elf"), args, a.latency,
                      a.timeout, a.fused): (op, kern, rows) for op, kern, rows, args in todo}
    for f in concurrent.futures.as_completed(futs):
      op, kern, rows = futs[f]
      print("PERF " + json.dumps(dict(op=op, kernel=kern, model=a.model, m=rows,
                                      cycles=f.result(), read_latency=a.latency,
                                      simulator="arcsim")), flush=True)


if __name__ == "__main__":
  main()
