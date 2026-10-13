# Packed ternary memory instructions and DTCM staging

Revision 0.2 proposal, extending the [queue ISA](README.md). **Nothing here is
implemented in Coral's decoder, LSU, vector register file access or DMA path.**
The executable model tests a proposed contract; it does not simulate those units.
The added mnemonics are `bt.lpq` and `bt.spq`. They move compressed bytes
losslessly; they do not quantize, unpack, apply scales, launch DMA or run GEMM.

## Actual memory topology inspected first

The pinned core sources and frozen Alex rebuild RTL give this distinction:

| Configuration/source | Observed property | Design consequence |
| --- | --- | --- |
| `Parameters.scala` | RVV VLEN=128; default DTCM 32 KiB at 0x00010000, highmem configuration differs | Use one existing 128-bit vector register; never assume every image has the same memory map |
| `CoreAxi.scala`, `TCM.scala`, `SRAM.scala` | One TCM128 address/enable/write/data/mask interface; shared read/write service, byte masks, registered read-valid | At most one 16-byte DTCM service operation per cycle; unaligned 16-byte movement needs two service operations |
| `Fabric.scala` / CoreAxi wiring | Fixed-priority sources 0 CPU dbus, 1 external AXI fabric, 2 debug; source0 always has priority, core dbus ready tied true | No independent engine DTCM port; an external client can be starved by CPU traffic |
| Frozen `CoralF2Axi.sv`, `TCM128_1.sv`, `FabricMux.sv`, `FabricArbiter.sv` | DTCM instance has 11-bit row address and 128-bit data; fabric window 0x00010000..0x00017fff; same shared arbitration | Actual frozen image topology is 2048*16=32768 bytes, not the repository's larger configurable targets |
| `BankedDtcm.scala`, `TlulLocalXbar.scala` | Separate TLUL generator defaults to eight address-selected 4 KiB banks and three hosts with per-bank arbiters | This is not instantiated as the frozen AXI DTCM; eight-bank parallelism cannot be claimed for Alex's image |
| `scalar/Lsu.scala`, `rvv/RvvInterface.scala` | Scalar, RVV and VME requests share the LSU; two RVV request interfaces do not imply two DTCM SRAM ports | New instructions must participate in existing arbitration, fault and dependency machinery |

At 25 MHz the single 128-bit port has a theoretical aggregate ceiling of
400 MB/s before arbitration and instruction overhead. A DTCM-to-DTCM copy uses
a read and write, so even aligned movement has a 200 MB/s payload ceiling through
that port. These are port bounds, not measured bandwidth. HBM-to-DTCM movement
also uses the existing external-memory path; there is no evidence of independent
full-bandwidth overlap. No capacity is reserved for this proposal yet: stacks,
runtime state, other operators and existing allocations reduce the 32 KiB pool.
The accompanying memory audit records hashes of the actual files inspected.

## Exact architectural forms and encodings

Both use custom-0 opcode 0x0b, funct7=0x41. This is distinct from the queue
funct7=0x40 and from Coral's existing VME msetmtype funct7=0x41 at opcode 0x57.
They are R-format encodings with deliberately heterogeneous register fields:

| Mnemonic | funct3 | Match / mask | [11:7] | [19:15] | [24:20] |
| --- | --- | --- | --- | --- | --- |
| `bt.lpq vd, (rs1), rs2` | 0 | 0x8200000b / 0xfe00707f | Vector destination v0..v31 | Scalar address x0..x31 | Scalar trit count x0..x31 |
| `bt.spq vs3, (rs1), rs2` | 1 | 0x8200100b / 0xfe00707f | Vector source v0..v31 | Scalar address x0..x31 | Scalar trit count x0..x31 |

All register combinations are legal, including v0 and scalar aliases. x0
supplies zero, while v0 is ordinary vector data, not an implicit predicate.
Other funct3 values in this allocation are reserved. Feature-off, absent RVV,
or a configuration without 128-bit vector registers rejects these encodings.
The feature manifest advertises queue support and packed-memory support
separately so a driver can use byte-copy fallback with an integrated queue.

These examples encode vector v4 in the R-format rd field using the assembler's
integer register spelling x4; this is only `.insn` syntax, not scalar x4 access:

```asm
.insn r 0x0b, 0, 0x41, x4, a0, a1   # bt.lpq v4,(a0),a1 = 0x82b5020b
.insn r 0x0b, 1, 0x41, x4, a0, a1   # bt.spq v4,(a0),a1 = 0x82b5120b
```

Each executes in slot 0 alone with rs1/rs2 dependencies and a full-vector
destination dependency (load) or source dependency (store). Neither writes an
integer rd. The first implementation may wait for RVV idle and serialize these
instructions until completion; a later pipelined implementation must preserve
the same register/memory semantics. Existing scalar-only `.insn` inline assembly
constraints do not tell a compiler about v4: wrappers must declare that vector
clobber/use, memory effects and scalar inputs explicitly, or use an out-of-line
assembly routine with a documented vector clobber set. The assembler audit is
not a validated C intrinsic implementation.

## Lossless data and tail rules

Let C be unsigned x[rs2], 0..64 trits, and B=ceil(C/4) bytes. C>64 causes an
illegal-instruction exception (mcause 2) before access or register changes.
C=0 performs no memory access and leaves the vector register/memory unchanged,
even if the supplied address is unmapped. For C>0:

* `bt.lpq` reads exactly bytes [address,address+B), places them in bits
  [8B-1:0] in increasing-address/little-endian order, and zeros bits [127:8B].
  It does not alter another vector register.
* `bt.spq` writes exactly B low bytes of the source vector, preserving all other
  memory bytes. It neither zeroes a larger bus beat nor writes 16 bytes for a
  short count. It does not modify its source register.
* Every bit in the last addressed byte is transferred, including the one to
  three unused trit positions when C is not a multiple of four. The caller must
  own the entire byte. There is no hidden read-modify-write and no bit-level
  atomic merge with another producer. Use explicit masks in software when
  sharing packed bytes; descriptor staging buffers must not share them.

Lane i is bits 2*(i mod4)+:2 of byte floor(i/4). PQ2 arithmetic interprets
00=-1,01=0,10=+1 and rejects active11. **Movement preserves all four codes**,
including11, with no validation or conversion exception. This permits exact
roundtrip and inspection of malformed payloads; `bt.submit`'s arithmetic engine
still rejects active11. Inactive padded K lanes must use code01 and activation0
before a full-group arithmetic command. A raw load/store never inserts padding.

Native model groups are 34 bytes: a two-byte little-endian raw FP16 scale followed
by 32 code bytes. Transfer codes with two C=64 loads/stores; LHU and SH transfer
the scale's exact 16 bits. Weight scales are not BF16, and are never converted
through a floating-point register during movement. Signaling/quiet NaN,
subnormal and signed-zero scale bit patterns all survive copying; scale validity
is checked only by the later scaled command. FP32 activation scales likewise
use existing LW/SW or byte/vector copies, without arithmetic.

An explicit software `unpack_pq2` extracts codes and maps code-1, rejecting11
in active lanes. Its inverse accepts only exact integer {-1,0,+1}; values such
as 2 or 0.5 fail rather than requantize. Any FP-to-ternary quantizer is a separate
explicit operation with its own scale/rounding policy. No such conversion
instruction is added here. `bt.spq` cannot be used as an implicit quantizer.

## Addresses, strides, faults and ordering

The address is x[rs1] with no immediate or postincrement. Byte alignment is
supported, including native code addresses at group_base+2; a transfer may cross
a 16-byte beat. The widened address+B calculation must not wrap 32 bits. The
complete byte range must lie in one authorized normal-memory region; MMIO is
excluded. Reject invalid range/access policy before any bus request. No access
outside that authorized region is permitted. Architectural loads observe only
the B requested bytes; physical read beats may cover additional bytes only
inside the same permitted normal-memory region without side effects. Stores
always use exact byte strobes. Never overread a protected neighboring region.

Only contiguous transfer is encoded. Software ADD/ADDI selects row/group strides:
native block stride34, tensor row stride34*G; slab group stride1088, code-row
stride32 and scale-row stride2. Negative or irregular strides use ordinary
address arithmetic with checked bounds; there is no silent modulo addressing.
These instructions do not directly copy memory-to-memory: load into a vector,
then store. Overlapping bulk copies require memmove traversal or a separate
buffer; the staging repack helper rejects overlapping source/destination ranges.

Loads buffer all B bytes and update the vector only on successful completion.
An access failure raises synchronous load-access fault 5, records the instruction
PC and faulting byte address, and leaves the old vector unchanged. Stores may
require two bus beats. Validate bounds before either, issue beats in increasing
address order and wait for each response before the next. A store-access fault 7
may leave earlier successful beats written; the failing beat's byte effects are
backend-dependent, but bytes outside the requested range must remain unchanged.
There is no transactional rollback, vstart restart index or asynchronous ticket.
Do not consume a partly filled staging buffer. On a recoverable memory fault,
software may repeat the whole store only to an exclusively owned normal-memory
buffer with the same source bytes; otherwise discard and refill the slot.

The fault address is the first rejected byte for range checks, or the first
requested byte of the failing beat for bus faults; address wrap reports the
original base address because the out-of-range sum is not representable in RV32.
Illegal count/feature/encoding
uses mcause 2 and instruction word in mtval. A pending interrupt may be taken
before issue or after retirement; once issued, finish the operation or report
its synchronous fault before taking that interrupt. An interrupt must not leave
an unreported half-store. This requires explicit LSU/retirement integration.

Successful nonempty loads mark vector state Dirty, but do not alter vl, vtype,
vstart, vxsat, vxrm, FCSR or VME state. Stores leave vector state unchanged.
Nonzero vstart is illegal for these nonrestartable operations, including C=0;
software must finish/resolve any interrupted RVV operation first. No predicate,
SEW, LMUL, vl or tail-policy setting changes their byte semantics. They use the
same machine-mode restriction as the queue extension. Future lower-privilege
support must retain normal memory protection and vector-state enable checks.

Packed loads/stores have ordinary architectural load/store ordering, not queue
release/acquire semantics. FENCE rw,rw orders them like other memory operations.
The queue's submit release must include completed packed stores; its terminal
acquire must make engine stores visible to later packed loads. Neither a fence
nor these instructions creates cache coherence. Reset may leave bytes from an
in-flight store; coordinated reset invalidates the entire staging generation
and all in-flight/completed-but-unconsumed buffers. DTCM contents need not zero.

## DTCM ownership, capacity and ping-pong

The proposed descriptor keeps ABI1's format and accepts W/A/S/Y/F addresses in
a linker-reserved DTCM pool **only after** an integrated engine-to-DTCM adapter
is advertised. Current standalone tops expose their own tagged read/output
ports and have no such access. The adapter must route DTCM and HBM requests,
arbitrate with the existing external client, preserve IDs across conversion,
write output strobes, honor faults and complete reset/drain. It must not loop
the outbound HBM master back into DTCM without reviewed routing/arbitration.

Software partitions the reserved pool into nonoverlapping slot0/slot1 regions,
optionally called logical banks. They are **not independent SRAM ports**. Each
slot contains only buffers whose full size fits its allocation. One driver owns
each slot state and a software generation number:

```text
FREE -> FILLING -> READY -> INFLIGHT(ticket,generation) -> COMPLETE -> FREE
                   |           |
copy fault -> INVALID          command error -> INVALID after drain/reap
reset: all slots INVALID; increment generation; clear tickets; explicitly refill
```

The CPU owns writes while FILLING. Only after all copies and validation succeed
does it execute FENCE and mark READY. Submit READY on the single command slot;
once accepted, the engine owns inputs read-only and outputs write-only. CPU,
host DMA and debug must not modify those ranges. CPU may fill the other FREE
slot, using disjoint input/output/descriptor bytes. After terminal poll/reap and
acquire/FENCE, the CPU owns output consumption. Release to FREE only after outputs
are consumed/scattered. A ticket must match both slot and current generation;
stale completion cannot free or publish a newly filled slot. On command error,
discard the whole tile even when some Y/F bytes were written. On partial copy
fault never mark READY; either fully refill INVALID or abandon it.

The production driver rejects overlapping slots and any descriptor output/input
alias, and verifies the allocations against the actual linker reservation.
These are software ownership rules, not implemented hardware bank locks. The
one-command queue cannot run both slots concurrently. Filling slot1 while slot0
computes is logically safe but may serialize/stall on the shared DTCM/AXI paths.
Fixed CPU priority can starve engine accesses routed through the external port;
qualify bounded admission/fairness or producer quiescence before claiming forward
progress under concurrent filling. A register-only polling loop avoids adding
DTCM polling traffic, but does not by itself prove system-wide fairness.

For full N32 tiles, dense footprint including a 64-byte descriptor and Y/F is
`G*(1088+128*M+32) + 160*M + 64`, before allocator padding/runtime reserves:

| Tile | One complete DTCM slot | Two complete slots | 32 KiB assessment |
| --- | ---: | ---: | --- |
| Scaled M1,G1 | 1,472 B | 2,944 B | Fits if reserved; useful staging test |
| Q decode M1,G16 | 20,192 B | 40,384 B | One may fit; full ping-pong does not |
| Q prefill M8,G16 | 35,648 B | 71,296 B | Even one does not fit |
| Down M1,G48 | 60,128 B | 120,256 B | Does not fit |
| Down M8,G48 | 104,256 B | 208,512 B | Does not fit |

Do not split K into independent scaled commands to make it fit: that would reset
the ordered FP32 accumulator. For Q prefill, a practical mixed-memory proposal
is M4: leave W in prepacked HBM and stage A/S/Y/F plus descriptor in DTCM.
One slot is 8,192+512+512+128+64=9,408 bytes; two are 18,816 bytes before reserves.
Eight M4 commands cover M32 per N tile, versus four M8 commands, so extra command
overhead must be measured. Down G48 may require still smaller M or streaming
from HBM. A smaller N does not shrink ABI1's fixed 32-row W slab allocation.
Streaming group panels through tiny ping-pong buffers while one scaled command
is active requires a new producer-consumer descriptor protocol or internal
adapter buffering; it is not secretly provided by these memory instructions.

## GEMM staging and software fallback

```text
# Repack native PQ2 to ABI1 slab W in an owned DTCM slot when it fits.
# native is row-major [n][g][34]; destination is group-major [g][1088].
require_disjoint(native_range, slot_range)
for g in 0..G-1:
  for n in 0..N-1:
    src = native + (n*G+g)*34
    dst = slot.W + g*1088 + n*32
    xcount = 64
    bt.lpq v4,(src+2),xcount      # pseudocode: ADDI computes each address
    bt.spq v4,(dst),xcount
    bt.lpq v4,(src+18),xcount
    bt.spq v4,(dst+16),xcount
    scale_bits = LHU(src)
    SH(slot.W+g*1088+1024+n*2, scale_bits)
  fill_inactive_rows_with_code01_and_raw_scale_zero()
copy_A8_and_F32_scales_with_existing_scalar_or_RVV_instructions()
fence rw,rw
slot.state = READY
ticket = bt.submit(slot.descriptor)
require(ticket != 0)
slot.state = INFLIGHT(ticket,generation)
# Optional fill of the disjoint other slot; do not assume independent ports.
wait_poll_reap_with_deadline(ticket)
fence rw,rw
if successful: consume_or_scatter_all_Y_and_F_then_free_slot()
else: invalidate_slot()
```

Decode uses M1; prefill loops over the selected M tile size. If full W does not
fit, prepack it in HBM and stage only the fitting operand/output ranges as above.
Every loop retains all G groups in one scaled command. The extension does not
replace activation quantization or any non-matmul Coral operation.

On current hardware the functionally equivalent packing fallback copies each
32-byte code block with 32 LBU/SB pairs (or existing RVV byte loads/stores) and its
scale with LHU/SH. No new opcode is executed. This fallback exactly preserves
reserved codes and every scale bit. Existing RVV implementations must handle
their own vl/vtype and tail policy; when emulating an individual `bt.lpq`, use
a zeroed 16-byte temporary with B copied bytes and a whole-register load, so
register bytes B..15 are deterministically zero. A zero-count operation does
nothing. Existing queue absence still requires the existing Coral kernel path.

The new operations can avoid repeated vector configuration and make packed
length explicit, but do not compress HBM traffic beyond its already packed
bytes or prove a faster LSU. Compare them against an optimized RVV copy kernel,
not only a scalar byte loop, before claiming a performance advantage.

## Validation and integration gates

`check_encodings.py` audits all five instructions against the pinned decode
sources and each other, and assembles every legal register combination.
`check_memory.py` models lossless load/store, faults and ownership, tests all
counts 0..64 at every 16-byte alignment, reserved codes, exact native/slab roundtrip,
raw scale patterns, copy overlap rejection, bounds/wrap, partial failure and
reset generation. The recorded checks are **Python specification tests only**.

```bash
python3 experimental/bonsai_lookup/isa/check_memory.py \
  --alex-rtl /path/to/alex/input/core/rtl --out /tmp/bonsai-memory-audit.json
```

The model records logical byte accesses; it is not a physical AXI transaction
model. Store-fault tests cover earlier completed beats and a failing beat that
changes zero or a prefix of its requested bytes. Actual backends may change a
different subset within the failing beat, so hardware acceptance must test their
documented response semantics too.

Actual implementation must add packed decode, scalar/vector dependencies,
vector state events, LSU operations and vector commit buffering, synchronous
fault/interrupt rules, FENCE visibility and DTCM adapter/pool capability. Required
CPU RTL tests repeat the model cases with real register file and memory buses,
including backpressure, hazards, reset between beats, stale responses, unfair
CPU traffic, unchanged neighboring bytes, invalid-count/no-access and disabled
feature behavior. FPGA validation must measure contention and fill/compute/store
timelines at 25 MHz before claiming DMA overlap, bandwidth or inference speedup.
