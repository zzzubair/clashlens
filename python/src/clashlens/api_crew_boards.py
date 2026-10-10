"""A crew's boards: Live, Top players, Top attackers, Best and Worst
defenders, and Highest streaks.

Every board reads tracked data whatever date an account joined, and lists
anyone with data in the period; there is no other minimum. Boards only read
the current Season, so each one starts fresh at the Season's first Reset.

The average boards follow the player page's Season summary
(``website/app/lib/battle-statistics.ts``): finished Legend days only, each
recorded battle counted once, trophies divided by the Legend days in the
window. "Last 7 days" is the last seven finished days of this Season, so it
is shorter in the first week and empty on Day 1. Streaks count only attacks
Clash Lens saw, and today's too except over the last seven days. Top players
is the saved Reset board for today's Reset, in its order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import api_crews
from .api_accounts import _MEMBER_JOINS
from .api_db import ApiDatabase, _battle_id_sort_key, _screen_events, _text
from .api_leaderboard import _LIVE_ORDER_SQL, _LIVE_PLAYERS_SQL, _season_params
from .domain import RANKED_DAY_DURATION, ranked_day_for

PERIODS = ("today", "week", "season")
_WEEK_DAYS = 7

# Each place's newest saved log per Legend day from ``%(first)s`` to today.
_DAYS_SQL = """
SELECT DISTINCT ON (daily.player_id, daily.ranked_day_start)
       player.normalized_tag, daily.ranked_day_start, daily.battles,
       daily.partial_reasons
FROM crew_players AS place
JOIN players AS player ON player.id = place.player_id
JOIN api_player_daily_logs AS daily ON daily.player_id = place.player_id
WHERE place.crew_id = %(crew)s
  AND daily.ranked_day_start >= %(first)s
  AND daily.ranked_day_start <= %(today)s
ORDER BY daily.player_id, daily.ranked_day_start, daily.version DESC
"""

# The places on the published Reset board for the Reset at ``%(reset)s``, in
# that board's order.
_RESET_BOARD_SQL = """
SELECT player.normalized_tag, entry.trophies
FROM leaderboard_snapshots AS board
JOIN leaderboard_snapshot_entries AS entry ON entry.snapshot_id = board.id
JOIN crew_players AS place ON place.player_id = entry.player_id
JOIN players AS player ON player.id = entry.player_id
WHERE board.snapshot_kind = 'frozen'
  AND board.state = 'published'
  AND board.boundary_at = %(reset)s
  AND place.crew_id = %(crew)s
ORDER BY entry.position
"""


class _Day:
    def __init__(self, row: Any) -> None:
        reasons = [reason for reason in row[3] or [] if isinstance(reason, str)]
        self.offense, self.defense = _screen_events(row[2])
        # The player page's rule: a day is a Legend day unless it has no
        # battles and saved profiles prove the player had not signed up yet.
        self.legend = bool(self.offense or self.defense) or "not_enrolled" not in reasons


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
    streak_window = [*finished, today] if period == "season" else window
    first = streak_window[0] if streak_window else today
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
        # A Season's first day has no Reset of its own yet.
        reset_board = (
            connection.execute(_RESET_BOARD_SQL, {"crew": crew_row, "reset": today}).fetchall()
            if finished
            else []
        )

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
    top_rows = [entry(_text(tag), trophies=int(trophies)) for tag, trophies in reset_board]

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
        streak_days = [days[start] for start in streak_window if start in days]
        attacks = _events(streak_days, "offense", streak_window[0], now) if streak_days else []
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
