"""Stored Live-board membership and periodic unplayed-profile maintenance."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from . import league_week
from .analytics import LIVE_TALLY_SECONDS, unplayed_since_sql, unplayed_sql
from .domain import SEASON_START_TROPHIES, ranked_day_for, season_opening_reset
from .season_reset_sql import season_reset_waiting_sql

# Shared membership and confirmation rule for the page and operator measurements.
# A profile still naming an earlier Season than the calendar shows trophies from
# before that player's Season reset, so it waits off the board until it updates.
# On a Season's first Legend day, trophies other than 5,000 that still equal
# the player's frozen pre-Reset final trophies, or that the day's recorded
# battles cannot explain, wait the same way.
_OPENING_DAY_WAITING_SQL = season_reset_waiting_sql(
    "player.id", "profile.trophies", "%(opening_reset)s"
)
# When the player last entered Legend I: the Season's start, or the Monday
# Reset the weekly league record says they were promoted or first seen at,
# as Daily boards count it.
_ENTERED_SQL = unplayed_since_sql("player.id", "%(season_start)s", "%(week_start)s")
# A tracked Legend I player at 5,000 with no Legend battle saved since they
# entered Legend I has not played yet, so they rank at 5,000 from their
# newest profile even when it names no current Season, such as Season ID 0,
# or was rejected for it. On 11 October 2026, 1,374 tracked players waited
# off the board so.
_UNPLAYED_SQL = unplayed_sql("player.id", "version", _ENTERED_SQL, "%(season_end)s")
# The player's current accepted profile, and whether it ranks them this Season:
# a Legend I one read since they entered Legend I. On 26 October 2026, 317
# players promoted into Legend I still had a current Legend II profile from
# the Season's first week, whose Season ID passed, and 98 promoted back had a
# Legend I one from before their drop; each ranked at those old trophies.
CURRENT_PROFILE_JOINS = f"""
LEFT JOIN LATERAL (
    SELECT name, trophies, profile_json, current_league_season_id,
           eligibility_state
    FROM player_profile_versions
    WHERE id = player.current_profile_version_id
      AND source_contract_state = 'accepted'
    -- Keep one indexed current-profile lookup per player as history grows.
    OFFSET 0
) AS profile ON true
CROSS JOIN LATERAL (
    SELECT COALESCE(profile.current_league_season_id = %(season_id)s
                    AND profile.eligibility_state = 'eligible'
                    -- One read this week needs no lookup: on 26 October
                    -- 2026 the lookup for every player added 250 ms.
                    AND CASE WHEN player.current_observed_at >= %(week_start)s THEN true
                             ELSE player.current_observed_at >= {_ENTERED_SQL} END, false)
           AND NOT {_OPENING_DAY_WAITING_SQL} AS season_current
    -- Work it out once per player: copied into each use, on 26 October 2026
    -- its league-week lookup ran 5,259 times for 4,356 players.
    OFFSET 0
) AS current
"""
# Look only for players the current profile leaves off the board or shows
# at 5,000, which a newer profile may show instead.
UNPLAYED_CANDIDATE_SQL = f"""(NOT current.season_current
               OR profile.trophies = {SEASON_START_TROPHIES})"""


def unplayed_seen_sql(sighted_at: str, version_id: str, observation_ids: str) -> str:
    """When the 5,000 profile ``version_id`` was last seen: its newest
    sighting, or a later successful unchanged check of it, which confirms it
    without another sighting. ``observation_ids`` are observations known to
    show it."""
    return f"""greatest({sighted_at}, CASE
               WHEN {version_id} = player.current_profile_version_id
               THEN player.current_profile_confirmed_at
           END, (
               SELECT checked.last_success_at
               FROM collector_response_state AS checked
               WHERE checked.scope = 'player'
                 AND checked.identity_key = player.normalized_tag
                 AND checked.endpoint = 'profile'
                 AND (checked.last_observation_id IN ({observation_ids})
                      OR EXISTS (
                          SELECT 1 FROM player_profile_effects AS retained
                          WHERE retained.observation_id
                                = checked.last_observation_id
                            AND retained.profile_version_id = {version_id}))
           ))"""


# Their newest profile, when it ranks them at 5,000 instead.
UNPLAYED_SEARCH_SQL = f"""
    SELECT version.id, version.name, version.trophies, version.profile_json,
           version.observation_id, newest.observed_at AS sighted_at,
           newest.effect_id AS sighting_id,
           {
    unplayed_seen_sql("newest.observed_at", "version.id", "version.observation_id")
} AS observed_at
    FROM (
        -- The newest sighting of any of the player's profiles: a repeated
        -- profile is saved once and sighted again.
        SELECT version.id,
               COALESCE(sighting.observed_at, version.observed_at) AS observed_at,
               sighting.id AS effect_id
        FROM player_profile_versions AS version
        LEFT JOIN LATERAL (
            SELECT effect.observed_at, effect.id
            FROM player_profile_effects AS effect
            WHERE effect.profile_version_id = version.id
            ORDER BY effect.observed_at DESC, effect.id DESC
            LIMIT 1
        ) AS sighting ON true
        WHERE {UNPLAYED_CANDIDATE_SQL}
          AND version.normalized_tag = player.normalized_tag
        ORDER BY 2 DESC, version.id DESC
        LIMIT 1
    ) AS newest
    JOIN player_profile_versions AS version ON version.id = newest.id
    WHERE {_UNPLAYED_SQL}
"""


def season_params(now: datetime) -> dict[str, Any]:
    day = ranked_day_for(now)
    return {
        "season_id": day.official_season_id,
        "opening_reset": season_opening_reset(now),
        "season_start": day.season_start,
        "season_end": day.season_end,
        "week_start": league_week.week_start(now),
    }


_REFRESH_UNPLAYED_SQL = f"""
WITH searched AS MATERIALIZED (
    -- An outer join, so the battle check waits for the 5,000 profile instead
    -- of running for every player.
    SELECT player.id AS player_id, unplayed.id AS profile_version_id,
           unplayed.observation_id, unplayed.sighted_at, unplayed.sighting_id,
           unplayed.name, unplayed.trophies,
           unplayed.profile_json -> 'clan' ->> 'name' AS clan
    FROM players AS player
    {CURRENT_PROFILE_JOINS}LEFT JOIN LATERAL ({UNPLAYED_SEARCH_SQL}) AS unplayed ON true
    WHERE player.active = true
), found AS (
    SELECT * FROM searched WHERE profile_version_id IS NOT NULL
), saved AS (
    INSERT INTO live_unplayed_profiles AS saved
        (player_id, official_season_id, profile_version_id, observation_id,
         sighted_at, sighting_observation_id, name, trophies, clan)
    SELECT player_id, %(season_id)s, profile_version_id, observation_id, sighted_at,
           (SELECT sighting.observation_id FROM player_profile_effects AS sighting
            WHERE sighting.id = found.sighting_id),
           name, trophies, clan
    FROM found
    ON CONFLICT (player_id) DO UPDATE
    SET official_season_id = excluded.official_season_id,
        profile_version_id = excluded.profile_version_id,
        observation_id = excluded.observation_id,
        sighted_at = excluded.sighted_at,
        sighting_observation_id = excluded.sighting_observation_id,
        name = excluded.name,
        trophies = excluded.trophies,
        clan = excluded.clan,
        refreshed_at = clock_timestamp()
    WHERE (saved.official_season_id, saved.profile_version_id, saved.sighted_at,
           saved.sighting_observation_id)
          IS DISTINCT FROM (excluded.official_season_id, excluded.profile_version_id,
                            excluded.sighted_at, excluded.sighting_observation_id)
    RETURNING 1
), removed AS (
    DELETE FROM live_unplayed_profiles AS saved
    WHERE NOT EXISTS (SELECT 1 FROM found WHERE found.player_id = saved.player_id)
    RETURNING 1
)
SELECT (SELECT count(*) FROM found), (SELECT count(*) FROM saved)
       + (SELECT count(*) FROM removed)
"""
_next_unplayed_refresh_at = float("-inf")


def save_live_unplayed(connection: Any, now: datetime) -> tuple[int, int]:
    """Save who the Live board ranks at 5,000 from a newest profile with no
    battle this Season: (players found, rows changed)."""
    found, changed = connection.execute(
        _REFRESH_UNPLAYED_SQL, season_params(now)
    ).fetchone()
    return int(found), int(changed)


def refresh_live_unplayed(database: Any, *, now: datetime | None = None) -> None:
    """``save_live_unplayed`` every LIVE_TALLY_SECONDS; a failure waits a turn."""
    global _next_unplayed_refresh_at
    started = time.monotonic()
    if started < _next_unplayed_refresh_at:
        return
    _next_unplayed_refresh_at = started + LIVE_TALLY_SECONDS
    try:
        with database.pool.connection() as connection, connection.transaction():
            # Compiling the search to machine code took 4 s of its 17 s on
            # 11 October 2026 production data, more than it saved.
            connection.execute("SET LOCAL jit = off")
            found, changed = save_live_unplayed(connection, now or datetime.now(UTC))
    except Exception as error:  # noqa: BLE001 - tried again at the next turn
        print(
            json.dumps(
                {
                    "event": "live_unplayed_profiles",
                    "status": "failed",
                    "error": type(error).__name__,
                }
            ),
            flush=True,
        )
        return
    print(
        json.dumps(
            {
                "event": "live_unplayed_profiles",
                "status": "complete",
                "players": found,
                "changed": changed,
                "seconds": round(time.monotonic() - started, 1),
            }
        ),
        flush=True,
    )
