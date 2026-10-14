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
"""Tests each routine of the fused assembly kernel on the FPGA against the
NumPy references, then a full decode step against the per-kernel runtime.

  python3 -m models.tests.fpga_fused_test --elf models/firmware/out/gemma_decode.elf \
      [--weights weights/functiongemma --kernels models/firmware/out]
"""

import argparse
import sys
import traceback

import numpy as np

from models.host import device as dev
from models.host import fused as F
from models.host import weights as W
from models.tests import reference as R


class Harness:
  def __init__(self, backend, elf):
    self.be = backend
    self.image = dev.KernelImage(elf)
    self.dargs = self.image.elf.address("dargs")
    base, size = backend.hbm or backend.ddr
    self.cursor, self.limit = base + 0x10000, base + size

  def alloc(self, n):
    a = (self.cursor + 63) & ~63
    self.cursor = a + n
    return a

  def put(self, arr):
    a = self.alloc(arr.nbytes)
    self.be.write_array(a, arr)
    return a

  def get(self, addr, dtype, shape):
    return self.be.read_array(addr, dtype, shape)

  def run(self, op, **fields):
    blob = F.pack_dargs(dict(fields, op=op))
    d = self.be.dev
    d.hold_reset()
    for seg in self.image.segments:
      d.write_bytes(seg["address"], seg["data"])
    self.be._loaded = None
    d.write_bytes(self.dargs, blob)
    d.write32(self.image.ret, 0x0BADD00D)
    d.start(self.image.entry)
    d.wait_halt(120.0)
    status = d.read32(self.dargs + F.OFF["status"])
    cycles = d.read32(self.dargs + F.OFF["cycles"])
    ret = d.read32(self.image.ret)
    assert status == F.STATUS_OK and ret == 0, f"op {op}: status={status:#x} ret={ret:#x}"
    print(f"  op {op}: {cycles} cycles")
    return cycles


def t_rmsnorm(h):
  rng = np.random.default_rng(2)
  k = 1152
  x = rng.standard_normal(k).astype(np.float32)
  s = rng.standard_normal(k).astype(np.float32) * 0.1
  r = rng.standard_normal(k).astype(np.float32)
  out = h.alloc(k * 4)
  h.run(1, test_a=h.put(x), test_b=h.put(s), test_c=out, test_d=h.put(r), test_n=k, eps=1e-6)
  np.testing.assert_allclose(h.get(out, np.float32, (k,)), R.rmsnorm(x[None], s, residual=r[None])[0],
                             rtol=1e-4, atol=1e-5)


def t_quant_matvec(h):
  rng = np.random.default_rng(1)
  k, n = 1152, 64
  x = rng.standard_normal((1, k)).astype(np.float32)
  w = rng.standard_normal((n, k)).astype(np.float32) * 0.1
  nib, sc = W.quantize_q4_0(w)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  xq_out, sx_out, xs_out = h.alloc(k), h.alloc(k // 32 * 4), h.alloc(k // 32 * 4)
  h.run(2, test_a=h.put(x), test_c=xq_out, test_d=sx_out, test_e=xs_out, test_n=k)
  np.testing.assert_array_equal(h.get(xq_out, np.int8, (k,)), xq[0])
  np.testing.assert_allclose(h.get(sx_out, np.float32, (k // 32,)), sx[0], rtol=1e-6)
  np.testing.assert_array_equal(h.get(xs_out, np.float32, (k // 32,)), xsum8[0])
  out = h.alloc(n * 4)
  cyc = h.run(3, test_a=h.put(tiles), test_b=h.put(F.scales_to_f16(scales)), test_c=out,
              test_d=h.put(x), test_n=k, test_k=n)
  got = h.get(out, np.float32, (n,))
  sc16 = scales.astype(np.float16).astype(np.float32)
  ref = R.matvec_q4(xq, sx, xsum8, nib, sc16.transpose(0, 2, 1).reshape(n, k // 32))[0]
  np.testing.assert_allclose(got, ref, rtol=2e-4, atol=1e-4)
  print(f"  matvec k={k} n={n}: {cyc / (n // 16 * (k // 32)):.0f} cycles per group-tile, "
        f"max|err| {np.abs(got - ref).max():.2e}")


def t_rope(h):
  rng = np.random.default_rng(3)
  hd, pos = 256, 5
  x = rng.standard_normal(hd).astype(np.float32)
  half = hd // 2
  inv = 10000.0 ** (-np.arange(0, half) * 2 / hd)
  ang = pos * inv
  xin = h.put(x)
  h.run(4, test_a=xin, test_b=h.put(np.cos(ang).astype(np.float32)),
        test_c=h.put(np.sin(ang).astype(np.float32)), test_n=hd)
  ref = R.rope(x[None, None], np.array([pos]), 10000.0, hd)[0, 0]
  np.testing.assert_allclose(h.get(xin, np.float32, (hd,)), ref, rtol=1e-5, atol=1e-5)


def t_attention(h):
  rng = np.random.default_rng(4)
  heads, hd, pos, S = 4, 256, 6, 16
  q = rng.standard_normal((heads, hd)).astype(np.float32)
  kc = rng.standard_normal((S, hd)).astype(np.float32)
  vc = rng.standard_normal((S, hd)).astype(np.float32)
  out = h.alloc(heads * hd * 4)
  h.run(5, test_a=h.put(q), test_b=h.put(kc), test_c=h.put(vc), test_d=out, test_n=pos,
        test_k=0, heads=heads, head_dim=hd, qscale=hd ** -0.5)
  ref = R.attention(q[None], kc, vc, pos, hd ** -0.5)[0]
  np.testing.assert_allclose(h.get(out, np.float32, (heads, hd)), ref, rtol=1e-4, atol=1e-4)


def t_gelu(h):
  rng = np.random.default_rng(5)
  H = 2048
  gu = rng.standard_normal(2 * H).astype(np.float32)
  out = h.alloc(H * 4)
  h.run(6, test_a=h.put(gu), test_c=out, test_n=H)
  np.testing.assert_allclose(h.get(out, np.float32, (H,)), R.gelu_tanh(gu[:H]) * gu[H:],
                             rtol=1e-4, atol=1e-5)


def t_embed(h):
  rng = np.random.default_rng(6)
  V, D = 64, 640
  nib, sc = W.quantize_q4_0(rng.standard_normal((V, D)).astype(np.float32))
  tiles, scales = W.pack_tiles(nib, sc)
  out = h.alloc(D * 4)
  tok = 37
  h.run(7, token=tok, test_a=h.put(tiles), test_b=h.put(F.scales_to_f16(scales)), test_c=out,
        test_n=D, test_f=float(np.sqrt(D)))
  ref = W.unpack_tiles(tiles, scales.astype(np.float16).astype(np.float32), V, D)[tok] * np.float32(np.sqrt(D))
  np.testing.assert_allclose(h.get(out, np.float32, (D,)), ref, rtol=1e-6)


def t_topk(h):
  x = np.random.default_rng(8).standard_normal(4096).astype(np.float32)
  h.run(8, test_a=h.put(x), test_n=4096)
  idx = h.get(h.dargs + F.OFF["topk_idx"], np.uint32, (8,))
  val = h.get(h.dargs + F.OFF["topk_val"], np.float32, (8,))
  ridx, rval = R.topk(x, 8)
  np.testing.assert_array_equal(idx, ridx)
  np.testing.assert_array_equal(val, rval)


def t_full_step(h, args):
  """Fused step vs the per-kernel runtime on a short prompt."""
  from models.host import runtime  # pylint: disable=g-import-not-at-top
  model = runtime.GemmaNative(h.be, args.weights, args.kernels, cache_length=128)
  tokens = [2, 1000, 2000]
  idx_ref, _ = model.step(tokens)             # prefill with per-op kernels
  pos_after_prefill = model.pos
  ref_idx, ref_val = model.step([idx_ref[0]])  # per-op decode of one token
  # Same again with the fused kernel: re-run prefill, then fused decode.
  model.reset()
  model.step(tokens)
  assert model.pos == pos_after_prefill
  fd = F.FusedDecoder(model, h.image.path)
  idx, val = fd.step(idx_ref[0])
  print(f"  per-op  top-4 {ref_idx[:4]} {[round(v, 3) for v in ref_val[:4]]}")
  print(f"  fused   top-4 {idx[:4]} {[round(v, 3) for v in val[:4]]}  "
        f"{model.profile['gemma_decode'][1]} cycles, {model.profile['gemma_decode'][2]:.1f} s")
  assert idx[0] == ref_idx[0], "fused top-1 differs from per-kernel runtime"


CASES = [("rmsnorm", t_rmsnorm), ("quant+matvec", t_quant_matvec), ("rope", t_rope),
         ("gelu", t_gelu), ("embed", t_embed), ("topk", t_topk), ("attention", t_attention)]


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--elf", required=True)
  parser.add_argument("--weights", default=None)
  parser.add_argument("--kernels", default=None)
  parser.add_argument("--only", default=None)
  parser.add_argument("--slot", type=int, default=0)
  args = parser.parse_args()
  be = dev.F2Backend(args.slot)
  h = Harness(be, args.elf)
  failed = []
  for name, fn in CASES:
    if args.only and name != args.only:
      continue
    print(f"== {name}")
    try:
      fn(h)
      print("  PASS")
    except Exception:  # pylint: disable=broad-except
      traceback.print_exc()
      failed.append(name)
      print("  FAIL")
      be = dev.F2Backend(args.slot, check_image=False)
      h.be = be
  if args.weights and (not args.only or args.only == "full"):
    print("== full step")
    try:
      t_full_step(h, args)
      print("  PASS")
    except Exception:  # pylint: disable=broad-except
      traceback.print_exc()
      failed.append("full")
  be.close()
  print("failed:", failed or "none")
  sys.exit(1 if failed else 0)


if __name__ == "__main__":
  main()
