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
"""Routine launches of the fused decode kernel (models/asm/gemma_decode.S) at
one model's decode shapes, and the per-token composition.

gemma_decode.elf runs a single routine when dargs.op is 1..8 (its unit-test
mode) and reports that routine's mcycle count. A full decode step calls the
same routines at fixed shapes for every layer, so a token is rebuilt from the
routine timings (`compose`) instead of simulating all layers. Used by
kernels_fused_perf_cocotb_test.py (Verilator) and arcsim_perf.py --fused.

matvec cost is linear in output tiles: measured at 2 and 4 tiles per
reduction length. The routine test quantizes its input first, so the matvec
fixed cost includes one quant_act(K), which compose() replaces with the
quant_act calls the full step really makes. The logits matvec (out = 0) drains
each tile into the fused top-8.
"""

from models.host import weights as W

OPS = dict(rmsnorm=1, quant=2, matvec=3, rope=4, attention=5, gelu=6, embed=7, topk=8)


def header(model, pos):
  c = W.MODELS[model]
  return dict(layers=1, dim=c["dim"], hidden=c["hidden"], heads=c["heads"],
              head_dim=c["head_dim"], vocab=c["vocab"], pos=pos, token=0,
              window=c["window"], eps=1e-6, qscale=c["head_dim"] ** -0.5,
              embscale=float(c["dim"]) ** 0.5)


def launches(model, alloc, pos=127):
  """[(name, dargs fields)]; `alloc()` returns a fresh external buffer."""
  c = W.MODELS[model]
  D, H, NH, HD = c["dim"], c["hidden"], c["heads"], c["head_dim"]
  a = alloc
  out = []
  op = lambda name, kind, **t: out.append(
      (name, dict(header(model, pos), op=OPS[kind], **{"test_" + k: v for k, v in t.items()})))
  for k in (D, NH * HD, H):
    for tiles in (2, 4):
      op(f"f_matvec_k{k}_t{tiles}", "matvec", a=a(), b=a(), c=a(), n=k, k=16 * tiles, d=a())
  for tiles in (2, 4):  # logits: out = 0 drains into the fused top-8
    op(f"f_logits_t{tiles}", "matvec", a=a(), b=a(), c=0, n=D, k=16 * tiles, d=a())
  op("f_rmsnorm_dim", "rmsnorm", a=a(), b=a(), c=a(), n=D, d=0)
  op("f_rmsnorm_dim_residual", "rmsnorm", a=a(), b=a(), c=a(), n=D, d=a())
  op("f_rmsnorm_hd", "rmsnorm", a=a(), b=a(), c=a(), n=HD, d=0)
  for name, k in (("f_quant_dim", D), ("f_quant_attn", NH * HD), ("f_quant_hidden", H)):
    op(name, "quant", a=a(), n=k, c=a(), d=a(), e=a())
  op("f_rope_hd", "rope", a=a(), b=a(), c=a(), n=HD)
  op("f_attention", "attention", a=a(), b=a(), c=a(), d=a(), n=pos, k=0)
  op("f_gelu", "gelu", a=a(), c=a(), n=H)
  op("f_embed", "embed", a=a(), b=a(), c=a(), n=D, f=float(D) ** 0.5)
  return out


def compose(model, cyc):
  """{stage: cycles} for one decode token from {name: cycles}."""
  c = W.MODELS[model]
  L, D, H, NH, HD, V = c["layers"], c["dim"], c["hidden"], c["heads"], c["head_dim"], c["vocab"]

  def matvec(name, k, n):
    c2, c4 = cyc[f"{name}_t2"], cyc[f"{name}_t4"]
    per_tile = (c4 - c2) / 2
    return c2 - 2 * per_tile - cyc[{D: "f_quant_dim", NH * HD: "f_quant_attn",
                                    H: "f_quant_hidden"}[k]] + per_tile * n / 16

  mv = lambda k, n: matvec(f"f_matvec_k{k}", k, n)
  layer = {
      "matvec": mv(D, NH * HD) + mv(D, 2 * HD) + mv(NH * HD, D) + mv(D, 2 * H) + mv(H, D),
      "norm+quant": (2 * cyc["f_rmsnorm_dim"] + 2 * cyc["f_rmsnorm_dim_residual"] +
                     (NH + 1) * cyc["f_rmsnorm_hd"] + 2 * cyc["f_quant_dim"] +
                     cyc["f_quant_attn"] + cyc["f_quant_hidden"]),
      "rope+attention": (NH + 1) * cyc["f_rope_hd"] + cyc["f_attention"],
      "gelu": cyc["f_gelu"],
  }
  out = {k: L * v for k, v in layer.items()}
  out["logits+topk"] = matvec("f_logits", D, V)
  out["embed+final"] = cyc["f_embed"] + cyc["f_rmsnorm_dim"] + cyc["f_quant_dim"]
  return out
