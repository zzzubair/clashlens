"""Shared setup for player season summaries postgres tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

SEASON = "1785714000"


DAY0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)


SEASON_END = DAY0 + timedelta(days=28)


def _player(connection, tag="#2PP"):
    return connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state)
        VALUES (%s, true, 'eligible')
        RETURNING id
        """,
        (tag,),
    ).fetchone()[0]


def _ranked(
    connection, player_id, day_number, start, end, *, season=SEASON, version=1, trophies=None
):
    begin, eod = trophies or (6000 + (day_number - 1) * 10, 6000 + day_number * 10)
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
            f"{version:032x}{day_number:032x}",
            version,
            begin,
            eod,
            eod,
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


def _frozen_board(
    connection, observation_id, positions, *, version=1, state="published", reset=SEASON_END
):
    """Save one frozen board at reset; positions maps player to (position, official)."""
    snapshot_id = connection.execute(
        """
        INSERT INTO leaderboard_snapshots (
            snapshot_kind, boundary_at, version, ordering_rule_version,
            freshness_rule_version, state, measured_coverage, stale_entry_count
        ) VALUES ('frozen', %s, %s, 'ordering-v1', 'freshness-v1', %s, 1.0, 0)
        RETURNING id
        """,
        (reset, version, state),
    ).fetchone()[0]
    for player_id, (position, official_rank) in positions.items():
        connection.execute(
            """
            INSERT INTO leaderboard_snapshot_entries (
                snapshot_id, position, player_id, trophies, trophy_observation_id,
                trophy_observed_at, observation_age_seconds, freshness,
                confidence, tie_hash, official_rank
            ) VALUES (%s, %s, %s, 6280, %s, %s, 0, 'fresh', 'confirmed',
                      repeat('c', 64), %s)
            """,
            (snapshot_id, position, player_id, observation_id, reset, official_rank),
        )
    return snapshot_id
