# Isolated FPGA CI adapters

This directory contains the controller, fixed host launcher, immutable image
helpers and local tests for a bounded existing-host FPGA build pilot. Live
execution is disabled by default. `config.example.json` contains no approved
account, instance, bucket, image or OIDC identity, and all approval and
qualification flags are false. Passing the local tests does not qualify an
installed host, license, network boundary or routed FPGA image.

## Workflow routes

`.github/workflows/fpga.yml` always runs source, host-harness and CI-adapter tests
without AWS or FPGA-host credentials. `FPGA_CI_BACKEND` selects a build route:

| Value | Behavior |
| --- | --- |
| `source-only` (default) | Source checks run; readiness explicitly fails because no licensed artifact builder is enabled. |
| `direct` | Requires `FPGA_BUILD_ENABLED=true` and the existing runner/HDK/Vivado variables. Uses the proposed merge checkout. Forks are blocked. |
| `ssm` | Requires `FPGA_SSM_ENABLED=true` after target-host qualification. The trusted-main controller builds the exact PR head SHA. |

The SSM controller also requires a complete operator configuration in
`FPGA_CI_CONFIG_JSON`, validated against the example schema. Keep real operational
configuration outside source control, and never put credentials or license
secrets in it. Its workflow uses `workflow_run` on protected `main`; a PR copy
of the controller workflow does not activate it. Source merge and infrastructure
activation are separate operator decisions.

The controller checks the named source job through GitHub's API. It does not
require the entire source workflow to succeed, because a disabled builder's
readiness result must not prevent checking source eligibility. Same-repository
PRs from a current writer are eligible; forks or non-writers require a current
maintain/admin review of that exact head SHA. Effective maintainer requests for
changes veto admission. The API facts are checked again immediately before
submission. PR code is never checked out or executed in the credentialed
controller.

## Execution boundary

The controller submits only the fixed SSM document, pinned by numeric version,
AWS SHA256 and content. Parameters contain source/run metadata and a checksum;
no credentials or bearer upload URLs travel through SSM. The document invokes
only the installed root-owned wrapper. Build and Cancel use the same exact
request identity; cancellation cannot select an arbitrary process or unit.

The launcher limits a pilot to three admissions, one at a time, with a six-hour
independent host deadline. Stage/build/qualification/collection use immutable,
unprivileged containers. A private Docker/containerd engine, cgroup limits,
250 GiB / 2,000,000-inode filesystem bound, fixed syscall policy, pinned mount
inventories and measured network denials must be qualified before admission.
The examples do not install or configure those services automatically.

The shared physical lock is `/run/lock/coralnpu-fpga-slot-0.lock`. Setting a flag
is not proof that all physical test, image promotion and interactive jobs honor
it. `shared_lock_integrated_with_physical_tests` stays false in the example;
verify exclusion in both directions and obtain a coordinated host window before
activation. Cleanup never stops the shared instance, touches an FPGA slot or
kills a colleague's job.

A fixed collector copies only bounded regular files. After every guest process
stops, the host seals those files and uploads through a write-only artifact
prefix grant. The root uploader uses IMDSv2 credentials for the configured host
role; guests must be proven unable to reach metadata or other host services.
An instance profile is host-wide, so trusted host processes with IMDS access can
also use that role. The controller has read-only access to the artifact prefix
and validates run/source/ref identity plus actual sizes and hashes. A generated
receipt is uploaded last; no PR-provided success receipt is trusted.

Interrupted or uncertain submissions request exact-run cancellation. Failed
cancellation delivery leaves the independent host deadline as fallback.
Conditional upload collisions fail closed rather than overwriting prior keys or
silently adding S3 read permissions to the host.

Read-only vendor mounts can expose secrets. Review their complete transitive
contents using metadata and documentation, exclude credential and license-secret
files, and qualify offline licensing with a sealed public dependency snapshot.
No colleague home, cache, cloud credential, host socket or FPGA device is mounted
into a guest. See [the host contract](host/CONTRACT.md),
[the immutable helper image](image/README.md), and
[activation acceptance criteria](ACCEPTANCE.md).

## Local validation

Run pure tests without external services:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s fpga/aws/ci/tests -v
PYTHONDONTWRITEBYTECODE=1 python3 fpga/aws/ci/image/test_entrypoint.py
python3 fpga/aws/ci/validate_bundle.py
```

For the actual AWS SDK parameter-model tests, create an isolated environment and
install the wheel-hashed lock:

```bash
python3 -m venv /tmp/coralnpu-ci-sdk
/tmp/coralnpu-ci-sdk/bin/python -m pip --isolated --disable-pip-version-check install \
  --require-hashes --only-binary=:all: --no-deps -r fpga/aws/ci/requirements-controller.lock
PYTHONDONTWRITEBYTECODE=1 /tmp/coralnpu-ci-sdk/bin/python -m unittest discover -s fpga/aws/ci/tests -v
```

SDK tests use Stubber and forbid AWS network requests. Missing or mismatched SDK
packages skip those two model tests in the pure run; CI installs the exact lock
so they execute. `validate_bundle.py --activation` and the controller reject the
example before AWS access. Live IAM/SSM readback, runtime isolation, image/license
qualification and physical FPGA execution require separate evidence.
