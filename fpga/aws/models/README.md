# DDR model packages and validation

`pack_model.py` streams the original checkpoints into a relocatable `model.bin`
and a hashed `manifest.json`; it does not execute model-supplied code. Host work
is packaging, loading, tokenization and UI. The device runtime performs the full
projection, attention, normalization, RoPE, MLP, KV update and output projection.
CPU reference programs are separate validation tools, never a hardware fallback.

## Pinned identities

| Model | Accepted artifact | Revision | License |
| --- | --- | --- | --- |
| Qwen3-0.6B | `Qwen/Qwen3-0.6B/model.safetensors` | `c1899de289a04d12100db370d81485cdf75e47ca` | Apache-2.0 |
| Ternary Bonsai1.7B | `prism-ml/Ternary-Bonsai-1.7B-gguf/Ternary-Bonsai-1.7B-PQ2_0.gguf` | `983b5dec2ff16aab79990711ba0f828a499a7e6a` | Apache-2.0 |

SHA-256 pins:

- Qwen: `f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b`
- Bonsai: `de68ba48a8dacb21979915991e7741b917869d71410a370df951c0c3a237ae50`

Source: [Qwen artifact pointer](https://huggingface.co/Qwen/Qwen3-0.6B/blob/c1899de289a04d12100db370d81485cdf75e47ca/model.safetensors),
[Bonsai artifact commit](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-gguf/commit/983b5dec2ff16aab79990711ba0f828a499a7e6a).
The packer rejects hash, inventory, shape and dtype drift. A duplicate Qwen
`lm_head.weight` is accepted only when byte-identical to the tied embedding;
the package stores it once. There is no automatic substitution of FP8, group64,
unpacked, 1-bit or requantized weights. Retain upstream LICENSE/NOTICE files with
redistributed model artifacts. Checkpoint weights are not tracked here.

## Architecture and arithmetic

| Field | Qwen3-0.6B | Ternary Bonsai1.7B |
| --- | ---: | ---: |
| Layers / hidden width / MLP width | 28 / 1024 / 3072 | 28 / 2048 / 6144 |
| Query heads / KV heads / head width | 16 / 8 / 128 | 16 / 8 / 128 |
| Query projection width | 2048 | 2048 |
| Vocabulary | 151936 | 151669 |
| Tied embedding/output, Q/K per-head RMS norm | yes | yes |
| RMS epsilon / RoPE theta | 1e-6 / 1000000 | 1e-6 / 1000000 |
| RoPE | unscaled | YaRN factor 4, original context 8192 |
| Checkpoint context | 40960 | 32768 |
| Supported allocation cap in this implementation | **2048** | **2048** |

Sources: [Qwen config](https://huggingface.co/Qwen/Qwen3-0.6B/blob/c1899de289a04d12100db370d81485cdf75e47ca/config.json)
and the hashed Bonsai GGUF metadata; the official
[Bonsai MLX config](https://huggingface.co/prism-ml/Ternary-Bonsai-1.7B-mlx-2bit/blob/main/config.json)
corroborates architecture, not storage format. [Prism documentation](https://docs.prismml.com/models/bonsai-1-7b)
distinguishes the ternary model from 1-bit Bonsai.

Qwen's query width is not its hidden width. Both models use full attention,
split-half rotary pairs and a SiLU-gated MLP. Qwen weights remain BF16 on DDR;
`float_bits = bf16_bits << 16` widens them exactly for FP32 arithmetic. This
requires neither BF16 instructions nor permanent FP32 expansion.

Bonsai matrices, including embedding, remain native GGUF type 142 (`PQ2_0`).
A row contains 34-byte groups of 128 columns: a little-endian FP16 scale and
32 packed bytes. Column j uses bits `2*(j%4)` of byte `j/4`; its value is
`float(scale)*(code-1)`. **Code 3 means +2 and is preserved**. All four native
codes, group scales and column order are retained without quantization.
The 113 normalization tensors remain FP32. The output head aliases embedding.

Activations, logits, accumulators and KV are FP32; the target baseline uses
ordered multiply then add with contraction/fast-math disabled. Historical
PQ2_0 x Q8_K activation-quantized first-layer/golden results are a different
arithmetic contract and are not new evidence for this runtime.

The Bonsai GGUF omits YaRN beta and attention-factor parameters, so the CLI
requires them explicitly. `--yarn-attention-factor` is the final cosine/sine
multiplier, applied once. The example chooses beta 32/1 and `1+0.1*ln(4)`;
this is an explicit bring-up profile, not a claim of matching an unspecified
library default. Pin the same profile for every reference comparison.

## Export and tests

Obtain original files from the pinned publisher revision. No download occurs
inside the packer. Qwen needs the original `model.safetensors` and `config.json`.
Bonsai needs the original PQ2_0 GGUF.

```sh
python3 fpga/aws/models/pack_model.py qwen3-0.6b /path/to/qwen-snapshot /tmp/qwen-package --max-seq 512
python3 fpga/aws/models/pack_model.py bonsai-1.7b /path/to/Ternary-Bonsai-1.7B-PQ2_0.gguf /tmp/bonsai-package --max-seq 512 --yarn-beta-fast 32 --yarn-beta-slow 1 --yarn-attention-factor 1.138629436111989
python3 -m unittest discover -s fpga/aws/models -p 'test_*.py' -v
```

The output package must not already exist. `--max-seq` defaults to 2048 and
rejects values outside 1..2048. Allocation support is not numerical/physical
qualification at that context. Start with one token and small capacity. The
host runner must verify hashes, ELF geometry, binary metadata, DDR bounds and
full readback before releasing the core.

## Exact memory budget

The mapped aperture is `[0x20000000,0xa0000000)`, or 2147483648 bytes. Model
base `0x21000000` reserves the first 16777216 bytes for firmware/runtime.
The exporter enforces that minimum, 64-byte alignment and the complete upper
bound with arbitrary-precision host arithmetic. Firmware section bounds and
non-overlap also belong to the loader's preflight.

| Storage, bytes | Qwen3-0.6B | Ternary Bonsai1.7B |
| --- | ---: | ---: |
| Accepted tensors | 310 | 310 |
| Unique tensor payload | 1192099840 | 457345184 |
| PQ2 matrix bytes, included above | — | 456849568 |
| FP16 group scales, included above | — | 13436752 x 2 |
| FP32 norms, included above | — | 495616 |
| Header/directory/alignment | 10048 | 10080 |
| Package | 1192109888 | 457355264 |
| Logits | 607744 | 606676 |
| KV per token | 229376 | 229376 |
| KV at 512 tokens | 117440512 | 117440512 |
| Entire workspace at 512 | 117504256 | 117541120 |
| KV at 2048 | 469762048 | 469762048 |
| Entire workspace at 2048 | 469831936 | 469868800 |
| End address at 2048, tokens/logits/alignment included | `0x84189e40` | `0x584db300` |
| Unused aperture at 2048 | 468148672 | 1202867456 |

Both budgets are checkpoint-verified by local full exports. Qwen has
596049920 unique parameters; a duplicate tied head was verified byte-identical
and omitted from the packed inventory. Bonsai has:
197 PQ2 matrices and 113 FP32 norms, including packed embedding and no separate
head. Its original GGUF is 463290464 bytes; tokenizer metadata stays on host.

Workspace bytes are exactly `4*(3*dim + 2*n_heads*head_dim + 2*n_kv_heads*head_dim + 2*hidden_dim + seq_capacity + head_dim/2 + 2*n_layers*seq_capacity*n_kv_heads*head_dim)`.
The final term is KV. After the
package, separately allocate the token buffer (`4*capacity`), logits and
workspace, aligning each region to 64 bytes.

## ABI and qualification

`model_format.h` defines the little-endian binary: a 128-byte `CORALM01` header,
32-byte tensor directory entries, 64-byte-aligned payloads and relative u32
offsets. Global tensors use layer `0xffffffff`; norm vectors use rows 1 and
columns N. `manifest.json` schema `coral-model-package-v1` carries source hashes,
normalized config, segment address/size/hash, per-tensor hashes and memory
requirements. It contains no source machine paths. Both runtime and host must
validate the binary; manifest assertions do not authorize skipping checks.

Packaging, independent operator fixtures, native CPU execution, full reference
logits/tokens, cycle-accurate Coral simulation, routed timing, DDR hardware
readback and physical generation are separate gates. Preserve NOT_RUN for
missing evidence. Synthetic tokens do not establish real-model inference.

## CPU reference continuation

`cpu_reference.py` is an independent NumPy FP32 implementation of the full
network. It decodes bounded row tiles, rounds each multiply and ordered sum to
FP32, saves per-operator hashes and generates greedy tokens with persistent KV.
It uses NumPy transcendental functions independently of target math. It is a
validation executable, never linked to the device runner. Its synthetic tests
compare all three formats with separate scalar FP64 equations, verify repeated
runs and exercise both EOS and length stops. Real-model generation remains
NOT_RUN at this source handoff.

Use an isolated environment with `numpy==2.4.2` and `tokenizers==0.22.2`.
The tokenizer helper pins the Qwen tokenizer JSON; when `--bonsai-gguf` is
specified it verifies the original GGUF hash and checks every vocabulary entry
and every merge in order against that tokenizer. This comparison passed locally.
The supported prompt mode here is raw completion, without an invented chat
template. `Hello` maps to `[9707]`; `The capital of France is` maps to
`[785,6722,315,9625,374]`. EOS IDs must match the pinned generation profile.

```sh
python fpga/aws/models/tokenize_prompt.py /path/to/tokenizer.json --text Hello
python fpga/aws/models/tokenize_prompt.py /path/to/tokenizer.json --bonsai-gguf /path/to/Ternary-Bonsai-1.7B-PQ2_0.gguf --text Hello
python fpga/aws/models/cpu_reference.py /tmp/qwen-package/manifest.json --tokens 9707 --max-new-tokens 4 --eos 151645,151643 --output /tmp/qwen-hello-reference.json
python fpga/aws/models/cpu_reference.py /tmp/bonsai-package/manifest.json --tokens 9707 --max-new-tokens 4 --eos 151645,151643 --output /tmp/bonsai-hello-reference.json
python -m unittest discover -s fpga/aws/models -p 'test_*.py' -v
```

Repeat on the second prompt, repeat each case, decode IDs with the same helper's
`--decode-ids`, and compare target per-layer values/logits/generated IDs against
fresh reference outputs. A CPU reference token sequence alone does not qualify
Coral. No real-model CPU run was started during the source handoff.
