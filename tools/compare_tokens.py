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
"""Compares two generation records (chat_f2.py / mlx_reference.py JSON).

  tools/compare_tokens.py run_f2.json run_mpact.json --exact
  tools/compare_tokens.py run_f2.json ref_mlx_bf16.json

Per prompt it reports whether the generated tokens are identical, the first
divergent step, top-1 agreement over the shared prefix, the mean cosine of the
top-k logit vectors where both runs rank the same tokens, and, when both
outputs parse as JSON (function calls), whether the parsed objects match.
With --exact a non-identical token stream fails (exit 1).
"""

import argparse
import json
import math
import sys


def load(path):
  with open(path) as f:
    record = json.load(f)
  return record, {r["prompt"]: r for r in record["results"]}


def cosine(a, b):
  dot = sum(x * y for x, y in zip(a, b))
  na = math.sqrt(sum(x * x for x in a))
  nb = math.sqrt(sum(x * x for x in b))
  return dot / (na * nb) if na and nb else 0.0


def parse_json_call(text):
  start, end = text.find("{"), text.rfind("}")
  if start < 0 or end <= start:
    return None
  try:
    return json.loads(text[start:end + 1])
  except json.JSONDecodeError:
    return None


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("candidate")
  parser.add_argument("reference")
  parser.add_argument("--exact", action="store_true",
                      help="require identical token streams")
  args = parser.parse_args()
  cand, cand_by = load(args.candidate)
  ref, ref_by = load(args.reference)
  print(f"candidate: {cand.get('source')} {cand.get('device')} "
        f"{cand.get('simulator', '')} {cand.get('model')}")
  print(f"reference: {ref.get('source')} {ref.get('device')} "
        f"{ref.get('simulator', '')} {ref.get('model')}")

  ok = True
  for prompt, c in cand_by.items():
    r = ref_by.get(prompt)
    if r is None:
      print(f"- {prompt!r}: not in reference")
      ok = False
      continue
    if c["prompt_ids"] != r["prompt_ids"]:
      print(f"- {prompt!r}: prompt token ids differ (tokenizers disagree)")
    cg, rg = c["generated"], r["generated"]
    identical = cg == rg
    n = min(len(cg), len(rg))
    first_div = next((i for i in range(n) if cg[i] != rg[i]), None)
    if first_div is None and len(cg) != len(rg):
      first_div = n
    prefix = n if first_div is None else first_div
    cs, rs = c["steps"], r["steps"]
    m = min(len(cs), len(rs))
    agree = sum(1 for i in range(m) if cs[i]["token"] == rs[i]["token"])
    coss = [cosine(cs[i]["topk_val"], rs[i]["topk_val"]) for i in range(m)
            if cs[i]["topk_idx"] == rs[i]["topk_idx"]]
    cj, rj = parse_json_call(c["text"]), parse_json_call(r["text"])
    line = (f"- {prompt!r}: identical={identical} first_divergence="
            f"{first_div} top1_agreement={agree}/{m} "
            f"topk_cos={sum(coss) / len(coss):.4f}({len(coss)})"
            if coss else f"- {prompt!r}: identical={identical} "
            f"first_divergence={first_div} top1_agreement={agree}/{m}")
    if cj is not None or rj is not None:
      line += f" json_match={cj == rj}"
    print(line)
    print(f"    candidate: {c['text']!r}\n    reference: {r['text']!r}")
    if args.exact and not identical:
      ok = False
  sys.exit(0 if ok else 1)


if __name__ == "__main__":
  main()
