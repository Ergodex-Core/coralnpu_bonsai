# Calibrated CoralNPU decode model for Mirabilis VisualSim

`coral_decode_model.py` is a structural performance model of the fused Gemma
decode step (`models/asm/gemma_decode.S`), calibrated against Verilator. It
generates a VisualSim model (`xml`) and evaluates the same equations in Python
(`validate`, `sweep`), so design points can be explored in seconds without
RTL or FPGA synthesis (see `/mirabilis_utility.md` on `main`).

## Mechanism

- **Baseline LSU bridge (`DBus2AxiV2`):** every external 16 B row, read or written, waits the memory latency L. A routine takes `A + (R + W)·L` cycles, where R and W are rows read and written, computed from the model shapes. `A` is the latency-free work per routine, fitted from **baseline** Verilator runs only. Verilator confirms this exactly: for every matvec routine, the cycle count grows with L at precisely R + W cycles per cycle of latency.
- **Prefetch bridge (`DBus2AxiStream`):** buffered reads cost `delta` per load instruction, while writes and stream starts still wait L: `A + (R/rows_per_load)·delta + (W + S)·L`. This is bounded below by the stream bandwidth `min(16 B / 4 cycles, depth·16 B / (L + 10))`.
- **One fitted constant:** `delta` = 3.72 cycles, from the marginal cost of one matvec tile, Gemma 3 1B, L = 15.

## Validation (prefetch core predicted, not fitted)

| Model | Latency | Predicted Mcycles/token | Verilator | Speedup predicted / measured |
|---|---|---|---|---|
| Gemma 3 1B | 15 | 583.2 | 582.5 | 1.77× / 1.77× |
| Gemma 3 1B | 24 | 586.5 | 585.8 | 2.34× / 2.35× |
| Gemma 3 1B | 100 | 617.0 | 616.4 | 6.94× / 6.95× |
| FunctionGemma 270M (not used for fitting) | 24 | 163.4 | 163.2 | 2.47× / 2.48× |
| FunctionGemma 270M | 100 | 178.5 | 178.4 | 7.11× / 7.11× |

Per routine (48 predictions), median error is 3.6%, max 12.9% (small elementwise ops).

## Knobs

`Latency`, `Core_Prefetch`, `Posted_Writes`, `Stream_Depth`, and `Matvec_Compute_Scale`. The last one is the speedup of matvec issue, e.g. fused scale-accumulate in the matrix engine.

## Use

```bash
EV=architecture/improvements/experiments/2026-10-14-lsu-read-prefetch
python3 tools/visualsim/coral_decode_model.py validate --evidence $EV
python3 tools/visualsim/coral_decode_model.py sweep --evidence $EV --model gemma3-1b
python3 tools/visualsim/coral_decode_model.py xml --evidence $EV --model gemma3-1b --out Coral_Decode_gemma3-1b.xml
```

Run the XML with VisualSim's batch runner (as in the Oct 3 smoke test):

```bash
VS=~/VisualSim/VS_AR
java -Dvs.lic=default -Djava.security.manager=allow -Djava.security.policy=$VS/bin/policyAll \
  -classpath "$VS:$VS/lib/*:$VS/com/amity/flexlm/*" VisualSim.actor.gui.VisualSimBatchModeSimulator \
  -resultpath results Coral_Decode_gemma3-1b.xml
```

Token cycles = Σ `cycles_per_token` in `results/token-rows.txt` + `Other_<core>_At24` (attention and embed). The `xml` command prints the number the VisualSim run must reproduce.

**Status 2026-10-15:** the VisualSim licenses (Mac and EC2) expired on 2026-10-15, so the XML has not been run in VisualSim yet. All numbers here come from the Python twin of the same equations.
