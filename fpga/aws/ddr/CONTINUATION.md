# Transfer checkpoint — DDR inference

This feature starts at `fa0aeef84ea6dd9bdecb5f6d78f5b9fd29a28f56` and stacks on
`codex/fpga-pr-build-tests` (PR #1). Keep the DDR diff separate from its base.
The source checkpoint is portable; a cloud execution environment has not been
provisioned by this change. No private operational files, credentials, weights
or FPGA images are included in Git.

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
