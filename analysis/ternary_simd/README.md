# Reproducible Bonsai sizing analysis

See the [decision and integration proposal](../../doc/microarch/ternary/README.md).
This directory contains a Python standard-library analytical model and native
weight checks, not engine RTL or a simulator. Rates count one product and its
accumulation as one MAC-equivalent. No host Python timing is FPGA performance.

Run from the repository root:

```bash
python3 analysis/ternary_simd/sizing.py
python3 analysis/ternary_simd/test_analysis.py
python3 analysis/ternary_simd/sizing.py --weights /path/to/Q1-native/weights.bin
python3 analysis/ternary_simd/inspect_pq2.py /path/to/Ternary-Bonsai-1.7B-PQ2_0.gguf --output /tmp/pq2-inventory.json
```

The first command overwrites generated results with synthetic arithmetic checks
when weights are omitted. The third verifies the canonical Q1 image hash and
checks394 actual native blocks at widths32/64/128. The fourth scans every native
PQ2 code and checks first/last blocks from every PQ2 matrix; it reports the actual
SHA for comparison with the pinned target, not an automatic claim of model
equivalence. Model weights are not committed.

## Inputs and outputs

- `sources/bonsai-manifest.json`: canonical source-bound Q1 tensor inventory.
- `sources/matrix-receipts.json`: numeric-only extraction of12 passing physical
  Alex-image cases; host/AWS identities and raw device logs omitted.
- `sources/qwen-comparison.json`: Qwen-only phase context, not a Bonsai baseline.
- `sources/provenance.json`: source/image/input identities.
- `results/candidates.json`: widths32/64/128, engine counts1/2/4/8, Q1 and PQ2
  bandwidth sensitivity, ideal compute and roofline bounds.
- `results/workload.json`: source-derived shapes and decode/prefill work.
- `results/targets.json` and `amdahl.json`: explicit end-to-end sensitivities.
- `results/measured-matrix.json`: physical microbenchmark timing boundaries.
- `results/numerical-checks.json`: retained actual Q1 arithmetic check.
- `results/pq2-inventory.json`: actual PQ2 SHA, shapes, code census and dot checks.
- `DECISION.txt`: detailed initial Q1 context analysis, including Apex reference
  distinctions, bottlenecks and measurement requests; the native PQ2 decision in
  the linked proposal supersedes its initial target-selection/publication scope.

Only compute and packed weight transfer are included in rooflines. Perfect
overlap, pipelined group initiation, enough bank ports and local operand reuse
are assumptions. Quantization, scale/reduce, attention, output bytes and stalls
must be measured and added. Use sums rather than max when overlap is absent.
PQ2 workload bounds use the same matrix shapes, confirmed by the full inventory;
they do not reuse Q1 values or claim checkpoint numerical equivalence.

The13MB/s sensitivity approximates a weakly checked sequential copy diagnostic,
not a measured HBM read ceiling. Higher bandwidths are conditional scenarios.
Changing the model never changes the clock of the physical image.

## Validation scope

`test_analysis.py` checks the full four-code PQ2 mapping using an independently
constructed GGUF fixture, including code3=+2, and verifies every recovered design
snapshot against its provenance hash. Actual-file runs additionally verified
the pinned Q1/PQ2 payloads and2364 partitioned integer dot comparisons total.
These checks establish neither native activation quantization nor new hardware.

The draft is isolated from open PR1/2/3/4. It changes documentation and analysis
only. New RTL, driver, simulator, synthesis, FPGA execution and end-to-end Bonsai
comparison are explicitly not run. Repository lint/macro outcomes are recorded
in `results/validation.json`.

See [packing experiments](packing/README.md) for lossless layouts, actual-weight
host checks, candidate current-image firmware and the physical FPGA acceptance
plan. Host results and shape-derived byte counts are not FPGA measurements.
