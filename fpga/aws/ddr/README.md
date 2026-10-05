# DDR bring-up and full-model inference

This change connects the Coral external AXI master and host PCIS bulk loader to
AWS F2 `SH_DDR`. It is a correctness-first implementation. The new DDR image has
**not** been synthesized, routed, loaded or physically tested. The on-chip image
previously tested by PR #1 is not DDR evidence.

## Memory and execution contract

| Interface | Range / behavior |
| --- | --- |
| Core ITCM | `0x00000000..0x00001fff`, boot code |
| Core DTCM | `0x00010000..0x00017fff`, mailbox, stack and bounded matrix tiles |
| BAR0 control | `0x30000` reset/clock, `0x30004` start PC, `0x30008` status; preserved |
| Core DDR | `[0x20000000,0xa0000000)`, 2 GiB; subtract `0x20000000` for DDR offset |
| Host PCIS | `[0,0x80000000)`, direct DDR offsets |
| DDR code | first 64 KiB of DDR; entry remains in ITCM |
| Model/data allocations | at or above `0x21000000`; first 16 MiB reserved |
| BAR0 DDR capability | `0x40000`: `0x43444452` (CDDR); `+4`: ready bit 0, present bit 1, sticky fault bit 2; `+8`: aperture bytes; `+12`: core base; `+16`: register ABI 1 |

Core computation stays on Coral, including embedding, Q/K normalization,
RoPE/YaRN, projections, grouped attention, KV updates, SwiGLU, output projection
and greedy decoding. The host packages and loads weights, tokenizes inputs,
polls completion and displays results. CPU references run separately for
validation. There is no host model-operator fallback.

See [model formats, pinned checkpoints and exact budgets](../models/README.md)
and [real-core simulation](core_sim/README.md). Qwen weights stay BF16; Bonsai
retains native PQ2 groups/scales/order, including packed embedding. Arithmetic,
activations and KV are FP32. The allocation cap is 2048 tokens, subject to
prompt plus generated-token bounds; it is not a validated physical context.

## RTL behavior

The core runs at 50 MHz and the shell/DDR AXI interface at 250 MHz. Two XPM
asynchronous FIFOs cross complete requests and responses. Shell reset controls
the bridge; core CSR reset does not discard outstanding DDR transactions.
DDR calibration gates backend acceptance. Both reset releases are synchronized,
and XPM resets are stretched in their write clock domains.

Core AXI is 128-bit with 6-bit IDs; PCIS is 512-bit with 16-bit IDs. A fair
single-outstanding backend converts each source beat into one aligned 64-byte
DDR transaction. Narrow byte strobes preserve other bytes without a
read-modify-write. The design favors inspectable correctness over bandwidth.

Requests must be naturally aligned. Supported source beat sizes are 1..16 bytes
(core) and 1..64 bytes (PCIS), FIXED bursts up to 16 beats and INCR up to 256.
Aperture overflow, address overflow, 4-KiB crossing, unsupported bursts and locks
return DECERR. Malformed write data returns an error; earlier completed beats
in a failed burst may already have modified memory.

The backend watchdog, calibration loss or invalid response ID/RLAST returns
SLVERR for the current operation and sets a sticky fail-stop fault. Asserted
AXI obligations remain asserted and late responses are drained. Another parked
frontend can remain blocked until **shell reset**; host deadlines and fault
checks are mandatory. Core CSR reset alone does not clear this fault.

## Reproducible local gates

```sh
python3 fpga/aws/build/build.py --source-check
python3 -m unittest discover -s fpga/aws/ddr/sim -p 'test_*.py' -v
python3 -m unittest discover -s fpga/aws/models -p 'test_*.py' -v
python3 -m unittest discover -s fpga/aws/models/runtime -p 'test_*.py' -v
python3 -m unittest discover -s fpga/aws/ddr/host -p 'test_*.py' -v
```

Protocol tests require Icarus Verilog and `vvp`; skipped tests are not evidence.
The full FPGA build now requires and runs those tests before synthesis. Its
Vivado/HDK/shell/ISA/clock/TCM pins and existing route/timing/DRC/CDC gates remain
in force. `cl_ddr4_32g.xci` is additionally SHA-pinned, `EN_DDR=1`, `EN_HBM=0`,
and synthesis enables `XPM_FIFO`. Vendor XPM simulation and routed CDC constraint
coverage still require the pinned licensed environment. Portable FIFO tests
verify function, not metastability or vendor implementation signoff.

Firmware builds must use LLVM 18.1.3 and
`-march=rv32imf_zicsr_zifencei_zve32f_zvl128b -mabi=ilp32f`, without compressed or
double-precision instructions. An unpinned compiler cannot establish target
qualification. Do not load a firmware ELF unless geometry, image identity,
model hashes, DDR readiness and memory readback all pass.

## Remaining acceptance gates

1. Strict target compilation and ELF/ISA inspection for the final mailbox ABI.
2. Real-core tiny autoregressive generation with per-step logits/token checks,
   EOS/length handling, prompt variation and repeatability.
3. Both original checkpoint exports and pinned tokenizer/config inputs; matching
   full-model CPU references for several successive generated tokens.
4. Newly synthesized, routed DDR image with timing, DRC, CDC and bus-skew reports
   reviewed against the intended memory configuration. The old fixed report
   expectations fail closed; do not weaken them to claim a new image passed.
5. Authorized DDR image deployment, physical bulk readback and full-model token
   generation on Coral for **both** models. Record image/firmware/model hashes,
   every generated token and reference-logit tolerance, load/prefill/decode time,
   TTFT and measured decode tokens/second. Until executed, these remain NOT_RUN.

The scripts do not provision infrastructure, modify security, run image lifecycle
operations or authorize shared FPGA use. Those operations require coordinated
ownership and the separately approved build/deployment scope.
