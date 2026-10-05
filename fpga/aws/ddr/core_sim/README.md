# Pinned-core DDR simulation

`run.py` executes the generated Coral core behind the production `coral_host`
and `coral_ddr_subsystem` RTL. The core and shell clocks are 50 MHz and 250 MHz.
Boot instructions are loaded through BAR0 into ITCM; firmware DDR segments and
model fixtures are loaded through the 512-bit host interface. Readback uses the
same interfaces. The testbench does not calculate model operators or inject
inference outputs.

The SH_DDR controller is represented by a sparse AXI memory model with separate
AW/W capture, byte strobes, and deterministic backpressure. The FIFO model is
the portable functional Gray-pointer model in `../sim`. This test **does not
qualify vendor XPM CDC behavior, routed timing, SH_DDR calibration, FPGA
execution, or full checkpoint inference**. These remain separate gates.

## Run the compiler-independent smoke test

Supply the emitted RTL, ZIP includes and parameter header matching the pins in
`../../build/pins.json`, plus Verilator 5.050. The runner validates the emitted
core hash, complete extracted include inventory, parameters and tool version.

```sh
python3 fpga/aws/ddr/core_sim/run.py \
  --emitted-dir /path/to/pinned/emitted \
  --verilator /path/to/verilator-5.050/bin/verilator \
  --output /tmp/coral-core-ddr-smoke
```

The hand-encoded RV32I fixture boots in ITCM, jumps to instructions in DDR,
loads 40 from DDR, adds 2 on Coral, stores 42 in DDR and DTCM, applies a byte
store in DDR, fences and halts. Exact readback checks validate 42 and the
byte-preserved word `0xaabb2add`. Host loading, DDR instruction fetch, scalar
loads/stores, width conversion, both functional clock crossings and halt/status
handling participate in this test.

The output contains the build and simulation logs, normalized host-load files,
binary digest and `receipt.json`. `PASS` requires all expected readbacks. Failures
remain `FAIL` with diagnostics; absent physical/vendor evidence stays `NOTRUN`.
Compilation uses four jobs and no shared ccache.

## Run a decoder fixture

```sh
python3 fpga/aws/ddr/core_sim/run.py \
  --emitted-dir /path/to/pinned/emitted \
  --verilator /path/to/verilator-5.050/bin/verilator \
  --firmware /path/to/decoder.elf \
  --fixture /path/to/fixture.json \
  --reuse-build /tmp/coral-core-ddr-smoke \
  --output /tmp/coral-core-ddr-decoder
```

The ELF must be little-endian RISC-V ELF32 with entry zero. PT_LOAD segments are
loaded and zero-filled; all bytes must be inside ITCM, DTCM, or the DDR aperture.
JSON segments follow the ELF and may initialize its mailbox. File paths are
relative to the JSON. Example schema:

```json
{
  "metadata": {"workload": "synthetic tiny decoder; not a full checkpoint"},
  "segments": [{"address": "0x21000000", "file": "model.bin"}],
  "checks": [
    {"address": "0x10008", "value": 2},
    {"address": "0x22000000", "float": 0.125, "atol": 0.00001, "rtol": 0.00001}
  ]
}
```

Every expected address must appear in readback. Float checks reject nonfinite
values. The runtime must complete with `mpause`; core fault, backend fault,
missing halt or comparison failure fails the run. The reused binary is accepted
only when its digest, tool version and all RTL/model source digests match.
Use tiny deterministic fixtures here; full model loading and execution belong
to the physical runner and require separately qualified hardware evidence.
