"""The website's Season cards as bounded, read-time Python facts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .api_leaderboard import _frozen_publications
from .db_rows import text_value
from .domain import (
    MAX_BATTLE_TROPHIES,
    MAX_DAILY_ATTACKS,
    RANKED_DAY_DURATION,
    SEASON_DAY_COUNT,
    SEASON_START_TROPHIES,
)

GOAL_PACE_DAYS = 7


def dashboard_season(context: dict | None, recent: list[dict], season: list[dict], current: dict | None) -> dict | None:
    if context is None:
        return None
    start = datetime.fromisoformat(context["start"])
    saved = {}
    for day in [*recent, *season, *([] if current is None else [current])]:
        number = (datetime.fromisoformat(day["ranked_day_start"]) - start) // RANKED_DAY_DURATION + 1
        if 1 <= number <= context["current_day_number"]:
            saved.setdefault(number, day)
    days = []
    for number in range(1, context["current_day_number"]):
        day = saved.get(number)
        if day is None:
            continue
        gain, loss = day["attack_gain"], day["defense_loss"]
        net = day["net_trophy_change"]
        if net is None and day["battles_complete"] and gain is not None and loss is not None:
            net = gain - loss
        attacks, defenses = day["attack_count"], day["defense_count"]
        tripled = day["defense_three_star_count"]
        events = day["offense_events"]
        every_attack = (
            attacks == len(events) and gain == sum(event["trophy_change"] for event in events)
            and not any(event.get("perspective_disagreement") for event in events)
            and len(events) <= MAX_DAILY_ATTACKS
            and (attacks == MAX_DAILY_ATTACKS or day["coverage"] == "complete")
        )
        following = saved.get(number + 1, {}).get("display_start", {}).get("trophies")
        baseline = day["display_start"]["trophies"]
        reset = next((item for item in day["adjustments"] if item["type"] in ("weekly_reset", "season_reset")), None)
        amount = 0 if reset is None else reset["amount"]
        end = following if following is not None else baseline + net + amount if baseline is not None and net is not None else None
        days.append({
            "day": number, "start": day["ranked_day_start"], "end": day["ranked_day_end"],
            "attacks": attacks, "defenses": defenses, "offense": gain,
            "defense": None if loss is None else -loss, "net": net,
            "triples": day["attack_three_star_count"],
            "held": None if defenses is None or tripled is None else defenses - tripled,
            "end_trophies": end, "rank": day.get("reset_rank"),
            "hits": [[event["stars"], event["trophy_change"]]
                     for event in sorted(events, key=lambda event: datetime.fromisoformat(event["battle_timestamp"]))] if every_attack else None,
            "attack_slots": MAX_DAILY_ATTACKS if every_attack else None,
            "maximum_attack_gain": MAX_DAILY_ATTACKS * MAX_BATTLE_TROPHIES if every_attack else None,
            "kind": "shield" if attacks == 0 and defenses == 0 and net == 0 else "missing" if net is None else "played",
        })
    return {
        "today": context["current_day_number"], "start": context["start"], "end": context["end"],
        "day_count": SEASON_DAY_COUNT, "days_left": SEASON_DAY_COUNT - context["current_day_number"] + 1,
        "days": days,
    }


def _ratio(top, bottom):
    return top / bottom if bottom > 0 else None


def _rate(days, top, bottom=lambda day: 1):
    pairs = [(top(day), bottom(day)) for day in days]
    known = [(above, below) for above, below in pairs if above is not None and below is not None]
    return _ratio(sum(above for above, _ in known), sum(below for _, below in known))


def _form_totals(days):
    return {
        "net": _rate(days, lambda day: day["net"] if day["attacks"] or day["defenses"] else None),
        "per_attack": _rate(days, lambda day: day["offense"], lambda day: day["attacks"]),
        "tripled": _rate(days, lambda day: day["triples"], lambda day: day["attacks"]),
        "per_defense": _rate(days, lambda day: day["defense"], lambda day: day["defenses"]),
        "held": _rate(days, lambda day: day["held"], lambda day: day["defenses"]),
    }


def dashboard_windows(season: dict | None) -> dict:
    result = {}
    for key, size in (("3", 3), ("7", 7), ("season", SEASON_DAY_COUNT)):
        if season is None or season["today"] < 2:
            result[key] = None
            continue
        last = season["today"] - 1
        first = max(1, last - size + 1)
        span = last - first + 1
        days = [day for day in season["days"] if first <= day["day"] <= last]
        if not days:
            result[key] = None
            continue
        earlier = [day for day in season["days"] if first - size <= day["day"] < first] if span == size and first - size >= 1 else []
        nets = [day["net"] for day in days if day["net"] is not None]
        recorded = [day for day in days if day["hits"] is not None]
        consistency = None
        if recorded:
            hits = [hit for day in recorded for hit in day["hits"]]
            stars = [sum(star == 3 - index for star, _ in hits) for index in range(4)]
            trophies = sum(gained for _, gained in hits)
            misses = [gained for star, gained in hits if star < 3]
            slots = len(recorded) * MAX_DAILY_ATTACKS
            consistency = {
                "attacks": len(hits), "slots": slots, "triples": stars[0], "stars": stars,
                "per_attack": _ratio(trophies, len(hits)), "per_miss": _ratio(sum(misses), len(misses)),
                "missed": min(0, trophies - slots * MAX_BATTLE_TROPHIES),
                "per_day": [{"day": day["day"], "triples": None if day["hits"] is None else sum(star == 3 for star, _ in day["hits"])} for day in days],
            }
        result[key] = {
            "first": first, "last": last, "span": span,
            "day_numbers": [day["day"] for day in days],
            "form": {"counted": len(nets), "total": sum(nets) if nets else None,
                     "now": _form_totals(days), "before": _form_totals(earlier) if earlier else None},
            "consistency": consistency,
        }
    return result


def goal_plan(data: dict) -> dict | None:
    on_board = data["history"][-1] if data["history"] else None
    if data["finished"] < 1 or data["window"] < 1 or any(data[key] is None for key in ("you", "you_then", "line", "line_then")) or on_board is None:
        return None
    left = SEASON_DAY_COUNT - data["finished"]
    pace = (data["you"] - data["you_then"]) / data["window"]
    drift = (data["line"] - data["line_then"]) / data["window"]
    behind = data["line"] - on_board
    need = behind / left + drift
    return {
        "need": need, "pace": pace, "drift": drift, "gap": -behind, "days_left": left,
        "status": "ahead" if behind <= 0 and need <= 0 else "on" if pace >= need else "behind",
        # Zero is the default plan. The old website additionally listed 1–3.
        "shields": [{"days": days, "need": (behind + drift * left) / (left - days),
                     "on_pace": pace >= (behind + drift * left) / (left - days)}
                    for days in range(4) if days < left],
    }


def dashboard_goal(connection: Any, page: dict, rank: int) -> dict | None:
    ready = page["screen_ready"]
    season = ready["dashboard_season"]
    finished = 0 if season is None else season["today"] - 1
    window = min(GOAL_PACE_DAYS, finished)
    first = finished - window
    days = {} if season is None else {day["day"]: day for day in season["days"]}
    lines, history = {}, {}
    context = ready["season"]
    if finished and context is not None:
        start = datetime.fromisoformat(context["start"])
        numbers = list(range(max(1, first), finished + 1))
        # One batch selects the exact same newest publication as the Daily
        # board API. At most eight Reset dates, independent of Season length.
        rows = connection.execute(
            f"""
            WITH requested AS (
                SELECT * FROM unnest(%s::integer[], %s::timestamptz[])
                    AS wanted(day_number, boundary_at)
            )
            SELECT wanted.day_number, entry.position, player.normalized_tag, entry.trophies
            FROM requested AS wanted
            JOIN LATERAL (
                SELECT source, id FROM ({_frozen_publications()}) AS publication
                WHERE publication.boundary_at = wanted.boundary_at
                  AND publication.official_season_id = %s
                  AND publication.season_day_number = wanted.day_number
                ORDER BY (source = 'v2') DESC, version DESC, generation DESC, id DESC
                LIMIT 1
            ) AS board ON true
            JOIN LATERAL (
                SELECT position, player_id, trophies FROM leaderboard_snapshot_entries
                WHERE board.source = 'v2' AND snapshot_id = board.id
                  AND position = ANY(%s::integer[])
                UNION ALL
                SELECT position, player_id, trophies FROM api_frozen_leaderboard_entries
                WHERE board.source = 'legacy' AND leaderboard_id = board.id
                  AND position = ANY(%s::integer[])
            ) AS entry ON true
            JOIN players AS player ON player.id = entry.player_id
            """,
            (numbers, [start + number * RANKED_DAY_DURATION for number in numbers], context["id"],
             [rank, *[day["rank"] for day in days.values() if day["rank"] is not None]],
             [rank, *[day["rank"] for day in days.values() if day["rank"] is not None]]),
        ).fetchall()
        for number, position, tag, trophies in rows:
            if position == rank:
                lines[number] = int(trophies)
            if text_value(tag) == page["tag"] and position == days.get(number, {}).get("rank"):
                history[number] = int(trophies)

    def end(number):
        return SEASON_START_TROPHIES if number == 0 else days.get(number, {}).get("end_trophies")

    data = {
        "rank": rank, "finished": finished, "window": window,
        "you": end(finished) if finished else None, "you_then": end(first) if finished else None,
        "line": lines.get(finished) if finished else None,
        "line_then": SEASON_START_TROPHIES if finished and first == 0 else lines.get(first),
        "history": [SEASON_START_TROPHIES if number == 0 else history.get(number) for number in range(first, finished + 1)] if finished else [],
    }
    data["plan"] = goal_plan(data)
    return data
