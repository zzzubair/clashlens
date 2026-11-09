"""Shared setup for army analytics publication seasons postgres tests."""

from __future__ import annotations

from datetime import datetime, timedelta

from army_analytics_publication_postgres_support import (
    DAY_START,
)

from clashlens.db import Database


def _insert_confirmed_anchor(
    database: Database, current_season_id: str, previous_season_id: str
) -> None:
    """Insert one confirmed season anchor without replaying profile intake."""
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        observation_id = int(
            connection.execute("SELECT max(id) FROM collector_observations").fetchone()[0]
        )
        profile_version_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, current_league_season_id,
                previous_league_season_id, profile_json
            ) VALUES (
                %s, %s, '#2PP', 'v1', 'profile-schema-v1', 'test-fixture',
                %s, 200, 'Opp', 6000, 29000022, 'Legend League', 'eligible',
                %s, %s, '{}'::jsonb
            ) RETURNING id
            """,
            (player_id, observation_id, DAY_START, current_season_id, previous_season_id),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO legend_season_anchors (
                current_league_season_id, previous_league_season_id,
                current_start, previous_start, anchor_rule_version,
                source_profile_version_id, state
            ) VALUES (%s, %s, %s, %s - interval '28 days',
                      'legend-season-anchor-v1', %s, 'confirmed')
            """,
            (current_season_id, previous_season_id, DAY_START, DAY_START,
             profile_version_id),
        )


def _seed_frozen_snapshot_at(
    database: Database,
    boundary: datetime,
    entries: list[tuple[int, int, str, str]],
) -> int:
    """entries: (player_id, position, freshness, confidence)."""
    with database.pool.connection() as connection:
        observation_id = connection.execute(
            "SELECT id FROM collector_observations ORDER BY id LIMIT 1"
        ).fetchone()[0]
        snapshot_id = connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                snapshot_kind, boundary_at, version, ordering_rule_version,
                freshness_rule_version, state, measured_coverage, stale_entry_count
            ) VALUES ('frozen', %s, 1, 'ordering-v1', 'freshness-v1', 'published', 1.0, 0)
            RETURNING id
            """,
            (boundary,),
        ).fetchone()[0]
        for player_id, position, freshness, confidence in entries:
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, trophies,
                    trophy_observation_id, trophy_observed_at,
                    observation_age_seconds, freshness, confidence, tie_hash
                ) VALUES (%s, %s, %s, 6000, %s, %s, 10, %s, %s, repeat('c', 64))
                """,
                (
                    snapshot_id,
                    position,
                    player_id,
                    observation_id,
                    boundary - timedelta(days=1),
                    freshness,
                    confidence,
                ),
            )
        return snapshot_id
