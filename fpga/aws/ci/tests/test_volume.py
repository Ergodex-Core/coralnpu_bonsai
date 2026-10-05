"""Injected volume tests. No allocation, formatting, loop, or mount commands run."""
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
import volume as v

UID = 501
SCRATCH_DEVICE = os.makedev(8, 1)
LOOP_DEVICE = os.makedev(7, 12)


def info(
    inode,
    *,
    mode=stat.S_IFDIR | 0o700,
    device=SCRATCH_DEVICE,
    size=0,
    blocks=0
):
    return SimpleNamespace(
        st_ino=inode,
        st_dev=device,
        st_mode=mode,
        st_uid=UID,
        st_nlink=1,
        st_size=size,
        st_blocks=blocks,
        st_rdev=0
    )


def config():
    return {
        "enabled": True,
        "persistent_changes_approved": True,
        "per_run_disk_bytes": v.CAP_BYTES,
        "per_run_inode_limit": v.CAP_INODES,
        "workspace_bind_target": v.MOUNT_TARGET,
        "loop_filesystem_root": v.MOUNT_TARGET,
        "workspace_source": "/approved/private/scratch"
    }


class FakeFs:

    def __init__(self):
        self.calls = []
        self.mounted = False
        self.empty = True
        self.reserve_blocks = True
        self.directory_error = None
        self.source = info(100)
        self.target = info(200)
        self.mounted_root = info(
            2, mode=stat.S_IFDIR | 0o755, device=LOOP_DEVICE
        )
        self.loop = info(700, mode=stat.S_IFBLK | 0o600)
        self.loop.st_rdev = LOOP_DEVICE
        self.image = None
        self.receipt = None
        self.usage = SimpleNamespace(
            f_blocks=v.CAP_BYTES // 4096 - 1000,
            f_frsize=4096,
            f_files=v.FORMAT_INODES
        )

    def open_directory(self, path, *, owner, private=False):
        self.calls.append(("directory", path, owner, private))
        if self.directory_error:
            raise self.directory_error
        if path == config()["workspace_source"]:
            return 10
        if path == v.MOUNT_TARGET:
            return 21 if self.mounted else 20
        raise AssertionError("unexpected directory")

    def lstat(self, fd, name):
        assert fd == 10
        if name == v.IMAGE_NAME:
            return self.image
        if name == v.RECEIPT_NAME:
            return info(
                101, mode=stat.S_IFREG | 0o600
            ) if self.receipt is not None else None
        raise AssertionError("unexpected filename")

    def open_image(self, fd, *, create):
        self.calls.append(("image", create))
        assert fd == 10
        if create:
            if self.image is not None:
                raise FileExistsError()
            self.image = info(102, mode=stat.S_IFREG | 0o600)
        elif self.image is None:
            raise FileNotFoundError()
        return 30

    def reserve(self, fd, size):
        self.calls.append(("reserve", size))
        assert fd == 30 and size == v.CAP_BYTES
        self.image.st_size = size
        self.image.st_blocks = size // 512 if self.reserve_blocks else 1

    def fstat(self, fd):
        return {
            10: self.source,
            20: self.target,
            21: self.mounted_root,
            30: self.image,
            40: self.loop
        }[fd]

    def statvfs(self, fd):
        assert fd == 21
        return self.usage

    def open_loop(self, path, *, owner):
        self.calls.append(("open_loop", path))
        assert owner == UID
        return 40

    def fchmod(self, fd, mode):
        self.calls.append(("chmod", fd, mode))
        entry = self.fstat(fd)
        entry.st_mode = stat.S_IFMT(entry.st_mode) | mode

    def sync(self, fd):
        self.calls.append(("sync", fd))

    def close(self, fd):
        self.calls.append(("close", fd))

    def is_empty(self, fd):
        assert fd == 20
        return self.empty

    def read_receipt(self, fd, *, owner):
        return copy.deepcopy(self.receipt)

    def write_receipt(self, fd, receipt, *, owner):
        self.calls.append(("receipt", ))
        assert self.receipt is None and owner == UID
        self.receipt = copy.deepcopy(receipt)
        return True


class FakeRunner:

    def __init__(self, fs):
        self.fs = fs
        self.calls = []
        self.loop_changes = {}
        self.mount_changes = {}
        self.fail = None
        self.extra_mounts = []
        self.discard_during_mkfs = False
        self.stdout_only = False

    def run(self, argv, *, timeout, pass_fds=()):
        self.calls.append((argv, timeout, pass_fds))
        if argv[0] == self.fail:
            return subprocess.CompletedProcess(argv, 1, "failure")
        if argv[0] == v.FINDMNT:
            rows = [{"target": "/", "source": "/dev/root"}]
            if self.fs.mounted:
                rows.append({
                    "target": v.MOUNT_TARGET,
                    "source": "/dev/loop12",
                    "fstype": "ext4",
                    "options": "rw,nosuid,nodev,relatime",
                    "maj:min": "7:12",
                    "fsroot": "/",
                    **self.mount_changes
                })
            rows += self.extra_mounts
            output = json.dumps({"filesystems": rows})
        elif argv[0] == v.MKFS:
            if self.discard_during_mkfs:
                self.fs.image.st_blocks = 1
            output = ""
        elif argv[0] == v.LOSETUP and "--find" in argv:
            output = "/dev/loop12\n"
        elif argv[0] == v.LOSETUP:
            output = json.dumps({
                "loopdevices": [{
                    "name": "/dev/loop12",
                    "back-ino": self.fs.image.st_ino,
                    "back-maj:min": "8:1",
                    "maj:min": "7:12",
                    "sizelimit": v.CAP_BYTES,
                    "offset": 0,
                    "ro": False,
                    "partscan": False,
                    **self.loop_changes
                }]
            })
        elif argv[0] == v.MOUNT:
            self.fs.mounted = True
            output = ""
        else:
            raise AssertionError("unexpected executable")
        return output if self.stdout_only else subprocess.CompletedProcess(
            argv, 0, output
        )

    def mutations(self):
        return [
            argv for argv, _, _ in self.calls if argv[0] in {v.MKFS, v.MOUNT}
            or (argv[0] == v.LOSETUP and "--find" in argv)
        ]


class VolumeTests(unittest.TestCase):

    def setUp(self):
        self.fs = FakeFs()
        self.runner = FakeRunner(self.fs)
        self.config = config()
        self.manager = v.BoundedVolume(
            self.config, self.runner, fs=self.fs, expected_owner_uid=UID
        )

    def existing(self):
        receipt = self.manager.ensure()
        self.fs.calls.clear()
        self.runner.calls.clear()
        return receipt

    def test_one_reserved_image_exact_bound_and_second_admission_only_verifies(
        self
    ):
        receipt = self.existing()
        self.assertEqual(receipt["cap_bytes"], 250 * 1024**3)
        self.assertLessEqual(receipt["mount"]["bytes"], v.CAP_BYTES)
        self.assertLessEqual(receipt["mount"]["inodes"], v.CAP_INODES)
        self.assertEqual(self.manager.ensure(), receipt)
        self.assertEqual(self.runner.mutations(), [])
        self.assertFalse(any(call[0] == "reserve" for call in self.fs.calls))
        self.assertEqual(self.manager.ensure(create=False), receipt)

    def test_shared_linux_runner_stdout_contract_and_private_new_mount(self):
        self.runner.stdout_only = True
        receipt = self.manager.ensure()
        self.assertEqual(stat.S_IMODE(self.fs.mounted_root.st_mode), 0o700)
        self.assertIn(("chmod", 21, 0o700), self.fs.calls)
        self.assertEqual(self.manager.ensure(), receipt)

    def test_existing_mount_losing_private_mode_is_not_repaired(self):
        self.existing()
        self.fs.mounted_root.st_mode = stat.S_IFDIR | 0o755
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertFalse(any(call[0] == "chmod" for call in self.fs.calls))
        self.assertEqual(self.runner.mutations(), [])

    def test_new_format_uses_inherited_fd_and_nodiscard_and_safe_mount_flags(
        self
    ):
        self.manager.ensure()
        mkfs, timeout, fds = next(
            call for call in self.runner.calls if call[0][0] == v.MKFS
        )
        self.assertEqual(fds, (30, ))
        self.assertEqual(mkfs[-1], "/proc/self/fd/30")
        self.assertEqual(mkfs[mkfs.index("-N") + 1], "1900000")
        self.assertIn("nodiscard", mkfs[mkfs.index("-E") + 1])
        mount = next(
            call[0] for call in self.runner.calls if call[0][0] == v.MOUNT
        )
        self.assertEqual(
            mount[mount.index("--options") + 1], "rw,nosuid,nodev"
        )
        self.assertNotIn("remount", " ".join(mount))

    def test_disabled_or_unapproved_performs_no_adapter_calls(self):
        for key in ("enabled", "persistent_changes_approved"):
            with self.subTest(key=key):
                self.config[key] = False
                with self.assertRaises(v.VolumeError):
                    self.manager.ensure()
                self.assertEqual(self.fs.calls, [])
                self.assertEqual(self.runner.calls, [])
                self.config[key] = True

    def test_unreserved_image_is_never_formatted(self):
        self.fs.reserve_blocks = False
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertEqual(self.runner.mutations(), [])
        self.assertIsNone(self.fs.receipt)

    def test_format_that_loses_reservation_never_attaches_loop(self):
        self.runner.discard_during_mkfs = True
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertEqual([cmd[0] for cmd in self.runner.mutations()], [v.MKFS])

    def test_existing_image_without_receipt_is_never_formatted(self):
        self.fs.image = info(
            102,
            mode=stat.S_IFREG | 0o600,
            size=v.CAP_BYTES,
            blocks=v.CAP_BYTES // 512
        )
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertEqual(self.runner.mutations(), [])

    def test_existing_unknown_mount_and_nonempty_target_remain_untouched(self):
        self.fs.mounted = True
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertEqual(self.runner.mutations(), [])
        self.assertIsNone(self.fs.image)
        self.fs.mounted = False
        self.fs.empty = False
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertIsNone(self.fs.image)

    def test_wrong_loop_backing_device_inode_offset_and_limit_are_rejected(
        self
    ):
        self.existing()
        for changed in ({"back-maj:min": "8:2"}, {"back-ino": 999}, {"offset":
                                                                     4096},
                        {"sizelimit":
                         v.CAP_BYTES + 1}, {"maj:min": "7:13"}, {"ro": True}):
            with self.subTest(changed=changed):
                self.runner.loop_changes = changed
                with self.assertRaises(v.VolumeError):
                    self.manager.ensure()
                self.assertEqual(self.runner.mutations(), [])

    def test_actual_block_and_inode_caps_are_checked(self):
        self.existing()
        original = copy.copy(self.fs.usage)
        for field, value in (("f_blocks", v.CAP_BYTES // 4096 + 1),
                             ("f_files", v.CAP_INODES + 1), ("f_frsize", 0)):
            with self.subTest(field=field):
                self.fs.usage = copy.copy(original)
                setattr(self.fs.usage, field, value)
                with self.assertRaises(v.VolumeError):
                    self.manager.ensure()
                self.assertEqual(self.runner.mutations(), [])

    def test_mount_flags_source_filesystem_and_device_must_match(self):
        self.existing()
        for changed in ({"options": "rw,nodev"}, {"options":
                                                  "rw,nosuid,nodev,dev"},
                        {"fstype":
                         "xfs"}, {"maj:min":
                                  "7:13"}, {"fsroot":
                                            "/subtree"}, {"source":
                                                          "/dev/sda"}):
            with self.subTest(changed=changed):
                self.runner.mount_changes = changed
                with self.assertRaises(v.VolumeError):
                    self.manager.ensure()
                self.assertEqual(self.runner.mutations(), [])

    def test_existing_receipt_identity_mismatch_and_unmounted_state_fail(self):
        self.existing()
        self.fs.receipt["image"]["inode"] += 1
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.fs.mounted = False
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertEqual(self.runner.mutations(), [])

    def test_symlink_hardlink_and_world_writable_images_rejected(self):
        for mode, links in ((stat.S_IFLNK | 0o600, 1),
                            (stat.S_IFREG | 0o600, 2), (stat.S_IFREG | 0o666,
                                                        1)):
            with self.subTest(mode=mode, links=links):
                self.fs.image = info(
                    102,
                    mode=mode,
                    size=v.CAP_BYTES,
                    blocks=v.CAP_BYTES // 512
                )
                self.fs.image.st_nlink = links
                with self.assertRaises(v.VolumeError):
                    self.manager.ensure()
                self.assertEqual(self.runner.mutations(), [])

    def test_symlink_or_foreign_parent_from_fs_stops_before_any_command(self):
        self.fs.directory_error = OSError(
            "injected no-follow parent rejection"
        )
        with self.assertRaises(OSError):
            self.manager.ensure()
        self.assertEqual(self.runner.calls, [])

    def test_nested_mount_and_findmnt_failure_do_not_mean_unmounted(self):
        self.runner.extra_mounts = [{"target": v.MOUNT_TARGET + "/colleague"}]
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.runner.extra_mounts = []
        self.runner.fail = v.FINDMNT
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertIsNone(self.fs.image)
        self.assertEqual(self.runner.mutations(), [])

    def test_verify_does_not_create_and_bad_configuration_has_no_effect(self):
        with self.assertRaises(v.VolumeError):
            self.manager.ensure(create=False)
        self.assertIsNone(self.fs.image)
        self.fs.calls.clear()
        self.runner.calls.clear()
        self.config["workspace_source"] = "/approved/../elsewhere"
        with self.assertRaises(v.VolumeError):
            self.manager.ensure()
        self.assertEqual(self.fs.calls, [])
        self.assertEqual(self.runner.calls, [])


class PosixAdapterTests(unittest.TestCase):

    def test_image_open_refuses_actual_local_symlink_without_touching_target(
        self
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "sentinel"
            sentinel.write_bytes(b"untouched")
            (root / v.IMAGE_NAME).symlink_to(sentinel)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    v.PosixFs().open_image(fd, create=False)
                with self.assertRaises(FileExistsError):
                    v.PosixFs().open_image(fd, create=True)
            finally:
                os.close(fd)
            self.assertEqual(sentinel.read_bytes(), b"untouched")


if __name__ == "__main__":
    unittest.main()
