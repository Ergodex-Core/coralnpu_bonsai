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
"""Tile-load (vtle8) versus vector-load streaming bandwidth on the FPGA:
python3 -m models.tests.tilebench --kernels DIR"""

import argparse

import numpy as np

from models.host import device as dev

MODES = {0: "16 x vle8 (same reg)", 1: "vtle8 tile load (256 B)", 2: "16 x vle8 (16 regs)"}


def main():
  parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  parser.add_argument("--kernels", required=True)
  parser.add_argument("--mib", type=int, default=1)
  args = parser.parse_args()
  be = dev.F2Backend(0)
  img = dev.KernelImage(f"{args.kernels}/tilebench.elf")
  n = args.mib << 20
  try:
    for region, (base, _) in (("DDR", be.ddr), ("HBM", be.hbm)):
      base += 0x100000
      be.write_array(base, np.arange(n, dtype=np.uint8))
      for mode, label in MODES.items():
        r = be.launch(img, n=n, in0=base, flags=mode)
        print(f"{region} {label:24s}: {r.cycles:9d} cycles  {n / r.cycles:5.2f} B/cycle  "
              f"{r.cycles / (n / 256):6.1f} cycles per 256 B")
  finally:
    be.close()


if __name__ == "__main__":
  main()
