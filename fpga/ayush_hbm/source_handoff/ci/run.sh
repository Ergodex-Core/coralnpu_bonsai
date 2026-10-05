#!/usr/bin/env bash
set -eo pipefail
CI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export CORAL_ROOT="${CORAL_ROOT:-$(dirname "$CI_DIR")}"
export AWS_FPGA_REPO_DIR="${AWS_FPGA_REPO_DIR:?Set AWS_FPGA_REPO_DIR to the pinned HDK checkout}"
export VIVADO_SETTINGS="${VIVADO_SETTINGS:-/opt/Xilinx/2025.2/Vivado/settings64.sh}"
source "$VIVADO_SETTINGS"
cd "$AWS_FPGA_REPO_DIR"
source hdk_setup.sh -s
export CL_DIR="$CORAL_ROOT/cl_coralnpu_hbm"
export SHELL_MODE=small_shell
cd "$CORAL_ROOT"
exec python3 "$CI_DIR/pipeline.py" "$@"
