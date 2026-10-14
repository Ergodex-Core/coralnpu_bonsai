# ArcSim simulation

ArcSim, from Ergodex, is a cycle-accurate RTL simulator built on CIRCT's
arcilator. It compiles the same SystemVerilog the Verilator flow uses into a
native model. The `//tests/arcsim_sim` targets wrap that model in a C++
testbench with the same command line as the Verilator simulators in
`//tests/verilator_sim`.

| ArcSim target | Top module | Verilator equivalent |
| --- | --- | --- |
| `//tests/arcsim_sim:core_mini_axi_sim` | `CoreMiniAxi` | `//tests/verilator_sim:core_mini_axi_sim` |
| `//tests/arcsim_sim:rvv_core_mini_axi_sim` | `RvvCoreMiniAxi` | `//tests/verilator_sim:rvv_core_mini_axi_sim` |
| `//tests/arcsim_sim:vme_core_mini_axi_sim` | `VmeCoreMiniAxi` | none |

## Requirements

ArcSim is licensed and runs only on **Linux x86-64 with glibc 2.38 or newer**,
for example Ubuntu 24.04 or the Debian trixie dev container in
`utils/coralnpu.dockerfile`. On Apple Silicon, run the container with
`--platform linux/amd64`. That works, but emulation is slow, so measure
performance on native x86 hardware.

All ArcSim targets are tagged `arcsim`. They are skipped by default and need
`--config=arcsim`, like the VCS targets need `--config=vcs`.

## Installing the toolchain

Bazel finds ArcSim through the `@arcsim` repository (`rules/arcsim_repo.bzl`).
It checks these locations in order:

1. `$ARCSIM_HOME`
2. `<workspace>/arcsim-coral-matrix-baseline-20261013`
3. `~/.cache/arcsim/arcsim-coral-matrix-baseline-20261013`

**Developer bundle (default).** Extract and verify the bundle into the shared
cache, which every checkout and worktree picks up:

```sh
utils/arcsim/setup_arcsim.sh bundle ~/Downloads/arcsim-coral-matrix-baseline-20261013.zip
```

**Release package.** Install the `.deb`, activate your license, and point
Bazel at it:

```sh
utils/arcsim/setup_arcsim.sh deb ~/Downloads/arcsim_0.3.9_amd64.deb
export ARCSIM_HOME=/usr/lib/ergodex
```

The release package needs the matching `ergodex-site-tools` package. Its
simulators check the license when they run and exit with status 78 without one.

After changing `ARCSIM_HOME`, Bazel refetches `@arcsim` on its own. Run
`bazel fetch @arcsim//...` if it doesn't.

## Building and running

```sh
# Build a simulator.
bazel build --config=arcsim //tests/arcsim_sim:rvv_core_mini_axi_sim

# Build a program and run it, exactly as with Verilator.
bazel build //examples:coralnpu_v2_hello_world_add_floats
bazel-bin/tests/arcsim_sim/rvv_core_mini_axi_sim \
  --binary $(bazel cquery --output=files //examples:coralnpu_v2_hello_world_add_floats 2>/dev/null | grep '\.elf$')
```

The simulator prints the program output, then lines like these:

```
EXECUTION_CYCLES 12345
ARCSIM PASS model=RvvCoreMiniAxi cycles=... load_cycles=... evals=... wall_s=... cycles_per_s=...
```

The exit status is 0 on success, using the same rules as the Verilator
simulator. The run fails on `io_fault`, on a failing `tohost` exit code, on a
CSR status other than 1, or on a timeout.

| Flag | Meaning |
| --- | --- |
| `--binary` | ELF or raw binary to run. Required. |
| `--cycles` | Total cycle budget, including loading. Defaults to 100000000. |
| `--debug_axi` | Log AXI transactions. |
| `--trace`, `--instr_trace` | Accepted but not supported yet. No waveform or trace is written. |
| `--backdoor_load` | Accepted. The ELF is still loaded over AXI. |

## Differential tests

These tests run the same ELFs on ArcSim and Verilator and require matching
pass/fail results. They report cycle counts but don't compare them.

```sh
bazel test --config=arcsim //tests/arcsim_sim:all
```

## Performance comparison

Both simulators print a summary line when they exit. ArcSim prints
`ARCSIM PASS ... cycles=... wall_s=...` and Verilator prints
`VERILATOR PASS ... cycles=... wall_s=...`. The `*_perf` targets run each
workload on both simulators, alternating the order, and print a Markdown table
of median wall time, cycles per second and ArcSim's speedup:

```sh
bazel run --config=arcsim //tests/arcsim_sim:rvv_core_mini_axi_perf -- --runs=3 --cpu=3
```

The default workloads are the examples plus two RVV matmul kernels. Pin runs
to an idle CPU with `--cpu` for stable numbers.

To build and compare all three cores on a Linux x86-64 host, such as an EC2
instance, in one step:

```sh
utils/arcsim/run_perf.sh --runs 3 --cpu 3
```

The script never uses sudo. It reports any missing packages and writes results
to `perf-results/<timestamp>/`.

## Gemma 3 kernel bench

`//tests/perf/gemma3` estimates how fast the RVV Gemma kernels in
`tests/cocotb/rvv/ml_ops/gemma_kernels` would run at 25 MHz and 1 GHz. It uses
the layer shapes of a Gemma 3 270M single-token decode step on the highmem RVV
core.

```sh
bazel run //tests/perf/gemma3:gemma3_kernel_bench_verilator       # Verilator only
bazel run //tests/perf/gemma3:gemma3_kernel_bench_verilator_f2    # F2 memory latency
bazel run --config=arcsim //tests/perf/gemma3:gemma3_kernel_bench # both simulators
bazel run --config=arcsim //tests/perf/gemma3:gemma3_kernel_bench_arcsim -- \
  --kv_len=512 --freq_mhz=25,250,1000 --json=/tmp/gemma3.json
```

Each kernel runner times itself with `mcycle` and stores the result in a
`cycle_count` variable. The simulators set the shape variables with `--set`
and print `cycle_count` with `--dump`, so ELF loading is not counted. The
report has three parts:

- **Per-kernel table:** cycles on each simulator, the difference between them,
  latency at each clock, MAC/cycle, GMAC/s, and simulator wall time.
- **Per-layer and per-token decode estimates:** for fp32, bf16 and int8
  matmuls. These include tokens per second at each clock.
- **LM head:** the 640×262144 LM head is counted as 256 tiles. Use
  `--no_lm_head` to leave it out.

The estimates rest on these assumptions:

- **Tiling.** Matmuls larger than the runners' buffers run as tiles that fit,
  multiplied by the tile count. Accumulating partial sums between tiles is not
  included.
- **Input data.** Inputs keep their ELF initial values. The kernels' control
  flow depends only on shape, so this doesn't change the cycle count.
- **Memory latency.** By default, testbench memory, including DDR at
  `0x80000000`, responds in one cycle. Pass `--axi_read_latency=18
  --axi_write_latency=18` to approximate the F2 FPGA's memory path (see
  [External memory latency](#external-memory-latency)). Matmul weights live
  in DDR and activations in TCM.
- **Not modelled.** Host launch overhead (PCIe, sequencing) and the int4
  dequantisation the F2 firmware does.

### External memory latency

All three testbenches take the same latency knob, in core cycles:

| Testbench | Flag |
|---|---|
| Verilator SystemC (`//tests/verilator_sim:*_sim`) | `--axi_read_latency=N --axi_write_latency=N` |
| ArcSim (`//tests/arcsim_sim:*_sim`) | `--axi_read_latency=N --axi_write_latency=N` |
| cocotb (`coralnpu_test_utils`) | `--test_env=AXI_READ_LATENCY=N --test_env=AXI_WRITE_LATENCY=N` |

Read latency counts from AR acceptance to the first R beat. Reads are
pipelined, so several can be outstanding, like a DRAM controller. Write
latency counts from the last W beat to the B response. In the Verilator
testbench this relies on a small patch to the `axi2tlm` bridge
(`third_party/libsystemctlm-soc/axi2tlm_pipelined_read_latency.patch`).

**F2 calibration: N = 18.** `//models/tests:f2_calibration_cocotb_test`
replays `models/tests/fpga_kernels_test.py` on the Verilator `VmeCoreMiniAxi`
model. It uses the same kernels, shapes and external-memory buffers as the
F2 run, and prints simulated cycles next to the F2's measured cycles:

```sh
models/firmware/build.sh /tmp/k            # clang ELFs, as on the F2
bazel test //models/tests:f2_calibration_cocotb_test \
  --test_env=KERNEL_DIR=/tmp/k --test_env=AXI_READ_LATENCY=18 \
  --test_env=AXI_WRITE_LATENCY=18 --test_output=all | grep CALIB
```

Measured on 2026-10-14 with the kernels from 82f37317 (clang 19 on the
simulator host, clang 18 on the F2). Cycles are the kernel's own `mcycle`
delta:

| Kernel | F2 | Verilator L=0 | L=16 | **L=18** | L=24 |
|---|---:|---:|---:|---:|---:|
| matvec_q4 decode (k=1152, n=64) | 92,914 | 41,492 (0.45×) | 86,804 (0.93×) | **93,748 (1.01×)** | 114,580 (1.23×) |
| matvec_q4 (m=3, k=64, n=32) | 7,724 | 2,662 (0.34×) | 7,246 (0.94×) | **7,822 (1.01×)** | 9,550 (1.24×) |
| topk, 262,144 logits | 1,817,142 | 1,010,132 (0.56×) | 1,674,952 (0.92×) | **1,808,272 (1.00×)** | 2,208,232 (1.22×) |
| topk, 320 | 41,360 | 16,900 (0.41×) | 37,370 (0.90×) | **40,040 (0.97×)** | 48,050 (1.16×) |
| rmsnorm | 8,634 | 3,030 (0.35×) | 8,086 (0.94×) | **8,726 (1.01×)** | 10,646 (1.23×) |
| quant_act | 10,855 | 4,247 (0.39×) | 10,257 (0.94×) | **11,049 (1.02×)** | 13,425 (1.24×) |
| rope | 11,075 | 3,488 (0.31×) | 10,449 (0.94×) | **11,345 (1.02×)** | 14,033 (1.27×) |
| embed | 7,206 | 3,096 (0.43×) | 6,820 (0.95×) | **7,414 (1.03×)** | 9,196 (1.28×) |
| attention | 27,634 | 9,286 (0.34×) | 24,070 (0.87×) | **25,926 (0.94×)** | 31,494 (1.14×) |
| gelu_mul | 34,068 | 14,709 (0.43×) | 19,218 (0.56×) | **19,794 (0.58×)** | 21,522 (0.63×) |

`gelu_mul` is the one outlier. It is scalar and compute-bound, so its F2 gap
is not memory. Clang 18 against clang 19 code generation is the likely
cause. On the Verilator SystemC testbench, L=18 gives about
16 B / (4.45 + 18) cycles = 0.71 B/cycle for weight streaming. That falls
inside the F2's measured 0.65–0.77 B/cycle (`models/tests/membench.py`).

### `--set` and `--dump`

Both the Verilator and ArcSim AXI simulators accept these flags:

```sh
rvv_core_mini_highmem_axi_sim --binary=rvv_matmul.elf \
  --set=active_m=1,active_k=640,active_n=1024 --dump=cycle_count
# ...
# SYMBOL cycle_count=123456
```

Values are 32-bit integers, in decimal or `0x` hex, or `f32:<float>` for
float variables.

## CI on Blacksmith

`.github/workflows/sim-blacksmith.yml` runs on Blacksmith runners
(`blacksmith-16vcpu-ubuntu-2404`). It has two jobs:

- **verilator** builds the Verilator simulators and runs the examples.
- **arcsim** installs the developer bundle, runs the differential tests, and
  then runs all three `*_perf` comparisons on the same machine. The tables go
  to the job summary and the JSON results are uploaded as an artifact.

Before the workflow can run:

1. Install the Blacksmith GitHub app on the organization.
2. Add the repository secret `ARCSIM_BUNDLE_URL`. It must be an HTTPS URL for
   the bundle zip, such as a presigned S3 link. Without it, the ArcSim job
   skips its steps and says so in the summary.

Blacksmith runners are shared. Treat CI timings as a trend rather than a
benchmark.

## Options

- `--//rules:arcsim_assertions=false` builds with `--ignore-assertions
  -DSYNTHESIS`. The ArcSim benchmark bundles were validated in this
  configuration. The default keeps assertions, which matches Verilator but has
  not been validated with ArcSim yet.
- The `arcsim_model` rule in `rules/arcsim.bzl` takes `arc_args`, `opt_args`
  and `cpu`. Use them to try the bundle's experimental lowering passes or
  `cpu = "native"`. Compare results against the default build before trusting
  a faster configuration.

## How it works

`arcsim_model` runs `circt-verilog`, `arcsim`, `opt` and `llc`. That produces
a native model object and a `state.json` describing the model's state layout.
`utils/arcsim/gen_ports.py` turns the state file into `arcsim_model.h`. The
testbench in `tests/arcsim_sim/arcsim_sim.cc` looks ports up by name in that
header, so it keeps working when the RTL changes. The testbench copies the
Verilator testbench's behaviour:

- **ELF loading** goes over the core's AXI slave port, with read-back
  verification. The entry point is written to CSR+0x4, then the clock gate and
  reset are released.
- **AXI master traffic** goes to a 4 MiB memory at `0x20000000` and a UART at
  `0x54000000`, as in `tests/systemc/Xbar.h`.
- **HTIF `tohost` semihosting** works when the core exposes `io_debug_dbus_*`.
  `CoreMiniAxi` does. The base RVV and VME cores don't, the same as with
  Verilator.

## Known limitations

- There is no waveform tracing yet.
- AXI response timing differs slightly from the SystemC bridges in the
  Verilator testbench. Cycle counts are close but not identical.
- Assertion-preserving builds and the VME frontend's memory use have not been
  validated on this flow yet. If the frontend fails, try
  `--//rules:arcsim_assertions=false`.
