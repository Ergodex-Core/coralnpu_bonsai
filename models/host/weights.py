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
"""Prepares Gemma 3 weights in the kernels' tiled Q4_0 layout.

  weights.py --model gemma3-1b --out weights/gemma3-1b          # Q4_0 GGUF
  weights.py --model functiongemma-270m --out weights/functiongemma

Output: <out>/weights.npz with, per linear, `<name>.tiles` (uint8) and
`<name>.scales` (f32 [N/16][K/32][16]); f32 norm vectors; plus config.json.

Tile layout (kernel_args.h): for tile t (16 channels), group g (32 k), k4 (4 k):
  byte[lane]      = nib(k0)   | nib(k0+1) << 4
  byte[16 + lane] = nib(k0+2) | nib(k0+3) << 4        nib = code + 8
"""

import argparse
import json
import os

import numpy as np

GROUP = 32
TILE = 16

MODELS = {
    "gemma3-270m": dict(layers=18, dim=640, hidden=2048, heads=4, kv_heads=1,
                        head_dim=256, vocab=262144, window=512,
                        pattern=("local",) * 5 + ("global",),
                        local_base=10_000.0, global_base=1_000_000.0,
                        default="google/gemma-3-270m-it", fmt="safetensors",
                        tokenizer="mlx-community/gemma-3-270m-it-bf16"),
    "functiongemma-270m": dict(layers=18, dim=640, hidden=2048, heads=4,
                               kv_heads=1, head_dim=256, vocab=262144,
                               window=512, pattern=("local",) * 5 + ("global",),
                               local_base=10_000.0, global_base=1_000_000.0,
                               default="google/functiongemma-270m-it",
                               fmt="safetensors",
                               tokenizer="mlx-community/functiongemma-270m-it-bf16"),
    "gemma3-1b": dict(layers=26, dim=1152, hidden=6912, heads=4, kv_heads=1,
                      head_dim=256, vocab=262144, window=512,
                      pattern=("local",) * 5 + ("global",),
                      local_base=10_000.0, global_base=1_000_000.0,
                      default="gaunernst/gemma-3-1b-it-int4-gguf", fmt="gguf",
                      tokenizer="mlx-community/gemma-3-1b-it-qat-bf16"),
}


# --- Q4_0 --------------------------------------------------------------------

def quantize_q4_0(w: np.ndarray):
  """ggml Q4_0 on rows of [N, K]: d = max_signed / -8, q = clip(round(w/d) + 8)."""
  w = np.asarray(w, np.float32)
  n, k = w.shape
  blocks = w.reshape(n, k // GROUP, GROUP)
  amax_idx = np.argmax(np.abs(blocks), axis=-1, keepdims=True)
  mx = np.take_along_axis(blocks, amax_idx, axis=-1)
  d = mx / -8.0
  inv = np.where(d == 0, 0.0, 1.0 / np.where(d == 0, 1.0, d))
  q = np.clip(np.trunc(blocks * inv + 8.5), 0, 15).astype(np.uint8)
  scale = d.astype(np.float16).astype(np.float32).reshape(n, k // GROUP)
  return q.reshape(n, k), scale  # nibbles 0..15, scale (weight = (nib-8)*scale)


def decode_q4_0_blocks(raw: np.ndarray, shape):
  """Raw ggml Q4_0 bytes -> (nibbles uint8 [N,K], scales f32 [N,K/32])."""
  blocks = np.asarray(raw, np.uint8).reshape(-1, 18)
  d = blocks[:, :2].copy().view(np.float16).astype(np.float32).reshape(-1)
  qs = blocks[:, 2:]
  nib = np.concatenate([qs & 0xF, qs >> 4], axis=1)
  n, k = shape[0], int(np.prod(shape[1:]))
  return nib.reshape(n, k), d.reshape(n, k // GROUP)


def pack_tiles(nib: np.ndarray, scales: np.ndarray):
  """[N,K] nibbles + [N,K/32] scales -> kernel tiles (uint8) and [N/16,K/32,16]."""
  n, k = nib.shape
  assert n % TILE == 0 and k % GROUP == 0, (n, k)
  t = nib.reshape(n // TILE, TILE, k // GROUP, 8, 4)  # [t, lane, g, k4, j]
  t = t.transpose(0, 2, 3, 4, 1)                        # [t, g, k4, j, lane]
  lo = (t[..., 0, :] | (t[..., 1, :] << 4)).astype(np.uint8)   # [t,g,k4,lane]
  hi = (t[..., 2, :] | (t[..., 3, :] << 4)).astype(np.uint8)
  tiles = np.stack([lo, hi], axis=3).reshape(-1)        # [t,g,k4,(lo|hi),lane]
  sc = scales.reshape(n // TILE, TILE, k // GROUP).transpose(0, 2, 1)
  return tiles, np.ascontiguousarray(sc, dtype=np.float32)


def unpack_tiles(tiles: np.ndarray, scales: np.ndarray, n: int, k: int):
  """Inverse of pack_tiles, as f32 weights [N, K] (for tests)."""
  t = tiles.reshape(n // TILE, k // GROUP, 8, 2, TILE)
  lo, hi = t[:, :, :, 0, :], t[:, :, :, 1, :]
  nib = np.stack([lo & 15, lo >> 4, hi & 15, hi >> 4], axis=3)  # [t,g,k4,j,lane]
  nib = nib.transpose(0, 4, 1, 2, 3).reshape(n, k).astype(np.int32) - 8
  sc = np.repeat(scales.transpose(0, 2, 1).reshape(n, k // GROUP), GROUP, axis=1)
  return nib.astype(np.float32) * sc


# --- loading -----------------------------------------------------------------

def _hf_files(repo_or_dir, patterns):
  if os.path.isdir(repo_or_dir):
    return repo_or_dir
  from huggingface_hub import snapshot_download  # pylint: disable=g-import-not-at-top
  return snapshot_download(repo_or_dir, allow_patterns=patterns)


_ST_DTYPES = {"F32": np.float32, "F16": np.float16, "BF16": "bf16",
              "I8": np.int8, "U8": np.uint8, "I32": np.int32}


def _read_safetensors(path):
  """Reads a .safetensors file with numpy only; bf16 is widened to f32."""
  with open(path, "rb") as f:
    n = int.from_bytes(f.read(8), "little")
    header = json.loads(f.read(n))
    data = f.read()
  out = {}
  for name, info in header.items():
    if name == "__metadata__":
      continue
    start, end = info["data_offsets"]
    raw = data[start:end]
    kind = _ST_DTYPES[info["dtype"]]
    if kind == "bf16":
      arr = (np.frombuffer(raw, np.uint16).astype(np.uint32) << 16).view(np.float32)
    else:
      arr = np.frombuffer(raw, kind)
    out[name] = arr.reshape(info["shape"])
  return out


def load_safetensors(repo_or_dir):
  local = _hf_files(repo_or_dir, ["*.safetensors", "*.json"])
  state = {}
  for f in sorted(os.listdir(local)):
    if f.endswith(".safetensors"):
      state.update(_read_safetensors(os.path.join(local, f)))
  out = {}
  for name, v in state.items():
    for prefix in ("model.", "language_model.model."):
      if name.startswith(prefix):
        name = name[len(prefix):]
    out[name] = v
  return out  # HF names without the "model." prefix; bf16/f32 arrays


_GGUF_TO_HF = {
    "token_embd.weight": "embed_tokens.weight",
    "output_norm.weight": "norm.weight",
    "attn_q.weight": "self_attn.q_proj.weight",
    "attn_k.weight": "self_attn.k_proj.weight",
    "attn_v.weight": "self_attn.v_proj.weight",
    "attn_output.weight": "self_attn.o_proj.weight",
    "attn_q_norm.weight": "self_attn.q_norm.weight",
    "attn_k_norm.weight": "self_attn.k_norm.weight",
    "attn_norm.weight": "input_layernorm.weight",
    "post_attention_norm.weight": "post_attention_layernorm.weight",
    "ffn_norm.weight": "pre_feedforward_layernorm.weight",
    "post_ffw_norm.weight": "post_feedforward_layernorm.weight",
    "ffn_gate.weight": "mlp.gate_proj.weight",
    "ffn_up.weight": "mlp.up_proj.weight",
    "ffn_down.weight": "mlp.down_proj.weight",
}


def load_gguf(repo_or_path):
  """Q4_0 GGUF -> {hf_name: (nib, scales) or f32 array (norms minus 1)}."""
  import gguf  # pylint: disable=g-import-not-at-top
  path = repo_or_path
  if not os.path.exists(path):
    from huggingface_hub import HfApi, hf_hub_download  # pylint: disable=g-import-not-at-top
    files = [f for f in HfApi().list_repo_files(repo_or_path) if f.endswith(".gguf")]
    path = hf_hub_download(repo_or_path, files[0])
  reader = gguf.GGUFReader(path)
  out = {}
  for t in reader.tensors:
    name = t.name
    if name.startswith("blk."):
      _, layer, rest = name.split(".", 2)
      hf = f"layers.{layer}.{_GGUF_TO_HF[rest]}"
    else:
      hf = _GGUF_TO_HF[name]
    shape = tuple(int(d) for d in reversed(t.shape))
    kind = t.tensor_type.name
    if kind == "F32":
      v = np.asarray(t.data, np.float32).reshape(shape)
      out[hf] = v - 1.0 if hf.endswith("norm.weight") else v
    elif kind == "Q4_0":
      out[hf] = decode_q4_0_blocks(np.asarray(t.data), shape)
    else:
      raise ValueError(f"{name}: unsupported GGUF type {kind}")
  return out


def as_q4(value):
  """HF tensor (f32/bf16 array or (nib, scales)) -> (nib, scales)."""
  if isinstance(value, tuple):
    return value
  return quantize_q4_0(np.asarray(value, np.float32))


def prepare(model: str, source: str | None, out_dir: str):
  cfg = dict(MODELS[model])
  src = source or cfg["default"]
  fmt = "gguf" if src.endswith(".gguf") or "gguf" in src.lower() else cfg["fmt"]
  state = load_gguf(src) if fmt == "gguf" else load_safetensors(src)
  os.makedirs(out_dir, exist_ok=True)
  arrays = {}

  def put_linear(name, nib, scales):
    tiles, sc = pack_tiles(nib, scales)
    arrays[name + ".tiles"] = tiles
    arrays[name + ".scales"] = sc

  def f32(name):
    return np.asarray(state[name], np.float32).reshape(-1)

  nib, sc = as_q4(state["embed_tokens.weight"])
  put_linear("embed", nib, sc)
  arrays["final_norm"] = f32("norm.weight")
  for i in range(cfg["layers"]):
    p = f"layers.{i}."
    q = as_q4(state[p + "self_attn.q_proj.weight"])
    k = as_q4(state[p + "self_attn.k_proj.weight"])
    v = as_q4(state[p + "self_attn.v_proj.weight"])
    put_linear(f"l{i}.q", *q)
    put_linear(f"l{i}.kv", np.concatenate([k[0], v[0]]), np.concatenate([k[1], v[1]]))
    put_linear(f"l{i}.o", *as_q4(state[p + "self_attn.o_proj.weight"]))
    gate = as_q4(state[p + "mlp.gate_proj.weight"])
    up = as_q4(state[p + "mlp.up_proj.weight"])
    put_linear(f"l{i}.gate_up", np.concatenate([gate[0], up[0]]),
               np.concatenate([gate[1], up[1]]))
    put_linear(f"l{i}.down", *as_q4(state[p + "mlp.down_proj.weight"]))
    for ours, theirs in (("pre_attn", "input_layernorm"),
                         ("post_attn", "post_attention_layernorm"),
                         ("pre_ffw", "pre_feedforward_layernorm"),
                         ("post_ffw", "post_feedforward_layernorm"),
                         ("q_norm", "self_attn.q_norm"),
                         ("k_norm", "self_attn.k_norm")):
      arrays[f"l{i}.{ours}"] = f32(p + theirs + ".weight")
  np.savez(os.path.join(out_dir, "weights.npz"), **arrays)
  cfg["source"] = src
  cfg["format"] = fmt
  cfg["model"] = model
  with open(os.path.join(out_dir, "config.json"), "w") as f:
    json.dump(cfg, f, indent=2)
  total = sum(int(a.nbytes) for a in arrays.values())
  print(f"wrote {out_dir}/weights.npz ({total / 2**20:.1f} MiB) and config.json")


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--model", choices=sorted(MODELS), required=True)
  parser.add_argument("--source", default=None,
                      help="HF repo / dir / .gguf (default: the model's registry entry)")
  parser.add_argument("--out", required=True)
  args = parser.parse_args()
  prepare(args.model, args.source, args.out)


if __name__ == "__main__":
  main()
