"""Who is on a Reset's Daily board, by the weekly league record.

A board ranks the Legend day that ended at its Reset. Its members are the
players swept at the Reset, tracked as the record had it, unless the record
decided their ended day's week below Legend I or unknown, and the players
whose ended day's week it decided Legend I with that day's result saved
since, never one still Live, read at or before the Reset, as a board ranks
a player by their last reading before it, or with that day Complete, as a
Complete day proves its own total (``first_readings``), so a board never
waits for a player its Reset did not sweep (``board_members``). A Monday's
board ranks Sunday, whose week
was decided days earlier, so no board waits for a new week's decisions; a
swept player whose week is not decided yet stays a member, so a board never
loses its players while the record catches up. Before the record
(``league_week.RECORDED_FROM``) the members are the swept players, as they
always were.

A generation's members never change once taken (migration 0011). The first
generation takes them when it is created; each replacement takes them
again, keeping the results already recorded for those still members
(``create_generation``, ``copy_members``). So a player swept while below
Legend I or of unknown league is never waited for; a player whose day is
saved after the members were taken joins at the board's next rebuild
(``boundary._record_boundary_generation``); and a player whose week leaves
Legend I stops being waited for and ranks nowhere at once, and leaves the
member list at the next rebuild (``boundary.remove_league_members``, ``leagues``).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from . import league_week
from .analytics import FRESHNESS_RULE_VERSION, SNAPSHOT_ORDERING_RULE_VERSION
from .army_decoder import DECODER_VERSION
from .boundary_battles import moved_sides
from .db import Database
from .db_rows import text_value
from .domain import RANKED_DAY_DURATION
from .job_contracts import ANALYTICS_RULE_VERSION, ARMY_ANALYTICS_RULE_VERSION
from .league_week import week_start
from .publication_states import CorrectionState, day_snapshot_status, move_correction


def board_members(
    connection: Any, boundary_at: datetime, swept: list[int], only: int | None = None
) -> list[int]:
    """The board's members now, or with ``only`` whether that player is one:
    see the module notes."""
    day = boundary_at - RANKED_DAY_DURATION
    if only is not None:
        swept = [only] if only in swept else []
    if day < league_week.RECORDED_FROM:
        return sorted(set(swept))
    # A swept Legend I player is a member by the first half, so the second
    # reads the day and profile history only of the unswept, filtered first:
    # on the copy of 14 October 2026 that history was 78% of the 21,190 pages
    # read cold, under the Reset's full publication lock.
    return [int(row[0]) for row in connection.execute(
        """
        WITH unswept AS MATERIALIZED (
            SELECT week.player_id FROM player_league_weeks AS week
            WHERE week.week_start = %(week)s AND week.league = 'legend_i'
              {only}
              AND NOT EXISTS (
                  SELECT 1 FROM unnest(%(swept)s::bigint[]) AS member (player_id)
                  WHERE member.player_id = week.player_id))
        SELECT member.player_id FROM unnest(%(swept)s::bigint[]) AS member (player_id)
        LEFT JOIN player_league_weeks AS week
          ON week.player_id = member.player_id AND week.week_start = %(week)s
        WHERE week.league IS NULL OR week.league = 'legend_i'
        UNION
        SELECT week.player_id FROM unswept AS week
        JOIN players AS player ON player.id = week.player_id
        WHERE EXISTS (
              SELECT 1 FROM api_player_daily_logs AS day
              WHERE day.player_id = week.player_id AND day.ranked_day_start = %(day)s
                AND day.state <> 'Live')
          AND (EXISTS (
              SELECT 1 FROM player_profile_versions AS version
              WHERE version.normalized_tag = player.normalized_tag
                AND version.observed_at <= %(reset)s)
            OR (SELECT day.state FROM api_player_daily_logs AS day
                WHERE day.player_id = week.player_id AND day.ranked_day_start = %(day)s
                ORDER BY day.version DESC LIMIT 1) = 'Complete')
        ORDER BY 1
        """.format(only="" if only is None else "AND week.player_id = %(only)s"),
        {"week": week_start(day), "swept": swept, "day": day, "reset": boundary_at,
         "only": only},
    ).fetchall()]


# Whether the player was read in the ended day's week, from %(week)s, up to
# its Reset at %(reset)s, as a new profile or a sighting of an older one.
_READ_THIS_WEEK = """EXISTS (
    SELECT 1 FROM player_profile_versions AS version_read
    WHERE version_read.normalized_tag = {tag} AND version_read.observed_at <= %(reset)s
      AND (version_read.observed_at >= %(week)s OR EXISTS (
          SELECT 1 FROM player_profile_effects AS seen
          WHERE seen.profile_version_id = version_read.id
            AND seen.observed_at BETWEEN %(week)s AND %(reset)s)))"""


def first_readings(
    connection: Any, boundary_at: datetime, days: dict[int, int]
) -> dict[int, tuple[Any, ...]]:
    """Each player of ``days``, a player and their ranked day ending at the
    Reset, whose day is Complete and who was not read in its week before
    the Reset, with their first Legend I profile read after it, in any
    source state, as ``boundary_manifest`` reads a newest profile. On 27
    October 2026, 173 players promoted at the Monday Reset were read only
    from 05:01: on Monday the game still showed them in Legend II. Their
    Complete Mondays give their totals; the reading only names them."""
    if not days:
        return {}
    return {
        int(row[0]): row[1:]
        for row in connection.execute(
            f"""
            SELECT member.player_id, earliest.id, earliest.observation_id, earliest.observed_at,
                   earliest.profile_json, earliest.normalized_tag, earliest.name, earliest.trophies,
                   earliest.eligibility_state, earliest.current_league_season_id
            FROM unnest(%(players)s::bigint[], %(versions)s::bigint[])
                AS member (player_id, version_id)
            JOIN ranked_day_versions AS ranked
              ON ranked.id = member.version_id AND ranked.state = 'Complete'
            JOIN players AS player ON player.id = member.player_id
            CROSS JOIN LATERAL (
                SELECT id, observation_id, observed_at, profile_json, normalized_tag,
                       name, trophies, eligibility_state, current_league_season_id
                FROM player_profile_versions AS version
                WHERE version.normalized_tag = player.normalized_tag
                  AND version.observed_at > %(reset)s
                  AND version.eligibility_state = 'eligible'
                ORDER BY version.observed_at, version.id LIMIT 1
            ) AS earliest
            WHERE NOT {_READ_THIS_WEEK.format(tag="player.normalized_tag")}
            """,
            {"players": list(days), "versions": list(days.values()), "reset": boundary_at,
             "week": week_start(boundary_at - RANKED_DAY_DURATION)},
        ).fetchall()
    }


def joins_late(boundary_at: datetime) -> bool:
    """Whether a player whose ended day is saved after the board's members
    were taken joins it: from the record on, a saved day is in a Legend I
    week, so its player is a member."""
    return boundary_at - RANKED_DAY_DURATION >= league_week.RECORDED_FROM


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
    inherit_rules: bool = False,
    affected_artifacts: list[str] | None = None,
    correction_id: int | None = None,
) -> tuple[int, int]:
    """Create the board's generation ``generation`` with its members now,
    replacing ``supersedes_id`` when given, then apply ``pending_inputs``.
    The caller holds the Reset's full publication lock."""
    target_at = boundary_at + timedelta(
        minutes=10 if boundary_at.weekday() == 0 else 5
    )
    rules = (ordering_rule_version or SNAPSHOT_ORDERING_RULE_VERSION,
             FRESHNESS_RULE_VERSION, "active-members-v1", ANALYTICS_RULE_VERSION,
             ARMY_ANALYTICS_RULE_VERSION, "boundary-delay-v1", target_at)
    if inherit_rules:
        source = connection.execute(
            """SELECT ordering_rule_version, freshness_rule_version, membership_rule_version,
                      snapshot_rule_version, army_rule_version, target_rule, target_at
               FROM boundary_publication_generations WHERE id = %s FOR UPDATE""",
            (supersedes_id,),
        ).fetchone()
        assert source is not None
        rules = (ordering_rule_version or source[0], *source[1:])
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
            rules[0],
            rules[1],
            0,
            _population_hash([]),
            *rules[2:],
            supersedes_id,
            supersedes_id,
            "active" if supersedes_id is not None else "none",
            affected_artifacts if affected_artifacts is not None else (
                ["snapshot", "army"] if supersedes_id is not None else []),
        ),
    ).fetchone()
    assert row is not None
    generation_id = int(row[0])
    if correction_id is not None:
        move_correction(connection, correction_id, CorrectionState.INHERITANCE)
    copy_members(database, connection, generation_id, supersedes_id, boundary_at)
    for pending in pending_inputs or []:
        pending_version = pending.get("ranked_day_version_id") if isinstance(pending, dict) else None
        if pending_version is None:
            continue
        record_day(database, connection, generation_id=generation_id, boundary_at=boundary_at,
                   player_id=int(pending["player_id"]), ranked_day_version_id=int(pending_version),
                   input_hash=pending.get("input_hash"), settled=True)
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
    joined = [int(row[0]) for row in connection.execute(
        """
        INSERT INTO boundary_publication_generation_members (generation_id, player_id)
        SELECT %s, unnest(%s::bigint[])
        ON CONFLICT DO NOTHING
        RETURNING player_id
        """,
        (generation_id, player_ids),
    ).fetchall()]
    take_saved_days(database, connection, generation_id, boundary_at, joined)
    _recount(connection, generation_id)


def take_saved_days(
    database: Database, connection: Any, generation_id: int, boundary_at: datetime,
    player_ids: list[int] | None = None,
) -> None:
    """Give each member of ``generation_id`` waiting with no day, or each of
    ``player_ids``, the result of their saved day, when there is one. A
    rebuild's member whose day is saved after its members were taken gets
    no new result when the day comes out as it was before, which saves
    nothing new: on 22 October 2026 six rebuilds waited for 74 such players
    for good, so the publication check gives them their saved day too
    (``board_deadline.settle_waiting_members``)."""
    for player_id, version_id, input_hash in connection.execute(
        """
        SELECT member.player_id, saved.ranked_day_version_id, saved.input_hash
        FROM boundary_publication_generation_members AS member
        CROSS JOIN LATERAL (
            SELECT day.ranked_day_version_id, version.input_hash
            FROM api_player_daily_logs AS day
            JOIN ranked_day_versions AS version ON version.id = day.ranked_day_version_id
            WHERE day.player_id = member.player_id AND day.ranked_day_start = %(day)s
            ORDER BY day.version DESC LIMIT 1
        ) AS saved
        WHERE member.generation_id = %(generation)s AND member.status = 'pending'
          AND member.ranked_day_version_id IS NULL
          AND (%(players)s::bigint[] IS NULL OR member.player_id = ANY(%(players)s))
        ORDER BY member.player_id
        FOR UPDATE OF member
        """,
        {"day": boundary_at - RANKED_DAY_DURATION, "generation": generation_id,
         "players": player_ids},
    ).fetchall():
        record_day(database, connection, generation_id=generation_id, boundary_at=boundary_at,
                   player_id=int(player_id), ranked_day_version_id=int(version_id),
                   input_hash=text_value(input_hash))


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
    if day < league_week.RECORDED_FROM:
        return {}
    found = dict(connection.execute(
        """
        SELECT player_id, league FROM player_league_weeks
        WHERE player_id = ANY(%s::bigint[]) AND week_start = %s
        """,
        (player_ids, week_start(day)),
    ).fetchall())
    return {player_id: text_value(found.get(player_id, "unknown")) for player_id in player_ids}


def snapshot_status(
    connection: Any,
    *,
    player_id: int,
    ranked_day_version_id: int,
    boundary_at: datetime,
) -> str:
    ranked = connection.execute(
        "SELECT state FROM ranked_day_versions WHERE id = %s AND player_id = %s",
        (ranked_day_version_id, player_id),
    ).fetchone()
    status = day_snapshot_status(text_value(ranked[0]) if ranked else "")
    if status != "complete":
        return status
    # Whether any accepted reading is at or before the Reset: stop at the
    # first. Sorting the whole history for it read about 118 pages a member
    # cold on the copy of 14 October 2026, at every member result.
    profile = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM player_profile_versions AS profile
            LEFT JOIN player_profile_effects AS effect
              ON effect.profile_version_id = profile.id
            WHERE profile.player_id = %s
              AND profile.source_contract_state = 'accepted'
              AND COALESCE(effect.observed_at, profile.observed_at) <= %s)
        """,
        (player_id, boundary_at),
    ).fetchone()[0]
    # A player not read in the ended day's week before the Reset ranks by
    # their Complete day, named by their first Legend I reading since.
    return "complete" if profile or (
        first_readings(
            connection, boundary_at, {player_id: ranked_day_version_id})
    ) else "missing"


def army_status(
    database: Database,
    connection: Any,
    *,
    player_id: int,
    ranked_day_version_id: int,
    snapshot_status: str,
) -> str:
    if snapshot_status in {"pending", "unavailable", "missing", "failed"}:
        return (
            "unavailable"
            if snapshot_status in {"unavailable", "missing", "failed"}
            else "pending"
        )
    daily_log = connection.execute(
        """
        SELECT battles, state, coverage
        FROM api_player_daily_logs
        WHERE player_id = %s AND ranked_day_version_id = %s
        ORDER BY version DESC LIMIT 1
        """,
        (player_id, ranked_day_version_id),
    ).fetchone()
    # Publication requires daily-log evidence before army readiness.
    if daily_log is None:
        return "pending"
    daily_state = text_value(daily_log[1])
    daily_coverage = text_value(daily_log[2])
    if daily_state == "Partial":
        return "partial"
    if daily_state != "Complete" or daily_coverage != "complete":
        return "pending"
    sides = [
        (int(event["battle_id"]), event.get("lens"))
        for event in (daily_log[0] if isinstance(daily_log[0], list) else [])
        if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
    ]
    if not sides:
        return "complete"
    moved = moved_sides(connection, sides)
    battle_ids = [moved.get(side, side[0]) for side in sides]
    decoded = connection.execute(
        """
        SELECT count(DISTINCT battle_id)
        FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[]) AND is_active AND decoder_version = %s
        """,
        (battle_ids, DECODER_VERSION),
    ).fetchone()
    return (
        snapshot_status
        if decoded is not None and int(decoded[0]) == len(set(battle_ids))
        else "pending"
    )


def record_day(
    database: Database, connection: Any, *, generation_id: int,
    boundary_at: datetime, player_id: int, ranked_day_version_id: int,
    input_hash: str | None, settled: bool = False, insert_missing: bool = False,
) -> None:
    """Apply a saved day and its readiness to a generation member.

    Pending correction inputs are already settled before activation. An
    arriving day may create a late member; captured correction inputs and
    saved-day checks only update members already selected for that generation.
    """
    snapshot = snapshot_status(connection, player_id=player_id,
                               ranked_day_version_id=ranked_day_version_id,
                               boundary_at=boundary_at)
    army = army_status(database, connection, player_id=player_id,
                       ranked_day_version_id=ranked_day_version_id, snapshot_status=snapshot)
    write_member_result(connection, generation_id, player_id, snapshot, army,
                        day=(ranked_day_version_id, input_hash),
                        status="terminal" if settled else None, insert_missing=insert_missing)


def write_member_result(
    connection: Any, generation_id: int, player_id: int, snapshot: str, army: str,
    *, day: tuple[int, str | None] | None = None, status: str | None = None,
    insert_missing: bool = False,
) -> None:
    """Write member readiness, optionally replacing its saved-day identity."""
    status = status or ("terminal" if snapshot != "pending" else "pending")
    assignments = "status = %s, snapshot_status = %s, army_status = %s"
    values = [status, snapshot, army]
    if day is not None:
        assignments = "ranked_day_version_id = %s, ranked_day_input_hash = %s, " + assignments
        values = [*day, *values]
    updated = connection.execute(
        f"""
        UPDATE boundary_publication_generation_members
        SET {assignments}, updated_at = clock_timestamp()
        WHERE generation_id = %s AND player_id = %s
        """, (*values, generation_id, player_id),
    )
    if insert_missing and updated.rowcount == 0:
        assert day is not None
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (generation_id, player_id, *day, status, snapshot, army),
        )


def inherit_artifact(
    connection: Any, generation_id: int, source_id: int, artifact: str,
    *, published: bool = False, touch: bool = False,
) -> None:
    """Keep an unaffected artifact's immutable identity in a successor."""
    columns = {
        "snapshot": ("snapshot_id", "snapshot_input_hash", "snapshot_manifest_id",
                     "snapshot_analytics_publication_id", "snapshot_coverage"),
        "army": ("army_input_hash", "army_manifest_id", "army_publication_id", "army_coverage"),
    }[artifact]
    state = "'published'" if published else f"source.{artifact}_state"
    assignments = [f"{artifact}_state = {state}"] + [f"{c} = source.{c}" for c in columns]
    if touch:
        assignments.append("updated_at = clock_timestamp()")
    connection.execute(
        f"""
        UPDATE boundary_publication_generations AS target
        SET {", ".join(assignments)}
        FROM boundary_publication_generations AS source
        WHERE target.id = %s AND source.id = %s
        """, (generation_id, source_id),
    )
