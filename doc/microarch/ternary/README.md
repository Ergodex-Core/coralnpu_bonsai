# Ternary SIMD and systolic design recovery

**Design and sizing draft. No new engine RTL, FPGA build or deployment.**

Recommend evaluating **two 128-wide dot engines at the existing 25 MHz**,
with a four-engine expansion gated on measured memory, scaling and routing.
Eight engines are not justified. This is a conditional architectural choice;
there is no matched Bonsai INT8 whole-model measurement supporting a promised
2x or 5x end-to-end gain yet.

## Recovered work

The [provenance index](recovered/provenance.json) binds byte-identical snapshots
to commits in `Ergodex-Core/bonsai_on_chip`:

- `3c74aa0c`: [16x8 prefill mesh and folded decode proposal](recovered/ternary-systolic-array-3c74aa0c.txt).
- `3c74aa0c`: [design review](recovered/array-review-3c74aa0c.txt), including the
  correction that completed banks retain dots, scales and identity under
  backpressure, and can be drained/released while BUSY.
- `aa69172d`: [updated HBM proposal](recovered/ternary-systolic-array-aa69172d.txt)
  and [independent design review](recovered/hbm-review-aa69172d.txt).

Relative source links inside these preserved text snapshots refer to the
original repository, not this checkout. Their historical reviews cover design
documents, not implementation, FPGA feasibility or this new sizing proposal.
The earlier “32-unit SIMD” discussion has not been located as a source-bound
artifact. Width32 is evaluated here as a candidate, not relabeled as recovered
working RTL.

The implemented reference is the serial
[DOT128](https://github.com/Ergodex-Core/bonsai_on_chip/blob/8bbf3a5a/hdl/verilog/first_slice/dot128.sv):
128 MAC cycles per group, a single job mailbox, and one physical memory read
outstanding in the sealed store. PR2 was still draft/open/unmerged when checked
on 2026-10-13. Its actual-weight simulator/FPGA/driver/mailbox and reviewed merge
gates remain separate from Qwen inference on Alex's image.

## Checkpoint and numerical boundaries

Three formats must remain distinct:

| Input | Group representation | Actual evidence |
| --- | --- | --- |
| First-slice Bonsai1.7B Q1_0 | 18 bytes: FP16 scale +128 binary signs | Canonical image SHA and394 native groups rechecked |
| Ternary Bonsai1.7B PQ2_0 | 34 bytes: FP16 scale +128 two-bit codes | Full file SHA, full code census and394 native groups rechecked |
| Directed ternary2 fixture | 32 bytes;00=0,01=+1,10=-1,11 invalid | Synthetic test format only |

Existing [Coral PR4](https://github.com/Ergodex-Core/coralnpu_bonsai/pull/4),
head `97697ba1a8a3af92ebcb055952c58af1fa4d03b3`, identifies native
`prism-ml/Ternary-Bonsai-1.7B-gguf` revision
`983b5dec2ff16aab79990711ba0f828a499a7e6a`. Its expected file SHA is
`de68ba48a8dacb21979915991e7741b917869d71410a370df951c0c3a237ae50`.
The local463,290,464-byte file matched that SHA. It contains197 PQ2 matrices,
113 F32 vectors,13,436,752 PQ2 groups and456,849,568 packed matrix bytes.
Shapes match the Q1 inventory, including the151669-row head tail.

The full payload census found531,316,607 negative,658,033,822 zero and
530,553,827 positive values, with **zero code3 values**. A ternary-only datapath
can therefore cover this pinned file. The generic PQ2 decoder still defines
code3 as+2: bind admission to this verified file or implement that value; never
silently treat it as the directed fixture's invalid code. Dense packed traffic
is unchanged by the approximately38.26% zero fraction; no sparse speedup is
credited. The27B PTQ/PQ2 candidate remains a separate capacity/transform gate.

PR4's current runtime uses ordered FP32 activation arithmetic and FP32 KV.
Seeded int8 dot equality does not establish equivalence to it. Freeze activation
quantization, scales, rounding and quality criteria before an INT8-model speedup
comparison. On both paths, apply each native group scale before accumulating
groups; preserve the same group order and activation-scale placement.

## Width, engine count and memory decision

Pinned current main `382b5c12030ad8eb74ab8deafb2529301faeab16` has64 dot cells,
four INT8 products per cell:256 products/cycle,6.4 GMAC/s at25MHz at full occupancy.
M1 dispatch contains only64 useful products and has issue/RAW/operand overhead.
The measured four-tile integer loop achieves0.931 GMAC/s including setup and
drain; it is a tiny resident-operand test, not a streaming Bonsai baseline.

| W128 engines | Ideal GMAC/s | Q1 feed GB/s | Native PQ2 feed GB/s |
| --- | --- | --- | --- |
| 1 | 3.2 | 0.45 | 0.85 |
| 2 | 6.4 | 0.90 | 1.70 |
| 4 | 12.8 | 1.80 | 3.40 |
| 8 | 25.6 | 3.60 | 6.80 |

Width64/32 halves/quarters compute and bandwidth per engine. Equal total lane
counts do not prove equal area: more narrow engines change banking, descriptors,
reduction pipelines and activation fanout. Two128 matches Coral's product-slot
count, not its area; four/eight add2x/4x slots while Coral remains present.

Alex's core AXI is128 bits at25MHz:0.4GB/s maximum before stalls. Native PQ2
decode has a **1.142s weight-transfer lower bound** there for every engine count.
At1.6GB/s, two engines are memory-limited to at least0.286s, versus0.537s for one;
four cannot improve that bandwidth bound. Two at80% occupancy need1.36GB/s of
useful PQ2 weights, plus activation/output traffic. Four need2.72GB/s and eight
5.44GB/s. A dedicated512-bit25MHz interface has a1.6GB/s ceiling; full four-engine
PQ2 occupancy requires more than doubling that. The300MHz HBM clock alone does
not establish end-to-end payload bandwidth.

Choose W128 for its exact native group match and useful decode occupancy.
Pipeline the reduction and retain independent in-flight groups. Unlike the
recovered16x8 mesh, this needs no systolic wave fill or folded decode network.
The mesh remains a valid alternative for measured prefill reuse; its isolated
group compute schedule is150cycles for128 MAC steps, before memory/scaling.
No measured area comparison between these designs exists.

For a shared8-token/16-output tile and maxK6144, double-buffered int8 activation
storage needs96KiB; reserve128KiB with banking. Native PQ2 weight panels need
52,224bytes double-buffered; reserve64KiB, plus8KiB result/control storage:
**about200KiB logical local SRAM budget**, not an inferred FPGA block count.
This cannot fit in32KiB DTCM. Broadcast128 activation bytes/cycle across engines;
use independent packed-weight banks and ping-pong ownership. Group-scale/reduce
must sustain E completed groups/cycle or it becomes the throughput limit.

Prefill32, with an8-token reuse tile and final-position head only, needs45.408G
products and1.580GB native PQ2 weight traffic. At0.4GB/s, ideal linear bounds for
1/2/4/8 engines are14.190/7.095/3.950/3.950seconds. They omit attention, quantize,
scale/reduce and outputs. Coral also benefits from fuller M occupancy in prefill.

## Integration contract and acceptance

Use a versioned descriptor interface; leave DOT128 v1 unchanged. A descriptor
needs format/version, image epoch, job cookie, logical tensor bounds, M/N/K,
group size/valid lanes, operand/output addresses and strides, scaling contract,
and owned buffer IDs. Allocate no new MMIO address in this design-only PR.
Alex and the older Ayush HBM bank-switch interface in PR4 are different ABIs;
their addresses, reset/drain rules and images must not be mixed.

The future memory adapter needs burst/line reuse, independent tagged requests,
bounded reorder storage and per-beat error handling. Seal every weight writer,
including host, core, DMA and aliases. Activation/KV buffers remain disjoint and
writable. Preserve epochs and drain late transactions before ID reuse.
The tile completion queue must hold dots, scale bits and job/epoch/group identity
until explicit release; firmware must be able to drain completed banks while
the job remains BUSY. This carries forward the recovered review's correction.

Required counters: useful/masked products, completed groups, read/write bytes,
memory/issue/scale/bank stalls, queue occupancy, failures and completed operator
cycles. Benchmark quantize/unpack/load/MAC/scale/reduce/store separately, then
report complete operators and identical whole-model requests.

End-to-end speedup is `1/(1-p+p/s+d)` for replaced-time fraction p, kernel speedup
s and added overhead fraction d. At p=.8,2x requires s>=2.667 with d=0;5x is only
an infinite-kernel limit. At p=.9,2x needs2.25 and5x needs9. Two engines at75%
efficiency permit the2x/p=.8 target only if the matched replaced baseline kernel
is at most1.8GMAC/s; real bandwidth/scaling can lower this threshold.

Acceptance sequence after sizing approval:

1. Freeze native checkpoint and actual activation/quantization oracle; reproduce
   current INT8-path operator and model outputs with phase counters.
2. Measure useful HBM read bandwidth and overlap through the intended client,
   checksummed and owner-coordinated; no current peak-bandwidth result exists.
3. Specify banking, scale/reduce and descriptor semantics, including backpressure,
   invalid codes, tails, scale lifetime, reset/fault/drain and image identity.
4. Compare CPU and simulator full-operator outputs, then bounded physical native
   operators, layers and model runs on a coordinated future build.
5. Route1/2/4 configurations at25MHz and compare actual resource/timing deltas.
   Alex's existing CL already uses786962LUT; tile state/access uses337499LUT.
   Neither arithmetic simplification nor free BRAM proves routability.

## Reproduction and measured scope

See [analysis/ternary_simd](../../../analysis/ternary_simd/README.md) for the
standalone model, native code census, retained numeric evidence and full analysis.
No throughput from the host Python checks is used as FPGA throughput.

| Check | Status |
| --- | --- |
| Historical source/review hashes | PASS |
| Full native Q1 image SHA and1182 width checks | PASS |
| Full native PQ2 file SHA/census and1182 width checks | PASS |
| Analytical sensitivity and physical receipt extraction | PASS |
| Earlier32-unit source artifact | NOT LOCATED |
| New SIMD RTL/software driver or simulator | NOT IMPLEMENTED / NOT RUN |
| New FPGA build, timing, resources, power or model run | NOT RUN |
| Matched Bonsai activation-INT8 baseline and end-to-end gain | NOT RUN |

This draft is based directly on current main. Open PR1/2/4 form a separate
qualification/DDR/inference stack; PR3 contains Ayush build automation. None is
silently included here. Alex's supplied rebuild recipe has now been inspected
read-only; see the [rebuild review](alex-rebuild-review.md).

## Architecture references

[Apex](https://github.com/apex-compute/unified-engine/tree/d0137dbbdfc5b91186bf80f79628af97c0d7147f) motivates streamed packed weights,
operator fusion and shared activation reuse. Its reported kernel utilization
and model throughput use different devices, clocks and workloads; the detailed
analysis retains those distinctions. They are not Bonsai performance inputs.

[TeLLMe v2](https://arxiv.org/html/2510.15926v2) makes table lookup with shared
activation precomputation, weight-memory banking and fused quantize/dequantize
credible alternatives to a plain dot engine. It also treats prefill and decode
attention separately. Evaluate lookup-table ports, replication and precompute
cost against the same native PQ2 oracle before selecting that datapath. Its
BitNet W1.58A8 results do not establish Bonsai activation equivalence.

[TerEffic](https://arxiv.org/html/2502.16473v1) reinforces that on-chip weight
residency and HBM-assisted scheduling are different regimes. Its reported
accelerator scope excludes embedding, position encoding and output layers;
our workload explicitly includes the large output head. Its throughput and
power figures cannot be transferred to this image.
