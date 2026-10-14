# Independent HBM ROM-equivalent design review

Decision: **PASS for the reviewed design-only change; no blocking findings.**
This review does not approve an HBM implementation or establish simulation,
FPGA execution, timing, bandwidth, power, ASIC fit, or model inference.
Reviewer: independent Codex agent `choose_first_issue`; no authorship or edits
to either reviewed document in this change.

Base revision supplied for the review: `c05d5415`.
The review covers the working-tree document bytes identified below, including
the HBM proposal changes over that base.

## Reviewed files

| File | SHA256 |
| --- | --- |
| `doc/microarch/weightstore.md` | `f9116ca77681006aa594a15778fcf1568f26c98fe7ee6e01b52b12456369f552` |
| `doc/microarch/ternary-systolic-array.md` | `34089ce077bf2fb3873e399ec5380c51db1ac2e4890c971ab3db0341dc2598a1` |

## Contract and scope findings

- The logical read interface remains 64-byte aligned lines, 48-bit logical
  offsets, 512-bit data, per-client tags and image epochs. HBM channel/base
  translation stays private to the backend. No new CPU CSR/address allocation
  or reinterpretation of DOT1 is introduced.
- Load, close-admission, drain, whole-logical-image readback/hash and seal remain
  ordered. The proposal explicitly covers every writer, mux, alias, descriptor
  and destructive control path. A software-disabled benchmark is not treated as
  a hardware write fence. The hash remains a disclosed trusted-host handshake.
- Warm core reset preserves the seal. Backend/controller reset invalidates
  readiness, epochs and staging and requires reload/verification/reseal.
  Fault handling preserves already presented/queued responses and drains late
  physical transactions before ID reuse. No silent fallback is accepted.
- The current frontend has 16 slots and one physical read outstanding. The
  proposal correctly distinguishes that implementation from future per-channel
  concurrency and prefetch/reorder storage. Sixteen line credits represent
  1,024 bytes of logical response capacity; this is arithmetic, not bandwidth.
- The 256 MiB logical aperture remains unchanged. The canonical 242,357,184-byte
  image fits it with 26,078,272 bytes remaining and is smaller than a 512 MiB
  reference channel region. The cited 16 GiB HBM capacity does not enlarge that
  logical aperture or prove a 27B-model allocation/fit.
- Both documents label HBM/array/ASIC-ROM/CoralNPU integration as proposals,
  retain DDR as the implemented baseline, and make no new measured execution,
  full-model, token-quality or performance claim.

## Pinned public-source checks

All source checks used the exact public AWS archive at revision
`b603a81f65666e0cf7a67ee5cf18b148eb6b08c3`, not a moving branch.

- [cl_mem_hbm_axi4.sv lines 73–83](https://github.com/aws/aws-fpga/blob/b603a81f65666e0cf7a67ee5cf18b148eb6b08c3/hdk/cl/examples/cl_mem_perf/design/cl_mem_hbm_axi4.sv#L73):
  32 ports, four-bit AXI length, 256-bit data, 34-bit addresses, six-bit IDs and
  host ingress MAP_PORT=15. The document correctly prioritizes this over the
  same revision's README channel-0 description. Lines 198/208 discard 30 upper
  address bits, and 233–235 force downstream IDs to zero; the warning against
  copying those behaviors as bounds protection or concurrency is supported.
- [cl_mem_hbm_wrapper.sv lines 219–233](https://github.com/aws/aws-fpga/blob/b603a81f65666e0cf7a67ee5cf18b148eb6b08c3/hdk/cl/examples/cl_mem_perf/design/cl_mem_hbm_wrapper.sv#L219):
  readiness is two bits at [2:1], with bit 3 zero and bit 0 software reset.
  Lines 130 and 566/572 force the HBM size to 32-byte beats. A four-bit burst
  length therefore allows up to 16 beats/512 bytes; two such beats represent
  one 64-byte logical line. Parity, functional-error and thermal telemetry must
  still be reviewed rather than inferred from this reference wrapper.
- [cl_mem_pcis_dec.sv line 968 onward](https://github.com/aws/aws-fpga/blob/b603a81f65666e0cf7a67ee5cf18b148eb6b08c3/hdk/cl/examples/cl_mem_perf/design/cl_mem_pcis_dec.sv#L968):
  the DDRB-named test/scrub master is a real additional route and must be removed
  or protected. The HBM performance mux also selects an alternate writer.
- [cl_axi_ctl.sv lines 295–299](https://github.com/aws/aws-fpga/blob/b603a81f65666e0cf7a67ee5cf18b148eb6b08c3/hdk/cl/examples/cl_mem_perf/design/cl_axi_ctl.sv#L295):
  the reference performance checker qualifies RRESP-error reporting with RLAST.
  The proposal correctly requires checking every beat instead.

## Stripe arithmetic and implementation decisions

The candidate mapping is invertible for a nonempty ordered set of distinct
channels and a positive stripe size that is a power-of-two multiple of 64:
if j is the selected channel index and y its local offset, the inverse is
`x = (floor(y/S) * P + j) * S + (y mod S)`.
A line-aligned 64-byte request cannot straddle such a stripe. An 18-byte Q1 block
can straddle lines/stripes and correctly remains the consumer assembler's job.

An independent arithmetic check exercised 234 boundary points for P in
{1,2,3,8,16,32} and S in {64,4096}, including stripe wraps, 4 KiB transitions,
and the last canonical-image byte/line. All inverse and alignment checks passed.
These are formula checks only, not RTL or hardware measurements. The design
also explicitly requires capacity/overflow checks, controller routing rules,
physical burst splitting, immutable map parameters and logical-order hashes.

No requested source correction is necessary for this design-only patch.
Before implementation freeze, the already listed decisions must become exact:
ordered distinct channel list and allowed P, physical bases/lengths/alignment,
stripe size, map version, ID/credit limits, CDC/reset/error policy, and telemetry.
Those are acknowledged open implementation choices, not evidence of completed
HBM support. No repository files or AWS state were changed by this review.
