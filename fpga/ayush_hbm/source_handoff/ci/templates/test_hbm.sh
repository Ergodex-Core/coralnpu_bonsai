#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
VERILATOR="${VERILATOR:-verilator}"
"$VERILATOR" --binary --timing -j 16 --top-module tb_hbm --Mdir tests/obj_hbm \
  -Wno-fatal -Wno-BLKANDNBLK -Wno-WIDTH -Wno-UNOPTFLAT \
  +define+USE_GENERIC+SYNTHESIS+VLEN_128+ZVE32F_ON+TB_SUPPORT \
  +incdir+cl_coralnpu_hbm/rtl/include \
  cl_coralnpu_hbm/design/cl_dram_dma_pkg.sv cl_coralnpu_hbm/rtl/RvvCoreMiniAxi.sv cl_coralnpu_hbm/design/coral_host.sv tests/hbm_memory_model.sv tests/tb_hbm.sv
tests/obj_hbm/Vtb_hbm
