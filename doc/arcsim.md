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
- AXI response latency differs from the SystemC bridges in the Verilator
  testbench. Cycle counts are close but not identical.
- Assertion-preserving builds and the VME frontend's memory use have not been
  validated on this flow yet. If the frontend fails, try
  `--//rules:arcsim_assertions=false`.
