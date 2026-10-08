from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from psycopg_pool import PoolTimeout
from test_collector import _Client, _collector, _Spool, _Store

from clashlens import collector_liveness
from clashlens.collector_liveness import LOOPS, STUCK_SECONDS

LIVE = (200, "text/plain", b"live\n")


def _running_collector(store_type: type[_Store] = _Store):
    spool = _Spool()
    collector = _collector(spool, store_type(spool), _Client(spool))
    collector.loop_passes.update(dict.fromkeys(LOOPS, time.monotonic()))
    return collector


def test_slow_database_fails_readiness_but_not_the_container_check() -> None:
    # 7 October 2026: Podman's check was /readyz, which waits for a database
    # connection, so a slow database got a working collector killed 13 times.
    collector = _running_collector()
    collector.database.pool = MagicMock()
    collector.database.pool.connection.side_effect = PoolTimeout("pool busy")

    assert asyncio.run(collector.health_response("/readyz"))[2] == (
        b"database_unavailable\n"
    )
    assert asyncio.run(collector.health_response("/livez")) == LIVE


@pytest.mark.parametrize("loop", LOOPS)
def test_a_loop_that_stops_coming_round_fails_after_twenty_minutes(loop) -> None:
    collector = _running_collector()
    collector.loop_passes[loop] = time.monotonic() - STUCK_SECONDS + 60
    assert asyncio.run(collector.health_response("/livez")) == LIVE

    collector.loop_passes[loop] = time.monotonic() - STUCK_SECONDS - 1
    assert asyncio.run(collector.health_response("/livez")) == (
        503,
        "text/plain",
        b"stuck\n",
    )


def test_slow_cleanup_batches_do_not_fail_the_container_check(monkeypatch) -> None:
    # 1,024 kept copies in 16 batches of 90 s each keep the uploads loop from
    # coming round for 24 minutes while the database is slow.
    collector = _running_collector()
    now = [time.monotonic()]
    answers: list[bytes] = []

    def advance(seconds: float) -> None:
        now[0] += seconds
        collector.loop_passes.update(regular=now[0], intents=now[0])
        answers.append(asyncio.run(collector.health_response("/livez"))[2])

    def slow_delete(digests: list[str], _delete: object) -> int:
        advance(90.0)
        return len(digests)

    monkeypatch.setattr(
        collector_liveness, "time", SimpleNamespace(monotonic=lambda: now[0])
    )
    monkeypatch.setattr("clashlens.collector.time", SimpleNamespace(sleep=advance))
    collector.database.deletable = [f"{index:064x}" for index in range(1024)]
    collector.database.delete_spool_if_deletable = slow_delete

    assert collector.cleanup_uploaded() == (1024, 1024)
    assert now[0] - collector.loop_passes["start"] > STUCK_SECONDS
    assert len(answers) == 31
    assert set(answers) == {b"live\n"}

    advance(STUCK_SECONDS)
    assert answers[-1] == b"stuck\n"


def _livez_during_a_slow_call(collector, loop: str, set_up) -> bytes:
    """Answer /livez while ``loop`` waits on a database call that has not returned."""
    started, release = threading.Event(), threading.Event()

    def slow_statement() -> None:
        started.set()
        assert release.wait(timeout=10)

    async def loop_turn() -> None:
        collector_liveness.mark(collector, loop)
        await collector._database_call(slow_statement)

    async def scenario() -> bytes:
        call = asyncio.create_task(loop_turn())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            set_up()
            return (await collector.health_response("/livez"))[2]
        finally:
            release.set()
            await call

    return asyncio.run(scenario())


def test_a_stalled_loop_fails_while_another_loops_database_call_runs() -> None:
    # Player checks keep a database call running almost all the time. Until
    # 8 Oct 2026 any running call held off the verdict for every loop, so a
    # stalled Reset and intent loop could pass the check for ever.
    collector = _running_collector()

    def intents_stall() -> None:
        collector.loop_passes["intents"] = time.monotonic() - STUCK_SECONDS - 1

    assert _livez_during_a_slow_call(collector, "regular", intents_stall) == b"stuck\n"


def test_a_loops_own_slow_database_call_holds_off_stuck_for_twenty_minutes(
    monkeypatch,
) -> None:
    collector = _running_collector()
    now = [time.monotonic()]
    monkeypatch.setattr(
        collector_liveness, "time", SimpleNamespace(monotonic=lambda: now[0])
    )

    def regular_waits(seconds: float):
        def set_up() -> None:
            collector.loop_passes.update(
                regular=now[0] - STUCK_SECONDS - 600,
                intents=now[0] + seconds,
                uploads=now[0] + seconds,
            )
            now[0] += seconds

        return set_up

    # The call began just now: a slow database, not a stuck loop.
    assert _livez_during_a_slow_call(collector, "regular", regular_waits(60)) == (
        b"live\n"
    )
    # The same call still running twenty minutes after it began is stuck.
    assert _livez_during_a_slow_call(
        collector, "regular", regular_waits(STUCK_SECONDS)
    ) == b"stuck\n"


def test_startup_that_never_finishes_fails_after_twenty_minutes() -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    assert asyncio.run(collector.health_response("/livez"))[2] == b"starting\n"

    collector.loop_passes["start"] = time.monotonic() - STUCK_SECONDS - 1
    assert asyncio.run(collector.health_response("/livez"))[2] == b"stuck\n"


def test_states_only_a_restart_clears_still_fail_the_container_check() -> None:
    collector = _running_collector()
    collector._spool_io_failed = True
    assert asyncio.run(collector.health_response("/livez"))[2] == (
        b"spool_io_failure\n"
    )

    collector = _running_collector()
    collector._handoff_recovery_required = True
    assert asyncio.run(collector.health_response("/livez"))[2] == (
        b"handoff_recovery_required\n"
    )

    collector = _running_collector()
    collector.regular_keys.quarantine("regular-1")
    assert asyncio.run(collector.health_response("/livez"))[2] == (
        b"regular_keys_unhealthy\n"
    )


def test_health_port_answers_while_startup_recovery_is_slow() -> None:
    # 7 October 2026: startup recovery ran a 60 s read before the health port
    # opened, so each restarted collector was killed again before answering.
    collector = _running_collector()
    for loop in LOOPS:
        del collector.loop_passes[loop]
    recovering, release = threading.Event(), threading.Event()

    def slow_recovery() -> int:
        recovering.set()
        assert release.wait(timeout=10)
        return 0

    async def busy_loop(name: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            collector.loop_passes[name] = time.monotonic()
            await asyncio.sleep(0.01)

    collector.recover_handoffs = slow_recovery  # type: ignore[method-assign]
    collector._regular_loop = (  # type: ignore[method-assign]
        lambda stop, _idle: busy_loop("regular", stop)
    )
    collector._intent_loop = (  # type: ignore[method-assign]
        lambda stop, _rankings, _idle: busy_loop("intents", stop)
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def livez() -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /livez HTTP/1.1\r\nHost: collector\r\n\r\n")
        response = await reader.read()
        writer.close()
        return response.split(b"\r\n", 1)[0] + b" " + response.rsplit(b"\r\n", 1)[1]

    async def scenario() -> None:
        stop = asyncio.Event()
        run = asyncio.create_task(
            collector.run(stop, health_host="127.0.0.1", health_port=port)
        )
        try:
            assert await asyncio.to_thread(recovering.wait, 5)
            assert await livez() == b"HTTP/1.1 503 Service Unavailable starting\n"
            release.set()
            for _attempt in range(200):
                if (answer := await livez()).endswith(b"live\n"):
                    break
                await asyncio.sleep(0.05)
            assert answer == b"HTTP/1.1 200 OK live\n"
        finally:
            release.set()
            stop.set()
            await run

    asyncio.run(scenario())
