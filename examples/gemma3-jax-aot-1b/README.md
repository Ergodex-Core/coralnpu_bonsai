# Offline Gemma 3 1B JAX AOT adaptation

This adapts the official [Google Coral Gemma JAX AOT example](https://github.com/google-coral/coralnpu-compiler/tree/76ed531d2389784c89961177251daf3653dbc513/examples/gemma3-jax-aot) from 270M to the original Google `gemma-3-1b-it` checkpoint. It reuses `gemma.gm.nn.Gemma3_1B` and its attention, RoPE, cache, MLP and head. It is separate from Qwen runtime work.

**Current scope: verified weight mapping, semantic tests and actual 1B StableHLO exports. No Coral compiler lowering, Verilator execution or hardware TPS is claimed by this example.** Retained numerical comparisons show block-level differences that still need review. Keep downstream target use experimental.

## Architecture and precision

- 26 layers; 1152 hidden dimensions; 6912 MLP dimensions; 4 query heads; 1 KV head; 256 head dimensions; 262144 vocabulary entries.
- 22 local attention layers, window 512 and RoPE base 10000; global layers 5, 11, 17, 23 (zero-based), RoPE base 1000000. Maximum context 32768.
- 13/13 split, explicit parameter subtrees and manifests with argument order. The head returns all logits for numerical qualification instead of only argmax.
- Original BF16 weight bits are mapped/transposed exactly. No rounding or quantization during mapping. HF RMS uses FP32 variance and `(1+weight)` math, followed by BF16 rounding. This precision adaptation is explicit; upstream JAX and stock MLX can otherwise differ in RMS arithmetic.
- The upstream real-arithmetic RoPE patch is retained. All local/global attention behavior comes from the official 1B configuration. Cache allocation is complete and left-aligned for all 26 layers; local attention masks limit reads to 512 positions. There is no new ring-cache optimization. The caller must reject positions beyond its allocated cache; `make_mask` rejects overflow.
- RedHat W8A8/compressed-tensors checkpoints are deliberately rejected. Their per-channel weights and dynamic activation quantizer require a separate, numerically validated exporter. This work does not convert or relabel W8A8 as BF16.

## Reproduce locally

Use an isolated Python 3.12+ environment. The official repo's build documentation uses Python 3.13; this exporter was tested separately on Python 3.12 with the pinned versions below.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Only on Apple Silicon with kauldron 1.4.2: omits an unsupported typing annotation.
.venv/bin/python patch_macos_typing.py --apply
export HF_HUB_OFFLINE=1
export JAX_PLATFORMS=cpu
.venv/bin/python test_adapter.py
.venv/bin/python export_gemma.py --checkpoint /path/to/gemma-3-1b-it \
  --output /path/to/exports --cache-length 128 --mode all
```

The checkpoint directory must already contain official `config.json`, `model.safetensors`, and `tokenizer.json`. The exporter does not log in, download model files, execute repository model code, or accept license terms. It pins model revision `dcc83ea841ab6100d6b47a070329e1ba4cf78752` through exact weight SHA256 `3d4ef8d71c14db7e448a09ebe891cfb6bf32c57a9b44499ae0d1c098e48516b6` and validates the complete supported architecture.

`--mode all` emits a layer 0 K-projection graph, full 26-layer logits graph, two 13-layer graphs and a head graph. Parameters remain runtime arguments; the MLIR files contain no model weights. `export-manifest.json` records argument shapes/dtypes/order, hashes and the explicit NOT_RUN target status. StableHLO is not a firmware ELF or executable VMFB.

`validate_local.py --checkpoint ... --fixtures ... --output ...` checks all 182 matrix inverse mappings, the complete official parameter tree, tokenizer IDs, actual layers 0/5 K projections for M=1/M=10, and complete block outputs. Reference fixtures need a `manifest.json` with `files[{path,bytes,sha256}]` and FP32 exact widenings of BF16 values. Every reference is size/hash/finiteness-checked before use; partial reads are rejected. No fixture/model bytes are included here. See `evidence/` for sanitized local results. A comparison report is not an end-to-end model qualification.

## Target compiler/runtime blockers

Pinned official compiler revision: `76ed531d2389784c89961177251daf3653dbc513`.
Required source submodules:

| Component | Commit |
|---|---|
| IREE | `0df50f9a8e40ab1878479124b952f45d9f8c0650` |
| LLVM | `6cce5bca3b901ec32e250fb43e88eb0fe511af93` |
| Coral hardware/runtime API | `15573b6929f587fa3ef51b4e4cc2017c850f7e3b` |
| Coral MPACT | `704b28ce126debd2d951f9c3b36704080d75dc60` |

The official release API returned no prebuilt releases during preparation. Stock LLVM and PyPI IREE do not establish availability of the required Coral plugin or patched Zvt backend. The local source archive has empty submodule directories and no `iree-compile`/Coral runtime binary. A separate isolated source build is required. The upstream documented Linux prerequisites include Bash 4+, Bazel 9.1.0, clang/lld 19, CMake 3.26+, Python 3.13 and shfmt.

In a **fresh isolated** compiler checkout at the pinned revision, initialize its pinned submodules (including IREE's dependencies), apply the repository's `scripts/patch-third_party.sh` patches, then build:

```sh
bazel build --config=dev --jobs=2 \
  @iree_core//tools:iree-compile @iree_core//tools:iree-run-module \
  @iree_core//runtime/bindings/python:runtime \
  //crt:coralnpu_tcm_ld \
  @coralnpu_hw//hw_sim:libcoralnpu_simulator_vme.so
```

The normal linker target and generated `coralnpu_tcm.ld` output were confirmed in the pinned BUILD file. This build was not started on a shared EC2 host. Do not reuse or modify the Qwen owner's checkout or start competing jobs. The runtime defaults to MPACT; choose `--simulator=verilator` or `CORALNPU_SIMULATOR=verilator` explicitly and retain an assertion-enabled simulator receipt. MPACT instruction counts are not hardware cycles.

The 270M launch script selects a high-memory linker layout (1 MiB ITCM,1 MiB DTCM at 0x00100000). That layout is incompatible with the retained owner's 8 KiB ITCM/32 KiB DTCM at 0x10000 simulator. Use a reviewed normal-memory linker layout and verify every executable segment/entry/stack before launch. Code-size fit is not established merely by exporting StableHLO.

Compiler defaults request `+m,+f,+zvl128b,+zve32f,+zfbfmin,+zvfbfmin,+zvfbfwma,+zvtbase,+zvt8e,+zvt16e,+zvti8i32mm,+zvtf16f32mm,+zvtf32f32mm`. Disassemble actual output and qualify these features against the selected RTL revision; VLEN128 and matrix support alone are insufficient. No emitted matrix instructions have yet been checked because target lowering has not run.

Official VMFB execution also needs the matching shared simulator API and host dispatch/argument marshaling. The retained standalone `core_sim SNAPSHOT OUTPUT ...` executable is not a drop-in IREE simulator shared library. Hybrid CPU+Coral compilation can leave operations on CPU; inspect affinity/dispatch placement before calling a result Coral-only inference.

## Memory and 25 MHz estimates

Original Google BF16 payload is 1,999,771,904bytes, greater than the 1 GiB hardware aperture before workspace. This implementation allocates 26 full caches: 13,631,488bytes at context 512, excluding positions/control. Partitioning compilation does not by itself solve weight residency. Streaming or additional explicit quantization must be designed and measured. Do not silently ternarize.

The separately prepared projection snapshot is a bounded diagnostic, not this official AOT runtime. Hardware seconds are target cycles/25,000,000; complete-model decode TPS requires all model operations and real transfer/residency costs. No numeric TPS is inferred from exporter wall time, JAX CPU timing or MPACT instruction counts.

## Retained first Verilator diagnostic

The separate custom-firmware M=1,N=256,K=1152 layer-0 K projection passed all output/return/guard checks. Google BF16 took 447644 harness cycles (17.90576ms at 25 MHz); RedHat W8A8 and signed-range regression each took 282549 (11.30196ms). Packing, activation quantization and host preload are excluded. Memory used a deterministic two-cycle latency model, not calibrated HBM. BF16 passed analytic error bounds but did not match sequential/pairwise FP32 references bitwise; W8 INT32 and scaling matched all 256 outputs exactly. The first diagnostic is not official compiler output or full-model inference. See `evidence/first-verilator-projection.json`.
