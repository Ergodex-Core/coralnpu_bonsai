# Lossless packing and physical measurement handoff

These are executable host packing experiments and candidate scalar firmware
kernels. **No new FPGA numbers have been collected.** Packing changes weight
representation only: native PQ2 codes and every FP16 scale bit round-trip exactly.
Activation quantization is unchanged and remains a separate numerical-contract gate.

## Candidates and byte accounting

Native PQ2 has 128 two-bit codes and a two-byte scale per 34-byte group.
Slabs store row tiles in `(row_tile, K_group, row_within_tile)` order, sharing
one activation group across the output rows. Each slab stores all code bytes,
then a 64-byte scale block. Weight and scale bases are aligned to 64 bytes.
The 32-row PQ2 slab is 1,024 code bytes plus 64 scale bytes: 17 bus beats.
The 16-row slab wastes 32 bytes of scale padding and is a comparison candidate.
Do not pad every native group to 64 bytes; that nearly doubles traffic.

Radix-3 stores five ternary codes per byte: 26 bytes per 128 weights, then the
unchanged scale. Two unused trits in its last byte are canonical zero-weight
codes. The 32-row slab is 832 code bytes plus 64 scale bytes: 14 beats.
This is lossless for the verified checkpoint, but rejects code3 (+2). It is not
a generic replacement for PQ2. Its division/table-lookup, unpack and reduction
cost must be measured before adopting it. No sparse speedup is credited.

| Representation | Model body bytes | Complete proposed bundle bytes | Change vs native payload |
| --- | ---: | ---: | ---: |
| Native34 | 456,849,568 | 456,878,080 | +0.0062% |
| Each group padded64 | 859,952,128 | 859,980,608 | +88.24% |
| 16-row PQ2 slab | 483,729,408 | 483,757,888 | +5.89% |
| 32-row PQ2 slab | 456,855,552 | 456,884,032 | +0.0075% |
| 32-row radix-3 slab | 376,233,984 | 376,262,464 | -17.64% |

These are exact shape-derived sizes, not physical traffic measurements. Totals
include all 197 matrices, masked output-head rows, 128-byte tensor headers,
64-byte tensor-end alignment and a 3,264-byte bundle header/directory allowance.
The bundle allowance is specified, not yet implemented as a bundle serializer.
Tensor files are implemented. `layouts.py` defines the format and validates
hashes, header bounds, padding and exact native reconstruction. Shape/control
metadata is outside the hot data path after loading; count it separately on
cold loads. Scales are always included in streamed body bytes.

## Host evidence and reproduction

```bash
python3 analysis/ternary_simd/packing/experiment.py \
  --weights /path/to/Ternary-Bonsai-1.7B-PQ2_0.gguf \
  --fixtures-out /tmp/bonsai-packing-native \
  --report /tmp/packing-native.json
```

Omit `--weights` for deterministic synthetic vectors, including an output-row
tail. The script verifies the full pinned model SHA, then selects the actual
`blk.0.attn_output.weight` 2048x2048 tensor. All five layouts round-trip exactly.
Portable C decoders are independently checked against Python's native unpack
in logical-row and physical-slab traversal orders. Synthetic tests cover N=1,
16,31,32,33, scale-bit preservation, corrupt data and all four native codes.
No model payload is committed. The generated fixture directory is the handoff
input; its native and expected-output hashes are in the retained report.

[Retained host results](../results/packing-native.json) include file/body/output
hashes and complete byte counts. Host runtime is never used as an FPGA timing.
`kernels.c` compiled and passed the host checks. Local Apple Clang cannot target
RV32, so the firmware compile is **BLOCKED_TOOLCHAIN**, not passed. The owner
must use the existing tested `clang-18` RV32 firmware toolchain and linker/startup
adapter; these source files are not a validated firmware ELF.

## Current-image microbenchmark proposal: coordinate before execution

Use Alex's already loaded 25 MHz image only during a parent/owner-approved quiet
window. Do not reload an AFI, reset hardware, or claim unowned HBM addresses.
Ask the owner to allocate two disjoint, 64-byte-aligned scratch ranges plus
canaries, at most 32 MiB each, inside the existing 1 GiB aperture. The input is
one body per layout, without the 128-byte header. Destination capacity must cover
4,259,840 bytes for the selected tensor's expanded output. Host BAR4 translation
is a loader responsibility; the probe accepts core addresses only.

`probe.c` defines a candidate mailbox: words 0..11 contain magic `0x42505132`,
version1, operation, layout, traversal order, N,K, source address/bytes,
destination address/capacity and cookie. Words12..23 report status, kernel rc,
start/end/elapsed 64-bit core cycles, checksum, output bytes, cookie and expected
25 MHz clock. Unconfigured/invalid descriptors fail without payload operations.
This is a benchmark-local adapter, not the new engine or DOT128 production ABI.
The owner must adapt their established loader/startup; do not assume its existing
mailbox or termination convention matches this candidate. Preserve its exit code.

Run one bounded small smoke case before the full tensor. Cases are:

1. Empty fenced timing interval, recorded separately without silently subtracting.
2. Aligned read+checksum of each body (scalar issue/checksum included).
3. Aligned copy of each body, complete host byte comparison after timing.
4. Decode to 128 signed bytes plus two untouched scale bytes per native group,
   in logical-row and physical-slab orders; compare every output byte with the
   generated oracle and verify guard bytes. Partial writes on error are invalid.

Record first-run and at least three steady samples, with min/median/max. Exclude
host upload/download from kernel timing but record them separately. No concurrent
quiet-prompt timing or openTPU CPU work without the owner's agreement. Set a
bounded host timeout and stop on mismatch/error; recovery remains owner-controlled.
Run no giant sweep before the first receipt is reviewed.

Use unsigned high-low-high `mcycle` reads and a final fence, retaining start/end
and elapsed values. Convert elapsed cycles at the verified 25 MHz image clock.
Read+checksum counts one input stream. Copy counts input bytes for useful copy
bandwidth and input+output for total payload traffic. Decode counts actual
layout bytes read and expanded output bytes written, including scales. The probe
has no AXI beat counter: these are logical payload bytes, **not measured bus beats**.
Native-equivalent GB/s uses original34-byte-group bytes, clearly distinguished
from compressed bytes/s. Inspect compiler output; scalar code does not establish
DMA or HBM peak bandwidth. These kernels do not perform model inference.

## Result gate and physical inference acceptance

| Experiment | Correctness | Core cycles / seconds | Useful GB/s | FPGA status |
| --- | --- | --- | --- | --- |
| Native34 read/copy/unpack | Host PASS; FPGA pending | Pending | Pending | NOT_RUN |
| Padded64 read/copy/unpack | Host PASS; FPGA pending | Pending | Pending | NOT_RUN |
| Slab16 PQ2 read/copy/unpack | Host PASS; FPGA pending | Pending | Pending | NOT_RUN |
| Slab32 PQ2 read/copy/unpack | Host PASS; FPGA pending | Pending | Pending | NOT_RUN |
| Slab32 radix3 read/copy/unpack | Host PASS; FPGA pending | Pending | Pending | NOT_RUN |
| Dedicated-client DMA/banked SRAM | Pending | Pending | Pending | REQUIRES_NEW_IMAGE |
| 2/4/6/8 ternary or lookup engines | Pending | Pending | Pending | REQUIRES_NEW_IMAGE |
| Prefill TPS / TTFT / decode TPS / total latency | Pending | Pending | N/A | REQUIRES_COMPLETE_RUNTIME_AND_FPGA_RUN |

Every physical receipt must bind source commit, compiler/options and ELF SHA,
model SHA, layout/version and body SHA, activation/oracle SHA, expected/actual
output SHA, requested and completed byte counts, cycles, verified clock, DCP/AFI
identity and loaded slot observation, prompt/context/batch, and concurrency state.
Keep raw receipts and failed runs. Populate numeric columns only from those
physical receipts. Existing Qwen or resident-matrix receipts do not fill this table.

The following implementation gate is a burst-capable independent HBM client,
tagged multiple requests, finite response buffering, banked ping-pong SRAM and
activation reuse. Keep core25/shell250/HBM-AXI300 MHz and the existing mapping.
A 512-bit25 MHz compute-side path caps at1.6 GB/s; two PQ2 engines at80% occupancy
need1.36 GB/s useful weights before other traffic. Measure the actual arbitration,
CDC, banking and scale/reduce path; do not infer bandwidth from the HBM clock.
A streamed slab buffer can be much smaller than full double-buffered K panels,
but must tolerate measured latency. The original200 KiB N16 allocation is not
a verified N32 full-panel allocation: doubling N also doubles weight-panel storage.

New engines cannot be evaluated on the current image. After RTL simulation and
reviewed synthesis/routing/timing/CDC signoff, coordinate build resources, image
creation/identity, physical qualification and slot windows with the parent.
No promise is made that all 2/4/6/8 variants fit or route. Physical kernel correctness
and bandwidth precede full-model integration. Where a complete model runtime
exists, measure matched prefill TPS, TTFT, decode TPS and end-to-end latency on
FPGA, with the same inputs/numerics and explicit timing boundaries. Standalone
Arcsim/RTL kernels cannot substantiate those inference metrics.
