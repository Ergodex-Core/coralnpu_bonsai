#!/usr/bin/env bash
# Ayush's entry point, adapted for explicit modes and fresh output directories.
set -euo pipefail
CI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${CI_DIR}/run.sh" "$@"
