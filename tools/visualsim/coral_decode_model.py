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
"""Calibrated CoralNPU decode-step performance model, for Mirabilis VisualSim.

Mechanism (verified against Verilator, see `validate`):
  - Baseline LSU bridge: every external 16 B row (read or write) waits the
    memory latency L, so routine cycles = A + (R + W) * L, where R and W are the
    rows read and written. A is the latency-free work, fitted per routine from
    baseline runs only.
  - Prefetch bridge (DBus2AxiStream): reads come from stream buffers at a fixed
    cost of `delta` cycles per load instruction; writes and stream starts still
    wait: cycles = A + (R / rows_per_load) * delta + (W + S) * L.
    The reads are also bounded by the stream bandwidth: depth * 16 B in
    flight per (L + lsu_overhead) cycles, and at most 16 B per lsu_row_cycles.
R, W, S (streams) and rows_per_load come from the model shapes and the kernel
structure. `delta` is one constant, fitted on one routine at one latency.

Knobs for architecture exploration (the VisualSim model exposes the same):
  latency, posted_writes, stream_depth, compute_scale (speedup of the A term
  of matvec routines, e.g. fused scale-accumulate in the matrix engine).

  python3 tools/visualsim/coral_decode_model.py validate --evidence <dir>
  python3 tools/visualsim/coral_decode_model.py sweep --evidence <dir>
  python3 tools/visualsim/coral_decode_model.py xml --evidence <dir> --out model.xml
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as E

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from models.host.weights import MODELS  # noqa: E402
from models.tests import fused_perf_ops  # noqa: E402

LSU_ROW_CYCLES = 4.0     # one 16 B row per ~4 cycles through the LSU (membench, prefetch core)
LSU_OVERHEAD = 10.0      # core-side cycles added to memory latency per row (membench, baseline)


def load_perf(path):
  d = {}
  for line in open(path):
    if line.startswith("PERF "):
      j = json.loads(line[5:])
      d[j["op"]] = j["cycles"]
  return d


def structure(model, op):
  """(reads, writes, streams, rows_per_load) for one routine test, in 16 B rows."""
  c = MODELS[model]
  D, H, NH, HD = c["dim"], c["hidden"], c["heads"], c["head_dim"]
  m = re.match(r"f_(matvec_k(\d+)|logits)_t(\d+)", op)
  if m:
    k = int(m.group(2)) if m.group(2) else D
    n = 16 * int(m.group(3))
    return (n * k // 2 + n * k // 16) // 16 + k // 4, 0 if "logits" in op else n // 4, 3, 1
  n = {"f_rmsnorm_dim": D, "f_rmsnorm_dim_residual": D, "f_rmsnorm_hd": HD}.get(op)
  if n:
    return (4 if "residual" in op else 3) * n // 4, n // 4, 3, 8
  n = {"f_quant_dim": D, "f_quant_attn": NH * HD, "f_quant_hidden": H}.get(op)
  if n:
    return n // 4, n // 16 + 2 * (n // 32 * 4) // 16, 4, 8
  if op == "f_rope_hd":
    return HD // 4 + 2 * (HD // 2) // 4, HD // 4, 3, 4
  if op == "f_gelu":
    return 3 * H // 4, H // 4, 3, 8
  return None  # attention, embed: kept at their measured values


def fit(model, base, L0):
  """Latency-free work A per routine from a baseline run at latency L0."""
  return {op: base[op] - (s[0] + s[1]) * L0 for op in base if (s := structure(model, op))}


def predict(model, A, op, latency, core, delta, posted_writes=False, stream_depth=16,
            compute_scale=1.0):
  if core != "base" and op.endswith("_t4"):
    # A token is made of per-tile costs (t4 - t2); bound each tile by the stream
    # bandwidth, not the whole call.
    op2 = op[:-1] + "2"
    kw = dict(posted_writes=posted_writes, stream_depth=stream_depth, compute_scale=compute_scale)
    t2 = predict(model, A, op2, latency, core, delta, **kw)
    raw = lambda o: _pf_cycles(model, A, o, latency, delta, posted_writes, compute_scale)
    dR = structure(model, op)[0] - structure(model, op2)[0]
    return t2 + max(raw(op) - raw(op2), dR * 16.0 / _stream_bw(latency, stream_depth))
  R, W, S, w = structure(model, op)
  a = A[op] / compute_scale if ("matvec" in op or "logits" in op) else A[op]
  if core == "base":
    return a + (R + W) * latency
  t = _pf_cycles(model, A, op, latency, delta, posted_writes, compute_scale)
  return max(t, R * 16.0 / _stream_bw(latency, stream_depth))


def _stream_bw(latency, depth):
  """Bytes per cycle one stream sustains: depth rows in flight per round trip,
  at most one 16 B row per LSU_ROW_CYCLES."""
  return min(16.0 / LSU_ROW_CYCLES, depth * 16.0 / (latency + LSU_OVERHEAD))


def _pf_cycles(model, A, op, latency, delta, posted_writes, compute_scale):
  R, W, S, w = structure(model, op)
  a = A[op] / compute_scale if ("matvec" in op or "logits" in op) else A[op]
  return a + (R / w) * delta + W * (delta if posted_writes else latency) + S * latency


def token(model, cyc):
  return sum(fused_perf_ops.compose(model, cyc).values())


def calibrate_delta(model, base, pf, L0, k=1152):
  """delta from the marginal cost of one matvec tile (t4 - t2) at latency L0:
  the quantity a token's cost is made of, without per-call fixed costs."""
  s2, s4 = structure(model, f"f_matvec_k{k}_t2"), structure(model, f"f_matvec_k{k}_t4")
  dR, dW = s4[0] - s2[0], s4[1] - s2[1]
  db = base[f"f_matvec_k{k}_t4"] - base[f"f_matvec_k{k}_t2"]
  dp = pf[f"f_matvec_k{k}_t4"] - pf[f"f_matvec_k{k}_t2"]
  dA = db - (dR + dW) * L0
  return (dp - dA - dW * L0) / (dR / s2[3])


def cmd_validate(args):
  ev = args.evidence
  b = {L: load_perf(f"{ev}/fused_gemma3-1b_L{L}_base.jsonl") for L in (15, 24, 100)}
  p = {L: load_perf(f"{ev}/fused_gemma3-1b_L{L}_pf.jsonl") for L in (15, 24, 100)}
  delta = calibrate_delta("gemma3-1b", b[15], p[15], 15)
  print(f"delta = {delta:.3f} cycles per buffered load (fitted on one matvec tile, Gemma 3 1B, L=15)")
  print("\nToken, fused decode (Mcycles): model vs Verilator")
  rows = [("gemma3-1b", b, p, 15)]
  fb = {L: load_perf(f"{ev}/fused_functiongemma-270m_L{L}_base.jsonl") for L in (24, 100)}
  fp = {L: load_perf(f"{ev}/fused_functiongemma-270m_L{L}_pf.jsonl") for L in (24, 100)}
  rows.append(("functiongemma-270m", fb, fp, 24))
  for model, base, pf, L0 in rows:
    A = fit(model, base[L0], L0)
    for L in sorted(pf):
      cyc_b, cyc_p = {}, {}
      for op in base[L]:
        if structure(model, op):
          cyc_b[op] = predict(model, A, op, L, "base", delta)
          cyc_p[op] = predict(model, A, op, L, "pf", delta)
        else:   # attention, embed: measured
          cyc_b[op], cyc_p[op] = base[L][op], pf[L][op]
      mb, mp = token(model, base[L]), token(model, pf[L])
      tb, tp = token(model, cyc_b), token(model, cyc_p)
      print(f"  {model:20s} L={L:3d}  base {tb/1e6:7.1f} vs {mb/1e6:7.1f} ({(tb-mb)/mb*100:+.1f}%)"
            f"   pf {tp/1e6:7.1f} vs {mp/1e6:7.1f} ({(tp-mp)/mp*100:+.1f}%)"
            f"   speedup {tb/tp:.2f}x vs {mb/mp:.2f}x")


def cmd_sweep(args):
  ev = args.evidence
  model = args.model
  b15 = load_perf(f"{ev}/fused_gemma3-1b_L15_base.jsonl")
  p15 = load_perf(f"{ev}/fused_gemma3-1b_L15_pf.jsonl")
  delta = calibrate_delta("gemma3-1b", b15, p15, 15)
  meas = {(core, L): load_perf(f"{ev}/fused_{model}_L{L}_{core}.jsonl")
          for core in ("base", "pf") for L in (24, 100)}
  base = meas[("base", 24)]
  A = fit(model, base, 24)

  def measured(op, L, core):   # attention, embed: linear in latency between the measured points
    lo, hi = meas[(core, 24)][op], meas[(core, 100)][op]
    return lo + (hi - lo) * (L - 24) / 76

  def tok(L, core, **kw):
    cyc = {op: (predict(model, A, op, L, core, delta, **kw) if structure(model, op)
                else measured(op, L, core)) for op in base}
    return token(model, cyc)

  print(f"{model}: decode Mcycles per token (s/token at 1 GHz = Mcycles/1000)")
  print(f"{'latency':>8s} {'baseline':>9s} {'prefetch':>9s} {'+posted wr':>10s} {'+2x matvec':>10s} {'+4x matvec':>10s} {'4x, depth 64':>12s}")
  for L in (15, 24, 50, 100, 150, 200, 300):
    vals = [tok(L, "base"), tok(L, "pf"), tok(L, "pf", posted_writes=True),
            tok(L, "pf", posted_writes=True, compute_scale=2),
            tok(L, "pf", posted_writes=True, compute_scale=4),
            tok(L, "pf", posted_writes=True, compute_scale=4, stream_depth=64)]
    print(f"{L:8d} " + " ".join(f"{v/1e6:9.0f}" if i < 2 else f"{v/1e6:10.0f}" for i, v in enumerate(vals[:5]))
          + f" {vals[5]/1e6:12.0f}")


def token_rows(model, A, delta):
  """The token as rows (name, A, R, W, S, rows_per_load, is_matvec, count) such that
  sum(count * cycles(row)) + attention/embed equals token() of the Python twin
  (without the per-tile bandwidth floor detail)."""
  c = MODELS[model]
  L_, D, H, NH, HD, V = c["layers"], c["dim"], c["hidden"], c["heads"], c["head_dim"], c["vocab"]
  quant = {D: "f_quant_dim", NH * HD: "f_quant_attn", H: "f_quant_hidden"}
  sub = lambda a, b, k=1: tuple(x - k * y for x, y in zip(a, b))
  rows = []
  def matvec(prefix, k, tiles, calls):
    s2, s4 = structure(model, prefix + "_t2"), structure(model, prefix + "_t4")
    tile = (s4[0] - s2[0]) / 2, (s4[1] - s2[1]) / 2, 0, 1
    a_tile = (A[prefix + "_t4"] - A[prefix + "_t2"]) / 2
    rows.append((prefix + "_tile", a_tile, *tile, True, tiles))
    q = structure(model, quant[k])
    fixed = (s2[0] - 2 * tile[0] - q[0], s2[1] - 2 * tile[1] - q[1], s2[2] - q[2], 1)
    rows.append((prefix + "_call", A[prefix + "_t2"] - 2 * a_tile - A[quant[k]], *fixed, False, calls))
  for k, n in ((D, NH * HD), (D, 2 * HD), (NH * HD, D), (D, 2 * H), (H, D)):
    matvec(f"f_matvec_k{k}", k, L_ * n / 16, L_)
  matvec("f_logits", D, V / 16, 1)
  for op, n in (("f_rmsnorm_dim", 2 * L_ + 1), ("f_rmsnorm_dim_residual", 2 * L_),
                ("f_rmsnorm_hd", (NH + 1) * L_), ("f_quant_dim", 2 * L_ + 1), ("f_quant_attn", L_),
                ("f_quant_hidden", L_), ("f_rope_hd", (NH + 1) * L_), ("f_gelu", L_)):
    rows.append((op, A[op], *structure(model, op), False, n))
  return rows


def cmd_xml(args):
  """VisualSim model: one SDF iteration per token row; cycles from the same equations."""
  ev, model = args.evidence, args.model
  base = load_perf(f"{ev}/fused_{model}_L24_base.jsonl")
  delta = calibrate_delta("gemma3-1b", load_perf(f"{ev}/fused_gemma3-1b_L15_base.jsonl"),
                          load_perf(f"{ev}/fused_gemma3-1b_L15_pf.jsonl"), 15)
  A = fit(model, base, 24)
  rows = token_rows(model, A, delta)
  c = MODELS[model]
  # attention runs once per layer, embed once per token (both measured, not modelled)
  def other_cycles(core, L):
    m = load_perf(f"{ev}/fused_{model}_L{L}_{core}.jsonl")
    return c["layers"] * m["f_attention"] + m["f_embed"]
  other = {core: {L: other_cycles(core, L) for L in (24, 100)} for core in ("base", "pf")}
  P, S = "VisualSim.data.expr.Parameter", "VisualSim.kernel.util.StringAttribute"
  r = E.Element("entity", name=f"Coral_Decode_{model.replace('-', '_')}",
                **{"class": "VisualSim.actor.TypedCompositeActor"})
  def prop(a, n, v, cl=P):
    return E.SubElement(a, "property", name=n, value=str(v), **{"class": cl})
  d = E.SubElement(r, "property", name="SDF", **{"class": "VisualSim.simulators.sdf.kernel.SDFDirector"})
  prop(d, "iterations", len(rows))
  knobs = dict(Latency=24.0, Core_Prefetch="true", Posted_Writes="false", Stream_Depth=16.0,
               Matvec_Compute_Scale=1.0, Delta=round(delta, 4), LSU_Row_Cycles=LSU_ROW_CYCLES,
               LSU_Overhead=LSU_OVERHEAD, Clock_MHz=25.0)
  for k, v in knobs.items():
    prop(r, k, v)
  # Attention + embed per token, linear in latency between the measured points (per core).
  for core in ("base", "pf"):
    lo, hi = other[core][24], other[core][100]
    prop(r, f"Other_{core}_At24", f"{lo:.1f}")
    prop(r, f"Other_{core}_Slope", f"{(hi - lo) / 76:.3f}")
  col = lambda i, fmt: "{" + ",".join(fmt(row[i]) for row in rows) + "}"
  prop(r, "Row_Names", col(0, lambda v: f'"{v}"'))
  for i, n in ((1, "Row_A"), (2, "Row_R"), (3, "Row_W"), (4, "Row_S"), (5, "Row_RowsPerLoad"), (7, "Row_Count")):
    prop(r, n, col(i, lambda v: f"{float(v):.4f}"))
  prop(r, "Row_IsMatvecTile", col(6, lambda v: "true" if v else "false"))
  src = E.SubElement(r, "entity", name="Row_Index", **{"class": "VisualSim.actor.lib.Ramp"})
  prop(src, "init", 0)
  bw = "min(16.0/LSU_Row_Cycles, Stream_Depth*16.0/(Latency+LSU_Overhead))"
  a = "(Row_IsMatvecTile(i) ? Row_A(i)/Matvec_Compute_Scale : Row_A(i))"
  pf = (f"max({a} + (Row_R(i)/Row_RowsPerLoad(i))*Delta + Row_W(i)*(Posted_Writes ? Delta : Latency)"
        f" + Row_S(i)*Latency, Row_R(i)*16.0/{bw})")
  cyc = f"(Core_Prefetch ? {pf} : {a} + (Row_R(i)+Row_W(i))*Latency)"
  ex = E.SubElement(r, "entity", name="Row_Cycles", **{"class": "VisualSim.actor.lib.Expression"})
  prop(ex, "expression", "{row=Row_Names(i), count=Row_Count(i), cycles=" + cyc +
       ", cycles_per_token=Row_Count(i)*" + cyc + "}", S)
  port = E.SubElement(ex, "port", name="i", **{"class": "VisualSim.actor.TypedIOPort"})
  E.SubElement(port, "property", name="input")
  w = E.SubElement(r, "entity", name="Row_Records", **{"class": "VisualSim.actor.lib.io.ExpressionWriter"})
  prop(w, "fileName", "results/token-rows.txt", "VisualSim.data.expr.FileParameter")
  prop(w, "append", "false")
  prop(w, "confirmOverwrite", "false")
  for i, (s_, t_) in enumerate([("Row_Index.output", "Row_Cycles.i"), ("Row_Cycles.output", "Row_Records.input")]):
    n = f"r{i}"
    E.SubElement(r, "relation", name=n, **{"class": "VisualSim.actor.TypedIORelation"})
    E.SubElement(r, "link", port=s_, relation=n)
    E.SubElement(r, "link", port=t_, relation=n)
  E.indent(r)
  E.ElementTree(r).write(args.out, encoding="utf-8", xml_declaration=True)
  # Same rows evaluated in Python: the number the VisualSim run must reproduce.
  for core in ("base", "pf"):
    tot = 0.0
    for name, a_, R, W, S_, w, mv, n in rows:
      if core == "base":
        t = a_ + (R + W) * 24
      else:
        t = max(a_ + (R / w) * delta + W * 24 + S_ * 24, R * 16.0 / _stream_bw(24, 16))
      tot += n * t
    tot += other[core][24]
    print(f"{model} {core} @L24: {tot/1e6:.1f} Mcycles per token (VisualSim should report the same)")
  print(f"wrote {args.out}: {len(rows)} rows")


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("command", choices=["validate", "sweep", "xml"])
  ap.add_argument("--evidence", required=True, help="directory with fused_*_L*_{base,pf}.jsonl")
  ap.add_argument("--model", default="gemma3-1b")
  ap.add_argument("--out", default="coral_decode.xml")
  args = ap.parse_args()
  {"validate": cmd_validate, "sweep": cmd_sweep, "xml": cmd_xml}[args.command](args)


if __name__ == "__main__":
  main()
