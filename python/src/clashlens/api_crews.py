"""Crews: invite-only groups of Legend League accounts with shared boards.

A crew's members are Clash Lens accounts (``crew_accounts``, which holds the
role); its places are Clash of Clans accounts (``crew_players``), each one
linked to the member holding it. A crew another account is not in reads
exactly like a missing one.

Every write takes its locks in one order so two writes never wait on each
other: the crew row, then the caller's account row (only create and accept,
which count the caller's crews), then player tags in sorted order, the same
"group row, then the tag" order Group saves use.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from . import api_db, api_player_lookup
from .api_accounts import _MEMBER_JOINS, _group_player
from .api_db import (
    ApiDatabase,
    OperationResult,
    RequestBinding,
    _frozen_trophies_sql,
    _opening_day_battles_sql,
    _text,
)
from .domain import ranked_day_for, season_opening_reset

MAX_CREWS_PER_ACCOUNT = 5
MAX_CREW_SIZE = 100
MIN_CREW_SIZE = 2
DEFAULT_CREW_SIZE = 50
INVITE_LIFETIME = timedelta(hours=48)
# Opening Invite shows the caller's newest link again while it has this long
# left, so a link shared yesterday keeps working.
INVITE_REUSE_MIN_LEFT = timedelta(hours=24)
MAX_LIVE_INVITES_PER_MEMBER = 3

_ROLE_ORDER = {"owner": 0, "admin": 1, "member": 2}


class _Refused(Exception):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(payload["error"])
        self.result = OperationResult(status_code, payload)


def _write(
    database: ApiDatabase,
    binding: RequestBinding,
    operation: Callable[[Any], OperationResult],
) -> OperationResult:
    """Run one repeat-safe write. A refusal undoes everything the write did
    so far and is stored as the answer, as every Group write's is."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            try:
                with connection.transaction():
                    result = operation(connection)
            except _Refused as refusal:
                result = refusal.result
            api_db._complete_request(connection, binding.request_id, result)
            return result


def _membership(
    connection: Any, crew_id: str, account_id: int, *, lock: bool
) -> tuple[int, int, str]:
    """The crew's row id, size and the caller's role, locking the crew row
    for a write."""
    row = connection.execute(
        f"""
        SELECT crew.id, crew.size, member.role
        FROM crews AS crew
        JOIN crew_accounts AS member
          ON member.crew_id = crew.id AND member.account_id = %s
        WHERE crew.public_id = %s
        {"FOR UPDATE OF crew" if lock else ""}
        """,
        (account_id, crew_id),
    ).fetchone()
    if row is None:
        raise _Refused(404, {"error": "crew_not_found"})
    return int(row[0]), int(row[1]), _text(row[2])


def _require(role: str, *allowed: str) -> None:
    if role not in allowed:
        raise _Refused(403, {"error": "crew_forbidden"})


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
    new_role: str | None,
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
            (crew_row, account_id, new_role),
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
               {_frozen_trophies_sql("player.id", "%s")},
               {_opening_day_battles_sql("player.id", "%s")},
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
                           AND newer.version > day.version))
        FROM {source}
        ORDER BY player.normalized_tag
        """,
        (opening, opening, ranked_day_for(now).season_start, *params),
    ).fetchall()
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
                _ROLE_ORDER[member["role"]],
                member["display_name"].casefold(),
                member["username"],
            ),
        )
        # Owners and admins can turn off any link, so they see them all; a
        # member sees only their own.
        invites = connection.execute(
            """
            SELECT invite.public_id, account.display_name, invite.expires_at,
                   invite.created_by_account_id = %s
            FROM crew_invites AS invite
            JOIN clash_lens_accounts AS account
              ON account.id = invite.created_by_account_id
            WHERE invite.crew_id = %s AND invite.revoked_at IS NULL
              AND invite.expires_at > %s
              AND (%s OR invite.created_by_account_id = %s)
            ORDER BY invite.created_at DESC, invite.id DESC
            """,
            (account_id, crew_row, now, my_role != "member", account_id),
        ).fetchall()
    return {
        "kind": "crew",
        **header,
        "my_role": my_role,
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
            new_role="owner",
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
        _require(role, "owner", "admin")
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
        _require(role, "owner")
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
        holder, player_id, holder_role = int(place[0]), int(place[1]), _text(place[2])
        if holder == account_id:
            if role == "owner" and len(_own_tags(connection, crew_row, holder)) == 1:
                raise _Refused(409, {"error": "owner_must_hand_over"})
        elif not (
            role == "owner" or (role == "admin" and holder_role == "member")
        ):
            raise _Refused(403, {"error": "crew_forbidden"})
        connection.execute(
            "DELETE FROM crew_players WHERE crew_id = %s AND player_id = %s",
            (crew_row, player_id),
        )
        # A member left with no places is out of the crew (the database's
        # crew_players_member_without_places trigger).
        still_in = connection.execute(
            "SELECT 1 FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
            (crew_row, holder),
        ).fetchone()
        return OperationResult(
            200, {"removed": True, "tag": tag, "left_crew": still_in is None}
        )

    return _write(database, binding, remove)


def _member(connection: Any, crew_row: int, username: str) -> tuple[int, str, str]:
    row = connection.execute(
        """
        SELECT member.account_id, account.display_name, member.role
        FROM crew_accounts AS member
        JOIN clash_lens_accounts AS account ON account.id = member.account_id
        WHERE member.crew_id = %s AND account.normalized_username = %s
        """,
        (crew_row, username),
    ).fetchone()
    if row is None:
        raise _Refused(404, {"error": "member_not_found"})
    return int(row[0]), _text(row[1]), _text(row[2])


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
        _require(my_role, "owner")
        target, display_name, target_role = _member(connection, crew_row, username)
        if target_role == "owner":
            raise _Refused(409, {"error": "owner_role_fixed"})
        connection.execute(
            "UPDATE crew_accounts SET role = %s WHERE crew_id = %s AND account_id = %s",
            (role, crew_row, target),
        )
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
        _require(my_role, "owner")
        target, _display_name, target_role = _member(connection, crew_row, username)
        if target_role == "owner":
            raise _Refused(409, {"error": "owner_role_fixed"})
        # The old owner steps down first, so the one-owner index holds
        # after every statement.
        connection.execute(
            """
            UPDATE crew_accounts SET role = 'admin'
            WHERE crew_id = %s AND account_id = %s
            """,
            (crew_row, account_id),
        )
        connection.execute(
            """
            UPDATE crew_accounts SET role = 'owner'
            WHERE crew_id = %s AND account_id = %s
            """,
            (crew_row, target),
        )
        return OperationResult(200, _header(connection, crew_row))

    return _write(database, binding, transfer)


def leave_crew(
    database: ApiDatabase, binding: RequestBinding, *, crew_id: str
) -> OperationResult:
    assert binding.account_id is not None
    account_id = binding.account_id

    def leave(connection: Any) -> OperationResult:
        crew_row, _size, role = _membership(connection, crew_id, account_id, lock=True)
        if role == "owner":
            raise _Refused(409, {"error": "owner_must_hand_over"})
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
        live = """
            FROM crew_invites
            WHERE crew_id = %s AND created_by_account_id = %s
              AND revoked_at IS NULL AND expires_at > %s
        """
        newest = connection.execute(
            f"SELECT public_id, code, expires_at {live} "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (crew_row, account_id, now),
        ).fetchone()
        if new or newest is None or newest[2] - now < INVITE_REUSE_MIN_LEFT:
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
            connection.execute(
                f"""
                UPDATE crew_invites SET revoked_at = %s
                WHERE id IN (
                    SELECT id {live}
                    ORDER BY created_at DESC, id DESC
                    OFFSET %s
                )
                """,
                (now, crew_row, account_id, now, MAX_LIVE_INVITES_PER_MEMBER),
            )
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
            """
            SELECT id, created_by_account_id FROM crew_invites
            WHERE crew_id = %s AND public_id = %s
              AND revoked_at IS NULL AND expires_at > %s
            """,
            (crew_row, invite_id, now),
        ).fetchone()
        if invite is None:
            raise _Refused(404, {"error": "invite_not_found"})
        if int(invite[1]) != account_id:
            _require(role, "owner", "admin")
        connection.execute(
            "UPDATE crew_invites SET revoked_at = %s WHERE id = %s", (now, invite[0])
        )
        return OperationResult(200, {"revoked": True, "invite_id": invite_id})

    return _write(database, binding, revoke)


def _valid_invite(connection: Any, code: str, now: datetime) -> Any:
    return connection.execute(
        """
        SELECT crew.id, crew.public_id, crew.name, crew.size, invite.expires_at
        FROM crew_invites AS invite
        JOIN crews AS crew ON crew.id = invite.crew_id
        WHERE invite.code = %s AND invite.revoked_at IS NULL
          AND invite.expires_at > %s
        """,
        (code, now),
    ).fetchone()


def get_invite(
    database: ApiDatabase, account_id: int, code: str, *, now: datetime
) -> dict[str, Any]:
    """What a signed-in clasher sees on opening an invite link. A link that
    doesn't work says nothing about the crew it was for."""
    with database.pool.connection() as connection:
        crew_count = int(
            connection.execute(
                "SELECT count(*) FROM crew_accounts WHERE account_id = %s",
                (account_id,),
            ).fetchone()[0]
        )
        crew = _valid_invite(connection, code, now)
        if crew is None:
            return {
                "kind": "crew-invite",
                "state": "invalid",
                "in_crew": False,
                "crew_count": crew_count,
                "accounts": [],
            }
        crew_row = int(crew[0])
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
            connection.execute(
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
    if used >= int(crew[3]):
        state = "full"
    elif not in_crew and crew_count >= MAX_CREWS_PER_ACCOUNT:
        state = "limit"
    else:
        state = "ok"
    return {
        "kind": "crew-invite",
        "state": state,
        "crew_id": str(crew[1]),
        "name": _text(crew[2]),
        "owner_display_name": None if owner is None else _text(owner[0]),
        "size": int(crew[3]),
        "used": used,
        "expires_at": crew[4],
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
        crew = _valid_invite(connection, code, now)
        if crew is not None:
            connection.execute(
                "SELECT 1 FROM crews WHERE id = %s FOR UPDATE", (crew[0],)
            )
            # Read again under the crew lock: the link may have been turned
            # off, or the crew deleted, while this waited.
            crew = _valid_invite(connection, code, now)
        if crew is None:
            raise _Refused(404, {"error": "invite_invalid"})
        crew_row = int(crew[0])
        member = connection.execute(
            "SELECT 1 FROM crew_accounts WHERE crew_id = %s AND account_id = %s",
            (crew_row, account_id),
        ).fetchone()
        _add_places(
            connection,
            crew_row=crew_row,
            size=int(crew[3]),
            account_id=account_id,
            tags=tags,
            new_role=None if member is not None else "member",
        )
        return OperationResult(200, {"crew_id": str(crew[1])})

    return _write(database, binding, accept)
