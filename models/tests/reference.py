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
"""NumPy references for every kernel and for a whole decode step.

The kernel references mirror the device arithmetic (int8 activations, int32
group sums, f32 scaling) so hardware results can be compared tightly; the
model reference composes them exactly like models/host/runtime.py.
"""

import numpy as np

from models.host import weights as W

GROUP = 32


def quant_act(x):
  """x f32 [M,K] -> (xq int8, sx f32 [M,K/32], xsum8 f32 [M,K/32])."""
  m, k = x.shape
  g = x.reshape(m, k // GROUP, GROUP).astype(np.float32)
  amax = np.maximum(np.abs(g).max(axis=-1, keepdims=True), 1e-12)
  sx = amax / 127.0
  xq = np.clip(np.rint(g / sx), -127, 127).astype(np.int8)
  xsum8 = 8.0 * xq.astype(np.int32).sum(axis=-1).astype(np.float32)
  return xq.reshape(m, k), sx.reshape(m, k // GROUP).astype(np.float32), xsum8


def matvec_q4(xq, sx, xsum8, nib, scales):
  """Device arithmetic: nib [N,K] 0..15, scales [N,K/32] -> f32 [M,N]."""
  m, k = xq.shape
  n = nib.shape[0]
  xg = xq.reshape(m, k // GROUP, GROUP).astype(np.int32)
  wg = nib.reshape(n, k // GROUP, GROUP).astype(np.int32)
  acc = np.einsum("mgk,ngk->mgn", xg, wg).astype(np.float32)   # int32 exact
  acc = acc - xsum8[:, :, None]
  acc = acc * sx[:, :, None] * scales.T[None, :, :]
  return acc.sum(axis=1)


def rmsnorm(x, scale, eps=1e-6, residual=None):
  var = np.mean(x.astype(np.float32) ** 2, axis=-1, keepdims=True)
  y = x / np.sqrt(var + eps) * (1.0 + scale)
  return (y + residual) if residual is not None else y


def rope(x, pos, base, hd):
  """Rotate-half rope on [M, heads, hd] at positions pos[M]."""
  half = hd // 2
  inv = base ** (-np.arange(0, half, dtype=np.float64) * 2 / hd)
  ang = np.asarray(pos, np.float64)[:, None] * inv[None, :]
  c, s = np.cos(ang).astype(np.float32)[:, None, :], np.sin(ang).astype(np.float32)[:, None, :]
  x1, x2 = x[..., :half], x[..., half:]
  return np.concatenate([x1 * c - x2 * s, x2 * c + x1 * s], axis=-1)


def attention(q, kcache, vcache, pos0, scale, window=0):
  """q [M, heads, hd]; caches [S, hd] (one KV head). Causal, optional window."""
  m, heads, hd = q.shape
  out = np.zeros_like(q)
  for i in range(m):
    pos = pos0 + i
    start = max(0, pos + 1 - window) if window else 0
    k = kcache[start:pos + 1]
    v = vcache[start:pos + 1]
    for h in range(heads):
      sc = (k @ q[i, h]) * scale
      p = np.exp(sc - sc.max())
      p /= p.sum()
      out[i, h] = p @ v
  return out


def gelu_tanh(x):
  return 0.5 * x * (1.0 + np.tanh(0.7978845608 * (x + 0.044715 * x ** 3)))


def topk(x, k=8):
  idx = np.argsort(-x, kind="stable")[:k]
  return idx.astype(np.uint32), x[idx].astype(np.float32)


class ModelRef:
  """Decode-step reference with the same quantized arithmetic as the device."""

  def __init__(self, cfg, arrays):
    self.cfg, self.a = cfg, arrays
    self.pos = 0
    L, S, hd = cfg["layers"], cfg.get("cache_length", 128), cfg["head_dim"]
    self.kc = np.zeros((L, S, hd), np.float32)
    self.vc = np.zeros((L, S, hd), np.float32)

  def linear(self, name, x):
    tiles, sc = self.a[name + ".tiles"], self.a[name + ".scales"]
    n, groups = sc.shape[0] * 16, sc.shape[1]
    k = groups * GROUP
    w = W.unpack_tiles(tiles, sc, n, k)
    nib = np.rint(w / np.repeat(sc.transpose(0, 2, 1).reshape(n, groups), GROUP, axis=1)).astype(np.int32) + 8
    xq, sx, xsum8 = quant_act(x)
    return matvec_q4(xq, sx, xsum8, nib.astype(np.uint8), sc.transpose(0, 2, 1).reshape(n, groups))

  def step(self, tokens):
    c, a = self.cfg, self.a
    D, H, nh, hd = c["dim"], c["hidden"], c["heads"], c["head_dim"]
    m, pos0 = len(tokens), self.pos
    emb_sc = a["embed.scales"]
    emb = W.unpack_tiles(a["embed.tiles"], emb_sc, c["vocab"], D)
    x = emb[np.asarray(tokens)] * np.float32(np.sqrt(D))
    for i in range(c["layers"]):
      local = c["pattern"][i % len(c["pattern"])] == "local"
      base = c["local_base"] if local else c["global_base"]
      xn = rmsnorm(x, a[f"l{i}.pre_attn"])
      q = self.linear(f"l{i}.q", xn).reshape(m, nh, hd)
      kv = self.linear(f"l{i}.kv", xn)
      k, v = kv[:, :hd], kv[:, hd:]
      q = rmsnorm(q, a[f"l{i}.q_norm"])
      k = rmsnorm(k, a[f"l{i}.k_norm"])
      pos = np.arange(pos0, pos0 + m)
      q = rope(q, pos, base, hd)
      k = rope(k[:, None, :], pos, base, hd)[:, 0]
      self.kc[i, pos0:pos0 + m] = k
      self.vc[i, pos0:pos0 + m] = v
      att = attention(q, self.kc[i], self.vc[i], pos0, hd ** -0.5,
                      c["window"] if local else 0).reshape(m, nh * hd)
      ao = self.linear(f"l{i}.o", att)
      h = rmsnorm(ao, a[f"l{i}.post_attn"], residual=x)
      xn = rmsnorm(h, a[f"l{i}.pre_ffw"])
      gu = self.linear(f"l{i}.gate_up", xn)
      act = gelu_tanh(gu[:, :H]) * gu[:, H:]
      down = self.linear(f"l{i}.down", act)
      x = rmsnorm(down, a[f"l{i}.post_ffw"], residual=h)
    xn = rmsnorm(x[-1:], a["final_norm"])
    logits = self.linear("embed", xn)[0]
    self.pos += m
    return topk(logits), logits
