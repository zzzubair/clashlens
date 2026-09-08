from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from clashlens.archive import ArchiveReadResult, SpoolFirstReader
from clashlens.spool import Spool, SpoolError, validate_root


class Fallback:
    max_body_bytes = 1024

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.bucket = "bucket"
        self.calls = 0

    def set_pool_acquire_observer(self, observer):
        pass

    def read_verified(self, reference: str, expected_hash: str, *, heartbeat=None) -> ArchiveReadResult:
        self.calls += 1
        return ArchiveReadResult(self.body, reference, expected_hash)


def test_spool_first_hit_avoids_remote(tmp_path: Path) -> None:
    body = b"exact response"
    digest = hashlib.sha256(body).hexdigest()
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    spool.publish(body, digest)
    fallback = Fallback(body)
    reader = SpoolFirstReader(
        fallback,
        spool_root=str(tmp_path / "spool"),
        max_bytes=4096,
        max_objects=7,
    )
    assert reader.spool.max_bytes == 4096
    assert reader.spool.max_objects == 7
    assert reader.read_verified(f"s3://bucket/sha256/{digest[:2]}/{digest}", digest).body == body
    assert fallback.calls == 0


def test_spool_first_repairs_missing_file(tmp_path: Path) -> None:
    body = b"repair me"
    digest = hashlib.sha256(body).hexdigest()
    root = tmp_path / "spool"
    fallback = Fallback(body)
    reader = SpoolFirstReader(fallback, spool_root=str(root))
    reader.read_verified(f"s3://bucket/sha256/{digest[:2]}/{digest}", digest)
    assert fallback.calls == 1
    assert reader.spool.verify(digest) == body


def test_spool_repairs_corruption_and_concurrent_writers_converge(tmp_path: Path) -> None:
    body = b"concurrent response"
    digest = hashlib.sha256(body).hexdigest()
    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=1024)
    spool.publish(body, digest)
    final = root / "sha256" / digest[:2] / digest
    final.write_bytes(b"corrupt")
    fallback = Fallback(body)
    reader = SpoolFirstReader(fallback, spool_root=str(root))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: reader.read_verified("s3://bucket/evidence", digest).body, range(4)))
    assert results == [body] * 4
    assert reader.spool.verify(digest) == body
    assert reader.spool.stats()["final_objects"] == 1


def test_spool_root_rejects_root_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        validate_root("/")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_root(link)


def test_publish_rejects_symlinked_hash_prefix_without_touching_outside(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=1024)
    body = b"race-resistant"
    digest = hashlib.sha256(body).hexdigest()
    prefix = root / "sha256" / digest[:2]
    prefix.mkdir()
    prefix.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    prefix.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, SpoolError)):
        spool.publish(body, digest)
    assert list(outside.iterdir()) == []


def test_reservation_descriptor_holds_exclusive_flock(tmp_path: Path) -> None:
    """The live reservation record is locked by its descriptor for its whole
    lifetime; an outside process must see the exclusive flock (Go treats an
    unlocked record as crash debris and would delete it)."""
    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=4096)
    fd, reservation_name = spool._reserve(1024)
    path = spool.root / ".control" / "reservations" / reservation_name
    try:
        holder = textwrap.dedent(
            """
            import fcntl, os, sys
            fd = os.open(sys.argv[1], os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("locked")
            else:
                print("unlocked")
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", holder, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.stdout.strip() == "locked"
    finally:
        spool._release(fd, path)
    # After release the record is gone entirely.
    assert not path.exists()


def test_reserve_sweeps_dead_reservations_and_enforces_floors(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    max_body = 2048
    spool = Spool(root, max_body_bytes=max_body, max_bytes=4096)
    # Plant a dead reservation (no flock) with a stale inflated ledger.
    dead = root / ".control" / "reservations" / "dead-process.json"
    dead.write_text(json.dumps({"limit": 3000}), encoding="utf-8")
    ledger_path = root / ".control" / "capacity.json"
    ledger_path.write_text(json.dumps({"reserved_bytes": 3000, "reserved_objects": 1}), encoding="utf-8")
    fd, path = spool._reserve(max_body)
    try:
        assert not dead.exists(), "dead unlocked reservation was not reconciled"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert ledger["reserved_bytes"] == max_body
        assert ledger["reserved_objects"] == 1
    finally:
        spool._release(fd, path)


def test_reserve_enforces_free_space_floor(tmp_path: Path) -> None:
    from clashlens.spool import SpoolError

    root = tmp_path / "spool"
    filesystem = os.statvfs(tmp_path)
    huge_floor = filesystem.f_bavail * filesystem.f_frsize * 10
    spool = Spool(root, max_body_bytes=1024, free_space_floor=huge_floor)
    with pytest.raises(SpoolError, match="free-space floor"):
        spool._reserve(512)
    inode_floor = filesystem.f_favail + 1000
    spool_inodes = Spool(tmp_path / "spool2", max_body_bytes=1024, free_inode_floor=inode_floor)
    with pytest.raises(SpoolError, match="free-inode floor"):
        spool_inodes._reserve(512)


def test_stripe_lock_interoperates_with_external_flock_holder(tmp_path: Path) -> None:
    """Cross-runtime contract: an external process holding flock LOCK_EX on a
    stripe file blocks the Python reader on the same inode."""
    root = tmp_path / "spool"
    body = b"interop"
    digest = hashlib.sha256(body).hexdigest()
    spool = Spool(root, max_body_bytes=64)
    stripe = root / ".locks" / f"{int(digest[:3], 16) & 0xFFF:04x}"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import fcntl, os, sys, time
                fd = os.open(sys.argv[1], os.O_RDWR)
                fcntl.flock(fd, fcntl.LOCK_EX)
                print("held", flush=True)
                time.sleep(float(sys.argv[2]))
                """
            ),
            str(stripe),
            "1.5",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        start = time.monotonic()
        with spool.lock(digest):  # shared lock must wait for the external writer
            held_for = time.monotonic() - start
        assert held_for > 0.5, "stripe lock did not exclude the external flock holder"
    finally:
        holder.wait(timeout=30)


def test_spool_rejects_symlink_substitution_race(tmp_path: Path) -> None:
    """A directory swapped for a symlink must never be followed."""
    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=4096)
    body = b"substitution target"
    digest = hashlib.sha256(body).hexdigest()
    spool.publish(body, digest)
    assert spool.verify(digest) == body

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    prefix = root / "sha256" / digest[:2]
    prefix.rename(prefix.with_name("real"))
    prefix.symlink_to(outside, target_is_directory=True)

    # Reads through the substituted path refuse instead of following.
    with pytest.raises(SpoolError):
        spool.verify(digest)
    # The outside directory was never populated through the link.
    assert not any(outside.iterdir())


def _substituted_reopen(tmp_path: Path, control: str) -> None:
    """Close a spool, swap one trusted top-level directory for a symlink to an
    outside directory, and prove reopening refuses and never populates it."""
    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=1024)
    body = b"substitution guard"
    digest = hashlib.sha256(body).hexdigest()
    spool.publish(body, digest)
    spool.close()

    outside = tmp_path / f"outside-{control.strip('.')}"
    outside.mkdir(mode=0o700)
    swapped = root / control
    swapped.rename(swapped.with_name("real"))
    swapped.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises((OSError, ValueError, SpoolError)):
            Spool(root, max_body_bytes=1024)
        assert not any(outside.iterdir())
    finally:
        swapped.unlink()
        swapped.with_name("real").rename(swapped)


def test_reopen_rejects_symlinked_control_directory(tmp_path: Path) -> None:
    _substituted_reopen(tmp_path, ".control")


def test_reopen_rejects_symlinked_lock_directory(tmp_path: Path) -> None:
    _substituted_reopen(tmp_path, ".locks")


def _capacity(filesystem_type: str, inode_model: str, free_inodes: int = 1000, free_bytes: int = 10_000_000) -> dict:
    return {
        "filesystem_type": filesystem_type,
        "inode_model": inode_model,
        "free_inodes": free_inodes,
        "free_bytes": free_bytes,
        "inode_total": 1000 if inode_model == "finite" else 0,
        "block_size": 4096,
    }


def test_btrfs_zero_inodes_passes_inode_gate_but_not_byte_or_object_limits(tmp_path: Path) -> None:
    from unittest import mock

    from clashlens import spool as spool_module

    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=1024, max_bytes=4096, free_inode_floor=10000, free_space_floor=1000)
    btrfs = _capacity("btrfs", "dynamic", free_inodes=0)
    with mock.patch.object(spool_module, "filesystem_capacity", return_value=btrfs):
        stats = spool.stats()
        assert stats["inode_model"] == "dynamic"
        assert stats["free_inodes"] == 0
        assert spool.readiness()[0] is True
        fd, name = spool._reserve(512)
        spool._release(fd, spool.root / ".control" / "reservations" / name)
    # Low free bytes and exhausted logical limits still block Btrfs.
    tiny = _capacity("btrfs", "dynamic", free_inodes=0, free_bytes=10)
    with mock.patch.object(spool_module, "filesystem_capacity", return_value=tiny):
        assert spool.readiness()[1] == "degraded_free_space"
        with pytest.raises(SpoolError, match="free-space floor"):
            spool._reserve(512)
    full = Spool(tmp_path / "full", max_body_bytes=1024, max_bytes=10, max_objects=1)
    with mock.patch.object(spool_module, "filesystem_capacity", return_value=btrfs):
        with pytest.raises(SpoolError, match="degraded_capacity"):
            full._reserve(1024)


def test_unknown_and_failed_capacity_do_not_admit(tmp_path: Path) -> None:
    from unittest import mock

    from clashlens import spool as spool_module

    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=1024)
    for facts in (
        _capacity("other", "unknown", free_inodes=0),
        _capacity("unknown", "unknown", free_inodes=0),
    ):
        with mock.patch.object(spool_module, "filesystem_capacity", return_value=facts):
            with pytest.raises(SpoolError, match="unknown"):
                spool._reserve(512)
            assert spool.readiness() == (False, "degraded_capacity")
    with mock.patch.object(spool_module, "filesystem_capacity", side_effect=OSError("statvfs failed")):
        with pytest.raises(OSError):
            spool._reserve(512)
        ready, reason = spool.readiness()
        assert ready is False and reason.startswith("storage_error:")


def test_finite_zero_floor_still_requires_one_inode(tmp_path: Path) -> None:
    from unittest import mock

    from clashlens import spool as spool_module

    root = tmp_path / "spool"
    spool = Spool(root, max_body_bytes=1024, free_inode_floor=0)
    exhausted = _capacity("ext4", "finite", free_inodes=0)
    with mock.patch.object(spool_module, "filesystem_capacity", return_value=exhausted):
        with pytest.raises(SpoolError, match="free-inode floor"):
            spool._reserve(512)
        assert spool.readiness() == (False, "degraded_free_inodes")


def test_mountinfo_parsing_covers_root_nested_escaped_and_prefix_collision(tmp_path: Path) -> None:
    from clashlens.filesystem import (
        _is_prefix,
        _match_mount,
        _parse_mountinfo_line,
        classify_inode_model,
    )

    # Root mount with ID and options retained for evidence.
    parsed = _parse_mountinfo_line("1 0 8:1 / / rw - ext4 /dev/sda rw")
    assert parsed and parsed["mount_point"] == "/" and parsed["filesystem_type"] == "ext4"
    assert parsed["mount_id"] == 1 and parsed["options"] == "rw"
    # Nested mount and escaped space decode.
    parsed = _parse_mountinfo_line(r"2 1 8:1 / /mnt/my\040disk rw - ext4 /dev/sda rw")
    assert parsed and parsed["mount_point"] == "/mnt/my disk"
    # Malformed lines are unknown, not exceptions.
    assert _parse_mountinfo_line("malformed line") is None
    assert _parse_mountinfo_line("1 0 8:1 / rw - ext4") is None
    assert _parse_mountinfo_line("x y 8:1 / / rw - ext4 /dev/sda rw") is None
    # Component-aware prefix: /mnt/a never matches /mnt/ab/c.
    assert _is_prefix("/", "/anything") is True
    assert _is_prefix("/mnt/a", "/mnt/a/b") is True
    assert _is_prefix("/mnt/a", "/mnt/a") is True
    assert _is_prefix("/mnt/a", "/mnt/ab/c") is False
    # Strict mnt_id mapping with most-specific cross-check.
    mounts = [
        {"mount_id": 1, "mount_point": "/", "filesystem_type": "ext4", "source": "/dev/sda", "major_minor": "8:1", "options": "rw"},
        {"mount_id": 2, "mount_point": "/mnt/data", "filesystem_type": "ext4", "source": "/dev/sdb", "major_minor": "8:2", "options": "rw"},
    ]
    assert _match_mount("/mnt/data/file", 2, mounts)["mount_id"] == 2
    # Root remains the most-specific match when no nested mount applies.
    assert _match_mount("/mnt/ab/c", 1, mounts)["mount_id"] == 1
    # Prefix collision: /mnt/a never matches /mnt/ab/c, even with its mnt_id.
    collision = [
        {"mount_id": 1, "mount_point": "/", "filesystem_type": "ext4", "source": "/dev/sda", "major_minor": "8:1", "options": "rw"},
        {"mount_id": 3, "mount_point": "/mnt/a", "filesystem_type": "ext4", "source": "/dev/sdc", "major_minor": "8:3", "options": "rw"},
    ]
    assert _match_mount("/mnt/ab/c", 3, collision) is None
    # Missing, duplicate, and non-prefix identities are unknown.
    assert _match_mount("/mnt/data/file", 99, mounts) is None
    dup = mounts + [{"mount_id": 2, "mount_point": "/other", "filesystem_type": "xfs", "source": "/dev/x", "major_minor": "8:3", "options": "rw"}]
    assert _match_mount("/mnt/data/file", 2, dup) is None
    assert _match_mount("/elsewhere", 2, mounts) is None
    # Ambiguous same-length bind mounts are unknown.
    bind = [
        {"mount_id": 10, "mount_point": "/mnt/bind", "filesystem_type": "ext4", "source": "/dev/sda", "major_minor": "8:1", "options": "rw"},
        {"mount_id": 11, "mount_point": "/mnt/bind", "filesystem_type": "xfs", "source": "/dev/sdb", "major_minor": "8:2", "options": "rw"},
    ]
    assert _match_mount("/mnt/bind/file", 10, bind) is None
    # Pure classifier agrees with Go on representative cases.
    assert classify_inode_model("btrfs", 0, 0) == "dynamic"
    assert classify_inode_model("ext4", 1000, 999) == "finite"
    assert classify_inode_model("ext4", 1000, 0) == "finite"
    assert classify_inode_model("other", 0, 0) == "unknown"
    assert classify_inode_model("other", 1000, 1001) == "unknown"
    assert classify_inode_model("unknown", 1000, 10) == "finite"
    assert classify_inode_model("other", (1 << 64) - 1, (1 << 64) - 1) == "unknown"


def test_fdinfo_mnt_id_parser_rejects_duplicates_malformed_and_missing() -> None:
    from clashlens.filesystem import _parse_fdinfo_mnt_id_text

    assert _parse_fdinfo_mnt_id_text("pos:\t0\nflags:\t0\nmnt_id:\t21\n") == 21
    assert _parse_fdinfo_mnt_id_text("pos:\t0\nflags:\t0\n") is None
    assert _parse_fdinfo_mnt_id_text("mnt_id:\t\n") is None
    assert _parse_fdinfo_mnt_id_text("mnt_id:\tabc\n") is None
    assert _parse_fdinfo_mnt_id_text("mnt_id:\t21\nmnt_id:\t22\n") is None
    assert _parse_fdinfo_mnt_id_text("mnt_id:\t21\nmnt_id:\t21\n") is None


def _fake_statvfs(files=1000, favail=900, bavail=1000, frsize=4096):
    return type("V", (), {"f_files": files, "f_favail": favail, "f_bavail": bavail, "f_frsize": frsize})()


def _fake_stat(dev):
    return type("S", (), {"st_dev": dev})()


def test_btrfs_anon_dev_with_matching_mnt_id_is_dynamic() -> None:
    from unittest import mock

    import clashlens.filesystem as fs

    mounts = [
        {"mount_id": 10, "mount_point": "/", "filesystem_type": "ext4", "source": "/dev/sda", "major_minor": "8:1", "options": "rw"},
        {"mount_id": 20, "mount_point": "/mnt/btrfs", "filesystem_type": "btrfs", "source": "/dev/sdb", "major_minor": "8:2", "options": "rw"},
    ]
    # Btrfs getattr anon_dev (0:100) differs from mountinfo s_dev (8:2),
    # yet the verified mnt_id mapping must still grant dynamic.
    with (
        mock.patch.object(fs, "_read_fd_mnt_id", return_value=20),
        mock.patch.object(fs, "_read_mounts", return_value=mounts),
        mock.patch.object(fs.os, "fstat", return_value=_fake_stat(os.makedev(0, 100))),
    ):
        assert fs._identify_via_fd("/mnt/btrfs/file", 99) == "btrfs"


def test_nested_btrfs_subvolume_resolves_most_specific() -> None:
    from unittest import mock

    import clashlens.filesystem as fs

    mounts = [
        {"mount_id": 20, "mount_point": "/mnt/btrfs", "filesystem_type": "btrfs", "source": "/dev/sdb", "major_minor": "8:2", "options": "rw"},
        {"mount_id": 21, "mount_point": "/mnt/btrfs/subvol", "filesystem_type": "btrfs", "source": "/dev/sdb", "major_minor": "8:2", "options": "rw"},
    ]
    with (
        mock.patch.object(fs, "_read_fd_mnt_id", return_value=21),
        mock.patch.object(fs, "_read_mounts", return_value=mounts),
    ):
        assert fs._identify_via_fd("/mnt/btrfs/subvol/file", 99) == "btrfs"
    # Stale parent mnt_id for a nested path is contradictory, not dynamic.
    with (
        mock.patch.object(fs, "_read_fd_mnt_id", return_value=20),
        mock.patch.object(fs, "_read_mounts", return_value=mounts),
    ):
        assert fs._identify_via_fd("/mnt/btrfs/subvol/file", 99) == "unknown"


def test_fdinfo_mountinfo_failure_mismatch_ambiguity_are_unknown() -> None:
    from unittest import mock

    import clashlens.filesystem as fs

    mounts = [
        {"mount_id": 1, "mount_point": "/", "filesystem_type": "ext4", "source": "/dev/sda", "major_minor": "8:1", "options": "rw"},
    ]
    with mock.patch.object(fs, "_read_fd_mnt_id", return_value=None):
        assert fs._identify_via_fd("/file", 99) == "unknown"
    with (
        mock.patch.object(fs, "_read_fd_mnt_id", return_value=1),
        mock.patch.object(fs, "_read_mounts", return_value=None),
    ):
        assert fs._identify_via_fd("/file", 99) == "unknown"
    with (
        mock.patch.object(fs, "_read_fd_mnt_id", return_value=99),
        mock.patch.object(fs, "_read_mounts", return_value=mounts),
    ):
        assert fs._identify_via_fd("/file", 99) == "unknown"


def test_non_btrfs_device_mismatch_falls_back_to_finite(tmp_path: Path) -> None:
    from unittest import mock

    import clashlens.filesystem as fs

    target = tmp_path / "spool"
    target.mkdir()
    mounts = [
        {"mount_id": 1, "mount_point": "/", "filesystem_type": "ext4", "source": "/dev/sda", "major_minor": "8:1", "options": "rw"},
    ]
    real_stat = os.stat(target)
    mismatched_dev = os.makedev(os.major(real_stat.st_dev) + 100, os.minor(real_stat.st_dev))
    opened: list[int] = []
    real_open = os.open
    real_close = os.close

    def fake_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd

    with (
        mock.patch.object(fs.os.path, "realpath", return_value=str(target)),
        mock.patch.object(fs.os, "open", side_effect=fake_open),
        mock.patch.object(fs.os, "fstatvfs", return_value=_fake_statvfs(files=1000, favail=900)),
        mock.patch.object(fs, "_read_fd_mnt_id", return_value=1),
        mock.patch.object(fs, "_read_mounts", return_value=mounts),
        mock.patch.object(fs.os, "fstat", return_value=_fake_stat(mismatched_dev)),
        mock.patch.object(fs.os, "close", side_effect=real_close) as close_mock,
    ):
        capacity = fs.filesystem_capacity(target)
        assert capacity["filesystem_type"] == "unknown"
        assert capacity["inode_model"] == "finite"
        assert capacity["free_inodes"] == 900
    assert close_mock.called
    for fd in opened:
        try:
            real_close(fd)
        except OSError:
            pass


def test_symlink_resolution_opens_real_target(tmp_path: Path) -> None:
    from unittest import mock

    import clashlens.filesystem as fs

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    seen: dict[str, str] = {}
    real_open = os.open

    def fake_open(path, flags, *args):
        seen["path"] = path
        return real_open(path, flags, *args)

    with (
        mock.patch.object(fs.os, "open", side_effect=fake_open),
    ):
        capacity = fs.filesystem_capacity(link)
        assert seen["path"] == str(real)
        assert capacity["inode_model"] in ("finite", "dynamic", "unknown")


def test_descriptors_close_and_probe_failures_propagate(tmp_path: Path) -> None:
    from unittest import mock

    import clashlens.filesystem as fs

    target = tmp_path / "spool"
    target.mkdir()
    # open failure propagates without a descriptor to close.
    with mock.patch.object(fs.os, "open", side_effect=OSError("open failed")):
        try:
            fs.filesystem_capacity(target)
            assert False, "open failure must propagate"
        except OSError:
            pass
    # fstatvfs failure propagates and the descriptor still closes.
    real_close = os.close
    with mock.patch.object(fs.os, "close", side_effect=real_close) as close_mock:
        with mock.patch.object(fs.os, "fstatvfs", side_effect=OSError("fstatvfs failed")):
            try:
                fs.filesystem_capacity(target)
                assert False, "fstatvfs failure must propagate"
            except OSError:
                pass
        assert close_mock.called
    # mount_facts open failure propagates for host incomplete handling.
    with mock.patch.object(fs.os, "open", side_effect=OSError("open failed")):
        try:
            fs.mount_facts(target)
            assert False, "mount open failure must propagate"
        except OSError:
            pass
