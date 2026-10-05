import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "orchestrator"), str(ROOT / "host")]
from eligibility import authorize, REPOSITORY_ID, SOURCE_PATH, SOURCE_JOB
from resolve_request import canonical_role
from pilot_state import PilotState
from container_argv import build_argv
from validate_bundle import activation_errors
from controller import validate_receipt

SHA = "a" * 40


def fixture(fork=False):
    pr = {
        "number": 1,
        "state": "open",
        "draft": False,
        "user": {
            "login": "author"
        },
        "base": {
            "ref": "main",
            "repo": {
                "id": REPOSITORY_ID
            }
        },
        "head": {
            "sha": SHA,
            "repo": {
                "id": 5 if fork else REPOSITORY_ID
            }
        }
    }
    run = {
        "id": 123,
        "repository": {
            "id": REPOSITORY_ID
        },
        "path": SOURCE_PATH,
        "event": "pull_request",
        "status": "completed",
        "head_sha": SHA,
        "pull_requests": [{
            "number": 1,
            "head": {
                "sha": SHA
            }
        }]
    }
    jobs = [{"id": 9, "name": SOURCE_JOB, "conclusion": "success"}]
    return run, pr, jobs


def review(state, day, ident=None, sha=SHA):
    return {
        "id": ident if ident is not None else day,
        "state": state,
        "submitted_at": f"2026-10-{day:02d}T12:00:00Z",
        "commit_id": sha,
        "user": {
            "login": "maintainer"
        }
    }


class EligibilityTests(unittest.TestCase):

    def call(self, fork=False, reviews=(), **changes):
        run, pr, jobs = fixture(fork)
        for key, value in changes.items():
            if key == "head":
                pr["head"]["sha"] = value
            elif key == "event":
                run["event"] = value
            elif key == "jobs":
                jobs = value
        return authorize(
            run, pr, jobs, list(reviews), {
                "author": "write",
                "maintainer": "maintain"
            }
        )

    def test_same_repo_current_writer(self):
        self.assertEqual(self.call()["source_sha"], SHA)

    def test_failed_overall_readiness_still_accepts_successful_source_job(
        self
    ):
        run, pr, jobs = fixture()
        run["conclusion"] = "failure"
        self.assertEqual(
            authorize(run, pr, jobs, [], {"author": "write"})["source_sha"],
            SHA
        )

    def test_fork_requires_exact_approval(self):
        with self.assertRaises(ValueError):
            self.call(True)
        self.assertEqual(
            self.call(True, [review("APPROVED", 1)])["exact_sha_approvers"],
            ["maintainer"]
        )

    def test_stale_approval_denied(self):
        with self.assertRaises(ValueError):
            self.call(True, [review("APPROVED", 1, sha="b" * 40)])

    def test_changed_head_denied(self):
        with self.assertRaises(ValueError):
            self.call(head="b" * 40)

    def test_comment_does_not_erase_veto(self):
        with self.assertRaises(ValueError):
            self.call(
                False,
                [review("CHANGES_REQUESTED", 1),
                 review("COMMENTED", 2)]
            )

    def test_comment_does_not_erase_approval(self):
        self.call(True, [review("APPROVED", 1), review("COMMENTED", 2)])

    def test_order_by_submission_not_creation_id(self):
        self.call(
            True, [
                review("CHANGES_REQUESTED", 1, ident=200),
                review("APPROVED", 2, ident=100)
            ]
        )

    def test_dismissal_revokes_approval(self):
        with self.assertRaises(ValueError):
            self.call(True, [review("APPROVED", 1), review("DISMISSED", 2)])

    def test_wrong_event_and_failed_checks(self):
        with self.assertRaises(ValueError):
            self.call(event="pull_request_target")
        with self.assertRaises(ValueError):
            self.call(
                jobs=[{
                    "id": 9,
                    "name": SOURCE_JOB,
                    "conclusion": "skipped"
                }]
            )

    def test_maintain_role_not_collapsed_to_write(self):
        self.assertEqual(
            canonical_role({
                "permission": "write",
                "role_name": "maintain"
            }), "maintain"
        )
        with self.assertRaises(ValueError):
            canonical_role({"permission": "write", "role_name": "custom-role"})
        with self.assertRaises(ValueError):
            canonical_role({"permission": "write"})


class PilotTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = PilotState(self.temp.name, "pilot-1", require_root=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_three_then_stop_and_persist(self):
        for run in range(1, 4):
            self.assertTrue(self.state.admit(run, 1, SHA)["launch"])
            self.state.finish(run, 1, "failed")
        reopened = PilotState(self.temp.name, "pilot-1", require_root=False)
        with self.assertRaises(ValueError):
            reopened.admit(4, 1, SHA)

    def test_retry_is_idempotent_and_sha_cannot_change(self):
        self.state.admit(1, 1, SHA)
        self.assertFalse(self.state.admit(1, 1, SHA)["launch"])
        with self.assertRaises(ValueError):
            self.state.admit(1, 1, "b" * 40)

    def test_active_run_blocks_parallel_and_new_attempt(self):
        self.state.admit(1, 1, SHA)
        with self.assertRaises(ValueError):
            self.state.admit(1, 2, SHA)
        with self.assertRaises(ValueError):
            self.state.admit(2, 1, SHA)

    def test_private_directory_and_symlink_protection(self):
        os.chmod(self.temp.name, 0o755)
        with self.assertRaises(ValueError):
            PilotState(self.temp.name, "pilot-1", require_root=False)
        os.chmod(self.temp.name, 0o700)
        target = Path(self.temp.name) / "target"
        target.write_text("secret sentinel")
        (Path(self.temp.name) / "pilot-1.json").symlink_to(target)
        with self.assertRaises(OSError):
            self.state.admit(1, 1, SHA)
        self.assertEqual(target.read_text(), "secret sentinel")


class BundleTests(unittest.TestCase):

    def test_receipt_cannot_substitute_another_run_or_attempt(self):
        receipt = {
            "run_id": "123",
            "attempt": "1",
            "source_sha": SHA,
            "qualified": True,
            "checkpoint_bytes": 1234,
            "evidence_bytes": 456,
            "checkpoint_sha256": "b" * 64,
            "evidence_sha256": "c" * 64
        }
        validate_receipt(receipt, "123", "1", SHA)
        for changed in [{"run_id": "124"}, {"attempt": "2"}, {"source_sha":
                                                              "d" * 40},
                        {"qualified": False}, {"checkpoint_bytes": True},
                        {"evidence_bytes": 499000001}, {"checkpoint_sha256":
                                                        "bad"}]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_receipt(receipt | changed, "123", "1", SHA)

    def test_config_fails_closed(self):
        config = json.loads((ROOT / "config.example.json").read_text())
        self.assertFalse(config["enabled"])
        errors = activation_errors(config)
        self.assertFalse(config["artifact_transport_scope_approved"])
        self.assertTrue(any("independent_cgroup_cleanup" in x for x in errors))
        self.assertTrue(any("bounded_disk" in x for x in errors))

    def test_ssm_has_no_shell_interpolation_or_bearer_field(self):
        doc = json.loads((ROOT / "ssm/CoralNpuFpgaCiBuild.json").read_text())
        self.assertTrue(
            all(
                p["interpolationType"] == "ENV_VAR"
                for p in doc["parameters"].values()
            )
        )
        commands = doc["mainSteps"][0]["inputs"]["runCommand"]
        self.assertFalse(any("{{" in command for command in commands))
        self.assertNotIn("Upload", " ".join(doc["parameters"]))
        source = (ROOT / "orchestrator/controller.py").read_text()
        self.assertNotIn("generate_presigned", source)

    def test_container_arguments_are_restricted(self):
        args = build_argv(
            "registry.example/coral@sha256:" + "c" * 64, 2100, 2100, "123-1",
            "/srv/coralnpu-ci/runs/123-1/work", []
        )
        for flag in ["--network=none", "--read-only", "--cap-drop=ALL",
                     "--cgroup-parent=coralnpu-ci.slice"]:
            self.assertIn(flag, args)
        self.assertNotIn("--privileged", args)
        with self.assertRaises(ValueError):
            build_argv(
                "coral:latest", 2100, 2100, "123-1",
                "/srv/coralnpu-ci/runs/123-1/work", []
            )
        with self.assertRaises(ValueError):
            build_argv(
                "coral@sha256:" + "c" * 64, 0, 0, "123-1",
                "/srv/coralnpu-ci/runs/123-1/work", []
            )
        with self.assertRaises(ValueError):
            build_argv(
                "coral@sha256:" + "c" * 64, 2100, 2100, "123-1",
                "/srv/coralnpu-ci/runs/..", []
            )


if __name__ == "__main__":
    unittest.main()
