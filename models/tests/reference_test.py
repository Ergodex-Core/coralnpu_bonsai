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
"""Host-side checks: tile packing round trip, Q4_0 quantizer, the quantized
matvec against an f32 matmul, and a tiny end-to-end reference step."""

import unittest

import numpy as np

from models.host import device as dev
from models.host import weights as W
from models.tests import reference as R


class PackingTest(unittest.TestCase):

  def test_tiles_round_trip(self):
    rng = np.random.default_rng(0)
    n, k = 48, 96
    nib = rng.integers(0, 16, size=(n, k), dtype=np.uint8)
    sc = rng.standard_normal((n, k // 32)).astype(np.float32)
    tiles, scales = W.pack_tiles(nib, sc)
    self.assertEqual(tiles.shape, (n * k // 2,))
    self.assertEqual(scales.shape, (n // 16, k // 32, 16))
    w = W.unpack_tiles(tiles, scales, n, k)
    expect = (nib.astype(np.int32) - 8) * np.repeat(sc, 32, axis=1)
    np.testing.assert_allclose(w, expect, rtol=1e-6)
    # Byte layout matches kernel_args.h for an arbitrary element.
    t, lane, g, k4, j = 2, 5, 1, 3, 2
    off = ((t * (k // 32) + g) * 8 + k4) * 32 + (16 if j >= 2 else 0) + lane
    byte = int(tiles[off])
    want = int(nib[t * 16 + lane, g * 32 + k4 * 4 + j])
    self.assertEqual((byte >> 4) if j % 2 else (byte & 15), want)

  def test_q4_0_matches_gguf(self):
    try:
      from gguf.quants import quantize, dequantize, GGMLQuantizationType as QT  # pylint: disable=g-import-not-at-top
    except ImportError:
      self.skipTest("gguf not installed")
    rng = np.random.default_rng(1)
    w = rng.standard_normal((32, 64)).astype(np.float32)
    nib, sc = W.quantize_q4_0(w)
    mine = (nib.astype(np.int32) - 8) * np.repeat(sc, 32, axis=1)
    ref = dequantize(quantize(w, QT.Q4_0), QT.Q4_0)
    np.testing.assert_allclose(mine, ref, atol=1e-6)
    nib2, sc2 = W.decode_q4_0_blocks(quantize(w, QT.Q4_0), w.shape)
    np.testing.assert_array_equal(nib, nib2)
    np.testing.assert_allclose(sc, sc2)


class KernelReferenceTest(unittest.TestCase):

  def test_matvec_matches_f32(self):
    rng = np.random.default_rng(2)
    m, k, n = 3, 128, 32
    x = rng.standard_normal((m, k)).astype(np.float32)
    w = rng.standard_normal((n, k)).astype(np.float32) * 0.1
    nib, sc = W.quantize_q4_0(w)
    wdeq = (nib.astype(np.int32) - 8) * np.repeat(sc, 32, axis=1)
    xq, sx, xsum8 = R.quant_act(x)
    out = R.matvec_q4(xq, sx, xsum8, nib, sc)
    ref = x @ wdeq.T
    err = np.abs(out - ref).max() / (np.abs(ref).max() + 1e-6)
    self.assertLess(err, 2e-2, f"int8 activation error {err}")

  def test_args_packing(self):
    blob = dev.pack_args(m=1, k=32, n=16, in0=0x80000000, f0=0.5, u3=7)
    self.assertEqual(len(blob), dev.ARGS_SIZE)
    vals = np.frombuffer(blob[:40], dtype=np.uint32)
    self.assertEqual((vals[0], vals[1], vals[2], vals[4]), (1, 32, 16, 0x80000000))
    self.assertEqual(np.frombuffer(blob[40:44], dtype=np.float32)[0], 0.5)
    self.assertEqual(np.frombuffer(blob[60:64], dtype=np.uint32)[0], 7)

  def test_model_step_runs(self):
    rng = np.random.default_rng(3)
    cfg = dict(layers=2, dim=64, hidden=96, heads=2, kv_heads=1, head_dim=32,
               vocab=64, window=512, pattern=("local",) * 5 + ("global",),
               local_base=10_000.0, global_base=1_000_000.0, cache_length=16)
    D, H, nh, hd, V = 64, 96, 2, 32, 64
    a = {}

    def lin(name, n, k):
      nib, sc = W.quantize_q4_0(rng.standard_normal((n, k)).astype(np.float32) * 0.2)
      a[name + ".tiles"], a[name + ".scales"] = W.pack_tiles(nib, sc)

    lin("embed", V, D)
    a["final_norm"] = rng.standard_normal(D).astype(np.float32) * 0.1
    for i in range(2):
      lin(f"l{i}.q", nh * hd, D)
      lin(f"l{i}.kv", 2 * hd, D)
      lin(f"l{i}.o", D, nh * hd)
      lin(f"l{i}.gate_up", 2 * H, D)
      lin(f"l{i}.down", D, H)
      for n_ in ("pre_attn", "post_attn", "pre_ffw", "post_ffw"):
        a[f"l{i}.{n_}"] = rng.standard_normal(D).astype(np.float32) * 0.1
      for n_ in ("q_norm", "k_norm"):
        a[f"l{i}.{n_}"] = rng.standard_normal(hd).astype(np.float32) * 0.1
    ref = R.ModelRef(cfg, a)
    (idx, val), logits = ref.step([3, 17, 42])
    self.assertEqual(logits.shape, (V,))
    self.assertEqual(int(idx[0]), int(np.argmax(logits)))
    (idx2, _), _ = ref.step([int(idx[0])])
    self.assertEqual(ref.pos, 4)
    self.assertTrue(np.isfinite(val).all() and len(idx2) == 8)


if __name__ == "__main__":
  unittest.main()
