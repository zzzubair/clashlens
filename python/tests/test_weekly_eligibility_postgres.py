from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_collector import _Client, _Spool
from test_collector import _collector as _fake_collector
from test_collector_db_postgres import _handoff
from test_domain_processing_postgres import _processor

from clashlens import profile
from clashlens.collector_db import CollectorDatabase
from clashlens.domain import BOOTSTRAP_CURRENT_SEASON_ID, SEASON_DURATION
from clashlens.history import prune_completed_history
from clashlens.profile import PROFILE_PARSER_VERSION
from clashlens.weekly_eligibility import next_check

MONDAY = datetime(2026, 10, 12, 5, tzinfo=UTC)
PROFILE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


def _collector(info):
    options = conninfo_to_dict(info)["options"] + " -c role=clashlens_collector"
    return CollectorDatabase(make_conninfo(info, options=options))


def _player(info, tag="#2PP"):
    with psycopg.connect(info) as connection:
        return connection.execute(
            """INSERT INTO players (normalized_tag, active, eligibility_state)
               VALUES (%s, false, 'unknown') RETURNING id""", (tag,)
        ).fetchone()[0]


def _profile(info, archive, *, at, eligible=False, tag="#2PP", tier=None):
    payload = json.loads(PROFILE.read_bytes())
    payload["tag"] = tag
    payload["currentLeagueSeasonId"] = int(datetime(2026, 10, 5, 5, tzinfo=UTC).timestamp())
    if not eligible:
        payload["leagueTier"] = tier or {"id": 105000035, "name": "Legend II"}
    observation, job = store_observation(
        info, archive, occurrence_key=f"weekly:{tag}:{at.isoformat()}",
        endpoint="profile", body=json.dumps(payload).encode(), observed_at=at,
        normalized_tag=tag,
    )
    database, processor = _processor(info, archive)
    try:
        result = processor.process_job(job, owner="weekly-test")
        assert result is not None and result.outcome == "processed"
    finally:
        database.close()
    return observation


@pytest.mark.parametrize("offset,expected", [
    (-1, MONDAY - timedelta(days=7)), (0, MONDAY), (1, MONDAY),
    (7 * 86400 - 1, MONDAY), (7 * 86400, MONDAY + timedelta(days=7)),
])
def test_week_starts_at_monday_0500_utc(database_url, offset, expected):
    with domain_database(database_url) as info, psycopg.connect(info) as connection:
        assert connection.execute(
            "SELECT clashlens_eligibility_week(%s)", (MONDAY + timedelta(seconds=offset),)
        ).fetchone()[0] == expected


def test_sunday_check_does_not_satisfy_monday_but_repeats_reuse_it(database_url, archive_server):
    with domain_database(database_url) as info:
        _profile(info, archive_server, at=MONDAY - timedelta(seconds=1))
        database = _collector(info)
        try:
            assert next_check(database, MONDAY - timedelta(microseconds=1), schedule=True) is None
            assert next_check(database, MONDAY, schedule=True) is None  # Reset not captured yet.
            database.begin_reset(MONDAY, local_regular_inflight=0)
            first = next_check(database, MONDAY, schedule=True)
            assert first is not None and first.eligibility_recheck
            assert next_check(database, MONDAY + timedelta(seconds=2), schedule=True).work_id == first.work_id
            _profile(info, archive_server, at=MONDAY + timedelta(seconds=3))
            remaining = next_check(database, MONDAY + timedelta(minutes=1), schedule=True)
            assert remaining is not None and remaining.work_id == first.work_id
            assert not remaining.profile_required and remaining.league_history_required
            spool = _Spool()
            client = _Client(spool)
            collector = _fake_collector(spool, database, client)
            assert asyncio.run(collector.collect_intent(remaining)) == "complete"
            assert client.fetch_count == 1
            assert "fetch:league_history" in spool.events and "fetch:profile" not in spool.events
            assert next_check(database, MONDAY + timedelta(minutes=1), schedule=True) is None
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, false)",
                    ([first.player_id], MONDAY + timedelta(days=2)),
                ).fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM collector_work WHERE eligibility_recheck").fetchone()[0] == 1
            following = MONDAY + timedelta(days=7)
            database.begin_reset(following, local_regular_inflight=0)
            second = next_check(database, following, schedule=True)
            assert second is not None and second.work_id != first.work_id
        finally:
            database.close()


@pytest.mark.parametrize("unchanged,own_failure", [(True, False), (False, False), (False, True)])
@pytest.mark.parametrize("after_reset", [False, True])
@pytest.mark.parametrize("queued_status", ["pending", "waiting_retry"])
@pytest.mark.parametrize("later_failure", [False, True])
def test_queued_weekly_check_reuses_refresh_awaiting_processing(
    database_url, unchanged, own_failure, after_reset, queued_status, later_failure,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            original = database.record_response(_handoff(
                occurrence_key="old-profile", response_hash="a" * 64,
                player_id=player, completed_at=MONDAY - timedelta(days=1),
            ))
            database.record_response(_handoff(
                occurrence_key="old-history", response_hash="d" * 64,
                player_id=player, endpoint="league_history",
                completed_at=MONDAY - timedelta(days=1),
            ))
            database.begin_reset(MONDAY, local_regular_inflight=0)
            queued = next_check(database, MONDAY, schedule=True)
            assert queued is not None and queued.profile_required
            if own_failure:
                database.record_response(_handoff(
                    occurrence_key="failed-weekly-profile", response_hash="c" * 64,
                    player_id=player, collector_work_id=queued.work_id,
                    completed_at=MONDAY + timedelta(seconds=1), http_status=503,
                ))
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE collector_work SET due_at = %s, status = %s WHERE id = %s",
                    (MONDAY + timedelta(seconds=58), queued_status, queued.work_id),
                )
                refresh = connection.execute(
                    """INSERT INTO collector_work (
                           kind, lane, scope, player_id, normalized_tag, due_at,
                           coalescing_key, league_history_status)
                       VALUES ('live_refresh', 'interactive', 'player', %s, '#2PP',
                               %s, 'intervening-refresh', 'not_applicable') RETURNING id""",
                    (player, MONDAY - timedelta(seconds=2)),
                ).fetchone()[0]
            response = database.record_response(_handoff(
                occurrence_key="intervening-profile",
                response_hash=("a" if unchanged else "b") * 64,
                player_id=player, collector_work_id=refresh,
                completed_at=MONDAY + timedelta(seconds=11 if after_reset else -1),
            ))
            assert response.changed is not unchanged
            if later_failure:
                database.record_response(_handoff(
                    occurrence_key="later-refresh-error", response_hash="e" * 64,
                    player_id=player, completed_at=MONDAY + timedelta(seconds=41),
                    http_status=503,
                ))
            admitted = next_check(database, MONDAY + timedelta(seconds=58), schedule=False)
            if after_reset:
                assert admitted is None
            else:
                assert admitted is not None and admitted.work_id == queued.work_id
                assert admitted.profile_required
            with psycopg.connect(info) as connection:
                assert text(connection.execute(
                    "SELECT status FROM collector_work WHERE id = %s", (queued.work_id,),
                ).fetchone()[0]) == ("cancelled" if after_reset else queued_status)
                assert text(connection.execute(
                    "SELECT eligibility_state FROM players WHERE id = %s", (player,),
                ).fetchone()[0]) == "unknown"
                assert connection.execute(
                    "SELECT clashlens_eligibility_checked_since(%s, %s, %s)",
                    (player, MONDAY, MONDAY + timedelta(seconds=58)),
                ).fetchone()[0] is False
                job = original.processing_job_id if unchanged else response.processing_job_id
                assert text(connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE id = %s", (job,),
                ).fetchone()[0]) == "pending"
                assert connection.execute("SELECT count(*) FROM collector_work").fetchone()[0] == 2
        finally:
            database.close()


@pytest.mark.parametrize("scheduled", [False, True])
def test_successful_fetch_survives_failure_before_enqueue(database_url, scheduled):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            database.record_response(_handoff(
                occurrence_key="successful-refresh", response_hash="a" * 64,
                player_id=player, completed_at=MONDAY + timedelta(seconds=1),
            ))
            database.record_response(_handoff(
                occurrence_key="failed-refresh", response_hash="b" * 64,
                player_id=player, completed_at=MONDAY + timedelta(seconds=32), http_status=503,
            ))
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if scheduled else [player], MONDAY + timedelta(seconds=58), scheduled),
                ).fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM collector_work").fetchone()[0] == 0
                assert text(connection.execute("SELECT eligibility_state FROM players").fetchone()[0]) == "unknown"
        finally:
            database.close()


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("unchanged", [False, True])
@pytest.mark.parametrize("after_reset", [False, True])
def test_discovery_admission_reuses_external_profile_and_finishes_history(
    database_url, weekly, unchanged, after_reset,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            database.record_response(_handoff(
                occurrence_key="old-profile", response_hash="a" * 64,
                player_id=player, completed_at=MONDAY - timedelta(days=1),
            ))
            database.begin_reset(MONDAY, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if weekly else [player], MONDAY, weekly),
                ).fetchone()[0] == 1
            response = database.record_response(_handoff(
                occurrence_key="refresh", response_hash=("a" if unchanged else "b") * 64,
                player_id=player, completed_at=MONDAY + timedelta(seconds=1 if after_reset else -1),
            ))
            assert response.changed is not unchanged
            database.record_response(_handoff(
                occurrence_key="refresh-error", response_hash="c" * 64,
                player_id=player, completed_at=MONDAY + timedelta(seconds=32), http_status=503,
            ))
            now = MONDAY + timedelta(seconds=58)
            admitted = (next_check(database, now, schedule=False) if weekly else
                        database.pending_intents(limit=1, now=now, interactive=False)[0])
            assert admitted is not None and admitted.league_history_required
            assert admitted.profile_required is not after_reset
            if after_reset:
                assert not database.complete_intent(admitted.work_id)
                spool = _Spool()
                client = _Client(spool)
                collector = _fake_collector(spool, database, client)
                assert asyncio.run(collector.collect_intent(admitted)) == "complete"
                assert client.fetch_count == 1
                assert "fetch:league_history" in spool.events
                assert "fetch:profile" not in spool.events
            with psycopg.connect(info) as connection:
                assert text(connection.execute("SELECT eligibility_state FROM players").fetchone()[0]) == "unknown"
                assert connection.execute(
                    "SELECT clashlens_eligibility_checked_since(%s, %s, %s)",
                    (player, MONDAY, now),
                ).fetchone()[0] is False
        finally:
            database.close()


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("history_recorded", [False, True])
def test_discovery_restart_fetches_only_unfinished_endpoints(database_url, weekly, history_recorded):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if weekly else [player], MONDAY, weekly),
                ).fetchone()[0] == 1
                work = connection.execute("SELECT id FROM collector_work").fetchone()[0]
            now = MONDAY + timedelta(seconds=1)
            first = (next_check(database, now, schedule=False) if weekly else
                     database.pending_intents(limit=1, now=now, interactive=False)[0])
            assert first is not None and first.profile_required and first.league_history_required
            database.record_response(_handoff(
                occurrence_key="own-profile", response_hash="a" * 64,
                player_id=player, collector_work_id=work, completed_at=now,
            ))
            if history_recorded:
                database.record_response(_handoff(
                    occurrence_key="own-history", response_hash="b" * 64,
                    player_id=player, collector_work_id=work, endpoint="league_history",
                    completed_at=now,
                ))
            database.close()
            database = _collector(info)
            resumed = (next_check(database, now, schedule=False) if weekly else
                       database.pending_intents(limit=1, now=now, interactive=False)[0])
            assert resumed is not None and resumed.work_id == work
            assert not resumed.profile_required
            assert resumed.league_history_required is not history_recorded
            spool = _Spool()
            client = _Client(spool)
            collector = _fake_collector(spool, database, client)
            assert asyncio.run(collector.collect_intent(resumed)) == "complete"
            assert client.fetch_count == (0 if history_recorded else 1)
            assert "fetch:profile" not in spool.events
        finally:
            database.close()


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("own_profile", [False, True])
@pytest.mark.parametrize("queued_status", ["pending", "waiting_retry"])
def test_processed_eligible_profile_preserves_history_across_restart(
    database_url, archive_server, weekly, own_profile, queued_status,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if weekly else [player], MONDAY, weekly),
                ).fetchone()[0] == 1
                work = connection.execute("SELECT id FROM collector_work").fetchone()[0]
                connection.execute(
                    "UPDATE collector_work SET status = %s WHERE id = %s", (queued_status, work),
                )
            at = MONDAY + timedelta(seconds=1)
            payload = json.loads(PROFILE.read_bytes())
            payload["tag"] = "#2PP"
            payload["currentLeagueSeasonId"] = int(datetime(2026, 10, 5, 5, tzinfo=UTC).timestamp())
            body = json.dumps(payload).encode()
            observation, job = store_observation(
                info, archive_server, occurrence_key="processed-discovery-profile",
                endpoint="profile", body=body, observed_at=at, normalized_tag="#2PP",
                deduplication_key="process-response:processed-discovery-profile",
                parser_version=PROFILE_PARSER_VERSION,
            )
            response = database.record_response(replace(_handoff(
                occurrence_key="processed-discovery-profile",
                response_hash=hashlib.sha256(body).hexdigest(), player_id=player,
                collector_work_id=work if own_profile else None, completed_at=at,
            ), byte_size=len(body)))
            assert response.observation_id == observation and response.processing_job_id == job
            worker_database, processor = _processor(info, archive_server)
            try:
                result = processor.process_job(job, owner="processed-discovery-restart")
                assert result is not None and result.outcome == "processed"
            finally:
                worker_database.close()
            with psycopg.connect(info) as connection:
                active, eligibility = connection.execute(
                    "SELECT active, eligibility_state FROM players WHERE id = %s", (player,),
                ).fetchone()
                assert active and text(eligibility) == "eligible"
                connection.execute("UPDATE players SET next_due_at = %s WHERE id = %s", (at, player))
            due = database.claim_due_players(limit=1, now=at)
            assert len(due) == 1 and due[0].player_id == player
            database.close()
            database = _collector(info)
            resumed = (next_check(database, at, schedule=False) if weekly else
                       database.pending_intents(limit=1, now=at, interactive=False)[0])
            assert resumed is not None and resumed.work_id == work
            assert not resumed.profile_required and resumed.league_history_required
            assert not database.complete_intent(work)
            spool = _Spool()
            client = _Client(spool)
            collector = _fake_collector(spool, database, client)
            assert asyncio.run(collector.collect_intent(resumed)) == "complete"
            assert client.fetch_count == 1
            assert "fetch:league_history" in spool.events and "fetch:profile" not in spool.events
            with psycopg.connect(info) as connection:
                status, history, retained = connection.execute(
                    "SELECT status, league_history_status, profile_observation_id FROM collector_work WHERE id = %s",
                    (work,),
                ).fetchone()
                assert text(status) == "complete" and text(history) == "observed"
                assert retained == observation
        finally:
            database.close()


def test_first_time_discovery_is_immediate_and_concurrent_repeats_share_work(database_url):
    with domain_database(database_url) as info:
        player = _player(info)

        def enqueue(_index):
            with psycopg.connect(info) as connection:
                connection.execute("SET ROLE clashlens_python_worker")
                return connection.execute(
                    "SELECT clashlens_enqueue_discovery_profiles(%s)", ([player],)
                ).fetchone()[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sum(executor.map(enqueue, range(2))) == 1
        with psycopg.connect(info) as connection:
            work = connection.execute(
                "SELECT id, due_at <= clock_timestamp(), eligibility_recheck FROM collector_work"
            ).fetchone()
            assert work[1:] == (True, False)
            connection.execute("UPDATE collector_work SET status = 'failed'")
        assert enqueue(0) == 0  # A terminal attempt cannot become an unlimited retry.


def test_promotion_reuses_identity_and_starts_regular_battle_collection(database_url, archive_server):
    with domain_database(database_url) as info:
        _profile(info, archive_server, at=MONDAY - timedelta(days=1))
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            check = next_check(database, MONDAY, schedule=True)
            assert check is not None
            _profile(info, archive_server, at=MONDAY + timedelta(seconds=5), eligible=True)
            due = database.claim_due_players(limit=10, now=MONDAY + timedelta(seconds=6))
            assert len(due) == 1 and due[0].player_id == check.player_id
            assert due[0].first_battle_pending
            remaining = next_check(database, MONDAY + timedelta(minutes=1), schedule=True)
            assert remaining is not None and remaining.work_id == check.work_id
            assert not remaining.profile_required and remaining.league_history_required
            spool = _Spool()
            client = _Client(spool)
            collector = _fake_collector(spool, database, client)
            assert asyncio.run(collector.collect_intent(remaining)) == "complete"
            assert client.fetch_count == 1
            assert "fetch:league_history" in spool.events and "fetch:profile" not in spool.events
            assert next_check(database, MONDAY + timedelta(minutes=1), schedule=True) is None
            with psycopg.connect(info) as connection:
                row = connection.execute("SELECT active, eligibility_state FROM players WHERE id = %s", (check.player_id,)).fetchone()
                assert (row[0], text(row[1])) == (True, "eligible")
                assert connection.execute("SELECT count(*) FROM player_profile_versions WHERE player_id = %s", (check.player_id,)).fetchone()[0] == 2
        finally:
            database.close()


@pytest.mark.parametrize("tier,tracked", [
    ({"id": 105000034, "name": "Legend III"}, False),
    ({"id": 105000032, "name": "Electro League 32"}, False),
    ({"id": 105000000, "name": "Unranked"}, True),
])
def test_lower_tier_ends_tracking_but_an_unlisted_tier_never_does(
    database_url, archive_server, tier, tracked,
):
    with domain_database(database_url) as info:
        _profile(info, archive_server, at=MONDAY - timedelta(days=1), eligible=True)
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            _profile(info, archive_server, at=MONDAY + timedelta(seconds=5), tier=tier)
            with psycopg.connect(info) as connection:
                row = connection.execute("SELECT active, eligibility_state FROM players").fetchone()
            assert (row[0], text(row[1])) == ((True, "eligible") if tracked else (False, "ineligible"))
            # Either way this week needs no weekly request.
            assert next_check(database, MONDAY + timedelta(seconds=6), schedule=True) is None
        finally:
            database.close()


# Migration 0037's candidate search, kept here to prove its replacement
# selects exactly the same players.
_0037_SELECTION = """
    SELECT player.id FROM players AS player
    WHERE (NOT player.active OR (NOT %(scheduled)s AND player.eligibility_state <> 'eligible'))
      AND (%(scheduled)s OR player.id = ANY(%(ids)s))
      AND NOT clashlens_eligibility_checked_since(player.id, %(boundary)s, %(instant)s)
      AND NOT clashlens_eligibility_fetched_since(player.id, %(boundary)s, %(instant)s)
      AND NOT EXISTS (
          SELECT 1 FROM collector_work AS work
          WHERE work.player_id = player.id
            AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
            AND (work.status IN ('pending', 'waiting_retry')
                 OR (work.due_at >= %(boundary)s AND work.due_at < %(boundary)s + interval '7 days')
                 OR EXISTS (
                     SELECT 1 FROM collector_observations AS observation
                     WHERE observation.id = work.profile_observation_id
                       AND observation.response_completed_at >= %(boundary)s
                       AND observation.response_completed_at <= %(instant)s)))
    ORDER BY player.id"""


def test_direct_selection_matches_0037_for_every_evidence_case(database_url, archive_server):
    instant = MONDAY + timedelta(days=1)
    with domain_database(database_url) as info:
        database = _collector(info)
        try:
            players = {}

            def player(name, active=False, state="unknown"):
                tag = "#" + "".join("0289PYLQGRJCUV"[int(d)] for d in str(len(players) + 10))
                players[name] = (_player(info, tag), tag)
                with psycopg.connect(info) as connection:
                    connection.execute(
                        "UPDATE players SET active = %s, eligibility_state = %s WHERE id = %s",
                        (active, state, players[name][0]),
                    )
                return players[name]

            def fetch(name, at, status=200, work=None):
                player_id, tag = players[name]
                return database.record_response(_handoff(
                    occurrence_key=f"{name}:{at.isoformat()}", tag=tag, player_id=player_id,
                    response_hash=hashlib.sha256(f"{name}{at}".encode()).hexdigest(),
                    completed_at=at, http_status=status, collector_work_id=work,
                ))

            def work(name, due, status="complete", kind="discovery_profile", week=None):
                player_id, tag = players[name]
                week = week or MONDAY + (due - MONDAY) // timedelta(days=7) * timedelta(days=7)
                key = (f"discovery-profile:{player_id}:{week:%Y-%m-%dT%H:%M:%SZ}"
                       if kind == "discovery_profile" else f"{name}:{due.isoformat()}")
                with psycopg.connect(info) as connection:
                    return connection.execute(
                        """INSERT INTO collector_work (kind, lane, scope, player_id, normalized_tag,
                               due_at, coalescing_key, status, battle_log_status,
                               league_history_status)
                           VALUES (%s, %s, 'player', %s, %s, %s, %s, %s, %s, 'not_applicable')
                           RETURNING id""",
                        (kind, "ordinary" if kind == "discovery_profile" else "interactive",
                         player_id, tag, due, key, status,
                         "not_applicable" if kind == "discovery_profile" else "pending"),
                    ).fetchone()[0]

            player("nothing")
            player("fetched_this_week"); fetch("fetched_this_week", MONDAY + timedelta(hours=1))
            player("fetched_after_instant"); fetch("fetched_after_instant", instant + timedelta(hours=1))
            player("fetched_last_week"); fetch("fetched_last_week", MONDAY - timedelta(hours=1))
            player("failed_fetch_this_week"); fetch("failed_fetch_this_week", MONDAY + timedelta(hours=1), 503)
            player("unchanged_this_week")
            fetch("unchanged_this_week", MONDAY - timedelta(hours=1))
            database.record_response(replace(_handoff(
                occurrence_key="unchanged-again", tag=players["unchanged_this_week"][1],
                player_id=players["unchanged_this_week"][0],
                response_hash=hashlib.sha256(
                    f"unchanged_this_week{MONDAY - timedelta(hours=1)}".encode()).hexdigest(),
                completed_at=MONDAY + timedelta(hours=1),
            )))
            for name, at, tier in (
                ("recognized_this_week", MONDAY + timedelta(hours=1), None),
                ("recognized_last_week", MONDAY - timedelta(hours=1), None),
                ("uncertain_this_week", MONDAY + timedelta(hours=1), {"id": 105000000, "name": "Unranked"}),
            ):
                _profile(info, archive_server, at=at, tag=player(name)[1], tier=tier)
            player("work_this_week"); work("work_this_week", MONDAY + timedelta(hours=1))
            player("failed_work_this_week"); work("failed_work_this_week", MONDAY + timedelta(hours=1), "failed")
            player("failed_work_last_week"); work("failed_work_last_week", MONDAY - timedelta(days=1), "failed")
            player("pending_old_work"); work("pending_old_work", MONDAY - timedelta(days=8), "pending")
            # Last week's check, retried past the Reset: 0037 counted it as this week's.
            player("retried_last_weeks_work")
            work("retried_last_weeks_work", MONDAY + timedelta(minutes=1), week=MONDAY - timedelta(days=7))
            player("old_refresh_answered_this_week")
            refresh = work("old_refresh_answered_this_week", MONDAY - timedelta(minutes=1),
                           "pending", kind="live_refresh")
            fetch("old_refresh_answered_this_week", MONDAY - timedelta(minutes=1), 503, work=refresh)
            fetch("old_refresh_answered_this_week", MONDAY + timedelta(minutes=1), 503, work=refresh)
            with psycopg.connect(info) as connection:
                connection.execute("UPDATE collector_work SET status = 'complete' WHERE id = %s", (refresh,))
            player("active_eligible", active=True, state="eligible")
            player("active_unknown", active=True)
            player("inactive_eligible", state="eligible")
            ids = [player_id for player_id, _tag in players.values()]
            by_id = {player_id: name for name, (player_id, _tag) in players.items()}

            for scheduled in (True, False):
                with psycopg.connect(info) as connection, connection.transaction(force_rollback=True):
                    last = connection.execute("SELECT max(id) FROM collector_work").fetchone()[0]
                    expected = [row[0] for row in connection.execute(_0037_SELECTION, {
                        "scheduled": scheduled, "ids": ids, "boundary": MONDAY, "instant": instant,
                    })]
                    assert connection.execute(
                        "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                        (None if scheduled else ids, instant, scheduled),
                    ).fetchone()[0] == len(expected) + 1
                    selected = [row[0] for row in connection.execute(
                        "SELECT player_id FROM collector_work WHERE id > %s ORDER BY player_id", (last,),
                    )]
                assert [by_id[i] for i in selected] == sorted(
                    [by_id[i] for i in expected] + ["retried_last_weeks_work"],
                    key=lambda name: players[name][0])
                assert {by_id[i] for i in expected} == {
                    "nothing", "fetched_after_instant", "fetched_last_week", "failed_fetch_this_week",
                    "recognized_last_week", "uncertain_this_week", "failed_work_last_week",
                    "inactive_eligible", *(() if scheduled else ("active_unknown",)),
                }
        finally:
            database.close()


def test_live_profiles_satisfy_week_without_extra_requests(database_url, archive_server):
    with domain_database(database_url) as info:
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            # A post-Reset departure is already this week's inactive check.
            _profile(info, archive_server, at=MONDAY, eligible=False)
            _profile(info, archive_server, at=MONDAY, eligible=True, tag="#2PQ")
            assert next_check(database, MONDAY + timedelta(seconds=1), schedule=True) is None
            with psycopg.connect(info) as connection:
                assert connection.execute("SELECT count(*) FROM collector_work WHERE eligibility_recheck").fetchone()[0] == 0
        finally:
            database.close()


def test_weekly_work_pauses_when_live_players_are_two_minutes_overdue(database_url):
    with domain_database(database_url) as info:
        inactive = _player(info)
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                connection.execute(
                    """INSERT INTO players (normalized_tag, active, next_due_at)
                       VALUES ('#2PQ', true, %s)""", (MONDAY - timedelta(minutes=3),)
                )
            assert next_check(database, MONDAY, schedule=True) is None
            with psycopg.connect(info) as connection:
                assert connection.execute("SELECT count(*) FROM collector_work").fetchone()[0] == 0
                connection.execute("UPDATE players SET next_due_at = %s WHERE active", (MONDAY,))
            assert next_check(database, MONDAY, schedule=True).player_id == inactive
        finally:
            database.close()


def test_queue_is_bounded_and_ordinary_loop_does_not_bypass_weekly_pacing(database_url):
    with domain_database(database_url) as info:
        for index in range(35):
            _player(info, f"#TEST{index}")
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            next_check(database, MONDAY, schedule=True)
            next_check(database, MONDAY + timedelta(minutes=2), schedule=True)
            assert database.pending_intents(limit=100, now=MONDAY + timedelta(minutes=2)) == []
            with psycopg.connect(info) as connection:
                times = [row[0] for row in connection.execute("SELECT due_at FROM collector_work ORDER BY due_at").fetchall()]
                assert times == [MONDAY + timedelta(seconds=2 * index) for index in range(30)]
        finally:
            database.close()


def _untracked_players(connection, count):
    return sorted(row[0] for row in connection.execute(
        """INSERT INTO players (normalized_tag, active, eligibility_state)
           SELECT '#' || translate(lpad(i::text, 9, '0'), '0123456789', '0289PYLQGR'),
                  false, 'ineligible'
           FROM generate_series(1, %s) AS i RETURNING id""", (count,)))


def _weekly_search(connection, at):
    """The players one weekly search queued, and how many player rows it read.

    Like the collector, each search commits; the batch finishes before the next.
    """
    def rows_read():
        # Rows a scan returned from the table, plus entries read from its indexes.
        return connection.execute(
            """SELECT sum(pg_stat_get_xact_tuples_returned(relation))::bigint
               FROM (SELECT 'players'::regclass::oid AS relation
                     UNION ALL SELECT indexrelid FROM pg_index
                     WHERE indrelid = 'players'::regclass) AS relations""",
        ).fetchone()[0]
    with connection.transaction():
        last = connection.execute("SELECT coalesce(max(id), 0) FROM collector_work").fetchone()[0]
        before = rows_read()
        connection.execute("SELECT clashlens_enqueue_weekly_eligibility(%s)", (at,))
        read = rows_read() - before
    queued = [row[0] for row in connection.execute(
        "SELECT player_id FROM collector_work WHERE id > %s ORDER BY id", (last,))]
    connection.execute(
        "UPDATE collector_work SET status = 'complete' WHERE eligibility_recheck AND status = 'pending'")
    return queued, read


@pytest.mark.parametrize("checked", [0, 1_000, 2_000])
def test_weekly_search_reads_only_players_not_yet_checked_this_week(database_url, checked):
    # On 9 October 2026 each search tested every player already checked that
    # week: with 9,510 of 13,215 checked, a search took up to 58.6 seconds.
    with domain_database(database_url) as info, psycopg.connect(info, autocommit=True) as connection:
        players = _untracked_players(connection, 2_000)
        connection.execute(
            """INSERT INTO collector_work (kind, lane, scope, player_id, normalized_tag, due_at,
                   coalescing_key, status, profile_status, battle_log_status,
                   league_history_status, eligibility_recheck)
               SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag, %s,
                      'discovery-profile:' || id || ':2026-10-12T05:00:00Z', 'complete',
                      'observed', 'not_applicable', 'not_applicable', true
               FROM players WHERE id = ANY(%s)""",
            (MONDAY + timedelta(hours=1), players[:checked]),
        )
        # The first search notes the players checked before this change, and the
        # second reads past their replaced rows once more, as each later search
        # does for the few players the search before it noted.
        for batch in range(5):
            queued, read = _weekly_search(connection, MONDAY + timedelta(days=1, minutes=batch))
            assert queued == players[checked + 30 * batch:checked + 30 * (batch + 1)]
            if batch >= 2:
                # A few rows for each player queued; none once everyone is checked.
                assert read <= 20 * len(queued)


def test_next_week_makes_every_untracked_player_due_again(database_url):
    with domain_database(database_url) as info, psycopg.connect(info, autocommit=True) as connection:
        players = _untracked_players(connection, 40)
        assert _weekly_search(connection, MONDAY)[0] == players[:30]
        assert _weekly_search(connection, MONDAY + timedelta(minutes=1))[0] == players[30:]
        assert _weekly_search(connection, MONDAY + timedelta(days=7) - timedelta(seconds=1))[0] == []
        assert _weekly_search(connection, MONDAY + timedelta(days=7))[0] == players[:30]


def test_weekly_search_looks_again_at_a_player_whose_check_was_waiting(database_url):
    with domain_database(database_url) as info, psycopg.connect(info, autocommit=True) as connection:
        waiting, *others = _untracked_players(connection, 32)
        connection.execute(
            """INSERT INTO collector_work (kind, lane, scope, player_id, normalized_tag, due_at,
                   coalescing_key, status, battle_log_status, league_history_status)
               SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag, %s,
                      'discovery-profile:' || id || ':2026-10-05T05:00:00Z', 'pending',
                      'not_applicable', 'not_applicable'
               FROM players WHERE id = %s""", (MONDAY - timedelta(days=1), waiting),
        )
        assert _weekly_search(connection, MONDAY)[0] == others[:30]
        # Last week's check failed without an answer, so this week's is still needed.
        connection.execute("UPDATE collector_work SET status = 'failed' WHERE player_id = %s", (waiting,))
        assert _weekly_search(connection, MONDAY + timedelta(minutes=1))[0] == [waiting, others[30]]


def test_pruning_keeps_this_weeks_attempt_until_the_next_monday(database_url):
    with domain_database(database_url) as info:
        player = _player(info)
        with psycopg.connect(info) as connection:
            connection.execute(
                """INSERT INTO collector_work (
                       kind, lane, scope, player_id, normalized_tag, due_at,
                       coalescing_key, status, completed_at, updated_at,
                       profile_status, battle_log_status, league_history_status)
                   SELECT 'discovery_profile', 'ordinary', 'player', %s, '#2PP',
                          clashlens_eligibility_week(clock_timestamp()) - age,
                          'prune:' || age, 'complete', clock_timestamp(),
                          clock_timestamp() - interval '4 days', 'observed',
                          'not_applicable', 'not_applicable'
                   FROM unnest(ARRAY[interval '0 days', interval '7 days']) AS age""",
                (player,),
            )
            connection.commit()
            result = prune_completed_history(connection, apply=True)
            assert result["deleted_collection_jobs"] == 1
            assert connection.execute("SELECT count(*) FROM collector_work").fetchone()[0] == 1


def test_unchanged_post_reset_profile_reuses_recognized_old_observation(database_url, archive_server):
    with domain_database(database_url) as info:
        old = MONDAY - timedelta(days=1)
        observation = _profile(info, archive_server, at=old)
        database = _collector(info)
        try:
            with psycopg.connect(info) as connection:
                player, digest, size = connection.execute(
                    """SELECT observation.player_id, observation.response_hash, catalogue.byte_size
                       FROM collector_observations AS observation
                       JOIN archive_catalogue AS catalogue ON catalogue.response_hash = observation.response_hash
                       WHERE observation.id = %s""", (observation,)
                ).fetchone()
                handoff = replace(_handoff(
                    occurrence_key="weekly-old-profile", response_hash=text(digest),
                    player_id=player, completed_at=old,
                ), byte_size=size)
                database._upsert_response_state(connection, handoff, observation)
            database.begin_reset(MONDAY, local_regular_inflight=0)
            result = database.record_response(replace(
                handoff, occurrence_key="weekly-unchanged-profile",
                request_started_at=MONDAY, response_completed_at=MONDAY + timedelta(seconds=1),
            ))
            assert not result.changed and result.observation_id is None
            assert next_check(database, MONDAY + timedelta(seconds=2), schedule=True) is None
            with psycopg.connect(info) as connection:
                assert connection.execute("SELECT count(*) FROM collector_observations").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM collector_work").fetchone()[0] == 0
        finally:
            database.close()


def test_restart_finishes_durable_profile_without_waiting_for_processing(database_url):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            first = next_check(database, MONDAY, schedule=True)
            assert first is not None
            database.record_response(_handoff(
                occurrence_key="weekly-before-crash", response_hash="a" * 64,
                player_id=player, collector_work_id=first.work_id,
                completed_at=MONDAY + timedelta(seconds=1),
            ))
            resumed = next_check(database, MONDAY + timedelta(seconds=2), schedule=True)
            assert resumed.work_id == first.work_id
            assert not resumed.profile_required
            assert resumed.league_history_required
        finally:
            database.close()


def test_delayed_pre_reset_profile_cannot_cancel_mondays_unfetched_check(database_url, archive_server):
    with domain_database(database_url) as info:
        _player(info)
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            first = next_check(database, MONDAY, schedule=True)
            _profile(info, archive_server, at=MONDAY - timedelta(seconds=1))
            pending = next_check(database, MONDAY + timedelta(seconds=2), schedule=False)
            assert pending is not None and pending.work_id == first.work_id
            assert pending.profile_required
        finally:
            database.close()


def test_finished_initial_and_refresh_checks_are_reused_before_processing(database_url):
    with domain_database(database_url) as info:
        database = _collector(info)
        try:
            database.begin_reset(MONDAY, local_regular_inflight=0)
            for kind, tag in (("initial_collection", "#2PP"), ("live_refresh", "#2PQ")):
                player = _player(info, tag)
                with psycopg.connect(info) as connection:
                    work = connection.execute(
                        """INSERT INTO collector_work (
                               kind, lane, scope, player_id, normalized_tag, due_at,
                               coalescing_key, league_history_status)
                           VALUES (%s, 'interactive', 'player', %s, %s, %s, %s, %s)
                           RETURNING id""",
                        (kind, player, tag, MONDAY - timedelta(seconds=1), kind,
                         "pending" if kind == "initial_collection" else "not_applicable"),
                    ).fetchone()[0]
                database.record_response(_handoff(
                    occurrence_key=kind, response_hash="a" * 64, player_id=player,
                    tag=tag, collector_work_id=work, completed_at=MONDAY,
                ))
                with psycopg.connect(info) as connection:
                    connection.execute(
                        "UPDATE collector_work SET status = 'complete', completed_at = %s WHERE id = %s",
                        (MONDAY, work),
                    )
            assert next_check(database, MONDAY + timedelta(seconds=1), schedule=True) is None
            with psycopg.connect(info) as connection:
                assert connection.execute("SELECT count(*) FROM collector_work").fetchone()[0] == 2
        finally:
            database.close()


@pytest.mark.parametrize("kind", ["discovery_profile", "initial_collection", "live_refresh"])
@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("after_reset", [False, True])
def test_unchanged_pending_profile_reuse_depends_on_fetch_completion(
    database_url, kind, scheduled, after_reset,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            old = MONDAY - timedelta(days=1)
            original = database.record_response(_handoff(
                occurrence_key="old-pending-profile", response_hash="a" * 64,
                player_id=player, completed_at=old,
            ))
            completed_at = MONDAY + timedelta(seconds=1 if after_reset else -1)
            with psycopg.connect(info) as connection:
                work = connection.execute(
                    """INSERT INTO collector_work (
                           kind, lane, scope, player_id, normalized_tag, due_at,
                           coalescing_key, battle_log_status, league_history_status)
                       VALUES (%s, %s, 'player', %s, '#2PP', %s, %s, %s, %s)
                       RETURNING id""",
                    (kind, "ordinary" if kind == "discovery_profile" else "interactive",
                     player, MONDAY - timedelta(seconds=2), kind,
                     "not_applicable" if kind == "discovery_profile" else "pending",
                     "pending" if kind == "initial_collection" else "not_applicable"),
                ).fetchone()[0]
            result = database.record_response(replace(_handoff(
                occurrence_key="unchanged-pending-profile", response_hash="a" * 64,
                player_id=player, collector_work_id=work, completed_at=completed_at,
            ), request_started_at=MONDAY - timedelta(seconds=2)))
            assert not result.changed and result.observation_id is None
            endpoints = []
            if kind != "discovery_profile":
                endpoints.append("battle_log")
            if kind == "initial_collection":
                endpoints.append("league_history")
            for endpoint in endpoints:
                database.record_response(_handoff(
                    occurrence_key=endpoint, response_hash="b" * 64,
                    player_id=player, endpoint=endpoint, collector_work_id=work,
                    completed_at=completed_at,
                ))
            assert database.complete_intent(work)
            instant = MONDAY + timedelta(seconds=2)
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_eligibility_checked_since(%s, %s, %s)",
                    (player, MONDAY, instant),
                ).fetchone()[0] is False
                if scheduled:
                    created = connection.execute(
                        "SELECT clashlens_enqueue_weekly_eligibility(%s)", (instant,),
                    ).fetchone()[0]
                else:
                    created = connection.execute(
                        "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, false)",
                        ([player], instant),
                    ).fetchone()[0]
                assert created == (0 if after_reset else 1)
                assert connection.execute(
                    "SELECT count(*) FROM collector_work WHERE id <> %s", (work,),
                ).fetchone()[0] == created
                assert text(connection.execute(
                    "SELECT eligibility_state FROM players WHERE id = %s", (player,),
                ).fetchone()[0]) == "unknown"
                assert text(connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (original.processing_job_id,),
                ).fetchone()[0]) == "pending"
                assert connection.execute(
                    "SELECT profile_observation_id FROM collector_work WHERE id = %s", (work,),
                ).fetchone()[0] == original.observation_id
        finally:
            database.close()


def _next_reset():
    """The next daily Reset: retries are due by the database clock, so run after it."""
    now = datetime.now(UTC)
    boundary = now.replace(hour=5, minute=0, second=0, microsecond=0)
    return boundary if boundary > now else boundary + timedelta(days=1)


class _EndpointClient(_Client):
    """A fake API answering each endpoint with its own HTTP status."""

    def __init__(self, spool, statuses):
        super().__init__(spool)
        self.statuses = statuses

    async def fetch_player(self, pool, tag, endpoint):
        return replace(await super().fetch_player(pool, tag, endpoint),
                       http_status=self.statuses.get(endpoint, 200))


@pytest.mark.parametrize("weekly", [False, True])
def test_temporary_failure_retries_the_same_weeks_work_until_the_api_answers(
    database_url, weekly,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            start = _next_reset()
            database.begin_reset(start, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if weekly else [player], start, weekly),
                ).fetchone()[0] == 1

            def admitted():
                now = start + timedelta(minutes=1)
                return (next_check(database, now, schedule=False) if weekly else
                        database.pending_intents(limit=1, now=now, interactive=False)[0])

            spool = _Spool()
            # The profile arrives; league history answers 503 once, then 200.
            client = _EndpointClient(spool, {"league_history": 503})
            collector = _fake_collector(spool, database, client)
            first = admitted()
            assert asyncio.run(collector.collect_intent(first)) == "retrying"
            client.statuses = {}
            retry = admitted()
            assert retry.work_id == first.work_id
            assert not retry.profile_required and retry.league_history_required
            assert asyncio.run(collector.collect_intent(retry)) == "complete"
            assert spool.events.count("fetch:profile") == 1
            assert spool.events.count("fetch:league_history") == 2
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT count(*), min(status) FROM collector_work"
                ).fetchone() == (1, "complete")
                # Another submission that week reuses the finished check.
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, false)",
                    ([player], start + timedelta(hours=1)),
                ).fetchone()[0] == 0
        finally:
            database.close()


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("status,runs", [(503, 4), (429, 4), (403, 1)])
def test_failed_check_retries_only_temporary_failures_and_a_bounded_number_of_times(
    database_url, weekly, status, runs,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            start = _next_reset()
            database.begin_reset(start, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if weekly else [player], start, weekly),
                )
                connection.execute(
                    "UPDATE collector_work SET league_history_status = 'not_applicable'")
            spool = _Spool()
            client = _Client(spool, http_status=status)
            collector = _fake_collector(spool, database, client)
            outcomes = []
            while True:
                now = start + timedelta(minutes=1)
                intent = (next_check(database, now, schedule=False) if weekly else
                          next(iter(database.pending_intents(limit=1, now=now, interactive=False)), None))
                if intent is None:
                    break
                outcomes.append(asyncio.run(collector.collect_intent(intent)))
            # One request per run: the first, then three retries while the API answers.
            assert outcomes == ["retrying"] * (runs - 1) + ["failed"]
            assert client.fetch_count == runs
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT count(*), min(status) FROM collector_work"
                ).fetchone() == (1, "failed")
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, false)",
                    ([player], start + timedelta(hours=1)),
                ).fetchone()[0] == 0
        finally:
            database.close()


@pytest.mark.parametrize("weekly", [False, True])
@pytest.mark.parametrize("tier,eligibility", [
    (None, "eligible"), ({"id": 105000034, "name": "Legend III"}, "ineligible"),
])
def test_recognized_profile_keeps_retrying_failed_league_history(
    database_url, archive_server, weekly, tier, eligibility,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            # Processing compares profile times with the database clock: use the last Reset.
            start = _next_reset() - timedelta(days=1)
            database.begin_reset(start, local_regular_inflight=0)
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT clashlens_enqueue_eligibility_profiles(%s, %s, %s)",
                    (None if weekly else [player], start, weekly),
                ).fetchone()[0] == 1
                work = connection.execute("SELECT id FROM collector_work").fetchone()[0]
            at = start
            payload = json.loads(PROFILE.read_bytes())
            payload["tag"] = "#2PP"
            bootstrap = datetime.fromtimestamp(int(BOOTSTRAP_CURRENT_SEASON_ID), UTC)
            payload["currentLeagueSeasonId"] = int(
                (bootstrap + (at - bootstrap) // SEASON_DURATION * SEASON_DURATION).timestamp())
            if tier:
                payload["leagueTier"] = tier
            body = json.dumps(payload).encode()
            _observation, job = store_observation(
                info, archive_server, occurrence_key="legend-i-profile",
                endpoint="profile", body=body, observed_at=at, normalized_tag="#2PP",
                deduplication_key="process-response:legend-i-profile",
                parser_version=PROFILE_PARSER_VERSION,
            )
            database.record_response(replace(_handoff(
                occurrence_key="legend-i-profile", response_hash=hashlib.sha256(body).hexdigest(),
                player_id=player, collector_work_id=work, completed_at=at,
            ), byte_size=len(body)))
            database.record_response(_handoff(
                occurrence_key="history-503", response_hash="c" * 64, player_id=player,
                endpoint="league_history", collector_work_id=work, completed_at=at,
                http_status=503,
            ))
            with psycopg.connect(info) as connection:
                connection.execute("UPDATE collector_work SET status = 'waiting_retry' WHERE id = %s", (work,))
            worker_database, processor = _processor(info, archive_server)
            try:
                assert processor.process_job(job, owner="history-retry").outcome == "processed"
            finally:
                worker_database.close()
            with psycopg.connect(info) as connection:
                assert tuple(map(text, connection.execute(
                    "SELECT active, eligibility_state FROM players WHERE id = %s", (player,),
                ).fetchone())) == (eligibility == "eligible", eligibility)
                connection.execute("UPDATE players SET next_due_at = %s WHERE id = %s",
                                   (start + timedelta(minutes=1), player))
            now = start + timedelta(minutes=1)
            retry = (next_check(database, now, schedule=False) if weekly else
                     database.pending_intents(limit=1, now=now, interactive=False)[0])
            assert retry is not None and retry.work_id == work
            assert not retry.profile_required and retry.league_history_required
            spool = _Spool()
            client = _Client(spool)
            collector = _fake_collector(spool, database, client)
            assert asyncio.run(collector.collect_intent(retry)) == "complete"
            assert spool.events.count("fetch:league_history") == 1
            assert "fetch:profile" not in spool.events
        finally:
            database.close()


def test_unchanged_weekly_profile_reprocesses_an_unrecognized_league(
    database_url, archive_server, monkeypatch,
):
    with domain_database(database_url) as info:
        player = _player(info)
        database = _collector(info)
        try:
            old = MONDAY - timedelta(days=1)
            payload = json.loads(PROFILE.read_bytes())
            payload["tag"] = "#2PP"
            payload["currentLeagueSeasonId"] = int(datetime(2026, 10, 5, 5, tzinfo=UTC).timestamp())
            payload["leagueTier"] = {"id": 105000034, "name": "Legend III"}
            body = json.dumps(payload).encode()
            observation, job = store_observation(
                info, archive_server, occurrence_key="old-legend-iii",
                endpoint="profile", body=body, observed_at=old, normalized_tag="#2PP",
                deduplication_key="process-response:old-legend-iii",
                parser_version=PROFILE_PARSER_VERSION,
            )
            handoff = replace(_handoff(
                occurrence_key="old-legend-iii", response_hash=hashlib.sha256(body).hexdigest(),
                player_id=player, completed_at=old,
            ), byte_size=len(body))
            database.record_response(handoff)
            # Processed before Legend III was a recognized tier.
            with monkeypatch.context() as patch:
                patch.setattr(profile, "RECOGNIZED_NON_LEGEND_TIERS_V1", {105000035: "Legend II"})
                worker_database, processor = _processor(info, archive_server)
                try:
                    assert processor.process_job(job, owner="old-catalogue").outcome == "processed"
                finally:
                    worker_database.close()
            with psycopg.connect(info) as connection:
                assert text(connection.execute(
                    "SELECT eligibility_state FROM player_profile_versions").fetchone()[0]) == "uncertain"
            database.begin_reset(MONDAY, local_regular_inflight=0)
            check = next_check(database, MONDAY, schedule=True)
            assert check is not None and check.profile_required
            result = database.record_response(replace(
                handoff, occurrence_key="weekly-unchanged-legend-iii",
                collector_work_id=check.work_id, request_started_at=MONDAY,
                response_completed_at=MONDAY + timedelta(seconds=1),
            ))
            assert result.changed and result.processing_job_id is not None
            with psycopg.connect(info) as connection:
                connection.execute("UPDATE python_processing_jobs SET due_at = now() WHERE id = %s",
                                   (result.processing_job_id,))
                # The same bytes are already archived; the upload would bind them.
                connection.execute(
                    """UPDATE collector_observations AS fresh
                       SET archive_reference = old.archive_reference,
                           archive_catalogue_hash = old.archive_catalogue_hash
                       FROM collector_observations AS old
                       WHERE fresh.id = %s AND old.id = %s""",
                    (result.observation_id, observation),
                )
            worker_database, processor = _processor(info, archive_server)
            try:
                assert processor.process_job(
                    result.processing_job_id, owner="current-catalogue").outcome == "processed"
            finally:
                worker_database.close()
            with psycopg.connect(info) as connection:
                active, eligibility = connection.execute(
                    "SELECT active, eligibility_state FROM players WHERE id = %s", (player,),
                ).fetchone()
                assert not active and text(eligibility) == "ineligible"
                assert connection.execute(
                    "SELECT profile_observation_id FROM collector_work WHERE id = %s", (check.work_id,),
                ).fetchone()[0] == result.observation_id
        finally:
            database.close()
