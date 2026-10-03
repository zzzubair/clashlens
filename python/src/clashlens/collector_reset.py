"""Reset collection work: the 05:00 UTC sweep and its delayed settlement check.

At each Reset the sweep freezes the active members, then schedules two
pieces of work for each of them in one transaction: the Reset pair, due at
once, and one settlement check, due 20 minutes later. The Reset pair holds
ordinary collection until it finishes; the settlement check never does. It
fetches a fresh profile, then the battle log that must cover it, and stops
making requests 23h55m after the Reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .domain import is_season_boundary

SETTLEMENT_DELAY = timedelta(minutes=20)
COLLECTION_WINDOW = timedelta(hours=23, minutes=55)


def begin_reset(connection: Any, boundary_at: datetime) -> int | None:
    """Freeze the Reset's members once and schedule their work; return the sweep."""
    utc_boundary = boundary_at.astimezone(UTC)
    if (
        utc_boundary.hour,
        utc_boundary.minute,
        utc_boundary.second,
        utc_boundary.microsecond,
    ) != (5, 0, 0, 0):
        raise ValueError("Reset boundary must be 05:00 UTC")
    # A season-ending Reset adds one league-history fetch per member.
    league_history_status = (
        "pending" if is_season_boundary(utc_boundary) else "not_applicable"
    )
    with connection.transaction():
        older_boundary = connection.execute(
            """
            SELECT sweep.boundary_at
            FROM collector_reset_sweeps AS sweep
            WHERE sweep.boundary_at < %s
              AND EXISTS (
                  SELECT 1 FROM collector_work AS work
                  WHERE work.sweep_id = sweep.id
                    AND work.kind = 'reset_baseline'
                    AND work.status NOT IN ('complete', 'failed', 'cancelled')
              )
            ORDER BY sweep.boundary_at
            LIMIT 1
            FOR UPDATE
            """,
            (utc_boundary,),
        ).fetchone()
        if older_boundary is not None:
            return None
        sweep_row = connection.execute(
            """
            INSERT INTO collector_reset_sweeps (boundary_at)
            VALUES (%s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (utc_boundary,),
        ).fetchone()
        first_capture = sweep_row is not None
        if sweep_row is None:
            sweep_row = connection.execute(
                "SELECT id FROM collector_reset_sweeps WHERE boundary_at = %s FOR UPDATE",
                (utc_boundary,),
            ).fetchone()
        assert sweep_row is not None
        sweep_id = int(sweep_row[0])
        if first_capture:
            member_ids = connection.execute(
                "SELECT COALESCE(array_agg(id ORDER BY id), '{}'::bigint[]) FROM players WHERE active = true"
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE collector_reset_sweeps
                SET member_ids = %s,
                    membership_captured_at = clock_timestamp()
                WHERE id = %s
                """,
                (member_ids, sweep_id),
            )
            # Only the sweep's first capture schedules settlement checks, so a
            # sweep an older release captured never gets them hours late.
            _schedule_settlement_checks(connection, sweep_id, utc_boundary, member_ids)
        else:
            member_ids = connection.execute(
                "SELECT member_ids FROM collector_reset_sweeps WHERE id = %s FOR UPDATE",
                (sweep_id,),
            ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at,
                coalescing_key, sweep_id, profile_status,
                battle_log_status, league_history_status
            )
            SELECT 'reset_baseline', 'reset', 'player', player.id,
                   player.normalized_tag, %s,
                   'reset:' || %s || ':' || player.id, %s, 'pending',
                   'pending', %s
            FROM unnest(%s::bigint[]) AS member(player_id)
            JOIN players AS player ON player.id = member.player_id
            WHERE NOT EXISTS (
                SELECT 1 FROM collector_work AS existing
                WHERE existing.coalescing_key = 'reset:' || %s || ':' || player.id
            )
            """,
            (
                utc_boundary,
                sweep_id,
                sweep_id,
                league_history_status,
                member_ids,
                sweep_id,
            ),
        )
    return sweep_id


def _schedule_settlement_checks(
    connection: Any, sweep_id: int, boundary_at: datetime, member_ids: list[int]
) -> None:
    """Add one settlement check per frozen member and link its boundary row."""
    connection.execute(
        """
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at,
            coalescing_key, sweep_id, profile_status,
            battle_log_status, league_history_status
        )
        SELECT 'reset_settlement', 'ordinary', 'player', player.id,
               player.normalized_tag, %(due)s,
               'reset-settlement:' || %(sweep)s || ':' || player.id, %(sweep)s,
               'pending', 'pending', 'not_applicable'
        FROM unnest(%(members)s::bigint[]) AS member(player_id)
        JOIN players AS player ON player.id = member.player_id
        ON CONFLICT DO NOTHING
        """,
        {"due": boundary_at + SETTLEMENT_DELAY, "sweep": sweep_id, "members": member_ids},
    )
    connection.execute(
        """
        INSERT INTO reset_boundary_settlements (
            player_id, boundary_at, sweep_id, delayed_work_id
        )
        SELECT work.player_id, %s, %s, work.id
        FROM collector_work AS work
        WHERE work.sweep_id = %s AND work.kind = 'reset_settlement'
        ON CONFLICT (player_id, boundary_at) DO UPDATE
        SET delayed_work_id = EXCLUDED.delayed_work_id
        WHERE reset_boundary_settlements.delayed_work_id IS NULL
        """,
        (boundary_at, sweep_id, sweep_id),
    )


def reset_ready(connection: Any, sweep_id: int) -> bool:
    """Whether every Reset pair of the sweep has finished; settlement checks never count."""
    if sweep_id < 1:
        raise ValueError("Reset sweep ID must be positive")
    return connection.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM collector_work WHERE sweep_id = %s AND kind = 'reset_baseline' AND status NOT IN ('complete', 'failed', 'cancelled'))",
        (sweep_id,),
    ).fetchone()[0]


def expire_settlement_checks(
    connection: Any, now: datetime, *, batch: int = 1000
) -> int:
    """Fail up to ``batch`` unfinished checks whose window closed by ``now``.

    The collector's scheduling loop calls this from 04:55 until none
    expire, so each Reset's checks expire before the next Reset without a
    request. Responses already saved keep their work
    reference and are still processed.
    """
    with connection.transaction():
        return connection.execute(
            """
            UPDATE collector_work AS work
            SET status = 'failed', failure_category = 'settlement_expired',
                failure_detail = 'no complete check within 23h55m of the Reset',
                updated_at = clock_timestamp()
            WHERE work.id IN (
                SELECT unfinished.id
                FROM collector_work AS unfinished
                JOIN collector_reset_sweeps AS sweep
                  ON sweep.id = unfinished.sweep_id
                WHERE unfinished.lane = 'ordinary'
                  AND unfinished.status IN ('pending', 'waiting_retry')
                  AND unfinished.kind = 'reset_settlement'
                  AND sweep.boundary_at + %s <= %s
                ORDER BY unfinished.id
                LIMIT %s
                FOR UPDATE OF unfinished
            )
            """,
            (COLLECTION_WINDOW, now, batch),
        ).rowcount
