"""The worker's health check: fail when the worker is stuck, not when it waits.

Podman runs ``clashlens.cli ready`` every 30 s and kills the worker after six
failures in a row. On 7 Oct 2026 that check waited on the spool lock and on a
new database connection, so a slow lock holder got a working worker killed
four times. Once the worker runs, each claim lane touches a progress file
every time round its loop, and the check fails only when no lane has done so
for STUCK_SECONDS. That is longer than the worker's 15-minute database query
limit, so a slow database slows the lanes without making them look stuck.
Until the first touch the check still proves the worker's dependencies once.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .spool import read_readiness

STUCK_SECONDS = 1200.0
MARK_INTERVAL_SECONDS = 5.0
# The health check runs inside the worker's container, which has its own /tmp.
PROGRESS_FILE = "/tmp/clashlens-worker-progress"


class ProgressMark:
    """Touch the progress file, at most once every MARK_INTERVAL_SECONDS."""

    def __init__(self) -> None:
        self._marked_at = float("-inf")
        self._lock = threading.Lock()

    def __call__(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._marked_at < MARK_INTERVAL_SECONDS:
                return
            self._marked_at = now
        try:
            Path(PROGRESS_FILE).touch()
        except OSError:
            pass  # A file that stops changing reads as stuck, which is right.


def seconds_since_progress() -> float | None:
    try:
        return max(0.0, time.time() - os.stat(PROGRESS_FILE).st_mtime)
    except FileNotFoundError:
        return None


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
        stuck = since >= STUCK_SECONDS
        return {
            "status": "not_ready" if stuck else "ready",
            "reason": "worker_stuck" if stuck else "worker_progressing",
            "seconds_since_progress": round(since),
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
