# LSU read-prefetch bridge (DBus2AxiStream)

| | |
|---|---|
| Date | 2026-10-14 |
| Owner | Claude Code (with Rachit) |
| Branch / worktree | `arch-improvements` @ `1986101d` (RTL); PR #12; base `main` @ `3124faeb` |
| Change class | A: memory RTL |
| Status | win in simulation; FPGA image building |

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
| T6 | F2 | — | AFI building (`fpga/f2_afi/`) |

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

New AFI for `VmeCoreMiniPfAxi`-equivalent `CoralF2Axi`, built with `fpga/f2_afi/build_afi.sh`:
- Run-054 package, HDK 2.3.0, Vivado 2025.2, 25 MHz.
- Regenerating the baseline reproduces run 054's RTL bit-exactly.
- Variant RTL differences are all from the bridge (15 files; see `package_pf/rtl_diff.txt` on the build instance).
- Status: Vivado synthesis started 2026-10-14 ~14:00 UTC on `coralnpu-pr12-afi-build`.
- AFI submission and F2 validation: pending, needs approval.
- **Prediction to check on F2:** Gemma 3 1B fused decode ≈ **23 s/token** (from 41.4).

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
