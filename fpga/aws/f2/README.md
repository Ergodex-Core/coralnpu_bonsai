# CoralNPU on AWS EC2 F2 (Alex's image)

Host software for `agfi-075a98d106eb1b6bf` (`alex-coralnpu-20261011-ed56260c`,
small shell `0x10212415`). See `/fpga.md` for the image inventory.

| Core address | Size | Host path |
|---|---|---|
| `0x00000000` ITCM | 8 KiB | AppPF BAR0 (OCL), 32-bit words |
| `0x00010000` DTCM | 32 KiB | BAR0 |
| `0x00030000` CSR | | BAR0: `+0` reset (bit0 reset, bit1 clock gate), `+4` start PC, `+8` status (bit0 halted, bit1 fault) |
| `0x80000000` DDR | 1 GiB | AppPF BAR4 (PCIS) offset `0x0` |
| `0xC0000000` HBM | 1 GiB | BAR4 offset `0x10_0000_0000` |

The core has no bf16 anywhere, a Zvt matrix unit with int8→int32 and
f32→f32 modes only, and no instruction fetch outside ITCM. The virtual LEDs
report `ddr_ready` (bit 1) and `hbm_ready` (bit 0).

## Files

- `f2_device.py`: `F2Device` (process-isolated AWS SDK access with timeouts,
  address routing BAR0/BAR4, reset/start/halt helpers) and `ElfImage`.
- `run_elf.py`: load and run a bare-metal ELF, report `_ret`.
- The IREE / coralnpu-compiler backend for whole models lives in
  `third_party/coralnpu-compiler/runtime/sim/f2/` (`--simulator=f2`).

## Prerequisites on the F2 instance

```bash
git clone https://github.com/aws/aws-fpga && cd aws-fpga && source sdk_setup.sh
sudo fpga-load-local-image -S 0 -I agfi-075a98d106eb1b6bf
sudo fpga-describe-local-image -S 0 -H
```

`run_elf.py` and the compiler backend need root or a udev rule for the FPGA
PCI device, and `/usr/local/lib/libfpga_mgmt.so` from `sdk_setup.sh`.
