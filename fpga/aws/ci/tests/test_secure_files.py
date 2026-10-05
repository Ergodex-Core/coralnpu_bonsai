"""Offline rejection tests; no host service, device, mount, or AWS operations."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import secure_files as sf


@contextmanager
def unchanged_timestamps():
    """Keep real identity/mode/size but model a same-tick Linux rewrite."""
    real_stat, real_fstat = os.stat, os.fstat

    def coarse(info):
        fields = {
            name: getattr(info, name)
            for name in (
                "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                "st_size"
            )
        }
        return SimpleNamespace(**fields, st_mtime_ns=0, st_ctime_ns=0)

    with mock.patch.object(sf.os, "stat", side_effect=lambda *a, **kw: coarse(real_stat(*a, **kw))), \
            mock.patch.object(sf.os, "fstat", side_effect=lambda fd: coarse(real_fstat(fd))):
        yield


class FileFixture(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uid = os.geteuid()
        self.anchor = os.open(self.root, sf.DIRECTORY_FLAGS)
        self.addCleanup(os.close, self.anchor)
        self.addCleanup(self.temp.cleanup)

    def directory(self, name):
        path = self.root / name
        path.mkdir(mode=0o700)
        fd = os.open(path, sf.DIRECTORY_FLAGS)
        self.addCleanup(os.close, fd)
        return path, fd


class TrustedPathTests(FileFixture):

    def test_checks_each_parent_and_private_leaf(self):
        parent = self.root / "parent"
        leaf = parent / "private"
        parent.mkdir(mode=0o755)
        leaf.mkdir(mode=0o700)
        fd = sf.open_trusted_directory(
            "parent/private",
            dir_fd=self.anchor,
            expected_owner_uid=self.uid,
            private=True
        )
        os.close(fd)
        parent.chmod(0o775)
        with self.assertRaises(sf.FileBoundaryError):
            sf.open_trusted_directory(
                "parent/private",
                dir_fd=self.anchor,
                expected_owner_uid=self.uid
            )
        parent.chmod(0o755)
        leaf.chmod(0o755)
        with self.assertRaises(sf.FileBoundaryError):
            sf.open_trusted_directory(
                "parent/private",
                dir_fd=self.anchor,
                expected_owner_uid=self.uid,
                private=True
            )

    def test_symlink_parent_and_owner_are_rejected(self):
        real, _ = self.directory("real")
        (self.root / "alias").symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            sf.open_trusted_directory(
                "alias", dir_fd=self.anchor, expected_owner_uid=self.uid
            )
        with self.assertRaises(sf.FileBoundaryError):
            sf.open_trusted_directory(
                "real", dir_fd=self.anchor, expected_owner_uid=self.uid + 1
            )

    def test_ambiguous_paths_and_untrusted_anchor_rejected(self):
        for path in ("../outside", "./child", "child//leaf", "/absolute",
                     "child/"):
            with self.subTest(path=path
                              ), self.assertRaises(sf.FileBoundaryError):
                sf.open_trusted_directory(
                    path, dir_fd=self.anchor, expected_owner_uid=self.uid
                )
        self.root.chmod(0o777)
        with self.assertRaises(sf.FileBoundaryError):
            sf.open_trusted_directory(
                "", dir_fd=self.anchor, expected_owner_uid=self.uid
            )
        self.root.chmod(0o700)

    def test_absolute_walk_default_owner_is_root(self):
        fd = sf.open_trusted_directory("/")
        try:
            self.assertEqual(os.fstat(fd).st_uid, 0)
        finally:
            os.close(fd)


class PrivateJsonTests(FileFixture):

    def create(self, value, name="123-1.json", **kwargs):
        return sf.create_private_json(
            self.anchor, name, value, expected_owner_uid=self.uid, **kwargs
        )

    def test_atomic_creation_identical_retry_and_changed_request(self):
        self.assertTrue(self.create({"source": "abc", "run": 123}))
        self.assertFalse(self.create({"run": 123, "source": "abc"}))
        self.assertEqual(
            sf.read_private_json(
                self.anchor, "123-1.json", expected_owner_uid=self.uid
            ), {
                "run": 123,
                "source": "abc"
            }
        )
        info = (self.root / "123-1.json").stat()
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_nlink, 1)
        with self.assertRaises(sf.FileBoundaryError):
            self.create({"run": 124})
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()), ["123-1.json"]
        )

    def test_symlink_hardlink_fifo_and_public_file_rejected(self):
        target = self.root / "sentinel"
        target.write_text('{"safe":true}\n')
        target.chmod(0o600)
        path = self.root / "123-1.json"
        for kind in ("symlink", "hardlink", "fifo", "public"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, path)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.write_text('{"safe":true}\n')
                    path.chmod(0o644)
                with self.assertRaises(sf.FileBoundaryError):
                    self.create({"safe": True})
                path.unlink()
        self.assertEqual(target.read_text(), '{"safe":true}\n')

    def test_size_filename_nan_and_wrong_owner_fail(self):
        for name in ("../outside.json", "/outside.json", ".", "a/b.json"):
            with self.subTest(name=name
                              ), self.assertRaises(sf.FileBoundaryError):
                self.create({}, name=name)
        with self.assertRaises(sf.FileBoundaryError):
            self.create({"large": "x" * 100}, max_bytes=20)
        with self.assertRaises(ValueError):
            self.create({"bad": float("nan")})
        with self.assertRaises(sf.FileBoundaryError):
            sf.create_private_json(
                self.anchor, "123-1.json", {}, expected_owner_uid=self.uid + 1
            )
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_publication_does_not_leave_partial_request(self):
        with mock.patch.object(sf.os, "link",
                               side_effect=OSError("injected failure")):
            with self.assertRaises(OSError):
                self.create({"run": 123})
        self.assertEqual(list(self.root.iterdir()), [])


class ArtifactTests(FileFixture):

    def setUp(self):
        super().setUp()
        self.source, self.source_fd = self.directory("output")
        self.inbox, self.inbox_fd = self.directory("inbox")
        self.payloads = {
            "checkpoint.tar": b"opaque checkpoint bytes" * 10,
            "evidence.tar": b"not parsed as an archive" * 7
        }
        for name, payload in self.payloads.items():
            (self.source / name).write_bytes(payload)
        self.addCleanup(self.restore_permissions)

    def restore_permissions(self):
        self.inbox.chmod(0o700)
        for path in self.inbox.iterdir():
            if path.is_file() and not path.is_symlink():
                path.chmod(0o600)

    def collect(self, **kwargs):
        return sf.collect_artifacts(
            self.source_fd,
            self.inbox_fd,
            deadline=10,
            clock=lambda: 0,
            **kwargs
        )

    def test_fixed_opaque_files_hashes_sizes_and_modes(self):
        (self.source / "ignored.sh").write_text("must not run")
        with unchanged_timestamps():
            result = self.collect(chunk_size=7)
        self.assertEqual(set(result), set(self.payloads))
        self.assertEqual(
            set(path.name for path in self.inbox.iterdir()),
            set(self.payloads)
        )
        for name, payload in self.payloads.items():
            self.assertEqual((self.inbox / name).read_bytes(), payload)
            self.assertEqual(
                result[name]["sha256"],
                hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(result[name]["bytes"], len(payload))
            self.assertEqual(
                result[name]["inode"], (self.inbox / name).stat().st_ino
            )
            self.assertEqual(
                stat.S_IMODE((self.inbox / name).stat().st_mode), 0o600
            )

    def test_symlink_hardlink_fifo_and_directory_rejected_before_open(self):
        path = self.source / "checkpoint.tar"
        sentinel = self.source / "sentinel"
        sentinel.write_bytes(b"private sentinel")
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                path.unlink()
                if kind == "symlink":
                    path.symlink_to(sentinel)
                elif kind == "hardlink":
                    os.link(sentinel, path)
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.mkdir()
                with self.assertRaises(sf.FileBoundaryError):
                    self.collect()
                self.assertEqual(list(self.inbox.iterdir()), [])
                if kind == "directory":
                    path.rmdir()
                else:
                    path.unlink()
                path.write_bytes(b"reset")
        self.assertEqual(sentinel.read_bytes(), b"private sentinel")

    def test_device_and_socket_metadata_rejected_without_open(self):
        actual_stat = sf.os.stat
        object_type = stat.S_IFCHR

        def device_stat(name, *args, **kwargs):
            info = actual_stat(name, *args, **kwargs)
            if name == "checkpoint.tar":
                values = list(info)
                values[0] = object_type | 0o600
                return os.stat_result(values)
            return info

        for object_type in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
            with self.subTest(object_type=object_type):
                with mock.patch.object(sf.os, "stat", side_effect=device_stat):
                    with self.assertRaises(sf.FileBoundaryError):
                        self.collect()
                self.assertEqual(list(self.inbox.iterdir()), [])

    def test_sparse_oversize_rejected_before_copy(self):
        for name, limit in sf.ARTIFACT_LIMITS.items():
            with self.subTest(name=name):
                with (self.source / name).open("wb") as stream:
                    stream.truncate(limit + 1)
                with self.assertRaises(sf.FileBoundaryError):
                    self.collect()
                self.assertEqual(list(self.inbox.iterdir()), [])
                (self.source / name).write_bytes(self.payloads[name])

    def test_existing_destination_and_symlink_are_never_replaced(self):
        # The second destination fails after the first copy has begun.
        sentinel = self.inbox / "sentinel"
        sentinel.write_bytes(b"untouched")
        (self.inbox / "evidence.tar").symlink_to(sentinel)
        with self.assertRaises(FileExistsError):
            self.collect()
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertTrue((self.inbox / "evidence.tar").is_symlink())
        self.assertFalse((self.inbox / "checkpoint.tar").exists())

    def test_pinned_directory_mismatch_and_same_directory_rejected(self):
        info = os.fstat(self.source_fd)
        with self.assertRaises(sf.FileBoundaryError):
            self.collect(
                expected_source_identity=(info.st_dev, info.st_ino + 1)
            )
        with self.assertRaises(sf.FileBoundaryError):
            sf.collect_artifacts(
                self.source_fd, self.source_fd, deadline=10, clock=lambda: 0
            )
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_replacement_and_same_size_mutation_during_copy_rejected(self):
        real_read = os.read
        for replacement in (False, True):
            changed = False

            def mutating_read(fd, count):
                nonlocal changed
                block = real_read(fd, count)
                if block and not changed:
                    changed = True
                    path = self.source / "checkpoint.tar"
                    if replacement:
                        path.unlink()
                    path.write_bytes(
                        b"X" * len(self.payloads["checkpoint.tar"])
                    )
                return block

            with self.subTest(replacement=replacement):
                (self.source / "checkpoint.tar").write_bytes(
                    self.payloads["checkpoint.tar"]
                )
                try:
                    with unchanged_timestamps(), mock.patch.object(
                            sf.os, "read", side_effect=mutating_read):
                        with self.assertRaises(sf.FileBoundaryError):
                            self.collect(chunk_size=8)
                    self.assertEqual(list(self.inbox.iterdir()), [])
                finally:
                    # Keep later subtests independent if a regression wrongly
                    # accepts this copy and leaves output behind.
                    for path in self.inbox.iterdir():
                        path.unlink()

    def test_earlier_source_rewrite_while_copying_later_file_rejected(self):
        real_read = os.read
        evidence_inode = (self.source / "evidence.tar").stat().st_ino
        changed = False

        def mutate(fd, count):
            nonlocal changed
            block = real_read(fd, count)
            if block and os.fstat(fd).st_ino == evidence_inode and not changed:
                changed = True
                (self.source / "checkpoint.tar").write_bytes(
                    b"X" * len(self.payloads["checkpoint.tar"])
                )
            return block

        with unchanged_timestamps(), mock.patch.object(sf.os, "read",
                                                       side_effect=mutate):
            with self.assertRaises(sf.FileBoundaryError):
                self.collect(chunk_size=8)
        self.assertTrue(changed)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_second_content_pass_is_bounded_and_deadline_checked(self):
        real_read, real_seek = os.read, os.lseek
        bytes_read = 0
        largest_read = 0

        def count(fd, size):
            nonlocal bytes_read, largest_read
            block = real_read(fd, size)
            bytes_read += len(block)
            largest_read = max(largest_read, size)
            return block

        with mock.patch.object(sf.os, "read", side_effect=count):
            self.collect(chunk_size=8)
        self.assertEqual(bytes_read, 2 * sum(map(len, self.payloads.values())))
        self.assertLessEqual(largest_read, 8)
        for path in self.inbox.iterdir():
            path.unlink()
        expired = False
        rechecking = False

        def rewind(fd, offset, whence):
            nonlocal rechecking
            rechecking = True
            return real_seek(fd, offset, whence)

        def expire_during_read(fd, size):
            nonlocal expired
            block = real_read(fd, size)
            if rechecking and block:
                expired = True
            return block

        with mock.patch.object(sf.os, "lseek", side_effect=rewind), \
                mock.patch.object(sf.os, "read", side_effect=expire_during_read):
            with self.assertRaises(TimeoutError):
                sf.collect_artifacts(
                    self.source_fd,
                    self.inbox_fd,
                    deadline=10,
                    clock=lambda: 10 if expired else 0,
                    chunk_size=8
                )
        self.assertTrue(expired)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_recheck_failure_preserves_unrelated_and_replaced_destinations(
        self
    ):
        real_read, real_seek = os.read, os.lseek
        sentinel = self.inbox / "unrelated"
        sentinel.write_bytes(b"keep unrelated")
        replaced = self.inbox / "checkpoint.tar"
        rechecking = False
        changed = False

        def rewind(fd, offset, whence):
            nonlocal rechecking
            rechecking = True
            return real_seek(fd, offset, whence)

        def mutate(fd, size):
            nonlocal changed
            block = real_read(fd, size)
            if rechecking and block and not changed:
                changed = True
                replaced.rename(self.inbox / "original-checkpoint")
                replaced.write_bytes(b"keep replacement inode")
                (self.source / "checkpoint.tar").write_bytes(
                    b"X" * len(self.payloads["checkpoint.tar"])
                )
            return block

        with unchanged_timestamps(), \
                mock.patch.object(sf.os, "lseek", side_effect=rewind), \
                mock.patch.object(sf.os, "read", side_effect=mutate):
            with self.assertRaises(sf.FileBoundaryError):
                self.collect(chunk_size=8)
        self.assertTrue(changed)
        self.assertEqual(sentinel.read_bytes(), b"keep unrelated")
        self.assertEqual(replaced.read_bytes(), b"keep replacement inode")
        self.assertFalse((self.inbox / "evidence.tar").exists())

    def test_short_writes_supported_and_zero_write_rejected(self):
        real_write = os.write
        with mock.patch.object(
                sf.os, "write",
                side_effect=lambda fd, data: real_write(fd, data[:3])):
            self.collect(chunk_size=8)
        for path in self.inbox.iterdir():
            path.unlink()
        with mock.patch.object(sf.os, "write", return_value=0):
            with self.assertRaises(sf.FileBoundaryError):
                self.collect()
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_deadline_midcopy_cleans_only_new_files(self):
        sentinel = self.inbox / "sentinel"
        sentinel.write_bytes(b"untouched")
        ticks = iter([0] * 9 + [10])
        with self.assertRaises(TimeoutError):
            sf.collect_artifacts(
                self.source_fd,
                self.inbox_fd,
                deadline=10,
                clock=lambda: next(ticks, 10),
                chunk_size=1
            )
        self.assertEqual(list(self.inbox.iterdir()), [sentinel])
        with self.assertRaises(TimeoutError):
            sf.collect_artifacts(
                self.source_fd, self.inbox_fd, deadline=10, clock=lambda: 10
            )

    def test_invalid_limits_and_empty_artifact_rejected(self):
        for chunk in (0, -1, sf.CHUNK_LIMIT + 1, True):
            with self.subTest(chunk=chunk
                              ), self.assertRaises(sf.FileBoundaryError):
                self.collect(chunk_size=chunk)
        with self.assertRaises(sf.FileBoundaryError):
            sf.collect_artifacts(
                self.source_fd,
                self.inbox_fd,
                deadline=float("nan"),
                clock=lambda: 0
            )
        (self.source / "evidence.tar").write_bytes(b"")
        with self.assertRaises(sf.FileBoundaryError):
            self.collect()

    def test_sealing_is_fd_based_and_verifiable(self):
        collected = self.collect()
        # Same-UID fchown is supported locally, with no root or real UID change.
        sealed = sf.seal_inbox(self.inbox_fd, self.uid, self.uid)
        self.assertEqual(stat.S_IMODE(os.fstat(self.inbox_fd).st_mode), 0o500)
        for name in self.payloads:
            self.assertEqual(sealed[name]["inode"], collected[name]["inode"])
            self.assertEqual(
                stat.S_IMODE((self.inbox / name).stat().st_mode), 0o400
            )
        self.assertEqual(
            sf.seal_inbox(self.inbox_fd, self.uid, self.uid), sealed
        )
        (self.inbox / "evidence.tar").chmod(0o600)
        with self.assertRaises(sf.FileBoundaryError):
            sf.verify_sealed_inbox(self.inbox_fd, expected_root_uid=self.uid)

    def test_sealing_preflights_all_files_before_mutation(self):
        self.collect()
        (self.inbox / "evidence.tar").unlink()
        (self.inbox / "evidence.tar").symlink_to(self.source / "evidence.tar")
        with mock.patch.object(sf.os, "fchown") as chown:
            with self.assertRaises(sf.FileBoundaryError):
                sf.seal_inbox(self.inbox_fd, self.uid, self.uid)
            chown.assert_not_called()

    def test_failed_build_collects_and_seals_only_evidence(self):
        (self.source / "checkpoint.tar").unlink()
        # Even a dangerous checkpoint cannot affect the failure-evidence path.
        os.mkfifo(self.source / "checkpoint.tar", 0o600)
        result = self.collect(qualified=False)
        self.assertEqual(set(result), {"evidence.tar"})
        self.assertEqual(
            set(path.name for path in self.inbox.iterdir()), {"evidence.tar"}
        )
        sealed = sf.seal_inbox(
            self.inbox_fd, self.uid, self.uid, qualified=False
        )
        self.assertEqual(set(sealed), {"evidence.tar"})
        self.assertEqual(
            sf.verify_sealed_inbox(
                self.inbox_fd, expected_root_uid=self.uid, qualified=False
            ), sealed
        )
        with self.assertRaises(FileNotFoundError):
            sf.verify_sealed_inbox(self.inbox_fd, expected_root_uid=self.uid)

    def test_success_requires_checkpoint_and_strict_qualification_flag(self):
        (self.source / "checkpoint.tar").unlink()
        with self.assertRaises(FileNotFoundError):
            self.collect()
        for value in (1, "false", None):
            with self.subTest(value=value
                              ), self.assertRaises(sf.FileBoundaryError):
                self.collect(qualified=value)


if __name__ == "__main__":
    unittest.main()
