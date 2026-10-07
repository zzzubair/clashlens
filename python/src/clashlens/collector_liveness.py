"""The collector's health port and the check Podman kills it on.

Podman checks ``/livez`` every 30 s and kills the collector after six failures
in a row. On 7 Oct 2026 that check was ``/readyz``, which waits for a database
connection, so a slow database got a working collector killed 13 times. Each
collector loop now records the time every time round, and ``/livez`` fails
only when one of them has not come round for STUCK_SECONDS while no database
call is running, or on a state only a restart clears. A slow database slows the
loops without making them look stuck. ``/livez`` reads only the collector's
memory: no lock, thread, file or database connection. ``/readyz`` still reports the database, spool capacity
and keys, for people to read.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector import Collector

# The worker's 20 minutes. The slowest pass on 7 Oct 2026 waited about 60 s.
STUCK_SECONDS = 1200.0
LOOPS = ("regular", "intents", "uploads")


def mark(collector: Collector, loop: str) -> None:
    collector.loop_passes[loop] = time.monotonic()


@contextmanager
def database_wait(collector: Collector) -> Iterator[None]:
    token = object()
    collector.database_waits.add(token)
    try:
        yield
    finally:
        collector.database_waits.discard(token)


def livez(collector: Collector) -> tuple[int, str, bytes]:
    passes = collector.loop_passes
    oldest = min(passes.get(name, passes["start"]) for name in LOOPS)
    if collector._handoff_recovery_required:
        state = "handoff_recovery_required"
    elif collector._spool_io_failed:
        state = "spool_io_failure"
    elif time.monotonic() - oldest >= STUCK_SECONDS and not collector.database_waits:
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
