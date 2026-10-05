# DDR protocol simulations

Run from the repository root with Icarus Verilog and Python 3 installed:

```sh
python3 fpga/aws/ddr/sim/test_rtl.py -v
```

The tests compile the production RTL and use an independent byte scoreboard.
They exercise both the core's AXI128 aperture and the host's AXI512 aperture:

| Test | Coverage |
| --- | --- |
| Frontends, 128 and 512 bits | Every natural narrow lane, partial strobes, W before AW, fixed and incrementing bursts, 256-beat bursts, 64-byte boundaries, ID preservation, request/response stalls, address and 4-KiB bounds, unsupported geometry, malformed WLAST, controller errors, reset |
| Backend | Round-robin arbitration, calibration gating/loss, independent AW/W order, AXI response propagation, invalid line addresses, corrupt IDs/RLAST, watchdog fail-stop, stable stalled VALID after failure, late-response draining, reset |
| CDC | Independent clocks, both depth-16 queues full, backpressure, pointer wrap, payload ordering, reset flushing |

`xpm_fifo_async_model.sv` is a portable **functional model** for local testing.
It does not qualify AMD XPM implementation behavior, metastability, or routed
CDC timing. Production synthesis uses the vendor XPM FIFO.

The existing `fpga/aws/build/sim/tb_host.sv` regression also tests the DDR status
registers and retains its TCM, scalar, vector, and restart checks. It requires
the emitted core RTL and includes pinned by `fpga/aws/build/pins.json`; the
normal build preparation simulation runs this bench with Verilator 5.050.

The checked-in `evidence/receipt.json` records the tested source hashes and
links sanitized logs. Both protocol tests and the updated host regression
passed. These results provide functional simulation evidence only. Vendor
controller/XPM simulation, synthesis, implementation, timing/DRC, physical DDR,
and full-model inference remain separate qualification gates.
