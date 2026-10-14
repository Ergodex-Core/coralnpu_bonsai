"""Strict nonsecret wire protocol shared by controller and fixed host launcher."""
import base64
import hashlib
import hmac
import json
import re

REQUEST_FIELDS = {
    "eligibility", "run_id", "attempt", "source_sha", "artifact_prefix"
}
ELIGIBILITY_FIELDS = {
    "repository", "repository_id", "pull_request", "source_sha", "source_ref",
    "source_kind", "source_run_id", "source_run_head_sha", "source_job_id",
    "eligibility_reason", "exact_sha_approvers"
}
ENV_FIELDS = {
    "Operation", "SourceSha", "RunId", "RunAttempt", "RequestSha256",
    "RequestBase64"
}


def require(value, message):
    if not value:
        raise ValueError(message)


def number(value, maximum=10**20 - 1):
    return type(value) is int and 0 < value <= maximum


def text_matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_request(request, config):
    require(
        isinstance(request, dict) and set(request) == REQUEST_FIELDS,
        "unexpected request fields"
    )
    require(
        text_matches(request["run_id"], r"[1-9][0-9]{0,19}"), "invalid run ID"
    )
    require(
        text_matches(request["attempt"], r"[1-9][0-9]{0,5}"),
        "invalid run attempt"
    )
    require(
        text_matches(request["source_sha"], r"[0-9a-f]{40}"),
        "invalid source SHA"
    )
    prefix = config["artifact_prefix"]
    require(
        text_matches(prefix, r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*/"),
        "invalid configured artifact prefix"
    )
    require(
        request["artifact_prefix"] ==
        f"{prefix}{request['run_id']}/{request['attempt']}/",
        "artifact destination differs from exact run prefix"
    )
    proof = request["eligibility"]
    require(
        isinstance(proof, dict) and set(proof) == ELIGIBILITY_FIELDS,
        "unexpected eligibility fields"
    )
    require(
        proof["repository"] == config["repository"]
        and proof["repository_id"] == config["repository_id"],
        "repository identity differs from trusted config"
    )
    require(
        number(proof["repository_id"])
        and number(proof["pull_request"], 10**10 - 1),
        "invalid repository or PR ID"
    )
    require(
        number(proof["source_run_id"]) and number(proof["source_job_id"]),
        "invalid source run/job ID"
    )
    require(
        proof["source_sha"] == request["source_sha"] ==
        proof["source_run_head_sha"], "source SHA mismatch"
    )
    require(
        proof["source_ref"] == f"refs/pull/{proof['pull_request']}/head",
        "source ref is not the fixed PR head ref"
    )
    require(
        proof["source_kind"] == "pr_head_exact_sha", "unsupported source kind"
    )
    require(
        proof["eligibility_reason"] in {
            "same-repository-author-with-current-write-permission",
            "current-maintainer-review-of-exact-head-sha"
        }, "invalid eligibility reason"
    )
    approvers = proof["exact_sha_approvers"]
    require(
        isinstance(approvers, list) and len(approvers) <= 100 and all(
            text_matches(x, r"[A-Za-z0-9-]{1,100}(?:\[bot\])?")
            for x in approvers
        ), "invalid approver list"
    )
    require(len(set(approvers)) == len(approvers), "duplicate approvers")
    require(
        proof["eligibility_reason"]
        != "current-maintainer-review-of-exact-head-sha" or bool(approvers),
        "exact-SHA approval is missing"
    )
    return request


def make_request(proof, config, run_id, attempt):
    return validate_request({
        "eligibility":
        proof,
        "run_id":
        run_id,
        "attempt":
        attempt,
        "source_sha":
        proof["source_sha"],
        "artifact_prefix":
        f"{config['artifact_prefix']}{run_id}/{attempt}/"
    }, config)


def encode_request(request, config, operation="Build"):
    validate_request(request, config)
    require(operation in {"Build", "Cancel"}, "unsupported operation")
    payload = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    require(len(payload) <= 9000, "request exceeds maximum decoded size")
    encoded = base64.b64encode(payload).decode("ascii")
    require(len(encoded) <= 12000, "request exceeds SSM parameter limit")
    return {
        "Operation": [operation],
        "SourceSha": [request["source_sha"]],
        "RunId": [request["run_id"]],
        "RunAttempt": [request["attempt"]],
        "RequestSha256": [hashlib.sha256(payload).hexdigest()],
        "RequestBase64": [encoded]
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def decode_ssm_environment(environment, config):
    values = {key: environment.get("SSM_" + key) for key in ENV_FIELDS}
    require(
        all(isinstance(value, str) for value in values.values()),
        "SSM ENV_VAR fields missing"
    )
    require(
        values["Operation"] in {"Build", "Cancel"}, "unsupported SSM operation"
    )
    require(
        text_matches(values["RequestSha256"], r"[0-9a-f]{64}"),
        "invalid request checksum"
    )
    encoded = values["RequestBase64"]
    require(len(encoded) <= 12000, "SSM request exceeds maximum size")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64 request") from exc
    require(len(payload) <= 9000, "decoded request exceeds maximum size")
    require(
        hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(), values["RequestSha256"]
        ), "request checksum differs"
    )
    request = json.loads(payload, object_pairs_hook=_unique_object)
    validate_request(request, config)
    require(
        request["source_sha"] == values["SourceSha"]
        and request["run_id"] == values["RunId"]
        and request["attempt"] == values["RunAttempt"],
        "SSM identity fields differ from request"
    )
    return request
