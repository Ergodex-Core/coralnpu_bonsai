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
  parser.add_argument("--max-tokens", type=int, default=16)
  parser.add_argument("--cache-length", type=int, default=128)
  parser.add_argument("--out", default=None)
  parser.add_argument("--verbose", action="store_true")
  args = parser.parse_args()

  with open(os.path.join(args.weights, "config.json")) as f:
    cfg = json.load(f)
  prompts = [{"text": p, "raw": args.raw} for p in args.prompt]
  if args.prompts_file:
    with open(args.prompts_file) as f:
      for item in json.load(f):
        prompts.append({"text": item, "raw": args.raw} if isinstance(item, str)
                       else {"text": item["text"], "raw": item.get("raw", args.raw)})
  if not prompts:
    sys.exit("no prompts")
  tok = load_tokenizer(args.tokenizer or cfg["tokenizer"])

  if args.backend == "f2":
    backend = dev.F2Backend(args.slot)
  else:
    from coralnpu_test_utils.sim_backends import mpact_npusim_test_fixture as mp  # pylint: disable=g-import-not-at-top
    backend = dev.SimBackend(dev._run(mp.MpactNpuSimTestFixture.Create(highmem=False)))
  model = runtime.GemmaNative(backend, args.weights, args.kernels,
                              cache_length=args.cache_length, verbose=args.verbose)

  results = []
  for item in prompts:
    text = item["text"] if item["raw"] else format_chat(item["text"])
    ids = tok.encode(text, add_special_tokens=False).ids
    if not ids or ids[0] != 2:
      ids = [2] + ids  # <bos>
    generated, steps, prefill = model.generate(ids, args.max_tokens, STOP)
    decode = [s["seconds"] for s in steps if s["tokens"] == 1][1:] or [1e-9]
    out_text = tok.decode([t for t in generated if t not in STOP])
    results.append(dict(prompt=item["text"], raw=item["raw"], prompt_ids=ids,
                        generated=generated, text=out_text, steps=steps,
                        prefill_seconds=prefill,
                        decode_tokens_per_second=len(decode) / sum(decode)))
    print(f"> {item['text']!r}\n{out_text!r}\n  prefill {prefill:.2f}s, "
          f"decode {results[-1]['decode_tokens_per_second']:.3f} tok/s")
  if args.verbose:
    print(model.profile_report())
  record = dict(model=cfg["model"], source="native", device=args.backend,
                simulator=args.backend, act="int8", cache_length=args.cache_length,
                results=results)
  if args.out:
    with open(args.out, "w") as f:
      json.dump(record, f, indent=1)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
  main()
