from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import api_leaderboard, api_player_lookup
from .api_db import (
    PLAYER_SCREEN_READY_VERSION,
    ApiDatabase,
    _daily_log,
    _frozen_trophies_sql,
    _historical_season_summary,
    _json_array,
    _opening_day_battles_sql,
    _public_confidence,
    _screen_daily_log_with_events,
    _season_reset_waiting_sql,
    _text,
    _withhold_unsupported_entries,
)
from .domain import (
    SEASON_DURATION,
    DomainRuleError,
    awaits_season_reset,
    ranked_day_for,
    season_opening_reset,
    validate_legend_season_start,
)
from .season_summaries import reset_board_ranks, season_final_rank

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
        if not season_id.isdigit():
            return []
        filters.append("history.league_season_id = %s")
        parameters.append(
            str(int(season_id) + int(SEASON_DURATION.total_seconds()))
        )
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
        # League history names a Season by the Reset that ended it, which is
        # the next Season's start: the row for 5 Oct 2026 05:00 holds the
        # results of the Season that started 7 Sep 2026.
        try:
            season_end = validate_legend_season_start(
                _text(row[0]), observed_at=observed_at
            )
        except DomainRuleError:
            # Old rows predate reader-side validation. They stay retained as
            # evidence, but an invalid boundary never reaches a season page.
            continue
        season_start = season_end - SEASON_DURATION
        trophies = None if row[2] is None else int(row[2])
        placement = None if row[3] is None else int(row[3])
        valid.append(
            {
                "official_season_id": str(int(season_start.timestamp())),
                "observed_at": observed_at,
                "season_start": season_start,
                "season_end": season_end,
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
                   profile.profile_json -> 'clan' ->> 'name',
                   player.current_profile_confirmed_at,
                   profile.current_league_season_id,
                   {_frozen_trophies_sql("player.id", "%s")},
                   {_opening_day_battles_sql("player.id", "%s")},
                   player.id
            FROM players AS player
            JOIN player_profile_versions AS profile
                ON profile.id = player.current_profile_version_id
            {metadata_join}
            WHERE player.normalized_tag = %s
              AND profile.source_contract_state = 'accepted'
            """,
            (season_opening_reset(now), season_opening_reset(now), normalized_tag),
        ).fetchone()
        if row is None:
            return None
        observed_at = max(row[5], row[11] or row[5]).astimezone(UTC)
        # A profile naming an earlier Season, or still showing the frozen
        # pre-Reset trophies on a Season's first day, shows trophies from
        # before this player's Season reset, not their current Season total.
        season_reset_pending = awaits_season_reset(
            _text(row[12]), int(row[4]), row[13], row[14], now
        )
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
        history_updated_at = max(
            (day[19].astimezone(UTC) for day in daily_rows), default=None
        )
        public_confidence = _public_confidence(bool(row[1]), _text(row[2]))
        # The page never shows decoded armies; Copy army uses each event's
        # army_share_code, and the army routes read the saved decodes.
        screen_days = [
            _screen_daily_log_with_events(day, public_confidence, now)
            for day in map(_daily_log, daily_rows)
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
        # Send each day once: the Season's days are almost always among the
        # recent ones, so the response lists their starts instead.
        days = {day["ranked_day_start"]: day for day in screen_days}
        season_day_starts = []
        for season_row in season_rows:
            start = season_row[0].astimezone(UTC).isoformat()
            if start not in days:
                days[start] = _screen_daily_log_with_events(
                    _daily_log(season_row), public_confidence, now
                )
            season_day_starts.append(start)
        # Each day's Clash Lens rank on the frozen board saved at its closing
        # Reset; unknown until that board exists or when it omits the player.
        ranks = reset_board_ranks(
            connection,
            int(row[15]),
            [day["ranked_day_end"] for day in days.values() if day["ranked_day_end"]],
        )
        for day in days.values():
            end = day["ranked_day_end"]
            day["reset_rank"] = None if end is None else ranks.get(datetime.fromisoformat(end))
        data_quality = []
        if age_seconds > freshness_seconds:
            data_quality.append(
                {
                    "code": "stale",
                    "label": "Stale saved profile",
                    "detail": "The collector has not confirmed this player profile within the current freshness limit.",
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
        if season_reset_pending:
            data_quality.append(
                {
                    "code": "uncertain",
                    "label": "Waiting for this player's Season reset",
                    "detail": "The latest profile still shows trophies from the previous Season. The new total appears once the game reports this player's Season reset.",
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
            "season_reset_pending": season_reset_pending,
            "current_league_season_id": _text(row[12]),
            "eligibility": _text(row[2]),
            "active": bool(row[1]),
            "freshness": "fresh" if age_seconds <= freshness_seconds else "stale",
            "age_seconds": age_seconds,
            "coverage": "ranked_days" if daily_rows else "profile_only",
            "observed_at": observed_at.isoformat(),
            "battle_history_updated_at": (
                None if history_updated_at is None else history_updated_at.isoformat()
            ),
            "source_http_status": int(row[6]),
            "endpoint_version": _text(row[7]),
            "schema_version": _text(row[8]),
            "parser_version": _text(row[9]),
            "clan": None if row[10] is None else _text(row[10]),
            "public_confidence": public_confidence,
            "screen_ready": {
                "days": sorted(
                    days.values(), key=lambda day: day["ranked_day_start"], reverse=True
                ),
                "current_day_start": None
                if current_day is None
                else current_day["ranked_day_start"],
                "recent_day_starts": [day["ranked_day_start"] for day in screen_days],
                "season_day_starts": season_day_starts,
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


def player_cards(
    connection: Any,
    players: list[tuple[int, str, str | None, str | None]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Each player's current trophies, Live Leaderboard position and today's
    battles so far, read for every player at once.

    ``players`` holds (player id, tag, name, clan). Every card carries the
    lookup state and reason its own page explains, and the player's Live
    Leaderboard position whenever it is on the board. Trophies and today come
    only from an accepted current profile, including for a player who has left
    Legend I. While a newer profile goes unaccepted they stay unknown, so
    Season 0 trophies stay on that page alone.
    """
    ids = [player[0] for player in players]
    profiles = {
        int(row[0]): row[1:]
        for row in connection.execute(
            f"""
            SELECT player.id, profile.trophies, profile.current_league_season_id,
                   {_frozen_trophies_sql("player.id", "%s")},
                   {_opening_day_battles_sql("player.id", "%s")}
            FROM players AS player
            JOIN player_profile_versions AS profile
                ON profile.id = player.current_profile_version_id
            WHERE player.id = ANY(%s) AND profile.source_contract_state = 'accepted'
            """,
            (season_opening_reset(now), season_opening_reset(now), ids),
        ).fetchall()
    }
    today = {
        int(row[0]): _screen_daily_log_with_events(_daily_log(row[1:]), "high", now)
        for row in connection.execute(
            """
            SELECT DISTINCT ON (player_id)
                   player_id, ranked_day_start, ranked_day_end, official_season_id,
                   season_day_number, version, state, coverage, confidence,
                   attack_count, attack_three_star_count, attack_gain,
                   defense_count, defense_three_star_count, defense_loss,
                   net_trophy_change, adjustments, battles, partial_reasons, NULL
            FROM api_player_daily_logs
            WHERE player_id = ANY(%s) AND ranked_day_start = %s
            ORDER BY player_id, version DESC
            """,
            (ids, ranked_day_for(now).start),
        ).fetchall()
    }
    cards = []
    for player_id, tag, name, clan in players:
        lookup = api_player_lookup._lookup(connection, tag)
        profile = profiles.get(player_id)
        reason = lookup.get("reason")
        if lookup["state"] == "tracking" and reason is None and profile is None:
            reason = "pending"
        card: dict[str, Any] = {
            "tag": tag,
            "name": name,
            "clan": clan,
            "state": lookup["state"],
            "reason": reason,
            "trophies": None,
            "season_reset_pending": False,
            "rank": None,
            "today": None,
        }
        if reason is None and profile is not None:
            # As on the player page: an earlier Season's trophies are no total.
            pending = awaits_season_reset(
                _text(profile[1]), int(profile[0]), profile[2], profile[3], now
            )
            card["trophies"] = None if pending else int(profile[0])
            card["season_reset_pending"] = pending
            day = today.get(player_id)
            if day is not None:
                card["today"] = {
                    # The live day has no net change until it ends; the
                    # player page shows its battles' sum only once every
                    # battle so far is recorded.
                    "net": day["attack_gain"] - day["defense_loss"]
                    if day["battles_complete"]
                    else None,
                    "attacks": day["attack_count"],
                    "defenses": day["defense_count"],
                }
        cards.append(card)
    positions = api_leaderboard.live_positions(
        connection, [card["tag"] for card in cards], now=now
    )
    for card in cards:
        card["rank"] = positions.get(card["tag"])
    return cards


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
                   summary.published_at, summary.season_start,
                   summary.daily_entries
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
                "end_trophies": None
                if row[5] is None
                or 28 in _withhold_unsupported_entries(_json_array(row[8]))
                else int(row[5]),
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
    """Read tracked detail when retained, with honest official fallback.

    A summary stored before EOD movement was kept reports that movement
    and its evidence states as unknown.
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
        history_rows = _official_history_rows(
            connection, normalized_tag, season_id=official_season_id
        )
        history = history_rows[0] if history_rows else None
        if row is not None:
            columns = [d.name for d in cursor.description]
            record = dict(zip(columns, row))
            result = _historical_season_summary(record)
            ranks = reset_board_ranks(
                connection,
                int(record["player_id"]),
                [
                    entry["ranked_day_end"]
                    for entry in result["daily_entries"]
                    if entry.get("ranked_day_end")
                ],
            )
            for entry in result["daily_entries"]:
                for key in ("eod_state", "eod_change", "eod_change_state"):
                    entry.setdefault(key, None)
                end = entry.get("ranked_day_end")
                entry["reset_rank"] = None if not end else ranks.get(datetime.fromisoformat(end))
            # Summaries can be written before the Season's newest final board
            # is published; that board's rank wins once it exists.
            result["final_rank"] = season_final_rank(
                connection,
                int(record["player_id"]),
                record["season_end"],
                official_season_id,
                without_board=result["final_rank"],
            )
            result["source"] = "tracked_summary"
            result["official_history"] = (
                None if history is None else _official_history_payload(history)
            )
            return result
        if history is None:
            return None
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (normalized_tag,)
        ).fetchone()[0]
        final_rank = season_final_rank(connection, int(player_id), history["season_end"])
        return {
            "kind": "player-season-summary",
            "tag": normalized_tag,
            "official_season_id": official_season_id,
            "season_start": history["season_start"].isoformat(),
            "season_end": history["season_end"].isoformat(),
            "start_trophies": None,
            "end_trophies": history["eod_trophies"],
            "final_rank": final_rank,
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
            f"""
            -- Match current names before checking history. Without this boundary,
            -- PostgreSQL can scan and decode every player's daily battle JSON.
            WITH matches AS MATERIALIZED (
                SELECT player.id, player.normalized_tag, profile.name,
                       profile.trophies, player.current_observed_at,
                       player.eligibility_state, player.active,
                       profile.profile_json -> 'clan' ->> 'name' AS clan,
                       profile.current_league_season_id,
                       {_frozen_trophies_sql("player.id", "%(opening_reset)s")}
                           AS frozen_trophies,
                       {_opening_day_battles_sql("player.id", "%(opening_reset)s")}
                           AS day_battles,
                       {_season_reset_waiting_sql(
                           "player.id", "profile.trophies", "%(opening_reset)s"
                       )} AS opening_day_waiting
                FROM players AS player
                JOIN LATERAL (
                    SELECT name, trophies, source_contract_state, profile_json,
                           current_league_season_id
                    FROM player_profile_versions
                    WHERE id = player.current_profile_version_id
                    -- Keep indexed current-profile reads as old versions grow.
                    OFFSET 0
                ) AS profile ON true
                WHERE player.current_profile_version_id IS NOT NULL
                  AND profile.name ILIKE %(pattern)s ESCAPE '\\'
                  AND profile.source_contract_state = 'accepted'
            )
            SELECT player.normalized_tag, player.name, player.trophies,
                   player.current_observed_at, player.eligibility_state, player.clan,
                   player.current_league_season_id, player.frozen_trophies,
                   player.day_battles
            FROM matches AS player
            -- Scalar subqueries stop after one row and cannot become a hashed
            -- EXISTS subplan that reads the entire history table.
            WHERE (player.active
                   OR (SELECT true FROM api_player_daily_logs AS history
                       WHERE history.player_id = player.id
                         AND (NOT history.partial_reasons @> '["player_not_eligible"]'::jsonb
                              OR jsonb_array_length(history.battles) > 0)
                       LIMIT 1)
                   OR (SELECT true FROM player_season_summaries AS history
                       WHERE history.player_id = player.id LIMIT 1)
                   OR (SELECT true FROM player_league_history_entries AS history
                       WHERE history.player_id = player.id LIMIT 1))
            -- An exact name match first, then the strongest players.
            ORDER BY lower(player.name) = lower(%(query)s) DESC,
                     CASE WHEN player.current_league_season_id = %(season_id)s
                               AND NOT player.opening_day_waiting
                          THEN player.trophies END DESC NULLS LAST,
                     lower(player.name), player.normalized_tag
            LIMIT %(limit)s
            """,
            {
                "pattern": f"%{escaped_query}%",
                "query": query,
                "season_id": ranked_day_for(now).official_season_id,
                "opening_reset": season_opening_reset(now),
                "limit": limit,
            },
        ).fetchall()
        results = []
        for row in rows:
            observed_at = row[3].astimezone(UTC)
            age_seconds = max(
                0, int((now.astimezone(UTC) - observed_at).total_seconds())
            )
            pending = awaits_season_reset(
                _text(row[6]), int(row[2]), row[7], row[8], now
            )
            results.append(
                {
                    "tag": _text(row[0]),
                    "name": _text(row[1]),
                    "clan": None if row[5] is None else _text(row[5]),
                    "trophies": None if pending else int(row[2]),
                    "season_reset_pending": pending,
                    "freshness": (
                        "fresh" if age_seconds <= freshness_seconds else "stale"
                    ),
                    "age_seconds": age_seconds,
                    "observed_at": observed_at.isoformat(),
                    "public_confidence": _public_confidence(True, _text(row[4])),
                }
            )
        return results
