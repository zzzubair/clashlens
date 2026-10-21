"""Season notebook: one per Clash Lens account per Season (migration 0124).

A Clasher keeps bases in the notebook's Library, each with an ID like
B-7K3QX9, and says which base each of their verified players ran from which
Legend day: a day pick. A player's base on any day is their latest pick on
or before it, so a base carries forward until the next pick, and one player
can never run two bases on one Legend day. A pick stops at the first day
the player is out of Legend League, so a player back in it has no base
until a new pick. A run is a stretch of Legend days one player ran one base.

A base is Active while some player runs it today or has it picked for a
later day. Its saved status follows the picks: Active while some player
runs it from tomorrow, and once that stops, Retired if it ran or back to To
try if it never did. A change that puts a To try base on a day it ran makes
it Retired, and one that takes a Retired base off its last day run sends it
back to To try. To try and Retired are otherwise the Clasher's own choice.
A base up today shows Active whatever is saved, and one saved Active that
no player runs from tomorrow, as after its player left Legend League, shows
Retired or To try, so the label changes at the Reset with nothing run then.

A base can be set only on a player's Legend days: days in a week the
weekly league record (league_week) says was Legend I. This week and
tomorrow's, until the record decides them, follow the week before, as Daily
boards and tracking assume a week carries on until decided. A base's
record adds up the worker's per-day defense counts
(army_package_defense_day_counts, migration 0106) for the Legend days and
players it ran on, read live, so it grows as each finished Legend day is
counted, about 30 minutes after the Reset. Held means the attacker did not
get 3 stars. Nothing here runs at the Reset or in the worker.

A base saved from Bases you attacked keeps whose base it was and the Legend
day of the attack, once per attack, and only after the Clasher's player's
battle log that day shows the attack. Its record for them, Them beside the
Clasher's own, is that player's defense counts on that day, read the same
way, so it shows once that day is counted.

Every write but Undo returns what undoes it: the picks and statuses it
changed, a base it made, an edited base's old name and link, or a deleted
base to put back.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .api import ApiError, StrictBody, _authorize, _safe_tag
from .api_accounts import _linked_players
from .api_db import ApiDatabase, _screen_events, _text
from .domain import RANKED_DAY_DURATION, ranked_day_for
from .league_week import WEEK, week_start

# Crockford's base-32 alphabet: no I, L, O or U, so no 1/I/L or 0/O mix-ups.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE = re.compile(r"B-[0-9A-HJKMNP-TV-Z]{6}")
# The in-game link that opens a layout; nothing else may be saved as one.
_BASE_LINK = re.compile(r"https://link\.clashofclans\.com/[A-Za-z0-9/?=&%._:;,+-]{1,270}")
# The invisible joiner inside emoji such as ❤️‍🔥, kept in names and notes.
_JOINER = "\u200d"
MAX_NAME = 60
MAX_NOTE = 1000
Status = Literal["active", "to_try", "retired"]


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


def runs(
    picks: dict[int, int | None], last_day: int, legend: set[int]
) -> list[tuple[int, int, int]]:
    """Each stretch of ``legend`` days 1 to ``last_day`` on one base, as
    (first day, last day, base), from the day picks {day: base or None for
    no base}. A pick stops at the first day not in ``legend``; days before
    the first pick or with no base are in no run."""
    result: list[tuple[int, int, int]] = []
    base: int | None = None
    for day in range(1, last_day + 1):
        if day in picks:
            base = picks[day]
        if day not in legend:
            base = None
        if base is None:
            continue
        if result and result[-1][2] == base and result[-1][1] == day - 1:
            result[-1] = (result[-1][0], day, base)
        else:
            result.append((day, day, base))
    return result


def base_on(picks: dict[int, int | None], day: int, legend: set[int]) -> int | None:
    """The base a player runs on ``day``: the run that reaches it, if any."""
    found = runs(picks, day, legend)
    return found[-1][2] if found and found[-1][1] == day else None


def _record() -> dict[str, Any]:
    return {"days": 0, "defenses": 0, "held": 0, "stars": [0, 0, 0, 0],
            "trophies_lost": 0}


def _add(record: dict[str, Any], counts: list[int] | None) -> None:
    """Add one counted Legend day: counts are (defenses, one-star,
    two-star, three-star, trophies lost), None for a day with no defense."""
    record["days"] += 1
    if counts is None:
        return
    defenses, one, two, three, lost = counts
    record["defenses"] += defenses
    record["held"] += defenses - three
    stars = record["stars"]
    stars[0] += defenses - one - two - three
    stars[1] += one
    stars[2] += two
    stars[3] += three
    record["trophies_lost"] += lost


class _Notebook:
    """One account's notebook for the Season ``now`` falls in, read in one
    transaction."""

    def __init__(self, connection: Any, account_id: int, now: datetime) -> None:
        self.connection = connection
        self.account_id = account_id
        day = ranked_day_for(now)
        self.season_id = day.official_season_id
        self.season_start = day.season_start
        self.season_end = day.season_end
        self.today = day.day_number
        # Changes take effect in game from the next Legend day; on Day 28
        # that is the next Season's, so picks stop at today.
        self.tomorrow = min(day.day_number + 1, 28)
        # Legend players first, each kind by tag.
        self.players = sorted(
            _linked_players(connection, account_id, None),
            key=lambda player: "Legend" not in (player[4] or ""),
        )
        self.player_ids = {tag: player_id for player_id, tag, *_ in self.players}

    def day_start(self, day: int) -> datetime:
        return self.season_start + (day - 1) * RANKED_DAY_DURATION

    def day_number(self, at: datetime) -> int:
        return (at - self.season_start) // RANKED_DAY_DURATION + 1

    def bases(self) -> dict[int, dict[str, Any]]:
        return {
            int(row[0]): {
                "code": _text(row[1]), "name": _text(row[2]),
                "link": None if row[3] is None else _text(row[3]),
                "note": _text(row[4]), "created_at": row[5], "status_at": row[6],
            }
            for row in self.connection.execute(
                """
                SELECT id, code, name, base_link, note, created_at, status_at
                FROM notebook_bases
                WHERE account_id = %s AND season_id = %s
                ORDER BY created_at, id
                """,
                (self.account_id, self.season_id),
            ).fetchall()
        }

    def picks(self) -> dict[int, dict[int, int | None]]:
        """Each verified player's day picks this Season: {player: {day: base}}."""
        result: dict[int, dict[int, int | None]] = {
            player_id: {} for player_id in self.player_ids.values()
        }
        for player_id, start, base_id in self.connection.execute(
            """
            SELECT player_id, ranked_day_start, base_id FROM notebook_day_picks
            WHERE account_id = %s AND player_id = ANY(%s)
              AND ranked_day_start >= %s AND ranked_day_start < %s
            """,
            (self.account_id, list(self.player_ids.values()),
             self.season_start, self.season_end),
        ).fetchall():
            result[int(player_id)][self.day_number(start)] = (
                None if base_id is None else int(base_id)
            )
        return result

    def counted(self) -> tuple[set[int], dict[tuple[int, int], list[int]]]:
        """This Season's Legend days whose defenses the worker has counted,
        and each player's counts on them: {(player, day): counts}."""
        days = {
            self.day_number(start)
            for (start,) in self.connection.execute(
                """
                SELECT ranked_day_start FROM army_type_defense_counted_days
                WHERE ranked_day_start >= %s AND ranked_day_start < %s
                """,
                (self.season_start, self.season_end),
            ).fetchall()
        }
        counts = {
            (int(player_id), self.day_number(start)): [int(value) for value in values]
            for player_id, start, *values in self.connection.execute(
                """
                SELECT population_player_id, ranked_day_start, sum(defenses),
                       sum(one_star), sum(two_star), sum(three_star), sum(trophies_lost)
                FROM army_package_defense_day_counts
                WHERE population_player_id = ANY(%s)
                  AND ranked_day_start >= %s AND ranked_day_start < %s
                GROUP BY 1, 2
                """,
                (list(self.player_ids.values()), self.season_start, self.season_end),
            ).fetchall()
        }
        return days, counts

    def them(self) -> dict[int, dict[str, Any]]:
        """Each base saved from an attack: its day, whether the worker has
        counted that day, and the attacked player's record that day, None
        until it is counted or when their defenses are not tracked: every
        tracked player's count includes the Clasher's attack."""
        result: dict[int, dict[str, Any]] = {}
        for base_id, start, counted, *counts in self.connection.execute(
            """
            SELECT base.id, base.from_day_start, counted.ranked_day_start IS NOT NULL,
                   sum(day.defenses), sum(day.one_star), sum(day.two_star),
                   sum(day.three_star), sum(day.trophies_lost)
            FROM notebook_bases AS base
            LEFT JOIN army_type_defense_counted_days AS counted
                ON counted.ranked_day_start = base.from_day_start
            LEFT JOIN players AS player ON player.normalized_tag = base.from_tag
            LEFT JOIN army_package_defense_day_counts AS day
                ON day.population_player_id = player.id
               AND day.ranked_day_start = base.from_day_start
            WHERE base.account_id = %s AND base.season_id = %s
              AND base.from_tag IS NOT NULL
            GROUP BY base.id, base.from_day_start, counted.ranked_day_start
            """,
            (self.account_id, self.season_id),
        ).fetchall():
            record = None
            if counted and counts[0] is not None:
                record = _record()
                _add(record, [int(value) for value in counts])
            result[int(base_id)] = {
                "day": self.day_number(start), "counted": counted, "record": record,
            }
        return result

    def attacked(self, player_id: int, opponent: str, day: int) -> bool:
        """Whether the player's battle log for ``day`` shows an attack on
        ``opponent``: the latest one published, as Bases you attacked reads."""
        row = self.connection.execute(
            """
            SELECT battles FROM api_player_daily_logs
            WHERE player_id = %s AND ranked_day_start = %s
            ORDER BY version DESC LIMIT 1
            """,
            (player_id, self.day_start(day)),
        ).fetchone()
        attacks, _ = _screen_events(None if row is None else row[0])
        return any(event["opponent"]["tag"] == opponent for event in attacks)

    def legend_days(self) -> dict[int, set[int]]:
        """Each player's Legend days this Season up to tomorrow: days in a
        week the weekly league record says was Legend I. This week or
        tomorrow's, while it has no record, follows the week before."""
        weeks: dict[tuple[int, datetime], bool] = {
            (int(player_id), start): league == "legend_i"
            for player_id, start, league in self.connection.execute(
                """
                SELECT player_id, week_start, league FROM player_league_weeks
                WHERE player_id = ANY(%s) AND week_start >= %s AND week_start < %s
                """,
                (list(self.player_ids.values()), week_start(self.season_start) - WEEK,
                 self.season_end),
            ).fetchall()
        }
        this_week = week_start(self.day_start(self.today))
        result: dict[int, set[int]] = {}
        for player_id in self.player_ids.values():
            result[player_id] = set()
            for day in range(1, self.tomorrow + 1):
                week = week_start(self.day_start(day))
                while week >= this_week and (player_id, week) not in weeks:
                    week -= WEEK
                if weeks.get((player_id, week), False):
                    result[player_id].add(day)
        return result

    def require_legend(self, player_id: int, day: int) -> None:
        if day not in self.legend_days()[player_id]:
            raise ApiError(422, "not_legend_day")

    def statuses(self) -> dict[int, tuple[str, datetime]]:
        return {
            int(base_id): (_text(status), status_at)
            for base_id, status, status_at in self.connection.execute(
                "SELECT id, status, status_at FROM notebook_bases"
                " WHERE account_id = %s AND season_id = %s",
                (self.account_id, self.season_id),
            ).fetchall()
        }

    def uses(self) -> tuple[set[int], set[int], set[int]]:
        """The bases some player runs today, the ones some player runs from
        tomorrow, and the ones that ran up to today, on Legend days only."""
        legend = self.legend_days()
        picks = self.picks()

        def up(day: int) -> set[int]:
            return {
                base for player_id, mine in picks.items()
                if (base := base_on(mine, day, legend[player_id])) is not None
            }

        ran = {
            base for player_id, mine in picks.items()
            for *_, base in runs(mine, self.today, legend[player_id])
        }
        return up(self.today), up(self.tomorrow), ran

    def shown(self) -> dict[int, str]:
        """Each base's status as the Clasher sees it: Active while some
        player runs it today, otherwise as saved, except that one saved
        Active that no player runs from tomorrow shows as settle() would
        save it."""
        today, running, ran = self.uses()
        result = {}
        for base_id, (status, _) in self.statuses().items():
            if base_id in today:
                status = "active"
            elif status == "active" and base_id not in running:
                status = "retired" if base_id in ran else "to_try"
            result[base_id] = status
        return result

    def settle(self, put_on: list[int], taken_off: list[int]) -> None:
        """Active for every base some player runs from tomorrow; one no
        longer run then turns Retired if it ran, or back to To try if it
        never did. Of the rest, a To try base ``put_on`` a day that ran
        turns Retired, and a Retired one ``taken_off`` a day that no longer
        ran goes back to To try."""
        _, running, ran = self.uses()
        self.connection.execute(
            """
            UPDATE notebook_bases
            SET status = CASE WHEN id = ANY(%(running)s) THEN 'active'
                              WHEN id = ANY(%(ran)s) THEN 'retired' ELSE 'to_try' END,
                status_at = clock_timestamp()
            WHERE account_id = %(account)s AND season_id = %(season)s
              AND ((status = 'active') <> (id = ANY(%(running)s))
                   OR (status = 'to_try' AND id = ANY(%(put_on)s) AND id = ANY(%(ran)s))
                   OR (status = 'retired' AND id = ANY(%(taken_off)s)
                       AND NOT id = ANY(%(ran)s)))
            """,
            {"running": list(running), "ran": list(ran), "account": self.account_id,
             "season": self.season_id, "put_on": put_on, "taken_off": taken_off},
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
    if code is None:
        raise ApiError(422, "base_required")
    row = notebook.connection.execute(
        "SELECT id FROM notebook_bases WHERE code = %s AND account_id = %s"
        " AND season_id = %s",
        (parse_code(code), notebook.account_id, notebook.season_id),
    ).fetchone()
    if row is None:
        raise ApiError(404, "base_not_found")
    return int(row[0])


def _attack(notebook: _Notebook, attack: Attacked) -> tuple[str, datetime]:
    """The attacked player's tag and the attack's Legend day start, once the
    Clasher's player's log that day shows it, on a Legend day of theirs up
    to today, and no base of this notebook was saved from it yet."""
    player_id = _player(notebook, attack.tag)
    opponent = _safe_tag(attack.opponent)
    if attack.day > notebook.today:
        raise ApiError(422, "invalid_day")
    notebook.require_legend(player_id, attack.day)
    if not notebook.attacked(player_id, opponent, attack.day):
        raise ApiError(422, "not_attacked")
    start = notebook.day_start(attack.day)
    if notebook.connection.execute(
        "SELECT 1 FROM notebook_bases WHERE account_id = %s AND season_id = %s"
        " AND from_tag = %s AND from_day_start = %s",
        (notebook.account_id, notebook.season_id, opponent, start),
    ).fetchone() is not None:
        raise ApiError(422, "already_saved")
    return opponent, start


def _moment(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(422, "invalid_request") from None


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


def _pick_day(notebook: _Notebook, day: int | None, *, past: bool) -> int:
    """A Legend day a pick may start on: today or tomorrow, or with ``past``
    any day of the Season up to tomorrow."""
    if day is None:
        raise ApiError(422, "day_required")
    if not (1 if past else notebook.today) <= day <= notebook.tomorrow:
        raise ApiError(422, "invalid_day")
    return day


def get_notebook(
    database: ApiDatabase, account_id: int, *, now: datetime, base: str | None = None
) -> dict[str, Any]:
    """The account's notebook this Season: its players, each one's runs and
    current base, and every base with its record. With ``base``, also that
    base's note, link and record on each player."""
    with database.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        notebook = _Notebook(connection, account_id, now)
        bases = notebook.bases()
        picks = notebook.picks()
        counted, counts = notebook.counted()
        legend = notebook.legend_days()
        shown = notebook.shown()
        them = notebook.them()
        detail = _base(notebook, base) if base is not None else None
    codes = {base_id: base["code"] for base_id, base in bases.items()}
    records = {base_id: _record() for base_id in bases}
    by_player: dict[int, dict[int, dict[str, Any]]] = {}
    last_ran: dict[int, int] = {}
    players = []
    for player_id, tag, name, _clan, league in notebook.players:
        mine = picks[player_id]
        player_runs = []
        for first, last, base_id in runs(mine, notebook.today, legend[player_id]):
            record = _record()
            for day in range(first, last + 1):
                if day in counted:
                    _add(record, counts.get((player_id, day)))
                    _add(records[base_id], counts.get((player_id, day)))
                    if base_id == detail:
                        _add(by_player.setdefault(base_id, {}).setdefault(
                            player_id, _record()), counts.get((player_id, day)))
            last_ran[base_id] = max(last_ran.get(base_id, 0), last)
            player_runs.append(
                {"from": first, "to": last, "base": codes[base_id], "record": record}
            )
        current = base_on(mine, notebook.today, legend[player_id])
        players.append({
            "tag": tag,
            "name": name,
            "league": league,
            "runs": player_runs,
            "current": None if current is None else codes[current],
            # The days up to tomorrow a base can be set on.
            "legend_days": sorted(legend[player_id]),
            # A pick from tomorrow, shown beside today's base.
            "next": (
                {"day": notebook.tomorrow, "base": codes.get(mine[notebook.tomorrow])}
                if notebook.tomorrow > notebook.today and notebook.tomorrow in mine
                and notebook.tomorrow in legend[player_id]
                else None
            ),
            "picks": [
                {"day": day, "base": None if base_id is None else codes[base_id]}
                for day, base_id in sorted(mine.items())
            ],
        })
    result: dict[str, Any] = {
        "kind": "notebook",
        "season": {
            "id": notebook.season_id,
            "start": notebook.season_start.isoformat(),
            "end": notebook.season_end.isoformat(),
            "today": notebook.today,
            "tomorrow": notebook.tomorrow,
        },
        # The newest Legend day whose defenses are counted: records are as of it.
        "as_of_day": max(counted) if counted else None,
        "players": players,
        "bases": [
            {
                "id": base["code"],
                "name": base["name"],
                "status": shown[base_id],
                "added_day": notebook.day_number(base["created_at"]),
                "last_ran_day": last_ran.get(base_id),
                # The Library's Recent order: the later of when it last ran and
                # when it was added or its status set.
                "recent": max([
                    base["status_at"],
                    *([notebook.day_start(last_ran[base_id]) + RANKED_DAY_DURATION
                       - timedelta(seconds=1)] if base_id in last_ran else []),
                ]).isoformat(),
                "record": records[base_id],
                # Saved from an attack: the day and how it held for them.
                "them": them.get(base_id),
            }
            for base_id, base in bases.items()
        ],
    }
    if detail is not None:
        base = bases[detail]
        result["base"] = {
            "id": base["code"],
            "note": base["note"],
            "link": base["link"],
            "players": [
                {"tag": tag, "record": by_player.get(detail, {}).get(player_id, _record())}
                for player_id, tag, *_ in notebook.players
                if player_id in by_player.get(detail, {})
            ],
        }
    return result


class UndoPick(StrictBody):
    tag: str = Field(min_length=2, max_length=16)
    day: int = Field(ge=1, le=28)
    # None: no base from that day; absent: no pick on that day.
    base: str | None = Field(default=None, max_length=12)
    absent: bool = False


class UndoStatus(StrictBody):
    base: str = Field(max_length=12)
    status: Status
    status_at: str = Field(max_length=40)


class UndoEdit(StrictBody):
    base: str = Field(max_length=12)
    name: str = Field(max_length=200)
    # "": no base link.
    link: str = Field(max_length=400)


class UndoBase(UndoEdit):
    season: str = Field(max_length=12)
    # Saved from an attack on this player on this day.
    from_tag: str | None = Field(default=None, max_length=16)
    from_day: int | None = Field(default=None, ge=1, le=28)
    note: str = Field(max_length=2 * MAX_NOTE)
    status: Status
    created_at: str = Field(max_length=40)
    status_at: str = Field(max_length=40)
    updated_at: str = Field(max_length=40)


class Undo(StrictBody):
    picks: list[UndoPick] = Field(default_factory=list, max_length=200)
    statuses: list[UndoStatus] = Field(default_factory=list, max_length=2000)
    # A base the change made, deleted on undo.
    delete: str | None = Field(default=None, max_length=12)
    # A base's name and link before an edit.
    edit: UndoEdit | None = None
    # A base the change deleted, put back before its picks.
    restore: UndoBase | None = None


class Attacked(StrictBody):
    """The attack a base is saved from: the Clasher's player, whose base it
    was, and the Legend day."""

    tag: str = Field(max_length=16)
    opponent: str = Field(max_length=16)
    day: int = Field(ge=1, le=28)


class NotebookWrite(StrictBody):
    action: Literal["add", "pick", "status", "edit", "delete", "undo"]
    base: str | None = Field(default=None, max_length=12)
    name: str | None = Field(default=None, max_length=200)
    link: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=2 * MAX_NOTE)
    status: Status | None = None
    tag: str | None = Field(default=None, max_length=16)
    day: int | None = Field(default=None, ge=1, le=28)
    attacked: Attacked | None = None
    undo: Undo | None = None


def save(
    database: ApiDatabase, account_id: int, body: NotebookWrite, *, now: datetime
) -> dict[str, Any]:
    """Apply one change to the account's notebook and say what undoes it."""
    with database.pool.connection() as connection, connection.transaction():
        # One change to an account's notebook at a time, so statuses follow
        # the picks that set them.
        connection.execute(
            "SELECT 1 FROM clash_lens_accounts WHERE id = %s FOR NO KEY UPDATE",
            (account_id,),
        )
        notebook = _Notebook(connection, account_id, now)
        before = notebook.statuses()
        seen_before = notebook.shown()
        touched: dict[tuple[int, int], tuple[bool, int | None]] = {}

        def pick(player_id: int, day: int, base_id: int | None, *, drop: bool = False) -> None:
            """Set (or with ``drop`` remove) the player's pick on ``day``,
            remembering what it was the first time it changes."""
            start = notebook.day_start(day)
            row = connection.execute(
                "SELECT base_id FROM notebook_day_picks WHERE account_id = %s"
                " AND player_id = %s AND ranked_day_start = %s",
                (account_id, player_id, start),
            ).fetchone()
            touched.setdefault((player_id, day), (
                row is not None, None if row is None or row[0] is None else int(row[0])
            ))
            if drop:
                connection.execute(
                    "DELETE FROM notebook_day_picks WHERE account_id = %s"
                    " AND player_id = %s AND ranked_day_start = %s",
                    (account_id, player_id, start),
                )
                return
            connection.execute(
                """
                INSERT INTO notebook_day_picks (account_id, player_id, ranked_day_start, base_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (account_id, player_id, ranked_day_start)
                DO UPDATE SET base_id = EXCLUDED.base_id, picked_at = clock_timestamp()
                """,
                (account_id, player_id, start, base_id),
            )

        made: str | None = None
        edited: dict[str, str] | None = None
        gone: dict[str, str] | None = None
        base_id: int | None = None
        status: str | None = None
        if body.action == "add":
            name = clean_name(body.name or "")
            link = clean_link(body.link or "")
            active = body.status == "active"
            if active:
                player_id = _player(notebook, body.tag)
                day = _pick_day(notebook, body.day, past=False)
                notebook.require_legend(player_id, day)
            source = None if body.attacked is None else _attack(notebook, body.attacked)
            for _attempt in range(5):
                row = connection.execute(
                    """
                    INSERT INTO notebook_bases (code, account_id, season_id, name,
                                                base_link, status, from_tag,
                                                from_day_start)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO NOTHING RETURNING id, code
                    """,
                    (new_code(), account_id, notebook.season_id, name, link,
                     "active" if active else body.status or "to_try",
                     *(source or (None, None))),
                ).fetchone()
                if row is not None:
                    break
            else:  # pragma: no cover - five draws all taken: about 1 in 10^15.
                raise ApiError(503, "busy")
            base_id, made = int(row[0]), _text(row[1])
            if active:
                pick(player_id, day, base_id)
        elif body.action == "pick":
            player_id = _player(notebook, body.tag)
            base_id = None if body.base is None else _base(notebook, body.base)
            day = _pick_day(notebook, body.day, past=True)
            # Clearing a day is always allowed; setting one needs a Legend day.
            if base_id is not None:
                notebook.require_legend(player_id, day)
            pick(player_id, day, base_id)
        elif body.action == "status":
            base_id = _base(notebook, body.base)
            if body.status is None:
                raise ApiError(422, "status_required")
            status = body.status
            if body.status == "active":
                player_id = _player(notebook, body.tag)
                day = _pick_day(notebook, body.day, past=False)
                notebook.require_legend(player_id, day)
                pick(player_id, day, base_id)
            else:
                # Stop it on every player running it, from tomorrow: a pick
                # of it for tomorrow goes when another base is up today, so
                # that one carries on; otherwise tomorrow has no base. Day 28
                # has no tomorrow this Season, so a base up then runs on.
                legend = notebook.legend_days()
                for player_id, mine in notebook.picks().items():
                    if base_on(mine, notebook.tomorrow, legend[player_id]) != base_id:
                        continue
                    if notebook.tomorrow == notebook.today:
                        raise ApiError(422, "runs_until_season_end")
                    pick(player_id, notebook.tomorrow, None,
                         drop=base_on(mine, notebook.today, legend[player_id]) != base_id)
        elif body.action in {"edit", "delete"}:
            base_id = _base(notebook, body.base)
            saved = connection.execute(
                "SELECT code, name, note, base_link, status, created_at, status_at,"
                " updated_at, from_tag, from_day_start FROM notebook_bases WHERE id = %s",
                (base_id,),
            ).fetchone()
            # The base as Undo puts it back.
            kept = {
                "base": _text(saved[0]), "season": notebook.season_id,
                "name": _text(saved[1]), "note": _text(saved[2]),
                "link": "" if saved[3] is None else _text(saved[3]),
                "status": _text(saved[4]), "created_at": saved[5].isoformat(),
                "status_at": saved[6].isoformat(), "updated_at": saved[7].isoformat(),
                "from_tag": None if saved[8] is None else _text(saved[8]),
                "from_day": None if saved[9] is None else notebook.day_number(saved[9]),
            }
            if body.action == "edit":
                edited = {key: kept[key] for key in ("base", "name", "link")}
                _edit(connection, base_id, body.name, body.link, body.note)
            else:
                gone = kept
                # Its days keep their picks, with no base, until Undo.
                for player_id, mine in notebook.picks().items():
                    for day, picked in mine.items():
                        if picked == base_id:
                            touched[(player_id, day)] = (True, base_id)
                connection.execute("DELETE FROM notebook_bases WHERE id = %s", (base_id,))
        else:
            if body.undo is None:
                raise ApiError(422, "undo_required")
            _undo(notebook, body.undo, pick)
        put_on = (
            [base_id] if base_id is not None and (body.action == "pick" or status == "active")
            else []
        )
        notebook.settle(put_on, [] if body.action == "undo" else [
            old for _, old in touched.values() if old is not None and old not in put_on
        ])
        if status is not None and status != "active":
            connection.execute(
                "UPDATE notebook_bases SET status = %s, status_at = clock_timestamp()"
                " WHERE id = %s AND status <> %s",
                (status, base_id, status),
            )
        after = notebook.statuses()
        seen_after = notebook.shown()
        codes = {
            int(row[0]): _text(row[1])
            for row in connection.execute(
                "SELECT id, code FROM notebook_bases WHERE account_id = %s"
                " AND season_id = %s", (account_id, notebook.season_id),
            ).fetchall()
        }
    if gone is not None and base_id is not None:
        codes[base_id] = gone["base"]
    tags = {player_id: tag for tag, player_id in notebook.player_ids.items()}
    changed = [
        {"base": codes[key], "status": seen_after[key]}
        for key in sorted(seen_after)
        if key in seen_before and seen_before[key] != seen_after[key]
    ]
    undo = None if body.action == "undo" else {
        "picks": [
            {"tag": tags[player_id], "day": day,
             "base": None if old is None else codes[old], "absent": not existed}
            for (player_id, day), (existed, old) in sorted(touched.items())
        ],
        "statuses": [
            {"base": codes[key], "status": before[key][0],
             "status_at": before[key][1].isoformat()}
            for key in sorted(before) if key in after and before[key] != after[key]
        ],
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


def _restored_from(notebook: _Notebook, base: UndoBase) -> tuple[Any, Any]:
    """A deleted base's attack, put back only if one of the account's
    players' logs that day still shows it."""
    if base.from_tag is None or base.from_day is None:
        return None, None
    opponent = _safe_tag(base.from_tag)
    if not any(notebook.attacked(player_id, opponent, base.from_day)
               for player_id in notebook.player_ids.values()):
        raise ApiError(422, "invalid_request")
    return opponent, notebook.day_start(base.from_day)


def _undo(notebook: _Notebook, undo: Undo, pick: Callable[..., None]) -> None:
    """Put back the base a change deleted, then the picks, statuses, name and
    link it replaced, and delete the base it made. Every base and player
    named must still be this account's."""
    if undo.restore is not None:
        base = undo.restore
        # Its days are this Season's, so it only comes back in this notebook.
        if base.season != notebook.season_id:
            raise ApiError(422, "invalid_request")
        row = notebook.connection.execute(
            """
            INSERT INTO notebook_bases (code, account_id, season_id, name, note,
                                        base_link, status, created_at, status_at,
                                        updated_at, from_tag, from_day_start)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO NOTHING RETURNING id
            """,
            (parse_code(base.base), notebook.account_id, notebook.season_id,
             clean_name(base.name), clean_note(base.note), clean_link(base.link),
             base.status, _moment(base.created_at), _moment(base.status_at),
             _moment(base.updated_at), *_restored_from(notebook, base)),
        ).fetchone()
        if row is None:
            raise ApiError(422, "invalid_request")
    for item in undo.picks:
        player_id = _player(notebook, item.tag)
        day = _pick_day(notebook, item.day, past=True)
        if item.absent:
            pick(player_id, day, None, drop=True)
        else:
            pick(player_id, day, None if item.base is None else _base(notebook, item.base))
    if undo.delete is not None:
        notebook.connection.execute(
            "DELETE FROM notebook_bases WHERE id = %s", (_base(notebook, undo.delete),)
        )
    for item in undo.statuses:
        notebook.connection.execute(
            "UPDATE notebook_bases SET status = %s, status_at = %s WHERE id = %s",
            (item.status, _moment(item.status_at), _base(notebook, item.base)),
        )
    if undo.edit is not None:
        _edit(notebook.connection, _base(notebook, undo.edit.base),
              undo.edit.name, undo.edit.link, None)


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
    def notebook(request: Request, base: str | None = None) -> JSONResponse:
        account_id = account(request, "account.read")
        if base is not None and len(base) > 12:
            raise ApiError(404, "base_not_found")
        return JSONResponse(content=get_notebook(
            database, account_id, now=current_time(), base=base
        ))

    @router.post("/v1/account/notebook")
    def write(body: NotebookWrite, request: Request) -> JSONResponse:
        account_id = account(request, "account.update")
        return JSONResponse(content=save(database, account_id, body, now=current_time()))

    app.include_router(router)
