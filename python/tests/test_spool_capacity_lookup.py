from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from clashlens import filesystem
from clashlens.archive import ArchiveReadError, ArchiveReadResult, SpoolFirstReader
from clashlens.spool import Spool, SpoolError
from clashlens.worker import MAX_CONCURRENCY


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def test_free_space_is_still_checked_after_the_filesystem_is_known(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024, free_space_floor=1 << 20)
    spool.reserve(512).release()
    full = type(
        "V", (), {"f_files": 1000, "f_favail": 900, "f_bavail": 0, "f_frsize": 4096}
    )()
    with mock.patch.object(filesystem.os, "fstatvfs", return_value=full):
        with pytest.raises(SpoolError, match="free-space floor"):
            spool.reserve(512)
    spool.reserve(512).release()


def test_a_repair_counts_the_files_another_process_removed(tmp_path: Path) -> None:
    # The worker saves an archived copy back while collector cleanup, a
    # separate process, removes uploaded files from the same folder.
    root = tmp_path / "spool"
    collector = Spool(root, max_body_bytes=1024, max_objects=2)
    saved = [b"first response", b"second response"]
    for body in saved:
        collector.publish(body, hashlib.sha256(body).hexdigest())
    worker = Spool(root, max_body_bytes=1024, max_objects=2)
    for body in saved:
        assert collector.delete(hashlib.sha256(body).hexdigest())

    repaired = b"archived response"
    worker.publish(repaired, hashlib.sha256(repaired).hexdigest())

    assert worker.verify(hashlib.sha256(repaired).hexdigest()) == repaired


def test_repairs_may_pass_the_folder_limit_only_by_their_allowance(
    tmp_path: Path,
) -> None:
    # The collector holds the folder's last slot for a response it is still
    # fetching. Worker repairs cannot see that reservation, which lives in the
    # collector's process: across successive turns they fill the folder to its
    # limit plus their allowance of one file per concurrent worker job, and the
    # collector's reserved save then lands one file beyond that.
    root = tmp_path / "spool"
    limits = {"max_body_bytes": 64, "max_bytes": 1 << 20, "max_objects": 3}
    collector = Spool(root, **limits)
    for body in (b"saved 1", b"saved 2"):
        with collector.reservation() as reservation:
            reservation.publish(body, _digest(body))
    last = collector.reserve()
    archive = SimpleNamespace(
        bucket="evidence",
        max_body_bytes=64,
        max_retries=0,
        retry_backoff_seconds=0.0,
        read_verified=lambda reference, digest, heartbeat=None: ArchiveReadResult(
            body=archived[digest], reference=reference, sha256=digest
        ),
    )
    worker = SpoolFirstReader(
        archive, spool_root=str(root), validate_database=False, **limits
    )
    archived = {
        _digest(body): body
        for body in (f"archived {n}".encode() for n in range(MAX_CONCURRENCY + 2))
    }
    digests = list(archived)

    def repair(digest: str) -> bytes:
        return worker.read_verified(f"s3://evidence/{digest}", digest).body

    repaired = 0
    while True:
        try:
            assert repair(digests[repaired]) == archived[digests[repaired]]
        except ArchiveReadError as error:
            refused = error
            break
        repaired += 1
    # Repairs stop once the files on disk reach the limit plus the allowance;
    # the next is refused as a full spool to wait on, never as missing proof.
    assert repaired == MAX_CONCURRENCY + 1
    assert (refused.category, refused.retryable) == ("spool_io_failed", True)
    assert collector.reconcile()["final_objects"] == (
        limits["max_objects"] + MAX_CONCURRENCY
    )
    # Every reservation the collector already held, here one, still lands on
    # top of the limit and the repairs' allowance.
    last.publish(b"fetched", _digest(b"fetched"))
    assert collector.reconcile()["final_objects"] == (
        limits["max_objects"] + MAX_CONCURRENCY + 1
    )
    with pytest.raises(SpoolError, match="reservation denied"):
        collector.reserve()
