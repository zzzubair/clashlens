"""Season notebook: one per Clash Lens account per Season (migration 0124).

A Clasher keeps bases in the notebook's Library, each with an ID like
B-7K3QX9, and says which base each of their verified players ran from which
Legend day: a day pick. A player's base on any day is their latest pick on
or before it, so a base carries forward until the next pick, and one player
can never run two bases on one Legend day. A run is a stretch of days one
player ran one base.

A base is Active while it is some player's base from that player's latest
pick on; when a change stops it everywhere it turns Retired. To try and
Retired are otherwise the Clasher's own choice.

A base's record adds up the worker's per-day defense counts
(army_package_defense_day_counts, migration 0106) for the days and players
it ran on, read live, so it grows as each finished Legend day is counted,
about 30 minutes after the Reset. Held means the attacker did not get 3
stars. Nothing here runs at the Reset or in the worker.

Every write returns what undoes it: the picks and statuses it changed, and
for a new base the base itself.
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
from .api_db import ApiDatabase, _text
from .domain import RANKED_DAY_DURATION, ranked_day_for

# Crockford's base-32 alphabet: no I, L, O or U, so no 1/I/L or 0/O mix-ups.
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE = re.compile(r"B-[0-9A-HJKMNP-TV-Z]{6}")
# Support may type an ID in lower case, or with I, L or O for 1 and 0.
_MISREAD = str.maketrans("ILO", "110")
# The in-game link that opens a layout; nothing else may be saved as one.
_BASE_LINK = re.compile(r"https://link\.clashofclans\.com/[A-Za-z0-9/?=&%._:;,+-]{1,270}")
MAX_NAME = 60
MAX_NOTE = 1000
Status = Literal["active", "to_try", "retired"]


def new_code() -> str:
    """A random base ID: B- and 6 characters, about 1.07 billion in all."""
    return "B-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def parse_code(value: str) -> str:
    """The base ID in ``value``, as support might type it, or ApiError."""
    cleaned = value.strip().upper().translate(_MISREAD)
    if not cleaned.startswith("B-"):
        cleaned = "B-" + cleaned
    if not _CODE.fullmatch(cleaned):
        raise ApiError(404, "base_not_found")
    return cleaned


def clean_name(value: str) -> str:
    name = " ".join(unicodedata.normalize("NFC", value).split())
    if not 1 <= len(name) <= MAX_NAME or any(
        unicodedata.category(character).startswith("C") for character in name
    ):
        raise ApiError(422, "invalid_name")
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
    if len(note) > MAX_NOTE or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        and character not in "\n\t"
        for character in note
    ):
        raise ApiError(422, "invalid_note")
    return note


def runs(picks: dict[int, int | None], last_day: int) -> list[tuple[int, int, int]]:
    """Each stretch of days 1 to ``last_day`` on one base, as (first day,
    last day, base), from the day picks {day: base or None for no base}.
    Days before the first pick and days with no base are in no run."""
    result: list[tuple[int, int, int]] = []
    base: int | None = None
    for day in range(1, last_day + 1):
        if day in picks:
            base = picks[day]
        if base is None:
            continue
        if result and result[-1][2] == base and result[-1][1] == day - 1:
            result[-1] = (result[-1][0], day, base)
        else:
            result.append((day, day, base))
    return result


def latest_base(picks: dict[int, int | None]) -> int | None:
    """The base a player runs from their latest pick on, or None."""
    return picks[max(picks)] if picks else None


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
                "code": _text(row[1]), "name": _text(row[2]), "status": _text(row[3]),
                "link": None if row[4] is None else _text(row[4]),
                "note": _text(row[5]), "created_at": row[6], "status_at": row[7],
            }
            for row in self.connection.execute(
                """
                SELECT id, code, name, status, base_link, note, created_at, status_at
                FROM notebook_bases
                WHERE account_id = %s AND season_id = %s
                ORDER BY id
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

    def statuses(self) -> dict[int, tuple[str, datetime]]:
        return {
            int(base_id): (_text(status), status_at)
            for base_id, status, status_at in self.connection.execute(
                "SELECT id, status, status_at FROM notebook_bases"
                " WHERE account_id = %s AND season_id = %s",
                (self.account_id, self.season_id),
            ).fetchall()
        }

    def settle(self) -> None:
        """Active for every base some player runs from their latest pick on;
        a base no longer run anywhere turns Retired."""
        running = {
            base for base in map(latest_base, self.picks().values()) if base is not None
        }
        self.connection.execute(
            """
            UPDATE notebook_bases
            SET status = CASE WHEN id = ANY(%s) THEN 'active' ELSE 'retired' END,
                status_at = clock_timestamp()
            WHERE account_id = %s AND season_id = %s
              AND (status = 'active') <> (id = ANY(%s))
            """,
            (list(running), self.account_id, self.season_id, list(running)),
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
        detail = _base(notebook, base) if base is not None else None
    codes = {base_id: base["code"] for base_id, base in bases.items()}
    records = {base_id: _record() for base_id in bases}
    by_player: dict[int, dict[int, dict[str, Any]]] = {}
    last_ran: dict[int, int] = {}
    players = []
    for player_id, tag, name, _clan, league in notebook.players:
        mine = picks[player_id]
        player_runs = []
        for first, last, base_id in runs(mine, notebook.today):
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
        current = latest_base(mine)
        players.append({
            "tag": tag,
            "name": name,
            "league": league,
            "runs": player_runs,
            "current": None if current is None else codes[current],
            # A pick from tomorrow, shown beside today's base.
            "next": (
                {"day": notebook.tomorrow, "base": codes.get(mine[notebook.tomorrow])}
                if notebook.tomorrow > notebook.today and notebook.tomorrow in mine
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
                "status": base["status"],
                "added_day": notebook.day_number(base["created_at"]),
                "last_ran_day": last_ran.get(base_id),
                # The Library's Recent order: the later of when it last ran and
                # when it was added or its status set.
                "recent": max([
                    base["status_at"],
                    *([notebook.day_start(last_ran[base_id]) + RANKED_DAY_DURATION
                       - timedelta(seconds=1)] if base_id in last_ran else []),
                ]).isoformat(),
                "has_link": base["link"] is not None,
                "record": records[base_id],
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


class Undo(StrictBody):
    picks: list[UndoPick] = Field(default_factory=list, max_length=200)
    statuses: list[UndoStatus] = Field(default_factory=list, max_length=2000)
    # A base the change made, deleted on undo.
    delete: str | None = Field(default=None, max_length=12)


class NotebookWrite(StrictBody):
    action: Literal["add", "pick", "status", "edit", "delete", "undo"]
    base: str | None = Field(default=None, max_length=12)
    name: str | None = Field(default=None, max_length=200)
    link: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=2 * MAX_NOTE)
    status: Status | None = None
    tag: str | None = Field(default=None, max_length=16)
    day: int | None = Field(default=None, ge=1, le=28)
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
        base_id: int | None = None
        status: str | None = None
        if body.action == "add":
            name = clean_name(body.name or "")
            link = clean_link(body.link or "")
            active = body.status == "active"
            if active:
                player_id = _player(notebook, body.tag)
                day = _pick_day(notebook, body.day, past=False)
            for _attempt in range(5):
                row = connection.execute(
                    """
                    INSERT INTO notebook_bases (code, account_id, season_id, name,
                                                base_link, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (code) DO NOTHING RETURNING id, code
                    """,
                    (new_code(), account_id, notebook.season_id, name, link,
                     "active" if active else body.status or "to_try"),
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
            pick(player_id, _pick_day(notebook, body.day, past=True), base_id)
        elif body.action == "status":
            base_id = _base(notebook, body.base)
            if body.status is None:
                raise ApiError(422, "status_required")
            status = body.status
            if body.status == "active":
                pick(_player(notebook, body.tag),
                     _pick_day(notebook, body.day, past=False), base_id)
            elif notebook.tomorrow > notebook.today:
                # Stop it on every player running it, from tomorrow: a pick
                # of it made for tomorrow goes, so the base before carries on.
                for player_id, mine in notebook.picks().items():
                    if latest_base(mine) == base_id:
                        pick(player_id, notebook.tomorrow, None,
                             drop=max(mine) == notebook.tomorrow)
        elif body.action == "edit":
            base_id = _base(notebook, body.base)
            changes = {
                "name": None if body.name is None else clean_name(body.name),
                "base_link": None if body.link is None else clean_link(body.link),
                "note": None if body.note is None else clean_note(body.note),
            }
            connection.execute(
                """
                UPDATE notebook_bases SET
                    name = COALESCE(%s, name),
                    base_link = CASE WHEN %s THEN %s ELSE base_link END,
                    note = COALESCE(%s, note),
                    updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (changes["name"], body.link is not None, changes["base_link"],
                 changes["note"], base_id),
            )
        elif body.action == "delete":
            base_id = _base(notebook, body.base)
            # Its days keep their picks, with no base.
            connection.execute("DELETE FROM notebook_bases WHERE id = %s", (base_id,))
        else:
            if body.undo is None:
                raise ApiError(422, "undo_required")
            _undo(notebook, body.undo, pick)
        notebook.settle()
        if body.action == "pick" and base_id is not None:
            # A To try base put on a day it no longer runs from has been tried.
            connection.execute(
                "UPDATE notebook_bases SET status = 'retired', status_at = clock_timestamp()"
                " WHERE id = %s AND status = 'to_try'",
                (base_id,),
            )
        if status is not None and status != "active":
            connection.execute(
                "UPDATE notebook_bases SET status = %s, status_at = clock_timestamp()"
                " WHERE id = %s AND status <> %s",
                (status, base_id, status),
            )
        after = notebook.statuses()
        codes = {
            int(row[0]): _text(row[1])
            for row in connection.execute(
                "SELECT id, code FROM notebook_bases WHERE account_id = %s"
                " AND season_id = %s", (account_id, notebook.season_id),
            ).fetchall()
        }
    tags = {player_id: tag for tag, player_id in notebook.player_ids.items()}
    changed = [
        {"base": codes[key], "status": after[key][0]}
        for key in sorted(after) if key in before and before[key][0] != after[key][0]
    ]
    undo = {
        "picks": [
            {"tag": tags[player_id], "day": day,
             "base": None if old is None else codes.get(old), "absent": not existed}
            for (player_id, day), (existed, old) in sorted(touched.items())
            # A pick of a base deleted since cannot come back.
            if old is None or old in codes
        ],
        "statuses": [
            {"base": codes[key], "status": before[key][0],
             "status_at": before[key][1].isoformat()}
            for key in sorted(before) if key in after and before[key] != after[key]
        ],
        "delete": made,
    }
    return {
        "kind": "notebook-saved",
        "base": made or (codes.get(base_id) if base_id is not None else None),
        "changed": changed,
        "undo": None if body.action in {"delete", "undo", "edit"} else undo,
    }


def _undo(notebook: _Notebook, undo: Undo, pick: Callable[..., None]) -> None:
    """Put back the picks and statuses a change replaced, and delete the base
    it made. Every base and player named must still be this account's."""
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
        try:
            status_at = datetime.fromisoformat(item.status_at)
        except ValueError:
            raise ApiError(422, "invalid_request") from None
        notebook.connection.execute(
            "UPDATE notebook_bases SET status = %s, status_at = %s WHERE id = %s",
            (item.status, status_at, _base(notebook, item.base)),
        )


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
