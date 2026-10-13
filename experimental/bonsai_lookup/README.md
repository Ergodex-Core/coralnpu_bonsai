# Standalone Bonsai group matmul: direct SIMD and shared lookup

**Executable RTL draft, not a complete Coral accelerator or FPGA result.**
This directory compares two independently implemented 128-lane group-dot paths
at 2/4/6/8 engines. `LOOKUP=0` uses sign/zero selection and a balanced adder tree.
`LOOKUP=1` uses [TeLLMe-inspired](NOTICE.md) shared activation-triple tables and
a balanced reduction. The same bounded feeder, operands and memory service are
used for both. The independent scalar oracle checks every result and scale bit.

The intended result is `M x N` integer group dots for one stored K=128 group,
with up to eight tokens and 32 output rows. Larger K requires ordered group
scaling/reduction in a later integration. **No FP32 scale multiplication,
activation quantization, attention, output HBM stores, full-model runtime,
production AXI/DMA adapter, banked SRAM implementation, or ping-pong overlap is
claimed.** Existing DOT128 ABI and Coral RTL are untouched.

## Arithmetic and layout

Activations are signed int8, including -128. Native PQ2 codes are 00=-1,
01=0,10=+1. Code11 means +2 in generic PQ2: this ternary candidate explicitly
flags an active occurrence, returns zero for that engine's dot, and makes the
job fail. Masked lanes/rows cannot raise this error. The pinned native checkpoint
has no code11; admission must remain bound to that verified checkpoint until
+2 support is deliberately added. Nothing silently requantizes a weight.

`job_lanes` may be 0..128; inactive activation/weight lanes contribute zero.
A triple partial is signed10 bits, covering -384..384. Balanced tree nodes are
signed16 bits, sufficient for the exact group bound -16384..16384. Outputs are
sign-extended int32, with no saturation or wrap at this bound. The scale's exact
FP16 bits accompany each dot without conversion. Per-group FP32 scaling and
ordered accumulation remain a separate shared oracle/implementation requirement;
integer kernel equality does not establish equivalence to FP32 model activations.

The lookup path registers 43 tables of 27 triple sums when an activation row is
accepted. Entries use `c0 + 3*c1 + 9*c2`; the last triple substitutes a zero
activation/code01 for its missing element. Tables are shared by all engines.
This represents 11,610 logical table bits before mapping and replication; it is
not a measured LUT/BRAM count. Direct reduction has seven adder-tree levels;
lookup has a table-select path plus six reduction levels and a separate table
construction cycle. No routed critical-path or Fmax claim follows from this.

The feeder consumes exactly one slab32 PQ2 group: first1,024 code bytes, then
64 scale bytes, in row order. This is losslessly derived from native weights.
Activation rows follow at their separate base, 128 bytes each. Scales are included
in the 1,088 weight bytes. One tile fetch reuses weights over M activation rows.

## RTL interface and reset contract

Ordered source files:

1. `rtl/bonsai_dot_cluster.sv`
2. `rtl/bonsai_matmul_tile.sv`

Top: `bonsai_matmul_tile`; parameters `ENGINES=2/4/6/8`, `LOOKUP=0/1`.
All ports are ordinary packed vectors. Reset is active-low, synchronous to the
rising edge. Apply at least16 reset cycles in the benchmark. There are no
black-box IP dependencies. The bundle generator emits one flattened file and
unique wrapper top for each variant, avoiding parameter-CLI ambiguity.

| Interface | Contract |
| --- | --- |
| Job | Accept `job_valid && job_ready`; M=1..8,N=1..32, valid lanes=0..128, two16-byte-aligned byte bases and32-bit tag. Hold until accepted. One active job. |
| Read request | `rd_valid/rd_ready`,32-bit byte address,4-bit ID, fixed16-byte read. Hold request stable until accepted. At most16 live IDs. |
| Read response | `rsp_valid/rsp_ready`,128-bit little-endian data, matching ID and error bit. Responses may reorder; producer holds until accepted. No same-cycle response to a newly allocated ID. |
| Output | `out_valid/out_ready`; E signed32-bit dots, E16-bit scale values, masks/errors, job tag, token and first row. Engine0 is least-significant. All payload held under backpressure. |
| Completion | `done` pulses after final output handshake; `job_error` remains until next accepted job. Invalid job shape/address immediately completes with error and no reads. |
| Counters | Cycles from accepted job to completion, load-phase cycles, and16 bytes per accepted valid response. Counter meanings exclude reset. |

IDs map responses to a 68-beat weight slab and M eight-beat activation rows.
A request slot is not reused before its response is accepted. Response errors
are drained, flag every returned active result, and fail the job. Invalid-code
errors flag only that output row and accumulate into job status. Unknown response
IDs fail the job status path but cannot be counted as valid data; the harness's
10,000-cycle limit catches missing real responses.

Reset cancels the bounded job and all local IDs. **The external harness must
flush the old memory epoch before restarting.** This is not a production
reset/drain solution for an AXI fabric carrying late responses. Production work
must add explicit epoch/drain semantics before integration.

## Common simulation protocol and measured scope

The local test uses one aggregate16-byte beat/cycle, response latency8 cycles,
16 outstanding IDs, request/output ready for cycle modulo16 in0..11, and
highest-eligible-ID responses to exercise reordering. Weights and activations
are completely loaded before compute. That intentional cold-tile baseline
exposes loading cost; it does not demonstrate DMA/compute overlap or the wider
memory path required by PR5. Outputs use ready/valid and are not HBM writes.

Both paths currently have the same registered group-output schedule. The feeder
issues a batch then waits for its output, so ideal sustained cluster acceptance
is not the achieved feeder rate. Changing datapath structure alone cannot
improve these cycle counts; area/timing and a decoupled feeder remain unmeasured.

| Engines | Decode M1,N32,K128 cycles | Load cycles | Prefill-like M8,N32,K128 cycles | Load cycles |
| --- | ---: | ---: | ---: | ---: |
| 2 | 151 | 109 | 521 | 181 |
| 4 | 131 | 109 | 353 | 181 |
| 6 | 123 | 109 | 309 | 181 |
| 8 | 119 | 109 | 268 | 181 |

These are **Icarus event-driven RTL simulation measurements**, equal for the
direct and lookup paths. Decode reads1,216 bytes; M8 reads2,112 bytes. They use
actual trained weights from one native matrix and seeded synthetic activations.
They are neither Arcsim nor FPGA measurements, and M8 is a matmul reuse case,
not measured model prefill. See `results/rtl-simulation.json` for exact values,
source hashes, capacity utilization and phase costs. Any25MHz time conversion
there is modeled time only; no inference tokens/s is reported.

## Reproduce and hand off

Generate the native tensor with PR5's `packing/experiment.py`, then:

```bash
python3 experimental/bonsai_lookup/tests/make_fixtures.py \
  --native-tensor /tmp/bonsai-packing-native/native.bin --out /tmp/bonsai-fixtures
python3 experimental/bonsai_lookup/tests/run_tests.py \
  --fixtures /tmp/bonsai-fixtures --report /tmp/rtl-simulation.json
verilator --lint-only --top-module bonsai_matmul_tile \
  experimental/bonsai_lookup/rtl/bonsai_dot_cluster.sv \
  experimental/bonsai_lookup/rtl/bonsai_matmul_tile.sv
```

The generator checks the tensor hash against PR5's full-model-bound receipt.
It extracts the first native K group from each of32 actual output rows and keeps
all scales. Each fixture includes full expected output values, not just a hash.
Separate cases cover native decode/reuse/tails, signed bounds, zero weights,
zero valid K, reserved codes, memory errors and reset cancellation/restart.

Use `tests/build_bundle.py` after committing to prepare the sanitized Library
handoff. It includes per-file hashes, source revision, fixed tops, protocol,
independent oracle and actual-weight fixture identities. Generate a fresh
`model.hpp` and `state.json` for **each** top on the existing authorized Arcsim
host. Use its own generated wide-signal access API, not assumed C++ storage.
Full Coral compilation remains a separate blocked task. No remote run is made
by this source-owner task.

## Physical FPGA acceptance: pending

Arcsim outputs must remain labeled standalone simulation. Before dispatch,
the parent and Arcsim owner coordinate host ownership, resource limits and the
existing lock; before physical builds/tests, the parent and image owner agree
build resources, licensed tool availability, AFI identity and slot windows.

| Gate | Status |
| --- | --- |
| Local exact-value RTL tests and source lint | PASS:72 exact-value cases; Verilator lint |
| Arcsim compile/model generation and2/4/6/8 runs | NOT_RUN by source owner; handoff required |
| Synthesis, resources, routing, timing and CDC for each variant | NOT_RUN |
| Qualified image and physical kernel correctness | NOT_RUN; requires new image |
| Actual useful memory bandwidth and DMA overlap | NOT_RUN |
| Physical prefill TPS, TTFT, decode TPS and end-to-end latency | NOT_RUN; requires complete matched runtime |

The current Alex image cannot validate added hardware. Build and qualify suitable
images only after simulation and signoff. Do not assume all counts fit or route.
Each result must bind RTL/source, compiler, firmware ELF, model, layout and oracle
hashes, DCP/AFI identity and observed slot, clocks, exact outputs, bytes and cycle
boundaries. Failed variants remain failed/pending instead of disappearing.

First validate physical group/operator correctness and real memory bandwidth.
Then integrate the same quantization and ordered scale/reduction as the existing
INT8 baseline. Only a complete model path can supply inference numbers: matched
prompt/context/batch, prefill token count and timing, TTFT including agreed setup,
steady decode TPS, and complete request latency. Report reset/loading separately
and disclose concurrency. Standalone dot tiles cannot substantiate model TPS.
