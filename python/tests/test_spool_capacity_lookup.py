from __future__ import annotations

import hashlib
from pathlib import Path
from unittest import mock

import pytest

from clashlens import filesystem
from clashlens.spool import Spool, SpoolError


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
    collector.publish(saved[0], hashlib.sha256(saved[0]).hexdigest())
    # The folder now holds its limit of two files, whoever saved them.
    with pytest.raises(SpoolError, match="reservation denied"):
        worker.publish(saved[1], hashlib.sha256(saved[1]).hexdigest())
