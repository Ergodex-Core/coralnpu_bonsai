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
"""Compares Verilator and ArcSim simulation speed on the same ELFs.

Both simulators print a summary line at exit:

  VERILATOR PASS model=... cycles=N wall_s=S cycles_per_s=R
  ARCSIM PASS model=... cycles=N load_cycles=... evals=... wall_s=S ...

Each ELF runs --runs times per simulator, alternating simulators (ABAB) to
spread host noise evenly. The table reports median wall time and simulated
cycles per second, plus ArcSim's speedup. Results are printed as Markdown,
appended to $GITHUB_STEP_SUMMARY when set, and optionally written as JSON.

Pin to an idle core with --cpu for stable numbers.
"""

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import sys

_LINE = re.compile(r"^(VERILATOR|ARCSIM) (PASS|FAIL) .*?\bcycles=(\d+)\b.*?"
                   r"\bwall_s=([0-9.]+)", re.MULTILINE)


def _cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def run_once(sim, elf, cpu, timeout, extra_args):
    cmd = [sim, "--binary=" + elf] + extra_args
    if cpu is not None:
        cmd = ["taskset", "-c", str(cpu)] + cmd
    proc = subprocess.run(cmd,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,
                          text=True,
                          timeout=timeout,
                          check=False)
    matches = _LINE.findall(proc.stdout)
    if not matches:
        tail = "\n".join(proc.stdout.splitlines()[-20:])
        raise RuntimeError("{} printed no summary line (rc={}):\n{}".format(
            os.path.basename(sim), proc.returncode, tail))
    _, status, cycles, wall = matches[-1]
    return dict(passed=status == "PASS" and proc.returncode == 0,
                cycles=int(cycles),
                wall_s=float(wall))


def summarize(samples):
    walls = [s["wall_s"] for s in samples]
    cycles = samples[-1]["cycles"]
    median_wall = statistics.median(walls)
    return dict(passed=all(s["passed"] for s in samples),
                cycles=cycles,
                wall_s=median_wall,
                cycles_per_s=cycles / median_wall if median_wall else 0.0,
                samples=samples)


def markdown(results, header):
    out = [header, ""]
    out.append("| Program | Verilator cycles | Verilator wall (s) | "
               "Verilator cycles/s | ArcSim cycles | ArcSim wall (s) | "
               "ArcSim cycles/s | ArcSim speedup |")
    out.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in results:
        v, a = r["verilator"], r["arcsim"]
        speedup = None
        if v and a and v["passed"] and a["passed"] and v["cycles_per_s"]:
            speedup = a["cycles_per_s"] / v["cycles_per_s"]

        def cell(x, key, fmt):
            if not x:
                return "n/a"
            text = fmt.format(x[key])
            return text if x["passed"] else text + " (FAIL)"

        out.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["program"],
            cell(v, "cycles", "{:,}"),
            cell(v, "wall_s", "{:.2f}"),
            cell(v, "cycles_per_s", "{:,.0f}"),
            cell(a, "cycles", "{:,}"),
            cell(a, "wall_s", "{:.2f}"),
            cell(a, "cycles_per_s", "{:,.0f}"),
            "{:.1f}x".format(speedup) if speedup else "n/a",
        ))
    out.append("")
    out.append("Wall time is the median of the runs and includes model "
               "construction and ELF loading. Cycle counts differ slightly "
               "because the AXI testbench latencies differ.")
    return "\n".join(out) + "\n"


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arcsim", required=True, help="ArcSim *_sim binary")
    parser.add_argument("--verilator",
                        default="none",
                        help="Verilator *_sim binary, or 'none'")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cpu", type=int, help="pin runs to this CPU")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--label", default="", help="name for the table")
    parser.add_argument("--json", help="write results here")
    parser.add_argument("--sim_arg",
                        action="append",
                        default=[],
                        help="extra argument for both simulators")
    parser.add_argument("files", nargs="+", help="ELFs (non-.elf ignored)")
    args = parser.parse_args(argv)

    elfs = [f for f in args.files if f.endswith(".elf")]
    if not elfs:
        parser.error("no .elf files given")
    sims = {"arcsim": args.arcsim}
    if args.verilator != "none":
        sims["verilator"] = args.verilator

    results = []
    failed = False
    for elf in elfs:
        program = os.path.basename(elf)[:-len(".elf")]
        samples = {name: [] for name in sims}
        for i in range(args.runs):
            order = list(sims) if i % 2 == 0 else list(reversed(list(sims)))
            for name in order:
                sample = run_once(sims[name], elf, args.cpu, args.timeout,
                                  args.sim_arg)
                samples[name].append(sample)
                print("{} run {} {}: cycles={} wall_s={:.3f} {}".format(
                    program, i + 1, name, sample["cycles"], sample["wall_s"],
                    "PASS" if sample["passed"] else "FAIL"),
                      flush=True)
        entry = dict(program=program, verilator=None, arcsim=None)
        for name in sims:
            entry[name] = summarize(samples[name])
            failed |= not entry[name]["passed"]
        results.append(entry)

    header = "### Verilator vs ArcSim{} on {} ({} runs each)".format(
        " — " + args.label if args.label else "", _cpu_model(), args.runs)
    table = markdown(results, header)
    print()
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(table + "\n")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(dict(label=args.label,
                           cpu=_cpu_model(),
                           runs=args.runs,
                           results=results),
                      f,
                      indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
