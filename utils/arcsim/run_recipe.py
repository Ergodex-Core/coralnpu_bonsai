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
"""Replays an ArcSim developer bundle's lowering recipe on a new design.

The developer bundles (e.g. arcsim-coral-matrix-baseline-20261013) record
their full pipeline in recipe.json. For the CoralNPU RVV/VME cores the plain
`arcsim --emit-llvm` lowering stops with "loop splitting did not eliminate
all loops"; the recipe's prestate and state-inference stages resolve those
false loops before the final lowering.

This replays the recipe stages from `prestate` through `lower` (the stage
that writes model.ll and state.json), starting from the caller's imported
HW MLIR instead of the bundle's own frontend output. LLVM optimization and
code generation are left to the caller.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

FIRST_STAGE = "prestate"
LAST_STAGE = "lower"


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, help="bundle root (ARCSIM_HOME)")
    parser.add_argument("--input", required=True, help="imported HW MLIR")
    parser.add_argument("--model_ll", required=True, help="output LLVM IR")
    parser.add_argument("--state", required=True, help="output state.json")
    parser.add_argument("--workdir", required=True, help="scratch directory")
    args = parser.parse_args(argv)

    with open(os.path.join(args.home, "recipe.json")) as f:
        recipe = json.load(f)
    names = [s["name"] for s in recipe["stages"]]
    if FIRST_STAGE not in names or LAST_STAGE not in names:
        parser.error("recipe.json lacks the {}..{} stages".format(
            FIRST_STAGE, LAST_STAGE))
    stages = recipe["stages"][names.index(FIRST_STAGE):names.index(LAST_STAGE) + 1]

    out = os.path.abspath(args.workdir)
    os.makedirs(out, exist_ok=True)
    shutil.copyfile(args.input, os.path.join(out, "imported.mlir"))
    root = os.path.abspath(args.home)

    def expand(value):
        return value.replace("{ROOT}", root).replace("{OUT}", out)

    # Same environment hygiene as the bundle's build.py.
    base_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ARC_", "CORAL_", "LLVM_PROFILE_"))
    }
    for stage in stages:
        command = [expand(a) for a in stage["command"]]
        env = dict(base_env, **stage.get("environment", {}))
        started = time.monotonic()
        log_path = os.path.join(out, stage["name"] + ".log")
        with open(log_path, "w") as log:
            result = subprocess.run(command, cwd=out, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, check=False)
        print("[recipe] {} exit={} {:.1f}s".format(stage["name"], result.returncode,
                                                   time.monotonic() - started),
              flush=True)
        if result.returncode != 0:
            with open(log_path) as log:
                sys.stderr.write(log.read()[-6000:])
            return result.returncode

    for name, dest in (("model.ll", args.model_ll), ("state.json", args.state)):
        src = os.path.join(out, name)
        if not os.path.exists(src):
            sys.stderr.write("recipe did not produce {}\n".format(name))
            return 1
        shutil.copyfile(src, dest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
