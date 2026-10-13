# Proposed Coral ternary command ISA, revision 0.1

Status: **proposal; no Coral decoder, assembler mnemonic, queue, DMA adapter or
CPU instruction execution is implemented by this change.** This specification
extends the actual Coral ISA inspected at
`b6e9dd7eb5c1765dabede62a07145d489ef39f9b`. The standalone integer/scaled RTL
and its existing tests remain unchanged. This is an RV32, single-hart,
firmware-owned accelerator interface, provisionally named `Xbonsai` here;
the name and encodings are vendor-private proposals, not ratified extensions.

## Decision and exact instruction delta

Add three scalar instructions: `bt.submit`, `bt.poll`, `bt.reap`. A descriptor
selects integer DOT128 or a complete ordered scaled tile. Reuse existing scalar
and RVV memory operations for packing, standard fences for ordering, and existing
VME integer/FP matrix instructions for other formats and fallback kernels.
No new vector load/store, tile register, quantization instruction, FP CSR,
interrupt source, or engine-count instruction is needed for revision 0.1.

All are 32-bit R-format words, with `opcode=0x0b`, `funct7=0x40`, `rs2=x0`.
`rs1` is an integer source and `rd` an integer destination; `rd=x0` is illegal
to avoid silently losing ownership/status. `rd=rs1` is legal: read the old value
before writeback. `rs1=x0` is legal and supplies zero. Every instruction occupies
slot 0 alone, has one scalar source dependency and one destination scoreboard
entry, and must commit exactly once without speculative accelerator effects.

| Proposed instruction | funct3 | Match / mask | Input in rs1 | Result in rd |
| --- | --- | --- | --- | --- |
| `bt.submit rd, rs1` | 0 | `0x8000000b / 0xfff0707f` | 32-bit descriptor byte address | Nonzero ticket, or zero if slot occupied or tickets exhausted |
| `bt.poll rd, rs1` | 1 | `0x8000100b / 0xfff0707f` | Ticket | Status snapshot; no slot change |
| `bt.reap rd, rs1` | 2 | `0x8000200b / 0xfff0707f` | Ticket | Status snapshot; releases slot only if terminal |

Match/mask must be combined with `rd != 0`. Bits are
`[31:25]=1000000, [24:20]=00000, [19:15]=rs1, [14:12]=funct3,
[11:7]=rd, [6:0]=0001011`. Other funct3 values in this funct7 allocation and
nonzero rs2 are reserved and illegal. Other custom allocations remain outside
this proposal. The feature-off configuration must continue to reject all three.
Firmware selects this ABI only from a matching image/driver capability manifest;
there is no proposed CSR allocation or claim that the current image supports it.
An unknown image uses existing kernels. A guarded illegal-instruction probe may
be added by the driver, but must not submit a real job merely to discover support.

Assembler equivalents, without a compiler/toolchain mnemonic patch:

```asm
.insn r 0x0b, 0, 0x40, a0, a1, zero   # bt.submit a0,a1 = 0x8005850b
.insn r 0x0b, 1, 0x40, a0, a1, zero   # bt.poll   a0,a1 = 0x8005950b
.insn r 0x0b, 2, 0x40, a0, a1, zero   # bt.reap   a0,a1 = 0x8005a50b
```

`bt.wait` is a software polling loop with a caller deadline, not a fourth
instruction. Poll and reap never wait for job completion. An absent memory
response must not prevent the CPU from polling or servicing interrupts.

## Collision audit against Coral

The [RISC-V opcode map](https://docs.riscv.org/reference/isa/v20260120/unpriv/rv-32-64g.html)
assigns `0x0b` to custom-0 and reserves that space from future standard use.
This does not reserve it against other vendor extensions; this repository's
actual decoder still needs an audit whenever its source changes.

| Existing family | Observed encoding | Source at the pinned revision |
| --- | --- | --- |
| Scalar I/M/Zbb, CSR, control and fences | Explicit BitPat tables; none overlap this proposal | `hdl/chisel/src/coralnpu/scalar/Decode.scala`, `DecodeInstruction.table` |
| Scalar FP/BF16 | Major bits [6:2] select 0x07/27/43/47/4b/4f/53 families | `hdl/chisel/src/coralnpu/float/FloatCoreInterface.scala`, `FloatInstruction.decode` |
| Vector loads/stores | 0x07 / 0x27, width and mew discriminate FP/RVV/VME | `hdl/chisel/src/coralnpu/rvv/RvvDecode.scala`, `from_uncompressed` |
| Vector ALU and VME configuration | 0x57; msetmtype funct7=0x41, msettn/tm/tk/mtypei funct7=0x42 with subfunctions | Same file, `isMset*` |
| `vtmmu`, `vtmms` | 0x77, funct6=0x3c, vm=1, funct3=0; rd encodes tile/sign | Same file, `isOpVeValid`; `tests/cocotb/vme_test/vme_test_utils.h` |
| `vtfmm` | Same major/funct6/vm, funct3=1, gated by enableFloat | Same sources |
| VME moves, zero, discard | 0x57, existing function fields | Same sources; reused unchanged |

`DecodeTest.scala` also labels a zero-field 0x0b word invalid; that test alone
would not prove the entire major opcode is unused. `check_encodings.py` instead
checks all scalar/FP BitPat masks, the real FP major dispatch (including its
five-bit major comparison), and the RVV major dispatch. This is a source-level
collision audit, not execution of the Chisel decoder. Keep the generated source
hashes with its receipt. No proposed instruction is disguised as an existing
VME encoding or silently changes its meaning.

## Queue, ownership and status

One command/completion slot belongs to one firmware driver on the hart. States
are EMPTY, BUSY, DONE_OK and DONE_ERR. Submit to EMPTY atomically reserves the
slot and returns a ticket before descriptor/data fetch completes. A failed
descriptor fetch or invalid descriptor therefore completes asynchronously with
DONE_ERR. Submit while the slot is occupied returns zero, performs no descriptor
read and leaves the old command unchanged. Interrupt handlers must not submit
or reap through a second owner; firmware owns serialization and context switches.

Ticket zero is never allocated. Allocate monotonically from 1 to 0xffffffff,
never reusing a ticket before coordinated reset. Exhaustion makes submit return
zero until that reset; the driver tracks successful submissions and must not
retry forever. Poll/reap accept only the current nonzero ticket; otherwise they
return INVALID. A terminal record remains stable until reap. Reap while BUSY
returns BUSY and does nothing. Reap of a terminal ticket returns its final
status and frees the slot atomically; subsequent queries return INVALID.

Status is a 32-bit scalar word:

| Bits | Definition |
| --- | --- |
| 1:0 | 0 INVALID, 1 BUSY, 2 DONE_OK, 3 DONE_ERR |
| 7:2 | Zero |
| 12:8 | OR of output FP flags: bit12 NV, 11 DZ, 10 OF, 9 UF, 8 NX |
| 15:13 | Zero |
| 23:16 | Error code; zero unless DONE_ERR |
| 31:24 | Zero |

INVALID and BUSY have every other bit zero. Error codes: 1 descriptor access;
2 descriptor ABI/dimensions/reserved fields/alignment; 3 range, alias or address
policy; 4 operand read/protocol; 5 reserved PQ2 code; 6 invalid scale;
7 output write/protocol; 8 internal sequence/reduction fault. First detected
fault wins; simultaneous faults take the lower code. Flags on DONE_ERR are
diagnostic partial flags, not a valid numerical output contract. A finite-input
IEEE overflow/underflow/inexact/invalid arithmetic result is DONE_OK with flags;
it is different from malformed input or a bus error.

No abort, partial-result resume, preemption, queue chaining, or cancellation is
defined. After detecting an error stop issuing new requests, drain all accepted
reads/writes and only then publish DONE_ERR. Any partial output memory is invalid
and the caller must discard the entire tile. A backend that never responds may
remain BUSY: the driver deadline escalates to coordinated platform recovery,
not unsafe tag reuse. Polling remains possible during the stall.

## Descriptor ABI 1

Descriptors are 64 bytes, 16-byte aligned, consisting of sixteen little-endian
uint32 words. Address arithmetic uses a wider internal sum and rejects wrap;
all ranges must lie wholly in the platform-authorized uncached shared buffer
pool. This proposal creates no new address aperture or DMA access authority.
The current Alex 32-bit core view is distinct from host BAR4 offsets; never put
the 64-bit host BAR4 offset into a descriptor. A platform integration must supply
the pool bounds and access policy before advertising this ABI.

| Offset | Field | Required value or meaning |
| --- | --- | --- |
| 0x00 | magic | `0x31544e42` (little-endian BNT1) |
| 0x04 | size | 64 |
| 0x08 | operation | 0 DOT128_I32, 1 SCALED_F32; others invalid |
| 0x0c | M | 1..8 token rows |
| 0x10 | N | 1..32 output rows |
| 0x14 | G | DOT: 1; scaled: 1..48 groups, K=128G |
| 0x18 | W | Packed weight slab base, 16-byte aligned |
| 0x1c | A | Signed INT8 activation base, 16-byte aligned |
| 0x20 | S | FP32 activation scale base, 16-byte aligned; zero for DOT |
| 0x24 | Y | Output base, 16-byte aligned |
| 0x28 | F | One FP flag byte per output, 16-byte aligned; zero for DOT |
| 0x2c | reserved | Zero |
| 0x30..0x3f | reserved | All zero |

The driver keeps descriptor and operand allocations immutable from submit until
terminal status. The engine snapshots all 64 descriptor bytes before validation
and operand reads. Descriptor alignment/range/policy checks precede that fetch;
failure completes with error 2 or 3 without issuing a read. Fetches are not atomic
against illegal concurrent CPU writes.
Reject overlap of Y/F with each other or any input/descriptor. Read-only operand
ranges may share an allocation if each validated range is legal. No speculative
operand read occurs before the descriptor and all range checks pass.

Layouts deliberately match the existing standalone tile staging contract:

* W has G records of 1088 bytes. Record g contains 32 contiguous 32-byte code
  blocks, then 32 little-endian FP16 scales. Lane i is bits `2*(i%4)+:2` of code
  byte `i/4`: 00=-1, 01=0, 10=+1; active 11 is an error. This lossless slab layout
  repacks each native 34-byte PQ2 block (2 scale bytes then 32 code bytes).
* A has G records of M*128 bytes, ordered group, token, lane. Signed byte -128
  is widened before negation. No unsigned or asymmetric zero-point mode exists.
* S has G records of 32 bytes: eight little-endian FP32 scale slots, first M
  active. Inactive slots and W rows N..31 are allocated but not interpreted.
* Y contains M*N four-byte outputs in token-major/row-minor order, indexed
  `m*N+n`; int32 for DOT or FP32 for scaled. F has M*N bytes in the same order,
  each byte bits4:0 = NV,DZ,OF,UF,NX and bits7:5 zero. Writes must preserve bytes
  outside these exact ranges, including the final partial bus beat.

DOT ignores all scale bits; it computes one complete 128-lane integer dot per
output. Scaled computes all G groups. Partial K groups are not an ABI feature:
the driver pads A with zero and W with code01 to 128 lanes while retaining the
intended group's scale. M/N tails use their actual values. Padding must be done
before submission, and its cost belongs in performance measurements. Larger M
and N loop in software; K>6144 falls back because splitting K across commands
would reset the accumulator and change the ordered FP32 contract.

## Arithmetic, state, privilege and reset

For scaled output y[m,n], initialize +0.0 and process g in increasing order:

```text
d = exact_int32_sum(i=0..127, int8(A[g,m,i]) * ternary(W[g,n,i]))
p = RN32(float32(d) * exact_fp16_to_fp32(weight_scale[g,n]))
q = RN32(p * activation_scale[g,m])
y = RN32(y + q)
```

The integer bound is [-16384,+16384], exactly representable in FP32. Each MUL,
MUL and ADD rounds separately to nearest, ties to even; never fuse or reassociate
K groups. Preserve subnormals, signed zeros and IEEE exceptions, without FTZ or
saturation. Canonical FP32 quiet NaN is 0x7fc00000 when arithmetic produces NaN.
Weight scales may be any finite FP16, including signed zero/subnormal/negative;
activation scales must be finite and strictly positive FP32. Reject nonfinite
weight or nonpositive/nonfinite activation scales as error 6. Each output flag
byte ORs flags over its own groups; scalar status additionally ORs all outputs.
DOT produces exact int32 and no FP flags. Activation quantization remains a
separate software/RVV kernel with a separately frozen numerical contract; the
existing deterministic A8 fixtures do not validate model quantization quality.

These instructions neither read nor change `frm`, `fflags`, `fcsr`, RVV vtype,
vl/vstart, vector registers, VME tiles/mtype or mstatus FS/VS/MS. FP state is
private to the command. Do not label its dedicated FPnew unit as reuse of the
physical scalar FPU. The queue, ticket, accumulator and outstanding transactions
are accelerator state owned by the driver, not ABI-preserved register state.
Firmware must drain/reap before handing ownership to another context.

Revision 0.1 targets Coral's machine-mode firmware environment. Lower-privilege
execution, if added to the core, must trap rather than bypass memory policy.
Absent extension, reserved encoding, or forbidden privilege raises the existing
illegal-instruction fault with no side effects, original PC and instruction
word recorded through the core fault path (mcause=2). Descriptor/operand faults
are asynchronous status errors, not delayed CPU load/store traps. Interrupts
after a committed submit retain its ticket; interrupts before commit must not
leave a command enqueued. No job-completion interrupt is added.

Reset is whole-path coordinated recovery: stop admission, drain or invalidate
the external transaction epoch, reset the core/queue/engine and restart the
driver together. Clear private state and completion, start ticket sequence at 1,
and discard all old software tickets and output buffers. Tickets are not unique
across reset. A core-only reset while DMA remains alive is forbidden; it must
be interlocked by integration. Current standalone reset assumes its memory
service flushes old responses and does not implement this production behavior.

## Memory ordering and kernel mapping

Submit has release semantics for prior scalar/RVV stores in the shared pool:
no descriptor/operand fetch can observe a state older than those stores. Poll
or reap returning terminal has acquire semantics: all command output writes
have received successful completion (or drained on error) before later CPU
loads proceed. The dispatcher/LSU/adapter must implement these obligations;
the existing standalone ready/valid ports provide neither. Ordinary FENCE does
not wait for an accelerator job to finish. Use fences around ownership transfer
as below and retain a compiler memory clobber in every wrapper. A fence does not
flush a noncoherent cache: ABI1 requires the validated uncached pool, not cached
aliases. Host PCIe publication additionally follows the existing platform flush
and synchronization procedure; CPU fences alone do not order host writes.

```text
# GEMM Y[M_total,N_total] = scaled A8 * native PQ2, K <= 6144
quantize_activations_with_frozen_reference_rules(A_float, A8, scales)
for m0 in range(0, M_total, 8):
  mt = min(8, M_total-m0)             # decode has M_total=1
  pack_group_major_A_and_scales(m0, mt)
  for n0 in range(0, N_total, 32):
    nt = min(32, N_total-n0)
    # Reuse prepacked W across token tiles; packing is not free.
    desc = abi1(SCALED_F32, mt, nt, ceil(K/128), W_slab[n0], A, S, Ytile, F)
    fence rw,rw
    ticket = bt.submit(desc)
    require(ticket != 0)             # single owner expects an empty slot
    do:
      status = bt.poll(ticket)
      enforce_driver_deadline()      # timeout escalates; never reuse DMA tags
    while status.state == BUSY
    require(status.state in {DONE_OK,DONE_ERR})
    saved = bt.reap(ticket)
    fence rw,rw
    if saved.state == DONE_ERR: discard_tile_and_report(saved)
    else: scatter_Ytile_and_flags_to_output(m0,n0)
```

For prefill M32 there are four token tiles per row tile; decode M1 uses one.
Use ordinary scalar/RVV loads/stores for packing/scattering; normalization,
activation, attention and KV work remain on existing Coral kernels. Double
buffer CPU preparation with an active job only in disjoint allocations; ABI1
has one accelerator command slot, not a multi-command overlap guarantee.
The internal adapter may overlap loads/compute when dependencies permit, but
neither this ISA nor existing simulation proves HBM overlap or bandwidth.

For a DOT128 microbenchmark use operation 0, G=1, S=0, F=0 and the same submit/poll/reap
sequence, then read int32 Y. Scale bits remain in W for software if needed.
This PQ2 format is **not** ERG-103's native binary Q1_0 or its differently coded
synthetic ternary format. The [ERG-103 mailbox contract](https://github.com/Ergodex-Core/bonsai_on_chip/blob/3ee0f99eb7216fe41ba55e719584c662313d3b10/doc/spec/first_slice.md)
uses DOT1 registers, a sealed DDR store and epochs, not CPU opcodes. It must
retain its actual-weight, sealed-memory/driver/mailbox simulator and FPGA gate;
this proposal neither replaces that gate nor claims PR2 is complete.

## Why a descriptor extension and what remains to integrate

A tightly coupled vector DOT instruction could consume resident activation and
weight vectors and write a VME tile quickly, but requires vector/tile ports,
dependency/exception handling and a separately encoded scale/reduction contract.
It does not eliminate HBM packing or movement. The current candidate is already
a memory-fed tile with private reduction state, so three scalar queue operations
are the smaller initial integration surface. Compared with pure MMIO, they give
explicit scalar dependencies and defined submit/completion ordering, at the cost
of real CPU decoder/dispatch changes. Pure MMIO is still a viable earlier
integration experiment, but would not implement this CPU ISA extension.

Engine count and lookup/direct choice are implementation parameters. Start the
integration candidate at two DOT128 engines and preserve the current 25 MHz
target. Increasing engines does not change a descriptor or instruction word.
No source-level spec establishes FPGA area, routing, Fmax, power, HBM bandwidth
or end-to-end speedup. The separately preserved local optimized Q timings are
lookup-only, not evidence for the direct-SIMD optimized path.

| Work item | Required implementation and validation |
| --- | --- |
| Decode/dispatch | Add enableBonsai feature and exact masks; readsRs1/writesRd/rdMark, slot0-only, undefined fault, once-only acceptance, scalar response/writeback arbitration, flush/interrupt interlock; feature-off and all VME/RVV/FP regressions |
| Queue/descriptor | One slot, immutable descriptor snapshot, ticket/status FSM, range/alias checks, no reads on rejected submit, acquire/release semantics, every descriptor field/error/reap/stale-ticket test |
| Memory service | Real AXI IDs and response routing, arbitration, CDC, pool policy, strobed output stores, write acknowledgements, error drain/epoch reset; current Alex master fixes IDs to zero so the standalone 16-tag bus cannot connect unchanged |
| Storage | Bank/port/capacity plan for activation/weight buffers and accumulator; current register arrays do not establish mapped BRAM efficiency |
| Numerical core | Reuse existing dot/scaled tops, check canonical NaNs and flags, preserve per-output K ordering across scheduling changes; compare every output to independent oracle |
| Toolchain/simulator | Initially .insn wrappers; later binutils/LLVM/disassembler entries with these masks, feature gating and reserved-word tests; functional simulator command model plus CPU-driven RTL execution, no claim from host-only encoding tests |
| Driver/model kernels | Uncached allocations, pooling/packing, deadlines, ownership, quantizer reference, fallback and tails; include costs in matched Coral/Bonsai prefill and decode timings |
| Physical acceptance | Owner-coordinated build after interface qualification, unchanged 25 MHz target, timing/CDC/resource evidence and actual FPGA memory/run validation; no build authorized by this spec |

Future decoder tests must cover all rd/rs1 aliases and reserved fields, feature
disabled, illegal privilege, RAW/WAW hazards, same-cycle faults/interrupts,
replay prevention, unrelated scalar execution while BUSY, and a missing DMA
response with successful polling. DMA tests must inject every error and reset
phase, late/stale response, partial final write, output backpressure and address
wrap. End-to-end CPU tests must run DOT plus Q M1/M32, both direct/lookup variants,
and all seven projection shapes with identical numerics before performance
comparison. Repository-wide core tests remain required after decoder changes.

Run the current, much narrower encoding/assembler audit with:

```bash
python3 experimental/bonsai_lookup/isa/check_encodings.py \
  --clang /path/to/riscv-capable/clang --out /tmp/bonsai-isa-check
```

It checks proposed words and reserved fields, verifies non-overlap against the
pinned local sources, and asks a real RISC-V assembler to emit all valid register
combinations through .insn. It does not instantiate hardware, validate queue
semantics or compile a new FPGA image. Its report records that boundary.
