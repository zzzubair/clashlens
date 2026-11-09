"""Persist requests to recalculate one Legend day or a range of days."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from . import domain
from .db import DEFAULT_PARSER_VERSION, ended_day_priority
from .job_contracts import (
    ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)


def request(
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
