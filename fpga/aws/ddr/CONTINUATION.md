# Transfer checkpoint — DDR inference

This feature starts at `fa0aeef84ea6dd9bdecb5f6d78f5b9fd29a28f56` and stacks on
`codex/fpga-pr-build-tests` (PR #1). Keep the DDR diff separate from its base.
The source checkpoint now runs in a saved Linux cloud coding environment.
Git fetch, native CPU validation and portable DDR protocol tests execute there.
This does not establish access to a licensed FPGA builder or physical device.
No private operational files, credentials, weights or FPGA images are in Git.

## Cloud continuation update

The frozen post-`ed62e94c` handoff was verified before applying it: archive
SHA256 `504d61338a95809a3c8a9c6801b931117c9c9c3be56f720148cc3b56b6c0a10d`,
with all 27 modified and two new source files matching their manifest hashes.
Repository formatting is kept separate from runtime diagnostic fixes.

- The requested Actions job `111826540057` failed because the four licensed
  builder variables were empty. Source checks passed; route/package was skipped.
  Keep readiness blocked until the CI owner qualifies and configures a backend.
- Host numerical failures now retain raw logits, hashes, generated IDs,
  comparison diagnostics and raw cycle counters. Failure remains failure.
- CPU reference runs export complete prediction rows. Native comparison verifies
  complete hashed traces and reports the first divergence without increasing the
  `3e-5` normalized budget. Native CPU wall times are not FPGA measurements.
- Tiny fixture generation covers prompt variation, repeated runs, prompt-only
  evaluation, early/later EOS and exact context bounds for BF16 and native PQ2.
  Generating a fixture is not executing it on Coral.
- A transferred LLVM 18.1.3 ELF was verified against the exact `ed62e94c` source
  archive `26f55ae5cd485ed7b07d62f14ae001eff17e77ce20a626b25cf6aee01d4dbcaf`.
  ELF SHA256 is `eb529b3f6f931ef902ae60e22c0cfaa6f8344941cabb37418e27fe21ffbd3818`;
  ITCM is 352 bytes, DDR code/rodata 9792 bytes, and DTCM 5424 bytes including
  stack. Its 2506 decoded instructions have no FMA, compressed or double
  instructions. The `_start` weak-binding warning remains recorded. Formatting
  changed source hashes, so the current source still needs a fresh pinned build.

Full-checkpoint execution has not been rerun in this cloud environment: access
to the pinned publisher downloads is blocked. The prior full-Qwen comparison
reported matching four-token IDs `[21806,0,358,2776]` but FAILED the unchanged
`3e-5` operator budget (worst normalized error about `1.28e-4`). Preserve that
failure. Synthetic replay isolates its own first difference to the exponential
approximation in SwiGLU; it does not establish the cause of the full-model error.

The PR2 CI RTL artifact embeds its synthetic merge commit in five SCM CSR words.
Replacing only those words with the reference stamp in memory reproduces both
the exact pinned top hash and include inventory hash. Raw artifacts and pins
remain unchanged. The build needs a reviewed stamp-aware provenance check that
verifies the actual checkout stamp and all remaining bytes. The CI artifact also
omits the parameter header required by the real-core harness. Obtain that header
and exact Verilator 5.050 before running the fixtures. Vendor CDC, routed DDR and
physical model generation remain NOT_RUN.

PR1 owns CI runtime/controller changes. Its reviewed immutable builder inputs
must explicitly include the DDR driver, tests and DDR4 XCI pin before that
backend can qualify PR2. Publishing CI source or integrating its branch is not
builder activation or DDR qualification.

Physical testing is the immediate priority. The published `ed62e94c` source and
verified ELF are an exact matching starting point. Before packaging, the build
must also explicitly require calibration BRAM presence and nonzero `INIT_2C`,
and recognize AWS-prefixed critical warnings. The CI owner is preparing that
gate fix separately; record its final candidate SHA and preserve all existing
timing, DRC, CDC and bus-skew gates. No qualified DDR routed image is available.
The new host memory smoke runner and tiny generation cases are staged for the
existing shared FPGA owner's coordinated execution; neither has run physically.

## Saved implementation

- `cl_coralnpu/design/coral_ddr_*.sv`: bounded core/PCIS DDR access, CDC,
  arbitration and watchdog; controller and DDR status wired in the top wrapper.
- `models/pack_model.py`: pinned original checkpoint identity and native-byte
  packaging; explicit BF16/PQ2/F32 numerical contract and exact DDR budgets.
- `models/runtime/`: full decoder and autoregressive greedy generation on Coral,
  mailbox ABI **2**, **128 bytes**, context cap **2048**, model reserve **16 MiB**.
- `ddr/host/`: matching ABI v2 loader/runner with bounded SDK operations, readback,
  complete per-step logit/token comparisons and device-cycle metrics.
- `ddr/core_sim/`: pinned real-core simulation, DDR memory model and firmware
  fixture loading. Portable FIFO model is explicitly not vendor CDC signoff.

## Evidence at transfer

| Gate | Result |
| --- | --- |
| Existing source/build gate tests | PASS, 9 executed; 2 optional retained-report tests skipped |
| Model packaging/operator tests | PASS, 11 tests with NumPy |
| Native decoder/generation tests | PASS, 5 groups; independent tiny references, cached tokens, EOS/repeatability |
| Host fake-device and file validation | PASS, 16 tests |
| DDR frontend/backend/functional CDC suites | PASS, 4 suites |
| Updated pinned-core TCM/scalar/RVV regression | PASS; `sim/evidence/` |
| Pinned-core ITCM boot into DDR code/read/write/byte preservation | PASS; `core_sim/evidence/` |
| Pinned HDK/core top interface lint | PASS; ports and widths only |
| Original Qwen and Bonsai package fidelity | PASS; both 310-tensor packages, Qwen tied head byte-verified; `models/packaging_validation.json` |
| Strict LLVM 18.1.3 firmware ELF | NOT_RUN at checkpoint preparation; bounded compile handoff staged separately |
| Tiny autoregressive generation on real core | NOT_RUN; depends on target ELF |
| Full Qwen/Bonsai reference and Coral token-generation comparisons | NOT_RUN |
| DDR synthesis/route/timing/DRC/vendor CDC and physical execution | NOT_RUN |

Rerun affected tests after any change. Historical on-chip physical examples,
old Q8_K Bonsai arithmetic and tiny fixtures do not establish new full-model DDR
inference. Do not upgrade these labels merely because a script exists.

## Continue in the next executor

1. Check out the published feature head and read `ddr/README.md`,
   `models/README.md`, `models/runtime/README.md`, `ddr/host/README.md` and
   `ddr/core_sim/README.md`. Verify a clean source tree and coherent ABI v2 before
   building. Execute the local gates listed in `ddr/README.md`.
2. Compile the final source with exactly LLVM **18.1.3**, using
   `models/runtime/build.py`; retain its ELF, SHA/source/tool manifest, map,
   disassembly and section/ISA audit. Preserve the `ld.lld` invocation name.
   On the previously qualified Linux installation the tools are
   `/usr/lib/llvm-18/bin/clang`, `/usr/bin/ld.lld`,
   `/usr/bin/llvm-objdump-18`, `/usr/bin/llvm-readelf-18`.
3. Run `make_fixture.py` for BF16 and native PQ2, then the pinned-core harness
   with each ELF/fixture. Cover four generated tokens, early EOS, prompt
   variation and repeatability. Compare every emitted token and per-step logits
   against fresh matching references. Investigate any fault before full models.
4. Obtain weights and tokenizer/config artifacts from the pinned official
   identities in `models/README.md`, outside Git. Verify SHA256, inventory,
   dtype, tied head and native ternary ordering before export. A partial download
   or configuration-derived size is not a verified checkpoint. Use `models/tokenize_prompt.py` for pinned raw-completion tokenization and
   detokenization; preserve its JSON receipt alongside token-ID input.
5. Generate same-format full-checkpoint CPU references and compare per-operator,
   per-layer, full logits and several autoregressive tokens for both models.
   Record prompt/tokenizer/profile hashes and numerical tolerances. CPU reference
   execution must remain separate from the host hardware runner.
6. Coordinate the existing shared FPGA owner before any remote job. A bounded
   firmware compile was authorized; heavy FPGA builds, paid capacity, artifact
   uploads and image create/load operations need their concrete approved scope.
   Do not change IAM, networking, credentials, drivers or another owner's slot.
7. Once authorized, build the pinned DDR-enabled image without bypassing any
   existing qualification gate. Review new XPM/controller CDC and bus-skew
   coverage. Then perform DDR bulk readback, tiny generation and real-model
   physical generation for both checkpoints. Report actual backend, all relevant
   hashes, load/prefill/decode times, TTFT and measured decode tokens/second.

The package budget table describes one logit row. Autoregressive validation
retains one row per generated token and needs additional output/EOS buffers;
the host allocator checks the complete request against the aperture. The 2048
position cap is an allocation limit, not a guarantee that every output-history
request fits or that numerical/physical execution has been qualified.
