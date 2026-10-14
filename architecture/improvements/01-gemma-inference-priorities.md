# Gemma inference: priority architecture improvements

Ranked hardware and system changes that would raise **TTFT** and **decode tokens/s** for
Gemma 3 1B and FunctionGemma 270M on CoralNPU. Each ranking is based on the FPGA, Verilator and
ArcSim runs to date (2026-10-14, branch `model-kernels-firmware`).

Numbers marked **est.** are projections. The arithmetic behind each one is given in §4.

## 1. Baseline

| Item | Value | Source |
|---|---|---|
| FPGA image | `CoralF2Axi` @ 25 MHz, 8 KiB ITCM, 32 KiB DTCM, RVV VLEN 128, Zvt int8/f32 (BF16 FMM off) | `report/report.tex` |
| FunctionGemma 270M, chat prompt, ISL 16 / OSL 8 | **TTFT 66.8 s, 27.7 s/token (0.036 tok/s)**, token-identical to MLX | `report/results/fg_chat.json` |
| Gemma 3 1B | expected 50–60 s/token (run in progress) | `report/report.tex` |
| MLX reference (M2 Max) | FG 270M: 1.13 s TTFT, 53 tok/s. Gemma 3 1B: 0.96 s, 56.9 tok/s | `report/results/mlx_*.json` |
| External memory bandwidth | **0.65–0.77 B/cycle**, ~24 cycles per 16 B load. Same for DDR and HBM, and it does not change with vector width | `models/tests/membench.py` |
| matvec decode (m = 1) | 645 cycles per 512 MACs (~0.8 MAC/cycle), near the memory floor | commit `d1d15cf6` |
| matvec instruction-issue ceiling | ~11 instructions per `vtmms` ⇒ ~6 MACs/cycle, plus one tile-row drain per 32-group | `models/README.md` |
| Time split, first FG run (235 s) | matvec 120 s (637 launches), scalar GELU 12 s, **~100 s elsewhere** (attention, top-k, norms, launches, host sequencing) | `report/report.tex` |
| Vocabulary top-k | 262,144 logits in 1.8 M cycles (73 ms) | `report/report.tex` |
| ArcSim (VmeCoreMiniAxi, matrix on) | 8,484 sim-cycles/s. Synthetic 4-token BF16 fixture = 1.51 M cycles. Token timing diverges from Verilator after cycle 13,101 | `arcsim-coral-matrix-baseline-20261013.zip` |
| Verilator | Kernel-level correctness and cycle counts only (`models/tests/kernels_cocotb_test.py`). No full-token runs | `models/README.md` |

**Simulator reach.** One FunctionGemma decode token is about 27.7 s × 25 MHz ≈ 690 M cycles. At 8.5 k
cycles/s that is about **22 hours in ArcSim**, and Verilator is slower still. Both simulators are
therefore tools for kernel-level A/B tests. Full-model numbers have to come from the FPGA (or from
MPACT once it is calibrated).

## 2. Where the time goes

**Root cause in the RTL.** External memory is limited by latency, not by bus width:

- `LsuV3` keeps a **single instruction in flight** ([ch. 3 §3.3.1](../03-memory-subsystem.md#331-overview)).
- `DBus2AxiV2` issues **single-beat** AXI (`len = 0`) and accepts no new request until the current
  one finishes, so there is **one outstanding transaction** ([ch. 3 §3.9.1](../03-memory-subsystem.md)).
- The result is about 24 cycles per 16 B load, or about 0.7 B/cycle.

| Per decode token | FunctionGemma 270M | Gemma 3 1B |
|---|---|---|
| Weight bytes streamed (int4 Q4_0) | ~65 MB layers + ~150 MB tied-embedding logits ≈ **215 MB** | ~350 MB + ~150 MB ≈ **500 MB** |
| Streaming floor at 0.7 B/cycle, 25 MHz | ~307 M cycles ≈ **12.3 s** | ~714 M cycles ≈ **28.6 s** |
| MACs per token (≈ parameter count) | ~0.27 G | ~1.0 G |
| Compute floor at 6 MACs/cycle | ~45 M cycles ≈ 1.8 s | ~167 M cycles ≈ 6.7 s |
| Measured | 27.7 s | (50–60 s expected) |

Three points follow from this table:

1. **Memory latency is the main limit.** It is about 5–7× the compute floor.
2. **About half of the FunctionGemma token time (~15 s) is not explained by weight streaming.**
   The candidates are kernel launches over PCIe, scalar loops paying ~24 cycles per external
   access, attention/KV traffic, and top-k. There is no counter that attributes this time yet.
3. **The logits projection dominates FunctionGemma.** The 262k-row vocabulary matrix is about 70% of
   its bytes per token.

**TTFT.** Prefill runs in 16-token chunks, and each chunk streams the weights once. TTFT is
therefore about ⌈ISL/16⌉ × (chunk time). The first FG chunk took 66.8 s, about 2.4× a decode step.
The extra time beyond the "≈ one decode step" in the report has not been attributed yet.

## 3. Priority table

**Legend.**

- **Pri:** P0 = largest gain or a prerequisite; P1 = large gain once P0 is done; P2 = incremental.
- **Metric:** TPS = decode tokens/s.
- **Effort:** S = days, M = weeks, L = a month or more of RTL plus verification.

| Pri | Feature / modification | Bottleneck evidence | Metric | Est. gain | Effort | Where |
|---|---|---|---|---|---|---|
| **P0** | **Multiple outstanding AXI reads + INCR bursts** in the LSU→AXI bridge (≥ 8 outstanding IDs, 4–16-beat bursts, R reordering by ID) | 24 cycles per 16 B, `len = 0`, 1 outstanding → 0.7 B/cycle | TPS, TTFT | **4–8× effective bandwidth** (est.), until compute-bound | M | `DBus2Axi.scala`, `CoreAxi.scala` AXI queues |
| **P0** | **Non-blocking vector/tile loads**: a load queue so `LsuV3` can issue the next vector or tile load before the previous one returns | `LsuV3` single in flight; the LSU waits for `dbus.ready` | TPS, TTFT | Required for the row above to pay off | M–L | `scalar/Lsu.scala` (`LsuSuperSlot`), `rvv2lsu` / `vme2lsu` paths |
| **P0** | **Weight-streaming DMA into DTCM with double buffering**, overlapping weight fetch with `vtmms`. Either reuse `bus/DmaEngine` ([ch. 6 §6.10](../06-soc-interconnect-and-peripherals.md#610-dma-engine-busdmaengine)) behind `CoralF2Axi`, or add a core-local streamer | Load and compute are serialized in matvec; the kernel already sits at the memory floor | TPS, TTFT | Up to ~2× on top of the AXI fix (est.); also helps if the LSU change slips | M | F2 top, `CoreAxi`, kernel `matvec_q4.cc` |
| **P0** | **Hardware perf counters**: `mhpmcounter3..N` / `mhpmevent` for AXI read/write beats, LSU stall cycles, RVV and Zvt busy, dispatch stall; readable over `CoreAxiCSR` | ~50% of FG decode time is unattributed; only `mcycle` / `minstret` exist | Enabler | Directs every later item | S–M | `scalar/Csr.scala`, `CoreAxiCSR.scala` (see `instrumentation/PLAN.md`) |
| **P0 (fix)** | **External-window base read bug.** The core reads wrong data in the first bytes of the HBM window; the runtime skips 64 KiB | Bring-up finding | Correctness | — | S | AXI address path / F2 shell glue |
| **P1** | **Wider LSU/AXI datapath** (256- or 512-bit; F2 host-side DDR is 512-bit) | 16 B per beat; `lsuDataBits = 256` does not elaborate `CoreAxi` (`TCM128` is hard-wired) | TPS, TTFT | ~2× bytes per beat (est.) after P0 | L | `Parameters.scala`, `TCM.scala`, `Lsu.scala` |
| **P1** | **Larger ITCM/DTCM** (the `CoreMini_ITCM512KB_DTCM512KBAxi` variant exists) → one persistent fused firmware per layer or model instead of one ELF per operator, and a bigger prefill chunk M | 8 KiB ITCM forces host-sequenced kernels (637 matvec launches per run); 32 KiB DTCM caps M at 16 | **TTFT** (×M/16), TPS (fewer launches) | TTFT ~4× at M = 64 for long ISL (est.); removes most launch overhead | M (config + timing closure on F2) | `hdl/chisel/src/coralnpu/BUILD`, F2 build, `models/host/runtime.py` |
| **P1** | **Fused Q4 group-scale accumulate in Zvt**: apply the per-32 f16 scale inside the PE or accumulator, with no tile-row drain per group | ~11 instructions per `vtmms`; drain per group; ~6 MACs/cycle ceiling | TPS once memory is fixed | ~2–3× compute ceiling (est.) | L | `design/Zvt/zvt_pe_array.sv`, `zvt_ctrl.sv`, decoder |
| **P1** | **Higher FPGA core clock**, 25 → 50+ MHz (the SoC default and the DDR core-sim already use 50 MHz) | Everything is cycle-bound; wall time scales with clock, provided memory latency in cycles holds | TTFT, TPS | ~2× wall time | M (timing closure) | F2 build, `fpga.md` |
| **P2** | **On-device logits + top-k fused into the vocab matvec**, plus an optional vocabulary shortlist for FunctionGemma tool-calling | ~150 MB per token for logits (≈ 70% of FG bytes); top-k is a separate 1.8 M-cycle pass over HBM | TPS (mostly FG) | 10–40% on FG (est.) | S–M | `topk.cc`, `matvec_q4.cc`, host |
| **P2** | **Vector exp/tanh support** (LUT or polynomial in an RVV FU, or a `vfexp`-class op) for GELU and softmax | Scalar GELU 12 s of 235 s | TTFT, TPS | ~5% | M | RVV backend FU; `attention.cc`, GELU kernel |
| **P2** | **Enable `ZVTBF16FMM_ON` on F2 and use a bf16 KV cache** | KV cache and attention are f32; BF16 FMM is off in the F2 build | Long-context TTFT and TPS | ½ KV bytes; matters for ISL ≥ 512 | S (flag) – M (kernels) | `flags.bzl`, `fpga.md`, `attention.cc` |
| **P2** | **Faster RTL simulation for kernel A/B** (ArcSim partitioning/PGO, kernel-sized fixtures) | 8.5 k cycles/s; full tokens are infeasible | Methodology | Turnaround on all of the above | M | ArcSim bundle |

**Dropped for now: speculative decoding.** It would use the 15 tile rows that sit idle in decode.
On F2, Gemma 3 1B with a 7-token Gemma 3 270M draft gave only **+9% tokens/s** (0.0150 vs
0.0138 tok/s, 50% acceptance, `g1b_chat_spec.json`), so it is off the list.

## 4. Projected stacking (est.)

These projections assume:

- Compute and memory overlap (P0 DMA / non-blocking LSU).
- The ~15 s of unattributed FunctionGemma overhead is removed by persistent firmware (P1).
- Decode tok/s = 1 / max(bytes ÷ BW, MACs ÷ MAC-rate).
- "1 GHz" assumes the memory system sustains the same bytes per cycle at 1 GHz (8 B/cycle = 8 GB/s, LPDDR-class).

| Configuration | BW (B/cycle) | MAC/cycle | FG 270M tok/s @25 MHz | Gemma 3 1B tok/s @25 MHz | FG / 1B tok/s @1 GHz |
|---|---|---|---|---|---|
| Today (measured / expected) | 0.7 | ~0.8 effective | 0.036 | ~0.02 | — |
| P0 only (4 B/cycle, overhead unchanged) | 4 | 6 | ~0.06 (overhead-bound) | ~0.15 | — |
| P0 + persistent firmware | 4 | 6 | ~0.45 | ~0.15 | ~18 / ~6 |
| P0 + P1 (8 B/cycle, fused scales ~15 MAC/cycle) | 8 | 15 | ~0.9 | ~0.37 | ~37 / ~15 |
| + 50 MHz FPGA clock | — | — | ×2 | ×2 | — |

**Arithmetic for the P0 + P1 Gemma 3 1B row.** 500 MB ÷ 8 B/cycle = 62.5 M cycles, and
1.0 G MACs ÷ 15 MACs/cycle = 67 M cycles. The slower of the two is ~67 M cycles, which is 2.7 s per
token at 25 MHz and ~15 tok/s at 1 GHz.

**TTFT.** TTFT scales as ⌈ISL/M⌉ × (chunk streaming time). The P0/P1 bandwidth items cut chunk time
by the same factor as decode. A larger M (DTCM) cuts the chunk count. For FG at ISL 16, TTFT moves
from 66.8 s to an estimated ~2–4 s at 25 MHz with P0 + P1.

## 5. Gaps and next experiments

1. **Attribute the ~15 s per FG token.** Run `models/host/run.py --verbose` for per-kernel cycles and
   wall time. Add the P0 perf counters, and separate host and PCIe launch time from device time.
2. **Explain why the first prefill chunk is 2.4× a decode step** (66.8 s vs 27.7 s).
3. **Finish the Gemma 3 1B ISL/OSL sweeps** to confirm the 50–60 s/token expectation and the
   bytes-per-token model above.
4. **Kernel A/B in simulation before an FPGA rebuild.** Prototype the multi-outstanding AXI bridge
   and run `membench` plus one `matvec_q4` shape on Verilator or ArcSim. Success means B/cycle well
   above 0.7 with the readbacks unchanged. ArcSim's 12 matrix cases are the Zvt regression check.
5. **Calibrate MPACT** (`tools/estimate_tps.py` on `fpga-workflow`) against the FPGA kernel cycles,
   so the §4 projections can be checked without full RTL simulation.
