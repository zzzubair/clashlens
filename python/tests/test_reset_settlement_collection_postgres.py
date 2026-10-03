"""One delayed settlement check per frozen Reset member, with results unchanged.

Twenty minutes after each 05:00 UTC Reset the collector fetches a fresh
profile, saves it, then fetches the battle log that must cover it. Nothing
reads the pair yet: it is evidence for a later step that decides whether the
Reset trophies settled.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_collector_db_postgres import _handoff, _hash
from test_reconciliation_postgres import DAY_END, _battle_log, _processor, _profile

from clashlens import collector_http, collector_reset, late_battle_sweep
from clashlens.collector import Collector
from clashlens.collector_db import CollectorDatabase, CollectorWork
from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient, ProviderOutage
from clashlens.spool import Spool

TAG = "#2PP"
SEASON_RESET = datetime(2026, 9, 7, 5, tzinfo=UTC)
MONDAY_RESET = datetime(2026, 9, 14, 5, tzinfo=UTC)
WEDNESDAY_RESET = datetime(2026, 9, 16, 5, tzinfo=UTC)


class _Provider(BaseHTTPRequestHandler):
    """A fake official API that records each request's path and start."""

    protocol_version = "HTTP/1.1"
    status: ClassVar[dict[str, int]] = {}
    requests: ClassVar[list[tuple[str, float]]] = []
    profile_delay: ClassVar[float] = 0.0

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        endpoint = self.path.rsplit("/", 1)[-1]
        endpoint = endpoint if endpoint in {"battlelog", "leaguehistory"} else "profile"
        type(self).requests.append((endpoint, time.time()))
        if endpoint == "profile":
            time.sleep(type(self).profile_delay)
        status = type(self).status[endpoint]
        body = (b'{"items":[]}' if endpoint != "profile" else b'{"tag":"#2PP"}')
        body = body if status == 200 else b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _provider():
    _Provider.status = {"profile": 200, "battlelog": 200, "leaguehistory": 200}
    _Provider.requests = []
    _Provider.profile_delay = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _collector(origin: str, database: CollectorDatabase, tmp_path) -> Collector:
    def keys(label: str) -> KeyPool:
        return KeyPool(
            [ApiKey(label, "secret")], starts_per_second=25, concurrency_per_key=6
        )

    return Collector(
        database=database,
        spool=Spool(tmp_path / "spool", max_body_bytes=4096),
        archive=None,
        client=OfficialApiClient(
            origin, allow_insecure_test_origin=True, max_body_bytes=4096
        ),
        regular_keys=keys("regular-1"),
        interactive_keys=keys("interactive-1"),
        archive_instance_id="fixture",
        collector_version="settlement-test",
        max_body_bytes=4096,
    )


def _players(connection_info: str, *tags: str, active: bool = True) -> list[int]:
    with psycopg.connect(connection_info) as connection:
        return [
            connection.execute(
                "INSERT INTO players (normalized_tag, active, next_due_at)"
                " VALUES (%s, %s, %s) RETURNING id",
                (tag, active, SEASON_RESET if active else None),
            ).fetchone()[0]
            for tag in tags
        ]


def _settlement_rows(connection_info: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT work.sweep_id, work.player_id, work.due_at - sweep.boundary_at,
                   work.lane, work.league_history_status, settlement.delayed_work_id = work.id
            FROM collector_work AS work
            JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
            LEFT JOIN reset_boundary_settlements AS settlement
              ON settlement.player_id = work.player_id
             AND settlement.boundary_at = sweep.boundary_at
            WHERE work.kind = 'reset_settlement'
            ORDER BY work.sweep_id, work.player_id
            """
        ).fetchall()


def _settlement_intents(database: CollectorDatabase, now: datetime) -> list:
    return [
        intent
        for intent in database.pending_intents(limit=20, now=now, interactive=False)
        if intent.kind == "reset_settlement"
    ]


def _work(connection_info: str) -> tuple:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT work.status, work.profile_observation_id,
                   work.battle_log_observation_id, profile.http_status,
                   profile.response_completed_at, log.request_started_at,
                   work.failure_category
            FROM collector_work AS work
            LEFT JOIN collector_observations AS profile ON profile.id = work.profile_observation_id
            LEFT JOIN collector_observations AS log ON log.id = work.battle_log_observation_id
            WHERE work.kind = 'reset_settlement'
            """
        ).fetchone()


def _finish_reset_pairs(connection_info: str) -> None:
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "UPDATE collector_work SET status = 'failed', updated_at = clock_timestamp()"
            " WHERE kind = 'reset_baseline'"
        )


def _clock_from(monkeypatch, at: datetime) -> None:
    """Run the API client's clock from ``at``, so a past Reset's check runs."""
    offset = datetime.now(UTC) - at

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - offset

    monkeypatch.setattr(collector_http, "datetime", Clock)


def _current_reset(now: datetime) -> datetime:
    boundary = now.replace(hour=5, minute=0, second=0, microsecond=0)
    boundary = boundary if boundary <= now else boundary - timedelta(days=1)
    if now - boundary > timedelta(hours=23, minutes=50):
        pytest.skip("this Legend day ends before the retries could be checked")
    return boundary


def test_one_delayed_work_per_frozen_member_survives_restart(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # The collector schedules and links checks with its own permissions.
        options = conninfo_to_dict(connection_info).get("options", "")
        collector_role = make_conninfo(
            connection_info, options=f"{options} -c role=clashlens_collector"
        )
        empty = CollectorDatabase(collector_role).begin_reset(SEASON_RESET)
        assert empty is not None and _settlement_rows(connection_info) == []

        first, second = _players(connection_info, "#2PP", "#8QV")
        _players(connection_info, "#9LL", active=False)
        databases = [CollectorDatabase(collector_role) for _ in range(2)]
        sweeps = []
        threads = [
            threading.Thread(
                target=lambda database=database: sweeps.append(
                    database.begin_reset(MONDAY_RESET)
                )
            )
            for database in databases
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sweep_id = sweeps[0]
        assert sweeps == [sweep_id, sweep_id]
        expected = [
            (sweep_id, player, timedelta(minutes=20), "ordinary", "not_applicable", True)
            for player in (first, second)
        ]
        assert _settlement_rows(connection_info) == expected

        # A member leaving after the freeze keeps its check; a newcomer gets none.
        # Restarts at 05:19 and 05:21 and finished or failed checks add nothing.
        _players(connection_info, "#QQQ")
        with psycopg.connect(connection_info) as connection:
            connection.execute("UPDATE players SET active = false WHERE id = %s", (second,))
        assert databases[0].begin_reset(MONDAY_RESET) == sweep_id
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = now(),"
                " profile_status = 'observed', battle_log_status = 'observed'"
                " WHERE kind = 'reset_settlement' AND player_id = %s",
                (first,),
            )
            connection.execute(
                "UPDATE collector_work SET status = 'failed'"
                " WHERE kind = 'reset_settlement' AND player_id = %s",
                (second,),
            )
        assert databases[1].begin_reset(MONDAY_RESET) == sweep_id
        assert _settlement_rows(connection_info) == expected
        with psycopg.connect(connection_info) as connection, pytest.raises(
            psycopg.errors.UniqueViolation
        ):
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at,
                    coalescing_key, sweep_id, league_history_status
                ) VALUES ('reset_settlement', 'ordinary', 'player', %s, '#2PP',
                          now(), 'another-key', %s, 'not_applicable')
                """,
                (first, sweep_id),
            )


def test_settlement_always_fetches_profile_then_covering_log(
    database_url: str, tmp_path
) -> None:
    boundary = _current_reset(datetime.now(UTC))
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        _provider() as origin,
    ):
        (player_id,) = _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        database.begin_reset(boundary)
        collector = _collector(origin, database, tmp_path)
        # An ordinary profile just collected would let an ordinary check reuse
        # it and skip the battle log; the settlement check fetches both again.
        ordinary = asyncio.run(
            collector.collect_player(
                CollectorWork(player_id, TAG, boundary), lane="ordinary"
            )
        )
        assert ordinary[0] == "recorded"
        _Provider.requests.clear()
        _Provider.status["battlelog"] = 503

        (intent,) = _settlement_intents(database, boundary + timedelta(minutes=21))
        assert asyncio.run(collector.collect_intent(intent)) == "retrying"
        status, profile_id, failed_log, profile_status, *_rest = _work(connection_info)
        assert (status, profile_status) == ("waiting_retry", 200) and failed_log
        assert [endpoint for endpoint, _at in _Provider.requests][:2] == [
            "profile", "battlelog"
        ]

        # The retry keeps the saved profile and fetches only a later battle log.
        _Provider.status["battlelog"] = 200
        (retry,) = _settlement_intents(database, datetime.now(UTC) + timedelta(minutes=1))
        assert (retry.profile_required, retry.battle_log_required) == (False, True)
        assert asyncio.run(collector.collect_intent(retry)) == "complete"
        status, kept, log_id, _status, profile_done, log_started, _ = _work(connection_info)
        assert status == "complete" and kept == profile_id
        assert log_id not in (None, failed_log) and log_started >= profile_done
        assert sum(endpoint == "profile" for endpoint, _at in _Provider.requests) == 1

        # Unchanged bytes still get their own saved responses.
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT count(*) FROM collector_observations WHERE endpoint = 'profile'"
            ).fetchone()[0] == 2

        # A later duplicate for the same check never replaces its pair.
        with psycopg.connect(connection_info) as connection:
            work_id = connection.execute(
                "SELECT id FROM collector_work WHERE kind = 'reset_settlement'"
            ).fetchone()[0]
        for endpoint in ("profile", "battle_log"):
            database.record_response(
                _handoff(
                    occurrence_key=f"duplicate-{endpoint}",
                    response_hash=_hash(f"duplicate-{endpoint}"),
                    player_id=player_id,
                    endpoint=endpoint,
                    completed_at=datetime.now(UTC),
                    collector_work_id=work_id,
                )
            )
        assert _work(connection_info)[1:3] == (profile_id, log_id)
        with pytest.raises(ValueError, match="identity"):
            database.record_response(
                _handoff(
                    occurrence_key="wrong-player",
                    response_hash=_hash("wrong-player"),
                    player_id=_players(connection_info, "#8QV")[0],
                    tag="#8QV",
                    completed_at=datetime.now(UTC),
                    collector_work_id=work_id,
                )
            )


def test_log_started_before_its_profile_arrived_is_fetched_again(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        (player_id,) = _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        database.begin_reset(WEDNESDAY_RESET)
        with psycopg.connect(connection_info) as connection:
            work_id = connection.execute(
                "SELECT id FROM collector_work WHERE kind = 'reset_settlement'"
            ).fetchone()[0]
        profile_at = WEDNESDAY_RESET + timedelta(minutes=20, seconds=2)
        # The log finished after the profile but started 0.5 s before it arrived.
        for key, endpoint, completed_at in (
            ("profile", "profile", profile_at),
            ("early-log", "battle_log", profile_at + timedelta(milliseconds=500)),
        ):
            database.record_response(
                _handoff(
                    occurrence_key=key, response_hash=_hash(key), player_id=player_id,
                    endpoint=endpoint, completed_at=completed_at,
                    collector_work_id=work_id,
                )
            )
        (intent,) = _settlement_intents(database, WEDNESDAY_RESET + timedelta(hours=1))
        assert (intent.profile_required, intent.battle_log_required) == (False, True)

        database.record_response(
            _handoff(
                occurrence_key="covering-log", response_hash=_hash("covering-log"),
                player_id=player_id, endpoint="battle_log",
                completed_at=profile_at + timedelta(seconds=5), collector_work_id=work_id,
            )
        )
        assert _settlement_intents(database, WEDNESDAY_RESET + timedelta(hours=1))[0] \
            .battle_log_required is False
        assert database.complete_intent(work_id) is True


def test_changed_old_day_log_is_preserved_without_reset_hash_match(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        (player_id,) = _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        sweep_id = database.begin_reset(WEDNESDAY_RESET)
        with psycopg.connect(connection_info) as connection:
            works = dict(connection.execute(
                "SELECT kind, id FROM collector_work WHERE sweep_id = %s", (sweep_id,)
            ).fetchall())
        at = WEDNESDAY_RESET

        def save(key: str, endpoint: str, minutes: float, body: str, work: str | None):
            return database.record_response(
                _handoff(
                    occurrence_key=key, response_hash=_hash(body), player_id=player_id,
                    endpoint=endpoint, completed_at=at + timedelta(minutes=minutes),
                    collector_work_id=None if work is None else works[work],
                )
            )

        save("early-profile", "profile", 0.5, "profile-5766", "reset_baseline")
        save("early-log", "battle_log", 0.6, "log-without-04:59:50", "reset_baseline")
        # The named profile repeats the early bytes and is still saved. The
        # named log adds yesterday's 04:59:50 battle, so it matches no early
        # Reset bytes; ordinary profiles then change role and other fields.
        assert save("named-profile", "profile", 20.1, "profile-5766", "reset_settlement").changed
        named = save("named-log", "battle_log", 20.3, "log-with-04:59:50", "reset_settlement")
        for minutes, body in ((21, "profile-role-elder"), (22, "profile-role-leader"),
                              (23, "profile-donations")):
            save(f"ordinary-{minutes}", "profile", minutes, body, None)
        repeated = save("ordinary-log", "battle_log", 24, "log-with-04:59:50", None)

        assert named.changed and named.observation_id is not None
        assert repeated.changed is False  # the ordinary repeat compacts
        with psycopg.connect(connection_info) as connection:
            kept = connection.execute(
                """
                SELECT work.battle_log_observation_id, observation.response_hash,
                       EXISTS (SELECT 1 FROM python_processing_jobs AS job
                               WHERE job.observation_id = observation.id)
                FROM collector_work AS work
                JOIN collector_observations AS observation
                  ON observation.id = work.battle_log_observation_id
                WHERE work.id = %s
                """,
                (works["reset_settlement"],),
            ).fetchone()
        assert kept == (named.observation_id, _hash("log-with-04:59:50"), True)


@pytest.mark.parametrize("old_order", [("profile", "battle_log"), ("battle_log", "profile")])
def test_saved_pair_processes_after_newer_profile_and_gapped_log(
    database_url: str, archive_server, old_order
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observations, jobs = {}, {}
        for key, endpoint, body, minutes in (
            ("profile", "profile", _profile(5766), 20),
            ("battle_log", "battle_log", _battle_log(), 20.1),
            ("newer_profile", "profile", _profile(5726), 40),
            ("newer_log", "battle_log", _battle_log(empty=True), 40.1),
        ):
            observations[key], jobs[key] = store_observation(
                connection_info, archive_server, occurrence_key=f"pair-{key}",
                endpoint=endpoint, body=body, normalized_tag=TAG,
                observed_at=DAY_END + timedelta(minutes=minutes),
            )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                WITH player AS (SELECT id FROM players WHERE normalized_tag = %(tag)s),
                sweep AS (
                    INSERT INTO collector_reset_sweeps (boundary_at, member_ids, membership_captured_at)
                    SELECT %(at)s, ARRAY[player.id], %(at)s FROM player RETURNING id
                )
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                    coalescing_key, status, profile_status, battle_log_status,
                    league_history_status, profile_observation_id,
                    battle_log_observation_id, completed_at
                ) SELECT 'reset_settlement', 'ordinary', 'player', player.id, %(tag)s,
                         sweep.id, %(at)s + interval '20 minutes', 'settlement-pair',
                         'complete', 'observed', 'observed', 'not_applicable',
                         %(profile)s, %(log)s, now()
                  FROM player, sweep
                """,
                {"tag": TAG, "at": DAY_END, "profile": observations["profile"],
                 "log": observations["battle_log"]},
            )
        database, processor = _processor(connection_info, archive_server)
        try:
            outcomes = {
                key: processor.process_job(jobs[key], owner=f"pair-{key}").outcome
                for key in ("newer_profile", "newer_log", *old_order)
            }
            with database.pool.connection() as connection:
                live, applied = connection.execute(
                    """
                    SELECT version.trophies,
                           (SELECT count(*) FROM player_profile_effects
                            WHERE observation_id = %s)
                    FROM players AS player
                    JOIN player_profile_versions AS version
                      ON version.id = player.current_profile_version_id
                    WHERE player.normalized_tag = %s
                    """,
                    (observations["profile"], TAG),
                ).fetchone()
        finally:
            database.close()
    assert outcomes == dict.fromkeys(outcomes, "processed")
    assert (live, applied) == (5726, 1)


def test_stalled_0520_pass_uses_actual_times_and_expires_without_new_requests(
    database_url: str, tmp_path
) -> None:
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        _provider() as origin,
    ):
        first, _second = _players(connection_info, TAG, "#8QV")
        database = CollectorDatabase(connection_info)
        database.begin_reset(WEDNESDAY_RESET)
        deadline = WEDNESDAY_RESET + timedelta(hours=23, minutes=55)
        assert len(_settlement_intents(database, deadline - timedelta(seconds=1))) == 2
        assert _settlement_intents(database, deadline) == []

        # A stalled pass saves the first profile late in the day, then stops.
        (intent,) = [
            intent
            for intent in _settlement_intents(database, deadline - timedelta(minutes=1))
            if intent.player_id == first
        ]
        collector = _collector(origin, database, tmp_path)
        work = CollectorWork(first, TAG, intent.due_at, collector_work_id=intent.work_id)
        saved = asyncio.run(collector.collect_player(work, lane="reset", endpoints=("profile",)))
        assert saved == ["recorded"]
        requests = len(_Provider.requests)

        # The scheduling loop expires closed windows in bounded batches.
        assert database.expire_settlement_checks(deadline - timedelta(seconds=1)) == 0
        with psycopg.connect(connection_info) as connection:
            assert collector_reset.expire_settlement_checks(connection, deadline, batch=1) == 1
        assert database.expire_settlement_checks(deadline) == 1
        assert database.expire_settlement_checks(deadline) == 0
        assert len(_Provider.requests) == requests
        with psycopg.connect(connection_info) as connection:
            rows = connection.execute(
                """
                SELECT work.status, work.failure_category, profile.request_started_at,
                       EXISTS (SELECT 1 FROM python_processing_jobs AS job
                               WHERE job.observation_id = profile.id)
                FROM collector_work AS work
                JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
                LEFT JOIN collector_observations AS profile
                  ON profile.id = work.profile_observation_id
                WHERE work.kind = 'reset_settlement' AND sweep.boundary_at = %s
                ORDER BY work.player_id
                """,
                (WEDNESDAY_RESET,),
            ).fetchall()
        # The saved profile keeps its real request time and its processing.
        (status, reason, started, queued), unanswered = rows
        assert (status, reason, queued) == ("failed", "settlement_expired", True)
        assert started > WEDNESDAY_RESET + timedelta(days=1)
        assert unanswered == ("failed", "settlement_expired", None, False)


def test_no_request_starts_after_the_cutoff_once_a_check_is_running(
    database_url: str, tmp_path
) -> None:
    boundary = _current_reset(datetime.now(UTC))
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        _provider() as origin,
    ):
        _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        database.begin_reset(boundary)
        (intent,) = _settlement_intents(database, boundary + timedelta(minutes=21))
        assert intent.collect_before == boundary + timedelta(hours=23, minutes=55)
        # Selected just before the cutoff, its profile arrives just after it.
        intent = dataclasses.replace(
            intent, collect_before=datetime.now(UTC) + timedelta(seconds=0.3)
        )
        _Provider.profile_delay = 0.6
        collector = _collector(origin, database, tmp_path)
        assert asyncio.run(collector.collect_intent(intent)) == "window_closed"
        assert asyncio.run(collector.collect_intent(intent)) == "window_closed"
        assert [endpoint for endpoint, _at in _Provider.requests] == ["profile"]
        # The saved profile stays referenced; the row waits for expiry.
        status, profile_id, log_id, profile_status, *_ = _work(connection_info)
        assert (status, profile_status, log_id) == ("pending", 200, None) and profile_id


def test_pending_settlement_does_not_block_regular_or_next_reset(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _players(connection_info, "#2PP", "#8QV", "#9LL", "#QQQ")
        database = CollectorDatabase(connection_info)
        sweep_id = database.begin_reset(WEDNESDAY_RESET)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                UPDATE collector_work AS work
                SET status = state.status,
                    completed_at = CASE WHEN state.status = 'complete' THEN now() END,
                    profile_status = CASE WHEN state.status = 'complete' THEN 'observed' ELSE profile_status END,
                    battle_log_status = CASE WHEN state.status = 'complete' THEN 'observed' ELSE battle_log_status END
                FROM (SELECT id, (ARRAY['pending', 'waiting_retry', 'complete', 'failed'])[row_number() OVER (ORDER BY id)::int] AS status
                      FROM collector_work WHERE kind = 'reset_settlement') AS state
                WHERE work.id = state.id
                """
            )
            work_id, player_id, tag = connection.execute(
                "SELECT id, player_id, normalized_tag FROM collector_work"
                " WHERE kind = 'reset_settlement' AND status = 'pending'"
            ).fetchone()

        def open_and_ready() -> tuple[bool, bool, bool]:
            with psycopg.connect(connection_info) as connection:
                finished = late_battle_sweep.reset_work_finished(connection, WEDNESDAY_RESET)
            return (
                database.regular_admission_open(WEDNESDAY_RESET + timedelta(minutes=30)),
                database.reset_ready(sweep_id),
                finished,
            )

        # Its saved profile waits for processing; the late-battle check does not.
        database.record_response(
            _handoff(
                occurrence_key="queued-settlement", response_hash=_hash("queued-settlement"),
                player_id=player_id, tag=tag,
                completed_at=WEDNESDAY_RESET + timedelta(minutes=21),
                collector_work_id=work_id,
            )
        )

        # Unfinished Reset pairs still hold everything, as before.
        assert open_and_ready() == (False, False, False)
        assert database.begin_reset(WEDNESDAY_RESET + timedelta(days=1)) is None
        _finish_reset_pairs(connection_info)
        assert open_and_ready() == (True, True, True)
        assert database.begin_reset(WEDNESDAY_RESET + timedelta(days=1)) is not None


def test_transport_retry_keeps_pinned_profile_and_stays_bounded(
    database_url: str, tmp_path
) -> None:
    boundary = _current_reset(datetime.now(UTC))
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        _provider() as origin,
    ):
        _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        database.begin_reset(boundary)
        collector = _collector(origin, database, tmp_path)
        _Provider.status["battlelog"] = 503

        async def collect() -> str:
            (intent,) = _settlement_intents(
                database,
                max(datetime.now(UTC) + timedelta(minutes=1), boundary + timedelta(minutes=21)),
            )
            return await collector.collect_intent(intent)

        async def run() -> list[str]:
            # During a provider-outage pause, retries are not counted.
            collector.client.provider_outage = ProviderOutage(
                threshold=1, base_delay=0.01, max_delay=0.01
            )
            results = [await collect() for _ in range(4)]
            collector.client.provider_outage = ProviderOutage(threshold=10**6)
            return results + [await collect() for _ in range(4)]

        assert asyncio.run(run()) == ["retrying"] * 7 + ["failed"]
        status, profile_id, log_id, profile_status, *_ = _work(connection_info)
        assert (status, profile_status) == ("failed", 200) and profile_id and log_id
        # The one usable profile was fetched once and stays the check's profile.
        assert sum(endpoint == "profile" for endpoint, _at in _Provider.requests) == 1


def test_missing_player_profile_still_brings_its_battle_log(
    database_url: str, tmp_path, monkeypatch
) -> None:
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        _provider() as origin,
    ):
        _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        database.begin_reset(WEDNESDAY_RESET)
        _Provider.status["profile"] = 404
        (intent,) = _settlement_intents(database, WEDNESDAY_RESET + timedelta(minutes=20))
        _clock_from(monkeypatch, intent.due_at)
        assert asyncio.run(_collector(origin, database, tmp_path).collect_intent(intent)) == "complete"
        assert [endpoint for endpoint, _at in _Provider.requests] == ["profile", "battlelog"]
        assert _work(connection_info)[3] == 404


@pytest.mark.parametrize("boundary", [MONDAY_RESET, SEASON_RESET], ids=["monday", "season"])
def test_monday_and_season_checks_have_two_endpoints_and_no_acceptance(
    database_url: str, tmp_path, monkeypatch, boundary
) -> None:
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        _provider() as origin,
    ):
        _players(connection_info, TAG)
        database = CollectorDatabase(connection_info)
        database.begin_reset(boundary)
        (intent,) = _settlement_intents(database, boundary + timedelta(minutes=20))
        assert not intent.league_history_required
        _clock_from(monkeypatch, intent.due_at)
        assert asyncio.run(_collector(origin, database, tmp_path).collect_intent(intent)) == "complete"
        assert [endpoint for endpoint, _at in _Provider.requests] == ["profile", "battlelog"]
        with psycopg.connect(connection_info) as connection:
            early_history = connection.execute(
                "SELECT league_history_status FROM collector_work WHERE kind = 'reset_baseline'"
            ).fetchone()[0]
            settlement = connection.execute(
                "SELECT state, selected_trophies, proof_kind FROM reset_boundary_settlements"
            ).fetchall()
        # The early Season pair still fetches league history; the check never does.
        assert early_history == ("pending" if boundary == SEASON_RESET else "not_applicable")
        assert settlement == [("provisional", None, None)]
