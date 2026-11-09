"""Read-time membership and battle statistics, ported from the website.

The three reference groups keep their original order and independent start
inference. Nothing in these projections changes saved day evidence.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from .api_player_presentation import day_presentation, display_end, display_starts
from .domain import (
    RANKED_DAY_DURATION,
    SEASON_DAY_COUNT,
    SEASON_START_TROPHIES,
    ranked_day_for,
)

# Day and week starts repeat across a page's three day lists and every
# request, so each string is parsed once.
_at = lru_cache(maxsize=4096)(datetime.fromisoformat)


def shape_of(start: datetime, today: int, weeks: list[dict], saved: dict[int, dict], *, historical: bool = False) -> dict[str, Any]:
    unplayed = {
        number for number, kept in saved.items()
        if kept.get("attack_count") == 0 and kept.get("defense_count") == 0
        and kept.get("net_change" if historical else "net_trophy_change") == 0
        and (kept.get("start_trophies") if historical else kept["display_start"]["trophies"]) == SEASON_START_TROPHIES
        and (historical or not kept["offense_events"] and not kept["defense_events"])
    }
    return _membership(start, today, weeks, {number: kept["ranked_day_start"] for number, kept in saved.items()}, unplayed)


def _membership(start: datetime, today: int, weeks: list[dict], saved: dict[int, str], unplayed: set[int]) -> dict[str, Any]:
    by_week = {_at(week["week_start"]): week for week in weeks}

    def week_of(number):
        return by_week.get(start + ((number - 1) // 7 * 7) * RANKED_DAY_DURATION, {})

    kinds = [
        "ahead" if number > today else "outside" if week_of(number).get("league") == "below_legend_i"
        else "legend" if week_of(number).get("league") == "legend_i" or number in saved
        else "unrecorded"
        for number in range(1, SEASON_DAY_COUNT + 1)
    ]
    runs = []
    number = 1
    while number <= min(today, SEASON_DAY_COUNT):
        if kinds[number - 1] != "legend":
            number += 1
            continue
        first = number
        while number < min(today, SEASON_DAY_COUNT) and kinds[number] == "legend":
            number += 1
        signed_up = None
        for candidate in range(first, number + 1):
            if candidate in saved and candidate not in unplayed:
                signed_up = candidate
                break
            if candidate in saved and candidate < today:
                kinds[candidate - 1] = "waiting"
        runs.append({
            "first": first, "last": number, "signed_up": signed_up,
            "promoted": week_of(first).get("entry") in ("promoted", "first_seen"),
            "dropped": number < SEASON_DAY_COUNT and kinds[number] == "outside",
        })
        number += 1
    expected = saved_count = 0
    last_legend = None
    for number in range(1, min(today, SEASON_DAY_COUNT + 1)):
        if kinds[number - 1] != "legend":
            continue
        expected += 1
        if number in saved:
            saved_count += 1
            last_legend = saved[number]
    if runs and runs[-1]["dropped"] and runs[-1]["signed_up"] is None:
        last_legend = saved.get(runs[-1]["last"], last_legend)
    return {
        "season_start": start.isoformat(), "today": today, "kinds": kinds, "runs": runs,
        "partial": any(kind in ("outside", "waiting") for kind in kinds),
        "outside_now": today <= SEASON_DAY_COUNT and kinds[today - 1] == "outside",
        "legend_days": {"expected": expected, "saved": saved_count},
        "last_legend_day_start": last_legend,
    }


def heatmap_day_numbers(start: datetime, today: int, weeks: list[dict], days: list[list]) -> list[int]:
    """Public-profile membership from its compact [day, ..., unplayed, listed] tuples.

    Use the same runs and pre-sign-up days as D6, but return only the final
    selection: 500 linked players need no full player presentation sections.
    """
    shape = _membership(
        start, today, weeks,
        {kept[0]: (start + (kept[0] - 1) * RANKED_DAY_DURATION).isoformat() for kept in days},
        {kept[0] for kept in days if kept[4]},
    )
    return [kept[0] for kept in days if kept[5] and shape["kinds"][kept[0] - 1] not in ("outside", "waiting")]


def historical_statistics(summary: dict[str, Any]) -> dict[str, Any]:
    """Historical card averages, preserving the compact-detail denominator.

    Retired daily detail is absent, so its unknown quiet days remain in the
    saved observed-day denominator. Do not infer them from Season membership.
    """
    def side(key, count_key, trophies_key):
        count, trophies = summary[count_key], summary[trophies_key]
        played = summary["days_observed"] - sum(entry.get(key) == 0 for entry in summary["daily_entries"])
        return {
            "per_day": None if trophies is None or played == 0 else trophies / played,
            "per_battle": None if trophies is None or count in (None, 0) else trophies / count,
        }

    return {"attack": side("attack_count", "attack_count", "attack_gain"),
            "defense": side("defense_count", "defense_count", "defense_loss")}


def historical_facts(summary: dict[str, Any], *, season_start: datetime | None = None) -> None:
    summary["summary_statistics"] = historical_statistics(summary)
    saved = {}
    for entry in summary["daily_entries"]:
        entry["presentation"] = day_presentation(entry, is_current=False)
        number = entry.get("season_day_number")
        if number is not None:
            saved.setdefault(number, entry)
    start = season_start or (_at(summary["season_start"]) if summary.get("season_start") else None)
    summary["season_shape"] = None if start is None else shape_of(
        start, SEASON_DAY_COUNT + 1, summary.get("season_weeks", []), saved,
        historical=True,
    )


def battle_statistics(recent: list[dict], season: list[dict], start: datetime, now: datetime, shape: dict | None) -> dict[str, Any]:
    today = ranked_day_for(now).start
    ordered = [*season, *recent]

    def day_from(at):
        return next((day for day in ordered if _at(day["ranked_day_start"]) == at), None) if at >= start else None

    left = shape["last_legend_day_start"] if shape and shape["outside_now"] else None
    latest_at = _at(left) if left else today - RANKED_DAY_DURATION
    latest = day_from(latest_at)
    previous = day_from(latest_at - RANKED_DAY_DURATION) if not left and (latest is None or latest.get("reset_rank") is None) else None
    selected = previous if previous and previous.get("reset_rank") is not None else latest
    reset_start = None if selected is None else selected["ranked_day_start"]
    result = {}
    for period in ("7", "14", "season"):
        first = start if period == "season" else max(start, today - int(period) * RANKED_DAY_DURATION)
        days = {_at(day["ranked_day_start"]): day for day in [*recent, *season]
                if first <= _at(day["ranked_day_start"]) < today}

        def side(key, days=days, first=first):
            events = {}
            played = 0
            for _, day in sorted(days.items()):
                before = len(events)
                for event in day[key]:
                    at = _at(event["battle_timestamp"])
                    if at < first or at > now or event["battle_id"] in events:
                        continue
                    events[event["battle_id"]] = event
                if len(events) > before:
                    played += 1
            stars = [0, 0, 0, 0]
            trophies = 0
            for event in events.values():
                stars[event["stars"]] += 1
                trophies += abs(event["trophy_change"])
            return {
                "count": len(events), "stars": stars, "trophies": trophies, "played_days": played,
                "per_day": trophies / played if played else None,
                "per_battle": trophies / len(events) if events else None,
                "disputed": any(event.get("perspective_disagreement") for event in events.values()),
            }

        saved_count = len(days)
        expected_count = (today - first) // RANKED_DAY_DURATION
        incomplete = any(not day["battles_complete"] for day in days.values())
        # BattleStatistics's Season summary leaves days outside Legend I,
        # and the known wait before sign-up, out of its missing-day warning.
        if period == "season" and shape and shape.get("partial"):
            saved_count = shape["legend_days"]["saved"]
            expected_count = shape["legend_days"]["expected"]
            incomplete = any(
                not day["battles_complete"]
                for day in ordered
                if 1 <= (number := (_at(day["ranked_day_start"]) - start) // RANKED_DAY_DURATION + 1) < shape["today"]
                and number <= SEASON_DAY_COUNT and shape["kinds"][number - 1] == "legend"
            )
        result[period] = {
            "start": first.isoformat(), "end": today.isoformat(), "days_saved": saved_count,
            "days_expected": expected_count,
            "incomplete": incomplete,
            "attack": side("offense_events"), "defense": side("defense_events"),
            "reset_day_start": reset_start,
        }
    return result


def trophy_trends(recent: list[dict], start: datetime, now: datetime) -> dict | None:
    """The deferred trend card's complete-day totals, separate from battle averages."""
    from .api_player_presentation import screen_event_timestamp

    today = ranked_day_for(now).start
    finished = (today - start) // RANKED_DAY_DURATION
    if finished <= 0:
        return None
    saved = {day["ranked_day_start"]: day for day in recent}
    counted = []
    for day in saved.values():
        at = screen_event_timestamp(day["ranked_day_start"])
        end = screen_event_timestamp(day["ranked_day_end"])
        if (at is not None and at < today and ranked_day_for(at).start == at
                and end == at + RANKED_DAY_DURATION
                and day["completeness"]["state"] == "complete" and day["net_trophy_change"] is not None):
            counted.append((at, day["net_trophy_change"]))
    result = {}
    for window in (7, 14):
        span = min(window, finished)
        values = [net for at, net in counted if today - span * RANKED_DAY_DURATION <= at]
        result[str(window)] = {"span": span, "counted": len(values), "total": sum(values) if values else None}
    return result


def player_facts(page: dict[str, Any], now: datetime, *, battles: bool) -> tuple[list[dict], list[dict], dict | None]:
    ready = page["screen_ready"]
    by_start = {day["ranked_day_start"]: day for day in ready["days"]}
    groups = [
        [dict(by_start[start]) for start in ready["season_day_starts"]],
        [dict(by_start[start]) for start in ready["recent_day_starts"]],
        [] if ready["current_day_start"] is None else [dict(by_start[ready["current_day_start"]])],
    ]
    for group in groups:
        display_starts(group, page)
    season, recent, current = groups
    saved = [*season, *current, *recent]
    # Copies of one day differ only in their start, so each day's
    # presentation is worked out once.
    presented: dict[tuple[str, bool], dict[str, Any]] = {}

    def presentation(day: dict[str, Any], is_current: bool) -> dict[str, Any]:
        key = (day["ranked_day_start"], is_current)
        if key not in presented:
            presented[key] = day_presentation(day, is_current=is_current)
        return presented[key]

    for day in saved:
        day["presentation"] = presentation(day, day["ranked_day_start"] == ready["current_day_start"])
        day["display_end"] = display_end(
            day, saved, ready["season_weeks"], now, presentation(day, False)["status"])
    for day in ready["days"]:
        copies = [item for item in saved if item["ranked_day_start"] == day["ranked_day_start"]]
        primary = copies[0]
        for key in ("display_start", "presentation", "display_end"):
            day[key] = primary[key]
        # A group may lack the next day used for backward inference. Keep
        # the answers addressable rather than silently changing one reader.
        alternatives = {name: next((item["display_start"] for item in group
                                    if item["ranked_day_start"] == day["ranked_day_start"]), None)
                        for name, group in zip(("season", "recent", "current"), groups)}
        if any(value is not None and value != primary["display_start"] for value in alternatives.values()):
            day["display_start_by_reference"] = alternatives
    context = ready["season"]
    calendar = ranked_day_for(now)
    start = _at(context["start"]) if context and _at(context["start"]) <= now < _at(context["end"]) else calendar.season_start
    today = min(SEASON_DAY_COUNT, (now - start) // RANKED_DAY_DURATION + 1)
    numbered = {}
    for day in saved:
        number = (_at(day["ranked_day_start"]) - start) // RANKED_DAY_DURATION + 1
        if 1 <= number <= SEASON_DAY_COUNT:
            numbered.setdefault(number, day)
    # Always the Season's 28 days, as the website's seasonShape drew them,
    # even right after the Season Reset before anything is saved.
    ready["season_shape"] = shape_of(start, today, ready["season_weeks"], numbered)
    below = {_at(week["week_start"]) for week in ready["season_weeks"] if week["league"] == "below_legend_i"}
    ready["stats_day_starts"] = None if context is None else [
        (_at(context["start"]) + offset * RANKED_DAY_DURATION).isoformat()
        for offset in range(context["current_day_number"])
        if _at(context["start"]) + (offset // 7 * 7) * RANKED_DAY_DURATION not in below
    ]
    # selectPlayerHistory's anchor guard deliberately differs from seasonShape.
    history_start = _at(context["start"]) if context and now < _at(context["end"]) else calendar.season_start
    history = {}
    history_numbers = {}
    for day in saved:
        number = (_at(day["ranked_day_start"]) - history_start) // RANKED_DAY_DURATION + 1
        if 1 <= number <= SEASON_DAY_COUNT and day["presentation"]["show_in_player_log"]:
            history.setdefault(day["ranked_day_start"][:10], day["ranked_day_start"])
            history_numbers.setdefault(day["ranked_day_start"], number)
    ready["history_day_starts"] = [history[key] for key in sorted(history, reverse=True)]
    ready["history_day_numbers"] = {start: history_numbers[start] for start in ready["history_day_starts"]}
    if battles:
        ready["trophy_trends"] = trophy_trends(recent, start, now)
        ready["battle_statistics"] = battle_statistics(recent, season, start, now, ready["season_shape"])
    return recent, season, current[0] if current else None
