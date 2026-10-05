# Source provenance

The FPGA wrapper, OCL bridge, testbench, synthesis script, implementation script,
and constraints were imported from the retained Coral NPU F2 build sources.
The baseline uses Coral commit `382b5c12030ad8eb74ab8deafb2529301faeab16`.

The original generated `RvvCoreMiniAxi.sv` has SHA-256
`2a19a5c7ea2ece404b0fa77e883810de061319920fb7c9f71927017803407720`.
The previously synthesized copy changed the word `sync` to
`coral_private_sync` in three comments. All include files match the corresponding
Bazel ZIP members byte for byte. No additional SRAM transformation was present:
`SYNTHESIS` selects the upstream generic `Sram.v` implementation. New builds keep
the original emitted file, which is functionally identical to that baseline.

The top wrapper and build Tcl/constraints derived from AWS HDK retain their
Amazon Software License notices; the license is included at
`../cl_coralnpu/LICENSE.Amazon.txt`. Upstream generated Coral RTL is generated at
build time and retains its upstream notices. The added Python tools use the
repository's license.

This source reconstruction and report validation do not establish a reproduced
implementation or physical FPGA execution. The retained candidate has unresolved
critical warnings, DRC warnings, and shell timing exceptions. The build gates
preserve their evidence and reject deployment packaging until those issues are
resolved or a separately reviewed exact baseline is implemented. No generic rule
waivers or previously approved warning dispositions are included.
