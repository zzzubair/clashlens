"""A Notebook base's page: its numbers over three windows, and who ran it.

The numbers read the same per-day defense counts as every base's record
(army_package_defense_day_counts), on the counted Legend days the base ran
that still count (not Waiting days), so the This Season window always
matches the base's record on the notebook page. Totals come from everyone
who ran the base; Who ran it lists only the game accounts of the people in
the notebook (yours, or a Crew's members'), with today's defenses so far
from their published logs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import army_analytics, army_types
from .api_accounts import _MEMBER_JOINS
from .api_dashboard import _DAY_LOGS_SQL
from .api_db import _screen_events, _text
from .api_notebook import DAY, _add, _History, _Notebook, _record
from .domain import ranked_day_for
from .league_week import week_start

# "Last 3 days": the base on the 3 newest counted Legend days.
RECENT_DAYS = 3
# "By starting trophies" splits defenses by the defender's Reset trophies:
# under 5,400, 5,400 to 5,799, and 5,800 or more.
BANDS = (5400, 5800)
# Who ran it shows at most this many rows, newest first.
WHO_ROWS = 100

Day = tuple[int, datetime]


def _armies(connection: Any, days: list[Day]) -> list[dict[str, Any]] | None:
    """Each army type that attacked the base on the given (player, Legend day
    start) days, most defenses first and Other last: its defenses,
    3-starred defenses and trophies lost. None while the worker is still
    labelling armies under new type rules."""
    if not army_types.rules_ready(connection):
        return None
    if not days:
        return []
    totals: dict[int, list[int]] = {}
    for type_id, troops, unnamed, *counts in connection.execute(
        f"""
        SELECT CASE WHEN saved.exact_army_id = 0 THEN {army_types.UNREADABLE}
                    ELSE label.type_id END,
               decode.home_troops, decode.unresolved_components,
               sum(saved.defenses), sum(saved.three_star), sum(saved.trophies_lost)
        FROM unnest(%s::bigint[], %s::timestamptz[]) AS ran (player_id, start)
        JOIN army_package_defense_day_counts AS saved
          ON saved.ranked_day_start = ran.start
         AND saved.population_player_id = ran.player_id
        LEFT JOIN army_package_types AS label
          ON label.exact_army_id = saved.exact_army_id
        LEFT JOIN battle_army_decodes AS decode ON decode.id = -saved.exact_army_id
        GROUP BY 1, 2, 3
        """,
        ([player_id for player_id, _ in days], [start for _, start in days]),
    ).fetchall():
        if troops is not None:
            # An older partly read army, typed from its saved home troops.
            type_id = army_types.classify(army_analytics.with_unnamed_ids(
                {"home_troops": troops, "unresolved_components": unnamed}
            )["home_troops"])
        total = totals.setdefault(type_id or army_types.OTHER, [0, 0, 0])
        for index, value in enumerate(counts):
            total[index] += int(value)
    rows = [
        {
            "name": army_types.TYPE_NAMES.get(type_id, f"Type {type_id}"),
            "defenses": defenses,
            "three_star": three,
            "trophies_lost": lost,
            "other": type_id == army_types.OTHER,
        }
        for type_id, (defenses, three, lost) in totals.items()
        if type_id != army_types.UNREADABLE and defenses
    ]
    rows.sort(key=lambda row: (row["other"], -row["defenses"], row["name"]))
    for row in rows:
        del row["other"]
    return rows


def _starts(connection: Any, days: list[Day]) -> dict[Day, int]:
    """Each saved Legend day's Reset trophies, from its newest publication."""
    if not days:
        return {}
    return {
        (int(player_id), start): int(trophies)
        for player_id, start, trophies in connection.execute(
            """
            SELECT DISTINCT ON (daily.player_id, daily.ranked_day_start)
                   daily.player_id, daily.ranked_day_start, ranked_day.start_trophies
            FROM unnest(%s::bigint[], %s::timestamptz[]) AS ran (player_id, start)
            JOIN api_player_daily_logs AS daily
              ON daily.player_id = ran.player_id AND daily.ranked_day_start = ran.start
            LEFT JOIN ranked_day_versions AS ranked_day
              ON ranked_day.id = daily.ranked_day_version_id
            ORDER BY daily.player_id, daily.ranked_day_start, daily.version DESC
            """,
            ([player_id for player_id, _ in days], [start for _, start in days]),
        ).fetchall()
        if trophies is not None
    }


def _so_far(connection: Any, tag: str, day: datetime) -> dict[str, Any] | None:
    """A Legend day not yet counted, from its newest published log."""
    row = connection.execute(_DAY_LOGS_SQL, {"tags": [tag], "day_start": day}).fetchone()
    if row is None:
        return None
    defended = _screen_events(row[5])[1]
    return {
        "start": None if row[7] is None else int(row[7]),
        "defenses": len(defended),
        "held": sum(1 for event in defended if event["stars"] < 3),
        "trophies_lost": -sum(min(0, event["trophy_change"]) for event in defended),
    }


def base_page(connection: Any, notebook: _Notebook, base_id: int) -> dict[str, Any]:
    """The base's numbers in each window, Who ran it, and, for a Crew's
    Uploaders, admins and owner, how many Clashers keep it in their own
    notebook."""
    firsts = dict(connection.execute(
        "SELECT player_id, min(ranked_day_start) FROM notebook_day_picks"
        " WHERE base_id = %s GROUP BY 1",
        (base_id,),
    ).fetchall())
    today = notebook.today
    last_counted = min(today, notebook.season_end) - DAY
    # Every day the base ran, today's included; the counted ones; and the
    # ones of the people in this notebook, for Who ran it.
    every: list[Day] = []
    ran: list[Day] = []
    shown: list[Day] = []
    history = None
    if firsts:
        first = min(firsts.values())
        history = _History(connection, [int(player_id) for player_id in firsts], first,
                           notebook.tomorrow, first, week_start(today))
        for player_id, found in history.runs.items():
            for start, end, base in found:
                day = start
                while base == base_id and day <= min(end, today):
                    if history.confirmed(player_id, day):
                        every.append((player_id, day))
                        if day in history.counted and day <= last_counted:
                            ran.append((player_id, day))
                        if player_id in notebook.runner_ids:
                            shown.append((player_id, day))
                    day += DAY
    recent = sorted({day for _, day in ran if day <= last_counted}, reverse=True)
    recent_days = set(recent[:RECENT_DAYS])

    def windows(day: datetime) -> list[str]:
        """The windows a day is in; a day newer than every counted one, such
        as today, is in Last 3 days."""
        found = ["all"]
        if notebook.season_start <= day < notebook.season_end:
            found.append("season")
        if day in recent_days or not recent or day > recent[0]:
            found.append("last3")
        return found

    starts = _starts(connection, ran + shown)
    result: dict[str, Any] = {}
    for window in ("last3", "season", "all"):
        days = [item for item in ran if window in windows(item[1])]
        record = _record()
        bands = [_record() for _ in range(len(BANDS) + 1)]
        counted: list[Day] = []
        for player_id, day in days:
            counts = history.day_counts(player_id, day) if history else None
            _add(record, counts)
            if counts is not None:
                counted.append((player_id, day))
                if (start := starts.get((player_id, day))) is not None:
                    _add(bands[sum(start >= edge for edge in BANDS)], counts)
        result[window] = {
            "record": record,
            "ran_by": len({player_id for player_id, day in every if window in windows(day)}),
            "armies": _armies(connection, counted),
            "bands": bands,
        }

    if notebook.crew is None:
        names = {player_id: (tag, name, None) for player_id, tag, name, *_ in notebook.players}
    else:
        names = {
            int(player_id): (_text(tag), None if name is None else _text(name),
                             _text(owner))
            for player_id, tag, name, owner in connection.execute(
                f"""
                SELECT member.player_id, player.normalized_tag,
                       COALESCE(accepted.name, latest.name), account.display_name
                FROM crew_players AS member {_MEMBER_JOINS}
                JOIN clash_lens_accounts AS account ON account.id = member.account_id
                WHERE member.crew_id = %s
                """,
                (notebook.owner[1],),
            ).fetchall()
        }
    rows = []
    for player_id, day in shown:
        if player_id not in names:
            continue
        tag, name, owner = names[player_id]
        row: dict[str, Any] = {
            "tag": tag, "name": name, "owner": owner,
            "day": ranked_day_for(day).day_number, "date": day.date().isoformat(),
            "live": day == today, "windows": windows(day),
            "start": starts.get((player_id, day)),
            "defenses": None, "held": None, "trophies_lost": None,
        }
        if history and day in history.counted and day <= last_counted:
            counts = history.day_counts(player_id, day)
            if counts is not None:
                defenses, _one, _two, three, lost = counts
                row.update(defenses=defenses, held=defenses - three, trophies_lost=lost)
        elif day >= today - DAY and (so_far := _so_far(connection, tag, day)):
            # Today, or yesterday before its defenses are counted.
            if so_far["start"] is None:
                del so_far["start"]
            row.update(so_far)
        rows.append(row)
    rows.sort(key=lambda row: ((row["name"] or "").casefold(), row["tag"]))
    rows.sort(key=lambda row: row["date"], reverse=True)
    copies = None
    if notebook.crew is not None and notebook.crew["role"] != "member":
        copies = int(connection.execute(
            """
            SELECT count(*) FROM notebook_entries AS entry
            JOIN notebooks AS book ON book.id = entry.notebook_id
            WHERE entry.base_id = %s AND book.account_id IS NOT NULL
            """,
            (base_id,),
        ).fetchone()[0])
    return {"windows": result, "who": {"rows": rows[:WHO_ROWS], "total": len(rows)},
            "copies": copies}
