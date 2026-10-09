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
from collections.abc import Iterable
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
from .ranked_day_inputs import _source_rows, load_first_reports
from .reconciliation import (
    BATTLE_LOG_MAX_ROWS,
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
    due_at: datetime | None = None,
) -> int | None:
    """Queue the recalculation of one day and, with ``later_days``, every
    saved later day of its Season, by default from the player's earliest
    saved battle log, ``observation_id``; ``None`` when it was already
    queued. Without ``priority``, the day's own: Reset priority while its
    Reset is the latest, else live. Due now, or at ``due_at`` if later."""
    day = domain.ranked_day_for(day_start)
    day_text = day.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                    "last_ranked_day_start": day_text,
                    "recalculate_season": day.official_season_id,
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


def queue_days_read_after(connection: Any, battle_ids: Iterable[int]) -> None:
    """When a battle reported before a reading that judged either player's
    day before is saved, that reading may show it, so recalculate that day,
    once per battle: a result left unchanged, or with no reading that judges
    it any more, still meets each later battle."""
    ids = sorted(battle_ids)
    if not ids:
        return
    rows = connection.execute(
        """
        SELECT DISTINCT player.id, battle.ranked_day_start - interval '1 day',
               battle.id
        FROM legend_battles AS battle
        JOIN battle_evidence AS evidence ON evidence.battle_id = battle.id
        CROSS JOIN LATERAL (
            VALUES (battle.attacker_player_id), (battle.defender_player_id)
        ) AS player (id)
        WHERE battle.id = ANY(%s::bigint[])
          AND EXISTS (
              SELECT 1 FROM ranked_day_versions AS day
              WHERE day.player_id = player.id
                AND day.ranked_day_start = battle.ranked_day_start - interval '1 day'
                AND day.reconciliation_rule_version = %s
                AND (day.input_evidence -> 'end_reading' ->> 'read_at')::timestamptz
                    > evidence.battle_timestamp
          )
        """,
        (ids, RECONCILIATION_RULE_VERSION),
    ).fetchall()
    for player_id, day_start, battle_id in rows:
        _queue(
            connection, int(player_id), day_start, None,
            key=f"reconcile:read-after:{player_id}:"
            f"{day_start.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}:{battle_id}",
            trigger="read_after_battle", later_days=False,
        )


def queue_day_read_again(
    connection: Any, player_id: int, profile_version_id: int, read_at: datetime
) -> None:
    """When a changed profile is read after the reading that judged the
    player's day before, and before their first battle of the new day, it
    can still decide that day, so recalculate it, once per profile version."""
    day = domain.ranked_day_for(read_at)
    previous = day.start - timedelta(days=1)
    saved = connection.execute(
        """
        SELECT (input_evidence -> 'end_reading' ->> 'read_at')::timestamptz
        FROM ranked_day_versions
        WHERE player_id = %s AND ranked_day_start = %s
          AND reconciliation_rule_version = %s
        ORDER BY version DESC LIMIT 1
        """,
        (player_id, previous, RECONCILIATION_RULE_VERSION),
    ).fetchone()
    if saved is None or saved[0] is None or saved[0] >= read_at:
        return
    new_day_from = domain.battle_window(day.start)[0]
    if load_first_reports(connection, player_id, new_day_from, new_day_from, read_at)[1]:
        return
    _queue(
        connection, player_id, previous, None,
        key=f"reconcile:new-reading:{player_id}:"
        f"{previous.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}:{profile_version_id}",
        trigger="new_reading", later_days=False,
    )


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


def queue_weekly_drop(connection: Any, player_id: int, observed_at: datetime) -> None:
    """Once a profile read on a weekly Monday's Legend day shows a league
    below Legend I, recalculate the day before that Reset and each saved day
    after it, which then show the drop (see
    ``reconciliation_db._dropped_after_reading``), for a player with a Reset
    reading there. Runs once per player and Monday, when no other work
    waits, no earlier than ``DAY_END_RECALCULATION_DELAY`` after that Reset,
    once any calculation running when the profile arrived has saved."""
    from .reconciliation_db import DAY_END_RECALCULATION_DELAY

    day = domain.ranked_day_for(observed_at)
    ended = day.start - timedelta(days=1)
    if day.start.weekday() != 0 or day.start == day.season_start or connection.execute(
        """
        SELECT 1 FROM reset_baseline_evidence
        WHERE player_id = %s AND boundary_at = %s LIMIT 1
        """,
        (player_id, day.start),
    ).fetchone() is None:
        return
    _queue(
        connection, player_id, ended, None,
        key=(f"reconcile:weekly-drop:{player_id}:"
             f"{ended:%Y-%m-%dT%H:%M:%SZ}:{RECONCILIATION_RULE_VERSION}"),
        trigger="weekly_drop", priority=PYTHON_BACKFILL_PRIORITY,
        due_at=day.start + DAY_END_RECALCULATION_DELAY,
    )


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
