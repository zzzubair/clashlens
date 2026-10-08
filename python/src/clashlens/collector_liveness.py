"""The collector's health port, the check Podman kills it on, and its database limits.

Podman checks ``/livez`` every 30 s and kills the collector after six failures
in a row. On 7 Oct 2026 that check was ``/readyz``, which waits for a database
connection, so a slow database got a working collector killed 13 times. Each
collector loop now records the time every time round, and ``/livez`` fails
when one of them has not come round for STUCK_SECONDS, or on a state only a
restart clears. A database call made by the loop itself, or by work it
started, holds that off for up to STUCK_SECONDS from when the call began, so a
slow database slows the loops without making them look stuck. Until 8 Oct
2026 any call held it off for every loop, and player checks keep one running
almost all the time, so a stuck Reset or upload loop could pass for ever.
Every collector statement also stops after STATEMENT_TIMEOUT, so no database
wait lasts indefinitely. ``/livez`` reads only the collector's memory: no
lock, thread, file or database connection. ``/readyz`` still reports the
database, spool capacity and keys, for people to read.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .collector import Collector

# The worker's 20 minutes. The slowest pass on 7 Oct 2026 waited about 60 s.
STUCK_SECONDS = 1200.0
LOOPS = ("regular", "intents", "uploads")
# The slowest collector statement of today's code from 1 to 8 Oct 2026 took
# 11.5 s (claiming an upload; pg_stat_statements). One cancelled at the limit
# fails like any database error: the call is retried, then its loop stops and
# the collector restarts. Only older code waited longer: 10 minutes for a row
# lock. The Reset sweep writes every member's work in one transaction, so it
# has BULK_STATEMENT_TIMEOUT instead.
STATEMENT_TIMEOUT = "60s"
BULK_STATEMENT_TIMEOUT = "5min"

# The loop that is running: set by each loop's mark and inherited by the tasks
# and threads it starts.
_loop: ContextVar[str] = ContextVar("collector_loop", default="start")


def bound_statements(connection: Any) -> None:
    """Configure each new collector connection with the statement limit."""
    connection.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
    connection.commit()


def mark(collector: Collector, loop: str) -> None:
    collector.loop_passes[loop] = time.monotonic()
    _loop.set(loop)


@contextmanager
def database_wait(collector: Collector) -> Iterator[None]:
    token = object()
    collector.database_waits[token] = (_loop.get(), time.monotonic())
    try:
        yield
    finally:
        collector.database_waits.pop(token, None)


def livez(collector: Collector) -> tuple[int, str, bytes]:
    now = time.monotonic()
    passes = collector.loop_passes
    # A copy: threads add and remove waits while this reads them.
    waits = tuple(collector.database_waits.values())

    def last_seen(loop: str) -> float:
        # A loop that has not come round yet answers for startup recovery.
        name = loop if loop in passes else "start"
        return max([passes[name], *(at for owner, at in waits if owner == name)])

    if collector._handoff_recovery_required:
        state = "handoff_recovery_required"
    elif collector._spool_io_failed:
        state = "spool_io_failure"
    elif any(now - last_seen(loop) >= STUCK_SECONDS for loop in LOOPS):
        state = "stuck"
    elif not all(name in passes for name in LOOPS):
        # Startup recovery still running; HealthStartPeriod ignores this.
        state = "starting"
    elif collector.regular_keys.health()["healthy"] == 0:
        state = "regular_keys_unhealthy"
    elif collector.interactive_keys.health()["healthy"] == 0:
        state = "interactive_key_unhealthy"
    else:
        state = "live"
    return (200 if state == "live" else 503), "text/plain", f"{state}\n".encode()


async def serve(
    collector: Collector, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0)
        first_line = request.split(b"\r\n", 1)[0].decode("ascii", "replace")
        parts = first_line.split()
        path = parts[1] if len(parts) == 3 and parts[0] == "GET" else ""
        status, content_type, body = await collector.health_response(path)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
        status, content_type, body = 400, "text/plain", b"bad request\n"
    reasons = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        503: "Service Unavailable",
    }
    response = (
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    writer.write(response)
    await writer.drain()
    writer.close()
    await writer.wait_closed()
