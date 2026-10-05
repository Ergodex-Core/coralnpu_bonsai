# Coral HBM build automation

This is Ayush's delivered `ci/build_from_coral.sh`, CL templates and required
support, adapted for a separate manual build workflow. It targets the pinned
Coral RVV128 core with 16 GiB HBM (two stacks), DDR disabled, a 50 MHz NPU,
250 MHz shell interface and 300 MHz HBM AXI clock. It does not change PR #1's
CI/controller, PR #2's DDR design, or the separately sealed HBM artifact.

The default command is a source-only dry-run. Building requires an explicit
mode, an isolated licensed Linux environment, pinned inputs and a fresh output
directory. No workflow is activated and no upload, AFI creation, FPGA load or
physical test is performed by the pipeline. `BUILD.bazel` keeps these templates
out of the existing Nexus recursive source glob.

## Source and evidence

[PROVENANCE.json](PROVENANCE.json) records original delivered hashes, sanitized
handoff hashes and integration changes. The verified handoff commit is
`4b84d0b5dd484f7de3eebbdd4ab341e95846b97a`; original archive SHA-256 is
`76d216e46f45df0c42822e7740c4f991e3deddba7fbf6b17d19c386e2dbbf297`.
All 36 source/license payloads were verified at intake. The omitted prebuilt
ELF is rebuilt from `hbm_vector.S` and `hbm.ld`; no binary is committed.
[HANDOFF_README.txt](HANDOFF_README.txt) describes the historical delivered flow,
not the adapted commands below. `SHA256SUMS` covers the current integration.

The inspector reported these findings for the separate `2026_10_05-161524`
artifact: routed WNS +0.009 ns, TNS 0, hold +0.010 ns; all 43 bus-skew constraints
passing; zero DRC errors but 1,477 warnings and one advisory; four critical CDC
findings in the opaque shell. Its configuration is HBM, not DDR. These are
inspector-supplied retained-artifact findings, not results from this workflow.
Source provenance is incomplete; some delivered script/template modification
times postdate that artifact. Reproduction of its exact DCP/tar is **NOT_PROVEN**.
Physical calibration and inference are **NOT_RUN** by this integration.

## Cheap checks from a clean checkout

Use Python 3.11 or newer and Bash. These commands do not require Vivado, AWS,
Bazel, an FPGA or an ELF binary:

```bash
git clone --branch codex/ayush-build-automation \
  https://github.com/Ergodex-Core/coralnpu_bonsai.git coral-automation
cd coral-automation
git rev-parse HEAD
(cd fpga/ayush_hbm && sha256sum --check SHA256SUMS)
bash fpga/ayush_hbm/ci/build_from_coral.sh --dry-run
python3 -m unittest discover -s fpga/ayush_hbm/ci -p 'test_*.py' -v
python3 utils/check_macro_signatures.py
```

The unit tests use fabricated reports, small RTL archives and fake devices.
They exercise failure handling and policy; they are not Vivado or FPGA evidence.
Record the exact integration commit printed above alongside test results.
[VALIDATION.json](VALIDATION.json) records the 29 local tests and remaining gaps.
The aggregate linter remains incomplete: buildifier and `mdl` are unavailable;
Verible cannot parse a macro in the byte-retained AWS `cl_hbm_wrapper.sv`.
Eight other SystemVerilog files were formatted and checked.

## Manual licensed workflow

The following procedure is provided for an already provisioned, authorized
build environment. No licensed build was run during integration. Use private,
quiescent copies of the source and HDK/IP trees: the pipeline's before/after
hashes detect accidental drift, not hostile concurrent mutation. Do not point
this at another user's shared build tree. Local vendor logs can contain private
paths and license diagnostics; review them before sharing.

1. Install the exact versions in [ci/pins.json](ci/pins.json): Bazel 9.1.0,
   Verilator 5.050, Clang/LLD 18.1.3, Vivado 2025.2 build 6299465, HDK 2.3.0.
   Use HDK commit `20fe90180574464fd509c994680ec67191e9759f` and IP commit
   `6d32be972e6da854e61a8d3d6ec0466ab491c1b3`. The IP pin is checked directly;
   it can differ from the superproject's default gitlink. Provision the small
   shell and generated IP dependencies before taking the inventory. Nothing
   in this flow downloads or installs HDK/IP or license material.
2. Make a clean source checkout at the pinned Coral revision. From the
   integration checkout, this can be a separate worktree:

   ```bash
   git worktree add --detach ../coral-source \
     382b5c12030ad8eb74ab8deafb2529301faeab16
   CORAL_SOURCE="$(cd ../coral-source && pwd)"
   AUTOMATION_CI="$PWD/fpga/ayush_hbm/ci"
   ```

3. Set `HDK_ROOT`, `VIVADO_SETTINGS` and `BUILD_PARENT` to absolute operator
   paths. `BUILD_PARENT` must exist, be outside both checkouts and have at least
   100 GiB free for a full build. HDK/output paths must contain only letters,
   numbers, `_`, `-`, `.`, and `/`, because the pinned HDK interpolates them
   into shell commands. Source the authorized Vivado settings to expose its
   executable for version preflight. Add the pinned Bazel/Verilator/LLVM tools
   to `PATH`; executable overrides are available in `--help`.
4. Capture the local input inventory without sourcing or executing HDK/Vivado:

   ```bash
   python3 "$AUTOMATION_CI/pipeline.py" --inventory --hdk-root "$HDK_ROOT" \
     > "$BUILD_PARENT/dependencies.json"
   ```

   Independently review that inventory against the provisioned dependencies.
   It pins the IP commit and every local HDK/generated dependency byte, shared
   setup scripts, and directory aliases. Taking an inventory does not certify
   those inputs. Preserve its reviewed SHA-256; changes require a new review.

   ```bash
   LOCK_SHA="$(sha256sum "$BUILD_PARENT/dependencies.json" | cut -d ' ' -f 1)"
   export VIVADO_SETTINGS
   source "$VIVADO_SETTINGS"
   COMMON=(--coral-repo "$CORAL_SOURCE" --hdk-root "$HDK_ROOT"
     --dependency-lock "$BUILD_PARENT/dependencies.json"
     --dependency-lock-sha256 "$LOCK_SHA")
   python3 "$AUTOMATION_CI/pipeline.py" --preflight "${COMMON[@]}"
   ```

5. Generate RTL, assemble templates, rebuild the ELF and run its parser/loader
   test without launching Vivado:

   ```bash
   python3 "$AUTOMATION_CI/pipeline.py" --generate-only "${COMMON[@]}" \
     --output "$BUILD_PARENT/generation-review"
   ```

   `--generate-only` executes Bazel and LLVM and may fetch Bazel dependencies;
   it is not a dry-run. The output path must not already exist. Inspect the
   receipt and generated sources before a full build.
6. Use another new output directory for a full build:

   ```bash
   python3 "$AUTOMATION_CI/pipeline.py" --build "${COMMON[@]}" \
     --output "$BUILD_PARENT/full-build"
   ```

   The flow generates fabric IP, simulates the core and both SmartConnects,
   checks the source-built ELF loader, synthesizes, routes, reopens the exact
   DCP and gates all reports before packaging. Every external stage is bounded;
   timeout/error cleanup kills its local process group. Failures leave evidence
   in that new output directory, never replace a previous project, and write
   `result.json` once the run directory has been created. Preflight/input errors
   happen before output creation and return nonzero with a diagnostic.

## Gates and remaining qualification

- Source/tool versions, HDK/IP commits, shell DCP hash, local dependency-lock
  hash and current source manifest must match. HBM XCI checks require two
  stacks, 16 GB density, 100 MHz reference/APB, 900 MHz memory clocks,
  controller/PHY mode and calibration bypass disabled. The XCI's inherited
  450 MHz AXI metadata is not used as measured clock evidence.
- H3 is passed consistently to the HDK and tar manifest. The delivered
  `hbm_300mhz.tcl` physical correction remains; reopened-DCP checks verify the
  actual MMCM dividers and 300 MHz generated clock. The pinned HDK driver
  requires `--aws_clk_gen` when recipe arguments are passed; that switch is
  supplied, but the separate HBM MMCM is still checked explicitly.
- Gates require complete routing, nonnegative setup/hold/pulse slack, zero
  failing endpoints, nonempty timing coverage, all 43 bus-skew constraints,
  complete timing checks, no waivers or changed DRC rules, and complete reports.
- No DRC/methodology finding, timing exception, CDC warning or critical finding
  is grandfathered. Unknown formats fail closed. Therefore the retained
  artifact's known findings are not accepted by this packaging policy. An
  independently reviewed, exact exception policy remains future work; changing
  source-clock allowlists or hiding report findings is not a substitute.
- Hardware readiness is the synchronized AND of both HBM stack completion
  signals. The host loader waits before attaching the memory BAR; fake-device
  tests cover timeout cleanup. Eight distinct bank patterns detect aliasing
  missed by the original identical-pattern smoke. Physical calibration, vendor
  simulation, RTL simulation and on-board inference still need actual runs.
- The tar gate checks member paths/types, the exact validated DCP hash and H3
  metadata. A successful local developer tar is not an AFI, deployed image,
  physical pass, or evidence of reproduction of the retained tar.

No live Vivado report corpus has been qualified against the stricter parser in
this PR. The source ELF compilation and fixture-dependent loader check require
LLVM 18.1.3 and remain NOT_RUN if those tools are absent. Record such gaps rather
than substituting a prebuilt binary or reported historical pass.

## Attribution and maintenance

Ayush supplied the integration scripts/templates. Amazon notices and both
bundled license files are retained byte-for-byte. AWS-derived files remain
subject to their notices, including the Amazon Software License's AWS use
limitation; this import does not relicense them. No generated vendor IP, weights,
checkpoints, credentials or private license configuration is included.

When Coral ports, widths, memory geometry, reset or clocks change, review the
hardcoded templates and update their tests and pins together. Do not just change
`coral_commit`. Original handoff hashes remain in provenance; current hashes are
in `SHA256SUMS`. Meaningful adaptations are recorded in provenance, including
fresh output handling, bounded stages, strict gates, H3 metadata, source-built
fixtures, removed cloud operations and distinct bank patterns.
