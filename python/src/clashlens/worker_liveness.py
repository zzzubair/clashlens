"""The worker's health check: fail when the worker is stuck, not when it waits.

Podman runs ``clashlens.cli ready`` every 30 s and kills the worker after six
failures in a row. On 7 Oct 2026 that check waited on the spool lock and on a
new database connection, so a slow lock holder got a working worker killed
four times. Once the worker runs, each claim lane and the maintenance timer
mark the time every time round their loops, and the progress file records
each thread's last mark. The check fails when no thread has written it for
STUCK_SECONDS, or when one thread has not come round for its own limit while
the others still do; until 8 Oct 2026 any one thread kept the whole worker
healthy. STUCK_SECONDS is longer than the worker's 15-minute database query
limit, so a slow database slows the lanes without making them look stuck.
Until the first write the check still proves the worker's dependencies once.
Each of several worker processes writes its own file, and the check fails
when any one of them, or any thread in one, is stuck.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from .spool import read_readiness

# From 1 to 8 Oct 2026 the longest finished job other than a build took 465 s.
STUCK_SECONDS = 1200.0
# Threads that may run a population build or the timer's publication checks.
# The longest finished build took 844 s; before 6 Oct some ran 63 minutes.
LONG_RUNNING_STUCK_SECONDS = 3600.0
MARK_INTERVAL_SECONDS = 5.0
# The health check runs inside the worker's container, which has its own /tmp.
PROGRESS_FILE = "/tmp/clashlens-worker-progress"


def progress_file(process_index: int = 0) -> str:
    """The progress file of one worker process; 0 is a worker on its own."""
    return f"{PROGRESS_FILE}-{process_index}" if process_index else PROGRESS_FILE


class ProgressMark:
    """Record the calling thread's turn; write every thread's at most once
    every MARK_INTERVAL_SECONDS."""

    def __init__(
        self, path: str | None = None, long_running: Collection[str] = ()
    ) -> None:
        self.path = path or PROGRESS_FILE
        self._long_running = frozenset(long_running)
        self._marks: dict[str, float] = {}
        self._written_at = float("-inf")
        self._lock = threading.Lock()

    def __call__(self) -> None:
        name = threading.current_thread().name
        with self._lock:
            self._marks[name] = time.time()
            if time.monotonic() - self._written_at < MARK_INTERVAL_SECONDS:
                return
            self._written_at = time.monotonic()
            limits = {
                thread: [
                    marked_at,
                    LONG_RUNNING_STUCK_SECONDS
                    if thread in self._long_running
                    else STUCK_SECONDS,
                ]
                for thread, marked_at in self._marks.items()
            }
            temporary = Path(self.path + ".new")
            try:
                temporary.write_text(json.dumps(limits))
                os.replace(temporary, self.path)
            except OSError:
                pass  # A file that stops changing reads as stuck, which is right.


def _progress_files() -> list[str]:
    """Every worker process's progress file, without half-written ones."""
    return [
        path
        for path in glob.glob(f"{PROGRESS_FILE}*")
        if not path.endswith(".new")
    ]


def seconds_since_progress() -> float | None:
    """Seconds since the stalest worker process last made progress."""
    ages = []
    for path in _progress_files():
        try:
            ages.append(max(0.0, time.time() - os.stat(path).st_mtime))
        except FileNotFoundError:
            continue
    return max(ages, default=None)


def stuck_thread() -> str | None:
    """The first thread past its own limit, from every process's file."""
    overdue = []
    for path in _progress_files():
        try:
            threads = json.loads(Path(path).read_text())
            overdue += [
                name
                for name, (marked_at, limit) in threads.items()
                if time.time() - float(marked_at) >= float(limit)
            ]
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # The file's age still catches a worker that stopped.
    return min(overdue, default=None)


def worker_readiness(
    arguments: argparse.Namespace,
    open_database: Callable[[], Any],
    open_archive: Callable[..., Any],
) -> dict[str, Any]:
    spool_root = getattr(arguments, "spool_root", "")
    spool_ready, reason = (
        read_readiness(spool_root) if spool_root else (True, "unconfigured")
    )
    spool = {"ready": spool_ready, "component": "spool", "reason": reason}
    if not spool_ready:
        return {"status": "not_ready", "spool": spool}
    since = seconds_since_progress()
    if since is not None:
        thread = stuck_thread()
        stuck = since >= STUCK_SECONDS or thread is not None
        return {
            "status": "not_ready" if stuck else "ready",
            "reason": "worker_stuck" if stuck else "worker_progressing",
            "seconds_since_progress": round(since),
            **({"stuck_thread": thread} if thread is not None else {}),
            "spool": spool,
        }
    database = open_database()
    try:
        if not database.is_ready(
            expected_contract_version=arguments.expected_contract_version
        ):
            return {
                "status": "not_ready",
                "reason": "database_contract",
                "spool": spool,
            }
        # No spool here: opening one recounts every saved file under its lock.
        archive = open_archive(
            argparse.Namespace(**{**vars(arguments), "spool_root": ""}),
            database=database,
        )
        validate_instance = getattr(archive, "validate_instance", None)
        if callable(validate_instance):
            validate_instance(database)
        if not spool_root and not archive.check_ready():
            return {"status": "not_ready", "reason": "archive", "spool": spool}
        remote_health = getattr(
            archive, "check_marker_health", lambda: "unconfigured"
        )()
    finally:
        database.close()
    # A marker mismatch is terminal configuration drift, not an outage;
    # readiness must fail so the operator resolves it before workers run.
    return {
        "status": "not_ready" if remote_health == "terminal" else "ready",
        "spool": spool,
        "remote_health": remote_health,
    }
