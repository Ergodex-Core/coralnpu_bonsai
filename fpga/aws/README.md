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

The PR workflow deliberately fails its readiness job until a licensed,
isolated Blacksmith builder is provisioned and approved. Committing this YAML
does not enable a working FPGA build service. No existing image is uploaded,
created, or loaded by this workflow.

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
GitHub's `pull_request` checkout uses the proposed merge commit; provenance
must distinguish that revision from the PR head revision.

## Blacksmith activation prerequisites

Activation requires a verified ephemeral runner with the complete pinned
HDK/IP/shell/toolchain installation and a valid license for its execution
location. A standard Ubuntu runner label alone does not provide Vivado.
Blacksmith documents `blacksmith-32vcpu-ubuntu-2404` with 32 vCPUs, 128 GB RAM,
and 1.5 TB disk; actual available disk, memory, tools, and license features
must still pass preflight. See the [runner specifications][runners].

Off-AWS Vivado requires an appropriate ML Enterprise license with
`EncryptedWriter_v2` and Partial Reconfiguration support. Do not copy an AWS
FPGA developer-AMI license to Blacksmith. See the [AWS licensing guide][license].
An alternative is an isolated AWS FPGA developer-AMI build host orchestrated
by Blacksmith; it needs an explicitly approved budget and narrowly scoped
permissions before implementation. F2 hardware is only needed for physical
execution, not checkpoint generation. See the [AWS development guide][aws].

After those prerequisites and spending limits are approved and verified,
configure repository variables to identify the actual installation:

| Variable | Required value |
| --- | --- |
| `FPGA_BUILD_ENABLED` | `true` after provisioning and approval |
| `FPGA_BLACKSMITH_RUNNER` | Verified ephemeral Blacksmith runner label |
| `FPGA_HDK_ROOT` | Installed, pinned HDK directory on that runner |
| `FPGA_VIVADO_SETTINGS` | Installed Vivado 2025.2 `settings64.sh` path |

No variable or credential is provisioned by this PR. Never put a license,
credential, private endpoint, or host/account identifier in these sources.
PR code can modify workflow files and build scripts: YAML fork checks alone
are not a credential boundary. Enforce access and isolation outside PR code;
do not expose persistent host credentials, license material, trusted writable
tool caches, or unrelated files. Do not use `pull_request_target` to execute
untrusted source with credentials. Forks receive source checks and an explicit
blocked build result; a maintainer must review their source before using a
licensed builder.

Each PR update requests a fresh build of the latest proposed merge commit.
A newer update cancels older work for that same PR. Different PR builds share
one licensed-build concurrency group with up to 100 pending jobs, using
GitHub's documented [`queue: max`][queue]. Beyond that limit GitHub cancels
additional jobs; retry after the queue drains. The build timeout is six hours.
No sticky disks or shared mutable tool caches are configured. A provider-side
budget and runner allocation limit are required before activation.

Artifacts have commit/run/attempt-specific names, seven-day retention, and
include the tar only after qualification succeeds. Failed builds retain
available logs and validation evidence. Missing artifact paths are failures.
Public-repository Actions logs and artifacts must be treated as public output;
keep all operational secrets out of the runner environment and tool logs.

## Reusing an existing F2 builder

An existing F2 developer host can run the same build entrypoint after access and
isolation are approved. Keep all temporary files, caches, and outputs on its
allocated build disk and serialize work with the host's other operators. Build
jobs must never load the FPGA or stop the shared instance.

The shortest activation route is a trusted Blacksmith orchestrator on protected
`main`, GitHub OIDC, and a custom SSM document that invokes a root-owned fixed
launcher on the exact approved host. This requires separate approval for the
OIDC role, a minimal SSM instance profile, the custom document, and host account
isolation. The orchestrator needs command access to only that instance/document
and narrowly scoped artifact storage; it needs no EC2 lifecycle permissions.
The current workflow does not create these resources or claim this route is live.

The launcher must validate the repository, exact reviewed commit SHA, and run ID.
Run PR code as an unprivileged account in a bounded container with no Docker
socket, host networking, FPGA devices, colleague directories, host credentials,
or metadata-service access. Use read-only tool mounts, one host lock, a six-hour
process deadline, and cleanup that survives workflow cancellation. Do not give
CI an existing developer's sudo-capable SSH identity. Public-fork execution on a
shared developer host requires a stronger verified isolation boundary; begin
with maintainer-approved exact SHAs and invalidate approval after each push.

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
