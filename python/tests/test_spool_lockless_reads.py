"""Worker reads of saved responses take no spool lock, and stay correct.

On 7 Oct 2026 the collector held the shared spool lock for about 60 s at a
time, so every worker read and the worker's health check waited behind it.
A saved file never changes once written, so reads do not need the lock.
"""

from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from clashlens import spool as spool_module
from clashlens.spool import Spool, read_readiness


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def test_reads_and_read_check_do_not_wait_for_a_lock_holder(tmp_path) -> None:
    root = tmp_path / "spool"
    collector = Spool(root, max_body_bytes=1024)
    worker = Spool(root, max_body_bytes=1024)
    body = b"saved response"
    collector.publish(body, _digest(body))
    holding, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with collector._capacity_lock():
            holding.set()
            assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=4) as executor:
        holder = executor.submit(hold_lock)
        try:
            assert holding.wait(timeout=2)
            read = executor.submit(worker.verify, _digest(body))
            check = executor.submit(worker.readiness, admission=False)
            check_by_path = executor.submit(read_readiness, root)
            assert read.result(timeout=2) == body
            assert check.result(timeout=2) == (True, "ready")
            assert check_by_path.result(timeout=2) == (True, "ready")
        finally:
            release.set()
        holder.result(timeout=2)


def test_read_check_fails_when_saved_files_cannot_be_reached(tmp_path) -> None:
    assert read_readiness(tmp_path / "missing") == (
        False,
        "storage_error:FileNotFoundError",
    )


def test_a_read_already_open_survives_a_delete(tmp_path, monkeypatch) -> None:
    root = tmp_path / "spool"
    reader = Spool(root, max_body_bytes=1024)
    deleter = Spool(root, max_body_bytes=1024)
    body = b"x" * 900
    digest = _digest(body)
    reader.publish(body, digest)
    real_fstat = os.fstat
    deleted = []

    def delete_then_fstat(fd: int) -> os.stat_result:
        # The collector's cleanup deletes the file after the read opened it.
        if not deleted:
            deleted.append(deleter.delete(digest))
        return real_fstat(fd)

    monkeypatch.setattr(spool_module.os, "fstat", delete_then_fstat)
    assert reader.verify(digest) == body
    monkeypatch.undo()
    assert deleted == [True]
    assert reader.verify(digest) is None


def test_reads_see_whole_files_or_nothing_while_files_are_saved_and_deleted(
    tmp_path,
) -> None:
    root = tmp_path / "spool"
    writer = Spool(root, max_body_bytes=1 << 16)
    reader = Spool(root, max_body_bytes=1 << 16)
    kept = os.urandom(40_000)
    writer.publish(kept, _digest(kept))
    churned = [os.urandom(30_000 + index) for index in range(8)]
    stop = threading.Event()

    def save_and_delete() -> None:
        while not stop.is_set():
            for body in churned:
                writer.publish(body, _digest(body))
            with writer.delete_unreferenced_batch() as delete:
                for body in churned:
                    delete(_digest(body))

    def read(rounds: int) -> list[str]:
        problems = []
        for _ in range(rounds):
            if reader.verify(_digest(kept)) != kept:
                problems.append("a saved file that was never deleted went missing")
            for body in churned:
                if reader.verify(_digest(body)) not in (body, None):
                    problems.append(
                        "a read returned bytes that were not the saved file"
                    )
        return problems

    with ThreadPoolExecutor(max_workers=4) as executor:
        churn = executor.submit(save_and_delete)
        try:
            readers = [executor.submit(read, 150) for _ in range(3)]
            problems = [
                problem for done in readers for problem in done.result(timeout=60)
            ]
        finally:
            stop.set()
        churn.result(timeout=10)
    assert problems == []
