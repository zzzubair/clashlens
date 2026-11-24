"""The worker's health check: fail when the worker is stuck, not when it waits.

Podman runs ``clashlens.cli ready`` every 30 s and kills the worker after six
failures in a row. On 7 Oct 2026 that check waited on the spool lock and on a
new database connection, so a slow lock holder got a working worker killed
four times. Once the worker runs, each claim lane and the maintenance timer
mark the time every time round their loops, and the progress file records
each thread's last mark and limit. The check fails when one thread has not
come round for its own limit, whether or not the others still do; until 8 Oct
2026 any one thread kept the whole worker healthy. A lone thread running a
long build writes nothing until it comes round, so only a file that cannot be
read falls back to its age: not written for STUCK_SECONDS. STUCK_SECONDS is longer than the worker's 15-minute database query
limit, so a slow database slows the lanes without making them look stuck.
Until the first write the check still proves the worker's dependencies once.
Each of several worker processes writes its own file, and the check fails
when any one of them, or any thread in one, is stuck.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from .spool import read_readiness

STUCK_SECONDS = 1200.0
LONG_RUNNING_STUCK_SECONDS = 3600.0
MARK_INTERVAL_SECONDS = 5.0
# The health check runs inside the worker's container, which has its own /tmp,
# as the same user, so the files sit in a folder only that user can open.
PROGRESS_FILE = "/tmp/clashlens-worker/progress"


def progress_file(process_index: int = 0) -> str:
    """The progress file of one worker process; 0 is a worker on its own."""
    return f"{PROGRESS_FILE}-{process_index}" if process_index else PROGRESS_FILE


def _private_folder(path: str) -> str:
    """The folder holding `path`, made on first use; refused unless it is a
    real folder of this user's that no one else can open."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    found = os.lstat(folder)
    if (
        not stat.S_ISDIR(found.st_mode)
        or found.st_uid != os.getuid()
        or found.st_mode & 0o077
    ):
        raise PermissionError(f"{folder} is not a private folder")
    return folder


def touch_progress_file(process_index: int) -> None:
    path = progress_file(process_index)
    _private_folder(path)
    Path(path).touch()


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
            try:
                descriptor, temporary = tempfile.mkstemp(
                    dir=_private_folder(self.path), prefix=".progress-", suffix=".new"
                )
                try:
                    with os.fdopen(descriptor, "w") as file:
                        file.write(json.dumps(limits))
                    os.replace(temporary, self.path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(temporary)
                    raise
            except OSError:
                pass


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


def stuck_thread() -> tuple[bool, str | None]:
    """Whether a process's progress file that cannot be read has not been
    written for STUCK_SECONDS, and the first thread past its own limit in the
    files that can."""
    stale, overdue = False, []
    for path in _progress_files():
        try:
            threads = json.loads(Path(path).read_text())
            overdue += [
                name
                for name, (marked_at, limit) in threads.items()
                if time.time() - float(marked_at) >= float(limit)
            ]
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError, AttributeError):
            try:
                stale |= time.time() - os.stat(path).st_mtime >= STUCK_SECONDS
            except OSError:
                continue
    return stale, min(overdue, default=None)


def worker_readiness(
    arguments: argparse.Namespace,
    open_database: Callable[[], Any],
    open_archive: Callable[..., Any],
) -> dict[str, Any]:
    spool_root = getattr(arguments, "spool_root", "")
    spool_ready, reason = (
        read_readiness(spool_root) if spool_root else (False, "unconfigured")
    )
    spool = {"ready": spool_ready, "component": "spool", "reason": reason}
    if not spool_ready:
        return {"status": "not_ready", "spool": spool}
    since = seconds_since_progress()
    if since is not None:
        stale, thread = stuck_thread()
        stuck = stale or thread is not None
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
        remote_health = getattr(
            archive, "check_marker_health", lambda: "unconfigured"
        )()
    finally:
        database.close()
    return {
        "status": "not_ready" if remote_health == "terminal" else "ready",
        "spool": spool,
        "remote_health": remote_health,
    }
