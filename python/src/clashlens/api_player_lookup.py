from __future__ import annotations

from typing import Any

from . import api_db
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text


def _lookup(connection: Any, tag: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT player.active, player.eligibility_state,
               EXISTS (SELECT 1 FROM player_profile_versions AS profile
                       WHERE profile.player_id = player.id),
               work.status, work.failure_category, processing.status
        FROM players AS player
        LEFT JOIN LATERAL (
            SELECT status, failure_category, profile_observation_id
            FROM collector_work
            WHERE player_id = player.id
              AND kind IN ('initial_collection', 'live_refresh', 'discovery_profile')
            ORDER BY id DESC LIMIT 1
        ) AS work ON true
        LEFT JOIN LATERAL (
            SELECT status FROM python_processing_jobs
            WHERE observation_id = work.profile_observation_id
              AND work_type = 'process_observation'
            ORDER BY id DESC LIMIT 1
        ) AS processing ON true
        WHERE player.normalized_tag = %s
        """,
        (tag,),
    ).fetchone()
    state = "unknown"
    if row is not None:
        active, eligibility, confirmed, work, failure, processing = row
        if active:
            state = "tracking"
        elif confirmed:
            state = (
                "not_in_legend" if _text(eligibility) == "ineligible" else "uncertain"
            )
        elif work in ("pending", "waiting_retry"):
            state = "checking"
        elif failure == "player_not_found":
            state = "not_found"
        elif work in ("failed", "cancelled") or processing in ("failed", "complete"):
            state = "failed"
        elif work == "complete":
            state = "checking"
    return {"tag": tag, "state": state}


def get_lookup(database: ApiDatabase, tag: str) -> dict[str, Any]:
    with database.pool.connection() as connection:
        return _lookup(connection, tag)


def submit_lookup(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            # The existing enqueue function takes this same tag lock. Serialize
            # the evidence read with admission so simultaneous visits reuse work.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (normalized_tag,),
            )
            lookup = _lookup(connection, normalized_tag)
            if lookup["state"] in {"unknown", "failed", "not_found"}:
                # Failed and negative checks have the same minimum retry interval
                # as Refresh. Known real players are never rechecked by a visit.
                recent = connection.execute(
                    """
                    SELECT 1 FROM collector_work
                    WHERE normalized_tag = %s
                      AND kind IN ('initial_collection', 'live_refresh', 'discovery_profile')
                      AND updated_at > clock_timestamp() - interval '30 seconds'
                    LIMIT 1
                    """,
                    (normalized_tag,),
                ).fetchone()
                if recent is None:
                    connection.execute(
                        "SELECT * FROM clashlens_enqueue_interactive('initial_collection', %s, 30)",
                        (normalized_tag,),
                    ).fetchone()
                    lookup = _lookup(connection, normalized_tag)
            result = OperationResult(200, lookup)
            api_db._complete_request(connection, binding.request_id, result)
            return result
