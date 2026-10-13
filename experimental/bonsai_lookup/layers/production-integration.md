# Production integration plan against Alex's frozen image

This is a plan, not a claim that the standalone tile is production-integrated.
The frozen wrapper/configuration and 247 RTL source hashes match the tested
25 MHz image; Alex's portable launcher passes25 MHz explicitly. Its underlying
Tcl defaults to50 MHz when arguments are absent. Preserve core25, shell250 and
HBM-AXI300 MHz through the launcher's explicit settings.

## 1. Close the numerical and complete-layer boundary

Use the seven full projection fixtures and ordered FP32 reference in this
folder. Add a layer sequencer carrying token,row,K-group,job and epoch identity.
Retain per-output FP32 accumulators across K groups; with M8,N32 the accumulator
payload alone is1,024 bytes. Apply each native scale and activation scale in the
specified order. Pipeline independent output rows while preserving accumulation
order for each row. Measure the scale/reduction initiation rate and stalls;
it must not be assumed to match the E integer dots/cycle sizing roofline.

A software scale/reduction path can establish first functional integration,
but its measured cycles must remain in the operator latency. A future FPGA
FP32 pipeline is a separately reviewed resource/timing decision. Include stores
and error completion in the full projection contract. A descriptor extension
must be versioned separately from DOT128 v1. The actual-weight mailbox/driver,
simulator/FPGA and reviewed-merge requirements of ERG-103 remain independent.

## 2. Add a real memory client and bounded on-chip storage

The frozen wrapper connects the NPU master through `coral_links_wrapper LINKS`
and `M_DATA` to `cl_axi_mstr_bus`, then into `CL_DMA_PCIS_SLV`, whose DDR/HBM
paths include `lcl_cl_sh_ddrb` and `CL_HBM`. The NPU-facing128-bit25 MHz path
cannot feed the proposed engine peak. A new client must enter downstream of
that bottleneck with explicit arbitration; do not simply rename the standalone
16-byte test bus as AXI or assume the existing bridge preserves16 concurrent IDs.

Inspect the pinned link/interconnect configuration before selecting the exact
attachment and ID routing. The current wrapper drives its master AR/AW IDs to
zero, so the standalone tagged interface cannot be wired to it unchanged.
Define burst lengths, alignment, IDs, RLAST/RRESP checking, timeout/drain,
read/write ordering and finite response buffering. Introduce a clock crossing
with documented reset/epoch behavior; local reset cannot discard fabric-owned
responses. Preserve the1 GiB core aperture and host translation until a separate
reviewed map change. Weight sealing must cover host/core/DMA writers and aliases;
activation/KV/output allocations remain writable and disjoint.

Use a banked weight/scale staging buffer and activation reuse across output
rows. Ping-pong buffers should overlap next-group reads with compute only after
ownership and bank-port conflicts are verified. A streamed32-row weight slab
needs1,088 bytes per group; two slabs alone need2,176 bytes, but that is not a
complete SRAM budget. Account for outstanding responses, activation rows,
FP32 accumulation, queues, alignment and measured memory latency. Full-K double
buffering grows with N and K and cannot reuse the earlier N16 storage budget
unchanged. Compare observed overlap against a non-overlap reference.

Measure useful bytes and bus beats separately. Two PQ2 engines at80% occupancy
need1.36 GB/s weight payload; four2.72, six4.08, eight5.44 GB/s, before other
traffic. These are feed requirements, not measured bandwidth. A512-bit25 MHz
compute-side stream tops out at1.6 GB/s, so higher engine counts need a wider
or appropriately scheduled memory/compute interface rather than free replication.

## 3. Rebuild and physically qualify in coordinated stages

Obtain the missing portable package root verifier/checksum files and referenced
review/provenance material. Verify pinned Vivado2025.2, HDK/IP/shell identities
and all120 dependency hashes on the approved build machine. Source-owner work
has not installed licenses, run the launcher or modified the shared host.

First qualify a bounded memory-client/loopback image with byte-exact checks and
bandwidth counters. Then qualify one/two-engine group and scaled-projection
images. Evaluate4/6/8 only after resource, placement, timing and CDC reports
justify them. The existing image's overall WNS is+0.032 ns; no engine count is
assumed to fit or route. Record failures and resource deltas as first-class results.

The parent and image owner must agree build resources, AFI identity, slot windows
and quiet-run isolation before dispatch. Run Alex's independent audit and review
its timing, route, CDC and methodology evidence, then physical kernel correctness,
byte-checked memory traffic, complete projections and matched model inference.
For every variant retain source/model/layout/oracle/compiler/firmware/DCP/AFI
identities, observed loaded slot, clocks, exact outputs and timing boundaries.
Prefill TPS, TTFT, decode TPS and end-to-end latency remain pending until measured
on FPGA with a complete runtime. No standalone simulation or reference throughput
is substituted for those inference measurements.
