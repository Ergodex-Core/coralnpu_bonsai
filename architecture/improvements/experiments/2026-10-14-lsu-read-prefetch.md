# LSU read-prefetch bridge (DBus2AxiStream)

| | |
|---|---|
| Date | 2026-10-14 |
| Owner | Claude Code (with Rachit) |
| Branch / worktree | `arch-improvements` @ `1986101d` (RTL); PR #12; base `main` @ `3124faeb` |
| Change class | A: memory RTL |
| Status | **win, confirmed on F2** |

## Result (one line)

Gemma 3 1B fused decode at F2-calibrated latency: **41.2 → 23.3 s/token (1.78×)** at 25 MHz. At 100-cycle latency: **171 → 24.7 s/token (6.9×)**. External read bandwidth **0.52 → 3.98 B/cycle** with 16 B loads and **7.68 B/cycle** with 128 B loads (latency 24). Device cycles only, Verilator, **est.**

## 1. Hypothesis

- **Bottleneck:** `DBus2AxiV2` issues one single-beat AXI read per LSU request and waits for it (`hdl/chisel/src/coralnpu/DBus2Axi.scala`, `dbus.ready = readFinished`), so every external load pays the full latency. On F2, membench measures 0.65–0.77 B/cycle.
- **Change:** a read bridge with 8 sequential streams. Each stream prefetches up to 16 rows ahead, using INCR bursts of up to 4 beats with up to 16 in flight on one ID. Rows already buffered return on the next cycle.
- **Prediction:** reads at ≥2.8 B/cycle, which is the target. Decode becomes compute-bound (~6 MAC/cycle issue ceiling), so the token gain is ~2× at F2 latency and grows with latency.
- **Arithmetic:** Little's law. 256 B in flight per stream ÷ (L + ~10) cycles: ≈10 B/cycle at L=15, ≈2.3 B/cycle at L=100. The LSU issues one 16 B row per ~4 cycles, so 16 B loads cap near 4 B/cycle.

## 2. Implementation

- Variant: `VmeCoreMiniPfAxi` (`--ebusPrefetchStreams=8`). Off by default, so every existing core is unchanged.
- Files:
  - `hdl/chisel/src/coralnpu/DBus2AxiStream.scala` (new, ~165 lines)
  - `DBus2Axi.scala`, `Parameters.scala`, `CoreAxi.scala` (flush on core reset/halt), `Core.scala`, `BUILD`
- Write ordering: writes are unchanged and still wait for B. A write blocks new read bursts and drops any stream that holds its row.
- Reads below `0x80000000` (CSR, UART) keep V2's exact address and size and are never kept.
- Area: 2 KiB row buffer (`data_128x128`), a 16-entry burst queue, and 8 address comparators.

## 3. Correctness

| Tier | Command | Baseline | Candidate |
|---|---|---|---|
| T0 | elaborate `VmeCoreMiniPfAxi`; F2 generator regen | pass | pass; run-054 regen bit-exact for base |
| T1 | `//hdl/chisel/src/coralnpu:coralnpu_dbus2axi_stream_tests` (4 directed cases vs a cycle-level AXI memory) | — | pass |
| T2 | core regression | unchanged cores (bridge off) | **not run on the prefetch core** |
| T3 | `kernels_cocotb_test{,_pf}`, 9 tests, clang-18 ELFs | pass at L = 0/24/100 | pass at L = 0/24/100 |
| T3 | `model_bench_cocotb_test{,_pf}`: real layers vs `ModelRef` | running | running |
| T4 | ArcSim cross-check | — | ArcSim VME build in progress |
| T5 | UVM co-sim | — | not run |
| T6 | F2: `fpga_kernels_test`, `fpga_fused_test`, token runs | pass | pass; tokens identical to baseline in all 11 runs |

New directed test: `DBus2AxiStreamTest.scala`. Its write-coherence case fails on a build with the write invalidation disabled (mutation checked 2026-10-14). The other three cases cover burst and outstanding behaviour, `flush`, and exact uncached reads.

## 4. Performance (simulation)

Tool: Verilator (cocotb), clang-18 kernel ELFs, decode (`m = 1`). Memory latency is the sim's pipelined AXI model. **Calibration:** 15 cycles reproduces F2. The fused baseline estimate is 41.2 s/token vs 41.4 measured, and the baseline membench gives 0.73 B/cycle vs 0.65–0.77 measured.

**External bandwidth (membench, 64 KiB, B/cycle):**

| Access | L=15 base → pf | L=24 base → pf | L=100 base → pf |
|---|---|---|---|
| read, 16 B loads | 0.73 → **3.98** | 0.52 → **3.98** | 0.15 → **2.34** |
| read, 128 B loads | 0.79 → **10.5** | 0.55 → **7.68** | 0.15 → **2.34** |
| write | 0.70 → 0.70 | 0.50 → 0.50 | 0.15 → 0.15 |

**Per-token estimate, fused kernel (`gemma_decode.S`), est., device cycles only:**

| Model | Latency | Baseline s/token @25 MHz | Prefetch | Speedup |
|---|---|---|---|---|
| Gemma 3 1B | 15 (F2) | 41.2 | 23.3 | **1.78×** |
| Gemma 3 1B | 24 | 55.0 | 23.4 | 2.35× |
| Gemma 3 1B | 100 | 171 | 24.7 | **6.9×** |
| FunctionGemma 270M | 24 | 16.2 | 6.5 | 2.5× |
| FunctionGemma 270M | 100 | 50.7 | 7.1 | 7.1× |

**Per-op kernels:** Gemma 3 1B, L=24: 67.6 → 22.5 s/token (3.0×). L=100: 222 → 25.9 (8.6×). Decode matvec: 795 → 261 cycles per 512 MACs at L=24.

**Measured vs predicted:**
- Agree: reads reach ≥2.8 B/cycle at L ≤ 24, and the token gain at F2 latency is ~1.8×.
- Prefetch-core decode is now nearly latency-independent (586 → 616 Mcycles from L=24 to 100). It is limited by instruction issue and matvec compute: 395 of 583 Mcycles at L=15.

**Next bottlenecks:**
1. Stream depth at silicon latency. 2.34 B/cycle at L=100 fits 256 B in flight; 7 B/cycle needs ~64 rows per stream. `ebusPrefetchDepth` sweep.
2. Writes: still one at a time (0.5 B/cycle). Posted writes are next.
3. matvec instruction issue: ~11 instructions per `vtmms` (compute item P1 in `01-gemma-inference-priorities.md`).

## 5. FPGA

New image built with `fpga/f2_afi/build_afi.sh`:
- **AFI `afi-08098031e6fce544c` / AGFI `agfi-0074b31b5fd1b9e53`**
- timing met: WNS +0.029 ns, WHS +0.010 ns, 0 violations
- shell timing pins identical to run 054

A/B on one F2 card (test instance `coralnpu-pr12-f2-test`, since terminated), against run 054 (`agfi-075a98d106eb1b6bf`). Same clang-18 kernels, weights and host code; fused decode kernel. Raw results: `2026-10-14-lsu-read-prefetch/fpga/`.

- **Correctness:**
  - `fpga_kernels_test` and `fpga_fused_test` pass on both images.
  - **Every run is token-identical between the two images** (11 runs, two models).

| Model | Prompt | TTFT before → after (s) | TTFT speedup | Decode before → after (s/token) | Decode speedup |
|---|---|---|---|---|---|
| FunctionGemma 270M | chat | 76.4 → 47.5 | 1.61× | 11.64 → 6.62 | **1.76×** |
| FunctionGemma 270M | ISL 8 / OSL 16 | 58.3 → 38.3 | 1.52× | 11.56 → 6.60 | 1.75× |
| FunctionGemma 270M | ISL 32 / OSL 16 | 157.6 → 95.9 | 1.64× | 11.90 → 6.66 | 1.79× |
| FunctionGemma 270M | ISL 64 / OSL 16 | 330.2 → 196.6 | 1.68× | 12.33 → 6.73 | 1.83× |
| FunctionGemma 270M | ISL 96 / OSL 16 | 514.7 → 299.2 | 1.72× | 12.76 → 6.84 | 1.86× |
| FunctionGemma 270M | ISL 32 / OSL 32 | 158.0 → 96.0 | 1.65× | 12.00 → 6.69 | 1.79× |
| Gemma 3 1B | chat | 310.8 → 156.6 | **1.98×** | 41.46 → 23.39 | **1.77×** |
| Gemma 3 1B | ISL 8 / OSL 8 | 210.8 → 105.3 | 2.00× | 41.29 → 23.37 | 1.77× |
| Gemma 3 1B | ISL 32 / OSL 8 | 625.8 → 312.2 | 2.00× | 41.81 → 23.45 | 1.78× |
| Gemma 3 1B | ISL 64 / OSL 8 | 1272.8 → 634.7 | 2.01× | 42.38 → 23.59 | 1.80× |
| Gemma 3 1B | ISL 32 / OSL 16 | 626.9 → 311.9 | 2.01× | 41.85 → 23.49 | 1.78× |

**membench on F2, B/cycle (DDR and HBM agree within 0.05):**

| Read size | Before | After |
|---|---|---|
| 16 B | 0.66 | **4.00** |
| 32 B | 0.70 | **6.37** |
| 64 B | 0.74 | **6.69** |
| 128 B | 0.74 | **6.79** |

Writes are unchanged (0.66–0.77).

**Prediction vs hardware:**

| Gemma 3 1B decode | Predicted | Measured |
|---|---|---|
| Fused routines, sim latency 15 | 23.3 s/token, 1.78× | **23.39 s/token, 1.77×** |
| VisualSim-model twin | 1.77× | 1.77× |

For 16 B reads, the predicted 3.98 B/cycle (LSU-issue bound) compares with **4.00** measured.

**TTFT** improves more than decode (2.0× for 1B). Prefill runs the per-op kernels, which re-read activations from DRAM and so gain more from prefetch (3.0× per-op in sim at latency 24).

## 6. Reproduce

```bash
# Kernels (clang-18), then correctness on both cores at three latencies
models/firmware/build.sh /tmp/fw
for L in 0 24 100; do
  bazel test //models/tests:kernels_cocotb_test //models/tests:kernels_cocotb_test_pf \
    --test_env=KERNEL_DIR=/tmp/fw --test_env=AXI_READ_LATENCY=$L --test_env=AXI_WRITE_LATENCY=$L
done
# Fused-routine perf (5 testcases x 2 cores), then the token estimate
bazel test //models/tests:kernels_fused_perf_cocotb_test{,_pf}_test_fused_{matvec_dim,matvec_attn_out,matvec_down,elementwise,attention} \
  --test_env=KERNEL_DIR=/tmp/fw --test_env=PERF_MODEL=gemma3-1b --test_env=AXI_READ_LATENCY=15 --test_env=AXI_WRITE_LATENCY=15
python3 -m models.tests.estimate_e2e --fused base.jsonl pf.jsonl   # PERF lines grepped from test.log
# Bandwidth
bazel test //models/tests:kernels_perf_cocotb_test{,_pf}_test_perf_membench --test_env=KERNEL_DIR=/tmp/fw --test_env=AXI_READ_LATENCY=24
```

Raw evidence: `2026-10-14-lsu-read-prefetch/*.jsonl` (`PERF` lines, one file per model × latency × core).
