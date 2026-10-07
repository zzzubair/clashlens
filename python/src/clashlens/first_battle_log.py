"""What a newly tracked player's first saved battle log proves.

A player first tracked after a Reset has no reading from it. Their first
battle log still holds every battle back to its oldest row, each with its own
time, so it can stand in for the missing Reset battle log, and the battles it
holds from earlier days of the Season give those days a result too. Days
built this way keep every usual day rule; a start from the Season rule stays
"inferred", never "exact".
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import battle, domain, reset_baselines
from .battle import ParsedBattleRow
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    PYTHON_BACKFILL_PRIORITY,
    Database,
    _text_value,
    ended_day_priority,
)
from .domain import SEASON_START_TROPHIES, RankedDay
from .ranked_day_inputs import _source_rows
from .reconciliation import (
    BATTLE_LOG_MAX_ROWS,
    MAX_DAILY_DEFENSES,
    RECONCILIATION_RULE_VERSION,
)


def _first_log(connection: Any, player_id: int) -> tuple | None:
    """The player's earliest saved battle log: id, observation id, time saved,
    row count and parser version."""
    return connection.execute(
        """
        SELECT id, observation_id, observed_at, row_count, parser_version
        FROM battle_log_observations
        WHERE player_id = %s
        ORDER BY observed_at, id
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()


def coverage_start(
    database: Database, connection: Any, player_id: int, ranked_day: RankedDay
) -> tuple[int, bool] | None:
    """The player's first saved battle log, when saved on or after the day's
    start and holding every battle of the day up to when it was saved: its
    oldest row is from before the day's battles, or it has fewer than 50
    rows, the whole log the game keeps. Also whether it was saved after the
    day's last battle, so it holds the whole day."""
    first = _first_log(connection, player_id)
    if first is None or first[2] < ranked_day.start:
        return None
    window_start, window_end = domain.battle_window(ranked_day.start)
    if int(first[3]) >= BATTLE_LOG_MAX_ROWS:
        relation, _, _ = _source_rows(database)
        parser_version = _text_value(first[4])
        times = []
        for (source,) in connection.execute(
            f"SELECT source_json FROM {relation} WHERE battle_log_observation_id = %s",
            (first[0],),
        ).fetchall():
            try:
                times.append(battle._parse_battle_timestamp(
                    battle._battle_timestamp_value(source, parser_version),
                    parser_version,
                ))
            except (AttributeError, battle.BattleLogParseError):
                continue
        if not times or min(times) >= window_start:
            return None
    return int(first[1]), first[2] >= window_end


def season_rule_start(
    connection: Any, player_id: int, ranked_day: RankedDay, *, complete: bool
) -> dict[str, Any] | None:
    """Day 1's start of 5,000 by the Season rule, for a player with no saved
    reading from the Season-opening Reset, once they have an accepted
    Legend I profile naming the Season. ``complete`` is whether a battle log
    proves the day's battles from the Reset."""
    if not domain.is_season_boundary(ranked_day.start) or not (
        reset_baselines._season_rule_holds(connection, player_id, ranked_day.start)
    ):
        return None
    return {
        "id": None,
        "version": None,
        "state": "season_rule",
        "complete": complete,
        "trophies": SEASON_START_TROPHIES,
        "eligibility_state": "eligible",
        "evidence": {
            "start_trophies_source": "season_rule",
            "reset_reading": None,
            "battle_log_observation_id": None,
        },
    }


def _queue(
    connection: Any, player_id: int, day_start: datetime, observation_id: int | None,
    *, key: str | None = None, trigger: str = "first_battle_log",
    later_days: bool = True, priority: int | None = None,
) -> int | None:
    """Queue the recalculation of one day and, with ``later_days``, every
    saved later day of its Season, by default from the player's earliest
    saved battle log, ``observation_id``; ``None`` when it was already
    queued. Without ``priority``, the day's own: Reset priority while its
    Reset is the latest, else live."""
    day = domain.ranked_day_for(day_start)
    day_text = day.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version, priority
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
            %s, %s, %s, %s, %s
        )
        ON CONFLICT (deduplication_key) DO NOTHING
        RETURNING id
        """,
        (
            key or f"reconcile:first-log:{player_id}:{day_text}:{observation_id}",
            Jsonb({
                "player_id": int(player_id),
                "ranked_day_start": day_text,
                **({
                    "last_ranked_day_start": day_text,
                    "recalculate_season": day.official_season_id,
                } if later_days else {}),
                "trigger": trigger,
            }),
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
            ended_day_priority(day.start) if priority is None else priority,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else None


def queue_earlier_days(
    connection: Any,
    player_id: int,
    observed_at: datetime,
    rows: list[ParsedBattleRow],
) -> None:
    """When a player's earliest saved battle log, even one processed after
    newer ones, was saved on Day 1, or holds their battles from an earlier
    day of the Season it was saved in, recalculate from that day."""
    saved_day = domain.ranked_day_for(observed_at)
    earlier = [
        row.battle.ranked_day_start
        for row in rows
        if row.battle is not None
        and saved_day.season_start <= row.battle.ranked_day_start < saved_day.start
    ]
    if saved_day.start == saved_day.season_start:
        earlier = [saved_day.start]
    if not earlier:
        return
    first = _first_log(connection, player_id)
    if first[2] < observed_at:
        return
    day = min(earlier)
    if domain.is_season_boundary(day):
        connection.execute(
            "SELECT id FROM players WHERE id = %s FOR NO KEY UPDATE", (player_id,)
        )
        if not reset_baselines._season_rule_holds(connection, player_id, day):
            return
    _queue(connection, player_id, day, int(first[1]))


def queue_day_1(connection: Any, player_id: int, profile_version_id: int) -> None:
    """When a player's first accepted Legend I profile naming a Season is
    saved after their first battle log of that Season, recalculate from Day 1
    as ``backfill`` would, now that the Season rule can start it. Also
    check whether it proves a late sign-up for the Season."""
    row = connection.execute(
        """
        SELECT profile.current_league_season_id
        FROM player_profile_versions AS profile
        WHERE profile.id = %s
          AND profile.eligibility_state = 'eligible'
          AND profile.source_contract_state = 'accepted'
          AND NOT EXISTS (
              SELECT 1 FROM player_profile_versions AS other
              WHERE other.player_id = profile.player_id
                AND other.id <> profile.id
                AND other.current_league_season_id
                    = profile.current_league_season_id
                AND other.eligibility_state = 'eligible'
                AND other.source_contract_state = 'accepted'
          )
        """,
        (profile_version_id,),
    ).fetchone()
    if row is not None:
        queue_not_enrolled(
            connection, player_id, datetime.fromtimestamp(int(row[0]), UTC)
        )
    first = _first_log(connection, player_id)
    if row is None or first is None:
        return
    first_day = domain.ranked_day_for(first[2])
    if first_day.official_season_id != _text_value(row[0]):
        return
    if first_day.start == first_day.season_start or connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM legend_battles
            WHERE ranked_day_start = %(day)s
              AND (attacker_player_id = %(player)s OR defender_player_id = %(player)s)
        )
        """,
        {"day": first_day.season_start, "player": player_id},
    ).fetchone()[0]:
        _queue(connection, player_id, first_day.season_start, int(first[1]))


def queue_not_enrolled(connection: Any, player_id: int, observed_at: datetime) -> None:
    """Once saved profiles prove a late sign-up for the Season of
    ``observed_at``, recalculate each day of it that ended before a confirmed
    Legend I profile still showed no Season, Season ID 0, followed by a
    profile showing the sign-up, so it can be shown as not enrolled. Either
    profile may be processed last."""
    day = domain.ranked_day_for(observed_at)
    latest = connection.execute(
        """
        SELECT max(waiting_seen.observed_at)
        FROM player_profile_versions AS waiting
        JOIN player_profile_effects AS waiting_seen
          ON waiting_seen.profile_version_id = waiting.id
        WHERE waiting.player_id = %(player)s
          AND waiting.eligibility_state = 'eligible'
          AND waiting.eligibility_reason = 'confirmed_legend_i'
          AND waiting.current_league_season_id = '0'
          AND waiting_seen.observed_at >= %(start)s
          AND EXISTS (
              SELECT 1
              FROM player_profile_versions AS joined
              JOIN player_profile_effects AS joined_seen
                ON joined_seen.profile_version_id = joined.id
              WHERE joined.player_id = waiting.player_id
                AND joined.current_league_season_id = %(season)s
                AND joined.eligibility_state = 'eligible'
                AND joined.source_contract_state = 'accepted'
                AND joined_seen.observed_at > waiting_seen.observed_at
                AND joined_seen.observed_at < %(end)s
          )
        """,
        {
            "player": player_id,
            "start": day.season_start,
            "season": day.official_season_id,
            "end": day.season_end,
        },
    ).fetchone()[0]
    day_start = day.season_start
    while latest is not None and day_start + timedelta(days=1) <= latest:
        day_text = day_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _queue(
            connection, player_id, day_start, None,
            key=f"reconcile:not-enrolled:{player_id}:{day_text}",
            trigger="late_enrollment", later_days=False,
        )
        day_start += timedelta(days=1)


def backfill(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    # An operator's batch: queued at backfill priority, like requeue_day_1,
    # so a worker thread runs it only when no higher-priority work that thread
    # can claim is due.
    """Find, and with ``queue`` recalculate, the days that players first
    tracked during the Season can now fill: Day 1 for each player whose first
    battle log was saved on Day 1, and, for a player first tracked later, the
    first day their own battles reach back to. Repeating it skips players
    already queued from their earliest saved battle log. A Day 1 waits for
    the accepted Legend I profile naming the Season that its Season-rule
    start needs; ``queue_day_1`` queues it when that profile is saved.

    A player first tracked later with no Legend battles on Day 1 gets no
    Day 1: they may not have joined the Season until later, and Clash Lens
    must not invent a Day 1 for them."""
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")
    with database.pool.connection() as connection:
        with connection.transaction():
            rows = connection.execute(
                """
                WITH first_logs AS (
                    SELECT DISTINCT ON (player_id)
                           player_id, observed_at AS first_at, observation_id
                    FROM battle_log_observations
                    ORDER BY player_id, observed_at, id
                ), first_days AS (
                    SELECT player_id, first_at, observation_id,
                           date_bin('1 day', first_at, %(start)s) AS first_day
                    FROM first_logs
                    WHERE first_at >= %(start)s AND first_at < %(end)s
                ), earlier AS (
                    SELECT first_days.player_id, min(battle.ranked_day_start) AS day
                    FROM legend_battles AS battle
                    CROSS JOIN LATERAL (VALUES (battle.attacker_player_id),
                                               (battle.defender_player_id))
                        AS side (player_id)
                    JOIN first_days ON first_days.player_id = side.player_id
                    WHERE battle.ranked_day_start >= %(start)s
                      AND battle.ranked_day_start < first_days.first_day
                    GROUP BY first_days.player_id
                )
                SELECT first_days.player_id,
                       COALESCE(earlier.day, first_days.first_day) AS day,
                       first_days.first_day = %(start)s AS first_seen_day_1,
                       first_days.observation_id,
                       EXISTS (
                           SELECT 1 FROM python_processing_jobs_worker AS job
                           WHERE job.deduplication_key = 'reconcile:first-log:'
                               || first_days.player_id || ':'
                               || to_char(COALESCE(earlier.day, first_days.first_day)
                                          AT TIME ZONE 'UTC',
                                          'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                               || ':' || first_days.observation_id
                       ) AS queued
                FROM first_days
                LEFT JOIN earlier USING (player_id)
                WHERE first_days.first_day = %(start)s OR earlier.day IS NOT NULL
                ORDER BY first_days.player_id
                """,
                {"start": season_start, "end": season_start + domain.SEASON_DURATION},
            ).fetchall()
            waiting = [row for row in rows if not row[4]]
            ready = [
                row for row in waiting
                if row[1] != season_start
                or reset_baselines._season_rule_holds(
                    connection, int(row[0]), season_start
                )
            ]
            job_ids = [
                job_id
                for player_id, day, _, observation_id, _ in (
                    ready[:max_jobs] if queue else []
                )
                if (job_id := _queue(
                    connection, int(player_id), day, int(observation_id),
                    priority=PYTHON_BACKFILL_PRIORITY,
                )) is not None
            ]
    return {
        "season": season_id,
        "players": len(rows),
        "first_seen_day_1": sum(1 for row in rows if row[2]),
        "first_seen_later_with_earlier_battles": sum(1 for row in rows if not row[2]),
        "days": {
            day.astimezone(UTC).isoformat(): count
            for day, count in sorted(Counter(row[1] for row in rows).items())
        },
        "already_queued": len(rows) - len(waiting),
        "waiting_for_profile": len(waiting) - len(ready),
        "queued": len(job_ids),
        "left_to_queue": len(ready) - len(job_ids),
    }


def requeue_day_1(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    """Find, and with ``queue`` recalculate, every player's saved Day 1 with
    1 to 7 defenses, and their later saved days, once: Day 1's automatic
    defense loss averages Day 1's own defenses only, is charged for
    (attacks - defenses) missing defenses when attacks are at least the
    defenses, and a Reset reading taken before it is read less it. Repeating
    it skips players already queued; players queued by the run before those
    last two changes are queued again. The batch is queued at backfill
    priority: a worker thread runs it only when no higher-priority work that
    thread can claim is due. Batch 1 of 775 players on 2026-10-07 was
    queued live, in the busy hour after Reset."""
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")
    day_text = season_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    def key(player_id: Any) -> str:
        return f"reconcile:season-day-1-unsettled-loss:{player_id}:{day_text}:{RECONCILIATION_RULE_VERSION}"

    with database.pool.connection() as connection:
        with connection.transaction():
            rows = connection.execute(
                """
                WITH day_1 AS (
                    SELECT DISTINCT ON (player_id) player_id, defense_count
                    FROM ranked_day_versions
                    WHERE ranked_day_start = %(start)s
                      AND reconciliation_rule_version = %(rule)s
                    ORDER BY player_id, version DESC, id DESC
                )
                SELECT player_id FROM day_1
                WHERE defense_count BETWEEN 1 AND %(most)s
                ORDER BY player_id
                """,
                {"start": season_start, "rule": RECONCILIATION_RULE_VERSION,
                 "most": MAX_DAILY_DEFENSES - 1},
            ).fetchall()
            queued = {
                row[0] for row in connection.execute(
                    "SELECT deduplication_key FROM python_processing_jobs_worker"
                    " WHERE deduplication_key = ANY(%s)",
                    ([key(row[0]) for row in rows],),
                ).fetchall()
            }
            waiting = [int(row[0]) for row in rows if key(row[0]) not in queued]
            job_ids = [
                job_id
                for player_id in (waiting[:max_jobs] if queue else [])
                if (job_id := _queue(
                    connection, player_id, season_start, None,
                    key=key(player_id), trigger="season_day_1",
                    priority=PYTHON_BACKFILL_PRIORITY,
                )) is not None
            ]
    return {
        "season": season_id,
        "players": len(rows),
        "already_queued": len(rows) - len(waiting),
        "queued": len(job_ids),
        "left_to_queue": len(waiting) - len(job_ids),
    }


def requeue_zero_result_slots(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    """Find, and with ``queue`` recalculate, each player's oldest ended day
    of the Season whose battle logs hold a "no opponent, no battle" row, and
    their later saved days, so the day after each such day pools it too.
    Days saved before October 2026 left those rows out of the automatic
    defense loss. Only saved days are queued, each player and day once, at
    backfill priority, as ``requeue_day_1``.

    The rows are read from the Season's saved battle rows only: their IDs
    start above the lowest one a battle of two days before the Season used.
    """
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")
    season_end = season_start + domain.SEASON_DURATION

    def key(player_id: int, day: datetime) -> str:
        return (f"reconcile:zero-result-slots:{player_id}:"
                f"{day:%Y-%m-%dT%H:%M:%SZ}:{RECONCILIATION_RULE_VERSION}")

    with database.pool.connection() as connection:
        with connection.transaction():
            rows = connection.execute(
                """
                WITH bound AS (
                    SELECT COALESCE(min(evidence.source_row_id), 0) AS id
                    FROM legend_battles AS fight
                    JOIN battle_evidence AS evidence ON evidence.battle_id = fight.id
                    WHERE fight.ranked_day_start = %(before)s
                ), candidate AS (
                    SELECT source.id, source.battle_log_observation_id,
                           source.source_json
                    FROM battle_source_rows AS source, bound
                    WHERE source.id >= bound.id
                      AND source.outcome = 'malformed_legend_row'
                      AND source.source_json ->> 'battleType' = 'legend'
                      AND (source.source_json -> 'battleTime')::text = '0'
                ), owner AS (
                    SELECT candidate.id, log.id AS log_id
                    FROM candidate
                    JOIN battle_payload_row_lists AS list
                      ON list.source_row_ids @> ARRAY[candidate.id]
                    JOIN battle_log_observations AS log
                      ON log.parsed_payload_id = list.parsed_payload_id
                     AND log.player_id = list.reporting_player_id
                    UNION
                    SELECT candidate.id, log.id
                    FROM candidate
                    JOIN battle_payload_rows AS member
                      ON member.source_row_id = candidate.id
                    JOIN battle_log_observations AS log
                      ON log.parsed_payload_id = member.parsed_payload_id
                     AND log.player_id = member.reporting_player_id
                    UNION
                    SELECT candidate.id, occurrence.battle_log_observation_id
                    FROM candidate
                    JOIN battle_log_observation_rows AS occurrence
                      ON occurrence.source_row_id = candidate.id
                    UNION
                    SELECT id, battle_log_observation_id FROM candidate
                    WHERE battle_log_observation_id IS NOT NULL
                )
                SELECT DISTINCT log.player_id, log.parser_version, candidate.source_json
                FROM owner
                JOIN candidate ON candidate.id = owner.id
                JOIN battle_log_observations AS log ON log.id = owner.log_id
                """,
                {"before": season_start - timedelta(days=2)},
            ).fetchall()
            now = datetime.now(UTC)
            found: set[tuple[int, datetime]] = set()
            for player_id, parser, source in rows:
                if not battle.is_no_opponent_row(source, parser):
                    continue
                try:
                    day = domain.battle_day_for(battle._parse_battle_timestamp(
                        battle._battle_timestamp_value(source, parser), parser))
                except battle.BattleLogParseError:
                    continue
                if season_start <= day.start < season_end and day.end <= now:
                    found.add((int(player_id), day.start))
            # Only a saved day changes, and the day after only pools a saved one.
            saved = connection.execute(
                """
                SELECT DISTINCT player_id, ranked_day_start FROM ranked_day_versions
                WHERE player_id = ANY(%s) AND ranked_day_start = ANY(%s)
                  AND reconciliation_rule_version = %s
                """,
                (sorted({player for player, _ in found}),
                 sorted({day for _, day in found}), RECONCILIATION_RULE_VERSION),
            ).fetchall()
            oldest: dict[int, datetime] = {}
            for player_id, day in found & {(int(p), d) for p, d in saved}:
                oldest[player_id] = min(oldest.get(player_id, day), day)
            days = sorted(oldest.items())
            queued = {
                row[0] for row in connection.execute(
                    "SELECT deduplication_key FROM python_processing_jobs_worker"
                    " WHERE deduplication_key = ANY(%s)",
                    ([key(*day) for day in days],),
                ).fetchall()
            }
            waiting = [day for day in days if key(*day) not in queued]
            job_ids = [
                job_id
                for player_id, day in (waiting[:max_jobs] if queue else [])
                if (job_id := _queue(
                    connection, player_id, day, None,
                    key=key(player_id, day), trigger="zero_result_slots",
                    priority=PYTHON_BACKFILL_PRIORITY,
                )) is not None
            ]
    return {
        "season": season_id,
        "players": len(days),
        "already_queued": len(days) - len(waiting),
        "queued": len(job_ids),
        "left_to_queue": len(waiting) - len(job_ids),
    }


def requeue_overlap_gap(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    """Find, and with ``queue`` recalculate, each player's oldest ended day
    of the Season whose latest result reports ``battle_log_overlap_gap``, and
    their later saved days, once per day. Before October 2026 two full logs
    overlapped only through a shared Legend battle, so logs sharing only
    other battles were a gap: 911 ended days on 5 and 6 October 2026. A day
    still reporting a gap after this is queued no more. The batch is queued
    at backfill priority, as ``requeue_day_1``."""
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")

    def key(player_id: Any, day: datetime) -> str:
        return (f"reconcile:overlap-gap:{player_id}:"
                f"{day.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}:{RECONCILIATION_RULE_VERSION}")

    with database.pool.connection() as connection:
        with connection.transaction():
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (player_id, ranked_day_start)
                           player_id, ranked_day_start, failure_reasons
                    FROM ranked_day_versions
                    WHERE ranked_day_start >= %(start)s
                      AND ranked_day_start < %(end)s
                      AND ranked_day_start + interval '1 day' <= clock_timestamp()
                    ORDER BY player_id, ranked_day_start, version DESC, id DESC
                )
                SELECT DISTINCT ON (player_id) player_id, ranked_day_start
                FROM latest
                WHERE failure_reasons ? 'battle_log_overlap_gap'
                ORDER BY player_id, ranked_day_start
                """,
                {"start": season_start, "end": season_start + domain.SEASON_DURATION},
            ).fetchall()
            queued = {
                row[0] for row in connection.execute(
                    "SELECT deduplication_key FROM python_processing_jobs_worker"
                    " WHERE deduplication_key = ANY(%s)",
                    ([key(*row) for row in rows],),
                ).fetchall()
            }
            waiting = [row for row in rows if key(*row) not in queued]
            job_ids = [
                job_id
                for player_id, day in (waiting[:max_jobs] if queue else [])
                if (job_id := _queue(
                    connection, int(player_id), day, None,
                    key=key(player_id, day), trigger="overlap_gap",
                    priority=PYTHON_BACKFILL_PRIORITY,
                )) is not None
            ]
    return {
        "season": season_id,
        "players": len(rows),
        "already_queued": len(rows) - len(waiting),
        "queued": len(job_ids),
        "left_to_queue": len(waiting) - len(job_ids),
    }
