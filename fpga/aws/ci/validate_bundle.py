"""Local structural validation, or fail-closed activation prerequisite check."""
import argparse
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def activation_errors(config):
    errors = []
    if not isinstance(config, dict):
        return ["configuration must be a JSON object"]
    for key in ["enabled", "persistent_changes_approved",
                "host_isolation_verified", "license_in_container_verified",
                "source_transfer_adapter_verified",
                "artifact_transfer_adapter_verified",
                "shared_lock_integrated_with_physical_tests",
                "artifact_transport_scope_approved",
                "bounded_disk_and_inodes_verified",
                "independent_cgroup_cleanup_verified",
                "shared_host_scheduling_permit_verified",
                "host_runtime_verified", "aggregate_cgroup_limits_verified",
                "immutable_entrypoints_verified", "private_engine_verified",
                "staging_network_verified"]:
        if config.get(key) is not True:
            errors.append(key + " is not verified/approved")
    for key, value in config.items():
        if isinstance(value, str) and "UNRESOLVED" in value:
            errors.append(key + " remains unresolved")
    if not isinstance(config.get("document_numeric_version"),
                      str) or not re.fullmatch(
                          r"[1-9][0-9]*", config["document_numeric_version"]):
        errors.append("numeric document version not pinned")
    if not isinstance(config.get("document_aws_sha256"),
                      str) or not re.fullmatch(r"[0-9a-f]{64}",
                                               config["document_aws_sha256"]):
        errors.append("AWS document SHA256 not pinned")
    bounds = {
        "maximum_distinct_admissions": 3,
        "maximum_parallel_jobs": 1,
        "runtime_seconds": 21600,
        "container_cpus": 16,
        "container_memory_bytes": 68719476736,
        "container_pids": 4096,
        "artifact_total_bytes_per_run": 2000000000,
        "per_run_disk_bytes": 268435456000,
        "per_run_inode_limit": 2000000,
        "orchestrator_session_seconds": 25200
    }
    for name, expected in bounds.items():
        if type(config.get(name)) is not int or config[name] != expected:
            errors.append(name + " differs from reviewed bound")
    patterns = {
        "aws_account": r"[0-9]{12}",
        "instance_id": r"i-[a-f0-9]{17}",
        "repository": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        "orchestrator_role": r"[A-Za-z0-9+=,.@_-]{1,64}",
        "host_role_and_profile": r"[A-Za-z0-9+=,.@_-]{1,64}",
        "document_name": r"[A-Za-z0-9_.-]{3,128}",
        "artifact_bucket": r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]",
        "container_image_digest": r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}"
    }
    for name, pattern in patterns.items():
        if not isinstance(config.get(name), str) or not re.fullmatch(
                pattern, config[name]):
            errors.append(name + " is invalid or unpinned")
    for name in ("host_uid", "host_gid"):
        if type(config.get(name)) is not int or config[name] < 1001:
            errors.append(name + " must identify the reserved nonroot account")
    if config.get("region") != "us-east-1" or config.get(
            "trusted_ref") != "refs/heads/main":
        errors.append("region or trusted branch differs from approved scope")
    if config.get("shared_lock") != "/run/lock/coralnpu-fpga-slot-0.lock":
        errors.append("wrong shared physical-harness lock")
    if (config.get("docker_socket") != "unix:///run/coralnpu-ci/docker.sock"
            or config.get("containerd_socket")
            != "/run/coralnpu-ci/containerd.sock"):
        errors.append(
            "private engine sockets differ from fixed reviewed paths"
        )
    if config.get("staging_network_qualification_path"
                  ) != "/etc/coralnpu-ci/staging-network.json":
        errors.append(
            "staging network proof path differs from fixed reviewed path"
        )
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activation", action="store_true")
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "CORALNPU_ACTIVATION_CONFIG", str(ROOT / "config.example.json")
        )
    )
    args = parser.parse_args()
    files = list(ROOT.rglob("*.json"))
    for path in files:
        json.loads(path.read_text())
    config = json.loads(Path(args.config).read_text())
    if args.activation:
        errors = activation_errors(config)
        if errors:
            raise SystemExit("ACTIVATION BLOCKED:\n- " + "\n- ".join(errors))
    else:
        if config["enabled"] is not False:
            raise SystemExit("Review bundle unexpectedly enabled")
        print(
            f"Validated {len(files)} JSON files; activation remains disabled"
        )


if __name__ == "__main__":
    main()
