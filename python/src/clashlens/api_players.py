from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from .api_db import (
    PLAYER_SCREEN_READY_VERSION,
    ApiDatabase,
    _daily_log,
    _historical_season_summary,
    _public_army,
    _public_confidence,
    _screen_daily_log_with_events,
    _text,
)
from .army_decoder import DECODER_VERSION
from .catalog import CATALOG_VERSION


def get_player_page(
    database: ApiDatabase,
    normalized_tag: str,
    *,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        metadata_join, metadata_columns = database._current_profile_metadata(connection)
        row = connection.execute(
            f"""
            SELECT player.normalized_tag, player.active, player.eligibility_state,
                   profile.name, profile.trophies,
                   player.current_observed_at,
                   {metadata_columns},
                   profile.profile_json -> 'clan' ->> 'name'
            FROM players AS player
            JOIN player_profile_versions AS profile
                ON profile.id = player.current_profile_version_id
            {metadata_join}
            WHERE player.normalized_tag = %s
              AND profile.source_contract_state = 'accepted'
            """,
            (normalized_tag,),
        ).fetchone()
        if row is None:
            return None
        observed_at = row[5].astimezone(UTC)
        age_seconds = max(
            0, int((now.astimezone(UTC) - observed_at).total_seconds())
        )
        daily_rows = connection.execute(
            """
            SELECT ranked_day_start, ranked_day_end, official_season_id,
                   season_day_number, version, state, coverage, confidence,
                   attack_count, attack_three_star_count, attack_gain,
                   defense_count, defense_three_star_count, defense_loss,
                   net_trophy_change, adjustments, battles, partial_reasons
            FROM (
                SELECT DISTINCT ON (ranked_day_start)
                       ranked_day_start, ranked_day_end, official_season_id,
                       season_day_number, version, state, coverage, confidence,
                       attack_count, attack_three_star_count, attack_gain,
                       defense_count, defense_three_star_count, defense_loss,
                       net_trophy_change, adjustments, battles, partial_reasons
                FROM api_player_daily_logs
                WHERE player_id = (
                    SELECT id FROM players WHERE normalized_tag = %s
                )
                ORDER BY ranked_day_start DESC, version DESC
            ) AS current_days
            ORDER BY ranked_day_start DESC
            LIMIT 28
            """,
            (normalized_tag,),
        ).fetchall()
        public_confidence = _public_confidence(bool(row[1]), _text(row[2]))
        daily_logs = [_daily_log(day) for day in daily_rows]
        battle_ids = {
            int(battle["battle_id"])
            for day in daily_logs
            for battle in day.get("battles", [])
            if isinstance(battle, Mapping)
            and isinstance(battle.get("battle_id"), (int, str))
            and str(battle["battle_id"]).isdigit()
        }
        army_rows = (
            connection.execute(
                """
            SELECT battle_id, perspective, status, failure_category,
                   home_troops, spells, siege, cc_troops, heroes,
                   unresolved_components, decoder_version, catalog_version
            FROM battle_army_decodes
            WHERE battle_id = ANY(%s::bigint[]) AND is_active
              AND decoder_version = %s AND catalog_version = %s
            """,
                (list(battle_ids), DECODER_VERSION, CATALOG_VERSION),
            ).fetchall()
            if battle_ids
            else []
        )
        armies = {
            (str(row[0]), _text(row[1])): _public_army(row) for row in army_rows
        }
        display_logs = deepcopy(daily_logs)
        for day in display_logs:
            for battle in day.get("battles", []):
                if not isinstance(battle, dict):
                    continue
                perspective = (
                    "attacker" if battle.get("lens") == "offense" else "defender"
                )
                army = armies.get((str(battle.get("battle_id")), perspective))
                if army is not None:
                    battle["army"] = army
        screen_days = [
            _screen_daily_log_with_events(day, public_confidence)
            for day in display_logs
        ]
        now_utc = now.astimezone(UTC)
        current_day_pair = next(
            (
                (raw_day, screen_day)
                for raw_day, screen_day in zip(daily_rows, screen_days, strict=True)
                if raw_day[1] is not None
                and raw_day[0].astimezone(UTC)
                <= now_utc
                < raw_day[1].astimezone(UTC)
            ),
            None,
        )
        current_day_raw = None if current_day_pair is None else current_day_pair[0]
        current_day = None if current_day_pair is None else current_day_pair[1]
        season_rows = []
        if (
            current_day_raw is not None
            and current_day_raw[2] is not None
            and current_day_raw[3] is not None
        ):
            # A season is exactly 28 ranked days. Bound this read to the
            # identified season while retaining the latest frozen
            # publication for each ranked-day start. Filtering before the
            # bound prevents previous-season rows from filling the result.
            season_rows = connection.execute(
                """
                SELECT ranked_day_start, ranked_day_end, official_season_id,
                       season_day_number, version, state, coverage, confidence,
                       attack_count, attack_three_star_count, attack_gain,
                       defense_count, defense_three_star_count, defense_loss,
                       net_trophy_change, adjustments, battles, partial_reasons
                FROM (
                    SELECT DISTINCT ON (ranked_day_start)
                           ranked_day_start, ranked_day_end, official_season_id,
                           season_day_number, version, state, coverage, confidence,
                           attack_count, attack_three_star_count, attack_gain,
                           defense_count, defense_three_star_count, defense_loss,
                           net_trophy_change, adjustments, battles, partial_reasons
                    FROM api_player_daily_logs
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = %s
                    )
                      AND ranked_day_start >= %s - (%s - 1) * interval '1 day'
                      AND ranked_day_start < %s + (29 - %s) * interval '1 day'
                    ORDER BY ranked_day_start DESC, version DESC
                ) AS latest_days
                WHERE official_season_id = %s
                ORDER BY season_day_number DESC NULLS LAST,
                         ranked_day_start DESC
                LIMIT 28
                """,
                (
                    normalized_tag,
                    current_day_raw[0],
                    int(current_day_raw[3]),
                    current_day_raw[0],
                    int(current_day_raw[3]),
                    _text(current_day_raw[2]),
                ),
            ).fetchall()
        season_display_logs = [_daily_log(day) for day in season_rows]
        for day in season_display_logs:
            for battle in day.get("battles", []):
                if not isinstance(battle, dict):
                    continue
                perspective = (
                    "attacker" if battle.get("lens") == "offense" else "defender"
                )
                army = armies.get((str(battle.get("battle_id")), perspective))
                if army is not None:
                    battle["army"] = army
        season_days = [
            _screen_daily_log_with_events(day, public_confidence)
            for day in season_display_logs
        ]
        data_quality = []
        if age_seconds > freshness_seconds:
            data_quality.append(
                {
                    "code": "stale",
                    "label": "Stale saved profile",
                    "detail": "The accepted player profile is older than the current freshness limit.",
                }
            )
        if current_day is None:
            data_quality.append(
                {
                    "code": "unavailable",
                    "label": "Missing current ranked-day data",
                    "detail": "No ranked-day publication covers the current UTC time.",
                }
            )
        elif current_day["completeness"]["state"] != "complete":
            data_quality.append(
                {
                    "code": current_day["completeness"]["state"],
                    "label": "Incomplete ranked-day data",
                    "detail": current_day["completeness"]["reason"],
                }
            )
        return {
            "tag": _text(row[0]),
            "name": _text(row[3]),
            "trophies": int(row[4]),
            "eligibility": _text(row[2]),
            "active": bool(row[1]),
            "freshness": "fresh" if age_seconds <= freshness_seconds else "stale",
            "age_seconds": age_seconds,
            "coverage": "ranked_days" if daily_rows else "profile_only",
            "observed_at": observed_at.isoformat(),
            "source_http_status": int(row[6]),
            "endpoint_version": _text(row[7]),
            "schema_version": _text(row[8]),
            "parser_version": _text(row[9]),
            "clan": None if row[10] is None else _text(row[10]),
            "public_confidence": public_confidence,
            "daily_logs": daily_logs,
            "screen_ready": {
                "current_day": current_day,
                "recent_days": screen_days,
                "season_days": season_days,
                "season": None
                if (
                    current_day is None
                    or current_day["official_season_id"] is None
                    or current_day["season_day_number"] is None
                )
                else {
                    "id": current_day["official_season_id"],
                    "current_day_number": current_day["season_day_number"],
                    "start": current_day["ranked_day_start"],
                    "end": current_day["ranked_day_end"],
                },
                "data_quality": data_quality,
                "provenance": {
                    "source": "api_player_daily_logs",
                    "observed_at": observed_at.isoformat(),
                    "freshness": "fresh"
                    if age_seconds <= freshness_seconds
                    else "stale",
                    "confidence": public_confidence,
                    "coverage": (
                        current_day["completeness"]["state"]
                        if current_day is not None
                        else "missing"
                    ),
                    "version": PLAYER_SCREEN_READY_VERSION,
                },
            },
        }


def list_player_seasons(database: ApiDatabase, normalized_tag: str) -> list[dict[str, Any]]:
    """List summarized historical seasons for one player.

    Reads the compact summary table only; it never touches current
    profile evidence, battle decodes, daily publications, or
    ranked-day detail.
    """
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT summary.official_season_id, summary.coverage_state,
                   summary.days_observed, summary.days_missing,
                   summary.start_trophies, summary.end_trophies,
                   summary.published_at
            FROM player_season_summaries AS summary
            JOIN players AS player ON player.id = summary.player_id
            WHERE player.normalized_tag = %s
            ORDER BY summary.season_start NULLS LAST,
                     summary.official_season_id
            """,
            (normalized_tag,),
        ).fetchall()
        return [
            {
                "official_season_id": _text(row[0]),
                "coverage_state": _text(row[1]),
                "days_observed": int(row[2]),
                "days_missing": int(row[3]),
                "start_trophies": None if row[4] is None else int(row[4]),
                "end_trophies": None if row[5] is None else int(row[5]),
                "published_at": row[6].astimezone(UTC).isoformat(),
            }
            for row in rows
        ]


def get_player_season_summary(
    database, normalized_tag: str, official_season_id: str
) -> dict[str, Any] | None:
    """Read one historical season directly from its compact summary.

    Uses shared player identity only. A missing summary returns None
    (the caller reports unavailable/not found); it never falls back to
    live detail.
    """
    with database.pool.connection() as connection:
        cursor = connection.execute(
            """
            SELECT player.normalized_tag, summary.*
            FROM player_season_summaries AS summary
            JOIN players AS player ON player.id = summary.player_id
            WHERE player.normalized_tag = %s
              AND summary.official_season_id = %s
            """,
            (normalized_tag, official_season_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cursor.description]
        record = dict(zip(columns, row))
        return _historical_season_summary(record)


def search_known_players(
    database: ApiDatabase,
    query: str,
    *,
    now: datetime,
    freshness_seconds: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("known player search limit is outside the supported range")
    escaped_query = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT player.normalized_tag, profile.name, profile.trophies,
                   player.current_observed_at,
                   player.eligibility_state,
                   profile.profile_json -> 'clan' ->> 'name'
            FROM players AS player
            JOIN player_profile_versions AS profile
                ON profile.id = player.current_profile_version_id
            WHERE profile.name ILIKE %s ESCAPE '\\'
              AND profile.source_contract_state = 'accepted'
            ORDER BY lower(profile.name), player.normalized_tag
            LIMIT %s
            """,
            (f"%{escaped_query}%", limit),
        ).fetchall()
        results = []
        for row in rows:
            observed_at = row[3].astimezone(UTC)
            age_seconds = max(
                0, int((now.astimezone(UTC) - observed_at).total_seconds())
            )
            results.append(
                {
                    "tag": _text(row[0]),
                    "name": _text(row[1]),
                    "clan": None if row[5] is None else _text(row[5]),
                    "trophies": int(row[2]),
                    "freshness": (
                        "fresh" if age_seconds <= freshness_seconds else "stale"
                    ),
                    "age_seconds": age_seconds,
                    "observed_at": observed_at.isoformat(),
                    "public_confidence": _public_confidence(True, _text(row[4])),
                }
            )
        return results


