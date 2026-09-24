from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime, timedelta
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
from .domain import SEASON_DURATION, DomainRuleError, validate_legend_season_start

_LEGEND_I_TIER_ID = 105000036


def _official_history_rows(
    connection: Any,
    normalized_tag: str,
    *,
    season_id: str | None = None,
) -> list[dict[str, Any]]:
    filters = ["player.normalized_tag = %s", "history.league_tier_id = %s"]
    parameters: list[Any] = [normalized_tag, _LEGEND_I_TIER_ID]
    if season_id is not None:
        filters.append("history.league_season_id = %s")
        parameters.append(season_id)
    rows = connection.execute(
        f"""
        SELECT history.league_season_id, history.observed_at,
               history.league_trophies, history.placement
        FROM player_league_history_entries AS history
        JOIN players AS player ON player.id = history.player_id
        WHERE {" AND ".join(filters)}
        ORDER BY history.league_season_id DESC
        """,
        parameters,
    ).fetchall()
    valid = []
    for row in rows:
        observed_at = row[1].astimezone(UTC)
        try:
            season_start = validate_legend_season_start(
                _text(row[0]), observed_at=observed_at
            )
        except DomainRuleError:
            # Old rows predate reader-side validation. They stay retained as
            # evidence, but an invalid boundary never reaches a season page.
            continue
        if season_start + SEASON_DURATION > observed_at:
            # Official league history describes completed seasons. A row for
            # the season still in progress cannot be presented as its EOD.
            continue
        trophies = None if row[2] is None else int(row[2])
        placement = None if row[3] is None else int(row[3])
        valid.append(
            {
                "official_season_id": _text(row[0]),
                "observed_at": observed_at,
                "season_start": season_start,
                "season_end": season_start + SEASON_DURATION,
                "eod_trophies": trophies
                if trophies is not None and trophies >= 0
                else None,
                "final_placement": (
                    placement if placement is not None and placement >= 1 else None
                ),
            }
        )
    return valid


def _official_history_payload(history: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "official_league_history",
        "observed_at": history["observed_at"].isoformat(),
        "eod_trophies": history["eod_trophies"],
        "final_placement": history["final_placement"],
    }


def _current_history_season(
    connection: Any, normalized_tag: str, now: datetime
) -> dict[str, Any] | None:
    rows = _official_history_rows(connection, normalized_tag)
    if not rows:
        return None
    latest = max(rows, key=lambda item: item["season_start"])
    now_utc = now.astimezone(UTC)
    first_possible_current = latest["season_end"]
    if now_utc < first_possible_current:
        return None
    elapsed_seasons = (now_utc - first_possible_current) // SEASON_DURATION
    season_start = first_possible_current + elapsed_seasons * SEASON_DURATION
    return {
        "id": str(int(season_start.timestamp())),
        "current_day_number": int((now_utc - season_start) // timedelta(days=1)) + 1,
        "start": season_start,
        "end": season_start + SEASON_DURATION,
        "anchor_source": "official_league_history",
        "anchor_observed_at": latest["observed_at"],
    }


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
        age_seconds = max(0, int((now.astimezone(UTC) - observed_at).total_seconds()))
        daily_rows = connection.execute(
            """
            SELECT ranked_day_start, ranked_day_end, official_season_id,
                   season_day_number, version, state, coverage, confidence,
                   attack_count, attack_three_star_count, attack_gain,
                   defense_count, defense_three_star_count, defense_loss,
                   net_trophy_change, adjustments, battles, partial_reasons,
                   start_trophies, published_at
            FROM (
                SELECT DISTINCT ON (daily.ranked_day_start)
                       daily.ranked_day_start, daily.ranked_day_end,
                       daily.official_season_id, daily.season_day_number,
                       daily.version, daily.state, daily.coverage, daily.confidence,
                       daily.attack_count, daily.attack_three_star_count,
                       daily.attack_gain, daily.defense_count,
                       daily.defense_three_star_count, daily.defense_loss,
                       daily.net_trophy_change, daily.adjustments, daily.battles,
                       daily.partial_reasons, ranked_day.start_trophies,
                       daily.published_at
                FROM api_player_daily_logs AS daily
                LEFT JOIN ranked_day_versions AS ranked_day
                    ON ranked_day.id = daily.ranked_day_version_id
                WHERE daily.player_id = (
                    SELECT id FROM players WHERE normalized_tag = %s
                )
                ORDER BY daily.ranked_day_start DESC, daily.version DESC
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
        armies = {(str(row[0]), _text(row[1])): _public_army(row) for row in army_rows}
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
                and raw_day[0].astimezone(UTC) <= now_utc < raw_day[1].astimezone(UTC)
            ),
            None,
        )
        current_day_raw = None if current_day_pair is None else current_day_pair[0]
        current_day = None if current_day_pair is None else current_day_pair[1]
        season_context = _current_history_season(connection, normalized_tag, now_utc)
        season_anchor_conflict = False
        if season_context is not None and current_day_raw is not None:
            season_anchor_conflict = (
                current_day_raw[2] is None
                or current_day_raw[3] is None
                or _text(current_day_raw[2]) != season_context["id"]
                or int(current_day_raw[3]) != season_context["current_day_number"]
            )
        if season_context is None and (
            current_day_raw is not None
            and current_day_raw[2] is not None
            and current_day_raw[3] is not None
        ):
            season_start = current_day_raw[0].astimezone(UTC) - timedelta(
                days=int(current_day_raw[3]) - 1
            )
            season_context = {
                "id": _text(current_day_raw[2]),
                "current_day_number": int(current_day_raw[3]),
                "start": season_start,
                "end": season_start + SEASON_DURATION,
                "anchor_source": "daily_publication",
                "anchor_observed_at": current_day_raw[19].astimezone(UTC),
            }
        season_rows = []
        if season_context is not None and not season_anchor_conflict:
            # A season is exactly 28 ranked days. Bound this read to the
            # confirmed season while retaining the latest frozen
            # publication for each ranked-day start. Filtering before the
            # bound prevents previous-season rows from filling the result.
            season_rows = connection.execute(
                """
                SELECT ranked_day_start, ranked_day_end, official_season_id,
                       season_day_number, version, state, coverage, confidence,
                       attack_count, attack_three_star_count, attack_gain,
                       defense_count, defense_three_star_count, defense_loss,
                       net_trophy_change, adjustments, battles, partial_reasons,
                       start_trophies
                FROM (
                    SELECT DISTINCT ON (daily.ranked_day_start)
                           daily.ranked_day_start, daily.ranked_day_end,
                           daily.official_season_id, daily.season_day_number,
                           daily.version, daily.state, daily.coverage,
                           daily.confidence, daily.attack_count,
                           daily.attack_three_star_count, daily.attack_gain,
                           daily.defense_count, daily.defense_three_star_count,
                           daily.defense_loss, daily.net_trophy_change,
                           daily.adjustments, daily.battles, daily.partial_reasons,
                           ranked_day.start_trophies
                    FROM api_player_daily_logs AS daily
                    LEFT JOIN ranked_day_versions AS ranked_day
                        ON ranked_day.id = daily.ranked_day_version_id
                    WHERE daily.player_id = (
                        SELECT id FROM players WHERE normalized_tag = %s
                    )
                      AND daily.ranked_day_start >= %s
                      AND daily.ranked_day_start < %s
                    ORDER BY daily.ranked_day_start DESC, daily.version DESC
                ) AS latest_days
                WHERE official_season_id = %s
                ORDER BY season_day_number DESC NULLS LAST,
                         ranked_day_start DESC
                LIMIT 28
                """,
                (
                    normalized_tag,
                    season_context["start"],
                    season_context["end"],
                    season_context["id"],
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
        if season_anchor_conflict:
            data_quality.append(
                {
                    "code": "uncertain",
                    "label": "Season boundary conflict",
                    "detail": "The official league history and ranked-day publication disagree, so season days are withheld.",
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
                if season_context is None or season_anchor_conflict
                else {
                    "id": season_context["id"],
                    "current_day_number": season_context["current_day_number"],
                    "start": season_context["start"].isoformat(),
                    "end": season_context["end"].isoformat(),
                    "anchor_source": season_context["anchor_source"],
                    "anchor_observed_at": season_context[
                        "anchor_observed_at"
                    ].isoformat(),
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


def list_player_seasons(
    database: ApiDatabase, normalized_tag: str
) -> list[dict[str, Any]]:
    """List compact summaries plus official history-only seasons."""
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT summary.official_season_id, summary.coverage_state,
                   summary.days_observed, summary.days_missing,
                   summary.start_trophies, summary.end_trophies,
                   summary.published_at, summary.season_start
            FROM player_season_summaries AS summary
            JOIN players AS player ON player.id = summary.player_id
            WHERE player.normalized_tag = %s
            """,
            (normalized_tag,),
        ).fetchall()
        seasons = {
            _text(row[0]): {
                "official_season_id": _text(row[0]),
                "coverage_state": _text(row[1]),
                "days_observed": int(row[2]),
                "days_missing": int(row[3]),
                "start_trophies": None if row[4] is None else int(row[4]),
                "end_trophies": None if row[5] is None else int(row[5]),
                "published_at": row[6].astimezone(UTC).isoformat(),
                "source": "tracked_summary",
                "official_history": None,
                "_sort_start": row[7],
            }
            for row in rows
        }
        for history in _official_history_rows(connection, normalized_tag):
            season_id = history["official_season_id"]
            if season_id in seasons:
                seasons[season_id]["official_history"] = _official_history_payload(
                    history
                )
                continue
            seasons[season_id] = {
                "official_season_id": season_id,
                "coverage_state": "partial",
                "days_observed": 0,
                "days_missing": 28,
                "start_trophies": None,
                "end_trophies": history["eod_trophies"],
                "published_at": None,
                "source": "official_league_history",
                "official_history": _official_history_payload(history),
                "_sort_start": history["season_start"],
            }
        ordered = sorted(
            seasons.values(),
            key=lambda season: (
                season["_sort_start"] is None,
                season["_sort_start"] or datetime.max.replace(tzinfo=UTC),
                season["official_season_id"],
            ),
        )
        for season in ordered:
            del season["_sort_start"]
        return ordered


def get_player_season_summary(
    database, normalized_tag: str, official_season_id: str
) -> dict[str, Any] | None:
    """Read tracked detail when retained, with honest official fallback."""
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
        history_rows = _official_history_rows(
            connection, normalized_tag, season_id=official_season_id
        )
        history = history_rows[0] if history_rows else None
        if row is not None:
            columns = [d.name for d in cursor.description]
            record = dict(zip(columns, row))
            result = _historical_season_summary(record)
            result["source"] = "tracked_summary"
            result["official_history"] = (
                None if history is None else _official_history_payload(history)
            )
            return result
        if history is None:
            return None
        return {
            "kind": "player-season-summary",
            "tag": normalized_tag,
            "official_season_id": official_season_id,
            "season_start": history["season_start"].isoformat(),
            "season_end": history["season_end"].isoformat(),
            "start_trophies": None,
            "end_trophies": history["eod_trophies"],
            "final_rank": history["final_placement"],
            "attack_count": None,
            "attack_gain": None,
            "attack_three_star_count": None,
            "defense_count": None,
            "defense_loss": None,
            "defense_three_star_count": None,
            "net_trophy_change": None,
            "attack_stars": {str(star): None for star in range(4)},
            "attack_stars_unknown": None,
            "defense_stars": {str(star): None for star in range(4)},
            "defense_stars_unknown": None,
            "days_observed": 0,
            "days_missing": 28,
            "missing_days": list(range(1, 29)),
            "coverage_state": "partial",
            "unresolved_flags": ["tracked_day_detail_unavailable"],
            "daily_entries": [],
            "projection_version": "official-league-history-fallback-v1",
            "published_at": None,
            "source": "official_league_history",
            "official_history": _official_history_payload(history),
        }


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
    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
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
