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
"""Greedy-decodes a prompt with the NumPy reference (device arithmetic) on the
CPU: python3 -m models.tests.cpu_reference_run --weights DIR --record run.json
[--max-tokens 8]. Prints the generated ids next to the record's."""

import argparse
import json
import os
import time

import numpy as np

from models.tests import reference as R


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--weights", required=True)
  parser.add_argument("--record", required=True, help="chat_f2/run.py JSON with prompt_ids")
  parser.add_argument("--max-tokens", type=int, default=8)
  args = parser.parse_args()
  with open(os.path.join(args.weights, "config.json")) as f:
    cfg = json.load(f)
  rec = json.load(open(args.record))["results"][0]
  ids = rec["prompt_ids"]
  a = dict(np.load(os.path.join(args.weights, "weights.npz")))
  ref = R.ModelRef(dict(cfg, cache_length=128), a)
  t0 = time.time()
  (idx, _), _ = ref.step(ids)
  gen = [int(idx[0])]
  print(f"prefill {len(ids)} tokens in {time.time() - t0:.0f}s; first token {gen[0]}", flush=True)
  while len(gen) < args.max_tokens and gen[-1] not in (0, 1, 106):
    t0 = time.time()
    (idx, _), _ = ref.step([gen[-1]])
    gen.append(int(idx[0]))
    print(f"  token {len(gen)}: {gen[-1]} ({time.time() - t0:.0f}s)", flush=True)
  print("reference:", gen)
  print("device:   ", rec["generated"][:len(gen)])
  print("match:", gen == rec["generated"][:len(gen)])


if __name__ == "__main__":
  main()
