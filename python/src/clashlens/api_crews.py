"""Crews: invite-only groups of Legend League accounts with shared boards.

A crew's members are Clash Lens accounts (``crew_accounts``, which holds the
role); its places are Clash of Clans accounts (``crew_players``), each one
linked to the member holding it. Anyone can view a crew and its boards
(``view_crew``), which name its places but never its member accounts;
everything else about a crew another account is not in reads exactly like a
missing one.

Every write takes its locks in one order so two writes never wait on each
other: the crew row, then the caller's account row (only create and accept,
which count the caller's crews), then player tags in sorted order, the same
"group row, then the tag" order Group saves use.

Roles and invite links follow the state machines in crew_states.py.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from . import api_db, api_player_lookup
from .api_accounts import _MEMBER_JOINS, _group_player
from .api_db import (
    ApiDatabase,
    OperationResult,
    RequestBinding,
    _text,
)
from .api_players import left_legend_i
from .crew_states import (
    INVITE_LIFETIME,
    LIVE_INVITE_SQL,
    MAX_CREWS_PER_ACCOUNT,
    PERMITTED,
    CrewAction,
    CrewEvent,
    CrewRole,
    Invite,
    InviteState,
    invite_state,
    move,
    require,
    reuse_invite,
)
from .crew_states import (
    Refused as _Refused,
)
from .domain import ranked_day_for, season_opening_reset
from .season_reset_sql import frozen_trophies_sql, opening_day_battles_sql

MAX_CREW_SIZE = 100
MIN_CREW_SIZE = 2
DEFAULT_CREW_SIZE = 50


def _write(
    database: ApiDatabase,
    binding: RequestBinding,
    operation: Callable[[Any], OperationResult],
) -> OperationResult:
    """Run one repeat-safe write. A refusal undoes everything the write did
    so far and is stored as the answer, as every Group write's is."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            try:
                with connection.transaction():
                    result = operation(connection)
            except _Refused as refusal:
                result = OperationResult(refusal.status_code, refusal.payload)
            api_db._complete_request(connection, binding.request_id, result)
            return result


def _membership(
    connection: Any, crew_id: str, account_id: int, *, lock: bool
) -> tuple[int, int, CrewRole]:
    """The crew's row id, size and the caller's role. A write locks the crew
    row first and reads the role afterwards, so a role changed while it
    waited is the one it sees."""
    crew = connection.execute(
        f"SELECT id, size FROM crews WHERE public_id = %s {'FOR UPDATE' if lock else ''}",
        (crew_id,),
    ).fetchone()
    member = None
    if crew is not None:
        member = connection.execute(
            "SELECT role FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
            (crew[0], account_id),
        ).fetchone()
    if member is None:
        raise _Refused(404, {"error": "crew_not_found"})
    return int(crew[0]), int(crew[1]), CrewRole(_text(member[0]))


def _used(connection: Any, crew_row: int) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM crew_players WHERE crew_id = %s", (crew_row,)
        ).fetchone()[0]
    )


def _header(connection: Any, crew_row: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT public_id, name, size FROM crews WHERE id = %s", (crew_row,)
    ).fetchone()
    return {
        "crew_id": str(row[0]),
        "name": _text(row[1]),
        "size": int(row[2]),
        "used": _used(connection, crew_row),
    }


def _add_places(
    connection: Any,
    *,
    crew_row: int,
    size: int,
    account_id: int,
    tags: list[str],
    new_role: CrewRole | None,
) -> list[str]:
    """Check every join rule under the crew lock, then give each tag a place.

    ``new_role`` is the role a caller not yet in the crew joins with, or
    None for a member adding more of their accounts.
    """
    tags = sorted(set(tags))
    if new_role is not None:
        # NO KEY UPDATE serializes two joins by one account without blocking
        # the account's other writes, which only reference the row.
        connection.execute(
            "SELECT 1 FROM clash_lens_accounts WHERE id = %s FOR NO KEY UPDATE",
            (account_id,),
        )
    for tag in tags:
        api_player_lookup.lock_tag(connection, tag)
    linked = {
        _text(row[0]): int(row[1])
        for row in connection.execute(
            """
            SELECT player.normalized_tag, player.id
            FROM verified_player_links AS link
            JOIN players AS player ON player.id = link.player_id
            WHERE link.account_id = %s AND player.normalized_tag = ANY(%s)
            """,
            (account_id, tags),
        ).fetchall()
    }
    for tag in tags:
        if tag not in linked:
            raise _Refused(422, {"error": "player_not_linked", "tag": tag})
    for tag in tags:
        state = api_player_lookup._lookup(connection, tag)["state"]
        if state in {"not_in_legend", "uncertain"}:
            raise _Refused(422, {"error": "player_not_in_legend", "tag": tag})
        if state != "tracking":
            # The website checks the account and tries again, as Group adds do.
            raise _Refused(
                409, {"error": "player_not_checked", "state": state, "tag": tag}
            )
    present = connection.execute(
        """
        SELECT player.normalized_tag
        FROM crew_players AS place
        JOIN players AS player ON player.id = place.player_id
        WHERE place.crew_id = %s AND place.player_id = ANY(%s)
        ORDER BY player.normalized_tag
        LIMIT 1
        """,
        (crew_row, list(linked.values())),
    ).fetchone()
    if present is not None:
        raise _Refused(
            409, {"error": "player_already_in_crew", "tag": _text(present[0])}
        )
    used = _used(connection, crew_row)
    if used + len(tags) > size:
        raise _Refused(409, {"error": "crew_full", "open_places": max(0, size - used)})
    if new_role is not None:
        crews = connection.execute(
            "SELECT count(*) FROM crew_accounts WHERE account_id = %s", (account_id,)
        ).fetchone()[0]
        if crews >= MAX_CREWS_PER_ACCOUNT:
            raise _Refused(409, {"error": "crew_limit_reached"})
        connection.execute(
            """
            INSERT INTO crew_accounts (crew_id, account_id, role)
            VALUES (%s, %s, %s)
            """,
            (crew_row, account_id, new_role.value),
        )
    # Only accounts already tracked can take a place, so unlike a Group add
    # there is no player check to start.
    for tag in tags:
        connection.execute(
            """
            INSERT INTO crew_players (crew_id, account_id, player_id)
            VALUES (%s, %s, %s)
            """,
            (crew_row, account_id, linked[tag]),
        )
    return _own_tags(connection, crew_row, account_id)


def _own_tags(connection: Any, crew_row: int, account_id: int) -> list[str]:
    return [
        _text(row[0])
        for row in connection.execute(
            """
            SELECT player.normalized_tag
            FROM crew_players AS place
            JOIN players AS player ON player.id = place.player_id
            WHERE place.crew_id = %s AND place.account_id = %s
            ORDER BY player.normalized_tag
            """,
            (crew_row, account_id),
        ).fetchall()
    ]


def list_crews(database: ApiDatabase, account_id: int) -> dict[str, Any]:
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT crew.public_id, crew.name, crew.size, member.role,
                   (SELECT count(*) FROM crew_players AS place
                    WHERE place.crew_id = crew.id),
                   ARRAY(SELECT player.normalized_tag
                         FROM crew_players AS place
                         JOIN players AS player ON player.id = place.player_id
                         WHERE place.crew_id = crew.id
                           AND place.account_id = member.account_id
                         ORDER BY player.normalized_tag)
            FROM crew_accounts AS member
            JOIN crews AS crew ON crew.id = member.crew_id
            WHERE member.account_id = %s
            ORDER BY lower(crew.name), crew.created_at, crew.id
            """,
            (account_id,),
        ).fetchall()
    crews = [
        {
            "crew_id": str(row[0]),
            "name": _text(row[1]),
            "size": int(row[2]),
            "used": int(row[4]),
            "role": _text(row[3]),
            "my_tags": [_text(tag) for tag in row[5]],
        }
        for row in rows
    ]
    return {
        "kind": "crew-list",
        "crews": crews,
        "crew_count": len(crews),
        "max_crews": MAX_CREWS_PER_ACCOUNT,
    }


def _players(
    connection: Any, source: str, params: tuple[Any, ...], now: datetime
) -> list[tuple[int, dict[str, Any]]]:
    """(account id, player) for each row of ``source``, a FROM clause naming
    ``member`` with ``account_id`` and ``player_id``: the stored name and
    trophies, and whether the account is in Legend League with a Legend day
    this Season."""
    opening = season_opening_reset(now)
    rows = connection.execute(
        f"""
        SELECT member.account_id, player.normalized_tag, player.active,
               COALESCE(accepted.name, latest.name), accepted.trophies,
               accepted.current_league_season_id,
               {frozen_trophies_sql("player.id", "%s")},
               {opening_day_battles_sql("player.id", "%s")},
               EXISTS (
                   SELECT 1 FROM api_player_daily_logs AS day
                   WHERE day.player_id = player.id
                     AND day.ranked_day_start >= %s
                     AND (jsonb_array_length(day.battles) > 0
                          OR NOT day.partial_reasons ? 'not_enrolled')
                     AND NOT EXISTS (
                         SELECT 1 FROM api_player_daily_logs AS newer
                         WHERE newer.player_id = day.player_id
                           AND newer.ranked_day_start = day.ranked_day_start
                           AND newer.version > day.version)),
               player.id
        FROM {source}
        ORDER BY player.normalized_tag
        """,
        (opening, opening, ranked_day_for(now).season_start, *params),
    ).fetchall()
    left = left_legend_i(connection, [int(row[9]) for row in rows], now)
    players = []
    for row in rows:
        player = _group_player(connection, row[1:8], now)
        if player["state"] != "tracking":
            status = "not_in_legend"
        else:
            status = "tracking" if row[8] else "no_battles_this_season"
        players.append(
            (
                int(row[0]),
                {
                    "tag": player["tag"],
                    "name": player["name"],
                    "trophies": player["trophies"],
                    "lookup": player["state"],
                    "status": status,
                    # This Season's Legend I part, for an account that left it.
                    "left_legend_i": left.get(int(row[9]))
                    if status == "not_in_legend"
                    else None,
                },
            )
        )
    return players


def get_crew(
    database: ApiDatabase, account_id: int, crew_id: str, *, now: datetime
) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        try:
            crew_row, _size, my_role = _membership(
                connection, crew_id, account_id, lock=False
            )
        except _Refused:
            return None
        header = _header(connection, crew_row)
        accounts = connection.execute(
            """
            SELECT member.account_id, account.username, account.display_name,
                   member.role
            FROM crew_accounts AS member
            JOIN clash_lens_accounts AS account ON account.id = member.account_id
            WHERE member.crew_id = %s
            """,
            (crew_row,),
        ).fetchall()
        places: dict[int, list[dict[str, Any]]] = {}
        for owner, player in _players(
            connection,
            f"crew_players AS member {_MEMBER_JOINS} WHERE member.crew_id = %s",
            (crew_row,),
            now,
        ):
            del player["lookup"]
            places.setdefault(owner, []).append(player)
        members = sorted(
            (
                {
                    "username": _text(row[1]),
                    "display_name": _text(row[2]),
                    "role": _text(row[3]),
                    "you": int(row[0]) == account_id,
                    "players": places.get(int(row[0]), []),
                }
                for row in accounts
            ),
            key=lambda member: (
                CrewRole(member["role"]).rank,
                member["display_name"].casefold(),
                member["username"],
            ),
        )
        # Owners and admins can turn off any link, so they see them all; a
        # member sees only their own.
        invites = connection.execute(
            f"""
            SELECT invite.public_id, account.display_name, invite.expires_at,
                   invite.created_by_account_id = %s
            FROM crew_invites AS invite
            JOIN clash_lens_accounts AS account
              ON account.id = invite.created_by_account_id
            WHERE invite.crew_id = %s AND {LIVE_INVITE_SQL}
              AND (%s OR invite.created_by_account_id = %s)
            ORDER BY invite.created_at DESC, invite.id DESC
            """,
            (
                account_id,
                crew_row,
                now,
                my_role in PERMITTED[CrewAction.ALL_LINKS],
                account_id,
            ),
        ).fetchall()
    return {
        "kind": "crew",
        **header,
        "my_role": my_role.value,
        "members": members,
        "invites": [
            {
                "invite_id": str(row[0]),
                "made_by": _text(row[1]),
                "expires_at": row[2],
                "mine": bool(row[3]),
            }
            for row in invites
        ],
    }


def _crew_and_role(
    connection: Any, crew_id: str, account_id: int | None
) -> tuple[int, str | None] | None:
    """The crew's row id and the caller's role in it, None for a caller
    signed out or not in the crew; None alone for a missing crew."""
    crew = connection.execute(
        "SELECT id FROM crews WHERE public_id = %s", (crew_id,)
    ).fetchone()
    if crew is None:
        return None
    member = None
    if account_id is not None:
        member = connection.execute(
            "SELECT role FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
            (crew[0], account_id),
        ).fetchone()
    return int(crew[0]), None if member is None else _text(member[0])


def view_crew(
    database: ApiDatabase, account_id: int | None, crew_id: str, *, now: datetime
) -> dict[str, Any] | None:
    """What anyone, signed in or not, sees of a crew: its name, places and
    the Clash of Clans accounts in them. Never which Clash Lens accounts
    hold them, nor its invite links."""
    with database.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        found = _crew_and_role(connection, crew_id, account_id)
        if found is None:
            return None
        crew_row, my_role = found
        header = _header(connection, crew_row)
        players = []
        for _owner, player in _players(
            connection,
            f"crew_players AS member {_MEMBER_JOINS} WHERE member.crew_id = %s",
            (crew_row,),
            now,
        ):
            del player["lookup"]
            players.append(player)
        clashers = (
            None
            if my_role is None
            else int(
                connection.execute(
                    "SELECT count(*) FROM crew_accounts WHERE crew_id = %s", (crew_row,)
                ).fetchone()[0]
            )
        )
    players.sort(
        key=lambda player: (
            player["trophies"] is None,
            -(player["trophies"] or 0),
            (player["name"] or "").casefold(),
            player["tag"],
        )
    )
    return {
        "kind": "crew-view",
        **header,
        "my_role": my_role,
        "clashers": clashers,
        "players": players,
    }


def player_crews(database: ApiDatabase, tag: str) -> dict[str, Any]:
    """The crews a Clash of Clans account has a place in."""
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT crew.public_id, crew.name
            FROM crew_players AS place
            JOIN players AS player ON player.id = place.player_id
            JOIN crews AS crew ON crew.id = place.crew_id
            WHERE player.normalized_tag = %s
            ORDER BY lower(crew.name), crew.created_at, crew.id
            """,
            (tag,),
        ).fetchall()
    return {
        "kind": "player-crews",
        "tag": tag,
        "crews": [{"crew_id": str(row[0]), "name": _text(row[1])} for row in rows],
    }


def create_crew(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    name: str,
    size: int,
    tags: list[str],
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def create(connection: Any) -> OperationResult:
        public_id = uuid4()
        crew_row = connection.execute(
            "INSERT INTO crews (public_id, name, size) VALUES (%s, %s, %s) RETURNING id",
            (public_id, name, size),
        ).fetchone()[0]
        _add_places(
            connection,
            crew_row=crew_row,
            size=size,
            account_id=account_id,
            tags=tags,
            new_role=move(None, CrewEvent.CREATE),
        )
        return OperationResult(
            201, {"crew_id": str(public_id), "name": name, "size": size}
        )

    return _write(database, binding, create)


def update_crew(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    crew_id: str,
    name: str | None = None,
    size: int | None = None,
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def update(connection: Any) -> OperationResult:
        crew_row, _size, role = _membership(connection, crew_id, account_id, lock=True)
        require(role, CrewAction.EDIT)
        if size is not None:
            used = _used(connection, crew_row)
            if size < used:
                raise _Refused(422, {"error": "crew_size_below_used", "used": used})
        connection.execute(
            """
            UPDATE crews
            SET name = COALESCE(%s, name), size = COALESCE(%s, size),
                updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (name, size, crew_row),
        )
        return OperationResult(200, _header(connection, crew_row))

    return _write(database, binding, update)


def delete_crew(
    database: ApiDatabase, binding: RequestBinding, *, crew_id: str
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def delete(connection: Any) -> OperationResult:
        crew_row, _size, role = _membership(connection, crew_id, account_id, lock=True)
        require(role, CrewAction.DELETE)
        connection.execute("DELETE FROM crews WHERE id = %s", (crew_row,))
        return OperationResult(200, {"deleted": True, "crew_id": crew_id})

    return _write(database, binding, delete)


def add_players(
    database: ApiDatabase, binding: RequestBinding, *, crew_id: str, tags: list[str]
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def add(connection: Any) -> OperationResult:
        crew_row, size, _role = _membership(connection, crew_id, account_id, lock=True)
        mine = _add_places(
            connection,
            crew_row=crew_row,
            size=size,
            account_id=account_id,
            tags=tags,
            new_role=None,
        )
        return OperationResult(200, {"crew_id": crew_id, "tags": mine})

    return _write(database, binding, add)


def remove_player(
    database: ApiDatabase, binding: RequestBinding, *, crew_id: str, tag: str
) -> OperationResult:
    """Remove the caller's own account, or kick someone else's."""
    assert binding.account_id is not None
    account_id = binding.account_id

    def remove(connection: Any) -> OperationResult:
        crew_row, _size, role = _membership(connection, crew_id, account_id, lock=True)
        place = connection.execute(
            """
            SELECT place.account_id, place.player_id, member.role
            FROM crew_players AS place
            JOIN players AS player ON player.id = place.player_id
            JOIN crew_accounts AS member
              ON member.crew_id = place.crew_id AND member.account_id = place.account_id
            WHERE place.crew_id = %s AND player.normalized_tag = %s
            """,
            (crew_row, tag),
        ).fetchone()
        if place is None:
            raise _Refused(404, {"error": "player_not_in_crew"})
        holder, player_id = int(place[0]), int(place[1])
        holder_role = CrewRole(_text(place[2]))
        if holder != account_id and not role.outranks(holder_role):
            raise _Refused(403, {"error": "crew_forbidden"})
        after: CrewRole | None = holder_role
        other_place = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM crew_players
                WHERE crew_id = %s AND account_id = %s AND player_id <> %s)
            """,
            (crew_row, holder, player_id),
        ).fetchone()
        if not other_place[0]:
            # The owner is refused; anyone else is out of the crew, which
            # the database's crew_players_member_without_places trigger does.
            after = move(holder_role, CrewEvent.LAST_PLACE)
        connection.execute(
            "DELETE FROM crew_players WHERE crew_id = %s AND player_id = %s",
            (crew_row, player_id),
        )
        return OperationResult(
            200, {"removed": True, "tag": tag, "left_crew": after is None}
        )

    return _write(database, binding, remove)


def _member(
    connection: Any, crew_row: int, username: str
) -> tuple[int, str, CrewRole]:
    """The member, locked so a game account moving away can't take them out
    of the crew before the write finishes."""
    row = connection.execute(
        """
        SELECT member.account_id, account.display_name, member.role
        FROM crew_accounts AS member
        JOIN clash_lens_accounts AS account ON account.id = member.account_id
        WHERE member.crew_id = %s AND account.normalized_username = %s
        FOR UPDATE OF member
        """,
        (crew_row, username),
    ).fetchone()
    if row is None:
        raise _Refused(404, {"error": "member_not_found"})
    return int(row[0]), _text(row[1]), CrewRole(_text(row[2]))


def _set_role(
    connection: Any, crew_row: int, account_id: int, role: CrewRole | None
) -> None:
    """Write a role `move` gave for someone staying in the crew."""
    assert role is not None
    changed = connection.execute(
        "UPDATE crew_accounts SET role = %s WHERE crew_id = %s AND account_id = %s",
        (role.value, crew_row, account_id),
    ).rowcount
    if changed != 1:
        raise _Refused(404, {"error": "member_not_found"})


def set_member_role(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    crew_id: str,
    username: str,
    role: str,
) -> OperationResult:
    """The owner makes a member an admin, or an admin a member again."""
    assert binding.account_id is not None and role in {"admin", "member"}
    account_id = binding.account_id

    def change(connection: Any) -> OperationResult:
        crew_row, _size, my_role = _membership(
            connection, crew_id, account_id, lock=True
        )
        require(my_role, CrewAction.SET_ROLES)
        target, display_name, target_role = _member(connection, crew_row, username)
        event = CrewEvent.MAKE_ADMIN if role == "admin" else CrewEvent.MAKE_MEMBER
        _set_role(connection, crew_row, target, move(target_role, event))
        return OperationResult(
            200, {"username": username, "display_name": display_name, "role": role}
        )

    return _write(database, binding, change)


def hand_over(
    database: ApiDatabase, binding: RequestBinding, *, crew_id: str, username: str
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def transfer(connection: Any) -> OperationResult:
        crew_row, _size, my_role = _membership(
            connection, crew_id, account_id, lock=True
        )
        require(my_role, CrewAction.HAND_OVER)
        target, _display_name, target_role = _member(connection, crew_row, username)
        new_owner = move(target_role, CrewEvent.TAKE_OVER)
        # The old owner steps down first, so the one-owner index holds
        # after every statement.
        _set_role(connection, crew_row, account_id, move(my_role, CrewEvent.HAND_OVER))
        _set_role(connection, crew_row, target, new_owner)
        # An owner whose game accounts all moved away kept the crew; as an
        # admin with no places they are out of it.
        left = connection.execute(
            """
            DELETE FROM crew_accounts AS member
            WHERE member.crew_id = %s AND member.account_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM crew_players AS place
                  WHERE place.crew_id = member.crew_id
                    AND place.account_id = member.account_id
              )
            """,
            (crew_row, account_id),
        ).rowcount
        return OperationResult(
            200, {**_header(connection, crew_row), "left_crew": left == 1}
        )

    return _write(database, binding, transfer)


def leave_crew(
    database: ApiDatabase, binding: RequestBinding, *, crew_id: str
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def leave(connection: Any) -> OperationResult:
        crew_row, _size, role = _membership(connection, crew_id, account_id, lock=True)
        move(role, CrewEvent.LEAVE)
        # The member's places go with it.
        connection.execute(
            "DELETE FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
            (crew_row, account_id),
        )
        return OperationResult(200, {"left": True, "crew_id": crew_id})

    return _write(database, binding, leave)


def make_invite(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    crew_id: str,
    new: bool,
    now: datetime,
) -> OperationResult:
    """The caller's newest live link while it has a day left, or a new one."""
    assert binding.account_id is not None
    account_id = binding.account_id

    def invite(connection: Any) -> OperationResult:
        crew_row, size, _role = _membership(connection, crew_id, account_id, lock=True)
        open_places = size - _used(connection, crew_row)
        if open_places <= 0:
            raise _Refused(409, {"error": "crew_full", "open_places": 0})
        # Turned-off links are kept until they would have expired.
        connection.execute(
            "DELETE FROM crew_invites WHERE crew_id = %s AND expires_at <= %s",
            (crew_row, now),
        )
        live = f"""
            FROM crew_invites AS invite
            WHERE invite.crew_id = %s AND invite.created_by_account_id = %s
              AND {LIVE_INVITE_SQL}
        """
        newest = connection.execute(
            f"SELECT invite.public_id, invite.code, invite.expires_at {live} "
            "ORDER BY invite.created_at DESC, invite.id DESC LIMIT 1",
            (crew_row, account_id, now),
        ).fetchone()
        if not reuse_invite(None if newest is None else newest[2], now, new=new):
            newest = connection.execute(
                """
                INSERT INTO crew_invites (
                    public_id, crew_id, created_by_account_id, code,
                    created_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING public_id, code, expires_at
                """,
                (
                    uuid4(),
                    crew_row,
                    account_id,
                    secrets.token_urlsafe(16),
                    now,
                    now + INVITE_LIFETIME,
                ),
            ).fetchone()
        live_count = connection.execute(
            f"SELECT count(*) {live}", (crew_row, account_id, now)
        ).fetchone()[0]
        return OperationResult(
            200,
            {
                "invite_id": str(newest[0]),
                "code": _text(newest[1]),
                "expires_at": newest[2].isoformat(),
                "open_places": open_places,
                "live_count": int(live_count),
            },
        )

    return _write(database, binding, invite)


def revoke_invite(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    crew_id: str,
    invite_id: str,
    now: datetime,
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def revoke(connection: Any) -> OperationResult:
        crew_row, _size, role = _membership(connection, crew_id, account_id, lock=True)
        invite = connection.execute(
            f"""
            SELECT invite.id, invite.created_by_account_id FROM crew_invites AS invite
            WHERE invite.crew_id = %s AND invite.public_id = %s AND {LIVE_INVITE_SQL}
            """,
            (crew_row, invite_id, now),
        ).fetchone()
        if invite is None:
            raise _Refused(404, {"error": "invite_not_found"})
        if int(invite[1]) != account_id:
            require(role, CrewAction.ALL_LINKS)
        connection.execute(
            "UPDATE crew_invites SET revoked_at = %s WHERE id = %s", (now, invite[0])
        )
        return OperationResult(200, {"revoked": True, "invite_id": invite_id})

    return _write(database, binding, revoke)


def _invite(connection: Any, code: str) -> Invite | None:
    """The link with this code and its crew, live or not."""
    row = connection.execute(
        """
        SELECT crew.id, crew.public_id, crew.name, crew.size, invite.expires_at,
               invite.revoked_at
        FROM crew_invites AS invite
        JOIN crews AS crew ON crew.id = invite.crew_id
        WHERE invite.code = %s
        """,
        (code,),
    ).fetchone()
    if row is None:
        return None
    return Invite(int(row[0]), str(row[1]), _text(row[2]), int(row[3]), row[4], row[5])


def get_invite(
    database: ApiDatabase, account_id: int | None, code: str, *, now: datetime
) -> dict[str, Any]:
    """What a clasher sees on opening an invite link; signed out (None), the
    crew without their accounts. A link that was turned off or ran out names
    its crew, which anyone can look inside; an unknown one says nothing."""
    with database.pool.connection() as connection:
        crew_count = (
            0
            if account_id is None
            else int(
                connection.execute(
                    "SELECT count(*) FROM crew_accounts WHERE account_id = %s",
                    (account_id,),
                ).fetchone()[0]
            )
        )
        invite = _invite(connection, code)
        if invite is None or not invite.live(now):
            return {
                "kind": "crew-invite",
                "state": invite_state(invite, now).value,
                **(
                    {}
                    if invite is None
                    else {"crew_id": invite.crew_id, "name": invite.name}
                ),
                "in_crew": False,
                "crew_count": crew_count,
                "accounts": [],
            }
        crew_row = invite.crew_row
        owner = connection.execute(
            """
            SELECT account.display_name
            FROM crew_accounts AS member
            JOIN clash_lens_accounts AS account ON account.id = member.account_id
            WHERE member.crew_id = %s AND member.role = 'owner'
            """,
            (crew_row,),
        ).fetchone()
        in_crew = (
            account_id is not None
            and connection.execute(
                "SELECT 1 FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
                (crew_row, account_id),
            ).fetchone()
            is not None
        )
        placed = {
            _text(row[0])
            for row in connection.execute(
                """
                SELECT player.normalized_tag
                FROM crew_players AS place
                JOIN players AS player ON player.id = place.player_id
                WHERE place.crew_id = %s AND place.account_id = %s
                """,
                (crew_row, account_id),
            ).fetchall()
        }
        accounts = []
        for _owner, player in _players(
            connection,
            f"""
            (SELECT link.account_id, link.player_id
             FROM verified_player_links AS link
             WHERE link.account_id = %s) AS member {_MEMBER_JOINS}
            """,
            (account_id,),
            now,
        ):
            if player["tag"] in placed:
                eligibility = "already_in_crew"
            elif player["lookup"] == "tracking":
                eligibility = "ok"
            elif player["lookup"] in {"not_in_legend", "uncertain"}:
                eligibility = "not_in_legend"
            else:
                eligibility = "unchecked"
            accounts.append(
                {
                    "tag": player["tag"],
                    "name": player["name"],
                    "trophies": player["trophies"],
                    "eligibility": eligibility,
                }
            )
        used = _used(connection, crew_row)
    state = invite_state(
        invite, now, used=used, in_crew=in_crew, crew_count=crew_count
    )
    return {
        "kind": "crew-invite",
        "state": state.value,
        "crew_id": invite.crew_id,
        "name": invite.name,
        "owner_display_name": None if owner is None else _text(owner[0]),
        "size": invite.size,
        "used": used,
        "expires_at": invite.expires_at,
        "in_crew": in_crew,
        "crew_count": crew_count,
        "accounts": accounts,
    }


def accept_invite(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    code: str,
    tags: list[str],
    now: datetime,
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def accept(connection: Any) -> OperationResult:
        invite = _invite(connection, code)
        if invite_state(invite, now) is InviteState.USABLE:
            assert invite is not None
            connection.execute(
                "SELECT 1 FROM crews WHERE id = %s FOR UPDATE", (invite.crew_row,)
            )
            # Read again under the crew lock: the link may have been turned
            # off, or the crew deleted, while this waited.
            invite = _invite(connection, code)
        if invite is None or invite_state(invite, now) is not InviteState.USABLE:
            raise _Refused(404, {"error": "invite_invalid"})
        member = connection.execute(
            "SELECT 1 FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
            (invite.crew_row, account_id),
        ).fetchone()
        # A full crew or the account's crew limit is refused by the join's
        # own checks, in their order.
        _add_places(
            connection,
            crew_row=invite.crew_row,
            size=invite.size,
            account_id=account_id,
            tags=tags,
            new_role=None if member is not None else move(None, CrewEvent.JOIN),
        )
        return OperationResult(200, {"crew_id": invite.crew_id})

    return _write(database, binding, accept)
