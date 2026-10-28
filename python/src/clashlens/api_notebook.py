"""Notebook (migration 0132): bases with one base ID each, notebooks that
point to them, and the base each game account ran on each Legend day.

A base stands on its own; a notebook entry only points to it, so adding or
removing one never copies or deletes a base or its results. Every Clash
Lens account has one personal notebook. Only a base's maker edits it, and a
base someone picked is never deleted.

A day pick says which base a verified game account ran from a Legend day
on. A game account's base on a day is its latest pick on or before it, so a
base carries forward, into the next Season too, until the next pick or the
first day the account is out of Legend League: one base per game account
per Legend day, across every notebook. Picks can be set from this Season's
Day 1 to tomorrow; a finished Season is locked. A run is a stretch of
Legend days one game account ran one base.

A base can be set only on Legend days: days in a week the weekly league
record (league_week) says was Legend I. This week and tomorrow's, until the
record decides them, follow the week before, as Daily boards and tracking
assume a week carries on until decided.

A base's record adds up the worker's per-day defense counts
(army_package_defense_day_counts, migration 0106) over the counted Legend
days anyone ran it, read live, so it grows as each finished Legend day is
counted, after 07:00 UTC (army_types.QUIET_END). A counted day with no
defense and no saved Legend day for that account is "no data", never zero.
Held means the attacker did not get 3 stars. Nothing here runs at the Reset
or in the worker.

Each Season has its own page: the bases put on it, by adding or bringing
them back then, and the ones the notebook's game accounts ran in it, so a
base still running when a Season ends carries over by itself and the rest
stay on their Season's page, one tap from coming back. On this Season's
page a base is Running while one of the notebook's game accounts runs it
today or from tomorrow, Retired once the Clasher retires it or 30 days after
the later of its last counted day and when it was added or brought back,
and otherwise just on this Season.

Every write but Undo returns what undoes it.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .api import ApiError, StrictBody, _authorize, _safe_tag
from .api_accounts import _linked_players
from .api_db import ApiDatabase, _text
from .domain import RANKED_DAY_DURATION, SEASON_DURATION, RankedDay, ranked_day_for
from .league_week import WEEK, week_start

DAY = RANKED_DAY_DURATION
# How far before a page's first day a pick can still carry a base into it.
LOOKBACK = 4 * SEASON_DURATION
# A base not run, added or brought back for this long retires on its own.
RETIRE_AFTER = timedelta(days=30)
# Crockford's base-32 alphabet: no I, L, O or U, so no 1/I/L or 0/O mix-ups.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE = re.compile(r"B-[0-9A-HJKMNP-TV-Z]{6}")
# A Season's official ID: its start in Unix seconds.
_SEASON = re.compile(r"[0-9]{1,12}")
# The in-game link that opens a layout; nothing else may be saved as one.
_BASE_LINK = re.compile(r"https://link\.clashofclans\.com/[A-Za-z0-9/?=&%._:;,+-]{1,270}")
# The invisible joiner inside emoji such as ❤️‍🔥, kept in names and notes.
_JOINER = "\u200d"
MAX_NAME = 60
MAX_NOTE = 1000


def new_code() -> str:
    """A random base ID: B- and 6 characters, about 1.07 billion in all."""
    return "B-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def parse_code(value: str) -> str:
    """``value`` if it is a base ID, or ApiError."""
    if not _CODE.fullmatch(value):
        raise ApiError(404, "base_not_found")
    return value


def _refuse_hidden(text: str) -> None:
    """Refuse an invisible control or format character other than a line
    break, a tab or the joiner, a surrogate or a private-use one. Unassigned
    code points pass: Python 3.12 has emoji newer than Unicode 15 as those."""
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}
        and character not in "\n\t" + _JOINER
        for character in text
    ):
        raise ApiError(422, "invalid_characters")


def clean_name(value: str) -> str:
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(name) <= MAX_NAME:
        raise ApiError(422, "invalid_name")
    _refuse_hidden(name)
    return name


def clean_link(value: str) -> str | None:
    """A base link, or None to remove it."""
    link = value.strip()
    if not link:
        return None
    if not _BASE_LINK.fullmatch(link):
        raise ApiError(422, "invalid_base_link")
    return link


def clean_note(value: str) -> str:
    note = unicodedata.normalize("NFC", value).strip()
    if len(note) > MAX_NOTE:
        raise ApiError(422, "invalid_note")
    _refuse_hidden(note)
    return note


def season_day(season: str | None, now: datetime) -> RankedDay:
    """The page's day: today for this Season (``season`` None or its ID), or
    the last day of an earlier one. ApiError for any other ID."""
    today = ranked_day_for(now)
    if season is None or season == today.official_season_id:
        return today
    try:
        if not _SEASON.fullmatch(season):
            raise ValueError
        first = ranked_day_for(datetime.fromtimestamp(int(season), UTC))
    except (OverflowError, OSError, ValueError):
        raise ApiError(404, "season_not_found") from None
    if first.official_season_id != season or first.season_start > today.season_start:
        raise ApiError(404, "season_not_found")
    return ranked_day_for(first.season_end - timedelta(microseconds=1))


Run = tuple[datetime, datetime, int]


def runs(
    picks: dict[datetime, int | None], first: datetime, last: datetime, legend: set[datetime]
) -> list[Run]:
    """Each stretch of ``legend`` days from ``first`` to ``last`` on one base,
    as (first day, last day, base), from the day picks {day: base or None for
    no base}. A pick stops at the first day not in ``legend``; days before
    the first pick or with no base are in no run."""
    result: list[Run] = []
    base: int | None = None
    day = first
    while day <= last:
        if day in picks:
            base = picks[day]
        if day not in legend:
            base = None
        if base is not None:
            if result and result[-1][2] == base and result[-1][1] == day - DAY:
                result[-1] = (result[-1][0], day, base)
            else:
                result.append((day, day, base))
        day += DAY
    return result


def base_on(found: list[Run], day: datetime) -> int | None:
    """The base a game account runs on ``day``, from its runs."""
    return next((base for start, end, base in found if start <= day <= end), None)


def _record() -> dict[str, Any]:
    return {"days": 0, "no_data": 0, "defenses": 0, "held": 0, "stars": [0, 0, 0, 0],
            "trophies_lost": 0}


def _add(record: dict[str, Any], counts: list[int] | None) -> None:
    """Add one counted Legend day: counts are (defenses, one-star, two-star,
    three-star, trophies lost), None for a day with no data."""
    if counts is None:
        record["no_data"] += 1
        return
    record["days"] += 1
    defenses, one, two, three, lost = counts
    record["defenses"] += defenses
    record["held"] += defenses - three
    stars = record["stars"]
    stars[0] += defenses - one - two - three
    stars[1] += one
    stars[2] += two
    stars[3] += three
    record["trophies_lost"] += lost


class _History:
    """Some game accounts' runs from ``first`` to ``last`` and their counted
    Legend days from ``counted_from`` on, read in one transaction."""

    def __init__(
        self, connection: Any, player_ids: list[int], first: datetime, last: datetime,
        counted_from: datetime, this_week: datetime,
    ) -> None:
        self.picks: dict[int, dict[datetime, int | None]] = {
            player_id: {} for player_id in player_ids
        }
        for player_id, start, base_id in connection.execute(
            """
            SELECT player_id, ranked_day_start, base_id FROM notebook_day_picks
            WHERE player_id = ANY(%s) AND ranked_day_start >= %s AND ranked_day_start <= %s
            """,
            (player_ids, first, last),
        ).fetchall():
            self.picks[int(player_id)][start] = None if base_id is None else int(base_id)
        weeks: dict[tuple[int, datetime], bool] = {
            (int(player_id), start): league == "legend_i"
            for player_id, start, league in connection.execute(
                """
                SELECT player_id, week_start, league FROM player_league_weeks
                WHERE player_id = ANY(%s) AND week_start >= %s AND week_start <= %s
                """,
                (player_ids, week_start(first) - WEEK, last),
            ).fetchall()
        }
        self.legend: dict[int, set[datetime]] = {}
        self.runs: dict[int, list[Run]] = {}
        for player_id in player_ids:
            legend = self.legend[player_id] = set()
            day = first
            while day <= last:
                week = week_start(day)
                while week >= this_week and (player_id, week) not in weeks:
                    week -= WEEK
                if weeks.get((player_id, week), False):
                    legend.add(day)
                day += DAY
            self.runs[player_id] = runs(self.picks[player_id], first, last, legend)
        self.counted = {
            start for (start,) in connection.execute(
                "SELECT ranked_day_start FROM army_type_defense_counted_days"
                " WHERE ranked_day_start >= %s AND ranked_day_start <= %s",
                (counted_from, last),
            ).fetchall()
        }
        self.counts = {
            (int(player_id), start): [int(value) for value in values]
            for player_id, start, *values in connection.execute(
                """
                SELECT population_player_id, ranked_day_start, sum(defenses),
                       sum(one_star), sum(two_star), sum(three_star), sum(trophies_lost)
                FROM army_package_defense_day_counts
                WHERE population_player_id = ANY(%s)
                  AND ranked_day_start >= %s AND ranked_day_start <= %s
                GROUP BY 1, 2
                """,
                (player_ids, counted_from, last),
            ).fetchall()
        }
        # Saved Legend days: a counted day with no defense is a real zero
        # only when the day itself was saved.
        self.saved = {
            (int(player_id), start)
            for player_id, start in connection.execute(
                """
                SELECT DISTINCT player_id, ranked_day_start FROM api_player_daily_logs
                WHERE player_id = ANY(%s)
                  AND ranked_day_start >= %s AND ranked_day_start <= %s
                """,
                (player_ids, counted_from, last),
            ).fetchall()
        }

    def day_counts(self, player_id: int, day: datetime) -> list[int] | None:
        """A counted day's counts on one game account, None for no data."""
        counts = self.counts.get((player_id, day))
        if counts is None and (player_id, day) in self.saved:
            return [0, 0, 0, 0, 0]
        return counts


class _Notebook:
    """One account's notebook as this Season's page, or the past Season
    ``season``'s read as on its last day, in one transaction."""

    def __init__(
        self, connection: Any, account_id: int, now: datetime, season: str | None = None
    ) -> None:
        self.connection = connection
        self.account_id = account_id
        self.now = now
        today = ranked_day_for(now)
        shown = season_day(season, now)
        self.this_season = today.official_season_id
        self.current = shown.official_season_id == self.this_season
        self.season_id = shown.official_season_id
        self.season_start = shown.season_start
        self.season_end = shown.season_end
        self.today = today.start
        self.tomorrow = today.start + DAY
        # Picks are set from this Season's Day 1 to tomorrow, which on Day 28
        # is the next Season's Day 1.
        self.editable_from = today.season_start
        # The page's last day: tomorrow for this Season, its Day 28 otherwise.
        self.last = self.tomorrow if self.current else shown.season_end - DAY
        # Legend players first, each kind by tag.
        self.players = sorted(
            _linked_players(connection, account_id, None),
            key=lambda player: "Legend" not in (player[4] or ""),
        )
        self.player_ids = {tag: player_id for player_id, tag, *_ in self.players}
        row = connection.execute(
            "SELECT id FROM notebooks WHERE account_id = %s", (account_id,)
        ).fetchone()
        self.notebook_id: int | None = None if row is None else int(row[0])

    def day_number(self, at: datetime) -> int:
        """``at``'s day number in the page's Season; 29 is the next Season's
        Day 1."""
        return (at - self.season_start) // DAY + 1

    def day_start(self, number: int) -> datetime:
        return self.season_start + (number - 1) * DAY

    def ensure(self) -> int:
        """The notebook's id, making it on its first change."""
        if self.notebook_id is None:
            self.connection.execute(
                "INSERT INTO notebooks (account_id) VALUES (%s)"
                " ON CONFLICT (account_id) DO NOTHING",
                (self.account_id,),
            )
            row = self.connection.execute(
                "SELECT id FROM notebooks WHERE account_id = %s", (self.account_id,)
            ).fetchone()
            self.notebook_id = int(row[0])
        return self.notebook_id

    def entries(self) -> dict[int, dict[str, Any]]:
        """Every base the notebook points to: {base id: base and entry}."""
        if self.notebook_id is None:
            return {}
        return {
            int(row[0]): {
                "code": _text(row[1]), "name": _text(row[2]), "note": _text(row[3]),
                "link": None if row[4] is None else _text(row[4]),
                "mine": row[5] == self.account_id,
                "created_at": row[6], "updated_at": row[7], "season": _text(row[8]),
                "added_at": row[9], "kept_at": row[10], "retired_at": row[11],
            }
            for row in self.connection.execute(
                """
                SELECT base.id, base.code, base.name, base.note, base.base_link,
                       base.made_by, base.created_at, base.updated_at,
                       entry.season_id, entry.added_at, entry.kept_at, entry.retired_at
                FROM notebook_entries AS entry
                JOIN notebook_bases AS base ON base.id = entry.base_id
                WHERE entry.notebook_id = %s
                ORDER BY entry.added_at, base.code
                """,
                (self.notebook_id,),
            ).fetchall()
        }

    def history(self, base_ids: list[int]) -> _History:
        """The runs of the notebook's game accounts and of anyone who ran one
        of ``base_ids``, from far enough back to know what carried into the
        page and whether a base retired on its own, up to tomorrow."""
        counted_from = min(self.season_start, self.today - RETIRE_AFTER)
        first = counted_from - LOOKBACK
        others = [
            int(player_id) for (player_id,) in self.connection.execute(
                """
                SELECT DISTINCT player_id FROM notebook_day_picks
                WHERE base_id = ANY(%s) AND ranked_day_start >= %s
                  AND ranked_day_start <= %s
                """,
                (base_ids, first, self.tomorrow),
            ).fetchall()
        ]
        players = sorted(set(self.player_ids.values()) | set(others))
        return _History(self.connection, players, first, self.tomorrow, counted_from,
                        week_start(self.today))

    def mine_up(self, history: _History, day: datetime) -> set[int]:
        """The bases the notebook's game accounts run on ``day``."""
        return {
            base for player_id in self.player_ids.values()
            if (base := base_on(history.runs[player_id], day)) is not None
        }

    def statuses(
        self, entries: dict[int, dict[str, Any]], history: _History
    ) -> dict[int, str]:
        """Each base's status on this Season's page: Running, Retired or on
        this Season."""
        up = self.mine_up(history, self.today) | self.mine_up(history, self.tomorrow)
        last_counted: dict[int, datetime] = {}
        for player_id, found in history.runs.items():
            for start, end, base in found:
                day = start
                while day <= min(end, self.today - DAY):
                    if day in history.counted and history.day_counts(player_id, day):
                        last_counted[base] = max(last_counted.get(base, day), day)
                    day += DAY
        result = {}
        for base_id, entry in entries.items():
            active = max([entry["kept_at"],
                          *([last_counted[base_id] + DAY] if base_id in last_counted else [])])
            if base_id in up:
                result[base_id] = "running"
            elif entry["retired_at"] is not None or active + RETIRE_AFTER <= self.now:
                result[base_id] = "retired"
            else:
                result[base_id] = "season"
        return result

    def on_page(
        self, entries: dict[int, dict[str, Any]], history: _History, season: str,
        start: datetime, end: datetime,
    ) -> set[int]:
        """The bases on one Season's page, from ``start`` to ``end``: put on
        it, or run on it by the notebook's game accounts."""
        ran = {
            base for player_id in self.player_ids.values()
            for first, last, base in history.runs[player_id]
            if first <= end and last >= start
        }
        return {
            base_id for base_id, entry in entries.items()
            if entry["season"] == season or base_id in ran
        }

    def this_page(self, entries: dict[int, dict[str, Any]], history: _History) -> set[int]:
        start = self.editable_from
        return self.on_page(entries, history, self.this_season, start,
                            max(self.tomorrow, start + SEASON_DURATION - DAY))

    def legend_numbers(self, history: _History, player_id: int) -> list[int]:
        """The page's days, up to tomorrow, a base can be set on."""
        return sorted(
            self.day_number(day) for day in history.legend[player_id]
            if self.season_start <= day <= self.last
        )


def _player(notebook: _Notebook, tag: str | None) -> int:
    """The verified player ``tag`` names, or the only one when it is None."""
    if tag is None:
        if len(notebook.player_ids) != 1:
            raise ApiError(422, "player_required")
        return next(iter(notebook.player_ids.values()))
    player_id = notebook.player_ids.get(_safe_tag(tag))
    if player_id is None:
        raise ApiError(404, "player_not_verified")
    return player_id


def _base(notebook: _Notebook, code: str | None) -> int:
    """The base ``code`` names in this notebook, or ApiError."""
    if code is None:
        raise ApiError(422, "base_required")
    row = notebook.connection.execute(
        """
        SELECT base.id FROM notebook_bases AS base
        JOIN notebook_entries AS entry ON entry.base_id = base.id
        WHERE base.code = %s AND entry.notebook_id = %s
        """,
        (parse_code(code), notebook.notebook_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "base_not_found")
    return int(row[0])


def _moment(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(422, "invalid_request") from None
    if moment.tzinfo is None:
        raise ApiError(422, "invalid_request")
    return moment


def _when(value: str) -> datetime:
    moment = _moment(value)
    assert moment is not None
    return moment


def get_notebook(
    database: ApiDatabase, account_id: int, *, now: datetime,
    season: str | None = None, base: str | None = None,
) -> dict[str, Any]:
    """The account's notebook as one Season's page: its game accounts, each
    one's runs and base, and every base on the page with its record from
    everyone who ran it. With ``base``, also that base's note, link and
    record on each of the notebook's game accounts."""
    with database.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        notebook = _Notebook(connection, account_id, now, season)
        entries = notebook.entries()
        detail = _base(notebook, base) if base is not None else None
        history = notebook.history(list(entries))
        statuses = notebook.statuses(entries, history)
        here = notebook.this_page(entries, history)
        page = notebook.on_page(entries, history, notebook.season_id,
                                notebook.season_start, notebook.season_end - DAY)
        seasons = {notebook.this_season} | {entry["season"] for entry in entries.values()}
        for (start,) in connection.execute(
            "SELECT DISTINCT ranked_day_start FROM notebook_day_picks"
            " WHERE player_id = ANY(%s) AND base_id IS NOT NULL",
            (list(notebook.player_ids.values()),),
        ).fetchall():
            seasons.add(ranked_day_for(start).official_season_id)
    if detail is not None:
        page.add(detail)
    first, last_counted = notebook.season_start, min(notebook.today, notebook.season_end) - DAY
    codes = {base_id: entry["code"] for base_id, entry in entries.items()}
    records = {base_id: _record() for base_id in entries}
    ran_by: dict[int, set[int]] = {base_id: set() for base_id in entries}
    last_ran: dict[int, int] = {}
    mine = set(notebook.player_ids.values())
    by_player: dict[int, dict[str, Any]] = {}
    player_runs: dict[int, list[dict[str, Any]]] = {player_id: [] for player_id in mine}
    for player_id, found in history.runs.items():
        for start, end, base_id in found:
            if base_id not in entries or end < first or start > min(notebook.today,
                                                                    notebook.season_end - DAY):
                continue
            start, end = max(start, first), min(end, notebook.today, notebook.season_end - DAY)
            ran_by[base_id].add(player_id)
            record = _record()
            day = start
            while day <= min(end, last_counted):
                if day in history.counted:
                    counts = history.day_counts(player_id, day)
                    _add(records[base_id], counts)
                    _add(record, counts)
                    if base_id == detail and player_id in mine:
                        _add(by_player.setdefault(player_id, _record()), counts)
                day += DAY
            if player_id in mine:
                last_ran[base_id] = max(last_ran.get(base_id, 0), notebook.day_number(end))
                player_runs[player_id].append({
                    "from": notebook.day_number(start), "to": notebook.day_number(end),
                    "base": codes[base_id], "record": record,
                })
    players = []
    for player_id, tag, name, _clan, league in notebook.players:
        found = history.runs[player_id]
        current = base_on(found, notebook.today) if notebook.current else None
        picks = {
            day: picked for day, picked in history.picks[player_id].items()
            if notebook.season_start <= day <= notebook.last
        }
        following = base_on(found, notebook.tomorrow)
        players.append({
            "tag": tag,
            "name": name,
            "league": league,
            "runs": player_runs[player_id],
            "current": None if current is None else codes.get(current),
            "legend_days": notebook.legend_numbers(history, player_id),
            # A different base, or none, from tomorrow, shown beside today's.
            "next": (
                {"day": notebook.day_number(notebook.tomorrow),
                 "base": None if following is None else codes.get(following)}
                if notebook.current and notebook.tomorrow in picks and following != current
                else None
            ),
            "picks": [
                {"day": notebook.day_number(day),
                 "base": None if picked is None else codes.get(picked)}
                for day, picked in sorted(picks.items())
            ],
        })
    counted_days = [day for day in history.counted if first <= day <= last_counted]
    result: dict[str, Any] = {
        "kind": "notebook",
        "season": {
            "id": notebook.season_id,
            "start": notebook.season_start.isoformat(),
            "end": notebook.season_end.isoformat(),
            "today": notebook.day_number(min(notebook.today, notebook.season_end - DAY)),
            "tomorrow": notebook.day_number(notebook.last),
            "current": notebook.current,
        },
        "seasons": sorted(seasons, key=int, reverse=True),
        # The newest Legend day whose defenses are counted: records are as of it.
        "as_of_day": notebook.day_number(max(counted_days)) if counted_days else None,
        "players": players,
        "bases": [
            {
                "id": entry["code"],
                "name": entry["name"],
                "mine": entry["mine"],
                # Running, retired or season on this Season's page.
                "status": statuses[base_id],
                # On this Season's page; a past page offers to bring it back.
                "here": base_id in here,
                "added_day": (
                    notebook.day_number(entry["added_at"])
                    if notebook.season_start <= entry["added_at"] < notebook.season_end
                    else None
                ),
                "last_ran_day": last_ran.get(base_id),
                "ran_by": len(ran_by[base_id]),
                "record": records[base_id],
            }
            for base_id, entry in entries.items()
            if base_id in page
        ],
    }
    if detail is not None:
        entry = entries[detail]
        result["base"] = {
            "id": entry["code"],
            "note": entry["note"],
            "link": entry["link"],
            "players": [
                {"tag": tag, "record": by_player[player_id]}
                for player_id, tag, *_ in notebook.players if player_id in by_player
            ],
        }
    return result


class UndoPick(StrictBody):
    tag: str = Field(min_length=2, max_length=16)
    # This Season's day; 29 is the next Season's Day 1.
    day: int = Field(ge=1, le=29)
    # None: no base from that day; absent: no pick on that day.
    base: str | None = Field(default=None, max_length=12)
    absent: bool = False


class UndoEntry(StrictBody):
    base: str = Field(max_length=12)
    # Not in the notebook before the change.
    absent: bool = False
    season: str = Field(default="0", max_length=12)
    added_at: str = Field(default="", max_length=40)
    kept_at: str = Field(default="", max_length=40)
    retired_at: str | None = Field(default=None, max_length=40)


class UndoEdit(StrictBody):
    base: str = Field(max_length=12)
    name: str = Field(max_length=200)
    # "": no base link.
    link: str = Field(max_length=400)
    note: str = Field(max_length=2 * MAX_NOTE)


class UndoBase(UndoEdit):
    created_at: str = Field(max_length=40)
    updated_at: str = Field(max_length=40)


class Undo(StrictBody):
    # A change touches at most one base on one player's two days.
    picks: list[UndoPick] = Field(default_factory=list, max_length=60)
    # The notebook's entry for the base before the change.
    entry: UndoEntry | None = None
    # A base the change made, removed on undo.
    delete: str | None = Field(default=None, max_length=12)
    # A base's name, link and note before an edit.
    edit: UndoEdit | None = None
    # A base the change deleted, put back first.
    restore: UndoBase | None = None


class NotebookWrite(StrictBody):
    action: Literal["add", "pick", "retire", "keep", "edit", "remove", "undo"]
    base: str | None = Field(default=None, max_length=12)
    name: str | None = Field(default=None, max_length=200)
    link: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=2 * MAX_NOTE)
    tag: str | None = Field(default=None, max_length=16)
    day: int | None = Field(default=None, ge=1, le=29)
    undo: Undo | None = None


def _edit(
    connection: Any, base_id: int, name: str | None, link: str | None, note: str | None
) -> None:
    """Rename a base, change its base link ("" removes it) or its note; None
    leaves that as it is."""
    connection.execute(
        """
        UPDATE notebook_bases SET
            name = COALESCE(%s, name),
            base_link = CASE WHEN %s THEN %s ELSE base_link END,
            note = COALESCE(%s, note),
            updated_at = clock_timestamp()
        WHERE id = %s
        """,
        (None if name is None else clean_name(name), link is not None,
         None if link is None else clean_link(link),
         None if note is None else clean_note(note), base_id),
    )


def _drop_if_unused(connection: Any, base_id: int) -> bool:
    """Delete a base no notebook points to and no one ever picked."""
    return connection.execute(
        """
        DELETE FROM notebook_bases AS base WHERE base.id = %s
          AND NOT EXISTS (SELECT FROM notebook_entries WHERE base_id = base.id)
          AND NOT EXISTS (SELECT FROM notebook_day_picks WHERE base_id = base.id)
        RETURNING id
        """,
        (base_id,),
    ).fetchone() is not None


def save(
    database: ApiDatabase, account_id: int, body: NotebookWrite, *, now: datetime
) -> dict[str, Any]:
    """Apply one change to the account's notebook and say what undoes it."""
    with database.pool.connection() as connection, connection.transaction():
        # One change to an account's notebook at a time.
        connection.execute(
            "SELECT 1 FROM clash_lens_accounts WHERE id = %s FOR NO KEY UPDATE",
            (account_id,),
        )
        notebook = _Notebook(connection, account_id, now)
        notebook_id = notebook.ensure()
        today_number = notebook.day_number(notebook.today)
        tomorrow_number = today_number + 1

        def state() -> tuple[dict[int, dict[str, Any]], _History, dict[int, str]]:
            entries = notebook.entries()
            history = notebook.history(list(entries))
            return entries, history, notebook.statuses(entries, history)

        entries, history, before = state()
        touched: dict[tuple[int, int], tuple[bool, int | None]] = {}

        def pick(player_id: int, day: int, base_id: int | None, *, drop: bool = False) -> None:
            """Set (or with ``drop`` remove) the player's pick on ``day``,
            remembering what it was the first time it changes."""
            start = notebook.day_start(day)
            row = connection.execute(
                "SELECT base_id FROM notebook_day_picks"
                " WHERE player_id = %s AND ranked_day_start = %s",
                (player_id, start),
            ).fetchone()
            touched.setdefault((player_id, day), (
                row is not None, None if row is None or row[0] is None else int(row[0])
            ))
            if drop:
                connection.execute(
                    "DELETE FROM notebook_day_picks"
                    " WHERE player_id = %s AND ranked_day_start = %s",
                    (player_id, start),
                )
                return
            connection.execute(
                """
                INSERT INTO notebook_day_picks (player_id, ranked_day_start, base_id, picked_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (player_id, ranked_day_start)
                DO UPDATE SET base_id = EXCLUDED.base_id, picked_by = EXCLUDED.picked_by,
                              picked_at = clock_timestamp()
                """,
                (player_id, start, base_id, account_id),
            )

        def pick_day(day: int | None, *, past: bool) -> int:
            """A day a pick may start on: today or tomorrow, or with ``past``
            any day of this Season up to tomorrow."""
            if day is None:
                raise ApiError(422, "day_required")
            if not (1 if past else today_number) <= day <= tomorrow_number:
                raise ApiError(422, "invalid_day")
            return day

        def require_legend(player_id: int, day: int) -> None:
            if notebook.day_start(day) not in history.legend[player_id]:
                raise ApiError(422, "not_legend_day")

        def entry_state(base_id: int) -> dict[str, Any]:
            """The base's entry as Undo puts it back."""
            entry = entries[base_id]
            return {"base": entry["code"], "absent": False, "season": entry["season"],
                    "added_at": entry["added_at"].isoformat(),
                    "kept_at": entry["kept_at"].isoformat(),
                    "retired_at": None if entry["retired_at"] is None
                    else entry["retired_at"].isoformat()}

        made: str | None = None
        edited: dict[str, str] | None = None
        gone: dict[str, str] | None = None
        entry_before: dict[str, Any] | None = None
        base_id: int | None = None
        if body.action == "add":
            name = clean_name(body.name or "")
            link = clean_link(body.link or "")
            note = clean_note(body.note or "")
            runner: int | None = None
            if body.tag is not None or body.day is not None:
                runner = _player(notebook, body.tag)
                day = pick_day(body.day, past=False)
                require_legend(runner, day)
            for _attempt in range(5):
                row = connection.execute(
                    """
                    INSERT INTO notebook_bases (code, made_by, name, note, base_link)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO NOTHING RETURNING id, code
                    """,
                    (new_code(), account_id, name, note, link),
                ).fetchone()
                if row is not None:
                    break
            else:  # pragma: no cover - five draws all taken: about 1 in 10^15.
                raise ApiError(503, "busy")
            base_id, made = int(row[0]), _text(row[1])
            # The API's clock, not the database's, dates entries: their
            # Retired date and Day added follow the Legend day it reads.
            connection.execute(
                "INSERT INTO notebook_entries (notebook_id, base_id, season_id, added_at,"
                " kept_at) VALUES (%s, %s, %s, %s, %s)",
                (notebook_id, base_id, notebook.this_season, now, now),
            )
            if runner is not None:
                pick(runner, day, base_id)
        elif body.action == "pick":
            player_id = _player(notebook, body.tag)
            base_id = None if body.base is None else _base(notebook, body.base)
            day = pick_day(body.day, past=True)
            # Clearing a day is always allowed; setting one needs a Legend day.
            if base_id is not None:
                require_legend(player_id, day)
            pick(player_id, day, base_id)
        elif body.action in {"retire", "keep"}:
            base_id = _base(notebook, body.base)
            entry_before = entry_state(base_id)
            if body.action == "keep":
                connection.execute(
                    "UPDATE notebook_entries SET season_id = %s, kept_at = %s,"
                    " retired_at = NULL WHERE notebook_id = %s AND base_id = %s",
                    (notebook.this_season, now, notebook_id, base_id),
                )
            else:
                connection.execute(
                    "UPDATE notebook_entries SET retired_at = %s"
                    " WHERE notebook_id = %s AND base_id = %s",
                    (now, notebook_id, base_id),
                )
                # Stop it on every game account running it, from tomorrow: a
                # pick of it for tomorrow goes when another base is up today,
                # so that one carries on; otherwise tomorrow has no base.
                for player_id in notebook.player_ids.values():
                    found = history.runs[player_id]
                    if base_on(found, notebook.tomorrow) != base_id:
                        continue
                    pick(player_id, tomorrow_number, None,
                         drop=base_on(found, notebook.today) != base_id)
        elif body.action in {"edit", "remove"}:
            base_id = _base(notebook, body.base)
            entry = entries[base_id]
            kept = {"base": entry["code"], "name": entry["name"], "note": entry["note"],
                    "link": entry["link"] or "", "created_at": entry["created_at"].isoformat(),
                    "updated_at": entry["updated_at"].isoformat()}
            if body.action == "edit":
                if not entry["mine"]:
                    raise ApiError(403, "not_your_base")
                edited = {key: kept[key] for key in ("base", "name", "link", "note")}
                _edit(connection, base_id, body.name, body.link, body.note)
            else:
                if base_id in (notebook.mine_up(history, notebook.today)
                               | notebook.mine_up(history, notebook.tomorrow)):
                    raise ApiError(422, "base_running")
                entry_before = entry_state(base_id)
                connection.execute(
                    "DELETE FROM notebook_entries WHERE notebook_id = %s AND base_id = %s",
                    (notebook_id, base_id),
                )
                if entry["mine"] and _drop_if_unused(connection, base_id):
                    gone = kept
        else:
            if body.undo is None:
                raise ApiError(422, "undo_required")
            _undo(notebook, body.undo, pick, pick_day)
        _, _, after = state()
        # Every base named in the answer, including one a pick ran before it
        # left the notebook or one this change deleted.
        named = {old for _, old in touched.values() if old is not None} | set(entries)
        codes = {
            int(key): _text(code) for key, code in connection.execute(
                "SELECT id, code FROM notebook_bases WHERE id = ANY(%s)", (list(named),)
            ).fetchall()
        } | {key: entry["code"] for key, entry in entries.items()}
    tags = {player_id: tag for tag, player_id in notebook.player_ids.items()}
    changed = [
        {"base": codes[key], "status": after[key]}
        for key in sorted(after)
        if key in before and before[key] != after[key]
    ]
    undo = None if body.action == "undo" else {
        "picks": [
            {"tag": tags[player_id], "day": day,
             "base": None if old is None else codes[old], "absent": not existed}
            for (player_id, day), (existed, old) in sorted(touched.items())
        ],
        "entry": entry_before,
        "delete": made,
        "edit": edited,
        "restore": gone,
    }
    return {
        "kind": "notebook-saved",
        "base": made or (codes.get(base_id) if base_id is not None else None),
        "changed": changed,
        "undo": undo,
    }


def _undo(
    notebook: _Notebook, undo: Undo, pick: Callable[..., None],
    pick_day: Callable[..., int],
) -> None:
    """Put back the base a change deleted, then the picks, entry, name, link
    and note it replaced, and remove the base it made. Every base named must
    still be in this notebook, and every player this account's."""
    connection = notebook.connection
    notebook_id = notebook.ensure()
    if undo.restore is not None:
        base = undo.restore
        row = connection.execute(
            """
            INSERT INTO notebook_bases (code, made_by, name, note, base_link,
                                        created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO NOTHING RETURNING id
            """,
            (parse_code(base.base), notebook.account_id, clean_name(base.name),
             clean_note(base.note), clean_link(base.link), _when(base.created_at),
             _when(base.updated_at)),
        ).fetchone()
        if row is None:
            raise ApiError(422, "invalid_request")
    if undo.entry is not None:
        item = undo.entry
        row = connection.execute(
            "SELECT id, made_by FROM notebook_bases WHERE code = %s", (parse_code(item.base),)
        ).fetchone()
        if row is None:
            raise ApiError(404, "base_not_found")
        if item.absent:
            connection.execute(
                "DELETE FROM notebook_entries WHERE notebook_id = %s AND base_id = %s",
                (notebook_id, row[0]),
            )
        else:
            if not _SEASON.fullmatch(item.season):
                raise ApiError(422, "invalid_request")
            # Only a base this notebook pointed to, or this account made,
            # comes back.
            listed = connection.execute(
                "SELECT 1 FROM notebook_entries WHERE notebook_id = %s AND base_id = %s",
                (notebook_id, row[0]),
            ).fetchone()
            if listed is None and row[1] != notebook.account_id:
                raise ApiError(404, "base_not_found")
            connection.execute(
                """
                INSERT INTO notebook_entries (notebook_id, base_id, season_id, added_at,
                                              kept_at, retired_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (notebook_id, base_id) DO UPDATE SET
                    season_id = EXCLUDED.season_id, added_at = EXCLUDED.added_at,
                    kept_at = EXCLUDED.kept_at, retired_at = EXCLUDED.retired_at
                """,
                (notebook_id, row[0], item.season, _when(item.added_at),
                 _when(item.kept_at), _moment(item.retired_at)),
            )
    for item in undo.picks:
        player_id = _player(notebook, item.tag)
        day = pick_day(item.day, past=True)
        if item.absent:
            pick(player_id, day, None, drop=True)
        else:
            pick(player_id, day, None if item.base is None else _base(notebook, item.base))
    if undo.delete is not None:
        base_id = _base(notebook, undo.delete)
        connection.execute(
            "DELETE FROM notebook_entries WHERE notebook_id = %s AND base_id = %s",
            (notebook_id, base_id),
        )
        _drop_if_unused(connection, base_id)
    if undo.edit is not None:
        base_id = _base(notebook, undo.edit.base)
        _edit(connection, base_id, undo.edit.name, undo.edit.link, undo.edit.note)


def register_notebook_routes(
    app: FastAPI,
    *,
    database: ApiDatabase,
    current_time: Callable[[], datetime],
    enabled: bool,
) -> None:
    """The notebook's two routes, both answering 404 ``notebook_disabled``
    while the dashboard is switched off, before reading anything."""

    def switch() -> None:
        if not enabled:
            raise ApiError(404, "notebook_disabled")

    router = APIRouter(dependencies=[Depends(switch)])

    def account(request: Request, operation: str) -> int:
        context = _authorize(request, operation, database)
        assert context.account is not None
        return context.account.internal_id

    @router.get("/v1/account/notebook")
    def notebook(
        request: Request, season: str | None = None, base: str | None = None
    ) -> JSONResponse:
        account_id = account(request, "account.read")
        if base is not None and len(base) > 12:
            raise ApiError(404, "base_not_found")
        if season is not None and len(season) > 12:
            raise ApiError(404, "season_not_found")
        return JSONResponse(content=get_notebook(
            database, account_id, now=current_time(), season=season, base=base
        ))

    @router.post("/v1/account/notebook")
    def write(body: NotebookWrite, request: Request) -> JSONResponse:
        account_id = account(request, "account.update")
        return JSONResponse(content=save(database, account_id, body, now=current_time()))

    app.include_router(router)
