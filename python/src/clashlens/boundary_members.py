"""Who is on a Reset's Daily board, by the weekly league record.

A board ranks the Legend day that ended at its Reset. Its members are the
players swept at the Reset, tracked as the record had it, unless the record
decided their ended day's week below Legend I or unknown, and the players
whose ended day's week it decided Legend I with that day's result saved
since, never one still Live, so a board never waits for a player its Reset
did not sweep (``board_members``). A Monday's board ranks Sunday, whose week
was decided days earlier, so no board waits for a new week's decisions; a
swept player whose week is not decided yet stays a member, so a board never
loses its players while the record catches up. Before the record
(``league_days.RECORDED_FROM``) the members are the swept players, as they
always were.

A generation's members never change once taken (migration 0011). The first
generation takes them when it is created; each replacement takes them
again, keeping the results already recorded for those still members
(``create_generation``, ``copy_members``). So a player swept while below
Legend I or of unknown league is never waited for; a player whose day is
saved after the members were taken joins at the board's next rebuild
(``boundary._record_boundary_generation``); and a player whose week leaves
Legend I stops being waited for and ranks nowhere at once, and leaves the
member list at the next rebuild (``leave``, ``leagues``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from . import league_days
from .analytics import FRESHNESS_RULE_VERSION, SNAPSHOT_ORDERING_RULE_VERSION
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    RESET_LOCK_WAIT,
    Database,
    _text_value,
)
from .domain import RANKED_DAY_DURATION
from .league_week import week_start


def board_members(connection: Any, boundary_at: datetime, swept: list[int]) -> list[int]:
    """The board's members now: see the module notes."""
    day = boundary_at - RANKED_DAY_DURATION
    if day < league_days.RECORDED_FROM:
        return sorted(set(swept))
    return [int(row[0]) for row in connection.execute(
        """
        SELECT member.player_id FROM unnest(%(swept)s::bigint[]) AS member (player_id)
        LEFT JOIN player_league_weeks AS week
          ON week.player_id = member.player_id AND week.week_start = %(week)s
        WHERE week.league IS NULL OR week.league = 'legend_i'
        UNION
        SELECT week.player_id FROM player_league_weeks AS week
        WHERE week.week_start = %(week)s AND week.league = 'legend_i'
          AND EXISTS (
              SELECT 1 FROM api_player_daily_logs AS day
              WHERE day.player_id = week.player_id AND day.ranked_day_start = %(day)s
                AND day.state <> 'Live')
        ORDER BY 1
        """,
        {"week": week_start(day), "swept": swept, "day": day},
    ).fetchall()]


def joins_late(boundary_at: datetime) -> bool:
    """Whether a player whose ended day is saved after the board's members
    were taken joins it: from the record on, a saved day is in a Legend I
    week, so its player is a member."""
    return boundary_at - RANKED_DAY_DURATION >= league_days.RECORDED_FROM


def _population_hash(player_ids: list[int]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(player_ids), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _swept(connection: Any, sweep_id: int) -> list[int]:
    row = connection.execute(
        "SELECT member_ids FROM collector_reset_sweeps WHERE id = %s", (sweep_id,)
    ).fetchone()
    return [int(value) for value in ((row[0] if row else None) or [])]


def create_generation(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    sweep_id: int,
    generation: int,
    supersedes_id: int | None,
    pending_inputs: list[dict[str, Any]] | None = None,
    ordering_rule_version: str | None = None,
) -> tuple[int, int]:
    """Create the board's generation ``generation`` with its members now,
    replacing ``supersedes_id`` when given, then apply ``pending_inputs``.
    The caller holds the Reset's full publication lock."""
    from .boundary import _boundary_army_status, _boundary_snapshot_status

    target_at = boundary_at + timedelta(
        minutes=10 if boundary_at.weekday() == 0 else 5
    )
    row = connection.execute(
        """
        INSERT INTO boundary_publication_generations (
            boundary_at, generation, sweep_id, ordering_rule_version,
            freshness_rule_version, expected_population_count,
            expected_population_hash, membership_rule_version,
            snapshot_rule_version, army_rule_version, target_rule, target_at,
            supersedes_id, source_generation_id,
            correction_state, affected_artifacts
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, generation
        """,
        (
            boundary_at,
            generation,
            sweep_id,
            ordering_rule_version or SNAPSHOT_ORDERING_RULE_VERSION,
            FRESHNESS_RULE_VERSION,
            0,
            _population_hash([]),
            "active-members-v1",
            ANALYTICS_RULE_VERSION,
            ARMY_ANALYTICS_RULE_VERSION,
            "boundary-delay-v1",
            target_at,
            supersedes_id,
            supersedes_id,
            "active" if supersedes_id is not None else "none",
            ["snapshot", "army"] if supersedes_id is not None else [],
        ),
    ).fetchone()
    assert row is not None
    generation_id = int(row[0])
    copy_members(database, connection, generation_id, supersedes_id, boundary_at)
    for pending in pending_inputs or []:
        pending_version = pending.get("ranked_day_version_id")
        pending_snapshot_status = _boundary_snapshot_status(
            connection,
            player_id=int(pending["player_id"]),
            ranked_day_version_id=int(pending_version),
            boundary_at=boundary_at,
        )
        pending_army_status = _boundary_army_status(
            database,
            connection,
            player_id=int(pending["player_id"]),
            ranked_day_version_id=int(pending_version),
            snapshot_status=pending_snapshot_status,
        )
        connection.execute(
            """
            UPDATE boundary_publication_generation_members
            SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                status = 'terminal', snapshot_status = %s,
                army_status = %s, updated_at = clock_timestamp()
            WHERE generation_id = %s AND player_id = %s
            """,
            (
                pending_version,
                pending.get("input_hash"),
                pending_snapshot_status,
                pending_army_status,
                generation_id,
                pending.get("player_id"),
            ),
        )
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET membership_captured_at = COALESCE(membership_captured_at, clock_timestamp())
        WHERE id = %s
        """,
        (generation_id,),
    )
    return generation_id, int(row[1])


def copy_members(
    database: Database, connection: Any, generation_id: int, source_id: int | None,
    boundary_at: datetime,
) -> None:
    """Give ``generation_id``, before its members are captured, the board's
    members now. Replacing ``source_id``, each one still a member keeps its
    recorded result; in a first generation each swept one waits for theirs.
    Each other has the result of their saved day, or waits for it."""
    from .boundary import _boundary_army_status, _boundary_snapshot_status

    sweep_id = connection.execute(
        "SELECT sweep_id FROM boundary_publication_generations WHERE id = %s", (generation_id,)
    ).fetchone()[0]
    swept = _swept(connection, int(sweep_id))
    player_ids = board_members(connection, boundary_at, swept)
    if source_id is None:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (generation_id, player_id)
            SELECT %s, unnest(%s::bigint[])
            """,
            (generation_id, sorted(set(player_ids) & set(swept))),
        )
    connection.execute(
        """
        INSERT INTO boundary_publication_generation_members (
            generation_id, player_id, ranked_day_version_id,
            ranked_day_input_hash, status, snapshot_status, army_status
        )
        SELECT %s, player_id, ranked_day_version_id,
               ranked_day_input_hash, status, snapshot_status, army_status
        FROM boundary_publication_generation_members
        WHERE generation_id = %s AND player_id = ANY(%s::bigint[])
        ON CONFLICT DO NOTHING
        """,
        (generation_id, source_id or 0, player_ids),
    )
    day = boundary_at - RANKED_DAY_DURATION
    for player_id, version_id, input_hash in connection.execute(
        """
        SELECT member.player_id, saved.ranked_day_version_id, saved.input_hash
        FROM unnest(%s::bigint[]) AS member (player_id)
        LEFT JOIN LATERAL (
            SELECT day.ranked_day_version_id, version.input_hash
            FROM api_player_daily_logs AS day
            JOIN ranked_day_versions AS version ON version.id = day.ranked_day_version_id
            WHERE day.player_id = member.player_id AND day.ranked_day_start = %s
            ORDER BY day.version DESC LIMIT 1
        ) AS saved ON true
        WHERE NOT EXISTS (
            SELECT 1 FROM boundary_publication_generation_members AS kept
            WHERE kept.generation_id = %s AND kept.player_id = member.player_id
        )
        """,
        (player_ids, day, generation_id),
    ).fetchall():
        snapshot_status = army_status = "pending"
        if version_id is not None:
            snapshot_status = _boundary_snapshot_status(
                connection, player_id=int(player_id),
                ranked_day_version_id=int(version_id), boundary_at=boundary_at,
            )
            army_status = _boundary_army_status(
                database, connection, player_id=int(player_id),
                ranked_day_version_id=int(version_id), snapshot_status=snapshot_status,
            )
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (generation_id, player_id, version_id, _text_value(input_hash) if input_hash else None,
             "terminal" if snapshot_status != "pending" else "pending",
             snapshot_status, army_status),
        )
    _recount(connection, generation_id)


def _recount(connection: Any, generation_id: int) -> None:
    """Set a generation's expected members to those it holds, before its
    membership is captured: then they never change."""
    player_ids = [int(row[0]) for row in connection.execute(
        "SELECT player_id FROM boundary_publication_generation_members WHERE generation_id = %s",
        (generation_id,),
    ).fetchall()]
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET expected_population_count = %s, expected_population_hash = %s,
            updated_at = clock_timestamp()
        WHERE id = %s
        """,
        (len(player_ids), _population_hash(player_ids), generation_id),
    )


def leagues(connection: Any, boundary_at: datetime, player_ids: list[int]) -> dict[int, str]:
    """Each player's league in the week of the board's ended day, by the
    record; empty before it. A member whose week left Legend I after the
    members were taken ranks on no board (``boundary_manifest``)."""
    day = boundary_at - RANKED_DAY_DURATION
    if day < league_days.RECORDED_FROM:
        return {}
    found = dict(connection.execute(
        """
        SELECT player_id, league FROM player_league_weeks
        WHERE player_id = ANY(%s::bigint[]) AND week_start = %s
        """,
        (player_ids, week_start(day)),
    ).fetchall())
    return {player_id: _text_value(found.get(player_id, "unknown")) for player_id in player_ids}


def leave(database: Database, connection: Any, player_id: int, days: list[datetime]) -> None:
    """The player's week holding ``days`` left Legend I: take them off the
    board of each Reset ending one where they are a member. A generation's
    members never change, so a board not frozen yet stops waiting for their
    day and ranks them nowhere (``leagues``), and a frozen one gets a queued
    correction, whose replacement takes the members again. The caller holds
    the player's week rows. The Reset's lock is taken as a day result takes
    it (``boundary.lock_boundary_members``), shared while many members wait,
    so this never waits behind them; a Reset busy past ``RESET_LOCK_WAIT``,
    as while its board freezes, raises LockNotAvailable, and the decider
    decides the batch again a minute later (``league_week.decide_due``)."""
    from .boundary import _try_enqueue_boundary_artifacts, lock_boundary_members

    # The Reset's newest generation, and the player's member row in it.
    newest = """
        SELECT generation.id, generation.snapshot_manifest_id,
               generation.army_manifest_id, member.status
        FROM boundary_publication_generations AS generation
        LEFT JOIN boundary_publication_generation_members AS member
          ON member.generation_id = generation.id AND member.player_id = %s
        WHERE generation.boundary_at = %s AND generation.snapshot_state <> 'superseded'
          AND generation.army_state <> 'superseded'
        ORDER BY generation.generation DESC LIMIT 1
    """
    for day in sorted(set(days)):
        boundary_at = day + RANKED_DAY_DURATION
        current = connection.execute(newest, (player_id, boundary_at)).fetchone()
        if current is None or current[3] is None:
            continue
        shared = lock_boundary_members(connection, boundary_at, RESET_LOCK_WAIT)
        current = connection.execute(
            newest + ("" if shared else " FOR UPDATE OF generation"), (player_id, boundary_at)
        ).fetchone()
        if current is None or current[3] is None:
            continue
        if current[1] is None and current[2] is None:
            if _text_value(current[3]) == "pending":
                connection.execute(
                    """
                    UPDATE boundary_publication_generation_members
                    SET status = 'terminal', snapshot_status = 'unavailable',
                        army_status = 'unavailable', updated_at = clock_timestamp()
                    WHERE generation_id = %s AND player_id = %s
                    """,
                    (current[0], player_id),
                )
            if not shared:
                _try_enqueue_boundary_artifacts(
                    database, connection, boundary_at=boundary_at, generation_id=int(current[0]))
            continue
        queued = connection.execute(
            """
            SELECT id FROM boundary_publication_corrections
            WHERE boundary_at = %s AND source_generation_id = %s
              AND state IN ('queued', 'pending_inputs')
            ORDER BY id DESC LIMIT 1
            FOR UPDATE
            """,
            (boundary_at, current[0]),
        ).fetchone()
        if queued is not None:
            connection.execute(
                """
                UPDATE boundary_publication_corrections
                SET affected_artifacts = ARRAY(SELECT DISTINCT unnest(
                        affected_artifacts || ARRAY['snapshot', 'army']))
                WHERE id = %s
                """,
                (queued[0],),
            )
        else:
            connection.execute(
                """
                INSERT INTO boundary_publication_corrections
                    (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
                VALUES (%s, %s, ARRAY['snapshot', 'army'], '[]'::jsonb)
                """,
                (boundary_at, current[0]),
            )
