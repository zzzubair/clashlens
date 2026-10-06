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
from .battle import ParsedBattleRow
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
    _text_value,
    ended_day_priority,
)
from .domain import SEASON_START_TROPHIES, RankedDay
from .ranked_day_inputs import _source_rows
from .reconciliation import BATTLE_LOG_MAX_ROWS


def coverage_start(
    database: Database, connection: Any, player_id: int, ranked_day: RankedDay
) -> tuple[int, bool] | None:
    """The player's first saved battle log, when saved on or after the day's
    start and holding every battle of the day up to when it was saved: its
    oldest row is from before the day's battles, or it has fewer than 50
    rows, the whole log the game keeps. Also whether it was saved after the
    day's last battle, so it holds the whole day."""
    first = connection.execute(
        """
        SELECT id, observation_id, observed_at, row_count, parser_version
        FROM battle_log_observations
        WHERE player_id = %s
        ORDER BY observed_at, id
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()
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


def _queue(connection: Any, player_id: int, day_start: datetime) -> int | None:
    """Queue the recalculation of one day and every saved later day of its
    Season; ``None`` when it was already queued."""
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
            f"reconcile:first-log:{player_id}:{day_text}",
            Jsonb({
                "player_id": int(player_id),
                "ranked_day_start": day_text,
                "last_ranked_day_start": day_text,
                "recalculate_season": day.official_season_id,
                "trigger": "first_battle_log",
            }),
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
            ended_day_priority(day.start),
        ),
    ).fetchone()
    return int(row[0]) if row is not None else None


def queue_earlier_days(
    connection: Any,
    player_id: int,
    observed_at: datetime,
    rows: list[ParsedBattleRow],
) -> None:
    """When a player's first saved battle log holds their battles from an
    earlier day of the Season it was saved in, recalculate from that day."""
    saved_day = domain.ranked_day_for(observed_at)
    earlier = [
        row.battle.ranked_day_start
        for row in rows
        if row.battle is not None
        and saved_day.season_start <= row.battle.ranked_day_start < saved_day.start
    ]
    if not earlier or connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM battle_log_observations
            WHERE player_id = %s AND observed_at < %s
        )
        """,
        (player_id, observed_at),
    ).fetchone()[0]:
        return
    _queue(connection, player_id, min(earlier))


def backfill(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    """Find, and with ``queue`` recalculate, the days that players first
    tracked during the Season can now fill: Day 1 for each player whose first
    battle log was saved on Day 1, and, for a player first tracked later, the
    first day their own battles reach back to. Repeating it skips players
    already queued."""
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not domain.is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")
    with database.pool.connection() as connection:
        with connection.transaction():
            rows = connection.execute(
                """
                WITH first_logs AS (
                    SELECT player_id, min(observed_at) AS first_at
                    FROM battle_log_observations
                    GROUP BY player_id
                    HAVING min(observed_at) >= %(start)s
                       AND min(observed_at) < %(end)s
                ), first_days AS (
                    SELECT player_id, first_at,
                           date_bin('1 day', first_at, %(start)s) AS first_day
                    FROM first_logs
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
                       EXISTS (
                           SELECT 1 FROM python_processing_jobs_worker AS job
                           WHERE job.deduplication_key = 'reconcile:first-log:'
                               || first_days.player_id || ':'
                               || to_char(COALESCE(earlier.day, first_days.first_day)
                                          AT TIME ZONE 'UTC',
                                          'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                       ) AS queued
                FROM first_days
                LEFT JOIN earlier USING (player_id)
                WHERE first_days.first_day = %(start)s OR earlier.day IS NOT NULL
                ORDER BY first_days.player_id
                """,
                {"start": season_start, "end": season_start + domain.SEASON_DURATION},
            ).fetchall()
            waiting = [row for row in rows if not row[3]]
            job_ids = [
                job_id
                for player_id, day, _, _ in (waiting[:max_jobs] if queue else [])
                if (job_id := _queue(connection, int(player_id), day)) is not None
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
