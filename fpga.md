# CoralNPU F2 FPGA image (Alex, 2026-10-11)

Inventory of the FPGA image Alex shared and every related AWS resource.
Account `438374459156`, region `us-east-1`. Gathered 2026-10-14.

## The AFI

| Field | Value |
|---|---|
| AFI ID | `afi-05b9ecbfc7e351f73` |
| AGFI ID (use this to load) | `agfi-075a98d106eb1b6bf` |
| Name | `alex-coralnpu-20261011-ed56260c` |
| Description | Alex CoralNPU 2026_10_11-143033; tar SHA256 `ed56260c899e931967d058107fa82e82e9313a6e3df8fcc92de4b430185b3bd6` |
| State | available |
| Created / updated | 2026-10-12 11:14 UTC / 12:24 UTC |
| Shell | small shell `0x10212415` |
| PCI IDs | vendor `0x1d0f`, device `0xf001`, subsystem `0x1d51`, subsystem vendor `0xfedc` |
| Public | no; no extra load permissions |
| Region | us-east-1 only |

Load on an F2 instance:

```bash
sudo fpga-load-local-image -S 0 -I agfi-075a98d106eb1b6bf
sudo fpga-describe-local-image -S 0 -H
```

The image has no test program. The coralNPU TCMs must be loaded over PCIe
(BAR0 OCL for TCM/CSR, BAR4 PCIS for DDR/HBM) before execution. See
`software_and_signoff/software/README.md` for the porting contract.

### Build configuration (run 054)

- Core 25 MHz, shell/main 250 MHz, HBM AXI 300 MHz.
- Matrix/VME, DDR and HBM enabled. Scalar/vector FP retained. Matrix BF16/INT16 disabled.
- DISPATCH2, matrix RS output reg = 1, matrix pblocks across two SLRs.
- Vivado 2025.2, part `xcvu47p-fsvh2892-2-e`.
- coralNPU source commit `382b5c12030ad8eb74ab8deafb2529301faeab16` (current `main` HEAD of this repo).
- AWS HDK `20fe90180574464fd509c994680ec67191e9759f` (2.3.0), IP submodule `6d32be972e6da854e61a8d3d6ec0466ab491c1b3`.

## S3 files

Bucket: `s3://ergodex-bonsai-on-chip-438374459156-us-east-1/coralNPU-bonsai-DCPs/`

| Path | Size | What it is |
|---|---|---|
| `coralnpu.Developer_CL.tar` | 389 MiB | Developer CL tarball submitted for the AFI. Its SHA-256 matches the AFI description. |
| `validation/ed56260c899e9319/afi-05b9ecbfc7e351f73/` | 1.5 MiB, 4 files | AWS AFI ingestion output: `*_io_summary.rpt`, `*_timing_summary.rpt`, `*_vivado.log`, `State` (= available) |
| `rebuild/` | 30.6 MiB, 1533 files | Self-contained package to rebuild the routed DCP |
| `software_and_signoff/` | 42.3 MiB, 93 files | Sign-off reports, timing/CDC review, host software notes |

### `rebuild/`

| Path | Contents |
|---|---|
| `README.md` | Rebuild instructions, pinned dependencies, fixed configuration |
| `run.py` | Portable launcher: `check-deps`, `build`, `audit`, `--prepare-only` |
| `hdk_inputs.json` | Pinned HDK/IP revisions and SHA-256 of 120 HDK dependency files |
| `input/build_config.json`, `input/build_settings.tcl`, `input/sources.json` | Build parameters and source revisions |
| `input/constraints/` (9) | Clocks, floorplans (baseline, balanced, partitioned, matrix2slr), SLR1 links, integration timing |
| `input/core/` (5) + `input/core/rtl/` (247) | Frozen generated coralNPU RTL, including verification assert layers |
| `input/rtl/` (2) | `cl_coralnpu.sv` wrapper, ~23.5 KiB |
| `input/scripts/` (14) | Original run-054 Vivado Tcl scripts, verbatim |
| `audit/` (11) | Routed-DCP audit scripts and `static_baseline.rpt` |
| `generator/` | Optional RTL regeneration: README, configs (`fp_compat.json`; do not use `int8.json`), scripts, Bazel 9.1.0 hash |
| `generator/third_party/coralnpu_bonsai/` (1229) | Git archive of this repo at `382b5c12` |
| `generator/third_party/aws-fpga/.../cl_dram_hbm_dma.sv` | AWS example template used for the wrapper |

### `software_and_signoff/`

| Path | Contents |
|---|---|
| `timing_cdc_review.md` | Timing and CDC review |
| `validation.json` | Package self-checks (83 copied files verified, 35 shell pins equal, etc.) |
| `reports/final_audit/` (15) | Routed audit: timing summary (30.5 MiB), utilization, SLR utilization, methodology, route status, shell SPI timing, linked clocks, Vivado log |
| `reports/shell_baseline/`, `reports/shell_spi_baseline/` | Baseline shell timing/CDC/SPI reports for comparison |
| `reports/no_clock_pin_comparison.json` | Clock-pin comparison |
| `software/README.md` | Host loader status and F2 port contract (BAR0/BAR4 map, reset/start PC at `0x30000`/`0x30004`) |
| `software/ddr_controller/calibration_ddr.elf` | MicroBlaze DDR calibration firmware already in the bitstream. Not a RISC-V program. |
| `software/aws_sdk_reference/` | `fpga_mgmt.h`, `fpga_pci.h`, `fpga_common.h` API references |
| `software/upstream_reference/` | Unchanged upstream loader, CRT, linker template, Chisel sources from `382b5c12` |

The README links `../docs/acceptance.md`, `../provenance/` and `../evidence/audit/`.
Those folders are not in S3. They are probably only inside the tarball.

## Related EC2 resources

### Other coralNPU AFIs in this account (not from Alex)

| AFI | AGFI | Name | Created |
|---|---|---|---|
| `afi-09e68c2bdf879c106` | `agfi-04bc813f5ecea545f` | `coralnpu-hbm-300mhz-20261005-161524-5a0c0e40` | 2026-10-05 |
| `afi-075217859fded5c7a` | `agfi-00f57ea77524aef58` | `coralnpu-tcm-50mhz-20261005-042840-810dae62` | 2026-10-05 |
| `afi-03d7f2af91f1e27aa` | `agfi-092a0fbcba5d320c6` | `erg103-b49c4893-ram-first-slice` | 2026-10-03 |

### AMIs

| AMI | Name | Notes |
|---|---|---|
| `ami-006bc1f7f1c7d4095` | `bonsai-f2-clone-claude-20261003` | Clone of F2 dev instance `i-0256d2c7bdaaebd2f` for the ERG-103 build line |

### F2 instances that can load the AFI

| Instance | Name | AMI | State |
|---|---|---|---|
| `i-00a83cab3c81ac10f` | `fpga-dev-f2` | `ami-07c259dae738189ec` | running |
| `i-0256d2c7bdaaebd2f` | `bonsai-f2-platform-20260918` | `ami-017bd23ff95264395` | stopped |
| `i-06640bb8cc565a408` | `bonsai-native-matched-baseline-53c6f8ba-20261004` | `ami-017bd23ff95264395` | stopped |
| `i-0f91abd6550a51d9d` | `bonsai-f2-stock-coral-20261004` | `ami-006bc1f7f1c7d4095` | stopped |
| `i-0e9b9524d02386fe3` | `bonsai-f2-stock-coral-20261004b` | `ami-006bc1f7f1c7d4095` | stopped |
| `i-0adbe40acf807ef24` | `chipyard` | `ami-07c5616000d1cd98b` | stopped |
| `i-0e6d0af4becf6fbd1` | `umang-dev-fpga` | `ami-0521cb2d60cfbb1a6` | stopped |

## Useful commands

```bash
B=s3://ergodex-bonsai-on-chip-438374459156-us-east-1/coralNPU-bonsai-DCPs
aws s3 ls --recursive --human-readable $B/
aws s3 cp $B/coralnpu.Developer_CL.tar .
aws s3 sync $B/rebuild ./rebuild
aws ec2 describe-fpga-images --region us-east-1 --fpga-image-ids afi-05b9ecbfc7e351f73
```
