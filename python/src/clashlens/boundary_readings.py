"""A settled Reset reading on its Reset's first Daily board.

Owned by the board (``boundary``), which exposes it as
``boundary.reset_reading_settled``: the Reset reading job (``reset_baselines``)
judges the reading and hands the board only the fact that it settled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import boundary, boundary_members
from .db import Database
from .db_rows import text_value


def reset_reading_settled(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    reset_sweep_id: int,
    player_id: int,
    state: str,
) -> None:
    """Record one terminal reset result and reevaluate both artifacts."""
    boundary_at = boundary_at.astimezone(UTC)
    shared = boundary.lock_boundary_members(connection, boundary_at)
    sweep = connection.execute(
        """
        SELECT id, member_ids
        FROM collector_reset_sweeps
        WHERE id = %s AND boundary_at = %s
        """,
        (reset_sweep_id, boundary_at),
    ).fetchone()
    if sweep is None:
        return
    member_ids = [int(value) for value in (sweep[1] or [])]
    if player_id not in member_ids:
        return
    generation = connection.execute(
        """
        SELECT id, snapshot_state, army_state, correction_state,
               snapshot_manifest_id, army_manifest_id
        FROM boundary_publication_generations
        WHERE boundary_at = %s AND generation = 1
        """
        + ("" if shared else " FOR UPDATE"),
        (boundary_at,),
    ).fetchone()
    if shared:
        boundary.require_open_generation(generation and generation[1:], boundary_at)
    if generation is None:
        generation_id, _generation = boundary_members.create_generation(
            database,
            connection,
            boundary_at=boundary_at,
            sweep_id=reset_sweep_id,
            generation=1,
            supersedes_id=None,
        )
        generation = (generation_id, "pending", "pending", "none", None, None)
    # A frozen board keeps the member as it froze them; their day, saved
    # later, corrects it. On 8 November 2026 two Reset logs that waited in
    # the worker past the freeze put their players back to waiting on the
    # published board, and its army results then waited for them for good,
    # holding back the correction with their days.
    if generation[4] is not None or generation[5] is not None or (
        text_value(generation[1]) == "superseded"
        and text_value(generation[2]) == "superseded"
    ):
        return

    if state == "failed":
        snapshot_status = army_status = "unavailable"
    else:
        ranked_state = connection.execute(
            """
            SELECT ranked_day_version_id
            FROM boundary_publication_generation_members
            WHERE generation_id = %s AND player_id = %s
            FOR UPDATE
            """,
            (generation[0], player_id),
        ).fetchone()
        ranked_version_id = (
            int(ranked_state[0]) if ranked_state and ranked_state[0] is not None else None
        )
        snapshot_status = (
            boundary_members.snapshot_status(
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_version_id,
                boundary_at=boundary_at,
            )
            if ranked_version_id is not None
            else "pending"
        )
        army_status = (
            boundary_members.army_status(database,
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_version_id,
                snapshot_status=snapshot_status,
            )
            if ranked_version_id is not None
            else "pending"
        )
    boundary_members.write_member_result(
        connection, int(generation[0]), player_id, snapshot_status, army_status,
        status="unavailable" if snapshot_status == "unavailable" else None,
    )
    if not shared:
        boundary._try_enqueue_boundary_artifacts(database,
            connection, boundary_at=boundary_at, generation_id=int(generation[0])
        )
