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
"""Runs one decode step on the device and on the NumPy reference in lockstep
and reports the error at every stage: python3 -m models.tests.trace_compare
--weights DIR --kernels DIR [--tokens 2,1000,2000] [--layers 2]"""

import argparse
import json
import os

import numpy as np

from models.host import device as dev
from models.host import runtime
from models.host import weights as W
from models.tests import reference as R


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--weights", required=True)
  parser.add_argument("--kernels", required=True)
  parser.add_argument("--tokens", default="2,1000,2000")
  parser.add_argument("--layers", type=int, default=2, help="layers to trace")
  args = parser.parse_args()
  tokens = [int(t) for t in args.tokens.split(",")]
  m = len(tokens)
  with open(os.path.join(args.weights, "config.json")) as f:
    cfg = json.load(f)
  a = dict(np.load(os.path.join(args.weights, "weights.npz")))
  D, H, nh, hd, V = cfg["dim"], cfg["hidden"], cfg["heads"], cfg["head_dim"], cfg["vocab"]
  ref = R.ModelRef(dict(cfg, cache_length=128), a)

  # Reference intermediates for one step at position 0.
  emb = W.unpack_tiles(a["embed.tiles"], a["embed.scales"], V, D)
  x = emb[np.asarray(tokens)] * np.float32(np.sqrt(D))
  exp = {("embed", None): x}
  for i in range(cfg["layers"]):
    local = cfg["pattern"][i % len(cfg["pattern"])] == "local"
    base = cfg["local_base"] if local else cfg["global_base"]
    xn = R.rmsnorm(x, a[f"l{i}.pre_attn"]); exp[("xn_attn", i)] = xn
    q = ref.linear(f"l{i}.q", xn); kv = ref.linear(f"l{i}.kv", xn)
    exp[("qkv", i)] = (q, kv)
    q = R.rmsnorm(q.reshape(m, nh, hd), a[f"l{i}.q_norm"])
    k = R.rmsnorm(kv[:, :hd], a[f"l{i}.k_norm"]); v = kv[:, hd:]
    exp[("qk_norm", i)] = (q.reshape(m, -1), np.concatenate([k, v], 1))
    pos = np.arange(m)
    q = R.rope(q, pos, base, hd); k = R.rope(k[:, None, :], pos, base, hd)[:, 0]
    exp[("rope", i)] = (q.reshape(m, -1), k)
    ref.kc[i, :m] = k; ref.vc[i, :m] = v
    att = R.attention(q, ref.kc[i], ref.vc[i], 0, hd ** -0.5, cfg["window"] if local else 0)
    exp[("attn", i)] = att.reshape(m, -1)
    ao = ref.linear(f"l{i}.o", att.reshape(m, -1)); exp[("o_proj", i)] = ao
    h = R.rmsnorm(ao, a[f"l{i}.post_attn"], residual=x); exp[("h", i)] = h
    xn = R.rmsnorm(h, a[f"l{i}.pre_ffw"])
    gu = ref.linear(f"l{i}.gate_up", xn); exp[("gate_up", i)] = gu
    act = R.gelu_tanh(gu[:, :H]) * gu[:, H:]
    down = ref.linear(f"l{i}.down", act); exp[("down", i)] = down
    x = R.rmsnorm(down, a[f"l{i}.post_ffw"], residual=h); exp[("x", i)] = x
    if i + 1 >= args.layers:
      break
  logits_ref = None
  if args.layers >= cfg["layers"]:
    logits_ref = ref.linear("embed", R.rmsnorm(x[-1:], a["final_norm"]))[0]

  be = dev.F2Backend(0)
  model = runtime.GemmaNative(be, args.weights, args.kernels, cache_length=128)
  b = model.b

  def err(name, got, want):
    got, want = np.asarray(got, np.float32), np.asarray(want, np.float32)
    ae = np.abs(got - want).max()
    rel = ae / (np.abs(want).max() + 1e-9)
    flag = "  <-- MISMATCH" if rel > 2e-2 else ""
    print(f"{name:14s} max|err| {ae:9.3e}  rel {rel:8.2e}  |ref| {np.abs(want).max():8.3f}{flag}")

  def trace(stage, layer):
    key = (stage, layer)
    if key not in exp:
      if stage == "logits" and logits_ref is not None:
        err("logits", be.read_array(b["logits"], np.float32, (V,)), logits_ref)
      return
    want = exp[key]
    tag = f"L{layer} {stage}" if layer is not None else stage
    if stage in ("qkv", "qk_norm"):
      err(tag + " q", be.read_array(b["q"], np.float32, (16, nh * hd))[:m], want[0])
      err(tag + " kv", be.read_array(b["kv"], np.float32, (16, 2 * hd))[:m], want[1])
    elif stage == "rope":
      err(tag + " q", be.read_array(b["q"], np.float32, (16, nh * hd))[:m], want[0])
      kc = b["kcache"] + layer * model.S * hd * 4
      err(tag + " kcache", be.read_array(kc, np.float32, (m, hd)), want[1])
    else:
      buf = {"embed": "x", "xn_attn": "xn", "attn": "attn", "o_proj": "ao", "h": "h",
             "gate_up": "gate_up", "down": "ao", "x": "x"}[stage]
      width = {"gate_up": 2 * H, "attn": nh * hd}.get(stage, D)
      err(tag, be.read_array(b[buf], np.float32, (16, width))[:m], want)

  model.trace = trace
  idx, val = model.step(tokens)
  print("device top-k:", idx[:4], [round(v, 3) for v in val[:4]])
  if logits_ref is not None:
    ridx, rval = R.topk(logits_ref, 8)
    print("ref    top-k:", [int(i) for i in ridx[:4]], [round(float(v), 3) for v in rval[:4]])
  be.close()


if __name__ == "__main__":
  main()
