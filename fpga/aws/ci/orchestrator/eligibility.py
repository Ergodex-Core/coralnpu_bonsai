"""Pure eligibility gate. Inputs must come from fresh GitHub API responses.

This module performs no network or AWS operations. See workflow proposal for the
required API reads. It never treats PR-provided artifacts as authorization.
"""
import re
from datetime import datetime

REPOSITORY = "Ergodex-Core/coralnpu_bonsai"
REPOSITORY_ID = 1405746522
SOURCE_PATH = ".github/workflows/fpga.yml"
SOURCE_JOB = "FPGA source and host-test checks"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def authorize(run, pr, jobs, reviews, permissions):
    """Return a minimal request after checking current identity and approval.

permissions maps API-verified login -> current permission, not author_association.
run is fetched using the workflow_run ID from GitHub, pr is fetched afresh, and
jobs/reviews must include every page. Fail closed on API errors or ambiguity.
The build is the PR HEAD, not the moving merge ref. source-check SHA is recorded
separately; the later controller must not silently substitute a merge commit.
"""
    require(
        run.get("repository", {}).get("id") == REPOSITORY_ID,
        "wrong source-run repository"
    )
    require(
        run.get("path", "").split("@")[0] == SOURCE_PATH,
        "wrong source workflow"
    )
    require(
        run.get("event") == "pull_request"
        and run.get("status") == "completed",
        "source run must be a completed PR run"
    )
    require(
        pr.get("state") == "open" and not pr.get("draft"),
        "PR is closed or draft"
    )
    require(
        pr.get("base", {}).get("repo", {}).get("id") == REPOSITORY_ID,
        "wrong PR base repository"
    )
    require(pr.get("base", {}).get("ref") == "main", "wrong PR base branch")
    head = pr.get("head", {})
    sha = head.get("sha", "")
    require(
        re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "invalid source SHA"
    )
    associations = run.get("pull_requests", [])
    require(
        len(associations) == 1, "missing or ambiguous run-to-PR association"
    )
    association = associations[0]
    require(
        association.get("number") == pr.get("number"), "wrong associated PR"
    )
    require(
        association.get("head", {}).get("sha") == sha,
        "PR changed since the source run; require a new run"
    )
    require(
        run.get("head_sha") == sha,
        "unqualified workflow-run SHA semantics; do not guess merge/head identity"
    )
    source_jobs = [j for j in jobs if j.get("name") == SOURCE_JOB]
    require(
        len(source_jobs) == 1
        and source_jobs[0].get("conclusion") == "success",
        "source checks did not pass"
    )
    actor = pr.get("user", {}).get("login", "")
    same_repo = head.get("repo", {}).get("id") == REPOSITORY_ID
    trusted_author = permissions.get(actor) in {"write", "maintain", "admin"}
    latest = {}

    def submission_order(review):
        submitted = review.get("submitted_at")
        if not submitted:
            require(
                review.get("state") == "PENDING",
                "submitted review lacks timestamp"
            )
            return (float("inf"), review.get("id", 0))
        stamp = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "review timestamp lacks timezone")
        return (stamp.timestamp(), review.get("id", 0))

    for review in sorted(reviews, key=submission_order):
        reviewer = review.get("user", {}).get("login", "")
        if permissions.get(reviewer) in {"maintain", "admin"}:
            state = review.get("state")
            if state in {"APPROVED", "CHANGES_REQUESTED"}:
                latest[reviewer] = review
            elif state == "DISMISSED":
                # Conservative: dismissal clears a prior approval but cannot
                # erase an earlier active changes-requested opinion.
                if latest.get(reviewer,
                              {}).get("state") != "CHANGES_REQUESTED":
                    latest.pop(reviewer, None)
            elif state not in {"COMMENTED", "PENDING"}:
                raise ValueError("unknown review state")
    require(
        not any(
            r.get("state") == "CHANGES_REQUESTED" for r in latest.values()
        ), "maintainer has requested changes"
    )
    approvers = sorted(
        login for login, r in latest.items()
        if r.get("state") == "APPROVED" and r.get("commit_id") == sha
    )
    if same_repo and trusted_author:
        reason = "same-repository-author-with-current-write-permission"
    else:
        require(
            bool(approvers),
            "maintainer approval of this exact SHA is required"
        )
        reason = "current-maintainer-review-of-exact-head-sha"
    return {
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "pull_request": pr["number"],
        "source_sha": sha,
        "source_ref": f"refs/pull/{pr['number']}/head",
        "source_kind": "pr_head_exact_sha",
        "source_run_id": run["id"],
        "source_run_head_sha": run["head_sha"],
        "source_job_id": source_jobs[0]["id"],
        "eligibility_reason": reason,
        "exact_sha_approvers": approvers,
    }
