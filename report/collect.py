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
"""Builds results.tex from report/results/*.json (run.py and mlx_reference.py
records). Re-run after copying new result files, then build report.tex."""

import datetime
import glob
import json
import os
import platform
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_NAMES = {"functiongemma-270m": "FunctionGemma 270M",
               "gemma3-1b": "Gemma 3 1B", "gemma3-270m": "Gemma 3 270M"}


def esc(s):
  return (str(s).replace("\\", "\\textbackslash{}").replace("_", "\\_")
          .replace("%", "\\%").replace("&", "\\&").replace("#", "\\#")
          .replace("{", "\\{").replace("}", "\\}").replace("^", "\\^{}")
          .replace("~", "\\~{}"))


def mac_info():
  try:
    chip = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    mem = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) // 2**30
    return f"{chip}, {mem} GiB"
  except Exception:  # pylint: disable=broad-except
    return platform.platform()


def load_all():
  fpga, mlx = [], []
  for path in sorted(glob.glob(os.path.join(HERE, "results", "*.json"))):
    with open(path) as f:
      d = json.load(f)
    for r in d.get("results", []):
      r = dict(r)
      r["model"] = d.get("model") or os.path.basename(path).split("_")[1]
      r["file"] = os.path.basename(path)
      r["path"] = {"native": "per-op", "native-fused": "fused",
                   "native-spec": "speculative"}.get(d.get("source"), d.get("source", ""))
      (mlx if d.get("source") == "mlx" else fpga).append(r)
  return fpga, mlx


def model_key(name):
  n = name.lower()
  if "functiongemma" in n:
    return "functiongemma-270m"
  if "1b" in n:
    return "gemma3-1b"
  return "gemma3-270m"


def main():
  fpga, mlx = load_all()
  out = []
  w = out.append
  w(f"\\newcommand{{\\reportdate}}{{{datetime.date.today().isoformat()}}}")
  w(f"\\newcommand{{\\macinfo}}{{{esc(mac_info())}}}")

  # Correctness: chat prompt, FPGA vs MLX, same prompt text.
  w("\\newcommand{\\correctnesstable}{%")
  w("\\begin{tabular}{lp{3.6cm}p{4.6cm}l}\\toprule")
  w("Model & Backend & Generated text & Tokens match MLX\\\\\\midrule")
  for mk in ("functiongemma-270m", "gemma3-1b"):
    ref = [r for r in mlx if model_key(r["model"]) == mk and r.get("prompt") and "capital" in str(r["prompt"])]
    dev = [r for r in fpga if model_key(r["model"]) == mk and r.get("prompt") and "capital" in str(r["prompt"])]
    for r in ref:
      w(f"{MODEL_NAMES[mk]} & MLX {esc(r['model'].split('/')[-1])} & {esc(r['text'])} & reference\\\\")
    for r in sorted(dev, key=lambda r: r["path"]):
      match = "yes" if ref and r["generated"] == ref[0]["generated"] else ("no" if ref else "n/a")
      w(f"{MODEL_NAMES[mk]} & FPGA ({esc(r['path'])} kernels) & {esc(r['text'])} & {match}\\\\")
    if not dev:
      w(f"{MODEL_NAMES[mk]} & FPGA (F2, native kernels) & \\emph{{run in progress}} & --\\\\")
  w("\\bottomrule\\end{tabular}}")

  # FPGA timing table.
  w("\\newcommand{\\fpgatable}{%")
  w("\\begin{tabular}{lllrrrrr}\\toprule")
  w("Model & Kernels & Prompt & ISL & OSL & TTFT (s) & s/token & tok/s\\\\\\midrule")
  for mk in ("functiongemma-270m", "gemma3-1b"):
    rows = [r for r in fpga if model_key(r["model"]) == mk]
    if not rows:
      w(f"{MODEL_NAMES[mk]} & & \\emph{{runs in progress}} & & & & & \\\\")
    for r in sorted(rows, key=lambda r: (r["path"], r["isl"], r["osl"])):
      tps = r["decode_tokens_per_second"]
      label = r["prompt"] if isinstance(r["prompt"], str) else "synthetic"
      label = "chat" if "capital" in label else label
      w(f"{MODEL_NAMES[mk]} & {esc(r['path'])} & {esc(label)} & {r['isl']} & {r['osl']} & "
        f"{r['ttft_seconds']:.1f} & {1 / tps:.1f} & {tps:.4f}\\\\")
  w("\\bottomrule\\end{tabular}}")

  # MLX timing table.
  w("\\newcommand{\\mlxtable}{%")
  w("\\begin{tabular}{llrrrr}\\toprule")
  w("Model & MLX checkpoint & ISL & OSL & TTFT (s) & tok/s\\\\\\midrule")
  for r in mlx:
    w(f"{MODEL_NAMES[model_key(r['model'])]} & {esc(r['model'].split('/')[-1])} & "
      f"{len(r['prompt_ids'])} & {len(r['generated'])} & {r['prefill_seconds']:.3f} & "
      f"{r['decode_tokens_per_second']:.1f}\\\\")
  w("\\bottomrule\\end{tabular}}")

  # Per-kernel profile from the most detailed FPGA chat run, if present.
  w("\\newcommand{\\profilenote}{}")
  with open(os.path.join(HERE, "results.tex"), "w") as f:
    f.write("\n".join(out) + "\n")
  print(f"results.tex: {len(fpga)} FPGA rows, {len(mlx)} MLX rows")


if __name__ == "__main__":
  main()
