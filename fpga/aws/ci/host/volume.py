"""One bounded Linux filesystem for the entire three-admission pilot.

This module is installed operator code, never imported from PR source. Call only
under the shared host lock and an approved scheduling permit. All actual system
operations are injectable; local tests never reserve storage or invoke utilities.
There is no formatting/recovery path for an existing image, automatic remount,
detach, deletion, or modification of another filesystem. Interrupted provisioning
fails closed until an operator reconciles the private image and kernel state.

CLI: volume.py ensure | verify. The fixed root-private config must be enabled;
there are no command-line image, mount, size, owner, or config-path overrides.
"""
import argparse
import json
import os
import re
import stat
import subprocess

from secure_files import (
    open_trusted_directory, read_private_json, create_private_json, READ_FLAGS
)

CAP_BYTES = 250 * 1024**3
CAP_INODES = 2_000_000
FORMAT_INODES = 1_900_000
MOUNT_TARGET = "/srv/coralnpu-ci"
IMAGE_NAME = "coralnpu-ci-volume.ext4"
RECEIPT_NAME = "coralnpu-ci-volume.json"
FINDMNT = "/usr/bin/findmnt"
LOSETUP = "/usr/sbin/losetup"
MKFS = "/usr/sbin/mkfs.ext4"
MOUNT = "/usr/bin/mount"
TABLE_LIMIT = 1_048_576


class VolumeError(ValueError):
    """The dedicated filesystem cannot be safely admitted or provisioned."""


def _identity(info):
    return {"device": info.st_dev, "inode": info.st_ino}


def _major_minor(device):
    return f"{os.major(device)}:{os.minor(device)}"


def _positive_int(value):
    return type(value) is int and value > 0


class PosixFs:
    """Production Linux filesystem adapter. Does not run commands."""

    def open_directory(self, path, *, owner, private=False):
        return open_trusted_directory(
            path, expected_owner_uid=owner, private=private
        )

    def lstat(self, directory_fd, name):
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def open_image(self, directory_fd, *, create):
        flags = (
            os.O_RDWR if create else os.O_RDONLY
        ) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        return os.open(IMAGE_NAME, flags, 0o600, dir_fd=directory_fd)

    def reserve(self, fd, size):
        # ftruncate and sparse-file creation are deliberately not fallbacks.
        if not hasattr(os, "posix_fallocate"):
            raise VolumeError("Linux posix_fallocate is required")
        os.posix_fallocate(fd, 0, size)

    def open_loop(self, path, *, owner):
        if not re.fullmatch(r"/dev/loop(?:0|[1-9][0-9]{0,5})", path):
            raise VolumeError("invalid loop device path")
        dev_fd = open_trusted_directory("/dev", expected_owner_uid=owner)
        try:
            return os.open(path.rsplit("/", 1)[1], READ_FLAGS, dir_fd=dev_fd)
        finally:
            os.close(dev_fd)

    def is_empty(self, directory_fd):
        with os.scandir(directory_fd) as entries:
            return next(entries, None) is None

    def read_receipt(self, directory_fd, *, owner):
        return read_private_json(
            directory_fd, RECEIPT_NAME, expected_owner_uid=owner
        )

    def write_receipt(self, directory_fd, value, *, owner):
        return create_private_json(
            directory_fd, RECEIPT_NAME, value, expected_owner_uid=owner
        )

    fstat = staticmethod(os.fstat)
    statvfs = staticmethod(os.fstatvfs)
    fchmod = staticmethod(os.fchmod)
    sync = staticmethod(os.fsync)
    close = staticmethod(os.close)


class LocalRunner:
    """Fixed executable argv, clean environment, bounded trusted utility output."""

    def run(self, argv, *, timeout, pass_fds=()):
        # Utilities are trusted and receive only fixed paths or open FDs. Limits
        # prevent runaway utility output from becoming an unbounded host log.
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              pass_fds=pass_fds,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                                   "LC_ALL": "C"}) as process:
            import selectors
            import time
            chunks, total = [], 0
            end = time.monotonic() + timeout
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                "volume utility exceeded timeout"
                            )
                        for key, _ in selector.select(min(remaining, 1)):
                            block = os.read(key.fileobj.fileno(), 65536)
                            if not block:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(block)
                            if total > TABLE_LIMIT:
                                raise VolumeError(
                                    "volume utility output exceeded limit"
                                )
                            chunks.append(block)
                code = process.wait(timeout=max(0.001, end - time.monotonic()))
                return subprocess.CompletedProcess(
                    argv, code, b"".join(chunks).decode("utf-8", "strict")
                )
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                raise


class BoundedVolume:

    def __init__(self, config, runner, *, fs=None, expected_owner_uid=0):
        self.config, self.runner = config, runner
        self.fs = PosixFs() if fs is None else fs
        self.owner = expected_owner_uid

    def _enabled(self):
        config = self.config
        # This guard intentionally precedes every adapter call, even reads.
        if config.get("enabled") is not True or config.get(
                "persistent_changes_approved") is not True:
            raise VolumeError("volume activation is disabled or unapproved")
        if type(self.owner) is not int or self.owner < 0:
            raise VolumeError("invalid trusted owner")
        for key, value in (("per_run_disk_bytes", CAP_BYTES),
                           ("per_run_inode_limit", CAP_INODES)):
            if type(config.get(key)) is not int or config[key] != value:
                raise VolumeError(
                    "reviewed shared filesystem bound differs: " + key
                )
        if (config.get("workspace_bind_target") != MOUNT_TARGET or config.get(
                "loop_filesystem_root", MOUNT_TARGET) != MOUNT_TARGET):
            raise VolumeError(
                "mount target must be the fixed dedicated directory"
            )
        source = config.get("workspace_source")
        if (not isinstance(source, str) or not source.startswith("/")
                or any(part in {"", ".", ".."}
                       for part in source[1:].split("/"))
                or any(c in source
                       for c in ("\x00", "\n", "\r")) or source == MOUNT_TARGET
                or source.startswith(MOUNT_TARGET + "/")):
            raise VolumeError(
                "approved private workspace source is unresolved or invalid"
            )
        return source

    def _run(self, argv, *, timeout=30, pass_fds=()):
        result = self.runner.run(argv, timeout=timeout, pass_fds=pass_fds)
        # The shared Linux adapter raises on failure and returns decoded stdout;
        # standalone CLI and simple injected runners may return CompletedProcess.
        if not isinstance(result, str) and result.returncode != 0:
            # Do not echo command output or private host path data into receipts.
            raise VolumeError(
                "trusted volume utility failed: " + os.path.basename(argv[0])
            )
        output = result if isinstance(result, str) else result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", "strict")
        if not isinstance(output, str) or len(output.encode("utf-8")
                                              ) > TABLE_LIMIT:
            raise VolumeError("invalid or excessive utility output")
        return output

    def _mount_record(self):
        raw = self._run([
            FINDMNT, "--json", "--list", "--output",
            "TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN,FSROOT"
        ])
        table = json.loads(raw)
        rows = table.get("filesystems") if isinstance(table, dict) else None
        if not isinstance(rows, list) or not rows or len(rows) > 4096:
            raise VolumeError("invalid mount table")
        matches = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("target"),
                                                           str):
                raise VolumeError("invalid mount table entry")
            if row["target"].startswith(MOUNT_TARGET + "/"):
                raise VolumeError(
                    "unexpected nested mount in dedicated filesystem"
                )
            if row["target"] == MOUNT_TARGET:
                matches.append(row)
        if len(matches) > 1:
            raise VolumeError("multiple mounts cover the fixed target")
        return matches[0] if matches else None

    def _image(self, info, *, complete=True):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != self.owner
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise VolumeError(
                "backing image must be private owned single-link regular file"
            )
        if complete and (info.st_size != CAP_BYTES
                         or info.st_blocks * 512 < CAP_BYTES):
            raise VolumeError(
                "backing image is not fully reserved at the fixed size"
            )
        if not complete and info.st_size != 0:
            raise VolumeError("new image is unexpectedly nonempty")
        return {
            **_identity(info), "bytes": info.st_size,
            "allocated_bytes": info.st_blocks * 512
        }

    def _pinned_image(self, source_fd, image_fd):
        info = self.fs.fstat(image_fd)
        value = self._image(info)
        named = self.fs.lstat(source_fd, IMAGE_NAME)
        if named is None or self._image(named) != value:
            raise VolumeError(
                "backing image path no longer identifies its pinned FD"
            )
        if info.st_dev != self.fs.fstat(source_fd).st_dev:
            raise VolumeError(
                "backing image escaped approved scratch filesystem"
            )
        return value

    def _loop(self, device, image):
        if not isinstance(device, str) or not re.fullmatch(
                r"/dev/loop(?:0|[1-9][0-9]{0,5})", device):
            raise VolumeError("unexpected loop device")
        raw = self._run([
            LOSETUP, "--json", "--list", "--output",
            "NAME,BACK-INO,BACK-MAJ:MIN,MAJ:MIN,SIZELIMIT,OFFSET,RO,PARTSCAN",
            device
        ])
        value = json.loads(raw)
        rows = value.get("loopdevices") if isinstance(value, dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(
                rows[0], dict):
            raise VolumeError("invalid loop device metadata")
        row = rows[0]
        if (row.get("name") != device or type(row.get("back-ino")) is not int
                or row["back-ino"] != image["inode"]
                or row.get("back-maj:min") != _major_minor(image["device"])
                or type(row.get("sizelimit")) is not int
                or row["sizelimit"] != CAP_BYTES
                or type(row.get("offset")) is not int or row["offset"] != 0
                or row.get("ro") is not False
                or row.get("partscan") is not False):
            raise VolumeError(
                "loop device does not exclusively describe the pinned bounded image"
            )
        fd = self.fs.open_loop(device, owner=self.owner)
        try:
            info = self.fs.fstat(fd)
            if (not stat.S_ISBLK(info.st_mode) or info.st_uid != self.owner
                    or os.major(info.st_rdev) != 7
                    or os.minor(info.st_rdev) != int(device[len("/dev/loop"):])
                    or row.get("maj:min") != _major_minor(info.st_rdev)):
                raise VolumeError(
                    "loop node identity or device number differs"
                )
            return {
                "path": device,
                "device": info.st_rdev,
                "backing_device": image["device"],
                "backing_inode": image["inode"],
                "size_limit": CAP_BYTES,
                "offset": 0
            }
        finally:
            self.fs.close(fd)

    def _measure(self, record, mounted_fd, loop, *, private=True):
        if (record is None or record.get("target") != MOUNT_TARGET
                or record.get("source") != loop["path"]
                or record.get("fstype") != "ext4"
                or record.get("fsroot") != "/"
                or record.get("maj:min") != _major_minor(loop["device"])
                or not isinstance(record.get("options"), str)):
            raise VolumeError(
                "mount source, root, device or filesystem differs"
            )
        options = set(record["options"].split(","))
        if not {"rw", "nosuid", "nodev"
                } <= options or options & {"ro", "suid", "dev", "bind"}:
            raise VolumeError("dedicated mount lacks required isolation flags")
        info = self.fs.fstat(mounted_fd)
        if (info.st_dev != loop["device"] or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != self.owner
                or info.st_mode & (0o077 if private else 0o022)):
            raise VolumeError(
                "mounted directory identity or ownership differs"
            )
        usage = self.fs.statvfs(mounted_fd)
        if (not all(_positive_int(x)
                    for x in (usage.f_blocks, usage.f_frsize, usage.f_files))
                or usage.f_blocks * usage.f_frsize > CAP_BYTES
                or usage.f_files > CAP_INODES):
            raise VolumeError(
                "actual filesystem exceeds the block or inode cap"
            )
        return {
            **_identity(info), "target": MOUNT_TARGET,
            "fstype": "ext4",
            "options": sorted(options),
            "bytes": usage.f_blocks * usage.f_frsize,
            "inodes": usage.f_files
        }

    def ensure(self, *, create=True):
        source = self._enabled()
        if type(create) is not bool:
            raise VolumeError("create must be a trusted boolean")
        fds = []
        try:
            source_fd = self.fs.open_directory(
                source, owner=self.owner, private=True
            )
            fds.append(source_fd)
            target_fd = self.fs.open_directory(MOUNT_TARGET, owner=self.owner)
            fds.append(target_fd)
            source_identity = _identity(self.fs.fstat(source_fd))
            image_info = self.fs.lstat(source_fd, IMAGE_NAME)
            receipt_info = self.fs.lstat(source_fd, RECEIPT_NAME)
            record = self._mount_record()
            if image_info is not None:
                self._image(image_info)
                if receipt_info is None:
                    raise VolumeError(
                        "existing image has no qualification receipt; operator reconciliation required"
                    )
                if record is None:
                    raise VolumeError(
                        "recorded volume is unmounted; operator reconciliation required"
                    )
                receipt = self.fs.read_receipt(source_fd, owner=self.owner)
                if not isinstance(receipt,
                                  dict) or receipt.get("version") != 1:
                    raise VolumeError("invalid volume receipt")
                image_fd = self.fs.open_image(source_fd, create=False)
                fds.append(image_fd)
                image = self._pinned_image(source_fd, image_fd)
                loop = self._loop(record.get("source"), image)
                mount = self._measure(record, target_fd, loop)
                expected = {
                    "version": 1,
                    "source_directory": source_identity,
                    "image": image,
                    "loop": loop,
                    "mount": mount,
                    "cap_bytes": CAP_BYTES,
                    "cap_inodes": CAP_INODES
                }
                if receipt != expected:
                    raise VolumeError(
                        "existing mount or filesystem identity differs from receipt"
                    )
                return expected
            if receipt_info is not None or record is not None:
                raise VolumeError(
                    "unrecognized receipt or existing mount; no storage changes permitted"
                )
            if not create:
                raise VolumeError(
                    "dedicated bounded volume has not been provisioned"
                )
            if not self.fs.is_empty(target_fd):
                raise VolumeError(
                    "fixed mount directory must be empty before provisioning"
                )
            image_fd = self.fs.open_image(source_fd, create=True)
            fds.append(image_fd)
            self.fs.fchmod(image_fd, 0o600)
            self._image(self.fs.fstat(image_fd), complete=False)
            self.fs.reserve(image_fd, CAP_BYTES)
            self.fs.sync(image_fd)
            self.fs.sync(source_fd)
            image = self._pinned_image(source_fd, image_fd)
            image_path = f"/proc/self/fd/{image_fd}"
            # nodiscard prevents mkfs from punching holes in the reserved image.
            self._run([
                MKFS, "-F", "-q", "-b", "4096", "-N",
                str(FORMAT_INODES), "-m", "0", "-E",
                "nodiscard,lazy_itable_init=0,lazy_journal_init=0", image_path
            ],
                      timeout=900,
                      pass_fds=(image_fd, ))
            self.fs.sync(image_fd)
            image = self._pinned_image(source_fd, image_fd)
            device = self._run([
                LOSETUP, "--find", "--show", "--nooverlap", "--sizelimit",
                str(CAP_BYTES), image_path
            ],
                               pass_fds=(image_fd, )).strip()
            loop = self._loop(device, image)
            # Parents and target are root-trusted; no guest has run or can replace
            # either mount source. Recheck an absent mount before this one action.
            if self._mount_record(
            ) is not None or not self.fs.is_empty(target_fd):
                raise VolumeError("mount target changed during provisioning")
            self._run([
                MOUNT, "--types", "ext4", "--options", "rw,nosuid,nodev",
                "--source", device, "--target", MOUNT_TARGET
            ],
                      timeout=60)
            mounted_fd = self.fs.open_directory(MOUNT_TARGET, owner=self.owner)
            fds.append(mounted_fd)
            image = self._pinned_image(source_fd, image_fd)
            loop = self._loop(device, image)
            record = self._mount_record()
            # A new ext4 root starts 0755. Prove this is our exact new mount
            # before changing its mode through the pinned root directory FD.
            self._measure(record, mounted_fd, loop, private=False)
            self.fs.fchmod(mounted_fd, 0o700)
            self.fs.sync(mounted_fd)
            mount = self._measure(record, mounted_fd, loop)
            receipt = {
                "version": 1,
                "source_directory": source_identity,
                "image": image,
                "loop": loop,
                "mount": mount,
                "cap_bytes": CAP_BYTES,
                "cap_inodes": CAP_INODES
            }
            self.fs.write_receipt(source_fd, receipt, owner=self.owner)
            return receipt
        finally:
            for fd in reversed(fds):
                self.fs.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify or initialize the single approved CI volume"
    )
    parser.add_argument("operation", choices=("ensure", "verify"))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise VolumeError("volume CLI must run as root")
    fd = open_trusted_directory("/etc/coralnpu-ci", private=True)
    try:
        config = read_private_json(fd, "config.json")
    finally:
        os.close(fd)
    result = BoundedVolume(config, LocalRunner()).ensure(
        create=args.operation == "ensure"
    )
    print(
        json.dumps({
            "verified": True,
            "cap_bytes": result["cap_bytes"],
            "cap_inodes": result["cap_inodes"]
        },
                   sort_keys=True)
    )


if __name__ == "__main__":
    main()
