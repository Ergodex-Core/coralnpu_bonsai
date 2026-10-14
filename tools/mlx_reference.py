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
"""Greedy-decodes prompts with MLX on Apple Silicon and records top-k logits.

  pip install mlx-lm
  tools/mlx_reference.py --model mlx-community/gemma-3-1b-it-qat-bf16 \
      --prompt "France capital is" --max-tokens 16 --out ref_mlx_bf16.json

The JSON record matches chat_f2.py's so tools/compare_tokens.py can diff it
against FPGA / MPACT / CPU runs. MLX 4-bit models use group-wise affine
quantization (not the per-channel int4 used on CoralNPU), so MLX is a
quality reference, not a bit-exact golden.
"""

import argparse
import json
import time

import numpy as np

STOP_TOKENS = (0, 1, 106)


def format_chat(prompt: str) -> str:
  return (f"<start_of_turn>user\n{prompt}<end_of_turn>\n"
          "<start_of_turn>model\n")


def topk(logits: np.ndarray, k: int):
  idx = np.argpartition(-logits, k)[:k]
  idx = idx[np.argsort(-logits[idx])]
  return [int(i) for i in idx], [float(logits[i]) for i in idx]


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--model", required=True, help="MLX repo id or path")
  parser.add_argument("--prompt", action="append", default=[])
  parser.add_argument("--prompts-file", default=None)
  parser.add_argument("--raw", action="store_true")
  parser.add_argument("--max-tokens", type=int, default=16)
  parser.add_argument("--topk", type=int, default=8)
  parser.add_argument("--out", required=True)
  args = parser.parse_args()

  import mlx.core as mx  # pylint: disable=g-import-not-at-top
  from mlx_lm import load  # pylint: disable=g-import-not-at-top
  from mlx_lm.models.cache import make_prompt_cache  # pylint: disable=g-import-not-at-top

  prompts = [{"text": p, "raw": args.raw} for p in args.prompt]
  if args.prompts_file:
    with open(args.prompts_file) as f:
      for item in json.load(f):
        if isinstance(item, str):
          prompts.append({"text": item, "raw": args.raw})
        else:
          prompts.append({"text": item["text"], "raw": item.get("raw", args.raw)})
  if not prompts:
    parser.error("no prompts")

  model, tokenizer = load(args.model)
  results = []
  for item in prompts:
    text = item["text"] if item["raw"] else format_chat(item["text"])
    ids = tokenizer.encode(text)
    if not ids or ids[0] != tokenizer.bos_token_id:
      ids = [tokenizer.bos_token_id] + list(ids)
    cache = make_prompt_cache(model)
    steps = []
    t0 = time.perf_counter()
    logits = model(mx.array(ids)[None], cache=cache)
    mx.eval(logits)
    prefill = time.perf_counter() - t0
    logits_np = np.asarray(logits[0].astype(mx.float32))
    for pos in range(len(ids)):
      ti, tv = topk(logits_np[pos], args.topk)
      steps.append({"pos": pos, "input": int(ids[pos]), "token": ti[0],
                    "topk_idx": ti, "topk_val": tv, "seconds": prefill / len(ids)})
    next_tok = steps[-1]["token"]
    generated = []
    decode_s = []
    pos = len(ids)
    while len(generated) < args.max_tokens:
      generated.append(next_tok)
      if next_tok in STOP_TOKENS:
        break
      t0 = time.perf_counter()
      logits = model(mx.array([[next_tok]]), cache=cache)
      mx.eval(logits)
      decode_s.append(time.perf_counter() - t0)
      ti, tv = topk(np.asarray(logits[0, -1].astype(mx.float32)), args.topk)
      steps.append({"pos": pos, "input": int(next_tok), "token": ti[0],
                    "topk_idx": ti, "topk_val": tv, "seconds": decode_s[-1]})
      next_tok = ti[0]
      pos += 1
    out_text = tokenizer.decode([t for t in generated if t not in STOP_TOKENS])
    results.append({
        "prompt": item["text"], "raw": item["raw"], "prompt_ids": [int(i) for i in ids],
        "generated": generated, "text": out_text, "steps": steps,
        "prefill_seconds": prefill,
        "decode_tokens_per_second": (len(decode_s) / sum(decode_s)) if decode_s else 0,
    })
    print(f"> {item['text']!r}\n{out_text!r}")

  with open(args.out, "w") as f:
    json.dump({"model": args.model, "source": "mlx", "device": "mlx",
               "results": results}, f, indent=1)
  print(f"Wrote {args.out}")


if __name__ == "__main__":
  main()
