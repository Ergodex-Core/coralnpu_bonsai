"""Verify reviewed, root-owned tool snapshots before exposing them to a guest.

The proof is /etc/coralnpu-ci/mounts/<qualification_sha256>.json, mode 0600.
Its exact bytes are SHA256-pinned in root configuration. Schema version 1 has
exact keys: schema_version, source, target, identity, no_secrets, entries.
identity is {path, device, inode}. entries maps canonical relative paths ("."
for the root) to {kind, device, inode, uid, gid, mode}; file entries also contain
bytes and sha256, and symlink entries contain target. Modes are integer POSIX
permission bits. Every entry, including directories and symlinks, is enumerated.

no_secrets is a prior operator review attestation, not something this code can
infer. Unknown paths and unsafe metadata are rejected BEFORE reading tool file
contents. This verifier never creates an attestation, seals/chowns a tree, follows
an unreviewed symlink, executes a tool, or reads an unlisted file's contents.
Only reviewed snapshot files are hashed; all descendants must remain root-owned
and non-writable by group/others. Root administrators are trusted and must honor
the host scheduling lock; verification cannot constrain subsequent root edits.
"""
import hashlib
import json
import math
import os
import re
import stat
import time

from secure_files import open_trusted_directory, DIRECTORY_FLAGS, READ_FLAGS

PROOF_DIRECTORY = "/etc/coralnpu-ci/mounts"
TARGETS = {
    "/opt/amd", "/opt/Xilinx", "/opt/coral-tools", "/opt/aws-fpga", "/deps"
}
MANIFEST_LIMIT = 128 * 1024**2
ENTRY_LIMIT = 500_000
TOTAL_BYTES_LIMIT = 2 * 1024**4
CHUNK_BYTES = 1024**2
DEPTH_LIMIT = 128


class InventoryError(ValueError):
    """A snapshot no longer matches the root-reviewed mount qualification."""


class PosixFs:
    """Read-only FD-based operations; replace this adapter in offline tests."""

    def open_proof_directory(self, path, *, owner):
        return open_trusted_directory(
            path, expected_owner_uid=owner, private=True
        )

    def open_source(self, path, *, owner):
        return open_trusted_directory(path, expected_owner_uid=owner)

    def open_directory(self, directory_fd, name):
        return os.open(name, DIRECTORY_FLAGS, dir_fd=directory_fd)

    def open_file(self, directory_fd, name):
        return os.open(name, READ_FLAGS, dir_fd=directory_fd)

    def lstat(self, directory_fd, name):
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

    def readlink(self, directory_fd, name):
        return os.readlink(name, dir_fd=directory_fd)

    def names(self, directory_fd):
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                yield entry.name

    fstat = staticmethod(os.fstat)
    read = staticmethod(os.read)
    close = staticmethod(os.close)
    duplicate = staticmethod(os.dup)


def _absolute(value):
    if (not isinstance(value, str) or not value.startswith("/")
            or len(value) > 4096 or any(part in {"", ".", ".."}
                                        for part in value[1:].split("/"))
            or any(ord(character) < 32 for character in value)):
        raise InventoryError("canonical absolute snapshot path required")
    return value


def _relative(value):
    if value == ".":
        return value
    if (not isinstance(value, str) or len(value) > 4096
            or len(value.split("/")) > DEPTH_LIMIT
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(character) < 32 for character in value)):
        raise InventoryError("canonical relative inventory path required")
    return value


def _number(value, *, minimum=0, maximum=None):
    return type(value) is int and value >= minimum and (
        maximum is None or value <= maximum
    )


def _signature(info):
    return (
        info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    )


def _metadata(info, owner, device):
    if info.st_uid != owner or info.st_dev != device:
        raise InventoryError(
            "snapshot descendant owner or filesystem is untrusted"
        )
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    elif stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    else:
        raise InventoryError("special snapshot file is forbidden")
    # POSIX symlink permission bits are not access controls. Its parent is
    # root-owned/non-writable and every resolved destination is checked below.
    if kind != "symlink" and info.st_mode & 0o022:
        raise InventoryError(
            "snapshot descendant is writable by group or others"
        )
    if kind != "directory" and info.st_nlink != 1:
        raise InventoryError("hardlinked snapshot leaf is forbidden")
    result = {
        "kind": kind,
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": stat.S_IMODE(info.st_mode)
    }
    if kind == "file":
        if not 0 <= info.st_size <= TOTAL_BYTES_LIMIT:
            raise InventoryError("snapshot file exceeds byte bound")
        result["bytes"] = info.st_size
    return result


def _strict_json(raw):

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InventoryError("duplicate manifest JSON key")
            result[key] = value
        return result

    def constant(_):
        raise InventoryError("nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _schema(proof, mount, owner, check):
    keys = {
        "schema_version", "source", "target", "identity", "no_secrets",
        "entries"
    }
    if (not isinstance(proof, dict) or set(proof) != keys
            or type(proof["schema_version"]) is not int
            or proof["schema_version"] != 1
            or proof["source"] != mount["source"]
            or proof["target"] != mount["target"]
            or proof["no_secrets"] is not True):
        raise InventoryError(
            "mount proof schema, source, target, or prior secrets review differs"
        )
    identity = proof["identity"]
    if (not isinstance(identity, dict)
            or set(identity) != {"path", "device", "inode"}
            or identity["path"] != mount["source"]
            or not _number(identity["device"])
            or not _number(identity["inode"], minimum=1)):
        raise InventoryError("invalid source identity")
    entries = proof["entries"]
    if not isinstance(entries, dict) or not 1 <= len(entries) <= ENTRY_LIMIT:
        raise InventoryError("invalid transitive entry inventory")
    total = 0
    common = {"kind", "device", "inode", "uid", "gid", "mode"}
    for path, entry in entries.items():
        check()
        _relative(path)
        if not isinstance(entry, dict):
            raise InventoryError("invalid inventory entry")
        kind = entry.get("kind")
        additional = {
            "file": {"bytes", "sha256"},
            "directory": set(),
            "symlink": {"target"}
        }
        if kind not in additional or set(entry) != common | additional[kind]:
            raise InventoryError("invalid inventory entry schema")
        if (not _number(entry["device"])
                or entry["device"] != identity["device"]
                or not _number(entry["inode"], minimum=1)
                or not _number(entry["uid"]) or entry["uid"] != owner
                or not _number(entry["gid"])
                or not _number(entry["mode"], maximum=0o7777)
                or (kind != "symlink" and entry["mode"] & 0o022)):
            raise InventoryError(
                "manifest describes an untrusted owner, mode, or device"
            )
        if kind == "file":
            if (not _number(entry["bytes"], maximum=TOTAL_BYTES_LIMIT)
                    or not isinstance(entry["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise InventoryError("invalid file byte count or SHA256")
            total += entry["bytes"]
        if kind == "symlink":
            target = entry["target"]
            if (not isinstance(target, str) or not target or len(target) > 4096
                    or target.startswith("/")
                    or any(ord(c) < 32 for c in target)):
                raise InventoryError(
                    "only reviewed relative symlink targets are allowed"
                )
        if path != ".":
            parent = path.rpartition("/")[0] or "."
            if entries.get(parent, {}).get("kind") != "directory":
                raise InventoryError(
                    "manifest has a missing or non-directory parent"
                )
    if total > TOTAL_BYTES_LIMIT:
        raise InventoryError("snapshot total exceeds byte bound")
    root = entries.get(".", {})
    if (root.get("kind") != "directory"
            or root.get("device") != identity["device"]
            or root.get("inode") != identity["inode"]):
        raise InventoryError("manifest root and source identity differ")
    _resolve_links(entries, check)
    return entries, total


def _resolve_links(entries, check):
    """Model POSIX traversal entirely inside the approved entry graph."""
    for path, entry in entries.items():
        check()
        if entry["kind"] != "symlink":
            continue
        pending, prefix, followed = path.split("/"), [], 0
        while pending:
            check()
            part, pending = pending[0], pending[1:]
            if part in {"", "."}:
                continue
            if part == "..":
                if not prefix:
                    raise InventoryError("symlink escapes snapshot")
                prefix.pop()
                continue
            name = "/".join(prefix + [part])
            target = entries.get(name)
            if target is None:
                raise InventoryError(
                    "symlink resolves to an unreviewed or missing path"
                )
            if target["kind"] == "symlink":
                followed += 1
                if followed > 40:
                    raise InventoryError("symlink cycle or excessive chain")
                pending = target["target"].split("/") + pending
            else:
                if target["kind"] != "directory" and pending:
                    raise InventoryError("symlink traverses a regular file")
                prefix.append(part)


def _read_proof(fs, directory_fd, filename, expected_hash, owner, check):
    before = fs.lstat(directory_fd, filename)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != owner or stat.S_IMODE(before.st_mode) != 0o600
            or not 1 <= before.st_size <= MANIFEST_LIMIT):
        raise InventoryError(
            "qualification proof must be bounded private root-owned JSON"
        )
    fd = fs.open_file(directory_fd, filename)
    try:
        if _signature(fs.fstat(fd)) != _signature(before):
            raise InventoryError("proof changed while opening")
        remaining, digest, chunks = before.st_size, hashlib.sha256(), []
        while remaining:
            check()
            block = fs.read(fd, min(CHUNK_BYTES, remaining))
            if not block:
                raise InventoryError("short proof read")
            remaining -= len(block)
            digest.update(block)
            chunks.append(block)
        if fs.read(fd, 1) or digest.hexdigest() != expected_hash:
            raise InventoryError(
                "qualification JSON does not match configured SHA256"
            )
        if (_signature(fs.fstat(fd)) != _signature(before) or _signature(
                fs.lstat(directory_fd, filename)) != _signature(before)):
            raise InventoryError("proof changed while reading")
        check()
        return b"".join(chunks)
    finally:
        fs.close(fd)


def _scan(fs, root_fd, entries, owner, device, check):
    signatures = {}

    def visit(fd, path, before):
        check()
        expected = entries.get(path)
        if expected is None or len(signatures) >= ENTRY_LIMIT:
            raise InventoryError("unreviewed or excessive snapshot path")
        actual = _metadata(before, owner, device)
        wanted = {
            key: value
            for key, value in expected.items()
            if key not in {"sha256", "target"}
        }
        if actual != wanted:
            raise InventoryError(
                "snapshot metadata differs from reviewed inventory"
            )
        signatures[path] = _signature(before)
        if actual["kind"] != "directory":
            return
        for name in fs.names(fd):
            check()
            if not isinstance(name, str) or "/" in name or name in {"", ".",
                                                                    ".."}:
                raise InventoryError("invalid directory entry")
            child_path = name if path == "." else path + "/" + name
            _relative(child_path)
            if child_path not in entries:
                raise InventoryError(
                    "unreviewed snapshot path; contents were not read"
                )
            child = fs.lstat(fd, name)
            if stat.S_ISDIR(child.st_mode):
                child_fd = fs.open_directory(fd, name)
                try:
                    if _signature(fs.fstat(child_fd)) != _signature(child):
                        raise InventoryError("directory changed while opening")
                    visit(child_fd, child_path, child)
                finally:
                    fs.close(child_fd)
            else:
                visit(None, child_path, child)
                if stat.S_ISLNK(child.st_mode):
                    if fs.readlink(fd, name) != entries[child_path]["target"]:
                        raise InventoryError(
                            "symlink target differs from manifest"
                        )
            if _signature(fs.lstat(fd, name)) != _signature(child):
                raise InventoryError("snapshot path changed during traversal")
        if _signature(fs.fstat(fd)) != _signature(before):
            raise InventoryError("snapshot directory changed during traversal")

    visit(root_fd, ".", fs.fstat(root_fd))
    if set(signatures) != set(entries):
        raise InventoryError("reviewed snapshot entries are missing")
    return signatures


def _hash_file(fs, root_fd, path, expected, signatures, check):
    current = fs.duplicate(root_fd)
    try:
        prefix = []
        for component in path.split("/")[:-1]:
            check()
            child = fs.open_directory(current, component)
            prefix.append(component)
            if _signature(fs.fstat(child)) != signatures["/".join(prefix)]:
                fs.close(child)
                raise InventoryError(
                    "pinned inventory parent changed before hashing"
                )
            fs.close(current)
            current = child
        name = path.rsplit("/", 1)[-1]
        fd = fs.open_file(current, name)
        try:
            if _signature(fs.fstat(fd)) != signatures[path]:
                raise InventoryError("snapshot file changed before hashing")
            digest, remaining = hashlib.sha256(), expected["bytes"]
            while remaining:
                check()
                block = fs.read(fd, min(remaining, CHUNK_BYTES))
                if not block:
                    raise InventoryError("short snapshot file read")
                remaining -= len(block)
                digest.update(block)
            if fs.read(fd, 1) or digest.hexdigest() != expected["sha256"]:
                raise InventoryError("snapshot file content hash differs")
            if (_signature(fs.fstat(fd)) != signatures[path] or _signature(
                    fs.lstat(current, name)) != signatures[path]):
                raise InventoryError("snapshot file changed while hashing")
            check()
        finally:
            fs.close(fd)
    finally:
        fs.close(current)


def verify_mount(
    mount,
    *,
    proof_directory=PROOF_DIRECTORY,
    fs=None,
    expected_owner_uid=0,
    deadline=None,
    clock=time.monotonic
):
    """Verify one configured mount without running commands or writing files.

    deadline uses the injected monotonic clock; omission permits 15 minutes. The
    caller must enforce its independent overall runtime bound for blocked I/O.
    No hash cache is trusted: every reviewed regular file is streamed each time.
    """
    if (not isinstance(mount, dict)
            or set(mount) != {"source", "target", "qualification_sha256"}
            or mount["target"] not in TARGETS
            or not isinstance(mount["qualification_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", mount["qualification_sha256"])
            or not _number(expected_owner_uid)):
        raise InventoryError("invalid fixed mount configuration")
    _absolute(mount["source"])
    _absolute(proof_directory)
    now = clock()
    if deadline is None:
        deadline = now + 900
    if type(deadline) not in {int, float} or not math.isfinite(deadline):
        raise InventoryError("finite inventory deadline required")

    def check():
        value = clock()
        if not math.isfinite(value) or value >= deadline:
            raise TimeoutError(
                "mount inventory verification deadline exceeded"
            )

    check()
    fs = PosixFs() if fs is None else fs
    proof_fd = fs.open_proof_directory(
        proof_directory, owner=expected_owner_uid
    )
    try:
        raw = _read_proof(
            fs, proof_fd, mount["qualification_sha256"] + ".json",
            mount["qualification_sha256"], expected_owner_uid, check
        )
    finally:
        fs.close(proof_fd)
    proof = _strict_json(raw)
    entries, total = _schema(proof, mount, expected_owner_uid, check)
    check()
    source_fd = fs.open_source(mount["source"], owner=expected_owner_uid)
    try:
        signatures = _scan(
            fs, source_fd, entries, expected_owner_uid,
            proof["identity"]["device"], check
        )
        for path, entry in entries.items():
            if entry["kind"] == "file":
                _hash_file(fs, source_fd, path, entry, signatures, check)
        # Detect mutation of an earlier file or directory while another hashes.
        if _scan(fs, source_fd, entries, expected_owner_uid,
                 proof["identity"]["device"], check) != signatures:
            raise InventoryError(
                "snapshot changed across full inventory verification"
            )
        # Reopen through the trusted absolute source path to detect replacement
        # of the source directory while its old FD remained valid.
        reopened = fs.open_source(mount["source"], owner=expected_owner_uid)
        try:
            if _signature(fs.fstat(reopened)) != signatures["."]:
                raise InventoryError(
                    "source path no longer identifies the verified tree"
                )
        finally:
            fs.close(reopened)
        check()
        return {
            "identity": proof["identity"],
            "qualification_sha256": mount["qualification_sha256"],
            "entries": len(entries),
            "files": sum(e["kind"] == "file" for e in entries.values()),
            "bytes": total,
            "verified": True
        }
    finally:
        fs.close(source_fd)
