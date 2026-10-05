#!/usr/bin/env bash
set -eo pipefail
CI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$CI_DIR/run.sh" --generate-cl "$@"
