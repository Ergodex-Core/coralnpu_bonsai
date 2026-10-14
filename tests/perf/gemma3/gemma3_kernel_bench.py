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
"""Gemma 3 kernel performance bench for CoralNPU (Verilator and/or ArcSim).

Runs the RVV Gemma kernels in tests/cocotb/rvv/ml_ops/gemma_kernels with the
layer shapes of a Gemma 3 270M single-token decode step on the highmem RVV
core. Each runner times its kernel with mcycle and stores the delta in the
`cycle_count` symbol; the simulators set the shape symbols (--set) and print
`cycle_count` (--dump), so ELF loading is excluded from the measurement.

Kernel cycles are converted to latency and throughput at each target clock
(default 25 MHz, a typical FPGA build, and 1 GHz, an ASIC target), then summed
into per-layer and per-token decode estimates:

  * Matmuls larger than the runners' buffers (e.g. 640x2048 gate/up) are run
    as tiles that fit and multiplied by the tile count. Tile-level partial-sum
    accumulation is not modelled.
  * Inputs keep their ELF initial values (zeros, or the testbench's 0xa5 fill).
    The kernels' control flow is shape-driven, so cycle counts do not depend
    on the data except for floating-point corner cases.
  * Cycle counts assume single-cycle-latency testbench memory for EXTMEM/DDR
    (see tests/systemc/Xbar.h); real DDR latency will add to memory-bound
    kernels.
"""

import argparse
import collections
import json
import os
import platform
import re
import subprocess
import sys
import time

# Gemma 3 270M (google/gemma-3-270m config).
GEMMA3_270M = dict(
    hidden=640,
    intermediate=2048,
    q_heads=4,
    kv_heads=1,
    head_dim=256,
    layers=18,
    vocab=262144,
)

# Runner buffer limits (see the *_runner.cc files).
#   f32/bf16 matmul: M<=256 K<=640 N<=1024; int8 matmul: M<=128 K<=640 N<=512.


def _matmul(elf, m, k, n):
    return dict(elf=elf,
                set=dict(active_m=m, active_k=k, active_n=n),
                shape="{}x{}x{}".format(m, k, n),
                macs=m * k * n,
                elements=m * n)


def _rms(elf, seq, hidden):
    return dict(elf=elf,
                set=dict(active_seq_len=seq,
                         active_hidden_size=hidden,
                         active_epsilon="f32:1e-6"),
                shape="{}x{}".format(seq, hidden),
                macs=0,
                elements=seq * hidden)


def _eltwise(elf, n):
    return dict(elf=elf,
                set=dict(active_elements=n),
                shape=str(n),
                macs=0,
                elements=n)


def _flash(elf, q_heads, kv_heads, kv_len, dim):
    return dict(elf=elf,
                set=dict(active_num_heads=q_heads,
                         active_num_kv_heads=kv_heads,
                         active_seq_len=kv_len,
                         active_q_seq_len=1,
                         active_kv_seq_len=kv_len,
                         active_dim=dim),
                shape="h{}/kv{} L={} d={}".format(q_heads, kv_heads, kv_len,
                                                  dim),
                # QK^T and PV for one query token.
                macs=2 * q_heads * kv_len * dim,
                elements=q_heads * dim)


def catalog(cfg, kv_len):
    """Returns {precision: [(label, kernel, count_per_layer)]}, lm_head."""
    h, f = cfg["hidden"], cfg["intermediate"]
    qh, kvh, d = cfg["q_heads"], cfg["kv_heads"], cfg["head_dim"]
    assert qh * d == 1024 and h == 640 and f == 2048, "tiling below assumes 270M"
    out = {}
    for prec, mm, rms, add, gelu, flash in [
        ("fp32", "rvv_matmul", "rvv_rms_norm", "rvv_residual_add",
         "rvv_tanh_gelu_mul", "rvv_flashattention_test"),
        ("bf16", "rvv_bf16_matmul", "rvv_bf16_rms_norm",
         "rvv_bf16_residual_add", "rvv_bf16_tanh_gelu_mul",
         "rvv_bf16_flashattention"),
    ]:
        out[prec] = [
            ("q_proj + gate/up tiles (640->1024)", _matmul(mm, 1, h, 1024), 5),
            ("k_proj, v_proj (640->256)", _matmul(mm, 1, h, kvh * d), 2),
            ("o_proj + down tiles (512->640)", _matmul(mm, 1, 512, h), 6),
            ("RMSNorm x4 (640)", _rms(rms, 1, h), 4),
            ("q/k RMSNorm (5x256)", _rms(rms, qh + kvh, d), 1),
            ("residual add x2 (640)", _eltwise(add, h), 2),
            ("GeGLU tanh-gelu*up (2048)", _eltwise(gelu, f), 1),
            ("attention (1 query)", _flash(flash, qh, kvh, kv_len, d), 1),
        ]
    # int8 weights: matmuls only (elementwise stays bf16).
    out["int8 matmul"] = [
        ("q_proj + gate/up tiles (640->512)", _matmul("rvv_int8_matmul", 1, h, 512), 10),
        ("k_proj, v_proj (640->256)", _matmul("rvv_int8_matmul", 1, h, 256), 2),
        ("o_proj + down tiles (512->320)", _matmul("rvv_int8_matmul", 1, 512, 320), 12),
    ]
    lm_head = {
        "fp32": (_matmul("rvv_matmul", 1, h, 1024), cfg["vocab"] // 1024),
        "bf16": (_matmul("rvv_bf16_matmul", 1, h, 1024), cfg["vocab"] // 1024),
    }
    final_norm = {
        "fp32": _rms("rvv_rms_norm", 1, h),
        "bf16": _rms("rvv_bf16_rms_norm", 1, h),
    }
    return out, lm_head, final_norm


_SYMBOL = re.compile(r"^SYMBOL cycle_count=(\d+)$", re.MULTILINE)
_SUMMARY = re.compile(r"^(VERILATOR|ARCSIM) (PASS|FAIL) .*?\bwall_s=([0-9.]+)",
                      re.MULTILINE)


def run_kernel(sim_kind, sim, elf, settings, timeout):
    sets = ",".join("{}={}".format(k, v) for k, v in settings.items())
    cmd = [sim, "--binary=" + elf, "--set=" + sets, "--dump=cycle_count"]
    if sim_kind == "verilator":
        cmd.append("--backdoor_load")
    else:
        cmd.append("--verify_load=false")
    started = time.monotonic()
    proc = subprocess.run(cmd,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,
                          text=True,
                          timeout=timeout,
                          check=False)
    wall = time.monotonic() - started
    cycles = _SYMBOL.findall(proc.stdout)
    summary = _SUMMARY.findall(proc.stdout)
    passed = proc.returncode == 0 and bool(summary) and summary[-1][1] == "PASS"
    if not cycles:
        tail = "\n".join(proc.stdout.splitlines()[-15:])
        raise RuntimeError("{} {} printed no cycle_count (rc={}):\n{}".format(
            sim_kind, os.path.basename(elf), proc.returncode, tail))
    return dict(cycles=int(cycles[-1]), passed=passed, wall_s=wall)


def fmt_time(seconds):
    for unit, scale in (("s", 1), ("ms", 1e-3), ("us", 1e-6), ("ns", 1e-9)):
        if seconds >= scale:
            return "{:.3g} {}".format(seconds / scale, unit)
    return "{:.3g} ns".format(seconds / 1e-9)


def fmt_mhz(mhz):
    return "{:g} GHz".format(mhz / 1000) if mhz >= 1000 else "{:g} MHz".format(mhz)


def main(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verilator", help="Verilator highmem RVV *_sim binary")
    parser.add_argument("--arcsim", help="ArcSim highmem RVV *_sim binary")
    parser.add_argument("--elf",
                        action="append",
                        default=[],
                        help="runner=path.elf (repeatable)")
    parser.add_argument("--freq_mhz",
                        default="25,1000",
                        help="comma-separated clock frequencies in MHz")
    parser.add_argument("--kv_len",
                        type=int,
                        default=256,
                        help="decode context length for attention")
    parser.add_argument("--precision",
                        default="fp32,bf16,int8 matmul",
                        help="comma-separated: fp32, bf16, int8 matmul")
    parser.add_argument("--no_lm_head",
                        action="store_true",
                        help="exclude the 640x262144 LM head from per-token totals")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--json", help="write results here")
    parser.add_argument("--markdown", help="write the report here")
    args = parser.parse_args(argv)

    sims = collections.OrderedDict()
    if args.verilator:
        sims["verilator"] = args.verilator
    if args.arcsim:
        sims["arcsim"] = args.arcsim
    if not sims:
        parser.error("pass --verilator and/or --arcsim")
    elfs = {}
    for item in args.elf:
        name, _, path = item.partition("=")
        if path.endswith(".elf"):
            elfs[name] = path
    freqs = [float(f) for f in args.freq_mhz.split(",")]
    precisions = [p.strip() for p in args.precision.split(",") if p.strip()]

    cfg = GEMMA3_270M
    layer_kernels, lm_head, final_norm = catalog(cfg, args.kv_len)

    # Unique (runner, settings) measurements.
    jobs = collections.OrderedDict()

    def need(kernel):
        key = (kernel["elf"], tuple(sorted(kernel["set"].items())))
        jobs.setdefault(key, kernel)
        return key

    plan = {}
    for prec in precisions:
        if prec not in layer_kernels:
            parser.error("unknown precision " + prec)
        plan[prec] = [(label, need(k), k, n) for label, k, n in layer_kernels[prec]]
        if prec in lm_head:
            need(lm_head[prec][0])
            need(final_norm[prec])

    missing = sorted({k[0] for k in jobs} - set(elfs))
    if missing:
        parser.error("missing --elf for: " + ", ".join(missing))

    results = {}
    for key, kernel in jobs.items():
        results[key] = {}
        for sim_kind, sim in sims.items():
            r = run_kernel(sim_kind, sim, elfs[kernel["elf"]], kernel["set"],
                           args.timeout)
            results[key][sim_kind] = r
            print("{:<26} {:<22} {:<9} cycles={:<10} {} sim_wall={:.1f}s".format(
                kernel["elf"], kernel["shape"], sim_kind, r["cycles"],
                "PASS" if r["passed"] else "FAIL", r["wall_s"]),
                  flush=True)

    ref = "verilator" if "verilator" in sims else "arcsim"

    def cycles_of(key):
        return results[key][ref]["cycles"]

    host = platform.node()
    lines = []
    lines.append("## Gemma 3 270M decode kernels on CoralNPU (RvvCoreMiniHighmemAxi)")
    lines.append("")
    lines.append("Simulators: {}. Cycle counts are mcycle deltas measured by "
                 "the kernels. Estimates use {} cycles. Context length {}. "
                 "Host {}.".format(", ".join(sims), ref, args.kv_len, host))
    lines.append("")

    # Per-kernel table.
    header = ["Runner", "Shape"] + ["{} cycles".format(s) for s in sims]
    if len(sims) == 2:
        header.append("Δ cycles")
    for mhz in freqs:
        header.append("Latency @ {}".format(fmt_mhz(mhz)))
    header += ["MAC/cycle", "Elems/cycle"]
    for mhz in freqs:
        header.append("GMAC/s @ {}".format(fmt_mhz(mhz)))
    header.append("Sim wall (s)")
    lines.append("### Kernels")
    lines.append("")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * 2 + ["---:"] * (len(header) - 2)) + "|")
    kernel_rows = []
    for key, kernel in jobs.items():
        res = results[key]
        cyc = cycles_of(key)
        row = [kernel["elf"], kernel["shape"]]
        for s in sims:
            text = "{:,}".format(res[s]["cycles"])
            row.append(text if res[s]["passed"] else text + " (FAIL)")
        if len(sims) == 2:
            a, b = res["verilator"]["cycles"], res["arcsim"]["cycles"]
            row.append("{:+.2%}".format((b - a) / a) if a else "n/a")
        for mhz in freqs:
            row.append(fmt_time(cyc / (mhz * 1e6)))
        row.append("{:.2f}".format(kernel["macs"] / cyc) if kernel["macs"] else "-")
        row.append("{:.2f}".format(kernel["elements"] / cyc))
        for mhz in freqs:
            row.append("{:.3g}".format(kernel["macs"] / cyc * mhz * 1e6 / 1e9)
                       if kernel["macs"] else "-")
        row.append(" / ".join("{:.1f}".format(res[s]["wall_s"]) for s in sims))
        lines.append("| " + " | ".join(row) + " |")
        kernel_rows.append(dict(runner=kernel["elf"],
                                shape=kernel["shape"],
                                settings=kernel["set"],
                                macs=kernel["macs"],
                                elements=kernel["elements"],
                                results=res))
    lines.append("")

    # Per-layer / per-token estimates.
    estimates = {}
    for prec, items in plan.items():
        lines.append("### Decode estimate: {}".format(prec))
        lines.append("")
        head = ["Op", "Kernel shape", "Count/layer", "Cycles/layer"]
        lines.append("| " + " | ".join(head) + " |")
        lines.append("|---|---|---:|---:|")
        layer = 0
        for label, key, kernel, count in items:
            c = cycles_of(key) * count
            layer += c
            lines.append("| {} | {} | {} | {:,} |".format(label, kernel["shape"],
                                                          count, c))
        lines.append("| **Layer total** | | | **{:,}** |".format(layer))
        lines.append("")
        token = layer * cfg["layers"]
        parts = ["{} layers".format(cfg["layers"])]
        if prec in lm_head:
            token += cycles_of(need(final_norm[prec]))
            parts.append("final norm")
            if not args.no_lm_head:
                tile, n = lm_head[prec]
                token += cycles_of(need(tile)) * n
                parts.append("LM head ({}x {} tiles)".format(n, tile["shape"]))
        est = dict(layer_cycles=layer, token_cycles=token, per_freq={})
        lines.append("| Clock | Layer latency | Token latency ({}) | Tokens/s |".format(
            " + ".join(parts)))
        lines.append("|---|---:|---:|---:|")
        for mhz in freqs:
            hz = mhz * 1e6
            est["per_freq"][str(mhz)] = dict(layer_s=layer / hz,
                                             token_s=token / hz,
                                             tokens_per_s=hz / token)
            lines.append("| {} | {} | {} | {:.3g} |".format(
                fmt_mhz(mhz), fmt_time(layer / hz), fmt_time(token / hz),
                hz / token))
        lines.append("")
        estimates[prec] = est

    lines.append("Notes: matmuls larger than the runner buffers are tiled and "
                 "the tile count multiplies the tile cycles; partial-sum "
                 "accumulation between tiles is not included. int8 rows cover "
                 "matmuls only. Testbench memory has single-cycle latency, so "
                 "memory-bound kernels will be slower with real DDR.")
    report = "\n".join(lines) + "\n"
    print()
    print(report)

    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report + "\n")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(dict(model="gemma-3-270m",
                           config=cfg,
                           kv_len=args.kv_len,
                           freq_mhz=freqs,
                           reference_sim=ref,
                           kernels=kernel_rows,
                           estimates=estimates),
                      f,
                      indent=2)
    failed = any(not r["passed"] for res in results.values() for r in res.values())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
