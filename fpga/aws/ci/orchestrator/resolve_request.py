"""Read-only GitHub resolver for the trusted-main workflow proposal."""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

from eligibility import authorize, REPOSITORY


def api(path, paginate=False):
    argv = ["gh", "api", path]
    if paginate:
        argv += ["--paginate", "--slurp"]
    result = subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=90
    )
    return json.loads(result.stdout)


def resolve(run_id):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise ValueError("invalid source run id")
    prefix = "repos/" + REPOSITORY
    run = api(f"{prefix}/actions/runs/{run_id}")
    associated = run.get("pull_requests", [])
    if len(associated) != 1:
        raise ValueError(
            "source run has no unique associated PR; manual resolution is unqualified"
        )
    number = associated[0]["number"]
    if not isinstance(number, int) or number < 1:
        raise ValueError("invalid associated PR")
    pr = api(f"{prefix}/pulls/{number}")
    jobs = [
        j for page in api(
            f"{prefix}/actions/runs/{run_id}/jobs?filter=latest&per_page=100",
            True
        ) for j in page["jobs"]
    ]
    reviews = [
        r
        for page in api(f"{prefix}/pulls/{number}/reviews?per_page=100", True)
        for r in page
    ]
    logins = {pr["user"]["login"]} | {r["user"]["login"] for r in reviews}
    permissions = {}
    for login in sorted(logins):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}(?:\[bot\])?", login):
            raise ValueError("unexpected GitHub login format")
        # A 403/API error is a blocked gate, never inferred permission.
        response = api(
            f"{prefix}/collaborators/{quote(login, safe='')}/permission"
        )
        permissions[login] = canonical_role(response)
    return authorize(run, pr, jobs, reviews, permissions)


def canonical_role(response):
    # REST 'permission' collapses maintain -> write; role_name preserves it.
    role = response.get("role_name")
    if role not in {"none", "read", "triage", "write", "maintain", "admin"}:
        raise ValueError(
            "unknown/custom/missing repository role requires explicit review"
        )
    if role in {"maintain", "write"} and response.get("permission") != "write":
        raise ValueError("inconsistent collaborator permission response")
    if role == "admin" and response.get("permission") != "admin":
        raise ValueError("inconsistent administrator permission response")
    return role


def main():
    request = resolve(os.environ["SOURCE_RUN_ID"])
    path = Path(os.environ["RUNNER_TEMP"]) / "coralnpu-request.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(request, stream, sort_keys=True)
    print(f"Eligible exact PR head: {request['source_sha']}")


if __name__ == "__main__":
    main()
