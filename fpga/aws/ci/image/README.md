# Trusted container helpers — prepared, not runtime-qualified

This directory supplies the fixed helper image context. Local tests do not
qualify a built image, host service, filesystem mount, or Vivado license inside
a container. The fixed source policy is recorded in
`trusted-identity.json`; the final image must be selected by its measured digest.

The root launcher owns the run directories and proves that previous guest
processes are gone before advancing phases. Each helper runs as UID/GID 23001,
with an immutable root filesystem and only the mounts below. Every helper waits
at most 30 seconds for `/control/go`, a root-owned mode-0444 regular file containing
`<run-key> <phase> <source-sha>\n`. Root publishes this file only after inspecting
the running container's actual cgroup, namespace, image and mount configuration.

| Phase | Writable mounts | Read-only mounts | Network |
| --- | --- | --- | --- |
| stage | `/job/source`, `/job/metadata`, fresh `/job/tmp` | `/control` | separately qualified GitHub-only staging network |
| build | `/job/source`, `/job/build`, fresh `/job/tmp` | `/job/metadata`, `/deps`, selected vendor tool paths, `/control` | none |
| qualify | `/job/qualification`, fresh `/job/tmp` | `/job/source`, `/job/build`, `/job/metadata`, selected vendor tool paths, `/control` | none |
| collect | `/inbox` | `/work/output` bound to qualification/output, `/control` | none |

Do not mount a colleague's home, repository/cache, host Docker socket, host
devices, credentials, instance identity/metadata helper, or license-secret file
into any helper. Metadata and license-helper files must be excluded from tool
mounts. Prior host Synthesis/Implementation checkout successes do not prove an
offline container has a usable license. No fallback expands this
boundary if license checkout fails.

All four commands are `/opt/coral-ci/bin/<phase>` with fixed validated arguments:

```text
--repository Ergodex-Core/coralnpu_bonsai
--source-sha <40-lowercase-hex>
--source-ref refs/pull/<positive-number>/head
--run-key <run-id>-<attempt>
```

The collector additionally takes `--deadline <monotonic-seconds>` and optional
`--evidence-only`. Qualify also accepts `--evidence-only`: after a prior phase
failure it creates diagnostic evidence without invoking Vivado or requiring a
complete staging receipt. It exits nonzero because the build is unqualified.
The collector delegates opaque byte transfer to the shared reviewed
`secure_files.py` boundary, copied into the image at context preparation.

Staging uses only `https://github.com/Ergodex-Core/coralnpu_bonsai.git`, fetches
the supplied validated PR head ref, verifies its exact Git commit, and requires
the pinned Coral comparison commit in the fetched history. It disables hooks,
user/system Git config, credential helpers, submodule recursion, external/file
protocols and redirects. It verifies checked-out bytes against the Git tree,
records SHA256 per file in separately mounted metadata, and rejects special
files, gitlinks and symlinks that escape the checkout. Only trusted Git code runs
in this phase. GitHub TCP/443 and the dedicated DNS resolver are the only intended
egress. DNS, IP changes and IPv6 require actual root-enforced network tests;
the fixed remote URL alone is not an egress boundary.

The build uses baked `build.py`, pins and testbench against submitted source.
Bazel rules and submitted Vivado source/scripts remain untrusted execution inside
the offline, resource-limited container. It receives a private copy of the sealed
public repository cache; dependencies missing from the snapshot fail offline.
The driver can fail its own strict checks. The launcher must still run the
qualification phase after the stopped build to produce bounded failure evidence.
When staging or building failed, use its evidence-only mode. Never extend the
absolute run deadline just to collect evidence; an expired run can end with only
the root supervisor's terminal failure receipt.

Qualification rechecks staged source hashes, opens the exact routed checkpoint
with baked Tcl in a fresh Vivado process, and applies baked gates to the new
reports. PR qualification booleans, reports and packaged archives are not accepted
as approval. Prior synthesis/implementation logs are additional diagnostic gate
inputs; they do not replace freshly generated checkpoint reports. Passing creates
a new native AWS archive from the checkpoint and regenerated probes. Failing
creates only evidence. The DCP remains untrusted input to a proprietary parser;
the same credential-free container isolation and resource bounds apply here.

Outputs are `/job/qualification/output/checkpoint.tar` (only on success) and
`evidence.tar`. Evidence includes the trusted verdict, staging inventory, fresh
reports and explicitly labelled untrusted build logs. Names, file types and byte
counts are bounded. No arbitrary source/workspace tree is recursively archived.

`prepare_context.py --base-image ubuntu@sha256:<resolved-digest> --output <fresh>`
creates an isolated context without executing Docker. No mutable base tag is
accepted. OS package versions are recorded in the built image; after inspection,
the final image digest is the runtime pin. Package installation/image creation is
an operator preparation step, not something a PR can perform. A complete live
receipt must bind the image, package inventory, tool snapshots, mounts, cgroup,
quota, network denials and offline license outcome before activation.

Local tests cover identity validation, credential-environment exclusion, genuine
Git inventory, source tampering, unsafe links/special files, bounded evidence and
rejection of a PR-forged pass manifest. These tests do not prove Docker isolation,
offline dependencies, Vivado licensing or a full FPGA build.
