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

# Usage: arcsim_vs_verilator_test.sh <arcsim_sim> <verilator_sim|none> <files...>
#
# Runs every .elf among <files> on the ArcSim simulator and, unless the
# Verilator simulator is "none", on Verilator too. Fails if any run on ArcSim
# fails or if the two simulators disagree on pass/fail.

set -uo pipefail

arcsim="$1"
verilator="$2"
shift 2

logdir="${TEST_UNDECLARED_OUTPUTS_DIR:-$(mktemp -d)}"
status=0
count=0

for f in "$@"; do
  [[ "${f}" == *.elf ]] || continue
  count=$((count + 1))
  name="$(basename "${f}" .elf)"

  "${arcsim}" --binary="${f}" >"${logdir}/${name}.arcsim.log" 2>&1
  arc_rc=$?
  arc_cycles="$(grep -o 'EXECUTION_CYCLES [0-9]*' "${logdir}/${name}.arcsim.log" | cut -d' ' -f2)"

  if [[ "${verilator}" == "none" ]]; then
    ver_rc="${arc_rc}"
    ver_desc="n/a"
    expected=0
  else
    "${verilator}" --binary="${f}" >"${logdir}/${name}.verilator.log" 2>&1
    ver_rc=$?
    ver_desc="rc=${ver_rc}"
    expected="${ver_rc}"
  fi

  echo "${name}: arcsim rc=${arc_rc} cycles=${arc_cycles:-?} verilator ${ver_desc}"
  if [[ "${arc_rc}" -ne 0 || "${arc_rc}" -ne "${expected}" ]]; then
    echo "FAIL ${name}; logs in ${logdir}"
    tail -n 20 "${logdir}/${name}.arcsim.log"
    status=1
  fi
done

if [[ "${count}" -eq 0 ]]; then
  echo "error: no .elf files given" >&2
  exit 1
fi
exit "${status}"
