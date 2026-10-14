# Ternary array design review

Date: 2026-09-30. Scope: architecture draft only; no array implementation.

The draft was authored by one Codex agent and reviewed by a separate Codex agent. The review checked numeric bounds, group scaling, prefill/decode utilization, bandwidth dimensions and actual CoralNPU integration references. This is automated technical review, not human sign-off or measured hardware feasibility.

The actionable finding was result-scale lifetime under output backpressure. The revised design retains exact dots, raw scales and job/epoch/group identity in completed banks until release. Firmware may drain and release these banks while BUSY, so it does not deadlock waiting for final DONE.

Review result: no remaining actionable design findings. The final integration diagram was subsequently inspected by the author and primary agent; it changes no numerical or interface proposal.

Independent review snapshot SHA256: `f57f8b1172de9ff242e78f4d6febf47265316d4800d261b0cad032ffb1c996f5`. Final draft SHA256 after the diagram addition: `8e5be7a3050d01bcf3b1d4d9f7dcfd89ba493b71fe11b70a762f75948801361d`.

Implementation remains gated on ABI/map allocation, activation quantization, ports and routing, clock/CDC decisions, timing/resource closure and source-bound simulator/FPGA evidence.
