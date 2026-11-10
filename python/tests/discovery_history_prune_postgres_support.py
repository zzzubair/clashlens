"""Shared setup for discovery history prune postgres tests."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg

OLD = datetime(2026, 6, 20, 6, tzinfo=UTC)


def _attach_complete_work(connection_info, observation_id: int) -> int:
    with psycopg.connect(connection_info) as connection:
        endpoint, player_id, normalized_tag, scope = connection.execute(
            """
            SELECT endpoint, player_id, normalized_tag, scope
            FROM collector_observations
            WHERE id = %s
            """,
            (observation_id,),
        ).fetchone()
        if endpoint == "global_player_rankings":
            kind, lane = "global_player_rankings", "ordinary"
            profile_status, battle_log_status = "observed", "not_applicable"
            league_history_status = "not_applicable"
        elif endpoint == "profile":
            kind, lane = "initial_collection", "interactive"
            profile_status, battle_log_status = "observed", "pending"
            league_history_status = "observed"
        else:
            kind, lane = "initial_collection", "interactive"
            profile_status, battle_log_status = "pending", "observed"
            league_history_status = "observed"
        work_id = connection.execute(
            """
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                league_history_status,
                profile_observation_id, battle_log_observation_id, completed_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, 'complete', %s, %s,
                %s,
                CASE WHEN %s <> 'battle_log' THEN %s END,
                CASE WHEN %s = 'battle_log' THEN %s END,
                %s
            )
            RETURNING id
            """,
            (
                kind,
                lane,
                scope,
                player_id,
                normalized_tag,
                OLD,
                f"history:{observation_id}",
                profile_status,
                battle_log_status,
                league_history_status,
                endpoint,
                observation_id,
                endpoint,
                observation_id,
                OLD,
            ),
        ).fetchone()[0]
        connection.commit()
    return int(work_id)
