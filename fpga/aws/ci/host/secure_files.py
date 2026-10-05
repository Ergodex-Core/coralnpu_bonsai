"""Offline file boundary helpers. Nothing in this module invokes host services.

Production trusted-path ownership defaults to root. An injected trusted directory
FD and owner UID permit local tests without trusting the ancestors of /tmp.
The caller owns supplied FDs; returned FDs belong to the caller.

Collection must run in the isolated nonroot collector, after the build/stager
cgroups are empty. Sealing must happen only after the collector is also stopped.
The host must never use these helpers to collect PR output as root. Deadlines are
checked between bounded syscalls; the independent cgroup deadline must also stop
a process stuck in filesystem I/O. No archive or PR-selected path is parsed.
"""
import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time

ARTIFACT_LIMITS = {
    "checkpoint.tar": 1_500_000_000,
    "evidence.tar": 499_000_000
}
TOTAL_LIMIT = 2_000_000_000
JSON_LIMIT = 1_000_000
CHUNK_LIMIT = 1_048_576
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class FileBoundaryError(ValueError):
    """An input or filesystem object did not meet the fixed trust boundary."""


def _uid(value):
    if type(value) is not int or value < 0:
        raise FileBoundaryError("invalid expected owner UID")


def _name(value):
    if (not isinstance(value, str) or value in {".", ".."}
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value)):
        raise FileBoundaryError("one literal filename is required")
    return value


def _identity(info):
    return info.st_dev, info.st_ino


def _artifact_limits(qualified):
    if type(qualified) is not bool:
        raise FileBoundaryError(
            "trusted qualification outcome must be boolean"
        )
    return ARTIFACT_LIMITS if qualified else {
        "evidence.tar": ARTIFACT_LIMITS["evidence.tar"]
    }


def _snapshot(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(fd, *, owner=None, private=False, identity=None):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise FileBoundaryError("directory FD required")
    if owner is not None:
        _uid(owner)
        if info.st_uid != owner or info.st_mode & 0o022:
            raise FileBoundaryError(
                "directory owner or write permissions are untrusted"
            )
    if private and info.st_mode & 0o077:
        raise FileBoundaryError("directory must be private")
    if identity is not None and _identity(info) != tuple(identity):
        raise FileBoundaryError("pinned directory identity changed")
    return info


def open_trusted_directory(
    path, *, dir_fd=None, expected_owner_uid=0, private=False
):
    """Walk each component with O_NOFOLLOW, checking the anchor and every parent.

    Without dir_fd, path must be absolute and traversal begins at /. With dir_fd,
    path must be relative; an empty path duplicates and validates that anchor.
    Symlinks, dot segments, repeated separators, and writable parents fail closed.
    """
    _uid(expected_owner_uid)
    if not isinstance(path, str) or "\x00" in path:
        raise FileBoundaryError("invalid directory path")
    if dir_fd is None:
        if not path.startswith("/"):
            raise FileBoundaryError("absolute trusted path required")
        components = path[1:].split("/") if path != "/" else []
        fd = os.open("/", DIRECTORY_FLAGS)
    else:
        if path.startswith("/"):
            raise FileBoundaryError("relative path required with anchor FD")
        components = path.split("/") if path else []
        fd = os.dup(dir_fd)
        os.set_inheritable(fd, False)
    try:
        if any(part in {"", ".", ".."} for part in components):
            raise FileBoundaryError("ambiguous directory component")
        _directory(fd, owner=expected_owner_uid)
        for part in components:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            try:
                _directory(child, owner=expected_owner_uid)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        _directory(fd, owner=expected_owner_uid, private=private)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _regular(info, *, limit, owner=None, private=False, allow_empty=False):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FileBoundaryError("single-link regular file required")
    if info.st_size > limit or info.st_size < (0 if allow_empty else 1):
        raise FileBoundaryError("file size outside fixed limit")
    if owner is not None and info.st_uid != owner:
        raise FileBoundaryError("file owner is untrusted")
    if private and info.st_mode & 0o077:
        raise FileBoundaryError("file must be private")


def _open_regular(directory_fd, name, *, limit, owner=None, private=False):
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    _regular(before, limit=limit, owner=owner, private=private)
    fd = os.open(name, READ_FLAGS, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        _regular(opened, limit=limit, owner=owner, private=private)
        if _snapshot(opened) != _snapshot(before):
            raise FileBoundaryError("file changed while opening")
        return fd, opened
    except BaseException:
        os.close(fd)
        raise


def _stable(
    directory_fd, name, fd, before, *, limit, owner=None, private=False
):
    for after in (os.fstat(fd), os.stat(name, dir_fd=directory_fd,
                                        follow_symlinks=False)):
        _regular(after, limit=limit, owner=owner, private=private)
        if _snapshot(after) != _snapshot(before):
            raise FileBoundaryError(
                "file identity or contents changed during operation"
            )


def _json_bound(max_bytes):
    if type(max_bytes) is not int or not 1 <= max_bytes <= JSON_LIMIT:
        raise FileBoundaryError("invalid JSON byte limit")


def _read_private_bytes(directory_fd, name, *, expected_owner_uid, max_bytes):
    _name(name)
    _json_bound(max_bytes)
    _directory(directory_fd, owner=expected_owner_uid, private=True)
    fd, before = _open_regular(
        directory_fd,
        name,
        limit=max_bytes,
        owner=expected_owner_uid,
        private=True
    )
    try:
        chunks = []
        remaining = before.st_size
        while remaining:
            block = os.read(fd, min(remaining, 65536))
            if not block:
                raise FileBoundaryError("short JSON read")
            chunks.append(block)
            remaining -= len(block)
        if os.read(fd, 1):
            raise FileBoundaryError("JSON grew during read")
        _stable(
            directory_fd,
            name,
            fd,
            before,
            limit=max_bytes,
            owner=expected_owner_uid,
            private=True
        )
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_private_json(
    dir_fd, name, *, expected_owner_uid=0, max_bytes=JSON_LIMIT
):
    """Read bounded JSON from a previously verified root-private directory FD."""
    payload = _read_private_bytes(
        dir_fd,
        name,
        expected_owner_uid=expected_owner_uid,
        max_bytes=max_bytes
    )
    return json.loads(payload)


def _write_all(fd, block, check=lambda: None):
    view = memoryview(block)
    while view:
        check()
        count = os.write(fd, view)
        if count <= 0:
            raise FileBoundaryError("short write")
        view = view[count:]
        check()


def _unlink_own(directory_fd, name, identity):
    """Never delete a replacement object during failure cleanup."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(current) == identity:
            os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def create_private_json(
    dir_fd, name, value, *, expected_owner_uid=0, max_bytes=JSON_LIMIT
):
    """Publish complete mode-0600 JSON without replacing an existing request.

    Return True on creation, False for an identical canonical-byte retry. A
    different existing request, unsafe file, or crash residue fails closed.
    Link/unlink publication requires a normal local POSIX filesystem; the short
    two-link publication interval can safely reject a concurrent reader.
    """
    _name(name)
    _json_bound(max_bytes)
    _directory(dir_fd, owner=expected_owner_uid, private=True)
    payload = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False
        ) + "\n"
    ).encode("ascii")
    if len(payload) > max_bytes:
        raise FileBoundaryError("JSON exceeds byte limit")
    temporary = ".json-" + secrets.token_hex(16)
    fd = os.open(temporary, CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    identity = _identity(os.fstat(fd))
    try:
        os.fchmod(fd, 0o600)
        _regular(
            os.fstat(fd),
            limit=max_bytes,
            owner=expected_owner_uid,
            private=True,
            allow_empty=True
        )
        _write_all(fd, payload)
        os.fsync(fd)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False
            )
        except FileExistsError:
            existing = _read_private_bytes(
                dir_fd,
                name,
                expected_owner_uid=expected_owner_uid,
                max_bytes=max_bytes
            )
            if existing != payload:
                raise FileBoundaryError(
                    "request identity already has different JSON"
                )
            return False
        _unlink_own(dir_fd, temporary, identity)
        os.fsync(dir_fd)
        return True
    finally:
        os.close(fd)
        _unlink_own(dir_fd, temporary, identity)


def collect_artifacts(
    source_dir_fd,
    inbox_dir_fd,
    *,
    deadline,
    clock=time.monotonic,
    chunk_size=CHUNK_LIMIT,
    qualified=True,
    expected_source_identity=None,
    expected_inbox_identity=None
):
    """Copy fixed opaque files into a new, same-filesystem inbox.

    The caller supplies pinned FDs and proves all untrusted writers have stopped.
    Ownership and process isolation are the caller's responsibility, since this
    routine also supports dependency-injected offline tests. Files created here
    are private and exclusive; nothing already in the inbox is overwritten.
    Only the trusted supervisor supplies qualified; failure copies evidence only.
    All sources are reread once after copying the full set, under the same byte
    limits and deadline, to detect content rewrites even when stat timestamps do
    not change. This consistency check does not replace stopping all writers.
    """
    if (type(deadline) not in (int, float) or not math.isfinite(deadline)
            or type(chunk_size) is not int
            or not 1 <= chunk_size <= CHUNK_LIMIT):
        raise FileBoundaryError(
            "finite deadline and bounded chunk size required"
        )

    def check():
        now = clock()
        if not math.isfinite(now) or now >= deadline:
            raise TimeoutError("artifact collection deadline reached")

    check()
    limits = _artifact_limits(qualified)
    source = _directory(source_dir_fd, identity=expected_source_identity)
    inbox = _directory(
        inbox_dir_fd, private=True, identity=expected_inbox_identity
    )
    if source.st_dev != inbox.st_dev or _identity(source) == _identity(inbox):
        raise FileBoundaryError(
            "distinct directories on the bounded filesystem required"
        )
    sources = []
    created = []
    result = {}
    succeeded = False
    try:
        for name, limit in limits.items():
            check()
            fd, info = _open_regular(source_dir_fd, name, limit=limit)
            sources.append((name, limit, fd, info))
            if info.st_dev != source.st_dev:
                raise FileBoundaryError("source escaped bounded filesystem")
        if sum(item[3].st_size for item in sources) >= TOTAL_LIMIT:
            raise FileBoundaryError("combined artifact limit exceeded")
        for name, limit, source_fd, before in sources:
            check()
            target_fd = os.open(name, CREATE_FLAGS, 0o600, dir_fd=inbox_dir_fd)
            target_identity = _identity(os.fstat(target_fd))
            created.append((name, target_identity))
            try:
                os.fchmod(target_fd, 0o600)
                target_before = os.fstat(target_fd)
                _regular(
                    target_before, limit=limit, private=True, allow_empty=True
                )
                if target_before.st_dev != inbox.st_dev:
                    raise FileBoundaryError(
                        "destination escaped bounded filesystem"
                    )
                digest = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    check()
                    block = os.read(source_fd, min(chunk_size, remaining))
                    check()
                    if not block:
                        raise FileBoundaryError("source shortened during copy")
                    _write_all(target_fd, block, check)
                    digest.update(block)
                    remaining -= len(block)
                if os.read(source_fd, 1):
                    raise FileBoundaryError("source grew during copy")
                check()
                _stable(source_dir_fd, name, source_fd, before, limit=limit)
                os.fsync(target_fd)
                check()
                target_after = os.fstat(target_fd)
                _regular(target_after, limit=limit, private=True)
                if (_identity(target_after) != target_identity
                        or target_after.st_size != before.st_size):
                    raise FileBoundaryError(
                        "destination identity or size changed"
                    )
                _stable(
                    inbox_dir_fd,
                    name,
                    target_fd,
                    target_after,
                    limit=limit,
                    private=True
                )
                result[name] = {
                    "bytes": target_after.st_size,
                    "sha256": digest.hexdigest(),
                    "device": target_after.st_dev,
                    "inode": target_after.st_ino
                }
            finally:
                os.close(target_fd)
        # Rehash the whole set after copying every artifact. Metadata alone
        # can miss a same-size rewrite within one filesystem timestamp tick,
        # and an immediate per-file rehash misses earlier-file changes while
        # later files copy. One extra bounded pass is allowed; no retry loops.
        for name, limit, fd, before in sources:
            check()
            _stable(source_dir_fd, name, fd, before, limit=limit)
            os.lseek(fd, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                check()
                block = os.read(fd, min(chunk_size, remaining))
                check()
                if not block:
                    raise FileBoundaryError("source shortened during recheck")
                digest.update(block)
                remaining -= len(block)
            check()
            extra = os.read(fd, 1)
            check()
            if extra or digest.hexdigest() != result[name]["sha256"]:
                raise FileBoundaryError("source contents changed during copy")
            _stable(source_dir_fd, name, fd, before, limit=limit)
        _directory(source_dir_fd, identity=_identity(source))
        _directory(inbox_dir_fd, private=True, identity=_identity(inbox))
        os.fsync(inbox_dir_fd)
        check()
        succeeded = True
        return result
    finally:
        for _, _, fd, _ in sources:
            os.close(fd)
        if not succeeded:
            for name, identity in created:
                _unlink_own(inbox_dir_fd, name, identity)


def seal_inbox(
    dir_fd, expected_guest_uid, expected_root_uid=0, *, qualified=True
):
    """Seal fixed artifacts only after every guest/collector process is gone.

    No chown/chmod follows a pathname. Partial sealing may be retried; a different
    owner or inode, unsafe permissions, or invalid artifact aborts the operation.
    The caller must hold the run lock and verify the directory's pinned identity.
    """
    _uid(expected_guest_uid)
    _uid(expected_root_uid)
    limits = _artifact_limits(qualified)
    directory = _directory(dir_fd, private=True)
    if directory.st_uid not in {expected_guest_uid, expected_root_uid}:
        raise FileBoundaryError("unexpected inbox owner")
    files = []
    try:
        for name, limit in limits.items():
            fd, info = _open_regular(dir_fd, name, limit=limit, private=True)
            files.append((name, limit, fd, info))
            if info.st_uid not in {expected_guest_uid, expected_root_uid}:
                raise FileBoundaryError("unexpected artifact owner")
            if info.st_dev != directory.st_dev:
                raise FileBoundaryError("artifact escaped inbox filesystem")
        for name, limit, fd, before in files:
            _stable(dir_fd, name, fd, before, limit=limit, private=True)
            os.fchown(fd, expected_root_uid, -1)
            os.fchmod(fd, 0o400)
            os.fsync(fd)
        os.fchown(dir_fd, expected_root_uid, -1)
        os.fchmod(dir_fd, 0o500)
        os.fsync(dir_fd)
    finally:
        for _, _, fd, _ in files:
            os.close(fd)
    return verify_sealed_inbox(
        dir_fd, expected_root_uid=expected_root_uid, qualified=qualified
    )


def verify_sealed_inbox(dir_fd, *, expected_root_uid=0, qualified=True):
    """Return fixed file identities; upload must reopen/recheck its own FDs."""
    limits = _artifact_limits(qualified)
    directory = _directory(dir_fd, owner=expected_root_uid, private=True)
    if stat.S_IMODE(directory.st_mode) != 0o500:
        raise FileBoundaryError("inbox is not sealed read-only")
    result = {}
    for name, limit in limits.items():
        fd, info = _open_regular(
            dir_fd, name, limit=limit, owner=expected_root_uid, private=True
        )
        try:
            if stat.S_IMODE(info.st_mode
                            ) != 0o400 or info.st_dev != directory.st_dev:
                raise FileBoundaryError(
                    "artifact is not sealed on bounded filesystem"
                )
            _stable(
                dir_fd,
                name,
                fd,
                info,
                limit=limit,
                owner=expected_root_uid,
                private=True
            )
            result[name] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "bytes": info.st_size
            }
        finally:
            os.close(fd)
    return result


def main(argv=None):
    """Fixed immutable-image collector entrypoint; accepts no paths or filenames."""
    parser = argparse.ArgumentParser(
        description="Copy fixed opaque CI artifacts"
    )
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--evidence-only", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        raise FileBoundaryError("collector entrypoint must run as nonroot")
    work_fd = os.open("/work", DIRECTORY_FLAGS)
    try:
        source_fd = os.open("output", DIRECTORY_FLAGS, dir_fd=work_fd)
        try:
            inbox_fd = os.open("/inbox", DIRECTORY_FLAGS)
            try:
                result = collect_artifacts(
                    source_fd,
                    inbox_fd,
                    deadline=args.deadline,
                    qualified=not args.evidence_only
                )
                print(json.dumps(result, sort_keys=True))
            finally:
                os.close(inbox_fd)
        finally:
            os.close(source_fd)
    finally:
        os.close(work_fd)


if __name__ == "__main__":
    main()
