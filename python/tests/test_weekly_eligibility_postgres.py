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
from test_collector_db_postgres import _handoff
from test_collector import _Client, _Spool, _collector as _fake_collector
from test_domain_processing_postgres import _processor

from clashlens.collector_db import CollectorDatabase
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


def _profile(info, archive, *, at, eligible=False, tag="#2PP"):
    payload = json.loads(PROFILE.read_bytes())
    payload["tag"] = tag
    payload["currentLeagueSeasonId"] = int(datetime(2026, 10, 5, 5, tzinfo=UTC).timestamp())
    if not eligible:
        payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
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
