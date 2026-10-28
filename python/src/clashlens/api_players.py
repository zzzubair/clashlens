from __future__ import annotations

import heapq
import math
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from . import api_leaderboard, api_player_lookup
from .api_db import (
    PLAYER_SCREEN_READY_VERSION,
    ApiDatabase,
    SharedCopy,
    _daily_log,
    _frozen_trophies_sql,
    _historical_season_summary,
    _json_array,
    _opening_day_battles_sql,
    _public_confidence,
    _screen_daily_log_with_events,
    _season_reset_waiting_sql,
    _start_thread,
    _text,
    _withhold_unsupported_entries,
)
from .army_decoder import SIEGE_TROOP_IDS, DecodedArmy, decode_army_share_code
from .catalog import unit_label
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
        if not (season_id.isascii() and season_id.isdigit()):
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


def _season_weeks(
    connection: Any, player_id: int, season_start: datetime
) -> list[dict[str, Any]]:
    """The weekly league record's rows for the Season starting at
    ``season_start``, oldest first."""
    return [
        {
            "week_start": week_start.astimezone(UTC),
            "league": _text(league),
            "entry": None if entry is None else _text(entry),
            "decided_at": decided_at.astimezone(UTC),
        }
        for week_start, league, entry, decided_at in connection.execute(
            """
            SELECT week_start, league, entry, decided_at FROM player_league_weeks
            WHERE player_id = %s AND week_start >= %s AND week_start < %s
            ORDER BY week_start
            """,
            (player_id, season_start, season_start + SEASON_DURATION),
        ).fetchall()
    ]


def get_player_page(
    database: ApiDatabase,
    normalized_tag: str,
    *,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        metadata_join, metadata_columns = database._current_profile_metadata(connection)
        # A player who has not played yet this Season shows the newest
        # profile that ranks them at 5,000, as the Live Leaderboard does.
        unplayed = connection.execute(
            api_leaderboard.UNPLAYED_PROFILE_SQL,
            {**api_leaderboard._season_params(now), "tag": normalized_tag},
        ).fetchone()
        unplayed_id = None if unplayed is None else int(unplayed[0])
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
                   player.id,
                   -- The Live Leaderboard's rule: a profile check newer than
                   -- the last good one said the game has no such player.
                   (SELECT checked.last_not_found_at
                    FROM collector_response_state AS checked
                    WHERE checked.scope = 'player'
                      AND checked.identity_key = player.normalized_tag
                      AND checked.endpoint = 'profile'
                      AND (checked.last_success_at IS NULL
                           OR checked.last_not_found_at > checked.last_success_at)),
                   profile.profile_json -> 'clan' ->> 'tag'
            FROM players AS player
            JOIN player_profile_versions AS profile
                ON profile.id = COALESCE(%s::bigint, player.current_profile_version_id)
            {metadata_join}
            WHERE player.normalized_tag = %s
              AND (profile.source_contract_state = 'accepted'
                   OR profile.id = %s::bigint)
            """,
            (season_opening_reset(now), season_opening_reset(now), unplayed_id,
             normalized_tag, unplayed_id),
        ).fetchone()
        if row is None:
            return None
        observed_at = (
            max(row[5], row[11] or row[5]) if unplayed is None else unplayed[1]
        ).astimezone(UTC)
        not_found_at = None if row[16] is None else row[16].astimezone(UTC)
        # A profile naming an earlier Season, or still showing the frozen
        # pre-Reset trophies on a Season's first day, shows trophies from
        # before this player's Season reset, not their current Season total.
        season_reset_pending = unplayed_id is None and awaits_season_reset(
            _text(row[12]), int(row[4]), row[13], row[14], now
        )
        age_seconds = max(0, int((now.astimezone(UTC) - observed_at).total_seconds()))
        # The newest copy of each of the 28 latest days. The days come from the
        # index alone, so older copies and their battles are never read.
        daily_rows = connection.execute(
            """
            SELECT daily.ranked_day_start, daily.ranked_day_end,
                   daily.official_season_id, daily.season_day_number,
                   daily.version, daily.state, daily.coverage, daily.confidence,
                   daily.attack_count, daily.attack_three_star_count,
                   daily.attack_gain, daily.defense_count,
                   daily.defense_three_star_count, daily.defense_loss,
                   daily.net_trophy_change, daily.adjustments, daily.battles,
                   daily.partial_reasons, ranked_day.start_trophies,
                   ranked_day.input_evidence
                       #>> '{start_baseline_evidence,start_trophies_source}'
                       AS start_trophies_source,
                   daily.published_at
            FROM players AS player
            CROSS JOIN LATERAL (
                SELECT DISTINCT saved.ranked_day_start
                FROM api_player_daily_logs AS saved
                WHERE saved.player_id = player.id
                ORDER BY saved.ranked_day_start DESC
                LIMIT 28
            ) AS day
            CROSS JOIN LATERAL (
                SELECT * FROM api_player_daily_logs AS saved
                WHERE saved.player_id = player.id
                  AND saved.ranked_day_start = day.ranked_day_start
                ORDER BY saved.version DESC
                LIMIT 1
            ) AS daily
            LEFT JOIN ranked_day_versions AS ranked_day
                ON ranked_day.id = daily.ranked_day_version_id
            WHERE player.normalized_tag = %s
            ORDER BY daily.ranked_day_start DESC
            """,
            (normalized_tag,),
        ).fetchall()
        history_updated_at = max(
            (day[20].astimezone(UTC) for day in daily_rows), default=None
        )
        public_confidence = _public_confidence(bool(row[1]), _text(row[2]))
        # Events keep only the army_share_code: the page draws each army
        # from it, with army_units naming every unit once per page.
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
        calendar = ranked_day_for(now_utc)
        season_weeks = _season_weeks(connection, int(row[15]), calendar.season_start)
        newest_legend_week = max(
            (week for week in season_weeks if week["league"] == "legend_i"),
            key=lambda week: week["week_start"],
            default=None,
        )
        if season_context is None and current_day_raw is None and newest_legend_week:
            # A player who left Legend I before finishing any Season there has
            # no league history row and no Legend day now. The weekly league
            # record still says this Season held their Legend I days.
            season_context = {
                "id": calendar.official_season_id,
                "current_day_number": calendar.day_number,
                "start": calendar.season_start,
                "end": calendar.season_end,
                "anchor_source": "weekly_league_record",
                "anchor_observed_at": newest_legend_week["decided_at"],
            }
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
                "anchor_observed_at": current_day_raw[20].astimezone(UTC),
            }
        season_rows = []
        if (
            season_context is not None
            and not season_anchor_conflict
            and (len(daily_rows) < 28 or daily_rows[-1][0] <= season_context["start"])
        ):
            # The 28 latest days hold every day from the oldest of them on,
            # so here they hold all of the Season's: its days are these rows,
            # each already the newest copy, without reading them again.
            season_rows = sorted(
                (
                    day
                    for day in daily_rows
                    if season_context["start"] <= day[0] < season_context["end"]
                    and _text(day[2]) == season_context["id"]
                ),
                key=lambda day: (day[3] is not None, day[3] or 0, day[0]),
                reverse=True,
            )
        elif season_context is not None and not season_anchor_conflict:
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
                       start_trophies, start_trophies_source
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
                           ranked_day.start_trophies,
                           ranked_day.input_evidence
                           #>> '{start_baseline_evidence,start_trophies_source}'
                               AS start_trophies_source
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
        if not_found_at is not None:
            data_quality.append(
                {
                    "code": "unavailable",
                    "label": "Player not found",
                    "detail": "Clash of Clans did not find this player at its latest check, so they are left off the Live Leaderboard. The trophies shown are from the last check that found them, and saved Legend days stay as recorded.",
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
            "clan_tag": None if row[17] is None else _text(row[17]),
            # A missing player's saved profile is not current; saved days
            # keep their own confidence.
            "public_confidence": "uncertain" if not_found_at else public_confidence,
            "profile_not_found_at": (
                None if not_found_at is None else not_found_at.isoformat()
            ),
            "screen_ready": {
                "days": sorted(
                    days.values(), key=lambda day: day["ranked_day_start"], reverse=True
                ),
                "current_day_start": None
                if current_day is None
                else current_day["ranked_day_start"],
                "current_day_published_at": None
                if current_day_raw is None
                else current_day_raw[20].astimezone(UTC).isoformat(),
                "recent_day_starts": [day["ranked_day_start"] for day in screen_days],
                "season_day_starts": season_day_starts,
                # The calendar Season's weeks the weekly league record has
                # decided: a partial Season reads its Legend I weeks here.
                "season_weeks": [
                    {
                        "week_start": week["week_start"].isoformat(),
                        "league": week["league"],
                        "entry": week["entry"],
                    }
                    for week in season_weeks
                ],
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
                "army_units": army_units(
                    event.get("army_share_code")
                    for day in days.values()
                    for event in (*day["offense_events"], *day["defense_events"])
                ),
                "provenance": {
                    "source": "api_player_daily_logs",
                    "observed_at": observed_at.isoformat(),
                    "freshness": "fresh"
                    if age_seconds <= freshness_seconds
                    else "stale",
                    "confidence": (
                        "uncertain" if not_found_at else public_confidence
                    ),
                    "coverage": (
                        current_day["completeness"]["state"]
                        if current_day is not None
                        else "missing"
                    ),
                    "version": PLAYER_SCREEN_READY_VERSION,
                },
            },
        }


def army_units(codes: Iterable[str | None]) -> dict[str, dict[str, Any]]:
    """Each unit in these Copy army codes' armies, by its saved id: its name
    and whether the army shows it as a siege machine, as the decoder sorts
    them."""
    ids: set[str] = set()
    for code in codes:
        army = decode_army_share_code(code)
        if not isinstance(army, DecodedArmy):
            continue
        for fact in (*army.home_troops, *army.cc_troops, *army.spells, *army.siege):
            ids.add(fact.typed_id)
        for hero in army.heroes:
            ids.update((hero.hero_typed_id, *hero.equipment_typed_ids))
            if hero.pet_typed_id is not None:
                ids.add(hero.pet_typed_id)
    return {
        typed_id: {"name": unit_label(typed_id), "siege": typed_id in SIEGE_TROOP_IDS}
        for typed_id in sorted(ids)
    }


def left_legend_i(
    connection: Any, player_ids: list[int], now: datetime
) -> dict[int, dict[str, Any]]:
    """For each player who left Legend I this Season, at the Monday Reset
    after their newest Legend I day the weekly league record put them below
    it: that Reset, their last Legend I day's number and the total it ended
    on, which no weekly raise follows."""
    if not player_ids:
        return {}
    season = ranked_day_for(now.astimezone(UTC))
    return {
        int(player_id): {
            "left_at": left_at.astimezone(UTC).isoformat(),
            "last_day_number": None if day_number is None else int(day_number),
            "trophies": None if trophies is None else int(trophies),
        }
        for player_id, left_at, day_number, trophies in connection.execute(
            """
            SELECT DISTINCT ON (day.player_id)
                   day.player_id, day.ranked_day_end, day.season_day_number,
                   version.final_trophies_before_reset
            FROM api_player_daily_logs AS day
            JOIN player_league_weeks AS week
                ON week.player_id = day.player_id
               AND week.week_start = day.ranked_day_end
               AND week.league = 'below_legend_i'
            LEFT JOIN ranked_day_versions AS version
                ON version.id = day.ranked_day_version_id
            WHERE day.player_id = ANY(%s)
              AND day.ranked_day_start >= %s AND day.ranked_day_end <= %s
            ORDER BY day.player_id, day.ranked_day_start DESC, day.version DESC
            """,
            (player_ids, season.season_start, now),
        ).fetchall()
    }


def player_cards(
    connection: Any,
    players: list[tuple[int, str, str | None, str | None, str | None]],
    *,
    now: datetime,
    ranks: bool = True,
) -> list[dict[str, Any]]:
    """Each player's current trophies, Live Leaderboard position and today's
    battles so far, read for every player at once. Without ``ranks`` the
    caller fills the positions with ``rank_cards``.

    ``players`` holds (player id, tag, name, clan, league). Every card carries the
    lookup state and reason its own page explains, and the player's Live
    Leaderboard position whenever it is on the board, and the league its newest
    saved profile names. For a player outside Legend I, trophies come from its
    newest saved profile. Otherwise trophies and today come only from an
    accepted current profile. While a newer profile goes unaccepted they stay
    unknown, so a Legend I player's Season 0 trophies stay on its page alone.
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
                   net_trophy_change, adjustments, battles, partial_reasons, NULL, NULL
            FROM api_player_daily_logs
            WHERE player_id = ANY(%s) AND ranked_day_start = %s
            ORDER BY player_id, version DESC
            """,
            (ids, ranked_day_for(now).start),
        ).fetchall()
    }
    left = left_legend_i(connection, ids, now)
    cards = []
    for player_id, tag, name, clan, league in players:
        lookup = api_player_lookup._lookup(connection, tag, now)
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
            "league": league,
            "today": None,
        }
        if lookup["state"] in ("not_in_legend", "uncertain"):
            # Off the board, the newest saved profile's trophies, as on its page.
            card["trophies"] = lookup["profile"]["trophies"]
            # And this Season's Legend I part, when the player left it.
            card["left_legend_i"] = left.get(player_id)
        elif reason is None and profile is not None:
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
    if ranks:
        rank_cards(
            cards, lambda tags: api_leaderboard.live_positions(connection, tags, now=now)
        )
    return cards


def rank_cards(
    cards: list[dict[str, Any]], positions: Callable[[list[str]], dict[str, int]]
) -> None:
    """Each card's Live Leaderboard rank from ``positions``, asked only for
    the players tracked in Legend I now, and not at all without one: only
    they are on the board, and a rank read before a player left stays off."""
    tags = [card["tag"] for card in cards if card["state"] == "tracking"]
    found = positions(tags) if tags else {}
    for card in cards:
        card["rank"] = found.get(card["tag"])


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
            # Which weeks were Legend I, so a Season the player left or
            # joined partway counts only its Legend I days as missing.
            result["season_weeks"] = [
                {
                    "week_start": week["week_start"].isoformat(),
                    "league": week["league"],
                    "entry": week["entry"],
                }
                for week in _season_weeks(
                    connection,
                    int(record["player_id"]),
                    datetime.fromtimestamp(int(official_season_id), UTC),
                )
            ]
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


def _known_players_sql(opening_reset: datetime | None, name_filter: str) -> str:
    """Every known player a name search can find, matched by ``name_filter``."""
    # Off a Season's first Legend day no profile awaits its Season reset for
    # its trophies alone, so those per-player reads are left out.
    opening = (
        f"""{_frozen_trophies_sql("player.id", "%(opening_reset)s")}
                   AS frozen_trophies,
               {_opening_day_battles_sql("player.id", "%(opening_reset)s")}
                   AS day_battles,
               {_season_reset_waiting_sql(
                   "player.id", "profile.trophies", "%(opening_reset)s"
               )} AS opening_day_waiting"""
        if opening_reset is not None
        else """NULL::integer AS frozen_trophies, NULL::integer[] AS day_battles,
               false AS opening_day_waiting"""
    )
    return f"""
        -- Match current names before checking history. Without this boundary,
        -- PostgreSQL can scan and decode every player's daily battle JSON.
        WITH matches AS MATERIALIZED (
            SELECT player.id, player.normalized_tag, profile.name,
                   profile.trophies, player.current_observed_at,
                   player.eligibility_state, player.active,
                   profile.profile_json -> 'clan' ->> 'name' AS clan,
                   profile.current_league_season_id, {opening}
            FROM players AS player
            JOIN LATERAL (
                SELECT name, trophies, source_contract_state, profile_json,
                       current_league_season_id
                FROM player_profile_versions
                WHERE id = player.current_profile_version_id
                -- Keep indexed current-profile reads as old versions grow.
                OFFSET 0
            ) AS profile ON true
            WHERE player.current_profile_version_id IS NOT NULL{name_filter}
              AND profile.source_contract_state = 'accepted'
        )
        SELECT player.normalized_tag, player.name, player.trophies,
               player.current_observed_at, player.eligibility_state, player.clan,
               player.current_league_season_id, player.frozen_trophies,
               player.day_battles, player.opening_day_waiting
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
    """


def _lower(name: str) -> str:
    """``name`` lower-cased one character at a time, as PostgreSQL's lower()
    and ILIKE do: a capital sigma always becomes σ, never ς, and İ becomes i."""
    return "".join(character.lower()[0] for character in name)


def _read_known_players(
    database: ApiDatabase, now: datetime
) -> tuple[str, list[tuple[Any, ...]]]:
    """The Season ``now`` is in, and every player a name search can find,
    each with its place in name order and its name lower-cased."""
    opening_reset = season_opening_reset(now)
    with database.pool.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT known.*,
                   row_number() OVER (ORDER BY lower(known.name), known.normalized_tag)
            FROM ({_known_players_sql(opening_reset, "")}) AS known
            """,
            {"opening_reset": opening_reset},
        ).fetchall()
    season_id = ranked_day_for(now).official_season_id
    return season_id, [(*row, _lower(_text(row[1]))) for row in rows]


class KnownPlayerNames(SharedCopy[tuple[str, list[tuple[Any, ...]]]]):
    """Every player the header search can find, held so a search reads no
    database: read again in the background after a minute, and still served,
    across Resets too, while those reads fail or run late, but never past a
    day. A full read can outlast the statement timeout under the load after
    a Reset, so no search waits for one while any copy is held."""

    def __init__(
        self,
        seconds: float = 60,
        *,
        clock: Callable[[], float] = time.monotonic,
        start: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        super().__init__(
            _read_known_players,
            # One key for every Legend day: a search hides the trophies of a
            # copy read in an earlier Season instead of waiting for a new one.
            key=lambda _now: None,
            seconds=seconds,
            longest=24 * 60 * 60,
            name="known_player_names_copy",
            clock=clock,
            start=start,
        )


def _waits(row: tuple[Any, ...], now: datetime, earlier_season: bool) -> bool:
    """Whether a known player's trophies wait for their Season reset. Every
    one held from a copy read before this Season's opening Reset does: that
    copy cannot show whose Season reset has come."""
    return earlier_season or awaits_season_reset(
        _text(row[6]), int(row[2]), row[7], row[8], now
    )


def search_known_players(
    database: ApiDatabase,
    query: str,
    *,
    now: datetime,
    freshness_seconds: int,
    limit: int = 50,
    names: KnownPlayerNames | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("known player search limit is outside the supported range")
    season_id = ranked_day_for(now).official_season_id
    earlier_season = False
    if names is None:
        opening_reset = season_opening_reset(now)
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        name_filter = "\n              AND profile.name ILIKE %(pattern)s ESCAPE '\\'"
        with database.pool.connection() as connection:
            rows = connection.execute(
                f"""{_known_players_sql(opening_reset, name_filter)}
                -- An exact name match first, then the strongest players.
                ORDER BY lower(player.name) = lower(%(query)s) DESC,
                         CASE WHEN player.current_league_season_id = %(season_id)s
                                   AND NOT player.opening_day_waiting
                              THEN player.trophies END DESC NULLS LAST,
                         lower(player.name), player.normalized_tag
                LIMIT %(limit)s
                """,
                {
                    "pattern": f"%{escaped}%",
                    "query": query,
                    "season_id": season_id,
                    "opening_reset": opening_reset,
                    "limit": limit,
                },
            ).fetchall()
    else:
        needle = _lower(query)
        read_season, known = names.current(database, now)
        earlier_season = read_season != season_id

        def order(row: tuple[Any, ...]) -> tuple[Any, ...]:
            # An exact name match first, then the strongest players, then by name.
            ranked = row[2] is not None and not _waits(row, now, earlier_season)
            return (row[11] != needle, -row[2] if ranked else math.inf, row[10])

        rows = heapq.nsmallest(
            limit, (row for row in known if needle in row[11]), key=order
        )
    results = []
    for row in rows:
        observed_at = row[3].astimezone(UTC)
        age_seconds = max(0, int((now.astimezone(UTC) - observed_at).total_seconds()))
        pending = _waits(row, now, earlier_season)
        results.append(
            {
                "tag": _text(row[0]),
                "name": _text(row[1]),
                "clan": None if row[5] is None else _text(row[5]),
                "trophies": None if pending else int(row[2]),
                "season_reset_pending": pending,
                "freshness": "fresh" if age_seconds <= freshness_seconds else "stale",
                "age_seconds": age_seconds,
                "observed_at": observed_at.isoformat(),
                "public_confidence": _public_confidence(True, _text(row[4])),
            }
        )
    return results
