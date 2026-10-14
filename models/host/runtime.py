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
"""Gemma 3 decode loop on CoralNPU built from per-dispatch kernels.

The host owns the transformer control flow; the core runs one kernel at a
time. Weights live in DDR in the tiled Q4_0 layout, activations and the KV
cache in HBM, all read by the kernels directly over the core's data bus.

Per token step (M <= 16 tokens at positions pos0..pos0+M-1):
  embed -> for each layer:
    rmsnorm(pre_attn) -> quant_act -> matvec q, matvec kv
    -> rmsnorm(q_norm per head), rmsnorm(k_norm) -> rope q; rope k + cache
    -> attention -> quant_act -> matvec o -> rmsnorm(post_attn) + residual
    -> rmsnorm(pre_ffw) -> quant_act -> matvec gate_up -> gelu_mul
    -> quant_act -> matvec down -> rmsnorm(post_ffw) + residual
  -> rmsnorm(final) on the last row -> quant_act -> matvec logits (tied
  embedding) -> topk.
"""

import json
import math
import os
import time

import numpy as np

from models.host import device as dev

MAX_TOKENS = 16
GROUP = 32
TILE = 16
EPS = 1e-6
TOPK = 8

KERNELS = ("matvec_q4", "quant_act", "rmsnorm", "rope", "attention",
           "gelu_mul", "embed", "topk")


class Arena:
  """Bump allocator over one address window."""

  def __init__(self, base, size, name):
    self.base, self.size, self.name = base, size, name
    self.cursor = base

  def alloc(self, nbytes, align=64):
    addr = (self.cursor + align - 1) & ~(align - 1)
    if addr + nbytes > self.base + self.size:
      raise MemoryError(f"{self.name} window exhausted ({nbytes} bytes)")
    self.cursor = addr + nbytes
    return addr

  @property
  def used(self):
    return self.cursor - self.base


class GemmaNative:
  """Loads weights/kernels onto a backend and runs decode steps."""

  def __init__(self, backend: dev.Backend, weights_dir: str, kernels_dir: str,
               cache_length: int = 128, verbose: bool = False):
    self.be = backend
    self.verbose = verbose
    with open(os.path.join(weights_dir, "config.json")) as f:
      self.cfg = json.load(f)
    c = self.cfg
    self.L, self.D, self.H = c["layers"], c["dim"], c["hidden"]
    self.nh, self.hd, self.V = c["heads"], c["head_dim"], c["vocab"]
    assert c["kv_heads"] == 1, "kernels assume a single KV head"
    self.S = cache_length
    self.kernels = {k: dev.KernelImage(os.path.join(kernels_dir, k + ".elf"))
                    for k in KERNELS}
    # The core reads wrong data from the first bytes of a window (seen on the
    # F2 image at 0xC0000000), so the first 64 KiB of each window is unused.
    skip = 0x10000
    self.ddr = Arena(backend.ddr[0] + skip, backend.ddr[1] - skip, "DDR")
    self.hbm = (Arena(backend.hbm[0] + skip, backend.hbm[1] - skip, "HBM")
                if backend.hbm else self.ddr)
    self.profile = {}
    self.trace = None  # optional callback(stage: str, layer: int | None)
    self._weights_path = os.path.join(weights_dir, "weights.npz")
    self.fused = None  # FusedDecoder when the assembly kernel is enabled
    self._load_weights(self._weights_path)
    self._alloc_buffers()
    self._write_rope_tables()
    self.pos = 0

  # --- setup -----------------------------------------------------------------

  def _upload(self, arena, arr):
    arr = np.ascontiguousarray(arr)
    addr = arena.alloc(arr.nbytes)
    self.be.write_array(addr, arr)
    return addr

  def _load_weights(self, path):
    data = np.load(path)
    self.w = {}
    t0 = time.time()
    for name in data.files:
      self.w[name] = self._upload(self.ddr, data[name])
    if self.verbose:
      print(f"weights: {self.ddr.used / 2**20:.1f} MiB to DDR in "
            f"{time.time() - t0:.1f}s")

  def _alloc_buffers(self):
    f32 = 4
    kmax = max(self.D, self.H, self.nh * self.hd)
    a = self.hbm.alloc
    self.b = dict(
        tokens=a(MAX_TOKENS * 4),
        x=a(MAX_TOKENS * self.D * f32),       # residual stream
        xn=a(MAX_TOKENS * self.D * f32),      # normed input
        h=a(MAX_TOKENS * self.D * f32),       # after attention block
        xq=a(MAX_TOKENS * kmax),              # k-major
        xq1d=a(MAX_TOKENS * kmax),            # row-major (decode path)              # int8 activations
        sx=a(MAX_TOKENS * (kmax // GROUP) * f32),
        xsum8=a(MAX_TOKENS * (kmax // GROUP) * f32),
        q=a(MAX_TOKENS * self.nh * self.hd * f32),
        kv=a(MAX_TOKENS * 2 * self.hd * f32),
        attn=a(MAX_TOKENS * self.nh * self.hd * f32),
        ao=a(MAX_TOKENS * self.D * f32),
        gate_up=a(MAX_TOKENS * 2 * self.H * f32),
        act=a(MAX_TOKENS * self.H * f32),
        logits=a(MAX_TOKENS * self.V * f32),  # all rows for speculative verify
        topk=a(64 * MAX_TOKENS),
        kcache=a(self.L * self.S * self.hd * f32),
        vcache=a(self.L * self.S * self.hd * f32),
    )
    self.rope = {}
    if self.verbose:
      print(f"buffers: {self.hbm.used / 2**20:.1f} MiB in {self.hbm.name}")

  def _write_rope_tables(self):
    half = self.hd // 2
    for name, base in (("local", self.cfg["local_base"]),
                       ("global", self.cfg["global_base"])):
      inv = base ** (-np.arange(0, half, dtype=np.float64) * 2 / self.hd)
      ang = np.arange(self.S, dtype=np.float64)[:, None] * inv[None, :]
      self.rope[name] = (self._upload(self.hbm, np.cos(ang).astype(np.float32)),
                         self._upload(self.hbm, np.sin(ang).astype(np.float32)))

  # --- kernels ---------------------------------------------------------------

  def _launch(self, name, **args):
    r = self.be.launch(self.kernels[name], **args)
    p = self.profile.setdefault(name, [0, 0, 0.0])
    p[0] += 1
    p[1] += r.cycles
    p[2] += r.seconds
    return r

  def _quant(self, src, m, k, stride=None):
    self._launch("quant_act", m=m, k=k, in0=src, out0=self.b["xq"],
                 out1=self.b["sx"], in1=self.b["xsum8"], in3=self.b["xq1d"],
                 u0=stride or k)

  def _matvec(self, wname, m, k, n, out, out_stride, accumulate=False):
    self._launch("matvec_q4", m=m, k=k, n=n, in0=self.b["xq"], in1=self.b["sx"],
                 in2=self.b["xsum8"], in3=self.w[wname + ".tiles"],
                 u2=self.w[wname + ".scales"], u3=self.b["xq1d"], out0=out,
                 u0=out_stride, u1=0, flags=1 if accumulate else 0)

  def _rmsnorm(self, src, scale, out, m, k, stride=None, residual=None):
    self._launch("rmsnorm", m=m, k=k, in0=src, in1=scale, out0=out,
                 in2=residual or 0, flags=1 if residual else 0,
                 u0=stride or k, f0=EPS)

  def _t(self, stage, layer=None):
    if self.trace:
      self.trace(stage, layer)

  def _layer(self, i, m, pos0):
    b, w = self.b, self.w
    D, H, nh, hd = self.D, self.H, self.nh, self.hd
    local = self.cfg["pattern"][i % len(self.cfg["pattern"])] == "local"
    cos, sin = self.rope["local" if local else "global"]
    kc = b["kcache"] + i * self.S * hd * 4
    vc = b["vcache"] + i * self.S * hd * 4
    # Attention block.
    self._rmsnorm(b["x"], w[f"l{i}.pre_attn"], b["xn"], m, D)
    self._t("xn_attn", i)
    self._quant(b["xn"], m, D)
    self._matvec(f"l{i}.q", m, D, nh * hd, b["q"], nh * hd)
    self._matvec(f"l{i}.kv", m, D, 2 * hd, b["kv"], 2 * hd)
    self._t("qkv", i)
    self._rmsnorm(b["q"], w[f"l{i}.q_norm"], b["q"], m * nh, hd)
    self._rmsnorm(b["kv"], w[f"l{i}.k_norm"], b["kv"], m, hd, stride=2 * hd)
    self._t("qk_norm", i)
    self._launch("rope", m=m, n=nh * hd, in0=b["q"], in1=cos, in2=sin, u0=pos0,
                 u1=nh, u2=hd)
    self._launch("rope", m=m, n=2 * hd, in0=b["kv"], in1=cos, in2=sin, u0=pos0,
                 u1=1, u2=hd, flags=3, out0=kc, in3=b["kv"] + hd * 4, out1=vc)
    self._launch("attention", m=m, n=nh * hd, in0=b["q"], in1=kc, in2=vc,
                 out0=b["attn"], u0=pos0, u1=nh, u2=hd,
                 u3=self.cfg["window"] if local else 0, f0=hd ** -0.5)
    self._t("rope", i)
    self._t("attn", i)
    self._quant(b["attn"], m, nh * hd)
    self._matvec(f"l{i}.o", m, nh * hd, D, b["ao"], D)
    self._t("o_proj", i)
    self._rmsnorm(b["ao"], w[f"l{i}.post_attn"], b["h"], m, D, residual=b["x"])
    self._t("h", i)
    # MLP block.
    self._rmsnorm(b["h"], w[f"l{i}.pre_ffw"], b["xn"], m, D)
    self._quant(b["xn"], m, D)
    self._matvec(f"l{i}.gate_up", m, D, 2 * H, b["gate_up"], 2 * H)
    self._t("gate_up", i)
    self._launch("gelu_mul", m=m, k=H, in0=b["gate_up"], out0=b["act"])
    self._quant(b["act"], m, H)
    self._matvec(f"l{i}.down", m, H, D, b["ao"], D)
    self._t("down", i)
    self._rmsnorm(b["ao"], w[f"l{i}.post_ffw"], b["x"], m, D, residual=b["h"])
    self._t("x", i)

  # --- public ----------------------------------------------------------------

  def reset(self):
    self.pos = 0

  def step(self, tokens):
    """Runs up to 16 tokens at the current position; returns (idx, val) top-k
    for the last token."""
    m = len(tokens)
    assert 1 <= m <= MAX_TOKENS and self.pos + m <= self.S
    b, w = self.b, self.w
    pos0 = self.pos
    self.be.write_array(b["tokens"], np.asarray(tokens, dtype=np.uint32))
    self._launch("embed", m=m, k=self.D, in0=b["tokens"], in1=w["embed.tiles"],
                 in2=w["embed.scales"], out0=b["x"], f0=math.sqrt(self.D))
    self._t("embed")
    for i in range(self.L):
      self._layer(i, m, pos0)
    last = b["x"] + (m - 1) * self.D * 4
    self._rmsnorm(last, w["final_norm"], b["xn"], 1, self.D)
    self._quant(b["xn"], 1, self.D)
    self._matvec("embed", 1, self.D, self.V, b["logits"], self.V)
    self._t("logits")
    self._launch("topk", n=self.V, in0=b["logits"], out0=b["topk"], u0=TOPK)
    raw = self.be.read(b["topk"], 64)
    idx = np.frombuffer(raw[:32], dtype=np.uint32)
    val = np.frombuffer(raw[32:], dtype=np.float32)
    self.pos += m
    return [int(i) for i in idx], [float(v) for v in val]

  def step_verify(self, tokens):
    """Runs up to 16 tokens and returns the greedy next token after *each*
    row (list of ints) plus the per-row top-k. Used by speculative decoding:
    rows are [last accepted token, draft_1, ..., draft_k]."""
    m = len(tokens)
    assert 1 <= m <= MAX_TOKENS and self.pos + m <= self.S
    b, w = self.b, self.w
    pos0 = self.pos
    self.be.write_array(b["tokens"], np.asarray(tokens, dtype=np.uint32))
    self._launch("embed", m=m, k=self.D, in0=b["tokens"], in1=w["embed.tiles"],
                 in2=w["embed.scales"], out0=b["x"], f0=math.sqrt(self.D))
    for i in range(self.L):
      self._layer(i, m, pos0)
    self._rmsnorm(b["x"], w["final_norm"], b["xn"], m, self.D)
    self._quant(b["xn"], m, self.D)
    self._matvec("embed", m, self.D, self.V, b["logits"], self.V)
    tops = []
    for r in range(m):
      self._launch("topk", n=self.V, in0=b["logits"] + r * self.V * 4,
                   out0=b["topk"] + r * 64, u0=TOPK)
    raw = self.be.read(b["topk"], 64 * m)
    for r in range(m):
      blk = raw[r * 64:(r + 1) * 64]
      tops.append(([int(i) for i in np.frombuffer(blk[:32], np.uint32)],
                   [float(v) for v in np.frombuffer(blk[32:], np.float32)]))
    self.pos += m
    return tops

  def speculative_generate(self, prompt_ids, max_tokens, draft, k=7,
                           stop=(0, 1, 106)):
    """Greedy speculative decoding. `draft(context_ids, k)` returns up to k
    proposed next tokens. Returns (generated, step records, prefill seconds)."""
    self.reset()
    k = min(k, MAX_TOKENS - 1)
    steps = []
    t0 = time.perf_counter()
    idx = None
    for start in range(0, len(prompt_ids), MAX_TOKENS):
      chunk = prompt_ids[start:start + MAX_TOKENS]
      idx, val = self.step(chunk)
    prefill = time.perf_counter() - t0
    generated = [idx[0]]
    context = list(prompt_ids) + generated
    while len(generated) < max_tokens and generated[-1] not in stop:
      base_pos = self.pos
      last = generated[-1]
      ts = time.perf_counter()
      drafts = list(draft(context, k))[:k]
      draft_s = time.perf_counter() - ts
      room = min(len(drafts), self.S - self.pos - 1, max_tokens - len(generated) - 1)
      drafts = drafts[:max(room, 0)]
      ts = time.perf_counter()
      tops = self.step_verify([last] + drafts)
      verify_s = time.perf_counter() - ts
      targets = [t[0][0] for t in tops]  # argmax after row i
      accepted = 0
      new_tokens = []
      for i, d in enumerate(drafts):
        if targets[i] == d:
          accepted += 1
          new_tokens.append(d)
        else:
          break
      new_tokens.append(targets[accepted])  # correction or bonus token
      # Roll the cache back to the rows that were actually valid.
      self.pos = base_pos + 1 + accepted
      for tok in new_tokens:
        if len(generated) >= max_tokens:
          break
        generated.append(tok)
        context.append(tok)
        if tok in stop:
          break
      steps.append(dict(pos=base_pos, drafts=len(drafts), accepted=accepted,
                        emitted=len(new_tokens), draft_seconds=draft_s,
                        verify_seconds=verify_s, tokens=1 + len(drafts),
                        token=new_tokens[-1], topk_idx=tops[accepted][0],
                        topk_val=tops[accepted][1], seconds=draft_s + verify_s))
    return generated, steps, prefill

  def generate(self, prompt_ids, max_tokens, stop=(0, 1, 106)):
    """Greedy decode; returns (generated ids, per-step records)."""
    self.reset()
    steps = []
    idx = val = None
    t0 = time.perf_counter()
    for start in range(0, len(prompt_ids), MAX_TOKENS):
      chunk = prompt_ids[start:start + MAX_TOKENS]
      ts = time.perf_counter()
      idx, val = self.step(chunk)
      steps.append(dict(pos=self.pos - 1, input=chunk[-1], token=idx[0],
                        topk_idx=idx, topk_val=val,
                        seconds=time.perf_counter() - ts, tokens=len(chunk)))
    prefill = time.perf_counter() - t0
    generated = []
    next_tok = idx[0]
    while len(generated) < max_tokens:
      generated.append(next_tok)
      if next_tok in stop or self.pos >= self.S:
        break
      ts = time.perf_counter()
      if self.fused is not None:
        idx, val = self.fused.step(next_tok)
      else:
        idx, val = self.step([next_tok])
      steps.append(dict(pos=self.pos - 1, input=next_tok, token=idx[0],
                        topk_idx=idx, topk_val=val,
                        seconds=time.perf_counter() - ts, tokens=1))
      next_tok = idx[0]
    return generated, steps, prefill

  def profile_report(self):
    lines = ["kernel        launches     cycles(total)   seconds"]
    for name, (n, cyc, sec) in sorted(self.profile.items(), key=lambda kv: -kv[1][2]):
      lines.append(f"{name:12s} {n:9d} {cyc:16d} {sec:9.3f}")
    return "\n".join(lines)
