"""The worker's health check fails for a stuck worker, never a slow database.

On 7 Oct 2026 the check waited on the spool lock while the collector held it,
and Podman killed a working worker four times in 17 minutes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from clashlens import cli, worker_liveness
from clashlens.spool import Spool
from clashlens.worker import ObservationProcessor, ProcessResult, process_until_stopped


class _SlowDatabase:
    """A database too slow to answer inside Podman's 20-second limit."""

    def __init__(self, _url: str) -> None:
        raise AssertionError("a running worker's health check opened the database")


class _ReadyDatabase:
    def __init__(self, _url: str) -> None:
        pass

    def is_ready(self, *, expected_contract_version: int) -> bool:
        return expected_contract_version == 5

    def close(self) -> None:
        pass


def _arguments(tmp_path, *, spool_root) -> Namespace:
    return Namespace(
        database_url="postgresql://stub",
        database_url_file="",
        expected_contract_version=5,
        spool_root=str(spool_root),
    )


def _ready(arguments, capsys) -> tuple[int, dict]:
    exit_code = cli._run_ready(arguments)
    return exit_code, json.loads(capsys.readouterr().out)


def test_running_worker_stays_healthy_while_the_lock_is_held_and_the_database_slow(
    tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "spool"
    collector = Spool(root, max_body_bytes=1024)
    worker_liveness.ProgressMark()()
    monkeypatch.setattr(cli, "Database", _SlowDatabase)
    holding, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with collector._capacity_lock():
            holding.set()
            assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        try:
            assert holding.wait(timeout=2)
            started = time.monotonic()
            exit_code, payload = _ready(_arguments(tmp_path, spool_root=root), capsys)
            assert time.monotonic() - started < 2
        finally:
            release.set()
        holder.result(timeout=2)
    assert exit_code == 0
    assert payload["status"] == "ready"


def test_worker_without_progress_for_20_minutes_fails_its_health_check(
    tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "spool"
    Spool(root, max_body_bytes=1024)
    progress = Path(worker_liveness.PROGRESS_FILE)
    progress.touch()
    monkeypatch.setattr(cli, "Database", _SlowDatabase)
    arguments = _arguments(tmp_path, spool_root=root)

    nineteen_minutes_ago = time.time() - 19 * 60
    os.utime(progress, (nineteen_minutes_ago, nineteen_minutes_ago))
    assert _ready(arguments, capsys)[0] == 0

    twenty_minutes_ago = time.time() - 20 * 60
    os.utime(progress, (twenty_minutes_ago, twenty_minutes_ago))
    exit_code, payload = _ready(arguments, capsys)
    assert exit_code == 1
    assert payload["reason"] == "worker_stuck"


def test_starting_worker_proves_its_dependencies_without_the_spool_lock(
    tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "spool"
    collector = Spool(root, max_body_bytes=1024)
    opened = []

    class Archive:
        def check_ready(self) -> bool:
            raise AssertionError("the remote archive is not the worker's readiness")

        def check_marker_health(self) -> str:
            return "ready"

    def open_archive(arguments, **_kwargs):
        opened.append(arguments.spool_root)
        return Archive()

    monkeypatch.setattr(cli, "Database", _ReadyDatabase)
    monkeypatch.setattr(cli, "_archive", open_archive)
    holding, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with collector._capacity_lock():
            holding.set()
            assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lock)
        try:
            assert holding.wait(timeout=2)
            exit_code, payload = _ready(_arguments(tmp_path, spool_root=root), capsys)
        finally:
            release.set()
        holder.result(timeout=2)
    assert (exit_code, payload["status"]) == (0, "ready")
    assert opened == [""]


def test_starting_worker_fails_when_saved_responses_cannot_be_read(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "Database", _ReadyDatabase)
    exit_code, payload = _ready(
        _arguments(tmp_path, spool_root=tmp_path / "missing"), capsys
    )
    assert exit_code == 1
    assert payload["spool"]["ready"] is False


def test_idle_lanes_report_progress_and_stuck_lanes_stop_even_while_maintenance_ticks() -> (
    None
):
    stop = Event()
    lanes_stuck = Event()
    marks = []
    maintenance_ticks = []
    jobs_started = threading.Semaphore(0)

    class Processor:
        def process_once(self, **_kwargs):
            if lanes_stuck.is_set():
                jobs_started.release()
                stop.wait(timeout=10)  # A job that never finishes.
            # Otherwise the queue is empty.

    worker = threading.Thread(
        target=process_until_stopped,
        args=(Processor(),),
        kwargs={
            "concurrency": 2,
            "owner": "test-worker",
            "lease_seconds": 30,
            "stop_requested": stop,
            "idle_seconds": 0.01,
            "claims_ready": lambda: True,
            "maintain": lambda _turns: maintenance_ticks.append(1),
            "on_result": lambda _result: None,
            "progress": lambda: marks.append(1),
        },
        daemon=True,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while len(marks) < 20 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(marks) >= 20, "idle lanes must keep reporting progress"
        lanes_stuck.set()
        assert jobs_started.acquire(timeout=2) and jobs_started.acquire(timeout=2)
        stuck_marks, stuck_ticks = len(marks), len(maintenance_ticks)
        time.sleep(0.2)
        assert len(maintenance_ticks) > stuck_ticks
        assert len(marks) == stuck_marks
    finally:
        stop.set()
        worker.join(timeout=5)


def test_one_lane_reports_progress_before_every_job_in_a_batch() -> None:
    # One lane runs batches of up to 100 jobs; at 20 s a job one batch outlasts
    # the 20 minutes after which the health check calls the worker stuck.
    events = []

    class Processor(ObservationProcessor):
        def __init__(self) -> None:
            pass

        def process_once(self, **_kwargs):
            events.append("job")
            return ProcessResult(len(events), "processed")

    Processor().process_until_idle(
        owner="test-worker", max_jobs=3, progress=lambda: events.append("progress")
    )
    assert events == ["progress", "job"] * 3
