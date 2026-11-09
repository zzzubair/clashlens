"""Settlement checks run while the weekly discovery queue is full and slow.

At the 9 November 2026 Monday Reset the weekly eligibility check filled the
discovery queue (497 of its 500 places), and adding to it took 30 to 60
seconds on every pass. The collector picks one batch of due work at a time:
discovery checks sort ahead of settlement checks, so they filled every batch
and none of the 14,040 settlement checks started. The admission ran inside
that pick, so three timed-out admissions in a row stopped the collector
(8 restarts from 05:52 to 06:20 UTC).

These tests run the real collector loop against PostgreSQL and a fake game
API, from a swept Reset with its pairs finished to its settlement checks
saved.
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import psycopg
import pytest
from domain_test_support import domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clashlens import collector_liveness
from clashlens.collector import Collector
from clashlens.collector_db import CollectorDatabase
from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient
from clashlens.spool import Spool

SETTLEMENT_PLAYERS = 12
RESET_SLOTS = 8


class _Api(BaseHTTPRequestHandler):
    """A fake official API answering every player request."""

    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        path = unquote(self.path)
        if path.endswith("/leaguehistory"):
            time.sleep(0.2)  # discovery checks hold their slots a while
        tag = path.split("/v1/players/")[-1].split("/")[0]
        body = (
            b'{"items":[]}'
            if path.endswith(("/battlelog", "/leaguehistory")) or "/rankings/" in path
            else f'{{"tag":"{tag}","name":"x","trophies":5000}}'.encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _settlement_due_now() -> datetime:
    """Today's Reset, once its settlement checks are due and still open."""
    now = datetime.now(UTC)
    boundary = now.replace(hour=5, minute=0, second=0, microsecond=0)
    boundary = boundary if boundary <= now else boundary - timedelta(days=1)
    if not timedelta(minutes=21) <= now - boundary <= timedelta(hours=23, minutes=50):
        pytest.skip("settlement checks are not due and open at this time of day")
    return boundary


def _monday_queue(connection_info: str, discovery_checks: int) -> CollectorDatabase:
    """A swept Reset with finished pairs, plus a full discovery queue."""
    boundary = _settlement_due_now()
    options = conninfo_to_dict(connection_info).get("options", "")
    collector_role = make_conninfo(
        connection_info, options=f"{options} -c role=clashlens_collector"
    )
    later = datetime.now(UTC) + timedelta(hours=1)
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO players (normalized_tag, active, next_due_at)"
            " SELECT '#2P' || n, true, %s FROM generate_series(1, %s) AS n",
            (later, SETTLEMENT_PLAYERS),
        )
    database = CollectorDatabase(collector_role)
    assert database.begin_reset(boundary) is not None
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "UPDATE collector_work SET status = 'failed' WHERE kind = 'reset_baseline'"
        )
        connection.execute(
            """
            WITH waiting AS (
                INSERT INTO players (normalized_tag, active)
                SELECT '#8Q' || n, false FROM generate_series(1, %s) AS n
                RETURNING id, normalized_tag
            )
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                profile_status, battle_log_status, league_history_status
            )
            SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag,
                   now() - interval '1 minute', 'discovery-profile:' || id,
                   'pending', 'not_applicable', 'pending'
            FROM waiting
            """,
            (discovery_checks,),
        )
    return database


def _unfinished(connection_info: str, kind: str) -> int:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT count(*) FROM collector_work WHERE kind = %s"
            " AND status IN ('pending', 'waiting_retry') AND NOT eligibility_recheck",
            (kind,),
        ).fetchone()[0]


def _run_until_finished(
    database: CollectorDatabase, connection_info: str, tmp_path,
    kinds: tuple[str, ...], seconds: float,
) -> float:
    """Run the whole collector until all work of ``kinds`` finished, or ``seconds``."""

    def keys(label: str) -> KeyPool:
        return KeyPool(
            [ApiKey(f"{label}-{n}", "secret") for n in (1, 2)],
            starts_per_second=25,
            concurrency_per_key=24,
        )

    async def run(origin: str) -> float:
        collector = Collector(
            database=database,
            spool=Spool(tmp_path / "spool", max_body_bytes=4096),
            archive=None,
            client=OfficialApiClient(
                origin, allow_insecure_test_origin=True, max_body_bytes=4096
            ),
            regular_keys=keys("regular"),
            interactive_keys=keys("interactive"),
            archive_instance_id="fixture",
            collector_version="settlement-test",
            max_body_bytes=4096,
            regular_parallelism=RESET_SLOTS,
            weekly_eligibility_enabled=True,  # as in production
        )
        stop = asyncio.Event()
        started = time.monotonic()

        async def watch() -> None:
            while not stop.is_set():
                unfinished = [
                    await asyncio.to_thread(_unfinished, connection_info, kind)
                    for kind in kinds
                ]
                if not any(unfinished) or time.monotonic() - started > seconds:
                    stop.set()
                else:
                    await asyncio.sleep(0.25)

        watcher = asyncio.create_task(watch())
        try:
            await collector.run(stop, health_host="127.0.0.1", health_port=0)
        finally:
            stop.set()
            await watcher
        return time.monotonic() - started

    with _api() as origin:
        try:
            return asyncio.run(run(origin))
        finally:
            database.close()


def test_settlement_checks_start_while_the_discovery_queue_is_full(
    database_url: str, tmp_path
) -> None:
    # 1,000 discovery checks take about 40 s through the two keys; the
    # settlement checks need 24 requests, so they finish in a few seconds
    # only if they get their own slots meanwhile.
    with domain_database(database_url) as connection_info:
        database = _monday_queue(connection_info, discovery_checks=1000)
        elapsed = _run_until_finished(
            database, connection_info, tmp_path, ("reset_settlement",), 20
        )
        assert _unfinished(connection_info, "reset_settlement") == 0, elapsed
        assert _unfinished(connection_info, "discovery_profile") > 0


def test_a_slow_discovery_admission_never_stops_the_collector(
    database_url: str, tmp_path, monkeypatch
) -> None:
    # Every admission outlasts the statement limit, as on 9 November.
    monkeypatch.setattr(collector_liveness, "STATEMENT_TIMEOUT", "1s")
    with domain_database(database_url) as connection_info:
        database = _monday_queue(connection_info, discovery_checks=5)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "CREATE OR REPLACE FUNCTION clashlens_admit_discovery_profiles("
                "instant timestamptz) RETURNS void LANGUAGE sql AS 'SELECT pg_sleep(3)'"
            )
        _run_until_finished(
            database, connection_info, tmp_path,
            ("reset_settlement", "discovery_profile"), 20,
        )
        assert _unfinished(connection_info, "reset_settlement") == 0
        assert _unfinished(connection_info, "discovery_profile") == 0
