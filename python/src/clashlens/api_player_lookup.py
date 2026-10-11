from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import api_db
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text
from .domain import ranked_day_for


def _lookup(connection: Any, tag: str) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT player.id, player.active, player.eligibility_state,
               EXISTS (SELECT 1 FROM player_profile_versions AS profile
                       WHERE profile.player_id = player.id),
               player.current_profile_version_id IS NULL
        FROM players AS player
        WHERE player.normalized_tag = %s
        """,
        (tag,),
    ).fetchone()
    state = "unknown"
    if row is not None:
        player_id, active, eligibility, confirmed, no_current_profile = row
        if active:
            why = _why_no_results(connection, player_id)
            # A newest rejected profile is explained even after accepted ones.
            if no_current_profile or why["reason"] != "pending":
                return {"tag": tag, "state": "tracking", **why}
            state = "tracking"
        elif confirmed:
            state = (
                "not_in_legend" if _text(eligibility) == "ineligible" else "uncertain"
            )
            # Off the board, the newest saved profile still names the player,
            # and its league when that profile is itself outside Legend I.
            _, _, newest, _, name, clan, trophies, _, league = _newest_profile(
                connection, player_id
            )
            profile = {"name": _text(name), "clan": _text(clan), "trophies": trophies}
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
                if failure == "player_not_found":
                    state = "not_found"
                elif work in ("pending", "waiting_retry") or processing in (
                    "pending",
                    "leased",
                    "waiting_retry",
                    "waiting_dependency",
                ):
                    state = "checking"
                elif work in ("failed", "cancelled") or processing in (
                    "failed",
                    "complete",
                ):
                    state = "failed"
                elif work == "complete":
                    state = "checking"
    return {"tag": tag, "state": state}


def _newest_profile(connection: Any, player_id: int) -> Any:
    """The player's newest processed profile, read without accepting it."""
    return connection.execute(
        """
        SELECT profile.current_league_season_id, profile.eligibility_reason,
               profile.eligibility_state, profile.source_contract_state,
               profile.name, profile.profile_json->'clan'->>'name', profile.trophies,
               EXISTS (SELECT 1 FROM api_player_daily_logs AS day
                       WHERE day.player_id = profile.player_id
                         AND day.ranked_day_start >= %s
                         AND jsonb_array_length(day.battles) > 0
                         AND NOT EXISTS (
                             SELECT 1 FROM api_player_daily_logs AS newer
                             WHERE newer.player_id = day.player_id
                               AND newer.ranked_day_start = day.ranked_day_start
                               AND newer.version > day.version)),
               profile.league_tier_name
        FROM player_profile_versions AS profile
        CROSS JOIN LATERAL (
            SELECT max(observed_at) AS observed_at FROM player_profile_effects
            WHERE profile_version_id = profile.id
        ) AS effect
        WHERE profile.player_id = %s
        ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC, profile.id DESC
        LIMIT 1
        """,
        (ranked_day_for(datetime.now(UTC)).season_start, player_id),
    ).fetchone()


def _why_no_results(connection: Any, player_id: int) -> dict[str, Any]:
    """Explain why a tracked player's newest processed profile gives no
    current results.

    Among tracked players, only a Legend I profile whose Season ID is 0 shows
    its name, clan and trophies, and only on the player's own page.
    """
    row = _newest_profile(connection, player_id)
    if row is None:
        return {"reason": "pending"}
    season, reason, eligibility, contract, name, clan, trophies, battles, _league = row
    if (
        _text(season) == "0"
        and _text(reason) == "confirmed_legend_i"
        and _text(contract) == "conflict"
    ):
        return {
            # Every such player checked on 2026-10-03 had no Legend battle
            # this Season, so a recorded battle means something else is wrong.
            "reason": "season_unconfirmed" if battles else "no_legend_battles",
            "profile": {"name": _text(name), "clan": _text(clan), "trophies": trophies},
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
    if lookup["state"] in {"unknown", "failed", "not_found"}:
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
                "SELECT * FROM clashlens_enqueue_interactive('initial_collection', %s, 30)",
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
