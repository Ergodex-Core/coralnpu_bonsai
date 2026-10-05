"""Offline transitive-inventory tests using injected local trusted directory FDs."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import mount_inventory as inventory
from secure_files import open_trusted_directory, DIRECTORY_FLAGS


class AnchoredFs(inventory.PosixFs):

    def __init__(self, anchor):
        self.anchor = anchor
        self.opened = []
        self.opened_sources = 0
        self.fd_names = {}
        self.change_stat = None
        self.after_read = None

    def open_proof_directory(self, path, *, owner):
        if path != "/proofs":
            raise AssertionError("unexpected proof root")
        return open_trusted_directory(
            "proofs",
            dir_fd=self.anchor,
            expected_owner_uid=owner,
            private=True
        )

    def open_source(self, path, *, owner):
        if path != "/snapshot":
            raise AssertionError("unexpected source root")
        self.opened_sources += 1
        return open_trusted_directory(
            "snapshot", dir_fd=self.anchor, expected_owner_uid=owner
        )

    def open_file(self, directory_fd, name):
        self.opened.append(name)
        fd = super().open_file(directory_fd, name)
        self.fd_names[fd] = name
        return fd

    def lstat(self, directory_fd, name):
        value = super().lstat(directory_fd, name)
        if self.change_stat:
            return self.change_stat(name, value)
        return value

    def read(self, fd, size):
        data = os.read(fd, size)
        if self.after_read:
            self.after_read(self.fd_names.get(fd), data)
        return data


def changed_stat(value, **updates):
    fields = {
        key: getattr(value, key)
        for key in (
            "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
            "st_size", "st_mtime_ns", "st_ctime_ns"
        )
    }
    return SimpleNamespace(**(fields | updates))


class MountInventoryTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "snapshot"
        self.proofs = self.root / "proofs"
        self.source.mkdir(mode=0o700)
        self.proofs.mkdir(mode=0o700)
        (self.source / "bin").mkdir(mode=0o755)
        (self.source / "bin/tool").write_bytes(b"reviewed tool bytes\n")
        (self.source / "bin/tool").chmod(0o755)
        (self.source / "data").write_bytes(b"reviewed public data\n")
        (self.source / "data").chmod(0o644)
        self.anchor = os.open(self.root, DIRECTORY_FLAGS)
        self.addCleanup(os.close, self.anchor)
        self.uid = os.geteuid()
        self.fs = AnchoredFs(self.anchor)
        self.proof = self.manifest()
        self.mount = self.write_proof(self.proof)

    def manifest(self):
        entries = {}

        def visit(path, relative):
            info = path.lstat()
            kind = "directory" if path.is_dir() and not path.is_symlink(
            ) else ("symlink" if path.is_symlink() else "file")
            value = {
                "kind": kind,
                "device": info.st_dev,
                "inode": info.st_ino,
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": stat.S_IMODE(info.st_mode)
            }
            if kind == "file":
                value.update(
                    bytes=info.st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest()
                )
            if kind == "symlink":
                value["target"] = os.readlink(path)
            entries[relative] = value
            if kind == "directory":
                for child in path.iterdir():
                    visit(
                        child, child.name if relative == "." else relative +
                        "/" + child.name
                    )

        visit(self.source, ".")
        root = self.source.stat()
        return {
            "schema_version": 1,
            "source": "/snapshot",
            "target": "/deps",
            "no_secrets": True,
            "identity": {
                "path": "/snapshot",
                "device": root.st_dev,
                "inode": root.st_ino
            },
            "entries": entries
        }

    def write_proof(self, proof):
        raw = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = self.proofs / (digest + ".json")
        path.write_bytes(raw)
        path.chmod(0o600)
        return {
            "source": "/snapshot",
            "target": "/deps",
            "qualification_sha256": digest
        }

    def verify(self, **kwargs):
        return inventory.verify_mount(
            self.mount,
            proof_directory="/proofs",
            fs=self.fs,
            expected_owner_uid=self.uid,
            clock=lambda: 0,
            **kwargs
        )

    def test_full_snapshot_and_internal_chained_symlinks(self):
        (self.source / "tool-link").symlink_to("bin/tool")
        (self.source / "alias").symlink_to("tool-link")
        (self.source / "bin/data-link").symlink_to("../data")
        self.mount = self.write_proof(self.manifest())
        self.mask_timestamp_changes()
        result = self.verify()
        self.assertTrue(result["verified"])
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["entries"], 7)
        self.assertEqual(
            result["bytes"],
            len((self.source / "data").read_bytes()) +
            len((self.source / "bin/tool").read_bytes())
        )

    def test_manifest_exact_byte_hash_checked_before_source(self):
        path = self.proofs / (self.mount["qualification_sha256"] + ".json")
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertEqual(self.fs.opened_sources, 0)

    def test_proof_source_target_and_review_attestation_must_match(self):
        for field, value in (("source", "/other"), ("target", "/opt/amd"),
                             ("no_secrets", False)):
            with self.subTest(field=field):
                proof = self.manifest()
                proof[field] = value
                self.mount = self.write_proof(proof)
                with self.assertRaises(inventory.InventoryError):
                    self.verify()
        self.assertEqual(self.fs.opened_sources, 0)

    def test_unreviewed_file_is_rejected_without_reading_any_tool_contents(
        self
    ):
        (self.source /
         "unreviewed-private-file").write_bytes(b"must not be read")
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertEqual(
            self.fs.opened, [self.mount["qualification_sha256"] + ".json"]
        )

    def test_developer_writable_file_and_directory_rejected(self):
        for relative in ("bin/tool", "bin"):
            path = self.source / relative
            mode = stat.S_IMODE(path.stat().st_mode)
            with self.subTest(relative=relative):
                path.chmod(0o775)
                with self.assertRaises(inventory.InventoryError):
                    self.verify()
                path.chmod(mode)
        self.assertNotIn("tool", self.fs.opened)

    def test_manifest_cannot_approve_writable_descendants(self):
        (self.source / "bin/tool").chmod(0o777)
        self.mount = self.write_proof(self.manifest())
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertEqual(self.fs.opened_sources, 0)

    def test_nonroot_child_and_foreign_filesystem_rejected(self):
        for update in (lambda s: {"st_uid": self.uid + 1},
                       lambda s: {"st_dev": s.st_dev + 1}):
            self.fs.change_stat = lambda name, s: changed_stat(
                s, **update(s)
            ) if name == "tool" else s
            with self.assertRaises(inventory.InventoryError):
                self.verify()
        self.assertNotIn("tool", self.fs.opened)

    def test_same_size_file_content_change_rejected_by_hash(self):
        path = self.source / "data"
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertIn("data", self.fs.opened)

    def test_missing_file_and_replaced_inode_rejected(self):
        path = self.source / "data"
        original = path.read_bytes()
        path.rename(self.root / "original-data")
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        path.write_bytes(original)
        path.chmod(0o644)
        with self.assertRaises(inventory.InventoryError):
            self.verify()

    def test_hardlink_and_fifo_rejected_before_open(self):
        path = self.source / "data"
        os.link(path, self.root / "hardlink")
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        (self.root / "hardlink").unlink()
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertNotIn("data", self.fs.opened)

    def test_symlink_escape_absolute_dangling_and_cycle_rejected(self):
        path = self.source / "link"
        for target in ("../outside", "/etc/passwd", "missing", "link"):
            with self.subTest(target=target):
                path.symlink_to(target)
                self.mount = self.write_proof(self.manifest())
                with self.assertRaises(inventory.InventoryError):
                    self.verify()
                path.unlink()
        self.assertEqual(self.fs.opened_sources, 0)

    def test_indirect_symlink_escape_rejected(self):
        (self.source / "alias").symlink_to("bin/bridge")
        (self.source / "bin/bridge").symlink_to("../../outside")
        self.mount = self.write_proof(self.manifest())
        with self.assertRaises(inventory.InventoryError):
            self.verify()

    def test_untrusted_proof_modes_symlink_and_large_size_rejected(self):
        path = self.proofs / (self.mount["qualification_sha256"] + ".json")
        path.chmod(0o644)
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        path.chmod(0o600)
        self.fs.change_stat = lambda name, s: changed_stat(
            s, st_size=inventory.MANIFEST_LIMIT + 1
        )
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.fs.change_stat = None
        path.rename(self.root / "proof-original")
        path.symlink_to(self.root / "proof-original")
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertEqual(self.fs.opened, [])

    def mask_timestamp_changes(self):
        # Model Linux metadata that remains unchanged within one timestamp tick.
        real_lstat, real_fstat = self.fs.lstat, self.fs.fstat
        self.fs.lstat = lambda parent, name: changed_stat(
            real_lstat(parent, name), st_mtime_ns=0, st_ctime_ns=0
        )
        self.fs.fstat = lambda fd: changed_stat(
            real_fstat(fd), st_mtime_ns=0, st_ctime_ns=0
        )

    def test_mutation_during_file_read_rejected(self):
        self.mask_timestamp_changes()
        changed = False

        def mutate(name, data):
            nonlocal changed
            if name == "tool" and data and not changed:
                changed = True
                path = self.source / "bin/tool"
                path.write_bytes(b"x" * path.stat().st_size)

        self.fs.after_read = mutate
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertTrue(changed)

    def test_earlier_file_rewrite_during_later_read_rejected(self):
        self.mask_timestamp_changes()
        first_path = None
        changed = False

        def mutate(name, data):
            nonlocal first_path, changed
            if not data or name not in {"data", "tool"}:
                return
            path = self.source / ("bin/tool" if name == "tool" else "data")
            if first_path is None:
                first_path = path
            elif path != first_path and not changed:
                changed = True
                first_path.write_bytes(b"x" * first_path.stat().st_size)

        self.fs.after_read = mutate
        with self.assertRaises(inventory.InventoryError):
            self.verify()
        self.assertTrue(changed)

    def test_content_reverification_reads_at_most_two_file_passes(self):
        counts = {"tool": 0, "data": 0}

        def count(name, data):
            if name in counts:
                counts[name] += len(data)

        self.fs.after_read = count
        self.verify()
        self.assertEqual(
            counts, {
                "tool": 2 * self.proof["entries"]["bin/tool"]["bytes"],
                "data": 2 * self.proof["entries"]["data"]["bytes"]
            }
        )

    def test_deadline_also_bounds_second_content_pass(self):
        real_open = self.fs.open_file
        opens = 0
        expired = False

        def opening(parent, name):
            nonlocal opens
            if name == "tool":
                opens += 1
            return real_open(parent, name)

        def expire_during_second_read(name, data):
            nonlocal expired
            if name == "tool" and opens == 2 and data:
                expired = True

        self.fs.open_file = opening
        self.fs.after_read = expire_during_second_read
        with self.assertRaises(TimeoutError):
            inventory.verify_mount(
                self.mount,
                proof_directory="/proofs",
                fs=self.fs,
                expected_owner_uid=self.uid,
                deadline=900,
                clock=lambda: 900 if expired else 0
            )
        self.assertTrue(expired)

    def test_expired_deadline_opens_nothing(self):
        with self.assertRaises(TimeoutError):
            self.verify(deadline=0)
        self.assertEqual(self.fs.opened, [])
        self.assertEqual(self.fs.opened_sources, 0)

    def test_deadline_applies_during_schema_and_symlink_validation(self):
        times = iter([0] * 5 + [900])
        with self.assertRaises(TimeoutError):
            inventory.verify_mount(
                self.mount,
                proof_directory="/proofs",
                fs=self.fs,
                expected_owner_uid=self.uid,
                deadline=900,
                clock=lambda: next(times, 900)
            )
        self.assertEqual(self.fs.opened_sources, 0)

    def test_unknown_schema_fields_and_traversal_names_rejected(self):
        for mutation in (lambda p: p.update(unreviewed=True), lambda p: p[
                "entries"].update({"../escape": p["entries"]["data"]})):
            proof = self.manifest()
            mutation(proof)
            self.mount = self.write_proof(proof)
            with self.assertRaises(inventory.InventoryError):
                self.verify()
        self.assertEqual(self.fs.opened_sources, 0)

    def test_top_directory_replacement_detected_through_trusted_reopen(self):
        opened_source = self.fs.open_source
        calls = 0

        def changed_source(path, *, owner):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.source.rename(self.root / "old-snapshot")
                self.source.mkdir(mode=0o700)
            return opened_source(path, owner=owner)

        self.fs.open_source = changed_source
        with self.assertRaises(inventory.InventoryError):
            self.verify()


if __name__ == "__main__":
    unittest.main()
