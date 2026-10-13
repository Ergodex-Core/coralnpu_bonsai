# TeLLMe attribution and implementation boundary

Architecture reference: [UCI-CORSA/TeLLMe_FPGA_2026](https://github.com/UCI-CORSA/TeLLMe_FPGA_2026/tree/139ad882aba8485048e08993022872128bc00f51),
pinned commit `139ad882aba8485048e08993022872128bc00f51`.
The upstream BSD-3-Clause license is retained in `UPSTREAM_LICENSE.txt`.
Upstream `src/linear.hpp` additionally attributes copyright 2024-2026 to
Zhiheng Chen. No upstream HLS source, board bitstream, runtime or model payload
is copied into this implementation.

The independently written SystemVerilog uses TeLLMe's idea of precomputing
activation sums for ternary triples and sharing the tables across output rows,
as seen in `generate_table_simd` and `compute_linear_on_stream_block` in
`src/linear.hpp`. `src/config/config.hpp` sets triples of three and five-bit
indices. Its memory loading and row reuse informed the explicit bounded tile
feeder. This work uses Bonsai's native group/scales and a documented base-3
index, not TeLLMe's upstream index ordering or BitNet quantization scheme.

Paper: Qiao, Chen, Zhang, Wang and Huang, *TeLLMe: An Efficient End-to-End
Ternary LLM Prefill and Decode Accelerator with Table-Lookup Matmul on Edge
FPGAs*, FPGA 2026, [DOI](https://doi.org/10.1145/3748173.3779191).
The [public preprint](https://arxiv.org/html/2510.15926v2) explains shared
precomputation, weight-memory organization, fused elementwise operations and
separate prefill/decode attention. Only the group-dot lookup and weight reuse
techniques are implemented here. Fused FP operations and attention are not.
No published TeLLMe FPGA throughput, resources, clocks or power are reused as
measurements for this design; no endorsement is implied.
