# Ordered scaled-projection RTL candidate

`bonsai_scaled_tile` composes the unchanged integer tile with
`bonsai_group_reduce` and a group sequencer. It covers M1..8,N1..32 and G1..48
complete K128 groups. Sweep row tiles and token tiles to execute the complete
projection shapes in the Library fixtures. This is standalone simulated RTL,
not a production Coral memory client, FPGA image, or complete inference runtime.

## Numerical contract

For each token/output row, initialize the accumulator to positive FP32 zero.
For each K group in increasing order, convert the bounded integer dot exactly,
widen the native FP16 scale exactly, and perform three distinct RNE operations:

```text
p = RN32(float32(dot) * float32(native_FP16_weight_scale))
q = RN32(p * activation_scale_f32)
y = RN32(y + q)
```

The reducer instantiates one existing `fpnew_fma` arithmetic unit configured for
FP32 with four distributed pipeline registers, as in the frozen Alex design.
This instantiates an additional dedicated unit; it does not arbitrate or reuse
the existing physical Coral scalar FPU. It issues MUL, MUL and ADD separately; despite the unit's name, these operations
are not fused. The integer converter admits only -16384..16384, and FP16 widening
includes signed zero and subnormals. Weight scales must be finite; activation
scales must be finite and strictly positive, including positive subnormals.
The arithmetic unit preserves IEEE overflow, underflow, NaN and rounding behavior;
its five RISC-V exception flags are ORed across all groups for each output.
Exceptions are reported rather than silently clamped. Positive test scales do
not establish the quantizer or quality of an actual Bonsai A8 inference path.

Group, token and row identities are checked against the expected serialized
sequence. A bad identity, input error, out-of-range dot or rejected scale emits
canonical quiet NaN and marks the command failed. Fault status remains sticky
until the next accepted command. Consumers must discard the complete failed
command, including any results emitted before an error became known.

The register accumulator payload is 1,024 bytes plus 160 bytes of exception flags.
These are logical state sizes, not mapped BRAM, LUT, area or power measurements.
The same unit is shared by all dot engines. This deliberately serial first
integration closes the arithmetic boundary; it is not a throughput-optimized
reducer. No sustained E dots/cycle assumption applies to this composed top. Even a future
fully interleaved scheduler on this single arithmetic unit has a conditional
ceiling of one group update per three cycles if it issues one FP operation per
cycle: each update requires two multiplies and one add. That is an architectural
issue-rate bound, not measured routed throughput. Increasing integer engines
alone cannot remove this bottleneck; any wider or faster reduction path must
justify its extra arithmetic resources, state ports and clock crossings.

## Input layout and protocol

Keep the original group's tagged 16-byte read/response interface. Commands supply
M,N,G,tag and three 16-byte-aligned byte bases. Reject invalid dimensions or address
wrap before requesting memory. Layouts are:

| Region | Order and size |
| --- | --- |
| Weights | G slab32 records, 1,088 bytes each; 32 x 32 code bytes then 32 x 2 FP16 scale bytes |
| Activations | G contiguous M x 128 signed-int8 bytes |
| Activation scales | G records of 32 bytes, eight little-endian FP32 slots; slots M..7 unused |

The wrapper fetches the two scale beats sequentially, then starts the existing
integer tile. The integer tile's returned engine batch is serialized into the
shared reducer. It starts the next group's loads after the final integer batch
is accepted; the reducer retains each FP32 accumulator across all K groups.
Only final-group values appear at the wrapper output. Each output carries token,
row,tag,error and accumulated exception flags and stays stable under backpressure.
`done` follows consumption of the last result. There are no HBM output stores.

A returned scale ID must match the outstanding scale request. Response errors
and reserved active PQ2 codes fail the command. As with the earlier standalone
top, reset requires the external memory service to flush its old response epoch.
No timeout/recovery or production AXI transaction drain is implemented; a missing
response or absent output consumer can stall indefinitely. The test harness has
a bounded cycle deadline. Source-owner work does not claim this as a deployable
reset/abort mechanism.

## Reproducible validation and timing boundary

Supply the seven external RTL files whose exact hashes appear in
`results/fpnew-dependencies.json`, from Alex's frozen `input/core/rtl` directory.
They are used in place; no copies of third-party IP are committed here. Their
source licenses and original provenance remain applicable. The build script
checks every dependency hash before compiling and limits local C++ builds to two
jobs. Only known width/ascending-range diagnostics in the pinned external files
are waived; diagnostics in new RTL remain fatal.

```bash
python3 experimental/bonsai_lookup/tests/scaled/build.py \
  --deps /path/to/alex/input/core/rtl --out /tmp/bonsai-reducer --reducer
/tmp/bonsai-reducer/Vbonsai_group_reduce
python3 experimental/bonsai_lookup/tests/scaled/run.py \
  --deps /path/to/alex/input/core/rtl \
  --fixtures /path/to/bonsai-layer-fixtures --out /tmp/scaled-validation
```

Repository-wide CI does not automatically compile these standalone tops; the
explicit suite below supplies their executable validation. The suite verifies
complete fixture hashes before running. Its primary two-engine
lookup variant executes every output of all 14 actual projection-shape fixtures.
All eight variants execute complete Q decode and M32 projections. This harness
repackages each entire K dimension into the declared lossless tile layout and
checks every output against the previously generated independent C reference.
Host packing and oracle generation are outside simulation timing.

The deterministic service is 16 bytes/cycle aggregate, eight-cycle response
latency, up to 16 outstanding reads, request/output ready for cycle modulo 16
less than 12, and highest eligible ID response priority. Result backpressure is
checked. Reducer tests exhaust the bounded integer range and all finite FP16
scale patterns, plus random positive finite FP32 activation scales, ordered
rounding, exception accumulation, invalid input and reset/restart cases.

Projection counters measure accepted command through completed output consumption;
the driver separately reports one command-issue cycle per tile. Whole projection
cycles are the sum of actual executed tile counters, with actual output checks,
not multiplication of a sampled tile. Initial reset, host packing, a production
command/mailbox path, AXI arbitration/CDC and HBM output stores are excluded.
Simulation memory service is a declared model, not measured AWS HBM bandwidth.
Arcsim must compile this new top and reproduce these results independently before
its scaled-projection status can change from NOT_RUN. Prepare unique parameterized
tops on the owner's authorized machine with:

```bash
python3 experimental/bonsai_lookup/tests/scaled/prepare_arcsim.py \
  --deps /path/to/alex/input/core/rtl --out /tmp/scaled-arcsim
```

The script verifies dependencies and emits one complete source per fixed top,
with names/hashes in `variants.json`; it does not dispatch anything. Compile
`bonsai_scaled_e2_l1` first and adapt the C++ memory/output protocol from
`projection_tb.cc` to that model's generated access API. Use the existing Library
projection fixtures. Verify a complete Q decode before scaling the workload or
compiling other variants. Preserve the generated model/state/compiler hashes,
exact checked outputs, total counters, command issue cycles and memory service.
Compilation failure remains a blocker, not an excuse to use host arithmetic in
place of the FP unit. FPGA metrics remain NOT_RUN.

## Measured complete-projection simulation results

For the two-engine lookup candidate, every output of each complete projection
matches the independent FP32 reference. Cycles below include scale reads,
integer computation, both multiplies, ordered accumulation and output consumption.
They exclude the separately counted one-cycle command issue per tile and the
production costs listed above.

| Projection | M1 cycles | M32 cycles | M1 / M32 commands |
| --- | ---: | ---: | ---: |
| Q | 681,403 | 18,638,586 | 64 /256 |
| K | 340,699 | 9,319,290 | 32 /128 |
| V | 340,699 | 9,319,290 | 32 /128 |
| Output | 681,403 | 18,638,586 | 64 /256 |
| Gate | 2,044,219 | 55,915,770 | 192 /768 |
| Up | 2,044,219 | 55,915,770 | 192 /768 |
| Down | 2,041,275 | 55,863,034 | 64 /256 |

For Q, accepted read bytes occupy about 11.72% (M1) and 2.94% (M32) of the
modeled 16-byte/cycle channel capacity, computed from the recorded byte/cycle
counters. This is simulated channel occupancy, not measured HBM efficiency.
The serial arithmetic schedule limits this candidate before the ideal dot-engine
roofline; the memory model is not saturated in these runs.

All eight direct/lookup variants at 2/4/6/8 engines produce identical complete Q
cycle counts above: 681,403 for M1 and 18,638,586 for M32. All 28 complete-projection
runs pass, checking 1,148,928 final FP32 values. Nine protocol cases and 144,291
reducer stress updates also pass. Thus this shared-reducer candidate demonstrates
no cycle benefit from increasing the dot-engine count; it does not establish a
2x/5x end-to-end speedup over Coral.

The full report is `results/scaled-simulation.json`; scalar arithmetic stress
checks are in `results/reducer-simulation.json`. None of these results is a
physical FPGA latency or model inference measurement. The remaining Arcsim and
FPGA gates are separate from this local Verilator pass.

## Qualified candidate and shared-resource plan before synthesis

The candidate is qualified only by the standalone functional tests above; it
is not ready for a full FPGA image build. It uses two 128-lane lookup engines
plus one shared FP32
reducer at the existing 25 MHz core target. Direct SIMD at the same count remains
the comparison candidate. Four/six/eight engines are simulation comparisons;
no count is assumed to fit, route or improve end-to-end performance. The serialized
reducer is a baseline for locating stalls, not evidence of the requested 2x/5x gain.

Before any heavy build, the parent/image owner must approve one named build host,
its license/tool versions and storage, maximum parallel jobs/RAM, and the existing
shared-host lock window. Alex's rebuild README reports about 22 GB peak for the
main Vivado process and recommends 64 GB RAM plus at least 20 GB free scratch;
its launcher uses four Vivado threads and two synthesis jobs. Use those as the
initial reservation request, subject to the owner's confirmed available capacity,
and retain one build at a time. These historical numbers do not bound the added
engine/reducer build's peak; stop and review resource pressure before increasing
parallelism. Start with one candidate build and retain its raw logs;
do not launch concurrent variant builds or touch the FPGA slot. The source owner
supplies RTL hashes, exact FPnew dependencies, test report, descriptor/layout
contract and candidate source revision. The image owner supplies Alex's verified
wrapper/HDK/IP/shell inputs, complete portable audit material and tool/license
availability. Missing portable root verifier/checksum material remains a blocker.

A buildable physical candidate still requires the production memory adapter,
transaction-ID allocation, arbitration, CDC, epoch/drain recovery and output-store
path in `production-integration.md`. Qualify that memory path separately before
combining it with the scaled tile. Replace the current register staging with a
reviewed bank/port plan and account for the added FP unit, exception state and
queues. Do not infer area/Fmax from the source or simulator memory use.

After synthesis, review mapped resource deltas for the complete integrated top.
After place/route, require 25 MHz timing closure, CDC/reset review and unchanged
shell/HBM constraints; Alex's prior+0.032 ns WNS does not qualify this change.
Only then may the parent schedule image creation/loading and an exclusive quiet
measurement window with the FPGA/Qwen and openTPU owners. All FPGA lifecycle
operations and physical testing remain with the coordinated image/host owners.
