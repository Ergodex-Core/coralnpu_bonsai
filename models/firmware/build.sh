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
# Builds every kernel in models/kernels with clang (RISC-V target) and lld,
# without Bazel: models/firmware/build.sh [out_dir]. The Zvt instructions are
# emitted as .insn/.word, so no matrix-extension support is needed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"          # directory that contains models/
OUT="${1:-${HERE}/out}"
CLANG="${CLANG:-$(command -v clang-18 || command -v clang)}"
OBJDUMP="${OBJDUMP:-$(command -v llvm-objdump-18 || command -v llvm-objdump || true)}"
mkdir -p "${OUT}"

FLAGS=(
  --target=riscv32-unknown-elf -march=rv32imf_zicsr_zve32f -mabi=ilp32f
  -O2 -fno-vectorize -fno-slp-vectorize -ffp-contract=off -fno-fast-math
  -fno-builtin -ffunction-sections -fdata-sections -mno-relax -nostdlib
  -fno-exceptions -fno-rtti -I"${ROOT}"
  -fuse-ld=lld -Wl,--no-relax,--gc-sections,--build-id=none
  -Wl,-T,"${HERE}/kernel.ld"
)

for src in "${HERE}"/../kernels/*.cc "${HERE}"/../kernels/*.S "${HERE}"/../asm/*.S; do
  [[ -e "${src}" ]] || continue
  name="$(basename "${src}")"; name="${name%.*}"
  elf="${OUT}/${name}.elf"
  "${CLANG}" "${FLAGS[@]}" -Wl,-Map,"${OUT}/${name}.map" \
    "${HERE}/boot.S" "${HERE}/libc_min.c" "${src}" -o "${elf}"
  text=$(python3 - "${elf}" <<'PY'
import struct, sys
d = open(sys.argv[1], "rb").read()
phoff, phsize, phnum = struct.unpack_from("<I", d, 28)[0], struct.unpack_from("<H", d, 42)[0], struct.unpack_from("<H", d, 44)[0]
sizes = []
for i in range(phnum):
    t, off, va, pa, fsz, msz, fl, al = struct.unpack_from("<8I", d, phoff + i * phsize)
    if t == 1 and msz: sizes.append(f"{pa:#x}+{msz}")
print(" ".join(sizes))
PY
)
  echo "${name}.elf  segments: ${text}"
done
echo "kernels in ${OUT}"
