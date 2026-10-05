# AWS F2 CoralNPU build and bring-up

This flow targets the on-chip-memory `RvvCoreMiniAxi` design: a 50 MHz NPU,
250 MHz shell interface, 8 KiB ITCM, and 32 KiB DTCM. BAR0/OCL is the host
interface. DDR, HBM, and host DMA are not connected. It is a bring-up design,
not a full-model deployment. External-memory integration and full-model
execution remain subsequent project milestones.

## Current qualification status

The imported build flow has a pinned reference revision and hardware settings.
Source checks and host-runner regression tests are separate from synthesis,
routing, and physical FPGA tests. A source-check pass does not prove any of
those later stages.

Source and host-test checks run on `blacksmith-2vcpu-ubuntu-2404`, with a
ten-minute timeout and no licensed tools or host credentials.

The PR workflow defaults to source-only mode and explicitly fails build readiness
while the licensed backend is disabled. The trusted existing-host controller and
its fixed host runtime are implemented under [ci/](ci/), with local regression
coverage. Host installation, containment and licensing still require live
qualification. No fresh routed checkpoint has been produced by this service.
The workflows do not upload, create or load FPGA images.

The reference routed candidate had positive setup/hold slack, but retained
DRC warnings, critical warnings, shell timing exceptions, and clock-review
debt. These are not silently waived for new builds. Packaging requires the
validation gate to pass; inspect the logs when it fails. AWS image validation
and subsequent physical execution remain separate qualification stages.

## Reproduce a build

Use Linux x86-64 with the versions in `build/pins.json`, including Vivado
2025.2, HDK 2.3.0, the pinned HDK and IP Git revisions, Bazel 9.1.0, the F2
small shell, and the specified target part. Supply a private working copy of
the HDK to avoid changing another developer's build. The build entrypoint
verifies actual inputs rather than trusting directory names.

Run cheap source checks before using licensed tools:

```bash
python3 fpga/aws/build/build.py --source-check
python3 -m unittest discover -s fpga/aws/tests -p 'test_*.py' -v
python3 utils/check_macro_signatures.py
```

With an already installed, licensed toolchain, source its `settings64.sh`, set
`AWS_FPGA_REPO_DIR` to the verified HDK working copy, and choose a fresh output
directory:

```bash
python3 fpga/aws/build/build.py \
  --hdk-root "$AWS_FPGA_REPO_DIR" --output "$FPGA_OUTPUT-preflight" \
  --tag "$FPGA_TAG" --preflight
python3 fpga/aws/build/build.py \
  --hdk-root "$AWS_FPGA_REPO_DIR" --output "$FPGA_OUTPUT" \
  --tag "$FPGA_TAG"
```

The entrypoint generates RTL, prepares the CL sources, synthesizes, implements,
reopens the checkpoint for validation, and only then packages the developer
checkpoint. It rejects violated timing checkpoints. Logs, validation reports,
source/tool provenance, and checkpoint hashes accompany the result. The
`.Developer_CL.tar` is an input to AWS image creation; it is not a loaded FPGA
image or evidence of a hardware test pass.

The checked-out source commit is the input under test. The reference Coral
revision is retained for comparison, not used to replace every PR's RTL.
The direct runner builds GitHub's proposed merge commit. The trusted SSM
controller builds the exact current PR head after checking its source job and
approval. Provenance distinguishes the built revision from the PR head and
source-check revision.

## CI backends and activation

The source job runs on Blacksmith for every PR and validates the build, physical
test runner, controller, host runtime and immutable image helpers. It receives
no AWS credentials or OIDC permission. `FPGA_CI_BACKEND` selects the build route:

| Backend | Behavior and required variables |
| --- | --- |
| `source-only` (default) | Runs source checks, then reports disabled readiness and no checkpoint. |
| `direct` | Requires `FPGA_BUILD_ENABLED=true`, `FPGA_BLACKSMITH_RUNNER`, `FPGA_HDK_ROOT` and `FPGA_VIVADO_SETTINGS`; blocks forks. |
| `ssm` | Requires `FPGA_SSM_ENABLED=true` after live qualification, plus the trusted-main controller and its nonsecret `FPGA_CI_CONFIG_JSON`. |

A standard Ubuntu runner label does not provide Vivado. Direct mode requires an
approved ephemeral installation with all pins, sufficient CPU/RAM/disk and valid
licenses for that location. Blacksmith documents a 32-vCPU / 128-GB / 1.5-TB
runner; actual free resources and licensed features must pass preflight. See
the [runner specifications][runners] and [AWS licensing guide][license]. Do not
copy developer-AMI license material to another execution location.

The SSM route keeps the controller on trusted `main`. It resolves the source
workflow and named successful source job through GitHub, checks the current PR
head and permissions, then submits only the fixed version/hash of one custom
SSM document. Same-repository writers are eligible; forks and non-writers need
a current maintainer approval of the exact head SHA. Effective change requests
veto admission. These facts are checked again immediately before submission.
PR source never executes in the credentialed controller.

The root launcher validates the canonical request, holds the shared host lock,
and requires an explicit scheduling permit. Its durable pilot ledger admits at
most three builds, serially, each with a six-hour deadline. Resource limits cover
all helpers and private engines: 16 CPUs, 64 GiB, 4096 tasks, and a hard 250-GiB /
2,000,000-inode storage pool. Local tests exercise these policies; actual Linux
enforcement must be measured before activation.

The immutable staging helper fetches only the exact public Git revision over a
separately qualified restricted network. Build, validation and collection use
network none. Containers have no metadata access, host credentials, FPGA
devices, host sockets or colleague mounts. A separate root watchdog enforces
the absolute deadline even if the launcher or Docker becomes unresponsive.
Cancellation uses the same fixed SSM document and exact durable request identity.

Only after untrusted processes stop does root seal the bounded fixed-name files
and publish them through a write-only artifact role. The controller can retrieve
only that approved prefix and verifies receipt identity, sizes and hashes. The
host profile is host-wide; proving that guests cannot obtain it is mandatory.
Failed qualification cannot produce a deployable receipt.

See [the controller/runtime runbook](ci/README.md), [host contract](ci/host/CONTRACT.md)
and [activation acceptance criteria](ci/ACCEPTANCE.md). The checked-in example
configuration is disabled and unapproved. Operator configuration, IAM resource
identifiers and evidence belong outside this public repository. No repository
variable, IAM resource, service or private engine is installed by these sources.

Activation requires independent evidence for the private Docker and containerd
installation, bounded storage, sealed tool/dependency snapshots, actual image
digest, offline license checkout, restricted staging egress, metadata isolation,
aggregate cgroups, watchdog/cancellation and coordination with physical users.
Verify the actual trusted-main OIDC subject and pin the installed SSM document.
Do not change flags to bypass a failed prerequisite. A main merge and operational
activation are separate decisions from opening this source PR.

Every PR update requests a fresh eligible build after activation. Direct builds
share a licensed-runner queue; trusted controller runs share a separate queue.
Both use GitHub's documented [`queue: max`][queue] limit of 100 pending jobs. The
three-admission pilot cap remains authoritative even when a queue contains more
requests. Artifacts use unique run/attempt names and seven-day retention; a tar
is published only after strict timing, DRC and provenance checks pass.

Public Actions logs and artifacts must be treated as public output. Keep secrets,
license material, private endpoints and host/account identifiers out of source,
workflow logs and build evidence. Do not execute untrusted PR code with
credentials through `pull_request_target`.

## Physical tests

See [the physical-test tooling](tests/) for the bounded BAR0 loader and official
CoralNPU example suite. Programs must fit ITCM and DTCM including stack, use
supported instructions, complete with the expected return sentinel transition,
and match exact output bytes. Halt alone is not success.

Run against an explicitly selected, already approved image and slot. Preserve
ELF hashes, disassembly, source/build flags, expected/actual results, and repeated
execution evidence. Image upload, AFI creation, and slot loading require a
separate operational decision; this repository does not perform them.

[runners]: https://docs.blacksmith.sh/blacksmith-runners/overview
[license]: https://github.com/aws/aws-fpga/blob/f2/hdk/docs/on_premise_licensing_help.md
[aws]: https://awsdocs-fpga-f2.readthedocs-hosted.com/latest/User-Guide-AWS-EC2-FPGA-Development-Kit.html
[queue]: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idconcurrency
