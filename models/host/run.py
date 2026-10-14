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
"""Runs Gemma 3 1B / FunctionGemma on the CoralNPU F2 image with the native
kernels.

  python3 -m models.host.weights --model gemma3-1b --out weights/gemma3-1b
  bazel build //models/kernels:all_kernels
  python3 -m models.host.run --weights weights/gemma3-1b \\
      --kernels bazel-bin/models/kernels --prompt "France capital is" \\
      --max-tokens 16 --out run_native.json

The JSON record matches chat_f2.py / tools/mlx_reference.py, so
tools/compare_tokens.py can diff it against the compiler path and MLX.
"""

import argparse
import json
import os
import sys
import time

from models.host import device as dev
from models.host import runtime
from models.host import weights

STOP = (0, 1, 106)


def load_tokenizer(repo_or_dir):
  from tokenizers import Tokenizer  # pylint: disable=g-import-not-at-top
  if os.path.isdir(repo_or_dir):
    path = os.path.join(repo_or_dir, "tokenizer.json")
  else:
    from huggingface_hub import hf_hub_download  # pylint: disable=g-import-not-at-top
    path = hf_hub_download(repo_or_dir, "tokenizer.json")
  return Tokenizer.from_file(path)


def format_chat(prompt):
  return (f"<start_of_turn>user\n{prompt}<end_of_turn>\n"
          "<start_of_turn>model\n")


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--weights", required=True, help="dir from weights.py")
  parser.add_argument("--kernels", required=True, help="dir with <kernel>.elf")
  parser.add_argument("--backend", choices=("f2", "mpact"), default="f2")
  parser.add_argument("--slot", type=int, default=0)
  parser.add_argument("--tokenizer", default=None,
                      help="HF repo or dir with tokenizer.json")
  parser.add_argument("--prompt", action="append", default=[])
  parser.add_argument("--prompts-file", default=None)
  parser.add_argument("--raw", action="store_true", help="no chat template")
  parser.add_argument("--max-tokens", type=int, default=16,
                      help="output tokens (OSL)")
  parser.add_argument("--ignore-eos", action="store_true",
                      help="always generate max-tokens (fixed OSL)")
  parser.add_argument("--isl", type=int, action="append", default=[],
                      help="synthesize a raw prompt of exactly this many "
                      "tokens (repeatable); measures TTFT/TPS per length")
  parser.add_argument("--cache-length", type=int, default=128)
  parser.add_argument("--out", default=None)
  parser.add_argument("--verbose", action="store_true")
  parser.add_argument("--speculative", type=int, default=0, metavar="K",
                      help="verify up to K draft tokens per NPU step")
  parser.add_argument("--draft", default="ngram",
                      help="'ngram' or 'transformers:<model id>' (CPU draft)")
  parser.add_argument("--fused", default=None, metavar="ELF",
                      help="use the fused assembly decode kernel (gemma_decode.elf)")
  args = parser.parse_args()

  with open(os.path.join(args.weights, "config.json")) as f:
    cfg = json.load(f)
  prompts = [{"text": p, "raw": args.raw} for p in args.prompt]
  if args.prompts_file:
    with open(args.prompts_file) as f:
      for item in json.load(f):
        prompts.append({"text": item, "raw": args.raw} if isinstance(item, str)
                       else {"text": item["text"], "raw": item.get("raw", args.raw)})
  tok = load_tokenizer(args.tokenizer or cfg["tokenizer"])
  filler = ("The quick brown fox jumps over the lazy dog while the sun sets "
            "behind the distant mountains and the river flows quietly. ")
  for isl in args.isl:
    ids = tok.encode(filler * (isl // 8 + 2), add_special_tokens=False).ids
    prompts.append({"text": None, "raw": True, "ids": [2] + ids[:isl - 1],
                    "label": f"isl{isl}"})
  if not prompts:
    sys.exit("no prompts")

  if args.backend == "f2":
    backend = dev.F2Backend(args.slot)
  else:
    from coralnpu_test_utils.sim_backends import mpact_npusim_test_fixture as mp  # pylint: disable=g-import-not-at-top
    backend = dev.SimBackend(dev._run(mp.MpactNpuSimTestFixture.Create(highmem=False)))
  model = runtime.GemmaNative(backend, args.weights, args.kernels,
                              cache_length=args.cache_length, verbose=args.verbose)
  if args.fused:
    from models.host import fused  # pylint: disable=g-import-not-at-top
    model.fused = fused.FusedDecoder(model, args.fused)

  results = []
  stop = () if args.ignore_eos else STOP
  draft_fn = None
  if args.speculative:
    from models.host import draft as D  # pylint: disable=g-import-not-at-top
    draft_fn = D.make_draft(args.draft)
  for item in prompts:
    if item.get("ids"):
      ids = item["ids"]
    else:
      text = item["text"] if item["raw"] else format_chat(item["text"])
      ids = tok.encode(text, add_special_tokens=False).ids
      if not ids or ids[0] != 2:
        ids = [2] + ids  # <bos>
    label = item.get("label") or item["text"]
    if args.speculative:
      generated, steps, prefill = model.speculative_generate(
          ids, args.max_tokens, draft_fn, k=args.speculative, stop=stop)
      decode_tokens = sum(s["emitted"] for s in steps)
      decode_secs = sum(s["seconds"] for s in steps)
      decode = [decode_secs / decode_tokens] * decode_tokens if decode_tokens else [1e-9]
    else:
      generated, steps, prefill = model.generate(ids, args.max_tokens, stop)
      decode = [s["seconds"] for s in steps if s["tokens"] == 1] or [1e-9]
    out_text = tok.decode([t for t in generated if t not in STOP])
    results.append(dict(prompt=label, raw=item["raw"], prompt_ids=ids,
                        generated=generated, text=out_text, steps=steps,
                        isl=len(ids), osl=len(generated),
                        ttft_seconds=prefill, prefill_seconds=prefill,
                        decode_tokens_per_second=len(decode) / sum(decode),
                        total_seconds=prefill + sum(decode)))
    r = results[-1]
    if args.speculative and steps:
      acc = sum(s["accepted"] for s in steps) / max(1, sum(s["drafts"] for s in steps))
      r["speculative"] = dict(k=args.speculative, draft=args.draft, steps=len(steps),
                              acceptance=acc,
                              tokens_per_verify=sum(s["emitted"] for s in steps) / len(steps))
      print(f"  speculative: {len(steps)} verify steps, acceptance {acc:.2f}, "
            f"{r['speculative']['tokens_per_verify']:.2f} tokens per step")
    print(f"> {label!r}  ISL={r['isl']} OSL={r['osl']}\n{out_text!r}\n"
          f"  TTFT {prefill:.2f}s  decode {r['decode_tokens_per_second']:.4f} tok/s  "
          f"({1 / r['decode_tokens_per_second']:.1f} s/token)  total {r['total_seconds']:.1f}s")
  if args.verbose:
    print(model.profile_report())
  record = dict(model=cfg["model"], source=("native-spec" if args.speculative else "native-fused" if args.fused else "native"), device=args.backend,
                simulator=args.backend, act="int8", cache_length=args.cache_length,
                results=results)
  if args.out:
    with open(args.out, "w") as f:
      json.dump(record, f, indent=1)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
  main()
