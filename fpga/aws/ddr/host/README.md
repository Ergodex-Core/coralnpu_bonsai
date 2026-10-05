# DDR model host runner

This host loads and validates data; all model operators and greedy generation run
in CoralNPU firmware, with its KV cache retained between generated tokens. No
hardware access occurs unless `--execute-hardware` is explicitly supplied.

The runner consumes the `coral-model-package-v1` package, an explicitly hashed
RV32 ELF built by `fpga/aws/models/runtime/build.py`, and a JSON array of token
IDs produced by the checkpoint's tokenizer. Token IDs must be nonnegative and
inside its vocabulary. An optional EOS JSON array controls stopping; the default
has no EOS stopping. Text tokenization/detokenization is currently a separate
host-side step; use the pinned checkpoint's tokenizer and retain its revision.

```sh
python3 fpga/aws/ddr/host/run_inference.py \
  --package /path/to/model-package \
  --elf /path/to/firmware.elf --elf-sha256 "$FIRMWARE_SHA256" \
  --tokens-json /path/to/prompt-token-ids.json --max-new-tokens 8 \
  --eos-tokens-json /path/to/eos-token-ids.json \
  --report /path/to/preflight.json
```

For an approved, already loaded image, append `--execute-hardware`,
`--expected-agfi "$APPROVED_IMAGE"` and `--expected-shell "$APPROVED_SHELL"`.
This runner never builds, uploads, creates or loads images, changes drivers, or
provisions instances. The installed AWS XDMA driver and HDK 2.3.0 SDK shared
library are prerequisites. The slot lock matches the existing physical test
runner's `/run/lock/coralnpu-fpga-slot-N.lock` convention; operators must also
coordinate any other tool that does not honor this advisory lock.

Before reset release, the loader verifies ELF geometry and hash, package hash
and header/config agreement, DDR bounds, the loaded image identity, DDR ABI and
calibration, firmware and model readback, inputs, zeroed workspace and poisoned
outputs. Transfers are 64-byte aligned, bounded to 1 MiB, and use SDK channel 0
to map slot numbers correctly. OCL and DMA run in separate workers so a stalled
DMA call does not prevent an independent bounded reset attempt. Failure to
confirm cleanup reset is explicitly reported as `FAILING`.

`--reference-logits` supplies raw little-endian FP32 rows, one full vocabulary
row for each generated token, paired with `--reference-sha256`. EOS may shorten
the reference. `--max-new-tokens 0` compares the final prompt prediction only.
Every logit must meet `abs(actual-reference) <= atol + rtol*abs(reference)` and
every greedy token must match. Defaults are `--atol 1e-4 --rtol 1e-4`; these are
comparison settings, not a claim that the real models satisfy that tolerance.
Without a reference, completed execution reports `EXECUTED_UNVERIFIED`.

Before model loading, qualify the already loaded DDR image with independent
memory patterns at the aperture's first, middle and last cache lines. The core
smoke option boots hand-encoded RV32 instructions through ITCM into DDR and
checks core loads, stores and byte preservation:

```sh
python3 fpga/aws/ddr/host/run_memory_smoke.py \
  --execute-hardware --core-smoke \
  --expected-agfi "$APPROVED_IMAGE" --expected-shell "$APPROVED_SHELL" \
  --slot 0 --timeout 30 --report /path/to/ddr-memory-smoke.json
```

Omitting `--execute-hardware` performs preflight only. This runner uses the same
slot lock and bounded reset cleanup as model inference. It overwrites its test
windows and core fixture regions, so run it before loading model inputs. Client
out-of-bounds rejection is explicitly software-only evidence. Memory smoke wall
times are transport diagnostics, not model TTFT or token throughput.

The JSON report includes generated IDs, EOS/length stop reason, file hashes,
per-prediction comparison results, load/readback wall time, execution wall time,
and firmware 64-bit cycle counters for prefill, decode, first token and total.
Seconds and decode tokens/second use the wrapper's nominal 50 MHz core clock;
physical timing remains unqualified until the image passes implementation and
hardware gates. Decode counts exclude the first token produced by prefill.
The default result file is the report path with suffix `.logits.f32`.
Once bounded output readback is available, that binary is saved before output
validation, including on failed logit comparisons, nonfinite values or token
mismatches. Failure reports retain generated IDs, available prediction
diagnostics and raw `firmware_cycles`; derived timing requires valid completion
and counters. `physical_execution: COMPLETED` records observed core halt and
does not imply that firmware or numerical validation passed.

```sh
python3 -m unittest discover -s fpga/aws/ddr/host -p 'test_*.py' -v
```

These tests use fake devices and generated ELF fixtures. They establish host
protocol, bounds, corruption, reference and timeout handling only. They provide
no full-model numerical, Coral simulation, timing or physical FPGA evidence.
