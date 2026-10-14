"""Root-owned durable admission counter, tested locally; not installed on host.

Caller must ALSO hold the shared FPGA lock for the lifetime of the entire job.
Neither GitHub variables nor SSM parameters may set the pilot id or limit.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid


class PilotState:

    def __init__(self, directory, pilot_id, require_root=True):
        self.directory = Path(directory)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError(
                "state directory must be a private real directory"
            )
        expected_uid = 0 if require_root else os.getuid()
        if info.st_uid != expected_uid:
            raise ValueError("state owner is not trusted")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", pilot_id):
            raise ValueError("invalid pilot id")
        self.pilot_id = pilot_id
        self.path = self.directory / (pilot_id + ".json")
        self.lock = self.directory / (pilot_id + ".lock")

    def _transaction(self, mutation):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                data_fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(data_fd) as data_file:
                    state = json.load(data_file)
            else:
                state = {"pilot": self.pilot_id, "runs": {}}
            if state.get("pilot") != self.pilot_id:
                raise ValueError("pilot state mismatch")
            result = mutation(state)
            temp = self.directory / (".state-" + uuid.uuid4().hex)
            temp_fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(temp_fd, "w") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return result

    def admit(self, run_id, attempt, source_sha):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
            raise ValueError("invalid run id")
        if not re.fullmatch(r"[1-9][0-9]{0,5}", str(attempt)):
            raise ValueError("invalid attempt")
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("invalid SHA")
        key = f"{run_id}-{attempt}"

        def mutation(state):
            runs = state["runs"]
            if key in runs:
                if runs[key]["sha"] != source_sha:
                    raise ValueError("run identity reused with another SHA")
                return {"launch": False, "run": dict(runs[key])}
            if len(runs) >= 3:
                raise ValueError("three-build pilot exhausted")
            if any(r["status"] == "running" for r in runs.values()):
                raise ValueError(
                    "another run is active; fail closed on stale state"
                )
            now = time.time()
            runs[key] = {
                "sha": source_sha,
                "status": "running",
                "admitted": now,
                "deadline": now + 21600
            }
            return {"launch": True, "run": dict(runs[key])}

        return self._transaction(mutation)

    def finish(self, run_id, attempt, status):
        if status not in {"success", "failed", "timed_out", "cancelled"}:
            raise ValueError("invalid terminal status")
        key = f"{run_id}-{attempt}"

        def mutation(state):
            run = state["runs"][key]
            if run["status"] == "running":
                run["status"] = status
                run["finished"] = time.time()
            elif run["status"] != status:
                raise ValueError("cannot rewrite a terminal run outcome")
            return dict(run)

        return self._transaction(mutation)
