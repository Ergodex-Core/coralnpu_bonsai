#!/bin/bash
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

# Builds the Verilator and ArcSim simulators and compares their speed on this
# host (e.g. an EC2 x86 instance). Run from the repository root:
#
#   utils/arcsim/run_perf.sh [--runs N] [--cpu N] [--cores "rvv_core_mini_axi ..."]
#
# Uses ARCSIM_HOME if set (e.g. /usr/lib/ergodex for the release package),
# otherwise the bundle in ~/.cache/arcsim. Results go to perf-results/ as
# Markdown and JSON. Never uses sudo: missing prerequisites are reported with
# the command to install them.

set -euo pipefail

runs=3
cpu=""
cores="core_mini_axi rvv_core_mini_axi vme_core_mini_axi"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs) runs="$2"; shift 2 ;;
    --cpu) cpu="$2"; shift 2 ;;
    --cores) cores="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

missing=()
for tool in clang clang++ java unzip git python3 nm taskset; do
  command -v "${tool}" >/dev/null || missing+=("${tool}")
done
# Verilator's FST tracing needs lz4 headers.
[[ -f /usr/include/lz4.h ]] || missing+=("liblz4-dev")
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Missing tools: ${missing[*]}" >&2
  echo "Install them with (Ubuntu 24.04):" >&2
  echo "  sudo apt-get install -y clang-19 lld-19 openjdk-21-jdk-headless unzip zip libelf-dev srecord xxd \\" >&2
  echo "    libzstd-dev zlib1g-dev libncurses-dev liblz4-dev && sudo ln -sf /usr/bin/clang-19 /usr/local/bin/clang \\" >&2
  echo "    && sudo ln -sf /usr/bin/clang++-19 /usr/local/bin/clang++" >&2
  exit 1
fi

bazel="$(command -v bazel || command -v bazelisk || true)"
if [[ -z "${bazel}" ]]; then
  mkdir -p "${HOME}/.local/bin"
  curl -fsSL -o "${HOME}/.local/bin/bazelisk" \
    https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-amd64
  chmod +x "${HOME}/.local/bin/bazelisk"
  bazel="${HOME}/.local/bin/bazelisk"
fi

out="perf-results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${out}"
echo "ArcSim toolchain: ${ARCSIM_HOME:-~/.cache/arcsim bundle}" | tee "${out}/README.md"
echo "Host: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs), $(nproc) CPUs" | tee -a "${out}/README.md"

perf_targets=()
for core in ${cores}; do perf_targets+=("//tests/arcsim_sim:${core}_perf"); done

# Build timing is part of the comparison: ArcSim model compile vs Verilator.
start=$(date +%s)
if ! "${bazel}" build --config=arcsim --noshow_progress "${perf_targets[@]}" >"${out}/build.log" 2>&1; then
  # Show the first diagnostics, not just the tail: MLIR errors are followed by
  # long note chains.
  grep -m 10 -B 2 -A 6 -E "error:|ERROR:" "${out}/build.log" >&2 || tail -40 "${out}/build.log" >&2
  echo "Build failed; full log: ${out}/build.log" >&2
  exit 1
fi
echo "Build (both simulators, all inputs): $(( $(date +%s) - start ))s" | tee -a "${out}/README.md"

cpu_args=()
[[ -n "${cpu}" ]] && cpu_args=(--cpu="${cpu}")
for core in ${cores}; do
  "${bazel}" run --config=arcsim --noshow_progress "//tests/arcsim_sim:${core}_perf" -- \
    --runs="${runs}" "${cpu_args[@]}" --json="${PWD}/${out}/${core}.json" \
    | tee "${out}/${core}.log"
  sed -n '/^### /,$p' "${out}/${core}.log" >> "${out}/README.md"
done

echo
echo "Results: ${out}/README.md"
