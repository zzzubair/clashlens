"""Historical player-season summaries (issue #82, first slice).

A player_season_summaries row is derived from the latest published
api_player_daily_logs version per day, joined only to that log's exact
ranked_day_version_id. Historical reads use the summary alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation

from clashlens.api_db import ApiDatabase
from clashlens.db import Database
from clashlens.domain import SEASON_ANCHOR_RULE_VERSION
from clashlens.reconciliation import ReconciliationResult
from clashlens.season_summaries import (
    PROJECTION_VERSION,
    materialize_completed_seasons,
    materialize_player_season,
)

SEASON = "1785714000"
DAY0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)
SEASON_END = DAY0 + timedelta(days=28)
AFTER_SEASON = SEASON_END + timedelta(hours=1)


def _player(connection, tag="#2PP"):
    return connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state)
        VALUES (%s, true, 'eligible')
        RETURNING id
        """,
        (tag,),
    ).fetchone()[0]


def _ranked(connection, player_id, day_number, start, end, *, season=SEASON, version=1):
    return connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, result_hash, version,
            state, confidence, start_trophies, final_trophies_before_reset,
            next_start_trophies, attack_count, defense_count,
            attack_gain, observed_defense_loss
        ) VALUES (
            %s, %s, %s, %s, %s, 'season-anchor-v1',
            'reconciliation-v1', %s, %s,
            'Complete', 'exact', %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING id
        """,
        (
            player_id,
            start,
            end,
            season,
            day_number,
            f"{day_number:064x}",
            version,
            6000 + (day_number - 1) * 10,
            6000 + day_number * 10,
            6000 + day_number * 10,
            2,
            1,
            30,
            20,
        ),
    ).fetchone()[0]


def _log(
    connection,
    player_id,
    day_number,
    ranked_version_id,
    start,
    *,
    version=1,
    state="Complete",
    coverage="complete",
    attack_gain=30,
    defense_loss=20,
    net=10,
    battles=None,
    season=SEASON,
    partial_reasons=None,
):
    connection.execute(
        """
        INSERT INTO api_player_daily_logs (
            player_id, ranked_day_start, ranked_day_version_id, version,
            state, coverage, ranked_day_end, official_season_id,
            season_day_number, confidence, attack_count,
            attack_three_star_count, attack_gain, defense_count,
            defense_three_star_count, defense_loss, net_trophy_change,
            adjustments, battles, partial_reasons
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, 'exact', 2, 1, %s, 1, 0,
            %s, %s, '[]'::jsonb, %s::jsonb, %s::jsonb
        )
        """,
        (
            player_id,
            start,
            ranked_version_id,
            version,
            state,
            coverage,
            start + timedelta(days=1),
            season,
            day_number,
            attack_gain,
            defense_loss,
            net,
            json.dumps(battles if battles is not None else []),
            json.dumps(partial_reasons if partial_reasons is not None else []),
        ),
    )


def _event(lens, battle_id, stars, trophy):
    return {
        "included": True,
        "lens": lens,
        "battle_id": str(battle_id),
        "battle_timestamp": "2026-05-01T06:00:00Z",
        "opponent": {"tag": "#8PY", "name": "Opp"},
        "destruction_percentage": 100 if stars == 3 else 50,
        "stars": stars,
        "trophy_change": trophy,
    }


def _full_season(connection, player_id, *, battles=None):
    for day in range(1, 29):
        start = DAY0 + timedelta(days=day - 1)
        version_id = _ranked(connection, player_id, day, start, start + timedelta(days=1))
        day_battles = battles(day) if battles else []
        _log(connection, player_id, day, version_id, start, battles=day_battles)


def _summary(connection, player_id, season=SEASON):
    cursor = connection.execute(
        "SELECT * FROM player_season_summaries WHERE player_id = %s AND official_season_id = %s",
        (player_id, season),
    )
    row = cursor.fetchone()
    assert row is not None
    columns = [d.name for d in cursor.description]
    return dict(zip(columns, row))


def test_migration_is_reentrant(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            migration = (
                Path(__file__).parents[2]
                / "deploy"
                / "migrations"
                / "0019_player_season_summaries.sql"
            )
            connection.execute(migration.read_text(encoding="utf-8"))
            row = connection.execute(
                "SELECT count(*) FROM player_season_summaries"
            ).fetchone()
            assert row[0] == 0


def test_complete_season_totals_boundaries_and_stars(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)

                def battles(day):
                    return [
                        _event("offense", f"{day}a", 3, 20),
                        _event("offense", f"{day}b", 2, 10),
                        _event("defense", f"{day}c", 1, -20),
                    ]

                _full_season(connection, player_id, battles=battles)
                connection.commit()
                result = materialize_player_season(connection, player_id, SEASON)
                connection.commit()
            assert result["status"] == "published"
            with database.pool.connection() as connection:
                summary = _summary(connection, player_id)
            assert summary["coverage_state"] == "complete"
            assert summary["days_observed"] == 28
            assert summary["days_missing"] == 0
            assert summary["attack_count"] == 56
            assert summary["attack_gain"] == 840
            assert summary["defense_count"] == 28
            assert summary["defense_loss"] == 560
            assert summary["net_trophy_change"] == 280
            assert summary["start_trophies"] == 6000
            assert summary["end_trophies"] == 6280
            assert summary["season_start"] == DAY0
            assert summary["season_end"] == SEASON_END
            assert summary["final_rank"] is None
            assert (summary["attack_star_3"], summary["attack_star_2"]) == (28, 28)
            assert (summary["attack_star_0"], summary["attack_star_unknown"]) == (0, 0)
            assert summary["defense_star_1"] == 28
            assert summary["projection_version"] == PROJECTION_VERSION
            assert len(summary["daily_entries"]) == 28
            first = summary["daily_entries"][0]
            assert first["season_day_number"] == 1
            assert first["start_trophies"] == 6000
            assert first["end_trophies"] == 6010
            assert first["attack_gain"] == 30
            assert first["defense_loss"] == 20
            assert all("battle" not in key for key in first)
        finally:
            database.close()


def test_partial_season_marks_missing_and_null_boundaries(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                for day in (2, 3):
                    start = DAY0 + timedelta(days=day - 1)
                    version_id = _ranked(
                        connection, player_id, day, start, start + timedelta(days=1)
                    )
                    _log(connection, player_id, day, version_id, start)
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                summary = _summary(connection, player_id)
            assert summary["coverage_state"] == "partial"
            assert summary["days_observed"] == 2
            assert summary["days_missing"] == 26
            assert summary["missing_days"] == [1, *range(4, 29)]
            # Day 1 evidence is absent, so no season start boundary.
            assert summary["start_trophies"] is None
            assert summary["season_start"] is None
            assert summary["end_trophies"] is None
            assert "missing_days" in summary["unresolved_flags"]
        finally:
            database.close()


def test_complete_requires_clean_days_flags_and_boundaries(database_url: str) -> None:
    cases = [
        ("unlinked_day1", "detailed_boundaries_unavailable"),
        ("mismatch", "ranked_version_mismatch"),
        ("reason", "late_correction_pending"),
    ]
    for poison, expected_flag in cases:
        with domain_database(database_url, include_coordinator=True) as connection_info:
            database = ApiDatabase(connection_info)
            try:
                with database.pool.connection() as connection:
                    player_id = _player(connection)

                    def battles(day):
                        return [
                            _event("offense", f"{day}a", 3, 20),
                            _event("offense", f"{day}b", 2, 10),
                            _event("defense", f"{day}c", 1, -20),
                        ]

                    for day in range(1, 29):
                        start = DAY0 + timedelta(days=day - 1)
                        version_id = _ranked(
                            connection,
                            player_id,
                            day,
                            start,
                            start + timedelta(days=1),
                        )
                        if poison == "unlinked_day1" and day == 1:
                            _log(connection, player_id, day, None, start)
                        elif poison == "mismatch" and day == 5:
                            _log(
                                connection,
                                player_id,
                                day,
                                version_id,
                                start,
                                attack_gain=9999,
                                battles=battles(day),
                            )
                        elif poison == "reason" and day == 10:
                            _log(
                                connection,
                                player_id,
                                day,
                                version_id,
                                start,
                                battles=battles(day),
                                partial_reasons=["late_correction_pending"],
                            )
                        else:
                            _log(
                                connection,
                                player_id,
                                day,
                                version_id,
                                start,
                                battles=battles(day),
                            )
                    connection.commit()
                    materialize_player_season(connection, player_id, SEASON)
                    connection.commit()
                    summary = _summary(connection, player_id)
                assert summary["coverage_state"] == "partial", poison
                assert expected_flag in summary["unresolved_flags"], poison
                if poison == "unlinked_day1":
                    assert summary["start_trophies"] is None, poison
                    # The day-1 timestamp is known; only the trophy boundary
                    # is unknown, and that alone forces partial.
                    assert summary["season_start"] is not None, poison
            finally:
                database.close()


def test_repeated_and_opposite_events_count_once_per_lens_identity(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                start = DAY0
                version_id = _ranked(
                    connection, player_id, 1, start, start + timedelta(days=1)
                )
                battles = [
                    # Zero-star attacks with trophy movement are preserved.
                    _event("offense", "1", 0, 5),
                    # A repeated same-lens sighting counts once.
                    _event("offense", "1", 0, 5),
                    _event("defense", "2", 0, -10),
                    # Opposite perspectives of one battle both survive.
                    _event("offense", "6", 3, 20),
                    _event("defense", "6", 2, -15),
                    {**_event("offense", "3", 2, 10), "stars": "bad"},
                    {**_event("defense", "4", 1, -5), "stars": None},
                    {**_event("offense", "5", 3, 20), "included": False},
                    {"lens": "offense", "stars": 3},
                ]
                _log(connection, player_id, 1, version_id, start, battles=battles)
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                summary = _summary(connection, player_id)
            assert summary["attack_star_0"] == 1
            assert summary["attack_star_unknown"] == 1
            assert summary["attack_star_3"] == 1
            assert summary["defense_star_0"] == 1
            assert summary["defense_star_2"] == 1
            assert summary["defense_star_unknown"] == 1
        finally:
            database.close()


def test_single_long_reason_sets_overflow_marker(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                start = DAY0
                version_id = _ranked(
                    connection, player_id, 1, start, start + timedelta(days=1)
                )
                _log(
                    connection,
                    player_id,
                    1,
                    version_id,
                    start,
                    partial_reasons=["z" * 500],
                )
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                summary = _summary(connection, player_id)
            day_flags = summary["daily_entries"][0]["flags"]
            assert "z" * 64 in day_flags
            assert "truncated_reasons" in day_flags
            assert all(len(flag) <= 64 for flag in day_flags)
            assert "truncated_reasons" in summary["unresolved_flags"]
        finally:
            database.close()


def test_giant_partial_reasons_are_bounded_with_overflow_marker(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                start = DAY0
                version_id = _ranked(
                    connection, player_id, 1, start, start + timedelta(days=1)
                )
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_version_id,
                        version, state, coverage, ranked_day_end,
                        official_season_id, season_day_number, confidence,
                        attack_count, attack_three_star_count, attack_gain,
                        defense_count, defense_three_star_count, defense_loss,
                        net_trophy_change, adjustments, battles, partial_reasons
                    ) VALUES (
                        %s, %s, %s, 1, 'Partial', 'partial', %s, %s, 1,
                        'exact', 0, 0, 0, 0, 0, 0, 0, '[]'::jsonb,
                        '[]'::jsonb, %s::jsonb
                    )
                    """,
                    (
                        player_id,
                        start,
                        version_id,
                        start + timedelta(days=1),
                        SEASON,
                        json.dumps(
                            ["x" * 500]
                            + [
                                f"reason-{index}-" + "y" * 280
                                for index in range(50)
                            ]
                        ),
                    ),
                )
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                summary = _summary(connection, player_id)
            day_flags = summary["daily_entries"][0]["flags"]
            assert "truncated_reasons" in day_flags
            assert all(len(flag) <= 64 for flag in day_flags)
            assert "truncated_reasons" in summary["unresolved_flags"]
            assert "partial_days" in summary["unresolved_flags"]
            assert all(len(flag) <= 64 for flag in summary["unresolved_flags"])
            assert len(summary["unresolved_flags"]) <= 27
        finally:
            database.close()


def test_retry_is_noop_correction_replaces_and_failure_preserves(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                connection.commit()
                first = materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                before = _summary(connection, player_id)
                second = materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                assert second["status"] == "unchanged"
                assert (
                    _summary(connection, player_id)["published_at"]
                    == before["published_at"]
                )
                # A corrected publication (new version) replaces the summary.
                start = DAY0
                ranked2 = connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, version, state, confidence,
                        start_trophies, final_trophies_before_reset,
                        next_start_trophies
                    ) VALUES (%s, %s, %s, %s, 1, 'season-anchor-v1',
                              'reconciliation-v1', %s, 2, 'Complete', 'exact',
                              6000, 6010, 6010)
                    RETURNING id
                    """,
                    (player_id, start, start + timedelta(days=1), SEASON, "b" * 64),
                ).fetchone()[0]
                _log(
                    connection,
                    player_id,
                    1,
                    ranked2,
                    start,
                    version=2,
                    attack_gain=99,
                    net=79,
                )
                connection.commit()
                third = materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                assert third["status"] == "published"
                after = _summary(connection, player_id)
                assert after["attack_gain"] == 840 - 30 + 99
                assert after["content_digest"] != before["content_digest"]
                # A failed write keeps the prior summary.
                with pytest.raises(psycopg.errors.CheckViolation):
                    with connection.transaction():
                        connection.execute(
                            "UPDATE player_season_summaries SET attack_count = -1 WHERE player_id = %s",
                            (player_id,),
                        )
                assert (
                    _summary(connection, player_id)["content_digest"]
                    == after["content_digest"]
                )
            assert first["status"] == "published"
        finally:
            database.close()


def test_final_rank_from_season_final_freeze(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                board_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version,
                        coverage
                    ) VALUES (gen_random_uuid(), %s, 1, 'tracked-trophies-md5-v1',
                              '{}'::jsonb)
                    RETURNING id
                    """,
                    (SEASON_END,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboard_entries (
                        leaderboard_id, position, player_id, trophies,
                        observed_at, freshness, confidence, official_rank
                    ) VALUES (%s, 7, %s, 6280, %s, 'fresh', 'exact', 7)
                    """,
                    (board_id, player_id, SEASON_END),
                )
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                assert _summary(connection, player_id)["final_rank"] == 7
        finally:
            database.close()


def test_final_rank_ignores_older_board_when_newest_omits_player(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                old_board_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version,
                        coverage
                    ) VALUES (gen_random_uuid(), %s, 1, 'tracked-trophies-md5-v1',
                              '{}'::jsonb)
                    RETURNING id
                    """,
                    (SEASON_END,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboard_entries (
                        leaderboard_id, position, player_id, trophies,
                        observed_at, freshness, confidence, official_rank
                    ) VALUES (%s, 7, %s, 6280, %s, 'fresh', 'exact', 7)
                    """,
                    (old_board_id, player_id, SEASON_END),
                )
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version,
                        coverage
                    ) VALUES (gen_random_uuid(), %s, 2, 'tracked-trophies-md5-v1',
                              '{}'::jsonb)
                    """,
                    (SEASON_END,),
                )
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                assert _summary(connection, player_id)["final_rank"] is None
        finally:
            database.close()


def test_final_rank_null_when_newest_board_rank_is_null(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                old_board_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version,
                        coverage
                    ) VALUES (gen_random_uuid(), %s, 1, 'tracked-trophies-md5-v1',
                              '{}'::jsonb)
                    RETURNING id
                    """,
                    (SEASON_END,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboard_entries (
                        leaderboard_id, position, player_id, trophies,
                        observed_at, freshness, confidence, official_rank
                    ) VALUES (%s, 7, %s, 6280, %s, 'fresh', 'exact', 7)
                    """,
                    (old_board_id, player_id, SEASON_END),
                )
                new_board_id = connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version,
                        coverage
                    ) VALUES (gen_random_uuid(), %s, 2, 'tracked-trophies-md5-v1',
                              '{}'::jsonb)
                    RETURNING id
                    """,
                    (SEASON_END,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboard_entries (
                        leaderboard_id, position, player_id, trophies,
                        observed_at, freshness, confidence, official_rank
                    ) VALUES (%s, 7, %s, 6280, %s, 'fresh', 'exact', NULL)
                    """,
                    (new_board_id, player_id, SEASON_END),
                )
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                assert _summary(connection, player_id)["final_rank"] is None
        finally:
            database.close()


def test_historical_read_is_detail_independent(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection, "#2PP")
                _full_season(connection, player_id)
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                connection.execute(
                    "DELETE FROM api_player_daily_logs WHERE player_id = %s",
                    (player_id,),
                )
                connection.execute(
                    "DELETE FROM ranked_day_versions WHERE player_id = %s",
                    (player_id,),
                )
                connection.commit()
            page = database.get_player_season_summary("#2PP", SEASON)
            assert page is not None
            assert page["tag"] == "#2PP"
            assert page["attack_count"] == 56
            assert len(page["daily_entries"]) == 28
            assert page["daily_entries"][0]["season_day_number"] == 1
            assert database.get_player_season_summary("#2PP", "no-such-season") is None
            assert database.get_player_season_summary("#9Q2", SEASON) is None
            assert [
                s["official_season_id"] for s in database.list_player_seasons("#2PP")
            ] == [SEASON]
            assert database.list_player_seasons("#9Q2") == []
        finally:
            database.close()


def test_backfill_materializes_completed_seasons_only(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                old_id = _player(connection, "#2PP")
                _full_season(connection, old_id)
                live_id = _player(connection, "#8PY")
                live_start = AFTER_SEASON - timedelta(hours=12)
                live_version = _ranked(
                    connection,
                    live_id,
                    1,
                    live_start,
                    live_start + timedelta(days=1),
                    season="live-season",
                )
                _log(
                    connection,
                    live_id,
                    1,
                    live_version,
                    live_start,
                    season="live-season",
                    state="Live",
                    coverage="partial",
                )
                connection.commit()
            with database.pool.connection() as connection:
                report = materialize_completed_seasons(
                    connection, season_id=SEASON, max_players=10, now=AFTER_SEASON
                )
                connection.commit()
                live_report = materialize_completed_seasons(
                    connection,
                    season_id="live-season",
                    max_players=10,
                    now=AFTER_SEASON,
                )
                connection.commit()
            assert report["season_completed"] is True
            assert report["materialized"] == 1
            assert live_report["season_completed"] is False
            assert live_report["materialized"] == 0
            with database.pool.connection() as connection:
                assert _summary(connection, old_id)["days_observed"] == 28
        finally:
            database.close()


def test_backfill_cursor_pages_beyond_first_max_players(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_ids = []
                for tag in ("#2PP", "#8PY", "#9Q2"):
                    player_id = _player(connection, tag)
                    _full_season(connection, player_id)
                    player_ids.append(player_id)
                connection.commit()
            seen = []
            cursor = 0
            for _ in range(3):
                with database.pool.connection() as connection:
                    report = materialize_completed_seasons(
                        connection,
                        season_id=SEASON,
                        max_players=1,
                        now=AFTER_SEASON,
                        after_player_id=cursor,
                    )
                    connection.commit()
                assert report["season_completed"] is True
                assert report["materialized"] == 1
                assert report["next_after_player_id"] is not None
                cursor = report["next_after_player_id"]
                seen.append(cursor)
            assert seen == sorted(player_ids)
            with database.pool.connection() as connection:
                report = materialize_completed_seasons(
                    connection,
                    season_id=SEASON,
                    max_players=1,
                    now=AFTER_SEASON,
                    after_player_id=cursor,
                )
                connection.commit()
            assert report["materialized"] == 0
            assert report["next_after_player_id"] is None
            with database.pool.connection() as connection:
                for player_id in player_ids:
                    assert _summary(connection, player_id)["days_observed"] == 28
        finally:
            database.close()


def _anchor(connection_info, archive_server, *, current, previous, current_start):
    body = (
        Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
    ).read_bytes()
    observation_id, _job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"anchor-{current}",
        endpoint="profile",
        body=body,
        observed_at=current_start,
        normalized_tag="#ANCHOR",
    )
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#ANCHOR'"
        ).fetchone()[0]
        profile_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, profile_json
            ) VALUES (
                %s, %s, '#ANCHOR', 'profile-v1', 'profile-schema-v1',
                'profile-parser-v1', %s, 200, 'Anchor', 6000, 105000036,
                'Legend I', 'eligible', '{}'::jsonb
            )
            RETURNING id
            """,
            (player_id, observation_id, current_start),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO legend_season_anchors (
                current_league_season_id, previous_league_season_id,
                current_start, previous_start, anchor_rule_version,
                source_profile_version_id, state
            ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
            """,
            (
                current,
                previous,
                current_start,
                current_start - timedelta(days=28),
                SEASON_ANCHOR_RULE_VERSION,
                profile_id,
            ),
        )
        connection.commit()


def test_backfill_refuses_active_canonical_season_with_only_past_days(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        season_start = AFTER_SEASON - timedelta(days=3)
        _anchor(
            connection_info,
            archive_server,
            current="active-season",
            previous="prior-season",
            current_start=season_start,
        )
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                version_id = _ranked(
                    connection,
                    player_id,
                    1,
                    season_start,
                    season_start + timedelta(days=1),
                    season="active-season",
                )
                _log(
                    connection,
                    player_id,
                    1,
                    version_id,
                    season_start,
                    season="active-season",
                )
                connection.commit()
            with database.pool.connection() as connection:
                report = materialize_completed_seasons(
                    connection,
                    season_id="active-season",
                    max_players=10,
                    now=AFTER_SEASON,
                )
                connection.commit()
            assert report["season_completed"] is False
            assert report["reason"] == "season_not_completed"
            assert report["materialized"] == 0
        finally:
            database.close()


def test_backfill_allows_canonical_previous_season_missing_day_28(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        season_start = DAY0
        _anchor(
            connection_info,
            archive_server,
            current="next-season",
            previous=SEASON,
            current_start=SEASON_END,
        )
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                for day in (2, 3):
                    start = season_start + timedelta(days=day - 1)
                    version_id = _ranked(
                        connection, player_id, day, start, start + timedelta(days=1)
                    )
                    _log(connection, player_id, day, version_id, start)
                connection.commit()
            with database.pool.connection() as connection:
                report = materialize_completed_seasons(
                    connection, season_id=SEASON, max_players=10, now=AFTER_SEASON
                )
                connection.commit()
            assert report["season_completed"] is True
            assert report["materialized"] == 1
            with database.pool.connection() as connection:
                assert _summary(connection, player_id)["days_observed"] == 2
        finally:
            database.close()


def _publication_result(*, state="Complete", attack_gain=0, net=0):
    return ReconciliationResult(
        state=state,
        confidence="exact" if state == "Complete" else "partial",
        attack_count=0,
        defense_count=0,
        attack_trophy_gain=attack_gain,
        observed_defense_loss=0,
        automatic_defense_loss=None,
        automatic_defense_evidence_state="unknown",
        boundary_adjustment=0,
        boundary_adjustment_type=None,
        final_trophies_before_reset=6280,
        shield_state="not_inferred",
        shield_duration_days=None,
        coverage_complete=state == "Complete",
        failure_reasons=() if state == "Complete" else ("active_day",),
        net_trophy_change=net,
    )


def _publish(
    database, connection, player_id, day, version_id, start, *, version=1, result=None
):
    database._publish_player_daily_log(
        connection,
        player_id=player_id,
        ranked_day_start=start,
        ranked_day_end=start + timedelta(days=1),
        official_season_id=SEASON,
        season_day_number=day,
        version_number=version,
        ranked_day_version_id=version_id,
        result=result if result is not None else _publication_result(),
        contribution_evidence=[],
    )


def test_day28_publication_establishes_summary(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                start = DAY0 + timedelta(days=27)
                version_id = _ranked(
                    connection, player_id, 28, start, start + timedelta(days=1)
                )
                _publish(database, connection, player_id, 28, version_id, start)
                connection.commit()
                summary = _summary(connection, player_id)
            assert summary["days_observed"] == 1
            assert summary["coverage_state"] == "partial"
            assert summary["end_trophies"] == 6280
            assert summary["missing_days"] == list(range(1, 28))
        finally:
            database.close()


def test_late_correction_publication_updates_existing_summary(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                before = _summary(connection, player_id)
                assert before["attack_gain"] == 840
                day5_start = DAY0 + timedelta(days=4)
                correction_version = connection.execute(
                    """
                    INSERT INTO ranked_day_versions (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number,
                        season_anchor_rule_version, reconciliation_rule_version,
                        result_hash, version, state, confidence,
                        start_trophies, final_trophies_before_reset,
                        next_start_trophies
                    ) VALUES (%s, %s, %s, %s, 5, 'season-anchor-v1',
                              'reconciliation-v1', %s, 2, 'Complete', 'exact',
                              6040, 6050, 6050)
                    RETURNING id
                    """,
                    (
                        player_id,
                        day5_start,
                        day5_start + timedelta(days=1),
                        SEASON,
                        "c" * 64,
                    ),
                ).fetchone()[0]
                _publish(
                    database,
                    connection,
                    player_id,
                    5,
                    correction_version,
                    day5_start,
                    version=2,
                    result=_publication_result(attack_gain=99, net=79),
                )
                connection.commit()
                after = _summary(connection, player_id)
            assert after["attack_gain"] == 840 - 30 + 99
            assert after["content_digest"] != before["content_digest"]
        finally:
            database.close()


def test_live_and_active_day_publications_do_not_summarize(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                live_start = DAY0 + timedelta(days=27)
                live_version = _ranked(
                    connection,
                    player_id,
                    28,
                    live_start,
                    live_start + timedelta(days=1),
                )
                _publish(
                    database,
                    connection,
                    player_id,
                    28,
                    live_version,
                    live_start,
                    result=_publication_result(state="Live"),
                )
                day5_start = DAY0 + timedelta(days=4)
                day5_version = _ranked(
                    connection,
                    player_id,
                    5,
                    day5_start,
                    day5_start + timedelta(days=1),
                )
                _publish(database, connection, player_id, 5, day5_version, day5_start)
                connection.commit()
                count = connection.execute(
                    "SELECT count(*) FROM player_season_summaries WHERE player_id = %s",
                    (player_id,),
                ).fetchone()[0]
            assert count == 0
        finally:
            database.close()


def test_summary_row_size_is_small_and_labeled(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)

                def battles(day):
                    return [
                        _event("offense", f"{day}a", 3, 20),
                        _event("offense", f"{day}b", 2, 10),
                        _event("defense", f"{day}c", 1, -20),
                    ]

                _full_season(connection, player_id, battles=battles)
                connection.commit()
                materialize_player_season(connection, player_id, SEASON)
                connection.commit()
                size = connection.execute(
                    """
                    SELECT pg_column_size(summary.*)
                    FROM player_season_summaries AS summary
                    WHERE player_id = %s AND official_season_id = %s
                    """,
                    (player_id, SEASON),
                ).fetchone()[0]
            # Fixture row-size measurement only, not capacity acceptance.
            assert size < 32768
        finally:
            database.close()
