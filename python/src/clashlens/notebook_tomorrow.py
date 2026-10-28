"""The Notebook's Tomorrow screen: every base a Clasher can reach, from their
own notebook and every Crew they're in, ranked for one game account by how
many of tomorrow's 8 defenses it is likely to hold.

Who you will meet: matchmaking pairs players by their Reset trophies, as
close as the players near them allow. So the screen counts defenses on
players who started near the account's Reset trophies tomorrow, about
``width`` either side (the typical gap to a target, from the matchmaking
study, never under 40, 0.6 times on Mondays and 1.25 times at weekends),
each counted less the further away it started. Tomorrow's Reset trophies
are today's live trophies plus the middle of the 200 imagined finishes of
the day that Projected place works out (projected_place.py); a Season's
Day 1 starts everyone on 5,000.

How a base is scored: the meta near you is which army types attacked
players near those trophies on the last 7 counted days, and how often
(notebook_meta.py's daily table). Against each army, the base's 3-star rate
comes from everyone who ran it, each defense counted by how close its
defender started and how recent it is (the last 7 days fully, the 2 weeks
before a third, older a tenth), with 10 pretend defenses at the average
base's rate near you so one lucky day can't top the list. Holds of 8 adds
up each army's share times the base's chance to hold it, times 8. A base
with 24 defenses near you is ranked; with fewer it looks twice as wide
once, then goes in Early guesses. A base nobody ran has no prediction.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from . import projected_place
from .api import ApiError
from .api_accounts import _linked_players
from .api_dashboard import _DAY_LOGS_SQL, _PROJECTION_PLAYERS_SQL
from .api_db import ApiDatabase, _text
from .api_leaderboard import LiveBoardCache
from .api_notebook import (
    DAY,
    _crews,
    _History,
    _Notebook,
    base_on,
    note_visit,
)
from .domain import ranked_day_for
from .league_week import week_start
from .notebook_base import _starts

# The typical gap between an attacker's and their target's Reset trophies,
# by 100-trophy band, from the matchmaking study (2.07 million battles,
# October 2026); 4,800's below and 5,700's above.
GAPS = {4800: 33, 4900: 10, 5000: 7, 5100: 14, 5200: 26, 5300: 56, 5400: 98,
        5500: 171, 5600: 254, 5700: 335}
# Closer than this, attackers are about equally strong.
FLOOR = 40
# Mondays match closest, weekends widest (Monday is 0).
WEEKDAY_FACTORS = (0.6, 1, 1, 1, 1, 1.25, 1.25)
# Pretend defenses at the average base's rate near you, per army.
PRETEND = 10
# Defenses near you that rank a base: 3 full Legend days on one account.
RANKED = 24
META_DAYS = 7
# A triple loses the defender 40 trophies.
TRIPLE_LOSS = 40
SEASON_START = 5000


def gap(trophies: int) -> int:
    return GAPS[max(4800, min(5700, trophies // 100 * 100))]


def width(trophies: int, day: datetime) -> int:
    """How far either side of ``trophies`` defenses count fully on ``day``."""
    return max(FLOOR, round(gap(trophies) * WEEKDAY_FACTORS[day.weekday()]))


def near(apart: float, within: float) -> float:
    """A defense's weight by how far from you its defender started."""
    return math.exp(-0.5 * (apart / within) ** 2)


def recency(day: datetime, tomorrow: datetime) -> float:
    days = (tomorrow - day) // DAY
    return 1 if days <= 7 else 1 / 3 if days <= 21 else 0.1


def _place(board: list[tuple[Any, ...]], tag: str, trophies: int) -> float:
    """``tag``'s place on ``board``, the Live Leaderboard's rows, at
    ``trophies``: the middle of the places it shares with equal trophies, as
    projected_place.situations works it out; off the board, below everyone."""
    others = [int(row[2]) for row in board if _text(row[0]) != tag]
    if len(others) == len(board):
        return len(board) + 1
    above = sum(points > trophies for points in others)
    equal = sum(points == trophies for points in others)
    return above + 1 + equal / 2


def _about(
    connection: Any, now: datetime, player_id: int, tag: str, board: list[tuple[Any, ...]],
) -> dict[str, Any] | None:
    """Tomorrow's Reset trophies, about: live trophies plus the middle of 200
    imagined finishes of today, copied from players at the same level on
    ``board``, the Live Leaderboard's rows; None without live trophies."""
    row = connection.execute(
        """
        SELECT accepted.trophies FROM players AS player
        JOIN player_profile_versions AS accepted
          ON accepted.id = player.current_profile_version_id
         AND accepted.source_contract_state = 'accepted'
        WHERE player.id = %s
        """,
        (player_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    trophies = int(row[0])
    today = ranked_day_for(now).start
    log = connection.execute(_DAY_LOGS_SQL, {"tags": [tag], "day_start": today}).fetchone()
    attacks = max(0, 8 - int(log[1] or 0)) if log else 8
    defenses = max(0, 8 - int(log[2] or 0)) if log else 8
    change = 0
    examples = projected_place.read_examples(connection, now)
    if examples and (attacks or defenses):
        recent = connection.execute(
            _PROJECTION_PLAYERS_SQL, {"tags": [tag], "day_start": today}).fetchone()
        activity = projected_place.activity(int(recent[2]), int(recent[3]))
        # Copying days from players at their level (Top 200, 201-1,000 or
        # their trophy bracket), as the dashboard does.
        imagined = projected_place.ProjectedPlaces(
            [trophies], [attacks], [defenses], [activity], [None], [tag], examples,
            seed=int(now.timestamp()) // 60, places=[_place(board, tag, trophies)])
        # With one player, a day's key is their trophies after it, times
        # the key's width, plus less than one width.
        scale = projected_place.SPREAD + 2
        changes = sorted(keys[0] // scale - trophies for keys in imagined.keys)
        middle = len(changes) // 2
        change = round((changes[middle - 1] + changes[middle]) / 2)
    return {"now": trophies, "attacks_left": attacks, "defenses_left": defenses,
            "about": trophies + change}


def _bases(
    connection: Any, account_id: int, now: datetime,
) -> tuple[dict[int, dict[str, Any]], _History]:
    """Every base the account can reach and isn't retired: {id: code, name,
    where it is, whether it is in the account's own notebook}; and the runs
    of the account's own game accounts."""
    found: dict[int, dict[str, Any]] = {}
    own = _Notebook(connection, account_id, now)
    own_history = None
    mine: set[int] = set()
    for crew in [None, *_crews(connection, account_id)]:
        book = own if crew is None else _Notebook(connection, account_id, now, None, crew["id"])
        entries = book.entries()
        history = book.history(list(entries))
        if crew is None:
            # Retired there too: running it is a pick, not an add.
            own_history, mine = history, set(entries)
        statuses = book.statuses(entries, history)
        for base_id, entry in entries.items():
            if statuses[base_id] == "retired":
                continue
            base = found.setdefault(base_id, {
                "id": entry["code"], "name": entry["name"], "where": [],
                "mine": base_id in mine})
            base["where"].append("Your notebook" if crew is None else crew["name"])
    assert own_history is not None
    return found, own_history


def _meta(connection: Any, about: int, within: int, today: datetime) -> dict[int, list[float]]:
    """The last 7 counted days' defenses near ``about`` by army type, each
    weighted by how close its defenders started: [defenses, triples,
    trophies lost]."""
    rows = connection.execute(
        """
        SELECT meta.type_id, meta.step, meta.defenses, meta.three_star, meta.trophies_lost
        FROM notebook_meta_counts AS meta
        WHERE meta.ranked_day_start IN (
            SELECT ranked_day_start FROM notebook_meta_days WHERE ranked_day_start < %s
            ORDER BY ranked_day_start DESC LIMIT %s)
          AND meta.step BETWEEN %s AND %s
        """,
        (today, META_DAYS, (about - 4 * within) // 10, (about + 4 * within) // 10),
    ).fetchall()
    totals: dict[int, list[float]] = {}
    for type_id, step, defenses, three, lost in rows:
        weight = near(int(step) * 10 + 5 - about, within)
        total = totals.setdefault(int(type_id), [0.0, 0.0, 0.0])
        total[0] += weight * defenses
        total[1] += weight * three
        total[2] += weight * lost
    return totals


def _defenses(
    connection: Any, base_ids: list[int], today: datetime, tomorrow: datetime,
) -> dict[int, list[tuple[int, datetime, int, int, int, int]]]:
    """Each base's counted defenses from everyone who ran it, as (Reset
    trophies, Legend day, army type, defenses, triples, trophies lost)."""
    firsts = dict(connection.execute(
        "SELECT player_id, min(ranked_day_start) FROM notebook_day_picks"
        " WHERE base_id = ANY(%s) GROUP BY 1",
        (base_ids,),
    ).fetchall())
    if not firsts:
        return {}
    first = min(firsts.values())
    history = _History(connection, [int(player_id) for player_id in firsts], first, tomorrow,
                       first, week_start(today))
    ran: dict[tuple[int, datetime], int] = {}
    for player_id, found in history.runs.items():
        for start, end, base in found:
            day = start
            while base in base_ids and day <= min(end, today - DAY):
                if day in history.counted and history.confirmed(player_id, day):
                    ran[(player_id, day)] = base
                day += DAY
    starts = _starts(connection, list(ran))
    days = [item for item in ran if item in starts]
    result: dict[int, list[tuple[int, datetime, int, int, int, int]]] = {}
    if not days:
        return result
    for player_id, day, type_id, defenses, three, lost in connection.execute(
        """
        SELECT saved.population_player_id, saved.ranked_day_start, label.type_id,
               sum(saved.defenses), sum(saved.three_star), sum(saved.trophies_lost)
        FROM unnest(%s::bigint[], %s::timestamptz[]) AS ran (player_id, start)
        JOIN army_package_defense_day_counts AS saved
          ON saved.ranked_day_start = ran.start
         AND saved.population_player_id = ran.player_id
        JOIN army_package_types AS label ON label.exact_army_id = saved.exact_army_id
        GROUP BY 1, 2, 3
        """,
        ([player_id for player_id, _ in days], [day for _, day in days]),
    ).fetchall():
        key = (int(player_id), day)
        result.setdefault(ran[key], []).append(
            (starts[key], day, int(type_id), int(defenses), int(three), int(lost)))
    return result


def _score(
    defenses: list[tuple[int, datetime, int, int, int, int]], about: int, within: int,
    meta: dict[int, list[float]], tomorrow: datetime, names: dict[int, str],
) -> dict[str, Any]:
    """One base's holds of 8 against the meta near ``about``."""
    span = within
    for widen in (1, 2):
        span = within * widen
        count = sum(n for start, _, _, n, _, _ in defenses if abs(start - about) <= span)
        if count >= RANKED:
            break
    total = sum(item[0] for item in meta.values())
    holds = lost = weighted = 0.0
    why = []
    for type_id, (met, tripled, lost_near) in sorted(meta.items(), key=lambda item: -item[1][0]):
        share = met / total
        prior = tripled / met
        held_prior = (lost_near - TRIPLE_LOSS * tripled) / (met - tripled) if met > tripled else 20
        n = t = held = held_lost = 0.0
        raw = raw_three = 0
        for start, day, army, count_, three, lost_ in defenses:
            if army != type_id:
                continue
            weight = near(start - about, span) * recency(day, tomorrow)
            if weight < 0.01:
                continue
            n += weight * count_
            t += weight * three
            held += weight * (count_ - three)
            held_lost += weight * (lost_ - TRIPLE_LOSS * three)
            raw += count_
            raw_three += three
        rate = (t + PRETEND * prior) / (n + PRETEND)
        held_loss = (held_lost + PRETEND * held_prior) / (held + PRETEND)
        holds += 8 * share * (1 - rate)
        lost += 8 * share * (rate * TRIPLE_LOSS + (1 - rate) * held_loss)
        weighted += n
        if share >= 0.005:
            why.append({"name": names.get(type_id, f"Type {type_id}"), "share": round(share, 3),
                        "defenses": raw, "tripled": raw_three, "holds": round(1 - rate, 3)})
    share_held = holds / 8
    spread = 1.64 * math.sqrt(share_held * (1 - share_held) / (weighted + PRETEND))
    recent = sum(n for start, day, _, n, _, _ in defenses
                 if abs(start - about) <= span and recency(day, tomorrow) == 1)
    return {
        "ranked": count >= RANKED, "near": count, "recent": recent, "width": span,
        "widened": span > within, "holds": round(holds, 1),
        "low": round(max(0.0, 8 * (share_held - spread)), 1),
        "high": round(min(8.0, 8 * (share_held + spread)), 1),
        "lost": round(lost), "why": why[:6],
    }


def get_tomorrow(
    database: ApiDatabase, account_id: int, *, now: datetime, live_board: LiveBoardCache,
    tag: str | None = None,
) -> dict[str, Any]:
    """The Tomorrow screen for one of the account's game accounts: ``tag``,
    or its first Legend account."""
    from .army_types import TYPE_NAMES

    note_visit(database, account_id, now=now)
    board = live_board.current(database, now).rows
    with database.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        players = sorted(_linked_players(connection, account_id, None),
                         key=lambda player: "Legend" not in (player[4] or ""))
        if not players:
            raise ApiError(404, "player_not_verified")
        chosen = next((player for player in players if player[1] == tag), None)
        if chosen is None and tag is not None:
            raise ApiError(404, "player_not_verified")
        player_id, chosen_tag, name, *_ = chosen or players[0]
        today = ranked_day_for(now)
        tomorrow = ranked_day_for(today.start + DAY)
        about = _about(connection, now, player_id, chosen_tag, board)
        if about is not None and tomorrow.day_number == 1:
            about["about"] = SEASON_START
        found, history = _bases(connection, account_id, now)
        picked = base_on(history.runs.get(player_id, []), tomorrow.start)
        result: dict[str, Any] = {
            "kind": "notebook-tomorrow",
            "player": {"tag": chosen_tag, "name": name},
            "players": [{"tag": player[1], "name": player[2]} for player in players],
            "day": tomorrow.day_number,
            "weekday": tomorrow.start.strftime("%A"),
            "trophies": about,
            "width": None, "gap": None, "meta": [], "bases": [], "untried": [],
        }
        if about is None:
            return result
        within = width(about["about"], tomorrow.start)
        meta = _meta(connection, about["about"], within, today.start)
        total = sum(item[0] for item in meta.values())
        result.update(
            width=within, gap=gap(about["about"]),
            meta=[{"name": TYPE_NAMES.get(type_id, f"Type {type_id}"),
                   "share": round(item[0] / total, 3)}
                  for type_id, item in sorted(meta.items(), key=lambda item: -item[1][0])
                  if item[0] / total >= 0.005][:5] if total else [],
        )
        defenses = _defenses(connection, list(found), today.start, tomorrow.start)
    scored = []
    for base_id, base in found.items():
        shown = {**base, "running": base_id == picked}
        if base_id not in defenses or not total:
            result["untried"].append(shown)
            continue
        scored.append({**shown, **_score(defenses[base_id], about["about"], within, meta,
                                         tomorrow.start, TYPE_NAMES)})
    scored.sort(key=lambda base: (not base["ranked"], -base["holds"], base["name"].casefold()))
    result["bases"] = scored
    result["untried"].sort(key=lambda base: base["name"].casefold())
    return result
