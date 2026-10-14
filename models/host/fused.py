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
"""Host side of the fused assembly decode kernel (models/asm/gemma_decode.S).

`FusedDecoder` wraps a `GemmaNative` model: prefill chunks still use the
per-operator kernels (they handle up to 16 tokens), while each generated
token runs one launch of `gemma_decode.elf` with f16 group scales and
DTCM-resident activations. The argument block layout mirrors the comment at
the top of the assembly file.
"""

import struct

import numpy as np

from models.host import device as dev

LAYER_WORDS = 22
DARGS_HEADER = 224
# field name -> byte offset
OFF = dict(op=0, layers=4, dim=8, hidden=12, heads=16, head_dim=20, vocab=24,
           pos=28, token=32, emb_tiles=36, emb_scales=40, final_norm=44, x=48,
           h=52, xn=56, q=60, kv=64, attn=68, ao=72, gate_up=76, act=80,
           cos_local=84, sin_local=88, cos_global=92, sin_global=96,
           window=100, eps=104, qscale=108, embscale=112, status=116,
           cycles=120, topk_idx=124, topk_val=156, test_a=188, test_b=192,
           test_c=196, test_d=200, test_e=204, test_n=208, test_k=212,
           test_f=216)
FLOAT_FIELDS = {"eps", "qscale", "embscale", "test_f"}
STATUS_OK = 0x600D


def pack_dargs(fields: dict, layer_table: list[list[int]] | None = None) -> bytes:
  buf = bytearray(DARGS_HEADER + (len(layer_table or []) * LAYER_WORDS * 4))
  for name, value in fields.items():
    off = OFF[name]
    if name in FLOAT_FIELDS:
      struct.pack_into("<f", buf, off, float(value))
    else:
      struct.pack_into("<I", buf, off, int(value) & 0xFFFFFFFF)
  for i, rec in enumerate(layer_table or []):
    assert len(rec) == LAYER_WORDS
    struct.pack_into(f"<{LAYER_WORDS}I", buf, DARGS_HEADER + i * LAYER_WORDS * 4,
                     *[int(v) & 0xFFFFFFFF for v in rec])
  return bytes(buf)


def scales_to_f16(scales_f32: np.ndarray) -> np.ndarray:
  """[N/16][K/32][16] f32 -> same layout f16 (the kernel converts back)."""
  return np.ascontiguousarray(scales_f32, dtype=np.float32).astype(np.float16)


class FusedDecoder:
  """Runs single-token decode steps with the fused kernel."""

  def __init__(self, model, elf_path: str):
    self.model = model
    self.be = model.be
    self.image = dev.KernelImage(elf_path)
    self.dargs = self.image.elf.address("dargs")
    c, w, b = model.cfg, model.w, model.b
    self._f16 = {}
    data = np.load(model._weights_path)
    for name in data.files:
      if name.endswith(".scales"):
        self._f16[name] = model._upload(model.ddr, scales_to_f16(data[name]))
    self.layer_table = []
    for i in range(model.L):
      kc = b["kcache"] + i * model.S * model.hd * 4
      vc = b["vcache"] + i * model.S * model.hd * 4
      local = 1 if c["pattern"][i % len(c["pattern"])] == "local" else 0
      self.layer_table.append([
          w[f"l{i}.q.tiles"], self._f16[f"l{i}.q.scales"],
          w[f"l{i}.kv.tiles"], self._f16[f"l{i}.kv.scales"],
          w[f"l{i}.o.tiles"], self._f16[f"l{i}.o.scales"],
          w[f"l{i}.gate_up.tiles"], self._f16[f"l{i}.gate_up.scales"],
          w[f"l{i}.down.tiles"], self._f16[f"l{i}.down.scales"],
          w[f"l{i}.pre_attn"], w[f"l{i}.post_attn"], w[f"l{i}.pre_ffw"],
          w[f"l{i}.post_ffw"], w[f"l{i}.q_norm"], w[f"l{i}.k_norm"],
          kc, vc, local, 0, 0, 0])
    self.fixed = dict(
        layers=model.L, dim=model.D, hidden=model.H, heads=model.nh,
        head_dim=model.hd, vocab=model.V, emb_tiles=w["embed.tiles"],
        emb_scales=self._f16["embed.scales"], final_norm=w["final_norm"],
        x=b["x"], h=b["h"], xn=b["xn"], q=b["q"], kv=b["kv"], attn=b["attn"],
        ao=b["ao"], gate_up=b["gate_up"], act=b["act"],
        cos_local=model.rope["local"][0], sin_local=model.rope["local"][1],
        cos_global=model.rope["global"][0], sin_global=model.rope["global"][1],
        window=c["window"], eps=1e-6, qscale=model.hd ** -0.5,
        embscale=float(np.sqrt(model.D)))

  def launch(self, op=0, **extra):
    fields = dict(self.fixed, op=op, **extra)
    blob = pack_dargs(fields, self.layer_table if op == 0 else None)
    self.be.dev.hold_reset()
    for seg in self.image.segments:
      self.be.dev.write_bytes(seg["address"], seg["data"])
    self.be._loaded = self.image.name
    self.be.dev.write_bytes(self.dargs, blob)
    if self.image.ret is not None:
      self.be.dev.write32(self.image.ret, 0x0BADD00D)
    import time  # pylint: disable=g-import-not-at-top
    t0 = time.monotonic()
    self.be.dev.start(self.image.entry)
    self.be.dev.wait_halt(600.0)
    seconds = time.monotonic() - t0
    status = self.be.dev.read32(self.dargs + OFF["status"])
    cycles = self.be.dev.read32(self.dargs + OFF["cycles"])
    if status != STATUS_OK:
      raise RuntimeError(f"gemma_decode op={op}: status={status:#x}")
    return dev.LaunchResult(status, cycles, seconds)

  def step(self, token: int):
    """One decode token at model.pos; returns (idx[8], val[8]) and result."""
    r = self.launch(op=0, pos=self.model.pos, token=token)
    raw = self.be.read(self.dargs + OFF["topk_idx"], 64)
    idx = [int(v) for v in np.frombuffer(raw[:32], np.uint32)]
    val = [float(v) for v in np.frombuffer(raw[32:], np.float32)]
    self.model.pos += 1
    p = self.model.profile.setdefault("gemma_decode", [0, 0, 0.0])
    p[0] += 1
    p[1] += r.cycles
    p[2] += r.seconds
    return idx, val
