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
"""Checks large DDR/HBM uploads and the embed kernel on the real table:
python3 -m models.tests.upload_check --weights DIR --kernels DIR"""

import argparse
import json
import os
import time

import numpy as np

from models.host import device as dev
from models.host import weights as W


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("--weights", required=True)
  parser.add_argument("--kernels", required=True)
  parser.add_argument("--mib", type=int, default=96)
  args = parser.parse_args()
  be = dev.F2Backend(0)
  try:
    n = args.mib << 20
    base = be.ddr[0] + (64 << 20)
    rng = np.random.default_rng(0)
    pattern = rng.integers(0, 256, n, dtype=np.uint8)
    t0 = time.time()
    be.write_array(base, pattern)
    dt = time.time() - t0
    print(f"wrote {args.mib} MiB in {dt:.2f}s ({n / dt / 2**20:.0f} MiB/s)")
    for off in (0, 4096, 1 << 20, 16 << 20, 48 << 20, n - 65536):
      got = be.read_array(base + off, np.uint8, (65536,))
      ok = np.array_equal(got, pattern[off:off + 65536])
      first = int(np.argmax(got != pattern[off:off + 65536])) if not ok else -1
      print(f"  offset {off:>10d}: {'ok' if ok else f'MISMATCH at +{first}'}")
    # Same through HBM.
    hb = be.hbm[0] + (64 << 20)
    be.write_array(hb, pattern[:1 << 20])
    print("  hbm 1 MiB:", "ok" if np.array_equal(be.read_array(hb, np.uint8, (1 << 20,)), pattern[:1 << 20]) else "MISMATCH")

    with open(os.path.join(args.weights, "config.json")) as f:
      cfg = json.load(f)
    a = np.load(os.path.join(args.weights, "weights.npz"))
    tiles, scales = a["embed.tiles"], a["embed.scales"]
    D, V = cfg["dim"], cfg["vocab"]
    t_addr = be.ddr[0] + (256 << 20)
    s_addr = t_addr + tiles.nbytes + 4096
    be.write_array(t_addr, tiles)
    be.write_array(s_addr, scales)
    print("tiles readback:", "ok" if np.array_equal(be.read_array(t_addr + 5120 * 62, np.uint8, (5120 * 2,)), tiles[5120 * 62:5120 * 64]) else "MISMATCH")
    tokens = np.array([2, 1000, 2000], np.uint32)
    tok_addr = be.hbm[0] + (70 << 20)
    out = tok_addr + 4096
    be.write_array(tok_addr, np.pad(tokens, (0, 13)))
    img = dev.KernelImage(f"{args.kernels}/embed.elf")
    r = be.launch(img, m=3, k=D, in0=tok_addr, in1=t_addr, in2=s_addr, out0=out, f0=float(np.sqrt(D)))
    got = be.read_array(out, np.float32, (3, D))
    want = W.unpack_tiles(tiles, scales, V, D)[tokens] * np.float32(np.sqrt(D))
    print(f"embed kernel on real table: max|err| {np.abs(got - want).max():.3e} |ref| {np.abs(want).max():.3f} cycles {r.cycles}")
    print("  got[1,:6] ", got[1, :6]); print("  want[1,:6]", want[1, :6])
  finally:
    be.close()


if __name__ == "__main__":
  main()
