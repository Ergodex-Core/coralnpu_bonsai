# Fixed host runtime contract

The local runtime is implemented in `entrypoint.py`, `runtime.py`, `store.py`,
`linux_adapter.py`, `deadline_guard.py`, `volume.py`, `mount_inventory.py`, and
`secure_files.py`. Offline tests exercise injected command/filesystem boundaries;
none of this bundle has been installed or qualified on the live host. The
configuration remains disabled. The service files are installation proposals.

The fixed SSM wrapper invokes the private, root-owned hash-pinned Python SDK
virtual environment with `-I`. Installed modules and every parent must be root
owned and non-writable. Root never imports, executes or extracts PR source.
The SDK version gate runs before admission and again before credential use.

## Request and admission

Six named SSM values carry `Operation=Build|Cancel`, exact source SHA, run ID,
attempt, request checksum and base64-encoded nonsecret request. Strict validation
binds repository identity, PR ref, source-run evidence, approved SHA and artifact
prefix. Environment values cannot replace configuration or select commands.

A private per-request mutex serializes immutable request creation, cancellation
markers and admission. Cancel is available after future builds are disabled.
Cancel before Build creates a durable exact-request tombstone, consumes no
admission and stops no unit. Reusing an identity with a changed request fails.
Repeated Build delivery never restarts the launcher. SSM waits for a terminal
result; cancellation stops only the recorded request boundary. The independent
watchdog remains the fallback when cancellation transport fails.

The launcher holds the existing shared FPGA lock without replacing its inode.
An explicit, current root-owned scheduling permit must attest a colleague handoff
for that exact run and six-hour window. A process scan supplements that permit.
Neither momentary idleness nor a config flag alone permits use of the shared host.
Busy locks and failed preflight checks terminate without consuming admission.
The root ledger permits at most three distinct admissions, one at a time; failure
and timeout count. Quarantined or stale running state blocks later admission.

## Storage, private engine and containment

`volume.py` reserves one 250 GiB loop-backed ext4 filesystem for the complete pilot,
including all retained attempts. It measures allocated blocks, inode count,
mount options and loop/image identity. Existing unidentified or partial state is
never reformatted, detached or adopted. The root mount is private. All guest
writable directories, private Docker data and private containerd data must fit
this same cap. No changes to colleague directories or shared mounts are allowed.

Runtime Docker commands accept only `unix:///run/coralnpu-ci/docker.sock`.
The shared Docker daemon is never reconfigured or used. Preflight checks the
private Docker root `/srv/coralnpu-ci/engine/docker`, systemd cgroup-v2 driver,
AppArmor, immutable image identity and dedicated daemon unit containment. The
separate private containerd socket is `/run/coralnpu-ci/containerd.sock`.
Private daemon installation, storage configuration and live qualification remain
pending; daemon storage and helper placement require independent verification.

The aggregate `coralnpu-ci.slice` includes launcher, private engines and every
container helper: 16 CPUs, 64 GiB with zero swap, and 4096 tasks. Runtime reads the
actual ControlGroup returned by systemd and verifies caps and inode identity.
Each run has a permanent child slice. Its payload is never thawed or reused after
retirement. A daemon-delayed start remains caught by that frozen run boundary.

Immutable stage/build/qualify/collect containers run as the dedicated nonroot UID,
with read-only root, dropped capabilities, no new privileges, seccomp, AppArmor,
private namespaces and no devices, credentials, host socket or colleague binds.
Every actual bind source, destination and write flag must match the fixed phase
policy. HOME, TMPDIR and /run are separate per phase. Source provenance metadata
is writable only by the trusted stager and read-only thereafter. Tool snapshots
must pass the exact root-reviewed transitive inventory, ownership, content hash
and internal-symlink checks; read-only alone is insufficient to exclude secrets.

The stager may use only the separately qualified fixed GitHub HTTPS bridge.
Its root-owned receipt and current dedicated firewall rules must match; negative
host/private/metadata/IPv6 access probes remain a live activation prerequisite.
All later phases use network `none`. Helpers wait on a root-created control gate
until the launcher proves their actual PID cgroup and namespace containment.
Only the build phase executes PR scripts. Qualification uses baked source gates
and Tcl, verifies source inventory, and constructs the native deployable tar.
Stage/build failure invokes qualification's evidence-only mode; it never invokes
Vivado or produces a success checkpoint. Infrastructure failure or absolute
expiration starts no new guest phase.

## Deadline and cleanup

A separate root watcher outside the workload slice receives the same flock
open-file-description before any guest starts. Launcher death cannot release the
lease. The absolute CLOCK_BOOTTIME deadline directly freezes and kills the pinned
payload and kills a stuck launcher without calling Docker or systemd APIs.
The watcher releases its lease only after a proven frozen, empty payload. Unknown
cleanup state is quarantined and retains the lease. Malformed or disconnected
protocol peers cannot bypass this condition. Normal ExecStopPost preserves an
already terminal outcome. No daemon, colleague process, FPGA image or driver is
stopped or modified by these operations.

## Artifact publication

After all untrusted processes stop, the fixed offline collector copies only
`checkpoint.tar` and `evidence.tar` using pinned directory descriptors, no-follow
regular-file checks, single-link requirements, mutation detection and streaming
bounds. Failed builds require only evidence. Root never parses PR tar archives.
Collector nonzero exit prevents publication even if files appear complete.

Root permanently freezes the payload, seals the fixed inbox files as root-owned
read-only, and verifies it before creating the narrowly scoped upload client.
The uploader hashes actual bytes and uses only bounded PutObject operations with
AES256, expected account and create-only keys. A root-generated receipt is last.
Successful receipts require independently qualified checkpoint plus evidence;
failed receipts cannot label a checkpoint deployable. No guest can reach IMDS or
see host credentials. At deadline, a local terminal failure is authoritative;
there is no fabricated upload or extended collection window.

## Remaining activation evidence

Before enabling, independently verify private engine installation and all storage
roots, image digest, sealed nonsecret tool/dependency inventories, offline Vivado
license, dedicated fetch firewall/proxy probes, aggregate pressure limits,
watcher survival and delayed-start cutoff, bounded filesystem measurements,
mutual exclusion with physical tests/promotion, current colleague scheduling
permit, SSM document hash/version and approved-main OIDC identity. Local unit
success is not evidence that these Linux or cloud boundaries work on the host.
