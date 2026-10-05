#!/usr/bin/env bash
# Called only by --build, after pins/inputs pass, under a process-group timeout.
set -eo pipefail
CI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${VIVADO_SETTINGS:?Set VIVADO_SETTINGS to settings64.sh}"
: "${AWS_FPGA_REPO_DIR:?Set AWS_FPGA_REPO_DIR}"
: "${CORAL_ROOT:?Set CORAL_ROOT}"
# Vendor setup is not nounset-safe. Positional command arguments remain quoted.
# shellcheck disable=SC1090
source "${VIVADO_SETTINGS}"
cd "${AWS_FPGA_REPO_DIR}"
# shellcheck disable=SC1091
source hdk_setup.sh -s
export CL_DIR="${CORAL_ROOT}/cl_coralnpu_hbm"
export SHELL_MODE=small_shell
cd "${CORAL_STAGE_DIR:-${CORAL_ROOT}}"
python3 "${CI_DIR}/vendor_check.py"
exec "$@"
