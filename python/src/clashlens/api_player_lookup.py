from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from . import api_db, api_leaderboard
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text
from .domain import ranked_day_for


class LookupState(StrEnum):
    """What Clash Lens can tell a caller about a tag (``_lookup``'s ``state``).

    - ``UNKNOWN``: no player row, no check yet, or a check the collector
      holds right now with nothing processed.
    - ``CHECKING``: a profile check is waiting, its saved response is still
      being processed, collection finished but nothing is processed yet, or
      a profile was just saved and the weekly league record is deciding.
    - ``TRACKING``: Clash Lens tracks the player.
    - ``NOT_IN_LEGEND``: saved profiles show the player outside Legend I.
    - ``UNCERTAIN``: a profile is saved but its league is not confirmed.
    - ``NOT_FOUND``: the game says the tag has no player.
    - ``FAILED``: the newest check failed or was cancelled, or was
      processed without saving a profile.

    Events: ``admit`` queues a check for a tag in ``RECHECKABLE``, at most
    once per 30 seconds; the collector and worker then move it through
    ``CHECKING`` to an answer. Collection finishing is a separate, earlier
    point than a processed profile, so a Refresh whose collection is
    complete can still read ``CHECKING``. Nothing here is stored: every read
    derives the state again from the player's flags, newest profile, newest
    check and its processing job, so a restart changes nothing.
    """

    UNKNOWN = "unknown"
    CHECKING = "checking"
    TRACKING = "tracking"
    NOT_IN_LEGEND = "not_in_legend"
    UNCERTAIN = "uncertain"
    NOT_FOUND = "not_found"
    FAILED = "failed"


# A visit may start a new check only for these.
RECHECKABLE = frozenset({LookupState.UNKNOWN, LookupState.FAILED, LookupState.NOT_FOUND})
# The game confirmed these players exist.
CONFIRMED = frozenset({LookupState.TRACKING, LookupState.NOT_IN_LEGEND, LookupState.UNCERTAIN})


def _check_progress(
    work: str | None, failure: str | None, processing: str | None
) -> LookupState:
    """The state of a player with no saved profile, from its newest check's
    collection status and failure and that response's processing status."""
    if failure == "player_not_found":
        return LookupState.NOT_FOUND
    if work in ("pending", "waiting_retry") or processing in (
        "pending",
        "leased",
        "waiting_retry",
        "waiting_dependency",
    ):
        return LookupState.CHECKING
    if work in ("failed", "cancelled") or processing in ("failed", "complete"):
        return LookupState.FAILED
    if work == "complete":
        return LookupState.CHECKING
    return LookupState.UNKNOWN


def _lookup(
    connection: Any, tag: str, now: datetime | None = None
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT player.id, player.active, player.eligibility_state,
               EXISTS (SELECT 1 FROM player_profile_versions AS profile
                       WHERE profile.player_id = player.id),
               player.current_profile_version_id IS NULL,
               EXISTS (SELECT 1 FROM league_week_dirty AS mark
                       WHERE mark.player_id = player.id AND mark.failed_at IS NULL)
        FROM players AS player
        WHERE player.normalized_tag = %s
        """,
        (tag,),
    ).fetchone()
    state = LookupState.UNKNOWN
    if row is not None:
        player_id, active, eligibility, confirmed, no_current_profile, deciding = row
        if not active and confirmed and deciding:
            # The weekly league record decides within seconds whether a
            # player whose profile was just saved is tracked.
            state = LookupState.CHECKING
        elif active:
            why = _why_no_results(connection, player_id, tag, now or datetime.now(UTC))
            # A newest rejected profile is explained even after accepted ones.
            if no_current_profile or why.get("reason", "pending") != "pending":
                return {"tag": tag, "state": LookupState.TRACKING, **why}
            state = LookupState.TRACKING
        elif confirmed:
            state = (
                LookupState.NOT_IN_LEGEND
                if _text(eligibility) == "ineligible"
                else LookupState.UNCERTAIN
            )
            # Off the board, the newest saved profile still names the player,
            # and its league when that profile is itself outside Legend I.
            _, _, newest, _, name, clan, trophies, _, league, clan_tag = (
                _newest_profile(connection, player_id)
            )
            profile = {
                "name": _text(name),
                "clan": _text(clan),
                "clan_tag": _text(clan_tag),
                "trophies": trophies,
            }
            if _text(newest) == "ineligible" and league is not None:
                profile["league"] = _text(league)
            return {"tag": tag, "state": state, "profile": profile}
        else:
            work_row = connection.execute(
                """
                -- Cleanup deletes only finished jobs, so a saved profile
                -- response whose job is gone was processed.
                SELECT work.status, work.failure_category,
                       COALESCE(processing.status, CASE
                           WHEN work.profile_observation_id IS NOT NULL THEN 'complete'
                       END),
                       -- A profile saved since the read above, committed with its job.
                       EXISTS (SELECT 1 FROM player_profile_versions AS profile
                               WHERE profile.player_id = %s)
                FROM (
                    SELECT status, failure_category, profile_observation_id
                    FROM collector_work
                    WHERE player_id = %s
                      AND kind IN ('initial_collection', 'live_refresh', 'discovery_profile')
                    ORDER BY id DESC LIMIT 1
                ) AS work
                LEFT JOIN LATERAL (
                    SELECT status FROM python_processing_jobs
                    WHERE observation_id = work.profile_observation_id
                      AND work_type = 'process_observation'
                    ORDER BY id DESC LIMIT 1
                ) AS processing ON true
                """,
                (player_id, player_id),
            ).fetchone()
            if work_row is not None:
                work, failure, processing, saved_since = work_row
                if saved_since:
                    return _lookup(connection, tag)
                state = _check_progress(_text(work), _text(failure), _text(processing))
    return {"tag": tag, "state": state}


def _newest_profile(
    connection: Any, player_id: int, now: datetime | None = None
) -> Any:
    """The player's newest processed profile, read without accepting it."""
    # The battle check runs on the one newest profile. Inside the profile
    # search the planner can read every player's Season days for it instead.
    return connection.execute(
        """
        SELECT profile.current_league_season_id, profile.eligibility_reason,
               profile.eligibility_state, profile.source_contract_state,
               profile.name, profile.clan_name, profile.trophies,
               EXISTS (SELECT 1 FROM api_player_daily_logs AS day
                       WHERE day.player_id = profile.player_id
                         AND day.ranked_day_start >= %s
                         AND jsonb_array_length(day.battles) > 0
                         AND NOT EXISTS (
                             SELECT 1 FROM api_player_daily_logs AS newer
                             WHERE newer.player_id = day.player_id
                               AND newer.ranked_day_start = day.ranked_day_start
                               AND newer.version > day.version)),
               profile.league_tier_name, profile.clan_tag
        FROM (
            SELECT profile.player_id, profile.current_league_season_id,
                   profile.eligibility_reason, profile.eligibility_state,
                   profile.source_contract_state, profile.name,
                   profile.profile_json->'clan'->>'name' AS clan_name,
                   profile.profile_json->'clan'->>'tag' AS clan_tag,
                   profile.trophies, profile.league_tier_name
            FROM player_profile_versions AS profile
            CROSS JOIN LATERAL (
                SELECT max(observed_at) AS observed_at FROM player_profile_effects
                WHERE profile_version_id = profile.id
            ) AS effect
            WHERE profile.player_id = %s
            ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                     profile.id DESC
            LIMIT 1
        ) AS profile
        """,
        (ranked_day_for(now or datetime.now(UTC)).season_start, player_id),
    ).fetchone()


def _why_no_results(
    connection: Any, player_id: int, tag: str, now: datetime
) -> dict[str, Any]:
    """Explain why a tracked player's newest processed profile gives no
    current results.

    Among tracked players, only a Legend I profile whose Season ID is 0 shows
    its name, clan and trophies, and only on the player's own page. One the
    Live Leaderboard ranks at 5,000 needs none: the player has not played
    yet, and their page shows them at 5,000.
    """
    row = _newest_profile(connection, player_id, now)
    if row is None:
        return {"reason": "pending"}
    season, reason, eligibility, contract, name, clan, trophies, battles, _, clan_tag = row
    if (
        _text(season) == "0"
        and _text(reason) == "confirmed_legend_i"
        and _text(contract) == "conflict"
    ):
        if not battles and connection.execute(
            api_leaderboard.UNPLAYED_PROFILE_SQL,
            {**api_leaderboard._season_params(now), "tag": tag},
        ).fetchone():
            return {}
        return {
            # Every such player checked on 2026-10-03 had no Legend battle
            # this Season, so a recorded battle means something else is wrong.
            "reason": "season_unconfirmed" if battles else "no_legend_battles",
            "profile": {
                "name": _text(name),
                "clan": _text(clan),
                "clan_tag": _text(clan_tag),
                "trophies": trophies,
            },
        }
    if _text(eligibility) == "uncertain":
        return {"reason": "unknown_tier"}
    if _text(contract) == "conflict":
        return {"reason": "profile_rejected"}
    return {"reason": "pending"}


def get_lookup(database: ApiDatabase, tag: str) -> dict[str, Any]:
    with database.pool.connection() as connection:
        return _lookup(connection, tag)


def lock_tag(connection: Any, normalized_tag: str) -> None:
    """Take the per-tag lock the existing enqueue function also takes."""
    api_db._execute_without_waiting(
        connection,
        normalized_tag,
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (normalized_tag,),
    )


def admit(connection: Any, normalized_tag: str) -> dict[str, Any]:
    """Start a profile check for a tag Clash Lens has no answer for yet."""
    # Serialize the evidence read with admission so simultaneous visits
    # reuse work.
    lock_tag(connection, normalized_tag)
    lookup = _lookup(connection, normalized_tag)
    if lookup["state"] in RECHECKABLE:
        # Failed and negative checks have the same minimum retry interval
        # as Refresh. Known real players are never rechecked by a visit.
        recent = connection.execute(
            """
            SELECT 1 FROM collector_work
            WHERE normalized_tag = %s
              AND kind IN ('initial_collection', 'live_refresh', 'discovery_profile')
              AND updated_at > clock_timestamp() - interval '30 seconds'
            LIMIT 1
            """,
            (normalized_tag,),
        ).fetchone()
        if recent is None:
            api_db._execute_without_waiting(
                connection,
                normalized_tag,
                "SELECT 1 FROM clashlens_enqueue_interactive('initial_collection', %s, 30)",
                (normalized_tag,),
            ).fetchone()
            lookup = _lookup(connection, normalized_tag)
    return lookup


def submit_lookup(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            lookup = admit(connection, normalized_tag)
            result = OperationResult(200, lookup)
            api_db._complete_request(connection, binding.request_id, result)
            return result
