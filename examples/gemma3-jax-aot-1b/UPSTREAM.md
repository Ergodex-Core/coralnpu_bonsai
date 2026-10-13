This example derives from Google Coral's Apache-2.0 source:
https://github.com/google-coral/coralnpu-compiler/tree/76ed531d2389784c89961177251daf3653dbc513/examples/gemma3-jax-aot

The original `gemma3_util.py` supplies real-arithmetic RoPE and the wrapper/export structure. The adaptation uses the official Gemma3_1B architecture, replaces hard-coded18-layer/9-layer split assumptions with26/13, loads verified local HF BF16 weights, exposes complete logits, preserves HF RMS precision, validates cache limits and writes typed argument manifests. Interactive chat/default remote checkpoint loading and270M runtime helpers are intentionally omitted.

No model weights, private paths, credentials, model license acceptance, runtime binaries, generated ELF or new hardware-ready claim are included in this source contribution. The pinned model itself remains under its publisher's Gemma terms; Apache-2.0 applies to this example's code.
