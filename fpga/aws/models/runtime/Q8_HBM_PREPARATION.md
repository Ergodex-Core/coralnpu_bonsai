# Q8_0 / supplied HBM ABI preparation

This is a separate CORALM01 version2 numerical profile. Version1 BF16/PQ2
retains its previous arithmetic. Q8 matrices use unchanged GGUF Q8_0 blocks:
binary16 scale followed by32 signed bytes. Embeddings dequantize into FP32;
matvec input quantizes in blocks of32. Norms and KV remain FP32.

The official source is Qwen/Qwen3-0.6B-GGUF revision
23749fefcc72300e3a2ad315e1317431b06b590a, Qwen3-0.6B-Q8_0.gguf,
SHA256 9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031.
Source tensors are streamed unchanged; no BF16 requantization occurs.

The format, scalar activation equations, and integer block dot are based on
ggml-org/llama.cpp commit8345f333951c661d166b00e6f9362e553768f292:
- ggml/src/ggml-quants.c, quantize_row_q8_0_ref
- ggml/src/ggml-cpu/quants.c, ggml_vec_dot_q8_0_q8_0_generic
- ggml/src/ggml-cpu/arch/riscv/quants.c, ggml_vec_dot_q8_0_q8_0

MIT attribution is preserved in Q8_LICENSE.txt. This profile explicitly uses
scalar-reference ties-away activation codes, binary16 RNE scales, exact signed
I32 block sums, and ordered FP32 scale/multiply/add with FMA off. The upstream
RVV activation conversion follows FRM instead, so it is deliberately not copied.
This is not a claim of bit-exact agreement with arbitrary llama.cpp CPU backends.
Nonfinite activations, scale overflow, and reciprocal overflow fail closed.

CM_Q8_RVV enables only the integer dot's explicit intrinsics:
vle8 i8m2 -> vwmul i16m4 -> vwredsum i32m1. At VLEN128, LMUL2 holds32 codes.
Each product fits I16, and the complete block fits I32 even for all -128 codes.
No pairwise I16 addition is used. Local Coral RTL test sequences include VWMUL
and VWREDSUM, but target compile, disassembly, simulation, and silicon behavior
remain UNQUALIFIED. LLVM18.1.3 and a simulator are absent from this executor.

Target owner integration requirements:

1. For HBM, pair hbm.ld with -DCM_HBM_PROFILE. For integer RVV, additionally use
   -DCM_Q8_RVV with the existing rv32imf_zicsr_zifencei_zve32f_zvl128b / ilp32f
   flags and -ffp-contract=off -fno-fast-math. No target build was performed here.
2. build.py now accepts --memory-profile hbm --q8-rvv and optionally --probe.
   It validates through fpga/aws/hbm/host/elf_image_hbm.py. The matching HBM
   run_inference.py preserves strict3e-5 output checking and halt/drain guards.
   Preserve strict LLVM18.1.3 provenance and check
   vector loads, widening multiply/reduction, no unsupported ISA, no FMA, stack,
   DTCM usage, and no unresolved freestanding helpers in final disassembly.
3. NPU external bank is [0x80000000,0x100000000). PF0 BAR4 offset is
   bank*0x80000000 + (address-0x80000000); no prefix. The pure hbm_profile.py
   helpers cover all8 bank edges and explicit host-traffic drain requirements.
   They perform no hardware operations. Bank CSR is BAR0+0x40008, host-only.
4. Model packages use --memory-profile hbm --address 0x81000000. Budget actual
   generation logits, EOS/output arrays, tokens, code, and max-context workspace
   in the final host allocator. The package's historical planned_end_address
   includes one logits row, not every emitted-row snapshot.
5. Stage and read back all code/model/request bytes before reset release. Change
   banks only reset/halted with NPU and independently tracked host traffic drained.
   Read results only after halt/drain. Host performs load/tokenize/UI, no operators.

Validation on2026-10-05: Q8 primitive tests include all adjacent finite-positive
half midpoints and FP32 neighbors,2050 exact activation blocks against the ggml
scalar equations, signed-code extremes, matrix tile tails and ordered block sums.
HBM firmware predicates use widened arithmetic at the exclusive2^32 endpoint.

Two full Q8 CPU prompts produced matching four-token sequences and passed native
repeat/EOS controls. Hello passed the unchanged strict3e-5 operator gate; France
failed. First failure: position4 layer5 DOWN input, a SiLU difference5.96046448e-8
straddles half-scale midpoint0.0022859573364257812. Stored scales become half
bits6319 versus6318. On either identical input, C and NumPy DOWN matvec results
are bit-identical across1024 outputs. Later full-model drift reaches0.58689224;
this gate failure must remain visible. No full-model blanket PASS, simulation,
or physical inference claim is justified by this patch.
