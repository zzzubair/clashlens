"""Shared setup for boundary publication postgres tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clashlens import (
    boundary,
)
from clashlens.db import Database

BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)


DAY_START = BOUNDARY - timedelta(days=1)


def _player_and_version(
    connection, tag: str, version: int, input_hash: str
) -> tuple[int, int]:
    player_id = int(
        connection.execute(
            "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
            (tag,),
        ).fetchone()[0]
    )
    version_id = int(
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end, official_season_id,
                season_day_number, season_anchor_rule_version,
                reconciliation_rule_version, result_hash, version, state, confidence,
                input_hash, evidence_complete, coverage_complete
            ) VALUES (
                %s, %s, %s, 'test-season', 1, 'test-anchor', 'test-rules',
                %s, %s, 'Complete', 'exact', %s, true, true
            ) RETURNING id
            """,
            (player_id, DAY_START, BOUNDARY, input_hash, version, input_hash),
        ).fetchone()[0]
    )
    return player_id, version_id


def _sweep_with_members(connection, player_ids: list[int]) -> int:
    sweep_id = int(
        connection.execute(
            """
            INSERT INTO collector_reset_sweeps
                (boundary_at, member_ids, membership_captured_at)
            VALUES (%s, %s, clock_timestamp())
            RETURNING id
            """,
            (BOUNDARY, player_ids),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
            coalescing_key, profile_status, battle_log_status
        )
        SELECT 'reset_baseline', 'reset', 'player', player.id,
               player.normalized_tag, %s, %s,
               'reset:' || %s || ':' || player.id, 'pending', 'pending'
        FROM players AS player
        WHERE player.id = ANY(%s::bigint[])
        ON CONFLICT DO NOTHING
        """,
        (sweep_id, BOUNDARY, sweep_id, player_ids),
    )
    return sweep_id


def _board_published(database: Database, connection, generation: int = 1) -> None:
    """The generation's board and statistics publish, and the publication
    check freezes its army's inputs, which wait for them."""
    generation_id = connection.execute(
        "UPDATE boundary_publication_generations SET snapshot_state = 'published'"
        " WHERE generation = %s RETURNING id", (generation,)).fetchone()[0]
    boundary._try_enqueue_boundary_artifacts(
        database, connection, boundary_at=BOUNDARY, generation_id=generation_id, army=True)
