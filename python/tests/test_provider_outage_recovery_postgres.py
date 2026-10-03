from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens import boundary_publication, reset_baselines
from clashlens.archive import SpoolFirstReader
from clashlens.collector import Collector
from clashlens.collector_db import CollectorDatabase, CollectorWork
from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient, ProviderOutage
from clashlens.db import Database
from clashlens.spool import Spool
from clashlens.worker import ObservationProcessor

TAG = "#2PP"


class _Provider(BaseHTTPRequestHandler):
    """A fake official API that drops connections, fails, or answers."""

    protocol_version = "HTTP/1.1"
    mode = "drop"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if type(self).mode == "drop":
            self.close_connection = True
            self.connection.shutdown(2)
            return
        unavailable = (
            type(self).mode == "unavailable"
            or (type(self).mode == "battle_log_unavailable" and "/battlelog" in self.path)
            or (type(self).mode == "profile_unavailable" and "/battlelog" not in self.path)
        )
        status = 503 if unavailable else 200
        body = b'{"tag":"#2PP"}' if status == 200 else b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _provider():
    _Provider.mode = "drop"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _latest_reset(now: datetime) -> datetime:
    boundary = now.replace(hour=5, minute=0, second=0, microsecond=0)
    return boundary if boundary <= now else boundary - timedelta(days=1)


def _collector(origin: str, database: CollectorDatabase, spool: Spool) -> Collector:
    def keys(label: str) -> KeyPool:
        return KeyPool([ApiKey(label, "secret")], starts_per_second=25, concurrency_per_key=6)

    return Collector(
        database=database,
        spool=spool,
        archive=None,
        client=OfficialApiClient(origin, allow_insecure_test_origin=True, max_body_bytes=4096),
        regular_keys=keys("regular-1"),
        interactive_keys=keys("interactive-1"),
        archive_instance_id="fixture",
        collector_version="reset-outage-test",
        max_body_bytes=4096,
    )


def _reset_work(
    connection_info: str, boundary: datetime
) -> tuple[CollectorDatabase, int]:
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO players (normalized_tag, active, next_due_at) VALUES (%s, true, %s)",
            (TAG, boundary),
        )
    database = CollectorDatabase(connection_info)
    sweep_id = database.begin_reset(boundary)
    assert sweep_id is not None
    return database, sweep_id


def _collect_reset(collector: Collector, database: CollectorDatabase) -> str:
    (intent,) = database.pending_intents(
        limit=10, now=datetime.now(UTC) + timedelta(minutes=1), interactive=False
    )
    return asyncio.run(collector.collect_intent(intent))


def _work(connection_info: str) -> tuple[str, int | None, int | None]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT status, profile_observation_id, battle_log_observation_id"
            " FROM collector_work WHERE kind = 'reset_baseline'"
        ).fetchone()


def test_reset_outage_stays_retryable_and_collects_once_the_api_returns(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retry could be checked")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, sweep_id = _reset_work(connection_info, boundary)
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))

        assert _collect_reset(collector, database) == "retrying"
        assert _work(connection_info)[0] == "waiting_retry"
        # Ordinary collection keeps waiting for the Reset, which is not lost.
        assert database.reset_ready(sweep_id) is False

        _Provider.mode = "answer"
        assert _collect_reset(collector, database) == "complete"
        status, profile_id, battle_log_id = _work(connection_info)
        assert status == "complete" and profile_id and battle_log_id


@pytest.mark.parametrize("days_ago", [1, 3])
def test_reset_given_up_without_any_response_still_settles_its_publication(
    database_url: str, tmp_path, days_ago: int
) -> None:
    # An earlier Reset: retrying stopped when its Legend day ended. However
    # long the worker was stopped, its (re)start still settles the work.
    boundary = _latest_reset(datetime.now(UTC)) - timedelta(days=days_ago)
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, _sweep_id = _reset_work(connection_info, boundary)
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))

        assert _collect_reset(collector, database) == "failed"
        assert _work(connection_info) == ("failed", None, None)

        # A worker (re)start revisits the failed work: no processing job
        # exists, yet the Reset records failed evidence and its publication
        # stops waiting for this player, even with one database connection.
        worker = Database(connection_info, max_size=1)
        try:
            boundary_publication.reevaluate_boundary_publications(worker)
            boundary_publication.reevaluate_boundary_publications(worker)
        finally:
            worker.close()
        with psycopg.connect(connection_info) as connection:
            evidence = connection.execute(
                "SELECT state, failure_reasons FROM reset_baseline_evidence"
            ).fetchall()
            members = connection.execute(
                "SELECT member.snapshot_status, member.army_status"
                " FROM boundary_publication_generation_members AS member"
                " JOIN boundary_publication_generations AS generation"
                "   ON generation.id = member.generation_id"
                " WHERE generation.boundary_at = %s",
                (boundary,),
            ).fetchall()
        assert evidence == [
            ("failed", ["missing_profile_observation", "missing_battle_log_observation"])
        ]
        assert members == [("unavailable", "unavailable")]


def test_reset_recovery_reaches_later_work_past_rows_waiting_on_processing(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before its responses could be collected")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        with psycopg.connect(connection_info) as connection:
            for tag in (TAG, "#8QV"):
                connection.execute(
                    "INSERT INTO players (normalized_tag, active, next_due_at) VALUES (%s, true, %s)",
                    (tag, boundary),
                )
        database = CollectorDatabase(connection_info)
        assert database.begin_reset(boundary) is not None
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))
        first, later = sorted(
            database.pending_intents(
                limit=10, now=datetime.now(UTC) + timedelta(minutes=1), interactive=False
            ),
            key=lambda intent: intent.work_id,
        )
        # The first work saved responses inside the Reset window whose
        # processing has not finished, for example because their file cannot
        # be read yet; the later work got no response at all. Both then fail.
        _Provider.mode = "battle_log_unavailable"
        assert asyncio.run(collector.collect_intent(first)) == "retrying"
        _Provider.mode = "drop"
        assert asyncio.run(collector.collect_intent(later)) == "retrying"
        for intent in (first, later):
            assert database.fail_intent(intent.work_id, category="provider_failure") == "failed"

        worker = Database(connection_info)
        try:
            for _ in range(2):
                reset_baselines.settle_failed_reset_work(worker, max_works=1)
        finally:
            worker.close()
        with psycopg.connect(connection_info) as connection:
            states = dict(
                connection.execute(
                    "SELECT collector_work_id, state FROM reset_baseline_evidence"
                ).fetchall()
            )
        assert states == {first.work_id: "partial", later.work_id: "failed"}


def test_reset_retry_keeps_the_profile_that_already_answered(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retry could be checked")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, _sweep_id = _reset_work(connection_info, boundary)
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))
        _Provider.mode = "battle_log_unavailable"

        assert _collect_reset(collector, database) == "retrying"
        status, early_profile, failed_battle_log = _work(connection_info)
        assert status == "waiting_retry" and early_profile and failed_battle_log

        # The retry fetches only the battle log, so the profile collected
        # closest to the Reset stays the one the work proves it with.
        _Provider.mode = "answer"
        assert _collect_reset(collector, database) == "complete"
        status, profile_id, battle_log_id = _work(connection_info)
        assert status == "complete"
        assert profile_id == early_profile
        assert battle_log_id not in (None, failed_battle_log)


def test_retried_reset_profile_brings_a_battle_log_collected_after_it(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retry could be checked")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, _sweep_id = _reset_work(connection_info, boundary)
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))
        _Provider.mode = "profile_unavailable"

        assert _collect_reset(collector, database) == "retrying"
        early_battle_log = _work(connection_info)[2]
        assert early_battle_log

        # The retried profile brings a fresh battle log, collected after it.
        _Provider.mode = "answer"
        assert _collect_reset(collector, database) == "complete"
        assert _work(connection_info)[2] not in (None, early_battle_log)
        with psycopg.connect(connection_info) as connection:
            profile_at, battle_log_at = connection.execute(
                "SELECT profile.response_completed_at, battle_log.request_started_at"
                " FROM collector_work AS work"
                " JOIN collector_observations AS profile ON profile.id = work.profile_observation_id"
                " JOIN collector_observations AS battle_log"
                "   ON battle_log.id = work.battle_log_observation_id"
                " WHERE work.kind = 'reset_baseline'"
            ).fetchone()
        assert battle_log_at >= profile_at


def test_interrupted_reset_retry_fetches_the_battle_log_again_after_restart(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retry could be checked")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, _sweep_id = _reset_work(connection_info, boundary)
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))
        _Provider.mode = "profile_unavailable"
        assert _collect_reset(collector, database) == "retrying"
        early_battle_log = _work(connection_info)[2]

        # The retry saves a good profile, then stops before its battle log.
        _Provider.mode = "answer"
        (intent,) = database.pending_intents(
            limit=10, now=datetime.now(UTC) + timedelta(minutes=1), interactive=False
        )
        work = CollectorWork(
            intent.player_id, TAG, intent.due_at, collector_work_id=intent.work_id
        )
        assert asyncio.run(
            collector.collect_player(work, lane="reset", endpoints=("profile",))
        ) == ["recorded"]

        # After a restart the older battle log is still fetched again.
        (resumed,) = database.pending_intents(
            limit=10, now=datetime.now(UTC) + timedelta(minutes=1), interactive=False
        )
        assert not resumed.profile_required and resumed.battle_log_required
        assert asyncio.run(_collector(origin, database, collector.spool).collect_intent(resumed)) == "complete"
        assert _work(connection_info)[2] not in (None, early_battle_log)


def test_reset_server_error_is_not_final_while_the_collector_retries(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retry could be checked")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, _sweep_id = _reset_work(connection_info, boundary)
        spool = Spool(tmp_path / "spool", max_body_bytes=4096)
        collector = _collector(origin, database, spool)
        _Provider.mode = "unavailable"

        assert _collect_reset(collector, database) == "retrying"
        worker = Database(connection_info)
        try:
            reader = SpoolFirstReader(
                SimpleNamespace(max_body_bytes=4096),
                spool_root=str(spool.root),
                max_body_bytes=4096,
                validate_database=False,
            )
            results = ObservationProcessor(worker, reader).process_until_idle(
                owner="outage-worker", max_jobs=10
            )
        finally:
            worker.close()
        assert {result.outcome for result in results} == {"classified"}
        with psycopg.connect(connection_info) as connection:
            evidence = connection.execute(
                "SELECT state, failure_reasons FROM reset_baseline_evidence"
                " ORDER BY version DESC LIMIT 1"
            ).fetchone()
            unavailable = connection.execute(
                "SELECT count(*) FROM boundary_publication_generation_members"
                " WHERE snapshot_status = 'unavailable'"
            ).fetchone()[0]
        assert evidence[0] == "partial"
        assert "profile_non_success_retrying" in evidence[1]
        assert unavailable == 0


def test_collection_resumes_after_an_outage_with_the_newest_response_first(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "INSERT INTO players (normalized_tag, active, next_due_at)"
                " VALUES (%s, true, now()) RETURNING id",
                (TAG,),
            ).fetchone()[0]
        database = CollectorDatabase(connection_info)
        collector = _collector(
            origin, database, Spool(tmp_path / "spool", max_body_bytes=4096)
        )
        outage = ProviderOutage(threshold=2, base_delay=1.0, max_delay=1.0)
        collector.client.provider_outage = outage
        work = CollectorWork(player_id, TAG, datetime.now(UTC))
        _Provider.mode = "unavailable"

        async def run() -> list[str]:
            for _ in range(2):
                await collector.collect_player(work, lane="ordinary")
            assert outage.active
            # A regular check during the pause waits as paused work.
            assert await collector.collect_player(work, lane="ordinary") == ["capacity_paused"] * 2
            _Provider.mode = "answer"
            await asyncio.sleep(1.1)
            return await asyncio.wait_for(collector.collect_player(work, lane="ordinary"), 5)

        assert asyncio.run(run())[0] == "recorded"
        assert not outage.active

        worker = Database(connection_info)
        try:
            first = worker.newest_job_plan(limit=10)[0]
        finally:
            worker.close()
        with psycopg.connect(connection_info) as connection:
            status = connection.execute(
                "SELECT observation.http_status FROM python_processing_jobs AS job"
                " JOIN collector_observations AS observation"
                "   ON observation.id = job.observation_id WHERE job.id = %s",
                (first,),
            ).fetchone()[0]
        # The worker starts with the response saved after recovery, not with
        # the server errors saved during the outage.
        assert status == 200


def test_reset_failing_while_the_api_answers_settles_after_three_retries(
    database_url: str, tmp_path
) -> None:
    now = datetime.now(UTC)
    boundary = _latest_reset(now)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retry could be checked")
    with domain_database(database_url, include_coordinator=True) as connection_info, _provider() as origin:
        database, _sweep_id = _reset_work(connection_info, boundary)
        collector = _collector(origin, database, Spool(tmp_path / "spool", max_body_bytes=4096))

        async def collect() -> str:
            (intent,) = database.pending_intents(
                limit=10, now=datetime.now(UTC) + timedelta(minutes=1), interactive=False
            )
            return await collector.collect_intent(intent)

        async def run() -> None:
            # During a provider-outage pause, retries are not counted.
            collector.client.provider_outage = ProviderOutage(
                threshold=1, base_delay=0.01, max_delay=0.01
            )
            for _ in range(5):
                assert await collect() == "retrying"
            # Only this player keeps failing; the API answers for others.
            collector.client.provider_outage = ProviderOutage(threshold=10**6)
            for _ in range(3):
                assert await collect() == "retrying"
                assert database.claim_due_players(limit=10) == []
            assert await collect() == "failed"

        asyncio.run(run())

        # The day can publish without this player, and ordinary collection
        # resumes instead of waiting for the Legend day to end.
        worker = Database(connection_info)
        try:
            boundary_publication.reevaluate_boundary_publications(worker)
        finally:
            worker.close()
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT state FROM reset_baseline_evidence"
            ).fetchall() == [("failed",)]
        assert [work.normalized_tag for work in database.claim_due_players(limit=10)] == [TAG]
