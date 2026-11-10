"""Shared setup for season detail retirement postgres tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from player_season_summaries_postgres_support import _player

from clashlens import (
    season_finalization_guard,
)
from clashlens.army_season_summaries import materialize_completed_army_season
from clashlens.season_summaries import materialize_completed_seasons

SEASON = "1785714000"


DAY0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)


SEASON_END = DAY0 + timedelta(days=28)


AFTER_SEASON = SEASON_END + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _assume_expanded_history(monkeypatch):
    """Substitution: no check for the expanded history exists yet, so these
    deletion regressions assume it passes. The guard tests prove the real
    default refuses finalization and retirement."""
    monkeypatch.setattr(season_finalization_guard, "promised_history_gap", lambda *_: None)


def _ranked(connection, player_id, day_number, start, end, *, season=SEASON):
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
            'reconciliation-v1', %s, 1,
            'Complete', 'exact', %s, %s, %s, 2, 1, 30, 20
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
            6000 + (day_number - 1) * 10,
            6000 + day_number * 10,
            6000 + day_number * 10,
        ),
    ).fetchone()[0]


def _log(connection, player_id, day_number, ranked_version_id, start, *, season=SEASON):
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
            %s, %s, %s, 1, 'Complete', 'complete', %s, %s, %s, 'exact',
            2, 1, 30, 1, 0, 20, 10, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        )
        """,
        (
            player_id,
            start,
            ranked_version_id,
            start + timedelta(days=1),
            season,
            day_number,
        ),
    )


def _full_season(connection, player_id, *, season=SEASON, day0=DAY0):
    for day in range(1, 29):
        start = day0 + timedelta(days=day - 1)
        version_id = _ranked(connection, player_id, day, start, start + timedelta(days=1), season=season)
        _log(connection, player_id, day, version_id, start, season=season)


def _seed_army(connection, season=SEASON, day0=DAY0, *, tag="#2PP", base=7000):
    """Minimal facts + completed days so army summaries materialize."""
    connection.execute("SET LOCAL session_replication_role = replica")
    player_id = connection.execute(
        "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
    ).fetchone()
    player_id = int(player_id[0]) if player_id else _player(connection, tag)
    for lens, battle_id, evidence_id in (
        ("offense", base + 1, base + 1001),
        ("defense", base + 2, base + 1002),
    ):
        connection.execute(
            """
            INSERT INTO army_analytics_battle_facts (
                battle_id, evidence_id, source_ranked_day_version_id,
                ranked_day_start, official_season_id, season_day_number,
                lens, population_player_id, stars, destruction_percentage,
                army_state, perspective_disagreement,
                battle_time_trophies, input_hash, version
            ) VALUES (
                %s, %s, 4242, %s, %s, 1, %s, %s, 3, 100, 'decoded',
                false, 6000, %s, 1
            )
            """,
            (
                battle_id,
                evidence_id,
                day0,
                season,
                lens,
                player_id,
                hashlib.sha256(f"{season}:{lens}".encode()).hexdigest(),
            ),
        )
    for day in range(1, 29):
        connection.execute(
            """
            INSERT INTO army_analytics_completed_days (
                ranked_day_start, official_season_id, season_day_number,
                fact_input_hash
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (ranked_day_start) DO NOTHING
            """,
            (
                day0 + timedelta(days=day - 1),
                season,
                day,
                hashlib.sha256(f"{season}:day:{day}".encode()).hexdigest(),
            ),
        )
    return player_id


def _ensure_canonical_anchor(connection, season=SEASON, start=DAY0):
    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        """
        INSERT INTO legend_season_anchors (
            current_league_season_id, previous_league_season_id,
            current_start, previous_start, anchor_rule_version,
            source_profile_version_id, state
        )
        SELECT %s, %s, %s, %s, 'legend-season-anchor-v1', 1, 'confirmed'
        WHERE NOT EXISTS (
            SELECT 1 FROM legend_season_anchors
            WHERE state = 'confirmed' AND anchor_rule_version = 'legend-season-anchor-v1'
              AND (current_league_season_id = %s OR previous_league_season_id = %s)
        )
        """,
        (season, f"previous-{season}", start, start - timedelta(days=28), season, season),
    )
    connection.execute("SET LOCAL session_replication_role = DEFAULT")


def _materialize_all(connection, season=SEASON):
    _ensure_canonical_anchor(connection, season)
    player_report = materialize_completed_seasons(
        connection, season_id=season, max_players=1000, now=AFTER_SEASON
    )
    army_report = materialize_completed_army_season(
        connection, season_id=season, now=AFTER_SEASON
    )
    return player_report, army_report
