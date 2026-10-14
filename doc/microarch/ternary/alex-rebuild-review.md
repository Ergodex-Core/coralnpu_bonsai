# Alex rebuild recipe: read-only review

Reviewed on 2026-10-13 against the previously delivered software signoff and
current tested image. This is source/provenance inspection, not a rebuild.
No supplied Python/Tcl script was executed. No synthesis, AFI, FPGA slot,
security configuration or shared-host operation was performed.

## Identity and confidence

The owner-supplied rebuild prefix contains 1,533 objects (32,052,748 bytes).
303 selected source/configuration/review files were downloaded and hashed in
the isolated analysis workspace. All 247 frozen RTL files matched their supplied
manifest. The aggregate compilation-unit identity matches the tested signoff.
The wrapper, build configuration, build settings and HDK input manifest are
byte-identical to the earlier signoff. Exact hashes and inspection scope are in
[the retained summary](../../../analysis/ternary_simd/results/alex-rebuild-inspection.json).
Private payloads, vendor IP, host paths and credentials are not in this PR.

The supplied package pins:

| Input | Pinned value |
| --- | --- |
| Accelerator source | `382b5c12030ad8eb74ab8deafb2529301faeab16` |
| Vivado | 2025.2, Linux x86-64, licensed `xcvu47p` support |
| AWS HDK | `20fe90180574464fd509c994680ec67191e9759f` (2.3.0) |
| AWS IP | `6d32be972e6da854e61a8d3d6ec0466ab491c1b3` |
| Shell | `10212415` |
| Shell checkpoint SHA256 | `04a5331321e86cffc7ef740fb6132b29f179e68e2d58ccae5031c989403ecca6` |
| Previously tested accelerator DCP SHA256 | `2c8f2dbfe98ddf68535eab374d1a1f21095dc3adffdbf86aec444b1961f2d6e5` |
| Clocks | Core 25 MHz; HBM AXI 300 MHz; shell 250 MHz |

These identities support starting a later coordinated build from the frozen
input. They do not prove a clean rebuild will be bit-identical or meet timing.
The tested report's overall WNS is only +0.032 ns; adding engines still requires
placement, routing and interface timing evidence.

## Process learned from the scripts

The launcher checks Vivado and pinned HDK/IP inputs, requires a fresh output
directory, copies dependency IP to scratch, and records invocations and logs.
Its HDK verification covers 120 input files. The portable launcher supplies
25 MHz explicitly. A lower-level Tcl default is 50 MHz when arguments are
omitted: a future integration must retain the launcher's explicit 25 MHz
rather than invoke that Tcl with defaults.

The frozen run-054 flow retains the tested configuration: matrix dispatch two,
registered matrix reservation-station output, BF16/INT16 disabled, FP32 retained,
four Vivado threads and two synthesis jobs. The later acceptance scripts inspect
setup/hold/pulse checks, endpoint coverage, routes, DRC, critical warnings,
black boxes, bus skew and CDC. Passing an exit code alone is insufficient;
methodology/CDC review and physical functional evidence remain necessary.

The optional generator route pins source and Bazel 9.1.0. Its complete source
archive and generation were not verified or executed here. Its wrapper-template
HDK provenance differs from the actual pinned build HDK, so do not substitute
that template's revision for the tested integration dependency. Likewise,
an INT8-only configuration cannot silently remove FP support used by software.

## Remaining package and build gates

The supplied prefix is the rebuild subtree. Its README references package-root
`verify.py`/checksums, adjacent acceptance documentation, audit evidence and
provenance outside that subtree. Those files are not present under the supplied
prefix, so whole-package verification is incomplete. Existing signoff evidence
supports the comparisons above but does not replace the missing root manifest.
The future build owner must obtain the complete package and verify these inputs
on the licensed build machine before changing RTL.

Required external dependencies include the exact shell/HDK/IP and licensed
Vivado installation. No new FPGA build is authorized by this analysis PR.
After a sizing/numerics decision, integrate via a separate reviewed change,
retain the existing memory/driver ABI until an explicitly versioned extension,
run the full acceptance flow, then coordinate the actual-weight simulator and
FPGA gate with the image owner. No recipe step closes ERG-103 by itself.

## Follow-up sibling inventory

A recursive read-only listing of the authorized package prefix found 1,632
objects. Neither the root nor sibling prefixes contains the exact referenced
`verify.py`, root `SHA256SUMS`, `docs/acceptance.md`,
`evidence/audit/output/acceptance.txt`, or `provenance/README.md`.
Read-only tar-header inspection found only the routed DCP and AFI manifest
inside the existing release archive; no package verifier was hidden there.

The accessible `software_and_signoff/` sibling has its own `SHA256SUMS`,
`timing_cdc_review.md`, `reports/final_audit/acceptance.txt` and `provenance.md`.
These were fetched and hashed; its README explicitly scopes the checksums to
that supplemental folder. They support the prior signoff but do not cover the
new rebuild subtree. The actionable owner request is the complete portable
package root and its verifier/checksum manifest, plus the referenced adjacent
review/provenance files or an explicit mapping to the supplied equivalents.
