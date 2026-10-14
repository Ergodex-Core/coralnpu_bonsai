# Gemma 3 inference on CoralNPU with native kernels

Hand-written kernels and a host runtime that run **Gemma 3 1B** and
**FunctionGemma 270M** on Alex's AWS F2 image (`agfi-075a98d106eb1b6bf`,
see `/fpga.md`). This is the native alternative to the IREE / coralnpu-compiler
path on the `fpga-workflow` branch; both produce the same JSON records, so
`tools/compare_tokens.py` can diff them against each other and against MLX.

## Why one kernel per launch

The image has an 8 KiB ITCM and no instruction fetch from external memory, so
no monolithic inference program can run. Each operator is its own ELF
(`kernels/*.cc`, built with `coralnpu_v2_binary` at 8 KiB / 32 KiB). The host
(`host/runtime.py`) owns the transformer loop: per layer it loads the next
kernel into ITCM, writes a 72-byte argument block into DTCM (`kargs`), starts
the core and waits for the halt. All tensors stay in external memory and are
read by the kernels over the core's data bus:

| Memory | Contents |
|---|---|
| DDR `0x80000000` | Weights: tiled Q4_0 int4 (`host/weights.py`), f32 group scales, norm vectors |
| HBM `0xC0000000` | Activations (16-row buffers), KV cache, RoPE tables, logits |
| DTCM | `kargs`, small scratch (`acc`, attention scores) |

## Kernels (`kernels/`)

| Kernel | Engine | Work |
|---|---|---|
| `matvec_q4` | Zvt matrix (int8 x nibble -> int32), RVV f32 drain | `out = x @ W.T` for Q4_0 weights, M <= 16 tokens. Each `vtmms` consumes 4 k for 16 tokens x 16 channels; raw nibbles are the unsigned operand and the `-8` offset is removed with the group's activation sum. One tile drain per 32-wide group applies the Q4_0 and activation scales in f32. |
| `quant_act` | RVV | per-token, per-group int8 activations (`sx`, `xsum8`) |
| `rmsnorm` | RVV | Gemma RMSNorm `(1 + scale)` with optional residual add; also q/k norms |
| `rope` | RVV | rotate-half RoPE from host tables; writes K and V rows into the cache |
| `attention` | RVV + scalar exp | causal softmax attention over the cache, sliding window for local layers |
| `gelu_mul` | scalar | `gelu_tanh(gate) * up` |
| `embed` | scalar | gathers and dequantizes a token row from the tiled table, times sqrt(D) |
| `topk` | scalar | top-8 of the logits row |

The logits use the tied embedding table through `matvec_q4` with
N = 262144 for the last token only.

Weight precision: Gemma 3 1B uses the Q4_0 weights of
`gaunernst/gemma-3-1b-it-int4-gguf` unchanged; FunctionGemma / Gemma 3 270M
are quantized to the same Q4_0 blocks from their bf16 safetensors. Activations
are int8 per 32-wide group, residual stream, norms, attention and the KV cache
are f32. No bf16 anywhere, matching the image.

## Running

On the F2 host (Linux x86_64, AWS FPGA SDK, image loaded):

```bash
pip install numpy safetensors huggingface_hub gguf tokenizers
python3 -m models.host.weights --model gemma3-1b --out weights/gemma3-1b
python3 -m models.host.weights --model functiongemma-270m --out weights/functiongemma   # HF_TOKEN
bazel build //models/kernels:all_kernels
python3 -m models.host.run --weights weights/gemma3-1b --kernels bazel-bin/models/kernels \
    --prompt "France capital is" --max-tokens 16 --out run_native_1b.json --verbose
```

`--backend mpact` runs the same loop on the MPACT simulator (DDR only).
`--verbose` prints per-kernel launch counts, device cycles and wall time.

## Tests

- `tests/reference_test.py` (host only): tile packing round trip against the
  byte layout in `common/kernel_args.h`, the Q4_0 quantizer against
  `gguf.quants`, the int8 matvec against an f32 matmul, and a tiny
  end-to-end reference step.
- `tests/kernels_cocotb_test.py`: every kernel on the Verilator
  `VmeCoreMiniAxi` model (matrix engine, external memory at `0x80000000`)
  against `tests/reference.py`. Not yet run: it needs the Linux toolchain and
  Verilator.

## Expected throughput on the 25 MHz image

Decode is bound by instruction issue in `matvec_q4` (about 11 instructions per
`vtmms`, i.e. about 6 MACs per cycle, plus one tile-row drain per group):
roughly 5 s per token for Gemma 3 1B and under 1 s per token for 270M. At
1 GHz silicon the same kernels give about 8 and 60 tokens/s. Prefill uses all
16 tile rows and is correspondingly faster per token.
