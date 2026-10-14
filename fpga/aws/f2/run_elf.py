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
"""Loads and runs a bare-metal RV32 ELF on the CoralNPU F2 image.

  fpga/aws/f2/run_elf.py bazel-bin/tests/.../hello.elf [--slot 0] [--timeout 60]

Flow: check the expected image is loaded, wait for DDR/HBM, hold the core in
reset, write every PT_LOAD segment (TCM over BAR0, DDR/HBM over BAR4) and
verify it, seed `_ret` with the startup sentinel, set the start PC, release
reset, poll for halt (a fault fails), then read `_ret` (0 means `main`
returned 0). Prints a JSON result. Exit code 0 only on `_ret == 0`.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import f2_device as f2  # pylint: disable=g-import-not-at-top,wrong-import-position


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("elf")
  parser.add_argument("--slot", type=int, default=0)
  parser.add_argument("--timeout", type=float, default=60.0)
  parser.add_argument("--expected-agfi", default=f2.AGFI)
  parser.add_argument("--no-verify", action="store_true",
                      help="skip reading segments back after loading")
  parser.add_argument("--skip-image-check", action="store_true")
  parser.add_argument("--dump", action="append", default=[],
                      metavar="SYMBOL[:BYTES]",
                      help="hex-dump a symbol after the run")
  parser.add_argument("--json", default=None, help="write the result here")
  args = parser.parse_args()

  result = dict(elf=os.path.abspath(args.elf), slot=args.slot, status="failed")
  image = f2.ElfImage(args.elf)
  result["entry"] = image.entry
  result["segments"] = [{k: v for k, v in s.items() if k != "data"}
                        for s in image.segments]
  if not args.skip_image_check:
    f2.require_image(args.slot, args.expected_agfi)
  device = f2.F2Device(args.slot)
  try:
    result["vled"] = device.wait_memory_ready()
    device.hold_reset()
    t0 = time.monotonic()
    for seg in image.segments:
      device.write_bytes(seg["address"], seg["data"])
      if not args.no_verify and device.read_bytes(seg["address"], seg["size"]) != seg["data"]:
        raise AssertionError(f"readback mismatch at {seg['address']:#x}")
    result["load_seconds"] = time.monotonic() - t0
    ret_addr = image.symbols.get("_ret", (None, 0))[0]
    if ret_addr is not None:
      device.write32(ret_addr, f2.SENTINEL)
    device.start(image.entry)
    t0 = time.monotonic()
    result["core_status"] = device.wait_halt(args.timeout)
    result["run_seconds"] = time.monotonic() - t0
    if ret_addr is not None:
      result["ret"] = device.read32(ret_addr)
    for spec in args.dump:
      name, _, size = spec.partition(":")
      addr, sym_size = image.symbols[name]
      size = int(size, 0) if size else (sym_size or 4)
      result.setdefault("dumps", {})[name] = device.read_bytes(addr, size).hex()
    ok = result.get("ret", 0) == 0
    result["status"] = "passed" if ok else "failed"
  except Exception as exc:  # pylint: disable=broad-except
    result["error"] = f"{type(exc).__name__}: {exc}"
    ok = False
  finally:
    device.close()
  text = json.dumps(result, indent=1)
  print(text)
  if args.json:
    with open(args.json, "w") as f:
      f.write(text)
  sys.exit(0 if ok else 1)


if __name__ == "__main__":
  main()
