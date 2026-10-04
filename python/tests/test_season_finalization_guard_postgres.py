"""Finalization and retirement wait seven days after the Season ends and
refuse while relevant work is unfinished or promised history is missing."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from test_season_detail_retirement_postgres import (
    DAY0,
    SEASON,
    SEASON_END,
    _full_season,
    _materialize_all,
    _player,
    _seed_army,
)

from clashlens import season_finalization_guard
from clashlens.api_db import ApiDatabase
from clashlens.season_finalization_guard import close_blockers
from clashlens.season_retirement import finalize_season_detail, retire_season_detail

ELIGIBLE = SEASON_END + timedelta(days=7)
EARLY = ELIGIBLE - timedelta(microseconds=1)
WAITING = {"status": "blocked", "reason": "season_close_wait", "eligible_at": ELIGIBLE.isoformat()}
HISTORY_ONLY = {"promised_history": ["expanded_history_unavailable"]}
HELD = {"status": "blocked", "reason": "verification_failed", "eligible_at": ELIGIBLE.isoformat()}
BATTLE_LOG = (Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json").read_bytes()


def _ended_season(connection) -> None:
    _full_season(connection, _player(connection))
    _seed_army(connection)
    connection.commit()
    # Summaries build an hour after the Season ends, during the wait.
    player_report, army_report = _materialize_all(connection)
    connection.commit()
    assert player_report["materialized"] == 1
    assert army_report["season_completed"] is True


def _counts(connection) -> tuple[int, ...]:
    return tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("api_player_daily_logs", "army_analytics_battle_facts", "season_detail_retirements")
    )


def test_finalize_preview_and_apply_wait_seven_days(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                before = _counts(connection)
                assert before[2] == 0
                for apply in (False, True):
                    early = finalize_season_detail(connection, SEASON, EARLY, apply=apply)
                    assert early == {"season_id": SEASON, **WAITING, "already_finalized": False, "applied": False}
                    connection.rollback()
                not_ended = finalize_season_detail(connection, SEASON, SEASON_END - timedelta(hours=1))
                assert (not_ended["status"], not_ended["reason"], not_ended["eligible_at"]) == (
                    "not_completed", "season_not_completed", ELIGIBLE.isoformat(),
                )
                older = DAY0 - timedelta(days=56)
                no_history = finalize_season_detail(connection, str(int(older.timestamp())), ELIGIBLE)
                assert (no_history["reason"], no_history["eligible_at"]) == (
                    "no_history", (older + timedelta(days=35)).isoformat(),
                )
                connection.rollback()
                assert _counts(connection) == before
                held = finalize_season_detail(connection, SEASON, ELIGIBLE)
                assert (held["reason"], held["blocking_work"], held["applied"]) == (
                    "verification_failed", HISTORY_ONLY, False,
                )
                connection.rollback()
        finally:
            database.close()


def _summary_digests(connection, monkeypatch) -> dict:
    """What finalization would store if the promised history could be checked."""
    with monkeypatch.context() as patch:
        patch.setattr(season_finalization_guard, "promised_history_gap", lambda *_: None)
        ready = finalize_season_detail(connection, SEASON, ELIGIBLE)
    connection.rollback()
    assert ready["status"] == "ready"
    return ready


def _legacy_finalized_record(connection, ready) -> None:
    """A record older code could have written without any of these checks."""
    connection.execute(
        """
        INSERT INTO season_detail_retirements (
            official_season_id, status, season_start, season_end,
            player_summary_count, player_summary_digest, army_summary_digest
        ) VALUES (%s, 'finalized', %s, %s, 1, %s, %s)
        """,
        (SEASON, DAY0, SEASON_END, ready["player_summary_digest"], ready["army_summary_digest"]),
    )
    connection.commit()


def test_early_finalized_record_cannot_retire_or_hide_the_wait(database_url: str, monkeypatch) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                ready = _summary_digests(connection, monkeypatch)
                _legacy_finalized_record(connection, ready)
                before = _counts(connection)
                repeat = finalize_season_detail(connection, SEASON, EARLY, apply=True)
                assert repeat == {
                    "season_id": SEASON, **WAITING, "existing_status": "finalized",
                    "already_finalized": True, "applied": False,
                }
                connection.rollback()
                for apply in (False, True):
                    blocked = retire_season_detail(connection, SEASON, max_rows=1000, apply=apply, now=EARLY)
                    assert blocked == {
                        "season_id": SEASON, **WAITING, "existing_status": "finalized", "applied": False,
                    }
                    connection.rollback()
                assert _counts(connection) == before
                assert connection.execute(
                    "SELECT status, progress FROM season_detail_retirements"
                ).fetchone() == ("finalized", {})
                # Retirement reads the database clock by default; May 2026 has
                # waited, but the record cannot stand in for the close guard.
                assert retire_season_detail(connection, SEASON) == {
                    "season_id": SEASON, **HELD, "blocking_work": HISTORY_ONLY,
                    "existing_status": "finalized", "applied": False,
                }
                connection.rollback()
                late = ELIGIBLE + timedelta(days=1)
                for start, end, reason in (
                    (DAY0 + timedelta(days=1), SEASON_END + timedelta(days=1), "conflicting_season_boundary"),
                    (DAY0, SEASON_END - timedelta(days=1), "conflicting_season_boundary"),
                    (None, None, "unknown_season_boundary"),
                ):
                    connection.execute(
                        "UPDATE season_detail_retirements SET season_start = %s, season_end = %s",
                        (start, end),
                    )
                    for report in (
                        retire_season_detail(connection, SEASON, apply=True, now=late),
                        finalize_season_detail(connection, SEASON, late, apply=True),
                    ):
                        assert (report["reason"], report["eligible_at"]) == (reason, ELIGIBLE.isoformat())
                    connection.rollback()
                # Two Seasons back, past the confirmed timing, on the 28-day calendar.
                older = DAY0 - timedelta(days=56)
                older_eligible = (older + timedelta(days=35)).isoformat()
                later = DAY0 + timedelta(days=56)
                for season_id, start, reason, eligible_at in (
                    (str(int(older.timestamp())), older + timedelta(days=1), "conflicting_season_boundary", older_eligible),
                    (str(int(older.timestamp())), older, "player_summaries_missing", older_eligible),
                    (str(int(later.timestamp())), later, "unknown_season_boundary", None),
                    ("short-window", DAY0, "unknown_season_boundary", None),
                ):
                    connection.execute(
                        """
                        INSERT INTO season_detail_retirements (official_season_id, season_start, season_end)
                        VALUES (%s, %s, %s)
                        """,
                        (season_id, start, start + timedelta(days=28)),
                    )
                    reports = [retire_season_detail(connection, season_id, apply=True)]
                    if reason != "player_summaries_missing":
                        reports.append(finalize_season_detail(connection, season_id, late, apply=True))
                    for report in reports:
                        assert (report["reason"], report.get("eligible_at"), report["applied"]) == (
                            reason, eligible_at, False,
                        )
                    connection.rollback()
                assert _counts(connection) == before
        finally:
            database.close()


def _job(connection, work_type, input_json, *, status="pending", outcome=None, key=None) -> int:
    """Observationless work as the worker queue holds it."""
    lease = ("test", "token", SEASON_END) if status == "leased" else (None, None, None)
    connection.execute("SET LOCAL session_replication_role = replica")
    job_id = connection.execute(
        """
        INSERT INTO python_processing_jobs (
            work_type, deduplication_key, input_json, status, outcome,
            lease_owner, lease_token, lease_expires_at
        ) VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s) RETURNING id
        """,
        (work_type, key or f"guard-{work_type}-{json.dumps(input_json)}-{status}-{outcome}",
         json.dumps(input_json), status, outcome, *lease),
    ).fetchone()[0]
    connection.execute("SET LOCAL session_replication_role = DEFAULT")
    return int(job_id)


def _day(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _observation(connection_info, archive_server, key, observed_at) -> tuple[int, int]:
    return store_observation(
        connection_info, archive_server, occurrence_key=key, endpoint="battle_log",
        body=BATTLE_LOG, observed_at=observed_at, normalized_tag="#2PP",
    )


def _processed(connection, observation_id, job_id, outcome="processed", parser="p1") -> None:
    """The retained result a finished processing job leaves behind."""
    connection.execute(
        """
        INSERT INTO observation_processing_outcomes (
            observation_id, parser_version, processing_version, endpoint,
            response_hash, source_http_status, source_observed_at, outcome
        ) VALUES (%s, %s, 'v1', 'battle_log', repeat('a', 64), 200, %s, %s)
        """,
        (observation_id, parser, DAY0, outcome),
    )
    connection.execute(
        "UPDATE python_processing_jobs SET status = 'complete', outcome = %s,"
        " completed_at = clock_timestamp() WHERE id = %s",
        (outcome, job_id),
    )


def _closes(connection) -> dict:
    """Blockers, after checking finalization refuses and writes nothing."""
    report = finalize_season_detail(connection, SEASON, ELIGIBLE, apply=True)
    assert (report["status"], report["applied"]) == ("blocked", False)
    assert connection.execute("SELECT count(*) FROM season_detail_retirements").fetchone()[0] == 0
    return report["blocking_work"]


def test_failed_cancelled_and_waiting_work_blocks_close(database_url: str, archive_server) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                day = {"player_id": 1, "ranked_day_start": _day(DAY0 + timedelta(days=2))}
                for status, outcome in (
                    ("pending", None), ("leased", None), ("waiting_retry", None),
                    ("failed", "durable_failure"), ("cancelled", None),
                    ("complete", None), ("complete", "source_non_success"),
                    ("complete", "processed_with_gaps"), ("complete", "superseded"),
                    ("complete", "a_future_outcome"),
                ):
                    job_id = _job(connection, "reconcile_ranked_day", day, status=status, outcome=outcome)
                    assert _closes(connection)["processing_jobs"] == [job_id], (status, outcome)
                    connection.rollback()
                _job(connection, "reconcile_ranked_day", day, status="complete", outcome="processed")
                assert _closes(connection) == HISTORY_ONLY
                connection.rollback()
                observation_id, job_id = _observation(connection_info, archive_server, "guard-replay", DAY0 + timedelta(days=3))
                _processed(connection, observation_id, job_id)
                connection.commit()
                assert _closes(connection) == HISTORY_ONLY
                connection.rollback()
                for status in ("requested", "enqueued", "failed", "cancelled", "complete"):
                    request_id = connection.execute(
                        """
                        INSERT INTO python_replay_requests (
                            observation_id, operator_identity, reason, status,
                            target_parser_version, target_domain_rule_version
                        ) VALUES (%s, 'test.op', 'guard probe', %s, 'p2', 'r1') RETURNING id
                        """,
                        (observation_id, status),
                    ).fetchone()[0]
                    expected = HISTORY_ONLY if status == "complete" else {**HISTORY_ONLY, "replay_requests": [request_id]}
                    assert _closes(connection) == expected, status
                    connection.rollback()
                # Examples are capped; every other blocker is still found.
                for index in range(season_finalization_guard.EXAMPLE_LIMIT + 2):
                    _job(connection, "build_export", {"export_request_id": index + 1}, key=f"guard-export-{index}")
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "INSERT INTO boundary_publication_generations (boundary_at, generation, ordering_rule_version,"
                    " freshness_rule_version, expected_population_count, expected_population_hash, target_at)"
                    " VALUES (%s, 1, 'o1', 'f1', 0, repeat('a', 64), %s)",
                    (SEASON_END, SEASON_END),
                )
                blockers = _closes(connection)
                assert len(blockers["processing_jobs"]) == season_finalization_guard.EXAMPLE_LIMIT
                assert set(blockers) == {"processing_jobs", "boundary_generations", "promised_history"}
                connection.rollback()
        finally:
            database.close()


def test_late_fetched_old_day_and_late_replay_block_close(database_url: str, archive_server) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                # Fetched the day after the Season, after the wait, and long
                # after: a rolling log can still carry September battles.
                for key, fetched in (
                    ("guard-oct-6", SEASON_END + timedelta(days=1)),
                    ("guard-after-wait", ELIGIBLE + timedelta(hours=1)),
                    ("guard-much-later", ELIGIBLE + timedelta(days=40)),
                ):
                    observation_id, job_id = _observation(connection_info, archive_server, key, fetched)
                    blockers = _closes(connection)
                    assert (blockers["processing_jobs"], blockers["unproven_observations"]) == (
                        [job_id], [observation_id],
                    ), key
                    connection.rollback()
                    _processed(connection, observation_id, job_id)
                    connection.commit()
                assert _closes(connection) == HISTORY_ONLY
                connection.rollback()
                # A replay requested after the wait of a response fetched in the Season.
                observation_id, job_id = _observation(connection_info, archive_server, "guard-in-season", DAY0 + timedelta(days=27))
                _processed(connection, observation_id, job_id)
                connection.commit()
                request_id = connection.execute(
                    """
                    INSERT INTO python_replay_requests (
                        observation_id, operator_identity, reason, requested_at,
                        target_parser_version, target_domain_rule_version
                    ) VALUES (%s, 'test.op', 'late replay', %s, 'p2', 'r1') RETURNING id
                    """,
                    (observation_id, ELIGIBLE + timedelta(days=1)),
                ).fetchone()[0]
                assert _closes(connection) == {**HISTORY_ONLY, "replay_requests": [request_id]}
                connection.rollback()
                # Fetched before the Legend day ahead of the Season: cannot touch it.
                _observation(connection_info, archive_server, "guard-before", DAY0 - timedelta(days=1, seconds=1))
                assert _closes(connection) == HISTORY_ONLY
                connection.rollback()
        finally:
            database.close()


def test_closing_boundary_snapshot_analytics_army_and_correction_block_close(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                published = {"generation": 1, "manifest_id": 1, "manifest_digest": "a" * 64}
                for work_type, extra in (
                    ("build_snapshot", {}),
                    ("build_analytics", {"selection": {"ranked_day_version_id": 1}}),
                    ("build_army_analytics", {"official_season_id": SEASON}),
                ):
                    def build(moment, work_type=work_type, extra=extra):
                        dates = {"boundary_at": _day(moment)}
                        if work_type == "build_army_analytics":
                            dates["ranked_day_start"] = _day(moment)
                        return _job(connection, work_type, {**published, **extra, **dates})

                    job_id = build(SEASON_END)
                    assert _closes(connection)["processing_jobs"] == [job_id], work_type
                    connection.rollback()
                    # A build for the next Reset belongs to October.
                    build(SEASON_END + timedelta(days=1))
                    assert _closes(connection) == HISTORY_ONLY, work_type
                    connection.rollback()
                # The opening day reads the ending result; unreadable dates stay in scope.
                for input_json in (
                    {"player_id": 1, "ranked_day_start": _day(SEASON_END)},
                    {"player_id": 1, "ranked_day_start": "2026-05-32T05:00:00Z"},
                ):
                    job_id = _job(connection, "reconcile_ranked_day", input_json)
                    assert _closes(connection)["processing_jobs"] == [job_id], input_json
                    connection.rollback()
                _job(connection, "reconcile_ranked_day", {"player_id": 1, "ranked_day_start": _day(SEASON_END + timedelta(days=1))})
                assert _closes(connection) == HISTORY_ONLY
                connection.rollback()
                for snapshot_state, army_state in (
                    ("pending", "pending"), ("ready", "ready"), ("building", "published"),
                    ("published", "failed"), ("published", "published"), ("superseded", "superseded"),
                ):
                    connection.execute("SET LOCAL session_replication_role = replica")
                    generation_id = connection.execute(
                        """
                        INSERT INTO boundary_publication_generations (
                            boundary_at, generation, ordering_rule_version, freshness_rule_version,
                            expected_population_count, expected_population_hash, target_at,
                            snapshot_state, army_state
                        ) VALUES (%s, 1, 'o1', 'f1', 0, repeat('a', 64), %s, %s, %s) RETURNING id
                        """,
                        (SEASON_END, SEASON_END, snapshot_state, army_state),
                    ).fetchone()[0]
                    settled = {snapshot_state, army_state} <= {"published", "superseded"}
                    expected = HISTORY_ONLY if settled else {**HISTORY_ONLY, "boundary_generations": [generation_id]}
                    assert _closes(connection) == expected, (snapshot_state, army_state)
                    if not settled:
                        connection.rollback()
                        continue
                    # A later correction can still carry the ending result forward.
                    for state in ("queued", "activation", "pending_inputs", "inheritance", "active", "terminal", "finalized"):
                        correction_id = connection.execute(
                            """
                            INSERT INTO boundary_publication_corrections (
                                boundary_at, source_generation_id, generation_id, state
                            ) VALUES (%s, %s, %s, %s) RETURNING id
                            """,
                            (SEASON_END + timedelta(days=1), generation_id,
                             generation_id if state == "finalized" else None, state),
                        ).fetchone()[0]
                        expected = HISTORY_ONLY if state == "finalized" else {**HISTORY_ONLY, "boundary_corrections": [correction_id]}
                        assert close_blockers(connection, SEASON, DAY0, SEASON_END) == expected, state
                        connection.execute("DELETE FROM boundary_publication_corrections")
                    connection.rollback()
        finally:
            database.close()


def test_cleanup_does_not_turn_missing_processing_proof_into_success(database_url: str, archive_server) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                fetched = DAY0 + timedelta(days=5)
                proven, proven_job = _observation(connection_info, archive_server, "guard-proven", fetched)
                _processed(connection, proven, proven_job)
                superseded, superseded_job = _observation(connection_info, archive_server, "guard-superseded", fetched)
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'complete', outcome = 'superseded',"
                    " completed_at = clock_timestamp() WHERE id = %s",
                    (superseded_job,),
                )
                gaps, gaps_job = _observation(connection_info, archive_server, "guard-gaps", fetched)
                _processed(connection, gaps, gaps_job, outcome="processed_with_gaps")
                connection.execute(
                    "UPDATE python_processing_jobs SET updated_at = clock_timestamp() - interval '3 days'"
                )
                connection.commit()
                # The real finished-job cleanup removes the completed jobs.
                connection.execute("SELECT * FROM clashlens_prune_finished_jobs(48, 1000, true)")
                connection.commit()
                assert connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE id = ANY(%s)",
                    ([proven_job, superseded_job, gaps_job],),
                ).fetchone()[0] == 0
                blockers = _closes(connection)
                assert "processing_jobs" not in blockers
                assert sorted(blockers["unproven_observations"]) == sorted([superseded, gaps])
                connection.rollback()
                # A newer complete result replaces the gaps; nothing proves the superseded one.
                connection.execute(
                    """
                    INSERT INTO observation_processing_outcomes (
                        observation_id, parser_version, processing_version, endpoint,
                        response_hash, source_http_status, source_observed_at, outcome
                    ) VALUES (%s, 'p2', 'v1', 'battle_log', repeat('a', 64), 200, %s, 'processed')
                    """,
                    (gaps, fetched),
                )
                assert _closes(connection)["unproven_observations"] == [superseded]
                connection.rollback()
        finally:
            database.close()


def test_legacy_history_formats_keep_finalization_and_retirement_blocked(database_url: str, monkeypatch) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                ready = _summary_digests(connection, monkeypatch)
                # Every current summary matches and no work is left, yet the
                # promised history has no accepted format to check.
                assert _closes(connection) == HISTORY_ONLY
                connection.rollback()
                _legacy_finalized_record(connection, ready)
                before = _counts(connection)
                late = ELIGIBLE + timedelta(days=30)
                repeat = finalize_season_detail(connection, SEASON, late, apply=True)
                assert (repeat["status"], repeat["blocking_work"], repeat["existing_status"]) == (
                    "blocked", HISTORY_ONLY, "finalized",
                )
                connection.rollback()
                for apply in (False, True):
                    blocked = retire_season_detail(connection, SEASON, max_rows=1000, apply=apply, now=late)
                    assert (blocked["status"], blocked["blocking_work"], blocked["applied"]) == (
                        "blocked", HISTORY_ONLY, False,
                    )
                    connection.commit()
                assert _counts(connection) == before
                assert connection.execute(
                    "SELECT status, progress FROM season_detail_retirements"
                ).fetchone() == ("finalized", {})
        finally:
            database.close()


def test_missing_relation_and_timed_out_check_block_close(database_url: str, monkeypatch) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                connection.execute("ALTER TABLE python_replay_requests RENAME TO renamed_replay_requests")
                assert _closes(connection) == {**HISTORY_ONLY, "missing_relations": ["python_replay_requests"]}
                connection.rollback()
                caller_limit = connection.execute("SHOW statement_timeout").fetchone()[0]
                monkeypatch.setattr(season_finalization_guard, "CHECK_TIMEOUT", "200ms")
                with psycopg.connect(connection_info) as holder:
                    holder.execute("LOCK TABLE boundary_publication_corrections IN ACCESS EXCLUSIVE MODE")
                    blockers = _closes(connection)
                    holder.rollback()
                assert blockers == {
                    **HISTORY_ONLY,
                    "failed_checks": [{"check": "boundary_corrections", "error": "QueryCanceled"}],
                }
                # The caller's own time limit is untouched afterwards.
                assert connection.execute("SHOW statement_timeout").fetchone()[0] == caller_limit
                connection.rollback()
        finally:
            database.close()


@pytest.mark.parametrize("apply", [False, True])
def test_retirement_rechecks_work_after_finalization(database_url: str, archive_server, monkeypatch, apply) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                _legacy_finalized_record(connection, _summary_digests(connection, monkeypatch))
                before = _counts(connection)
                monkeypatch.setattr(season_finalization_guard, "promised_history_gap", lambda *_: None)
                observation_id, _ = _observation(connection_info, archive_server, "guard-retire", SEASON_END + timedelta(days=2))
                blocked = retire_season_detail(connection, SEASON, apply=apply, now=ELIGIBLE)
                assert (blocked["status"], blocked["blocking_work"]["unproven_observations"]) == (
                    "blocked", [observation_id],
                )
                connection.commit()
                assert _counts(connection) == before
        finally:
            database.close()
