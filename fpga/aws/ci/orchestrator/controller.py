"""Trusted-main controller with dependency-injected command/artifact transport.

No PR source is downloaded or executed. All live entrypoints fail before network
access until the external activation configuration is qualified and enabled.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from validate_bundle import activation_errors
from protocol import make_request, encode_request
from resolve_request import resolve
from sdk_runtime import verify_sdk_runtime, isolated_session


def require_enabled(config):
    errors = activation_errors(config)
    if errors:
        raise ValueError(
            "ACTIVATION BLOCKED; no AWS requests made: " + "; ".join(errors)
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def oidc_token(config, environment=None, opener=None):
    require_enabled(config)
    environment = os.environ if environment is None else environment
    location = urllib.parse.urlsplit(
        environment["ACTIONS_ID_TOKEN_REQUEST_URL"]
    )
    if (location.scheme != "https" or not location.hostname
            or not location.hostname.endswith(".actions.githubusercontent.com")
            or location.username or location.password
            or location.port not in {None, 443}):
        raise ValueError(
            "OIDC endpoint differs from the trusted GitHub endpoint family"
        )
    query = [(k, v)
             for k, v in urllib.parse.parse_qsl(location.query)
             if k != "audience"]
    query.append(("audience", "sts.amazonaws.com"))
    url = urllib.parse.urlunsplit(
        location._replace(query=urllib.parse.urlencode(query))
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization":
            "Bearer " + environment["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        }
    )
    opener = opener or urllib.request.build_opener(
        NoRedirect(), urllib.request.ProxyHandler({})
    ).open
    with opener(request, timeout=15) as response:
        data = response.read(1048577)
    if len(data) > 1048576:
        raise ValueError("OIDC response exceeds maximum")
    token = json.loads(data)["value"]
    if not isinstance(token, str) or len(token) > 32768 or not re.fullmatch(
            r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise ValueError("invalid OIDC token encoding")
    segment = token.split(".")[1]
    claims = json.loads(
        base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    )
    expected = {
        "aud": "sts.amazonaws.com",
        "sub": config["verified_oidc_subject"],
        "repository_id": str(config["repository_id"]),
        "ref": config["trusted_ref"]
    }
    if any(str(claims.get(k)) != str(v) for k, v in expected.items()):
        raise ValueError("OIDC claims differ from approved exact trust")
    # Local decode checks consistency only. STS verifies signature and claims.
    return token


def aws_clients(config, token, run_id, attempt):
    require_enabled(config)
    verify_sdk_runtime()
    from botocore import UNSIGNED
    from botocore.config import Config
    session = isolated_session()
    region = config["region"]
    options = {
        "proxies": {},
        "connect_timeout": 10,
        "read_timeout": 30,
        "retries": {
            "mode": "standard",
            "total_max_attempts": 2
        }
    }
    sts = session.create_client(
        "sts",
        region_name=region,
        endpoint_url=f"https://sts.{region}.amazonaws.com",
        config=Config(signature_version=UNSIGNED, **options)
    )
    assumed = sts.assume_role_with_web_identity(
        RoleArn=
        f"arn:aws:iam::{config['aws_account']}:role/{config['orchestrator_role']}",
        RoleSessionName=f"coral-{run_id}-{attempt}",
        WebIdentityToken=token,
        DurationSeconds=config["orchestrator_session_seconds"]
    )
    credentials = assumed["Credentials"]
    session.set_credentials(
        credentials["AccessKeyId"], credentials["SecretAccessKey"],
        credentials["SessionToken"]
    )
    s3 = session.create_client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", **options)
    )
    ssm = session.create_client(
        "ssm",
        region_name=region,
        endpoint_url=f"https://ssm.{region}.amazonaws.com",
        config=Config(**options)
    )
    return ssm, s3


def verify_document(ssm, config, expected_document):
    document = ssm.describe_document(
        Name=config["document_name"],
        DocumentVersion=config["document_numeric_version"]
    )["Document"]
    if document.get("HashType") != "Sha256" or document.get(
            "Hash") != config["document_aws_sha256"]:
        raise ValueError("remote document hash differs from approved version")
    if document.get("DocumentVersion") != config["document_numeric_version"]:
        raise ValueError("remote numeric document version differs")
    content = ssm.get_document(
        Name=config["document_name"],
        DocumentVersion=config["document_numeric_version"],
        DocumentFormat="JSON"
    )
    if content.get("DocumentVersion"
                   ) != config["document_numeric_version"] or json.loads(
                       content["Content"]) != expected_document:
        raise ValueError(
            "remote SSM document content/version differs from reviewed document"
        )


def download_bounded(s3, bucket, key, destination, maximum, owner):
    response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
    body = response["Body"]
    partial = destination.with_name(destination.name + ".partial")
    try:
        length = response.get("ContentLength")
        if type(length) is not int or not 0 < length <= maximum:
            raise ValueError("artifact length outside allowed bounds")
        if destination.exists():
            raise ValueError("artifact destination already exists")
        fd = os.open(
            partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600
        )
        total = 0
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as target:
            while chunk := body.read(1048576):
                total += len(chunk)
                if total > maximum or total > length:
                    raise ValueError(
                        "artifact stream exceeded declared size or maximum"
                    )
                digest.update(chunk)
                target.write(chunk)
        if total != length:
            raise ValueError("artifact length changed during download")
        # Hard-link no-replace commit prevents overwriting an existing path.
        os.link(partial, destination, follow_symlinks=False)
        partial.unlink()
        return digest.hexdigest()
    finally:
        body.close()
        if partial.exists():
            partial.unlink()


def validate_receipt(receipt, run_id, attempt, source_sha, eligibility=None):
    expected = {"run_id": run_id, "attempt": attempt, "source_sha": source_sha}
    if eligibility is not None:
        expected.update({
            k: eligibility[k]
            for k in (
                "repository", "repository_id", "pull_request", "source_ref",
                "source_kind", "source_run_id", "source_run_head_sha",
                "source_job_id"
            )
        })
        expected.update({"schema_version": 1, "status": "success"})
    if receipt.get("qualified") is not True or any(
            receipt.get(k) != v for k, v in expected.items()):
        raise ValueError(
            "receipt is not qualified for the exact run/attempt/source"
        )
    for name, maximum in [("checkpoint", 1500000000), ("evidence", 499000000)]:
        size = receipt.get(name + "_bytes")
        digest = receipt.get(name + "_sha256")
        if type(size) is not int or not 0 < size <= maximum:
            raise ValueError("receipt artifact size outside allowed bounds")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}",
                                                           digest):
            raise ValueError("receipt artifact SHA256 is invalid")


def _submit_and_collect(
    config, saved, resolver, ssm, s3, run_id, attempt, output,
    expected_document, state, monotonic, sleep
):
    """Run the real protocol against supplied clients; fakes exercise it offline."""
    require_enabled(config)
    # Validate caller identity before even the read-only document requests.
    make_request(saved, config, run_id, attempt)
    verify_document(ssm, config, expected_document)
    # Recheck fork approval/current head after document/client setup, just before submission.
    current = resolver(saved["source_run_id"])
    if current != saved:
        raise ValueError("PR identity/approval changed before submission")
    request = make_request(current, config, run_id, attempt)
    parameters = encode_request(request, config)
    state.update(request=request, may_be_dispatched=True)
    sent = ssm.send_command(
        InstanceIds=[config["instance_id"]],
        DocumentName=config["document_name"],
        DocumentVersion=config["document_numeric_version"],
        DocumentHash=config["document_aws_sha256"],
        DocumentHashType="Sha256",
        TimeoutSeconds=600,
        Parameters=parameters
    )
    command_id = sent["Command"]["CommandId"]
    if not isinstance(command_id, str) or not re.fullmatch(r"[a-f0-9-]{36}",
                                                           command_id):
        raise ValueError("invalid SSM command ID")
    deadline = monotonic() + 22260
    terminal = None
    while monotonic() < deadline:
        try:
            response = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=config["instance_id"]
            )
        except ssm.exceptions.InvocationDoesNotExist:
            sleep(min(30, max(0, deadline - monotonic())))
            continue
        if response["Status"] not in {"Pending", "InProgress", "Delayed",
                                      "Cancelling"}:
            terminal = response["Status"]
            break
        sleep(min(30, max(0, deadline - monotonic())))
    output = Path(output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    bucket, prefix, owner = config["artifact_bucket"], request[
        "artifact_prefix"], config["aws_account"]
    download_bounded(
        s3, bucket, prefix + "receipt.json", output / "receipt.json", 1000000,
        owner
    )
    evidence_digest = download_bounded(
        s3, bucket, prefix + "evidence.tar", output / "evidence.tar",
        499000000, owner
    )
    receipt = json.loads((output / "receipt.json").read_text())
    if terminal != "Success":
        raise ValueError("SSM build command did not finish successfully")
    validate_receipt(receipt, run_id, attempt, current["source_sha"], current)
    if (evidence_digest != receipt["evidence_sha256"] or
        (output / "evidence.tar").stat().st_size != receipt["evidence_bytes"]):
        raise ValueError("evidence hash/size differs from exact-run receipt")
    digest = download_bounded(
        s3, bucket, prefix + "checkpoint.tar", output / "checkpoint.tar",
        1500000000, owner
    )
    if (digest != receipt["checkpoint_sha256"]
            or (output / "checkpoint.tar").stat().st_size
            != receipt["checkpoint_bytes"]):
        (output / "checkpoint.tar").unlink()
        raise ValueError("checkpoint hash/size differs from exact-run receipt")
    state["completed"] = True
    return {"command_id": command_id, "receipt": receipt}


def cancel_request(config, request, ssm):
    """Request exact-run cleanup via the same pinned document; no extra IAM API."""
    parameters = encode_request(request, config, operation="Cancel")
    return ssm.send_command(
        InstanceIds=[config["instance_id"]],
        DocumentName=config["document_name"],
        DocumentVersion=config["document_numeric_version"],
        DocumentHash=config["document_aws_sha256"],
        DocumentHashType="Sha256",
        TimeoutSeconds=60,
        Parameters=parameters
    )


def submit_and_collect(
    config,
    saved,
    resolver,
    ssm,
    s3,
    run_id,
    attempt,
    output,
    expected_document,
    monotonic=time.monotonic,
    sleep=time.sleep
):
    state = {}
    try:
        return _submit_and_collect(
            config, saved, resolver, ssm, s3, run_id, attempt, output,
            expected_document, state, monotonic, sleep
        )
    finally:
        # Also cancel after an uncertain SendCommand response: Build may have been
        # accepted. Host tombstones prevent a later-delivered Build from starting.
        if state.get("may_be_dispatched") and not state.get("completed"):
            try:
                cancel_request(config, state["request"], ssm)
            except BaseException:
                print(
                    "Cancellation delivery was not confirmed; independent host deadline remains the fallback.",
                    file=sys.stderr
                )


def interrupted(signum, frame):
    raise KeyboardInterrupt("orchestration interrupted")


def main():
    signal.signal(signal.SIGTERM, interrupted)
    config_path = os.environ.get(
        "CORALNPU_ACTIVATION_CONFIG", str(ROOT / "config.example.json")
    )
    config = json.loads(Path(config_path).read_text())
    require_enabled(config)
    saved = json.loads(
        (Path(os.environ["RUNNER_TEMP"]) / "coralnpu-request.json").read_text()
    )
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ[
        "GITHUB_RUN_ATTEMPT"]
    make_request(saved, config, run_id, attempt)
    token = oidc_token(config)
    ssm, s3 = aws_clients(config, token, run_id, attempt)
    del token
    document = json.loads(
        (ROOT / "ssm" / "CoralNpuFpgaCiBuild.json").read_text()
    )
    result = submit_and_collect(
        config, saved, resolve, ssm, s3, run_id, attempt,
        Path(os.environ["RUNNER_TEMP"]) / "coralnpu-artifacts", document
    )
    print(
        "Qualified checkpoint received for " + result["receipt"]["source_sha"]
    )


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        # No SDK error response, token, raw host stdout or bearer data in logs.
        raise SystemExit(
            "Controller stopped safely: " + str(exc)
            if isinstance(exc, ValueError) else "Controller stopped safely: " +
            type(exc).__name__
        ) from None
