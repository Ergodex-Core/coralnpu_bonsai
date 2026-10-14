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
"""Runs every kernel on the F2 image against the NumPy references.

  python3 -m models.tests.fpga_kernels_test --kernels models/firmware/out [--only topk]

Each case allocates its buffers in HBM (falls back to DDR), launches the
kernel through F2Backend and compares the device output with
models/tests/reference.py.
"""

import argparse
import sys
import time
import traceback

import numpy as np

from models.host import device as dev
from models.host import weights as W
from models.tests import reference as R


class Harness:
  def __init__(self, backend, kernels_dir):
    self.be = backend
    base, size = backend.hbm or backend.ddr
    self.cursor, self.limit = base + 0x10000, base + size
    self.kernels = {k: dev.KernelImage(f"{kernels_dir}/{k}.elf") for k in
                    ("matvec_q4", "quant_act", "rmsnorm", "rope", "attention",
                     "gelu_mul", "embed", "topk")}

  def alloc(self, nbytes):
    addr = (self.cursor + 63) & ~63
    self.cursor = addr + nbytes
    assert self.cursor < self.limit
    return addr

  def put(self, arr):
    addr = self.alloc(arr.nbytes)
    self.be.write_array(addr, arr)
    return addr

  def get(self, addr, dtype, shape):
    return self.be.read_array(addr, dtype, shape)

  def launch(self, name, **args):
    r = self.be.launch(self.kernels[name], timeout=120.0, **args)
    print(f"  {name}: {r.cycles} cycles, {r.seconds * 1e3:.1f} ms")
    return r


def kmajor(xq):
  """[M,K] int8 -> device layout [K][16]."""
  out = np.zeros((xq.shape[1], 16), np.int8)
  out[:, :xq.shape[0]] = xq.T
  return out


def pad16(x):
  out = np.zeros((16,) + x.shape[1:], x.dtype)
  out[:x.shape[0]] = x
  return out


def case_topk(h):
  x = np.random.default_rng(7).standard_normal(320).astype(np.float32)
  out = h.alloc(64)
  h.launch("topk", n=320, in0=h.put(x), out0=out, u0=8)
  ridx, rval = R.topk(x, 8)
  np.testing.assert_array_equal(h.get(out, np.uint32, (8,)), ridx)
  np.testing.assert_array_equal(h.get(out + 32, np.float32, (8,)), rval)


def case_quant_act(h):
  rng = np.random.default_rng(0)
  m, k = 3, 96
  x = rng.standard_normal((m, k)).astype(np.float32)
  xq, sx, xs = h.alloc(16 * k), h.alloc(16 * 3 * 4), h.alloc(16 * 3 * 4)
  xq1d = h.alloc(16 * k)
  h.launch("quant_act", m=m, k=k, in0=h.put(pad16(x)), out0=xq, out1=sx, in1=xs, in3=xq1d, u0=k)
  rq, rs, rsum = R.quant_act(x)
  np.testing.assert_array_equal(h.get(xq, np.int8, (k, 16))[:, :m].T, rq)
  np.testing.assert_array_equal(h.get(xq1d, np.int8, (m, k)), rq)
  np.testing.assert_allclose(h.get(sx, np.float32, (m, 3)), rs, rtol=1e-6)
  np.testing.assert_array_equal(h.get(xs, np.float32, (m, 3)), rsum)


def case_matvec_q4(h):
  rng = np.random.default_rng(1)
  m, k, n = 3, 64, 32
  x = rng.standard_normal((m, k)).astype(np.float32)
  w = rng.standard_normal((n, k)).astype(np.float32) * 0.1
  nib, sc = W.quantize_q4_0(w)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  out = h.alloc(16 * n * 4)
  h.launch("matvec_q4", m=m, k=k, n=n, in0=h.put(kmajor(xq)), in1=h.put(pad16(sx)),
           in2=h.put(pad16(xsum8)), in3=h.put(tiles), u2=h.put(scales), out0=out,
           u0=n, u1=0)
  got = h.get(out, np.float32, (m, n))
  ref = R.matvec_q4(xq, sx, xsum8, nib, sc)
  np.testing.assert_allclose(got, ref, rtol=1e-4, atol=1e-4)
  print(f"  max |err| {np.abs(got - ref).max():.2e}")


def case_rmsnorm(h):
  rng = np.random.default_rng(2)
  m, k = 4, 64
  x = rng.standard_normal((m, k)).astype(np.float32)
  s = rng.standard_normal(k).astype(np.float32) * 0.1
  r = rng.standard_normal((m, k)).astype(np.float32)
  out = h.alloc(16 * k * 4)
  h.launch("rmsnorm", m=m, k=k, in0=h.put(pad16(x)), in1=h.put(s), in2=h.put(pad16(r)),
           out0=out, flags=1, u0=k, f0=1e-6)
  np.testing.assert_allclose(h.get(out, np.float32, (m, k)), R.rmsnorm(x, s, residual=r),
                             rtol=1e-4, atol=1e-5)


def case_rope(h):
  rng = np.random.default_rng(3)
  m, heads, hd, pos0, S = 2, 2, 64, 5, 16
  x = rng.standard_normal((m, heads, hd)).astype(np.float32)
  half = hd // 2
  inv = 10000.0 ** (-np.arange(0, half) * 2 / hd)
  ang = np.arange(S)[:, None] * inv[None, :]
  xin = h.put(pad16(x.reshape(m, heads * hd)))
  kc = h.alloc(S * hd * 4)
  h.launch("rope", m=m, n=heads * hd, in0=xin, in1=h.put(np.cos(ang).astype(np.float32)),
           in2=h.put(np.sin(ang).astype(np.float32)), u0=pos0, u1=heads, u2=hd, flags=1, out0=kc)
  ref = R.rope(x, np.arange(pos0, pos0 + m), 10000.0, hd)
  np.testing.assert_allclose(h.get(xin, np.float32, (m, heads, hd)), ref, rtol=1e-5, atol=1e-5)
  np.testing.assert_allclose(h.get(kc + pos0 * hd * 4, np.float32, (m, hd)), ref[:, 0],
                             rtol=1e-5, atol=1e-5)


def case_attention(h):
  rng = np.random.default_rng(4)
  m, heads, hd, pos0, S = 2, 2, 64, 3, 8
  q = rng.standard_normal((m, heads, hd)).astype(np.float32)
  kc = rng.standard_normal((S, hd)).astype(np.float32)
  vc = rng.standard_normal((S, hd)).astype(np.float32)
  out = h.alloc(16 * heads * hd * 4)
  h.launch("attention", m=m, n=heads * hd, in0=h.put(pad16(q.reshape(m, -1))), in1=h.put(kc),
           in2=h.put(vc), out0=out, u0=pos0, u1=heads, u2=hd, u3=0, f0=hd ** -0.5)
  np.testing.assert_allclose(h.get(out, np.float32, (m, heads, hd)),
                             R.attention(q, kc, vc, pos0, hd ** -0.5), rtol=1e-4, atol=1e-4)


def case_gelu_mul(h):
  rng = np.random.default_rng(5)
  m, k = 2, 48
  gu = rng.standard_normal((m, 2 * k)).astype(np.float32)
  out = h.alloc(16 * k * 4)
  h.launch("gelu_mul", m=m, k=k, in0=h.put(pad16(gu)), out0=out)
  np.testing.assert_allclose(h.get(out, np.float32, (m, k)), R.gelu_tanh(gu[:, :k]) * gu[:, k:],
                             rtol=1e-4, atol=1e-5)


def case_embed(h):
  rng = np.random.default_rng(6)
  V, D = 32, 64
  nib, sc = W.quantize_q4_0(rng.standard_normal((V, D)).astype(np.float32))
  tiles, scales = W.pack_tiles(nib, sc)
  tok = np.array([3, 17, 31], np.uint32)
  out = h.alloc(16 * D * 4)
  h.launch("embed", m=3, k=D, in0=h.put(pad16(tok)), in1=h.put(tiles), in2=h.put(scales),
           out0=out, f0=float(np.sqrt(D)))
  ref = W.unpack_tiles(tiles, scales, V, D)[tok] * np.float32(np.sqrt(D))
  np.testing.assert_allclose(h.get(out, np.float32, (3, D)), ref, rtol=1e-6)


def case_matvec_decode(h):
  rng = np.random.default_rng(11)
  k, n = 1152, 64
  x = rng.standard_normal((1, k)).astype(np.float32)
  w = rng.standard_normal((n, k)).astype(np.float32) * 0.1
  nib, sc = W.quantize_q4_0(w)
  tiles, scales = W.pack_tiles(nib, sc)
  xq, sx, xsum8 = R.quant_act(x)
  out = h.alloc(16 * n * 4)
  h.launch("matvec_q4", m=1, k=k, n=n, in0=h.put(kmajor(xq)), in1=h.put(pad16(sx)),
           in2=h.put(pad16(xsum8)), in3=h.put(tiles), u2=h.put(scales), u3=h.put(pad16(xq)),
           out0=out, u0=n, u1=0)
  got = h.get(out, np.float32, (1, n))
  ref = R.matvec_q4(xq, sx, xsum8, nib, sc)
  np.testing.assert_allclose(got, ref, rtol=1e-4, atol=1e-4)
  print(f"  decode k={k} n={n}: max |err| {np.abs(got - ref).max():.2e}")


def case_topk_big(h):
  x = np.random.default_rng(8).standard_normal(262144).astype(np.float32)
  out = h.alloc(64)
  h.launch("topk", n=262144, in0=h.put(x), out0=out, u0=8)
  ridx, rval = R.topk(x, 8)
  np.testing.assert_array_equal(h.get(out, np.uint32, (8,)), ridx)
  np.testing.assert_array_equal(h.get(out + 32, np.float32, (8,)), rval)


CASES = [("topk", case_topk), ("topk_big", case_topk_big), ("matvec_decode", case_matvec_decode), ("quant_act", case_quant_act), ("rmsnorm", case_rmsnorm),
         ("gelu_mul", case_gelu_mul), ("embed", case_embed), ("rope", case_rope),
         ("attention", case_attention), ("matvec_q4", case_matvec_q4)]


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--kernels", required=True)
  parser.add_argument("--slot", type=int, default=0)
  parser.add_argument("--only", default=None)
  args = parser.parse_args()
  backend = dev.F2Backend(args.slot)
  h = Harness(backend, args.kernels)
  failed = []
  try:
    for name, fn in CASES:
      if args.only and name != args.only:
        continue
      print(f"== {name}")
      t0 = time.time()
      try:
        fn(h)
        print(f"  PASS ({time.time() - t0:.1f}s)")
      except Exception:  # pylint: disable=broad-except
        traceback.print_exc()
        failed.append(name)
        print("  FAIL")
        backend = dev.F2Backend(args.slot, check_image=False)  # fresh attach
        h.be = backend
  finally:
    backend.close()
  print("failed:", failed or "none")
  sys.exit(1 if failed else 0)


if __name__ == "__main__":
  main()
