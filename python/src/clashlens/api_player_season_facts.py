"""Read-time membership and battle statistics, ported from the website.

The three reference groups keep their original order and independent start
inference. Nothing in these projections changes saved day evidence.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .api_player_presentation import day_presentation, display_end, display_starts
from .domain import (
    RANKED_DAY_DURATION,
    SEASON_DAY_COUNT,
    SEASON_START_TROPHIES,
    ranked_day_for,
)


def shape_of(start: datetime, today: int, weeks: list[dict], saved: dict[int, dict], *, historical: bool = False) -> dict[str, Any]:
    by_week = {datetime.fromisoformat(week["week_start"]): week for week in weeks}

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
            kept = saved.get(candidate)
            unplayed = kept is not None and (
                kept.get("attack_count") == 0 and kept.get("defense_count") == 0
                and kept.get("net_change" if historical else "net_trophy_change") == 0
                and (kept.get("start_trophies") if historical else kept["display_start"]["trophies"]) == SEASON_START_TROPHIES
                and (historical or not kept["offense_events"] and not kept["defense_events"])
            )
            if kept is not None and not unplayed:
                signed_up = candidate
                break
            if kept is not None and candidate < today:
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
            last_legend = saved[number]["ranked_day_start"]
    if runs and runs[-1]["dropped"] and runs[-1]["signed_up"] is None:
        last_legend = saved.get(runs[-1]["last"], {}).get("ranked_day_start", last_legend)
    return {
        "season_start": start.isoformat(), "today": today, "kinds": kinds, "runs": runs,
        "partial": any(kind in ("outside", "waiting") for kind in kinds),
        "outside_now": today <= SEASON_DAY_COUNT and kinds[today - 1] == "outside",
        "legend_days": {"expected": expected, "saved": saved_count},
        "last_legend_day_start": last_legend,
    }


def historical_facts(summary: dict[str, Any], *, season_start: datetime | None = None) -> None:
    saved = {}
    for entry in summary["daily_entries"]:
        entry["presentation"] = day_presentation(entry, is_current=False)
        number = entry.get("season_day_number")
        if number is not None:
            saved.setdefault(number, entry)
    start = season_start or (datetime.fromisoformat(summary["season_start"]) if summary.get("season_start") else None)
    summary["season_shape"] = None if start is None else shape_of(
        start, SEASON_DAY_COUNT + 1, summary.get("season_weeks", []), saved,
        historical=True,
    )


def battle_statistics(recent: list[dict], season: list[dict], start: datetime, now: datetime, shape: dict | None) -> dict[str, Any]:
    today = ranked_day_for(now).start
    ordered = [*season, *recent]

    def day_from(at):
        return next((day for day in ordered if datetime.fromisoformat(day["ranked_day_start"]) == at), None) if at >= start else None

    left = shape["last_legend_day_start"] if shape and shape["outside_now"] else None
    latest_at = datetime.fromisoformat(left) if left else today - RANKED_DAY_DURATION
    latest = day_from(latest_at)
    previous = day_from(latest_at - RANKED_DAY_DURATION) if not left and (latest is None or latest.get("reset_rank") is None) else None
    selected = previous if previous and previous.get("reset_rank") is not None else latest
    reset_start = None if selected is None else selected["ranked_day_start"]
    result = {}
    for period in ("7", "14", "season"):
        first = start if period == "season" else max(start, today - int(period) * RANKED_DAY_DURATION)
        days = {datetime.fromisoformat(day["ranked_day_start"]): day for day in [*recent, *season]
                if first <= datetime.fromisoformat(day["ranked_day_start"]) < today}

        def side(key, days=days, first=first):
            events = {}
            played = 0
            for _, day in sorted(days.items()):
                before = len(events)
                for event in day[key]:
                    at = datetime.fromisoformat(event["battle_timestamp"])
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
                if 1 <= (number := (datetime.fromisoformat(day["ranked_day_start"]) - start) // RANKED_DAY_DURATION + 1) < shape["today"]
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
    for day in saved:
        day["presentation"] = day_presentation(day, is_current=day["ranked_day_start"] == ready["current_day_start"])
        day["display_end"] = display_end(day, saved, ready["season_weeks"], now)
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
    start = datetime.fromisoformat(context["start"]) if context and datetime.fromisoformat(context["start"]) <= now < datetime.fromisoformat(context["end"]) else calendar.season_start
    today = min(SEASON_DAY_COUNT, (now - start) // RANKED_DAY_DURATION + 1)
    numbered = {}
    for day in saved:
        number = (datetime.fromisoformat(day["ranked_day_start"]) - start) // RANKED_DAY_DURATION + 1
        if 1 <= number <= SEASON_DAY_COUNT:
            numbered.setdefault(number, day)
    ready["season_shape"] = shape_of(start, today, ready["season_weeks"], numbered) if context or numbered or ready["season_weeks"] else None
    below = {datetime.fromisoformat(week["week_start"]) for week in ready["season_weeks"] if week["league"] == "below_legend_i"}
    ready["stats_day_starts"] = None if context is None else [
        (datetime.fromisoformat(context["start"]) + offset * RANKED_DAY_DURATION).isoformat()
        for offset in range(context["current_day_number"])
        if datetime.fromisoformat(context["start"]) + (offset // 7 * 7) * RANKED_DAY_DURATION not in below
    ]
    # selectPlayerHistory's anchor guard deliberately differs from seasonShape.
    history_start = datetime.fromisoformat(context["start"]) if context and now < datetime.fromisoformat(context["end"]) else calendar.season_start
    history = {}
    for day in saved:
        number = (datetime.fromisoformat(day["ranked_day_start"]) - history_start) // RANKED_DAY_DURATION + 1
        if 1 <= number <= SEASON_DAY_COUNT and day["presentation"]["show_in_player_log"]:
            history.setdefault(day["ranked_day_start"][:10], day["ranked_day_start"])
    ready["history_day_starts"] = [history[key] for key in sorted(history, reverse=True)]
    if battles:
        ready["battle_statistics"] = battle_statistics(recent, season, start, now, ready["season_shape"])
    return recent, season, current[0] if current else None
