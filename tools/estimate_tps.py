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
"""Estimates tokens/s on CoralNPU silicon from an MPACT run.

  CORALNPU_PROFILE_LOG=prof.log CORALNPU_SIMULATOR=mpact chat_f2 ... --out run.json
  tools/estimate_tps.py run.json --meta artifacts/gemma3-1b/gemma3-1b.json

MPACT is a functional simulator: its cycle count is retired instructions,
not pipeline or memory stalls, so the compute bound below is optimistic. The
memory bound assumes every parameter byte is streamed once per token. The
reported estimate is the slower of the two plus a per-dispatch launch cost.
"""

import argparse
import json


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("record", help="chat_f2.py JSON with cycles per step")
  parser.add_argument("--meta", help="<model>.json from export_gemma_f2 "
                      "(parameter bytes for the memory bound)")
  parser.add_argument("--freq-ghz", type=float, default=1.0)
  parser.add_argument("--bandwidth-gbs", type=float, nargs="+",
                      default=[2.0, 8.0, 16.0],
                      help="candidate weight-stream bandwidths in GB/s")
  parser.add_argument("--launch-ns", type=float, default=800.0,
                      help="per-dispatch launch overhead (roofline T_launch)")
  parser.add_argument("--profile-log", default=None,
                      help="raw CORALNPU_PROFILE_LOG to total instead")
  args = parser.parse_args()

  with open(args.record) as f:
    record = json.load(f)
  per_token = [r["decode_cycles_per_token"] for r in record["results"]
               if "decode_cycles_per_token" in r]
  dispatches = [r["decode_dispatches_per_token"] for r in record["results"]
                if "decode_dispatches_per_token" in r]
  if not per_token:
    raise SystemExit("record has no cycle data; run chat_f2 with "
                     "CORALNPU_PROFILE_LOG set on the mpact simulator")
  cycles = sum(per_token) / len(per_token)
  ndisp = sum(dispatches) / len(dispatches)
  param_bytes = None
  if args.meta:
    with open(args.meta) as f:
      param_bytes = json.load(f)["param_bytes"]

  f_hz = args.freq_ghz * 1e9
  compute_s = cycles / f_hz
  launch_s = ndisp * args.launch_ns * 1e-9
  print(f"model: {record.get('model')}  simulator: {record.get('simulator')}")
  print(f"decode: {cycles:,.0f} MPACT cycles/token over {ndisp:.0f} dispatches")
  print(f"@ {args.freq_ghz:g} GHz: compute bound {1 / compute_s:,.2f} tok/s "
        f"({compute_s * 1e3:.1f} ms), launch overhead {launch_s * 1e3:.2f} ms")
  if param_bytes:
    print(f"parameters streamed per token: {param_bytes / 2**20:.1f} MiB")
    print("bandwidth  memory-bound  combined-estimate")
    for bw in args.bandwidth_gbs:
      mem_s = param_bytes / (bw * 1e9)
      total = max(compute_s, mem_s) + launch_s
      print(f"{bw:6.1f} GB/s  {1 / mem_s:8.2f} tok/s  {1 / total:8.2f} tok/s")
  else:
    print(f"estimate (no memory bound): {1 / (compute_s + launch_s):,.2f} tok/s")
  print("F2 image (25 MHz) for comparison: "
        f"{1 / (cycles / 25e6 + launch_s):,.3f} tok/s compute bound")


if __name__ == "__main__":
  main()
