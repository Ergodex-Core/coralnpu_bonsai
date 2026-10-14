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

# Installs an ArcSim toolchain for the //tests/arcsim_sim targets.
#
#   setup_arcsim.sh bundle <arcsim-coral-matrix-baseline-*.zip> [dest_dir]
#       Verifies and extracts a developer bundle. dest_dir defaults to
#       ~/.cache/arcsim, which every checkout and worktree picks up
#       automatically when ARCSIM_HOME is unset.
#
#   setup_arcsim.sh deb <arcsim_*.deb>
#       Installs the licensed release package with apt (needs sudo and the
#       matching ergodex-site-tools package). Then export
#       ARCSIM_HOME=/usr/lib/ergodex.
#
# ArcSim only runs on Linux x86-64 (glibc >= 2.38).

set -euo pipefail

usage() {
  sed -n '17,28p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

check_host() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  if [[ "${os}" != "Linux" || "${arch}" != "x86_64" ]]; then
    echo "error: ArcSim needs Linux x86-64; this host is ${os} ${arch}." >&2
    echo "       On Apple Silicon use the dev container with --platform linux/amd64." >&2
    exit 1
  fi
}

install_bundle() {
  local zip="$1"
  local dest="${2:-${HOME}/.cache/arcsim}"
  [[ -f "${zip}" ]] || { echo "error: ${zip} not found" >&2; exit 1; }
  local name
  name="$(basename "${zip}" .zip)"
  local target="${dest}/${name}"
  if [[ -e "${target}" ]]; then
    echo "${target} already exists; remove it to reinstall." >&2
  else
    mkdir -p "${dest}"
    local tmp
    tmp="$(mktemp -d "${dest}/.extract.XXXXXX")"
    # Expanded now: tmp is local and gone by the time the EXIT trap runs.
    trap "rm -rf '${tmp}'" EXIT
    unzip -q "${zip}" -d "${tmp}"
    [[ -d "${tmp}/${name}" ]] || { echo "error: ${zip} has no top-level ${name}/" >&2; exit 1; }
    (cd "${tmp}/${name}" && sha256sum --quiet -c SHA256SUMS)
    mv "${tmp}/${name}" "${target}"
  fi
  for tool in circt-verilog arcsim opt llc; do
    [[ -x "${target}/toolchain/bin/${tool}" ]] || { echo "error: missing toolchain/bin/${tool}" >&2; exit 1; }
  done
  "${target}/toolchain/bin/arcsim" --version >/dev/null 2>&1 || \
    echo "warning: ${target}/toolchain/bin/arcsim --version failed; check glibc >= 2.38." >&2
  echo "ArcSim bundle ready at ${target}"
  if [[ "${dest}" != "${HOME}/.cache/arcsim" ]]; then
    echo "export ARCSIM_HOME=${target}"
  fi
}

install_deb() {
  local deb="$1"
  [[ -f "${deb}" ]] || { echo "error: ${deb} not found" >&2; exit 1; }
  sudo apt-get install -y "$(realpath "${deb}")"
  echo "ArcSim release installed. Activate your license, then:"
  echo "export ARCSIM_HOME=/usr/lib/ergodex"
}

[[ $# -ge 2 ]] || usage
check_host
case "$1" in
  bundle) install_bundle "$2" "${3:-}" ;;
  deb) install_deb "$2" ;;
  *) usage ;;
esac
