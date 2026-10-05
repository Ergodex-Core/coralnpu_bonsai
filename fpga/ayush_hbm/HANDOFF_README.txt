AYUSH HBM CI SOURCE REVIEW HANDOFF -- 2026-10-05

Purpose
Reviewed scripts and editable CL/test templates for a separate scripts PR owner.
This is a curated source handoff, not an FPGA image, an approved deployment, or
proof that the 2026_10_05-161524 DCP can be reproduced. No supplied script, test,
build, hardware operation, or AWS action was executed to create this bundle.

Identity and provenance
Derived from the reviewed delivered-ci bundle. Private machine paths and host
metadata were removed. Original and published hashes, source timestamps and
sanitization changes are recorded in PROVENANCE.json.
Templates had observed mtimes near 16:52:49 UTC; build_from_coral.sh, generate_cl.py
and pipeline.py near 16:55 UTC on 2026-10-05. They postdate artifact tag 161524.
The HBM workspace was not a Git repository. Observed mtimes are not immutable
build provenance. SHA256SUMS covers all payload files except itself. Use PROVENANCE.json for observed source times.

Contents and exclusions
ci/ preserves the reviewed entry scripts and templates. License notices are
retained under ci/templates/licenses/. Templates include AWS-derived source;
retaining these notices is not a new licensing grant or licensing review.
ci/templates/tests/hbm_vector.S and hbm.ld are source. The supplied prebuilt hbm_vector.elf fixture is omitted from this source-only
handoff. Its original hash is retained in PROVENANCE.json. Integration must
build that fixture from hbm_vector.S and hbm.ld before fixture-dependent tests. No generated Coral core RTL,
HDK/IP generated code, checkpoint, AFI, credentials, keys, or authentication
material is included. Generated clock/congestion reports and generated clock
constraints are omitted; the HDK build regenerates the clock constraints.

What the delivered workflow would do -- do not run as an artifact smoke test
build_from_coral.sh execs ci/run.sh --generate-cl. run.sh sources Vivado settings
and hdk_setup.sh -s, then invokes pipeline.py. By default the generator runs
Bazel target //hdl/chisel/src/coralnpu:rvv_core_mini_axi_cc_library_emit_verilog
against the current standalone/coralnpu checkout, renames sync identifiers,
unpacks includes, substitutes template roots, and installs a replacement tree.
Existing CL (including checkpoints), tests, and other replaced top-level paths
move to ci-cl-backups/<tag>. Generation rollback covers installation failures;
later build failures leave the replacement installed. The full flow generates
fabric IP, runs core/fabric/loader simulations, runs SynthCL and ImplCL with
A1/B2/C0/H2 metadata plus the physical 300 MHz correction, validates a new DCP,
and packages it. It does not load an FPGA or execute physical hardware tests.

Review caveats to retain in the PR
- HDK -s skips shell downloads/submodule setup in pinned HDK 20fe901...; Bazel
  has no offline restriction and may fetch dependencies and mutate caches.
- Coral HEAD/status are recorded, but commit 382b5c... and a clean worktree are
  not enforced. --rtl-dir accepts pre-generated RTL. HDK HEAD is checked;
  the actual IP submodule/generated dependency set is not comprehensively pinned.
- Before/after selected-file hashes do not prove complete or synthesis-time
  input identity. No step compares the new DCP with the existing f962adde... DCP.
- The per-workspace lock coordinates cooperating CI only. Shared HDK/IP inputs
  and other users of the host still need ownership coordination.
- --generate-only still generates/installs source (and can run Bazel).
  --preflight still sources tool/HDK setup and opens the workspace lock file.
- Optional --bucket uses ordinary aws s3 cp without a conditional immutable
  write. --create-afi creates/polls an image. No expected-account check exists.
  Neither option is enabled by default; permission to review is not permission
  to publish, replace the workspace, or run a new build.
- validate.tcl permits critical CDC findings by source-clock names rather than
  matching a complete previously reviewed exception set; retain report review.
- Existing hbm_host.py bank-0/bank-7 smoke uses identical expected patterns;
  its isolation PASS alone cannot detect complete aliasing. Physical memory
  qualification requires distinct per-bank/cross-stack patterns.
- Tool defaults were sanitized: --coral-repo and AWS_FPGA_REPO_DIR must be
  supplied explicitly; Bazel and Verilator resolve through PATH unless overridden.
- Supplied generation unit tests mock Bazel. Reported generation-only checks
  do not demonstrate a successfully reproduced DCP or physical HBM operation.

The old TCM rollback, Ayush HBM161524, and independent DDR candidate are separate.
No claim of a working full-model/Bonsai demo follows from this source handoff.

PUBLIC SOURCE HANDOFF
This isolated directory is not an activated GitHub workflow. Final integration
and its validation belong to the PR owner. No supplied script was executed for
publication; checks were limited to source hashes, sanitization and syntax.
The empty BUILD.bazel prevents ancestor Bazel globs from discovering fixtures.
AWS-derived files retain their original notices and bundled licenses, including
the Amazon Software License AWS use limitation; this handoff does not relicense
them. No generated vendor IP, checkpoint, credentials or private host data is
published.
