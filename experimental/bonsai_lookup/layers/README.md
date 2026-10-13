# Actual Bonsai projection fixtures: integer groups and scaled references

These fixtures use the complete block0 Q/K/V/output and gate/up/down matrices
from the pinned native Ternary-Bonsai1.7B PQ2 checkpoint. Decode is M=1; the
prefill workload is M=32. They contain complete host numerical references,
not measured layer performance. Activations and their scales are deterministic
test inputs, not captured inference activations or a validated model quantizer.

| Projection | N outputs | K inputs | Decode useful MACs | Prefill32 useful MACs | Cold tile jobs decode / prefill32 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Q | 2,048 | 2,048 | 4,194,304 | 134,217,728 | 1,024 /4,096 |
| K | 1,024 | 2,048 | 2,097,152 | 67,108,864 | 512 /2,048 |
| V | 1,024 | 2,048 | 2,097,152 | 67,108,864 | 512 /2,048 |
| Output | 2,048 | 2,048 | 4,194,304 | 134,217,728 | 1,024 /4,096 |
| Gate | 6,144 | 2,048 | 12,582,912 | 402,653,184 | 3,072 /12,288 |
| Up | 6,144 | 2,048 | 12,582,912 | 402,653,184 | 3,072 /12,288 |
| Down | 2,048 | 6,144 | 12,582,912 | 402,653,184 | 3,072 /12,288 |

All sizes are verified from the actual GGUF directory and full-file SHA.
`results/layer-fixtures.json` binds each weight payload to its tensor name,
GGUF offset and SHA, and each input/output file to its size and SHA. Model
weights and binary references are delivered in the Library companion bundle,
not committed to the repository.

## Independent numerical contracts

For each native128-element group, the integer reference is the exact sum of
signed-int8 activation times `(native_PQ2_code - 1)`. The C oracle deliberately
uses direct scalar multiplication, not sign-selection or lookup tables. It
supports all four generic codes; the pinned weights are ternary. Each dot is
int32. Every native FP16 scale bit is preserved.

The full scaled-projection reference is explicitly:

```text
y = +0.0f
for g in increasing K-group order:
    dot = exact_integer_dot(a_q[g], native_weights[g])
    p = RN32(float32(dot) * float32(native_FP16_weight_scale[g]))
    q = RN32(p * activation_scale_f32[token,g])
    y = RN32(y + q)
```

There is no fused FMA, bias, RMSNorm, QK normalization, RoPE, softmax, activation
function, gate/up product or residual add here. Thus this is an isolated scaled
linear projection, not an entire attention/MLP layer or model inference path.
Synthetic activation scales are explicit positive powers of two; no new
quantizer is applied. The current FP32 Bonsai runtime remains a separate quality
reference. This contract must be accepted before treating a future A8 model
as numerically comparable to that runtime.

`oracle.c` produces every integer group dot and scaled output. Compiler options
prohibit contraction/fast math. Python independently re-decodes native bytes
and checks selected first/interior/last rows and first/last tokens, rounding via
FP32 packing after each operation. Decode and first-prefill-token inputs and
outputs also agree byte-for-byte. Host runtime is not reported as accelerator
performance. The terminal tile from every projection/mode passed the unchanged
six-engine lookup RTL interface:14 local Icarus checks, **not full layer runs**.

## Reproduce and extract existing-RTL tiles

```bash
python3 experimental/bonsai_lookup/layers/prepare_layers.py \
  --weights /path/to/Ternary-Bonsai-1.7B-PQ2_0.gguf \
  --out /tmp/bonsai-layers --summary /tmp/layer-fixtures.json
python3 experimental/bonsai_lookup/layers/extract_tile.py \
  --fixtures /tmp/bonsai-layers --role down --mode prefill32 \
  --token-base 24 --row-base 2016 --group 47 --out /tmp/down-last-tile
```

Binary orders are weights `[N,G,34]`, signed activations `[M,K]`, FP32 activation
scales `[M,G]`, int32 group dots `[M,N,G]`, and FP32 scaled outputs `[M,N]`, all
little-endian. `extract_tile.py` writes the same `memory.hex`/`expected.hex`
protocol as PR6's existing harness, plus complete expected values and a sidecar
activation-scale vector. The current RTL does **not** consume that sidecar.

For a full integer-group sweep, keep one compiled model per engine/datapath
variant and execute all `(row_tile32, K_group128, token_tile8)` jobs against the
same fixture. Preserve and check every output identity/value; report actual
observed total cycles and accepted bytes, including per-job/setup/drain gaps.
Do not multiply one tile's latency by the job count and call that measured layer
performance. A sampled subset must be labeled sampled; the full shapes comprise
61,440 cold tile jobs across the14 workloads before engine/datapath variants.

The existing cold schedule rereads weights four times at M32 and reloads
activations across output-row tiles. Exact scheduled payload bytes are retained
in the summary. Those figures exclude output writes, descriptors, activation
scale sidecars and group-scale/reduction work. They are workload accounting,
not measured useful bandwidth or a production-optimal schedule.

Arcsim owner progression: one terminal tile per projection, then one complete
decode projection, then remaining full projections within the agreed resource
budget. Use identical memory service and fixtures for2/4/6/8 engines. Record
integer-group sweep latency separately from scaled projection latency. The latter
is currently **NOT_IMPLEMENTED_IN_RTL**; a host postprocessing reference cannot
supply its simulated hardware cycles. No host/slot operation is dispatched here.

## FPGA progression and integration prerequisites

See [production integration plan](production-integration.md). Existing Alex
hardware can measure the software baseline and packing kernels; it cannot
validate the added engines. Qualified new images are required for physical
integer kernels and full scaled projections. Every FPGA row stays NOT_RUN until
source/model/layout/oracle/firmware/image/slot/clock/output identities and raw
receipts exist. Full-model prefill TPS, TTFT, decode TPS and latency require a
complete matched runtime; individual projection fixtures cannot establish them.
