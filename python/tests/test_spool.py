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
