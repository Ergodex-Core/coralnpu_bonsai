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
"""Streams 1 MiB from DDR and HBM with vector loads/stores of several widths
and prints bytes per core cycle: python3 -m models.tests.membench --kernels DIR"""

import argparse

import numpy as np

from models.host import device as dev


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--kernels", required=True)
  parser.add_argument("--slot", type=int, default=0)
  parser.add_argument("--mib", type=int, default=1)
  args = parser.parse_args()
  be = dev.F2Backend(args.slot)
  img = dev.KernelImage(f"{args.kernels}/membench.elf")
  n = args.mib << 20
  try:
    for region, (base, _) in (("DDR", be.ddr), ("HBM", be.hbm)):
      base += 0x100000
      be.write_array(base, np.arange(n, dtype=np.uint8))
      for chunk in (16, 32, 64, 128):
        r = be.launch(img, n=n, in0=base, u0=chunk)
        print(f"{region} read  {chunk:3d}B: {r.cycles:9d} cycles  {n / r.cycles:5.2f} B/cycle  "
              f"{n / (r.cycles / 25e6) / 1e6:7.1f} MB/s @25MHz  {r.cycles / (n / chunk):6.1f} cycles/load")
      for chunk in (16, 128):
        r = be.launch(img, n=n, out0=base, u0=chunk, flags=1)
        print(f"{region} write {chunk:3d}B: {r.cycles:9d} cycles  {n / r.cycles:5.2f} B/cycle  "
              f"{n / (r.cycles / 25e6) / 1e6:7.1f} MB/s @25MHz  {r.cycles / (n / chunk):6.1f} cycles/store")
  finally:
    be.close()


if __name__ == "__main__":
  main()
