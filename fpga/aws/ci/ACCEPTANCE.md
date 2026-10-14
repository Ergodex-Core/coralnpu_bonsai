# Activation acceptance criteria

The checked-in configuration remains disabled. Local unit tests exercise real
validation and command construction against fakes; they do not establish that an
AWS role, shared host, private engine, license, network boundary or FPGA works.
Keep dated evidence and operational values outside this public checkout.

## Identity and permission readback

- Verify the repository ID, owner ID, protected default branch and actual trusted
  workflow OIDC subject and audience. Use one exact subject; do not use a wildcard
  or enable trust based on an assumed claim. Source workflow jobs have no OIDC.
- Read back the orchestrator role, host profile and bucket policy. The controller
  may invoke only the fixed document on the designated instance and read the
  designated artifact prefix. The host may write that prefix; it cannot list or
  read S3 objects. Neither role can change IAM, start instances, promote an FPGA
  image or modify unrelated storage.
- Compare the numeric SSM document version, AWS SHA256 and full document content
  with this reviewed source. Confirm that Build and Cancel accept only the fixed
  request schema and invoke the root-owned installed wrapper. Store no bearer
  capability, credential or license value in SSM parameters or logs.
- Install the controller from the protected branch and the host modules from a
  reviewed immutable source snapshot. Verify ownership, modes and all interpreter,
  SDK, helper image and entrypoint hashes. A local `sha256:<64 hex>` image ID or
  repository digest is required; mutable tags are rejected.

## Shared-host qualification

- Obtain a coordinated idle window, with physical test and image promotion
  priority. Integrate `/run/lock/coralnpu-fpga-slot-0.lock` into each participating
  physical workflow and demonstrate exclusion in both directions. The example's
  `shared_lock_integrated_with_physical_tests` flag stays false until then.
  Interactive synthesis may not honor the lock; separately inspect ownership of
  active jobs and obtain the host scheduling permit.
- Use a dedicated nonlogin account and a private Docker/containerd engine, sockets
  and data root. Never reuse the colleague engine, home directories or caches.
  Confirm the engine, its storage and every helper obey the dedicated bounds.
- Measure the preallocated 250 GiB filesystem and 2,000,000-inode limit; include
  staging, logs, engine storage, collection and temporary upload files. Verify
  exhaustion fails inside that bound without filling the shared filesystem.
- Prove aggregate limits of 16 CPUs, 64 GiB RAM and 4,096 tasks across staging,
  build, qualification and collection. Confirm every process belongs to the
  recorded CI cgroup. Test the independent six-hour deadline with an unresponsive
  Docker daemon; cleanup must use the verified cgroup and never a global daemon
  stop, host shutdown, colleague process or FPGA slot action.
- Prove crash-safe admission limits of three distinct runs and one concurrent
  run, including restart, replay, cancellation before admission, late Build
  delivery, repeated cancellation and terminal-success preservation.
- Qualify the syscall profile and nonroot, capability-free, read-only container
  boundary. Probe metadata, host services, peer networks and host sockets from
  every guest phase. Prove denial using the actual namespace/firewall setup;
  configuration strings alone are insufficient. The root uploader uses IMDSv2
  outside guests; remember an instance profile is available to other trusted
  host processes allowed to reach IMDS.

## Tool, license and artifact qualification

- Review complete transitive mount inventories using metadata and vendor
  documentation, without reading secrets. A read-only tool mount can still expose
  credentials or license-secret files. Approve the exact filtered mount and
  license mechanism; no broad vendor directory is presumed safe.
- Verify the pinned HDK, Vivado, shell, IP, RTL generator, compiler and public
  dependency snapshot. Run builds with network disabled and qualify any isolated
  source-fetch egress separately. Do not run PR Tcl or other PR code on the host.
- Stage one exact PR head through the fixed repository URL and compare commit
  identity before build. Exercise current same-repository writer eligibility,
  exact-head maintainer approval for forks, review vetoes, changed heads and a
  final eligibility recheck immediately before submission.
- Exercise successful and failed synthesis. Enforce strict stage, timing and DRC
  gates; preserve bounded failure evidence. Demonstrate safe collection rejecting
  symlinks, hardlinks, devices, FIFOs, traversal and oversized files. Stop all
  guests before sealing root-owned immutable files and uploading them.
- Test prefix-only conditional uploads, collision rejection, receipt-last order
  and bounded downloads. Compare run/attempt, source SHA/ref, actual sizes and
  SHA256 hashes. Only a qualified successful receipt can publish checkpoint.tar.
- Exercise CI cancellation, lost SendCommand responses and transport failure.
  Exact-run cancellation must be idempotent; an undelivered cancellation must
  still end at the independent host deadline.

## Enablement and evidence

Only after the relevant evidence passes may an operator set the corresponding
qualification flags in external configuration. Keep `enabled=false` until all
checks pass; then explicitly select `FPGA_CI_BACKEND=ssm` and
`FPGA_SSM_ENABLED=true`. The pilot is still capped at three admissions. Source
merge, host installation, live enablement and subsequent pilot extension are
separate actions. A passing source workflow does not imply successful synthesis,
physical execution, AFI creation or image promotion; record those outcomes
separately.
