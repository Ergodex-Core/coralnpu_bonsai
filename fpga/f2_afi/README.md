# F2 AFI builds for CoralNPU variants

Builds a new F2 image (AFI) for an RTL variant from the run-054 rebuild
package (`s3://ergodex-bonsai-on-chip-438374459156-us-east-1/coralNPU-bonsai-DCPs/rebuild/`,
inventory in [`/fpga.md`](../../fpga.md)). AFI builds take a working day, so they
run at a cadence, batching RTL changes that already passed simulation
(see the SOP in `architecture/sop/`). Everything else is measured in simulation.

## What changes between builds

Only the core RTL. `build_afi.sh` regenerates it with the package's own
generator: the 382b5c12 source snapshot with the variant's Chisel sources
copied over, the same `fp_compat.json` configuration plus the variant's
generator flags, DISPATCH2 and matrix RS output register 1. Everything else
stays pinned by the package:
- HDK 2.3.0 and its IP
- small shell `0x10212415`
- Vivado 2025.2
- constraints and floorplan
- 25 MHz core clock

Regenerating the unchanged design (`regen base`) must reproduce run 054's
frozen RTL byte for byte before any variant is trusted. The generator stamps
the source revision into read-only CSRs (`Csr.sv`, `kscm0..4`). `regen base`
keeps the package's revision; a variant sets `REV` to the commit of its RTL,
so the image can report which source it came from.

## Where it runs

A dedicated build instance: m7i.4xlarge (16 vCPU, 64 GiB, 300 GB) from the
FPGA Developer AMI (`ami-07c259dae738189ec`, Vivado 2025.2), reached over
SSM. Not the shared `fpga-dev-f2`. Vivado peaked at ~22 GB in run 054, and
routing alone took ~5 h.

## Steps

```bash
export WORK=~/afi SRC=~/afi/src     # SRC: the coralnpu_bonsai tree with the variant
B=$SRC/fpga/f2_afi/build_afi.sh
bash $B hdk                                   # pinned HDK + IP, run.py check-deps
bash $B regen base                            # must match run 054 exactly
REV=1986101d bash $B regen pf --ebusPrefetchStreams=8   # PR #12; REV = commit of its RTL
bash $B package pf                            # package copy with input/core from regen pf
bash $B build pf                              # synthesis + place + route (hours)
bash $B audit pf                              # routed-DCP acceptance checks
bash $B tar pf                                # Developer CL tar + manifest
```

Each stage leaves `$WORK/<stage>_<name>.done` and is skipped on rerun.

## Submitting (needs approval)

1. Upload `coralnpu_<name>.Developer_CL.tar` to
   `s3://ergodex-bonsai-on-chip-438374459156-us-east-1/coralNPU-bonsai-DCPs/<name>/`.
2. Run `aws ec2 create-fpga-image --region us-east-1 --name coralnpu-<name>-<date>`
   with `--input-storage-location` pointing at that tar and `--logs-storage-location`
   at a `logs/` prefix next to it.
3. Wait for `State=available` (`aws ec2 describe-fpga-images`).
4. Validate on an F2 instance (SOP stage 5). Ask before loading the image on
   the shared `fpga-dev-f2` slot.
