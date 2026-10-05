# Correctness-first DDR decoder

This freestanding C runtime implements every transformer operation on Coral:
embedding; RMS and per-head Q/K normalization; dense or native PQ2 projections;
split-half RoPE (including explicitly configured YaRN); causal GQA with FP32 KV;
SwiGLU; final normalization; tied or independent output head; greedy decoding.
Q projection width is `n_heads * head_dim`, independently of hidden-state width.
The host loads files/token IDs and presents results. It does not compute model
operators. `cm_generate` prefills the full prompt and reuses one KV state for
subsequent generated tokens; EOS and maximum length are firmware decisions.

Weights are F32, exact BF16-bit expansion, or native PQ2_0 (128 values per block,
little-endian F16 scale and 32 bytes of LSB-first codes; code minus one includes
code 3 = +2). Matvec performs ordered FP32 multiply then add with FMA disabled.
FP32 activations/KV and approximate FP32 elementary functions form an explicit
arithmetic contract. This is **not** historical Bonsai Q8_K arithmetic or a
bit-exact reproduction of a BF16 framework. Old checkpoint gold is not reused.
The explicit YaRN attention factor is the FINAL multiplier, applied once.

Each matvec stages 128 input floats and at most 512 weight bytes through DTCM.
Full activations and KV live in DDR. This scalar implementation is a runnable
correctness baseline; throughput, full-model accuracy and physical completion
remain unqualified until measured. Supported capacity is 1..2048 positions,
subject to the actual DDR budget. No sliding window or KV eviction is implemented.

## Build and test

```bash
python3 fpga/aws/models/runtime/test_runtime.py
python3 fpga/aws/models/runtime/build.py --output /tmp/coral-decoder-build \
  --clang clang-18 --linker ld.lld-18 \
  --objdump llvm-objdump-18 --readelf llvm-readelf-18
python3 fpga/aws/models/runtime/make_fixture.py --output /tmp/coral-tiny-bf16 \
  --elf /tmp/coral-decoder-build/decoder.elf
python3 fpga/aws/models/runtime/make_fixture.py --output /tmp/coral-tiny-pq2 \
  --encoding pq2 --elf /tmp/coral-decoder-build/decoder.elf
```

All four build tools must report **18.1.3**. `--allow-unpinned-toolchain` exists
only for exploratory builds, which are recorded as `UNQUALIFIED`. The manifest
hashes sources, compiler binaries and ELF and records memory geometry. The ELF
has ITCM boot at 0, code/rodata within DDR `0x20000000..0x2000ffff`, a 128-byte
DTCM mailbox at `0x10000`, bounded DTCM tile/data space and a 4096-byte stack.
External instruction fetch must be enabled in the generated CoreAxi RTL and the
DDR calibration gate must be satisfied before releasing reset. Success uses
`_ret=0` and `mpause`; failure uses a nonzero return plus `ebreak`.

The native test suite builds the same decoder/generation C sources. Independent
Python FP64 equations validate all exposed operator/layer/logit boundaries for
F32, BF16 and native PQ2 fixtures; exact greedy tokens; three cached token steps;
four-token generation; EOS; prompt variation; repeatability; and bounds checks.
Elementary-function tolerances: exp relative <8e-7, log absolute <7e-7, sin/cos
absolute <2.5e-4 through position 4096 (tested beyond the supported 2048 cap).
Tiny model normalized error must be <3e-5. These are synthetic host tests, not
full-checkpoint or target execution evidence. Core simulation fixtures contain
independent golden readback checks and can select early EOS with `--eos-first`.

## Request ABI v2

`mailbox.h` defines the exact little-endian 128-byte mailbox. Inputs are written
while reset is asserted. `model_addr >= 0x21000000`; all model/workspace/token/
logit/EOS/generated buffers must be disjoint and inside CPU DDR
`0x20000000..0x9fffffff`. Runtime checks ranges and rejects malformed inventory.

`max_new_tokens=0` performs prompt-only evaluation. Otherwise logits contain
`max_new_tokens` rows of `vocab` FP32 elements, one next-token prediction per
emitted token. Early EOS writes only `generated_count` rows. `logits_capacity`
is a float count, and `generated_capacity` is a token count. The last emitted
token is not processed again, because no further prediction is requested:
`completed_tokens = prompt_count + max(0, generated_count - 1)`.
`stop_reason` is 0 for prompt-only, 1 for length, 2 for EOS. Stable high/low/high
reads of the target cycle counter record prefill, decode, total and first-token
cycles; host-library tests return zero counters. Timing excludes loading and
initialization. `first_token_cycles` includes prompt processing and first
argmax. `decode_cycles` includes selection/emission overhead after prefill.

Current gates: native tiny tests PASS; pinned target build, full-model operator/
layer/logit checks, real-core execution, DDR P&R/timing and physical runs must be
reported separately. No result here asserts full Qwen or Bonsai physical success.
