"""Finalization and retirement wait seven days after the Season ends."""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import domain_database
from test_season_detail_retirement_postgres import (
    DAY0,
    SEASON,
    SEASON_END,
    _full_season,
    _materialize_all,
    _player,
    _seed_army,
)

from clashlens.api_db import ApiDatabase
from clashlens.season_retirement import finalize_season_detail, retire_season_detail

ELIGIBLE = SEASON_END + timedelta(days=7)
EARLY = ELIGIBLE - timedelta(microseconds=1)
WAITING = {"status": "blocked", "reason": "season_close_wait", "eligible_at": ELIGIBLE.isoformat()}


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
                ready = finalize_season_detail(connection, SEASON, ELIGIBLE)
                assert ready["status"] == "ready"
                connection.rollback()
        finally:
            database.close()


def test_early_finalized_record_cannot_retire_or_hide_the_wait(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                _ended_season(connection)
                ready = finalize_season_detail(connection, SEASON, ELIGIBLE)
                connection.rollback()
                # A record older code could have written an hour after the end.
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
                # Retirement reads the database clock by default; May 2026 has waited.
                assert retire_season_detail(connection, SEASON)["eligible_daily_logs"] > 0
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
