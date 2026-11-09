"""Shared setup for boundary publication lock order postgres tests."""

from __future__ import annotations

import time


def _advisory_waiters(connection) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        ).fetchone()[0]
    )


def _wait_for_waiters(connection, count: int, errors: list[BaseException]) -> None:
    deadline = time.monotonic() + 30
    while _advisory_waiters(connection) < count and not errors:
        assert time.monotonic() < deadline, "work never queued on the Reset lock"
        time.sleep(0.02)
