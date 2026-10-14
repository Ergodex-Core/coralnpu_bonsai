"""Fixed host artifact uploader. No archive parsing, traversal, or PR receipt trust.

Caller must stop every untrusted process and seal the inbox root-owned first.
The supplied client is dependency-injected; create_host_s3 is the live factory.
No network operations occur at import. Disabled config forbids even client setup.
"""
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import re
import stat
import urllib.request

from protocol import validate_request
from validate_bundle import activation_errors
from sdk_runtime import verify_sdk_runtime, isolated_session

LIMITS = {"checkpoint.tar": 1500000000, "evidence.tar": 499000000}


def require_enabled(config):
    errors = activation_errors(config)
    if errors:
        raise ValueError(
            "artifact upload activation blocked: " + "; ".join(errors)
        )


def _identity(info):
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns
    )


def _open_sealed(inbox_fd, name, owner):
    directory = os.fstat(inbox_fd)
    if not stat.S_ISDIR(
            directory.st_mode
    ) or directory.st_uid != owner or directory.st_mode & 0o077:
        raise ValueError("inbox is not a sealed private trusted directory")
    fd = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox_fd
    )
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != owner or info.st_mode & 0o222
                or info.st_dev != directory.st_dev):
            raise ValueError(
                "artifact is not a sealed single-link regular file on the inbox filesystem"
            )
        if not 0 < info.st_size <= LIMITS[name]:
            raise ValueError("artifact exceeds its fixed size bound")
        stream = os.fdopen(fd, "rb")
        fd = None
        return stream, info
    finally:
        if fd is not None:
            os.close(fd)


def _digest(stream, original):
    digest = hashlib.sha256()
    remaining = original.st_size
    while remaining:
        chunk = stream.read(min(1048576, remaining))
        if not chunk:
            raise ValueError("artifact shortened during hashing")
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1) or _identity(os.fstat(stream.fileno())
                                   ) != _identity(original):
        raise ValueError("artifact changed during hashing")
    stream.seek(0)
    return digest


def _put(s3, config, key, body, size, digest, content_type):
    # A collision/unknown outcome fails closed: write-only IAM cannot independently
    # verify an existing remote object. Never silently overwrite or add read grants.
    response = s3.put_object(
        Bucket=config["artifact_bucket"],
        Key=key,
        Body=body,
        ContentLength=size,
        ContentType=content_type,
        ServerSideEncryption="AES256",
        IfNoneMatch="*",
        ExpectedBucketOwner=config["aws_account"],
        ChecksumSHA256=base64.b64encode(digest.digest()).decode("ascii")
    )
    code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code != 200:
        raise ValueError("artifact upload did not return confirmed HTTP 200")
    returned = response.get("ChecksumSHA256")
    if returned is not None and returned != base64.b64encode(digest.digest()
                                                             ).decode("ascii"):
        raise ValueError("S3 checksum differs from sealed artifact")


def publish(config, request, inbox_fd, trusted_result, s3, expected_owner=0):
    """Publish sealed outputs and a generated commit receipt; return that receipt.

    expected_owner is a test seam; installed host callers must retain root (0).
    trusted_result comes from the immutable supervisor, never a PR-owned JSON.
    """
    require_enabled(config)
    validate_request(request, config)
    allowed = {"qualified", "source_sha", "source_ref", "status"}
    if not isinstance(trusted_result, dict) or set(trusted_result) != allowed:
        raise ValueError("unexpected trusted-result fields")
    if (type(trusted_result["qualified"]) is not bool
            or trusted_result["source_sha"] != request["source_sha"]
            or trusted_result["source_ref"]
            != request["eligibility"]["source_ref"] or trusted_result["status"]
            not in {"success", "failed", "timed_out", "cancelled"}
            or trusted_result["qualified"] != (trusted_result["status"]
                                               == "success")):
        raise ValueError(
            "trusted result does not identify the exact qualified run"
        )
    proof = request["eligibility"]
    receipt = {
        "schema_version": 1,
        "run_id": request["run_id"],
        "attempt": request["attempt"],
        "source_sha": request["source_sha"],
        "source_ref": proof["source_ref"],
        "source_kind": proof["source_kind"],
        "repository": proof["repository"],
        "repository_id": proof["repository_id"],
        "pull_request": proof["pull_request"],
        "source_run_id": proof["source_run_id"],
        "source_run_head_sha": proof["source_run_head_sha"],
        "source_job_id": proof["source_job_id"],
        "qualified": trusted_result["qualified"],
        "status": trusted_result["status"]
    }
    names = ["evidence.tar"]
    if trusted_result["qualified"]:
        names.insert(0, "checkpoint.tar")
    with ExitStack() as stack:
        files = {}
        # Validate/hash every fixed file before the first AWS request.
        for name in names:
            stream, info = _open_sealed(inbox_fd, name, expected_owner)
            stack.enter_context(stream)
            digest = _digest(stream, info)
            files[name] = (stream, info, digest)
            stem = name.split(".")[0]
            receipt[stem + "_bytes"] = info.st_size
            receipt[stem + "_sha256"] = digest.hexdigest()
        for name in names:
            stream, info, digest = files[name]
            if _identity(os.fstat(stream.fileno())) != _identity(info):
                raise ValueError("artifact mutated before upload")
            _put(
                s3, config, request["artifact_prefix"] + name, stream,
                info.st_size, digest, "application/x-tar"
            )
            if _identity(os.fstat(stream.fileno())) != _identity(info):
                raise ValueError(
                    "artifact mutated during upload; no success receipt published"
                )
    payload = json.dumps(
        receipt, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(payload) > 1000000:
        raise ValueError("generated receipt exceeds limit")
    _put(
        s3, config, request["artifact_prefix"] + "receipt.json",
        io.BytesIO(payload), len(payload), hashlib.sha256(payload),
        "application/json"
    )
    return receipt


def create_host_s3(config, urlopen=None, sdk_check=verify_sdk_runtime):
    """Use only fresh IMDSv2 credentials of the configured instance role.

    No ambient credential chain or host AWS configuration files are read. No
    metadata request happens until all activation gates pass. Tokens and secret
    values remain in process memory and are never printed or persisted.
    """
    require_enabled(config)
    sdk_check()
    if urlopen is None:
        urlopen = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        ).open
    base = "http://169.254.169.254/latest/"

    def read(path, method="GET", headers=None, maximum=16384):
        request = urllib.request.Request(
            base + path, method=method, headers=headers or {}
        )
        with urlopen(request, timeout=3) as response:
            data = response.read(maximum + 1)
        if not data or len(data) > maximum:
            raise ValueError("IMDS response outside permitted size")
        return data.decode("utf-8")

    token = read(
        "api/token", "PUT", {"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
        1024
    )
    headers = {"X-aws-ec2-metadata-token": token}
    role = read(
        "meta-data/iam/security-credentials/", headers=headers, maximum=256
    ).strip()
    if role != config["host_role_and_profile"] or not re.fullmatch(
            r"[A-Za-z0-9+=,.@_-]{1,64}", role):
        raise ValueError("IMDS role differs from approved host role")
    credentials = json.loads(
        read("meta-data/iam/security-credentials/" + role, headers=headers)
    )
    expiry = datetime.fromisoformat(
        credentials.get("Expiration", "").replace("Z", "+00:00")
    )
    if credentials.get("Code") != "Success" or (
            expiry - datetime.now(timezone.utc)).total_seconds() < 1200:
        raise ValueError("IMDS credentials are unavailable or expire too soon")
    if not all(isinstance(credentials.get(x), str) and credentials[x]
               for x in ("AccessKeyId", "SecretAccessKey", "Token")):
        raise ValueError("IMDS credential response is incomplete")
    from botocore.config import Config
    session = isolated_session()
    session.set_credentials(
        credentials["AccessKeyId"], credentials["SecretAccessKey"],
        credentials["Token"]
    )
    region = config["region"]
    return session.create_client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(
            signature_version="s3v4",
            proxies={},
            connect_timeout=10,
            read_timeout=30,
            retries={
                "mode": "standard",
                "total_max_attempts": 2
            }
        )
    )
