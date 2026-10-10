"""A crew's boards: Live, Top players, Top attackers, Best and Worst
defenders, and Highest streaks.

Every board reads tracked data whatever date an account joined, and lists
anyone with data in the period; there is no other minimum. Boards only read
the current Season, so each one starts fresh at the Season's first Reset.

The average boards follow the player page's Season summary
(``website/app/lib/battle-statistics.ts``): finished Legend days only, each
recorded battle counted once, trophies divided by the Legend days in the
window. "Last 7 days" is the last seven finished days of this Season, so it
is shorter in the first week and empty on Day 1. Streaks also count today's
attacks, and only attacks Clash Lens saw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import api_crews
from .api_accounts import _MEMBER_JOINS
from .api_db import (
    ApiDatabase,
    _battle_id_sort_key,
    _screen_events,
    _shown_total,
    _text,
)
from .api_leaderboard import _LIVE_ORDER_SQL, _LIVE_PLAYERS_SQL, _season_params
from .domain import RANKED_DAY_DURATION, ranked_day_for

PERIODS = ("today", "week", "season")
_WEEK_DAYS = 7

# Each place's newest saved log per Legend day from ``%(first)s`` to today,
# with that day's Reset reading.
_DAYS_SQL = """
SELECT DISTINCT ON (daily.player_id, daily.ranked_day_start)
       player.normalized_tag, daily.ranked_day_start, daily.battles,
       daily.partial_reasons, daily.adjustments, daily.net_trophy_change,
       daily.coverage, daily.attack_count, daily.defense_count,
       ranked_day.start_trophies
FROM crew_players AS place
JOIN players AS player ON player.id = place.player_id
JOIN api_player_daily_logs AS daily ON daily.player_id = place.player_id
LEFT JOIN ranked_day_versions AS ranked_day
    ON ranked_day.id = daily.ranked_day_version_id
WHERE place.crew_id = %(crew)s
  AND daily.ranked_day_start >= %(first)s
  AND daily.ranked_day_start <= %(today)s
ORDER BY daily.player_id, daily.ranked_day_start, daily.version DESC
"""


class _Day:
    def __init__(self, row: Any) -> None:
        reasons = [reason for reason in row[3] or [] if isinstance(reason, str)]
        self.offense, self.defense = _screen_events(row[2])
        # The player page's rule: a day is a Legend day unless it has no
        # battles and saved profiles prove the player had not signed up yet.
        self.legend = bool(self.offense or self.defense) or "not_enrolled" not in reasons
        self.start_trophies = None if row[9] is None else int(row[9])
        self.net = _shown_total(row[5], _text(row[6]), row[7], row[8], reasons)
        reset = next(
            (
                item.get("amount")
                for item in row[4] or []
                if isinstance(item, dict)
                and item.get("type") in {"weekly_reset", "season_reset"}
            ),
            0,
        )
        self.reset = reset if isinstance(reset, int) else 0


def _events(
    days: list[_Day], side: str, start: datetime, now: datetime
) -> list[dict[str, Any]]:
    """The days' battles on one side from ``start`` to ``now``, each once."""
    events: dict[str, dict[str, Any]] = {}
    for day in days:
        for event in getattr(day, side):
            if start <= datetime.fromisoformat(event["battle_timestamp"]) <= now:
                events[event["battle_id"]] = event
    return list(events.values())


def _average_row(days: list[_Day], side: str, start: datetime, now: datetime) -> dict[str, Any]:
    events = _events(days, side, start, now)
    return {
        "total": sum(abs(event["trophy_change"]) for event in events),
        "days": len(days),
        "battles": len(events),
    }


def _streak(attacks: list[dict[str, Any]]) -> dict[str, Any]:
    """The longest run of three-star attacks in a row, and whether the latest
    attack still extends a run that long."""
    best = run = 0
    for attack in sorted(
        attacks,
        key=lambda event: (event["battle_timestamp"], _battle_id_sort_key(event["battle_id"])),
    ):
        run = run + 1 if attack["stars"] == 3 else 0
        best = max(best, run)
    return {"best": best, "going": best > 0 and run == best, "attacks": len(attacks)}


def get_crew_boards(
    database: ApiDatabase, account_id: int, crew_id: str, *, period: str, now: datetime
) -> dict[str, Any] | None:
    """The crew's six boards for ``period`` (today, week or season), or None
    when the caller is not in the crew."""
    now = now.astimezone(UTC)
    day = ranked_day_for(now)
    today = day.start
    finished = [day.season_start + n * RANKED_DAY_DURATION for n in range(day.day_number - 1)]
    window = {"today": [today], "week": finished[-_WEEK_DAYS:], "season": finished}[period]
    # Streaks count from the window's first day and include today.
    streak_start = window[0] if window else today
    first = finished[-1] if period == "today" and finished else streak_start
    with database.pool.connection() as connection:
        try:
            crew_row, _size, _role = api_crews._membership(
                connection, crew_id, account_id, lock=False
            )
        except api_crews._Refused:
            return None
        places = api_crews._players(
            connection,
            f"crew_players AS member {_MEMBER_JOINS} WHERE member.crew_id = %s",
            (crew_row,),
            now,
        )
        tags = [player["tag"] for _owner, player in places]
        live = connection.execute(
            f"""
            SELECT normalized_tag, trophies, observed_at
            FROM ({_LIVE_PLAYERS_SQL}) AS live
            WHERE normalized_tag = ANY(%(tags)s)
            ORDER BY {_LIVE_ORDER_SQL}
            """,
            {"tags": tags, **_season_params(now)},
        ).fetchall()
        saved: dict[str, dict[datetime, _Day]] = {}
        for row in connection.execute(
            _DAYS_SQL, {"crew": crew_row, "first": first, "today": today}
        ):
            saved.setdefault(_text(row[0]), {})[row[1].astimezone(UTC)] = _Day(row)

    players = {player["tag"]: player for _owner, player in places}
    mine = {player["tag"] for owner, player in places if owner == account_id}

    def entry(tag: str, **values: Any) -> dict[str, Any]:
        return {"tag": tag, "name": players[tag]["name"], "you": tag in mine, **values}

    def board(rows: list[dict[str, Any]], reason: str) -> dict[str, Any]:
        """The rows, and why each crew account left off them is missing."""
        listed = {row["tag"] for row in rows}
        missing = []
        for tag, player in players.items():
            if tag in listed:
                continue
            if player["status"] != "tracking":
                why = player["status"]
            else:
                why = reason
            missing.append({"tag": tag, "name": player["name"], "reason": why})
        missing.sort(key=lambda item: ((item["name"] or "").casefold(), item["tag"]))
        return {"rows": rows, "missing": missing}

    def name_key(row: dict[str, Any]) -> tuple[str, str]:
        return (row["name"] or "").casefold(), row["tag"]

    live_rows = [
        entry(_text(tag), trophies=int(trophies), observed_at=observed_at.astimezone(UTC))
        for tag, trophies, observed_at in live
    ]
    position = {row["tag"]: index for index, row in enumerate(live_rows)}

    # Trophies at the last Reset: today's Reset reading, or failing that
    # yesterday's start plus its battles and any reset at its closing Reset.
    # None on a Season's first day, which has no finished day yet.
    top_rows = []
    if finished:
        for tag in players:
            days = saved.get(tag, {})
            reading = days[today].start_trophies if today in days else None
            yesterday = days.get(finished[-1])
            if (
                reading is None
                and yesterday is not None
                and yesterday.start_trophies is not None
                and yesterday.net is not None
            ):
                reading = yesterday.start_trophies + yesterday.net + yesterday.reset
            if reading is not None:
                top_rows.append(entry(tag, trophies=reading))
    top_rows.sort(
        key=lambda row: (-row["trophies"], position.get(row["tag"], len(position)), *name_key(row))
    )

    averages: dict[str, list[dict[str, Any]]] = {"offense": [], "defense": []}
    streak_rows = []
    for tag in players:
        days = saved.get(tag, {})
        legend = [days[start] for start in window if start in days and days[start].legend]
        for side, listed in averages.items():
            if period == "today":
                values = _average_row(legend, side, today, now)
                if values["battles"]:
                    listed.append(entry(tag, **values))
            elif legend:
                listed.append(entry(tag, **_average_row(legend, side, window[0], now)))
        streak_days = [saved_day for start, saved_day in days.items() if start >= streak_start]
        attacks = _events(streak_days, "offense", streak_start, now)
        if attacks:
            streak_rows.append(entry(tag, **_streak(attacks)))

    def per_day(row: dict[str, Any]) -> float:
        return row["total"] / row["days"]

    attackers = sorted(
        averages["offense"], key=lambda row: (-per_day(row), -row["days"], *name_key(row))
    )
    best = sorted(
        averages["defense"], key=lambda row: (per_day(row), -row["days"], *name_key(row))
    )
    worst = sorted(
        averages["defense"], key=lambda row: (-per_day(row), -row["days"], *name_key(row))
    )
    streak_rows.sort(key=lambda row: (-row["best"], not row["going"], *name_key(row)))
    quiet = "no_days_in_period"
    return {
        "kind": "crew-boards",
        "crew_id": crew_id,
        "period": period,
        "season_id": day.official_season_id,
        "day_number": day.day_number,
        "window_days": [start.isoformat() for start in window],
        "today_start": today.isoformat(),
        "boards": {
            "live": board(live_rows, "not_on_live_board"),
            "top": board(top_rows, "no_reset_reading"),
            "attackers": board(
                attackers, "no_attacks_today" if period == "today" else quiet
            ),
            "best_defenders": board(
                best, "no_defenses_today" if period == "today" else quiet
            ),
            "worst_defenders": board(
                worst, "no_defenses_today" if period == "today" else quiet
            ),
            "streaks": board(
                streak_rows, "no_attacks_today" if period == "today" else "no_attacks_in_period"
            ),
        },
    }
