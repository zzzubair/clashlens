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
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from . import battle, domain, reset_baselines
from .analytics import unplayed_profile_sql
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
    """The player's first battle log saved on or after the day's start, when
    it holds every battle of the day up to when it was saved: its oldest row
    is from before the day's battles, or it has fewer than 50 rows, the whole
    log the game keeps. Also whether it was saved after the day's last
    battle, so it holds the whole day. A player promoted into Legend I at the
    Reset may have older logs from a lower league."""
    first = connection.execute(
        """
        SELECT id, observation_id, observed_at, row_count, parser_version
        FROM battle_log_observations
        WHERE player_id = %s AND observed_at >= %s
        ORDER BY observed_at, id
        LIMIT 1
        """,
        (player_id, ranked_day.start),
    ).fetchone()
    if first is None:
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


def _first_seen_at_5000(
    connection: Any, player_id: int, boundary_at: datetime
) -> tuple[int | None, datetime | None]:
    """The player's first profile read from ``boundary_at`` to the end of its
    Season that can show them not played yet
    (``analytics.unplayed_profile_sql``), and when it was read."""
    row = connection.execute(
        f"""
        SELECT profile.id, COALESCE(effect.observed_at, profile.observed_at)
        FROM player_profile_versions AS profile
        LEFT JOIN player_profile_effects AS effect
          ON effect.profile_version_id = profile.id
        WHERE profile.player_id = %s
          AND {unplayed_profile_sql("profile")}
          AND COALESCE(effect.observed_at, profile.observed_at) >= %s
          AND COALESCE(effect.observed_at, profile.observed_at) < %s
        ORDER BY 2, profile.id
        LIMIT 1
        """,
        (player_id, boundary_at, domain.ranked_day_for(boundary_at).season_end),
    ).fetchone()
    return (None, None) if row is None else (int(row[0]), row[1])


def season_rule_start(
    connection: Any, player_id: int, boundary_at: datetime, *, complete: bool,
    at_5000: bool,
) -> dict[str, Any] | None:
    """The 5,000 by the Season rule at the Reset ``boundary_at``, for a
    player with no saved reading from it: when the game starts them there
    (``at_5000``: a Season's first Monday, or a Monday they were promoted
    at, by the weekly league record), or once a Legend I profile at 5,000
    read from it in the Season, whatever Season it names, came before their
    first Legend battle of the Season. ``complete`` is whether a battle log
    proves the day's battles up to the Reset."""
    if not (at_5000 or reset_baselines._unplayed(
        connection, player_id, boundary_at,
        *_first_seen_at_5000(connection, player_id, boundary_at),
    )):
        return None
    return {
        "id": None,
        "version": None,
        "state": "season_rule",
        "complete": complete,
        "trophies": SEASON_START_TROPHIES,
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
    due_at: datetime | None = None, last_day: datetime | None = None,
    every_day: bool = False,
) -> int | None:
    """Queue the recalculation of one day and, with ``later_days``, every
    saved later day of its Season and ``last_day``, saved or not, with
    ``every_day`` each day up to it too, by default
    from the player's earliest saved battle log, ``observation_id``; ``None``
    when it was already queued. Without ``priority``, the day's own: Reset
    priority while its Reset is the latest, else live. Due now, or at
    ``due_at`` if later."""
    day = domain.ranked_day_for(day_start)
    day_text = day.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_text = (day.start if last_day is None else last_day).astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    row = connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version, priority
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending',
            GREATEST(clock_timestamp(), %s::timestamptz), %s, %s, %s, %s, %s
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
                    "last_ranked_day_start": last_text,
                    "recalculate_season": day.official_season_id,
                    **({"every_day": True} if every_day else {}),
                } if later_days else {}),
                "trigger": trigger,
            }),
            due_at,
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
    day of the Season it was saved in, recalculate from that day: it proves
    those days' battles."""
    saved_day = domain.ranked_day_for(observed_at)
    earlier = [
        row.battle.ranked_day_start
        for row in rows
        if row.battle is not None
        and saved_day.season_start <= row.battle.ranked_day_start < saved_day.start
    ]
    if saved_day.start == saved_day.season_start:
        earlier = [saved_day.start]
    first = _first_log(connection, player_id)
    if first[2] < observed_at or not earlier:
        return
    _queue(connection, player_id, min(earlier), int(first[1]))


def backfill(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    # An operator's batch: queued at backfill priority, like the Season
    # repair, so a worker thread runs it only when no higher-priority work
    # that thread can claim is due.
    """Find, and with ``queue`` recalculate, the days that players first
    tracked during the Season can now fill: Day 1 for each player whose first
    battle log was saved on Day 1, and, for a player first tracked later, the
    first day their own battles reach back to. Repeating it skips players
    already queued from their earliest saved battle log. A day in a week the
    weekly league record does not say was Legend I saves nothing.

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
            job_ids = [
                job_id
                for player_id, day, _, observation_id, _ in (
                    waiting[:max_jobs] if queue else []
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
        "queued": len(job_ids),
        "left_to_queue": len(waiting) - len(job_ids),
    }
