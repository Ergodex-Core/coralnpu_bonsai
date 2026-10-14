#!/usr/bin/env bash
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
#
# Runs a model (or bare-metal ELF) on the CoralNPU AWS F2 image
# agfi-075a98d106eb1b6bf. Run this on the F2 instance (e.g. fpga-dev-f2).

set -euo pipefail

AGFI="agfi-075a98d106eb1b6bf"
SHELL_VERSION="0x10212415"
F2_CPU_FEATURES="+m,+f,+zvl128b,+zve32f,+zvtbase,+zvt8e,+zvti8i32mm,+zvtf32f32mm"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPILER_DIR="${CORALNPU_COMPILER_DIR:-${REPO_DIR}/third_party/coralnpu-compiler}"

SLOT="${SLOT:-0}"
LOAD_IMAGE=0
BACKEND="f2"           # f2 | mpact | cpu
PROMPTS=()
PROMPTS_FILE=""
MAX_TOKENS=16
OUT=""
REFERENCE=""
PROFILE_LOG=""
TIMEOUT=300
EXTRA_ARGS=()

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options] <model>

<model> is one of:
  path/to/program.elf          bare-metal RV32 ELF (8 KiB ITCM / 32 KiB DTCM)
  path/to/module.vmfb          compiled IREE module (iree-run-module)
  path/to/module.mlir          StableHLO/TOSA MLIR, compiled for F2 first
  path/to/model.tflite         converted to TOSA MLIR, then compiled
  path/to/artifacts-dir        export_gemma_f2 output (<model>.mlir/.json/_params.npz)
  gemma3-1b | functiongemma-270m | gemma3-270m
                               export, compile, run. gemma3-1b uses the public
                               Q4_0 GGUF gaunernst/gemma-3-1b-it-int4-gguf; the
                               others need HF_TOKEN for the gated checkpoints

Options:
  --slot N              FPGA slot (default ${SLOT})
  --load                load ${AGFI} into the slot first
  --backend B           f2 (default), mpact (simulator golden) or cpu
  --prompt TEXT         chat prompt (repeatable); --prompts-file JSON list
  --max-tokens N        tokens to generate (default ${MAX_TOKENS})
  --out FILE            JSON generation record (chat_f2.py format)
  --reference FILE      compare the record against an MLX/MPACT record
  --profile-log FILE    per-dispatch cycle log (CORALNPU_PROFILE_LOG)
  --timeout S           per-dispatch / ELF timeout (default ${TIMEOUT})
  --                    remaining args go to iree-run-module / run_elf.py

Environment: CORALNPU_COMPILER_DIR, HF_TOKEN, SLOT.
USAGE
  exit 1
}

log() { echo "[fpga_run] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slot) SLOT="$2"; shift 2 ;;
    --load) LOAD_IMAGE=1; shift ;;
    --backend) BACKEND="$2"; shift 2 ;;
    --prompt) PROMPTS+=("$2"); shift 2 ;;
    --prompts-file) PROMPTS_FILE="$2"; shift 2 ;;
    --max-tokens) MAX_TOKENS="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --reference) REFERENCE="$2"; shift 2 ;;
    --profile-log) PROFILE_LOG="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; EXTRA_ARGS=("$@"); break ;;
    -*) die "unknown option $1" ;;
    *) [[ -n "${MODEL:-}" ]] && die "one model only"; MODEL="$1"; shift ;;
  esac
done
[[ -n "${MODEL:-}" ]] || usage

# ---------------------------------------------------------------------------
# Preflight: image, SDK, memory readiness.
# ---------------------------------------------------------------------------
preflight_f2() {
  command -v fpga-describe-local-image >/dev/null ||
    die "AWS FPGA SDK not found; run 'source aws-fpga/sdk_setup.sh'"
  [[ -f /usr/local/lib/libfpga_mgmt.so ]] ||
    die "/usr/local/lib/libfpga_mgmt.so missing (sdk_setup.sh)"
  if (( LOAD_IMAGE )); then
    log "loading ${AGFI} into slot ${SLOT}"
    sudo fpga-load-local-image -S "${SLOT}" -I "${AGFI}"
  fi
  local desc
  desc="$(sudo fpga-describe-local-image -S "${SLOT}" -H)"
  echo "${desc}" >&2
  echo "${desc}" | grep -q "${AGFI}.*loaded" ||
    die "${AGFI} is not loaded in slot ${SLOT} (use --load)"
  echo "${desc}" | grep -qi "${SHELL_VERSION}" ||
    die "unexpected shell version (want ${SHELL_VERSION})"
  local deadline=$(( $(date +%s) + 10 )) led
  while :; do
    led="$(sudo fpga-get-virtual-led -S "${SLOT}" | tail -1 | tr -d ' -')"
    # The two least significant LEDs are ddr_ready and hbm_ready.
    [[ "${led: -2}" == "11" ]] && break
    (( $(date +%s) < deadline )) || die "DDR/HBM not ready (vLED ${led})"
    sleep 0.2
  done
  log "image ${AGFI} loaded, DDR and HBM ready"
}

# ---------------------------------------------------------------------------
# Tools.
# ---------------------------------------------------------------------------
bazel_bin() { echo "${COMPILER_DIR}/bazel-bin"; }
iree_compile() { echo "$(bazel_bin)/external/iree_core+/tools/iree-compile"; }
iree_run_module() { echo "$(bazel_bin)/external/iree_core+/tools/iree-run-module"; }

ensure_compiler_tools() {
  [[ -d "${COMPILER_DIR}" ]] || die "coralnpu-compiler not found at ${COMPILER_DIR}"
  if [[ ! -x "$(iree_compile)" || ! -x "$(iree_run_module)" ]]; then
    log "building iree-compile / iree-run-module (first build is slow)"
    (cd "${COMPILER_DIR}" && bazel build --config=dev \
      @iree_core//tools:iree-compile @iree_core//tools:iree-run-module)
  fi
  if [[ "${BACKEND}" == "f2" && ! -f "$(bazel_bin)/runtime/sim/f2/libcoralnpu_simulator_f2.so" ]]; then
    log "building the F2 backend library"
    (cd "${COMPILER_DIR}" && bazel build --config=dev //runtime/sim/f2:libcoralnpu_simulator_f2.so)
  fi
}

backend_env() {
  # Exported for iree-run-module and chat_f2.
  export LD_LIBRARY_PATH="$(bazel_bin)/runtime/sim/f2:$(bazel_bin)/runtime/sim:${LD_LIBRARY_PATH:-}"
  case "${BACKEND}" in
    f2)    export CORALNPU_SIMULATOR=f2 CORALNPU_PERSISTENT_DEVICE_MEMORY=1 \
                  CORALNPU_F2_SLOT="${SLOT}" CORALNPU_F2_TIMEOUT_S="${TIMEOUT}" ;;
    mpact) export CORALNPU_SIMULATOR=mpact ;;
    cpu)   ;;
    *)     die "unknown backend ${BACKEND}" ;;
  esac
  [[ -n "${PROFILE_LOG}" ]] && export CORALNPU_PROFILE_LOG="${PROFILE_LOG}"
  return 0
}

compile_mlir() {  # <in.mlir> <out.vmfb>
  log "compiling $1 for F2 (${F2_CPU_FEATURES})"
  "$(iree_compile)" \
    --iree-hal-target-device=local \
    --iree-hal-local-target-device-backends=llvm-cpu \
    --iree-llvmcpu-target-cpu=host \
    --iree-hal-target-device=coralnpu \
    --coralnpu-target-cpu-features="${F2_CPU_FEATURES}" \
    "$1" -o "$2"
}

run_vmfb() {  # <module.vmfb> [iree-run-module args...]
  local dev="coralnpu"
  [[ "${BACKEND}" == "cpu" ]] && dev="local-sync"
  log "running $1 on ${dev}"
  "$(iree_run_module)" --device="${dev}" --module="$1" "${@:2}"
}

run_gemma() {  # <artifacts dir>
  local dir="$1" meta model
  meta="$(ls "${dir}"/*.json 2>/dev/null | grep -v treedef | head -1)"
  [[ -n "${meta}" ]] || die "no <model>.json in ${dir}"
  model="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['model'])" "${meta}")"
  [[ -f "${dir}/${model}.vmfb" ]] || compile_mlir "${dir}/${model}.mlir" "${dir}/${model}.vmfb"
  local args=(--artifacts "${dir}" --max-tokens "${MAX_TOKENS}")
  [[ "${BACKEND}" == "cpu" ]] && args+=(--device cpu)
  for p in "${PROMPTS[@]:-}"; do [[ -n "${p}" ]] && args+=(--prompt "${p}"); done
  [[ -n "${PROMPTS_FILE}" ]] && args+=(--prompts-file "${PROMPTS_FILE}")
  [[ ${#PROMPTS[@]} -eq 0 && -z "${PROMPTS_FILE}" ]] && args+=(--prompt "France capital is")
  [[ -n "${OUT}" ]] || OUT="${dir}/run_${BACKEND}.json"
  args+=(--out "${OUT}")
  (cd "${COMPILER_DIR}" && bazel build --config=dev //examples/gemma3-jax-aot:chat_f2 \
     @iree_core//runtime/bindings/python:runtime)
  "$(bazel_bin)/examples/gemma3-jax-aot/chat_f2" "${args[@]}"
  log "record: ${OUT}"
  if [[ -n "${REFERENCE}" ]]; then
    local exact=()
    [[ "${REFERENCE}" == *mpact* ]] && exact=(--exact)
    python3 "${REPO_DIR}/tools/compare_tokens.py" "${OUT}" "${REFERENCE}" "${exact[@]}"
  fi
  if [[ -n "${PROFILE_LOG}" && "${BACKEND}" == "mpact" ]]; then
    python3 "${REPO_DIR}/tools/estimate_tps.py" "${OUT}" --meta "${meta}"
  fi
}

export_gemma() {  # <model name> -> artifacts dir
  local dir="${REPO_DIR}/artifacts/$1"
  if [[ ! -f "${dir}/$1.mlir" ]]; then
    [[ "$1" == "gemma3-1b" || -n "${HF_TOKEN:-}" ]] || log "HF_TOKEN not set; $1's checkpoint is gated"
    log "exporting $1 (int4 weights, int8 activations) to ${dir}"
    (cd "${COMPILER_DIR}" && bazel build --config=dev //examples/gemma3-jax-aot:export_gemma_f2)
    "$(bazel_bin)/examples/gemma3-jax-aot/export_gemma_f2" --model "$1" --out "${dir}"
  fi
  echo "${dir}"
}

# ---------------------------------------------------------------------------
# Dispatch by model type.
# ---------------------------------------------------------------------------
[[ "${BACKEND}" == "f2" ]] && preflight_f2

case "${MODEL}" in
  *.elf)
    [[ "${BACKEND}" == "f2" ]] || die "ELFs run on the F2 backend only"
    exec python3 "${REPO_DIR}/fpga/aws/f2/run_elf.py" "${MODEL}" --slot "${SLOT}" \
      --timeout "${TIMEOUT}" --expected-agfi "${AGFI}" "${EXTRA_ARGS[@]}"
    ;;
  *.vmfb)
    ensure_compiler_tools; backend_env
    run_vmfb "${MODEL}" "${EXTRA_ARGS[@]}"
    ;;
  *.mlir)
    ensure_compiler_tools; backend_env
    compile_mlir "${MODEL}" "${MODEL%.mlir}.vmfb"
    run_vmfb "${MODEL%.mlir}.vmfb" "${EXTRA_ARGS[@]}"
    ;;
  *.tflite)
    ensure_compiler_tools; backend_env
    MLIR="${MODEL%.tflite}.mlir"
    log "converting ${MODEL} to TOSA MLIR"
    (cd "${COMPILER_DIR}" && bazel build --config=dev //examples/mobilenetv2-tflite-aot:tflite_to_tosa)
    "$(bazel_bin)/examples/mobilenetv2-tflite-aot/tflite_to_tosa" "$(realpath "${MODEL}")" "$(realpath -m "${MLIR}")"
    compile_mlir "${MLIR}" "${MODEL%.tflite}.vmfb"
    run_vmfb "${MODEL%.tflite}.vmfb" "${EXTRA_ARGS[@]}"
    ;;
  *.task|*.litertlm)
    die "$(basename "${MODEL}") is a LiteRT-LM bundle; it cannot be compiled for CoralNPU. Use the Hugging Face checkpoint (fpga_run.sh gemma3-1b / functiongemma-270m)."
    ;;
  gemma3-1b|functiongemma-270m|gemma3-270m)
    ensure_compiler_tools; backend_env
    run_gemma "$(export_gemma "${MODEL}")"
    ;;
  *)
    [[ -d "${MODEL}" ]] || die "unknown model type: ${MODEL}"
    ensure_compiler_tools; backend_env
    run_gemma "${MODEL}"
    ;;
esac
