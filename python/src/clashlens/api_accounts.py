from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from . import (
    api_db,
    api_groups,
    api_leaderboard,
    api_player_lookup,
    api_players,
)
from .api_db import (
    AccountContext,
    ApiDatabase,
    OperationResult,
    RequestBinding,
    _account_context,
    _frozen_trophies_sql,
    _opening_day_battles_sql,
    _text,
)
from .domain import awaits_season_reset, ranked_day_for, season_opening_reset

# A public profile shows at most this many verified players.
PUBLIC_PLAYER_LIMIT = 500
# Saving a group starts at most this many new player checks, the most a
# comparison shows, so one save cannot flood the collector's visitor lane.
GROUP_LOOKUP_LIMIT = 20


def resolve_account(
    database, provider: str, provider_subject: str
) -> AccountContext | None:
    """The account holding this sign-in connection; None means setup required
    (see provider_membership)."""
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT a.id, a.public_id, a.normalized_username, a.display_name
            FROM account_provider_identities AS identity
            JOIN clash_lens_accounts AS a ON a.id = identity.account_id
            WHERE identity.provider = %s AND identity.provider_subject = %s
            """,
            (provider, provider_subject),
        ).fetchone()
        return None if row is None else _account_context(row)


def create_account(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    username: str,
    normalized_username: str,
    display_name: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            public_id = uuid4()
            try:
                with connection.transaction():
                    account = connection.execute(
                        """
                        INSERT INTO clash_lens_accounts (
                            public_id, username, normalized_username, display_name
                        ) VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                        (public_id, username, normalized_username, display_name),
                    ).fetchone()
                    assert account is not None
                    account_id = int(account[0])
                    connection.execute(
                        """
                        INSERT INTO account_provider_identities (
                            account_id, provider, provider_subject
                        ) VALUES (%s, %s, %s)
                        """,
                        (account_id, binding.provider, binding.provider_subject),
                    )
            except psycopg.errors.UniqueViolation as error:
                constraint = error.diag.constraint_name or ""
                if "normalized_username" in constraint:
                    result = OperationResult(409, {"error": "username_unavailable"})
                else:
                    result = OperationResult(
                        409, {"error": "provider_identity_conflict"}
                    )
                api_db._complete_request(connection, binding.request_id, result)
                return result

            result = OperationResult(
                201,
                {
                    "username": normalized_username,
                    "display_name": display_name,
                    "preferences": {},
                    "providers": [binding.provider],
                },
            )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def get_account(database: ApiDatabase, account_id: int) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT normalized_username, display_name, preferences
            FROM clash_lens_accounts
            WHERE id = %s
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            return None
        providers = [
            _text(item[0])
            for item in connection.execute(
                """
                SELECT provider
                FROM account_provider_identities
                WHERE account_id = %s
                ORDER BY provider
                """,
                (account_id,),
            )
        ]
        return {
            "username": _text(row[0]),
            "display_name": _text(row[1]),
            "preferences": dict(row[2]),
            "providers": providers,
        }


def update_account(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    username: str,
    normalized_username: str,
    display_name: str,
    preferences: dict[str, Any],
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            account = connection.execute(
                "SELECT normalized_username FROM clash_lens_accounts WHERE id = %s FOR UPDATE",
                (binding.account_id,),
            ).fetchone()
            if account is None:
                result = OperationResult(404, {"error": "account_not_found"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            if (
                normalized_username != _text(account[0])
                or username.lower() != normalized_username
            ):
                result = OperationResult(409, {"error": "username_locked"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            updated = connection.execute(
                """
                UPDATE clash_lens_accounts
                SET display_name = %s, preferences = %s,
                    updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (display_name, Jsonb(preferences), binding.account_id),
            )
            if updated.rowcount != 1:
                result = OperationResult(404, {"error": "account_not_found"})
            else:
                providers = [
                    _text(row[0])
                    for row in connection.execute(
                        """
                        SELECT provider FROM account_provider_identities
                        WHERE account_id = %s ORDER BY provider
                        """,
                        (binding.account_id,),
                    )
                ]
                result = OperationResult(
                    200,
                    {
                        "username": normalized_username,
                        "display_name": display_name,
                        "preferences": preferences,
                        "providers": providers,
                    },
                )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def submit_refresh(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    normalized_tag: str,
    cooldown_seconds: int,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            row = api_db._execute_without_waiting(
                connection,
                normalized_tag,
                """
                SELECT work_id, outcome
                FROM clashlens_enqueue_interactive('live_refresh', %s, %s)
                """,
                (normalized_tag, cooldown_seconds),
            ).fetchone()
            assert row is not None
            collector_work_id = int(row[0])
            public_id = uuid4()
            connection.execute(
                """
                INSERT INTO api_refresh_requests (
                    public_id, collector_work_id, normalized_tag, initial_outcome
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (collector_work_id) DO NOTHING
                """,
                (public_id, collector_work_id, normalized_tag, _text(row[1])),
            )
            refresh = connection.execute(
                """
                SELECT public_id, initial_outcome
                FROM api_refresh_requests
                WHERE collector_work_id = %s
                """,
                (collector_work_id,),
            ).fetchone()
            assert refresh is not None
            result = OperationResult(
                202,
                {
                    "refresh_id": str(refresh[0]),
                    "tag": normalized_tag,
                    "status": "pending",
                    "outcome": _text(refresh[1]),
                },
            )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def get_refresh_status(database: ApiDatabase, refresh_id: str) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT refresh.public_id, refresh.normalized_tag,
                   work.status, refresh.initial_outcome
            FROM api_refresh_requests AS refresh
            JOIN collector_work AS work ON work.id = refresh.collector_work_id
            WHERE refresh.public_id = %s
            """,
            (refresh_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "refresh_id": str(row[0]),
            "tag": _text(row[1]),
            "status": _text(row[2]),
            "outcome": _text(row[3]),
        }


def create_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    name: str,
    normalized_name: str,
    normalized_tags: list[str],
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            public_id = uuid4()
            try:
                with connection.transaction():
                    group = connection.execute(
                        """
                        INSERT INTO account_groups (
                            public_id, account_id, name, normalized_name
                        ) VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                        (public_id, binding.account_id, name, normalized_name),
                    ).fetchone()
                    assert group is not None
                    _replace_group_players(connection, int(group[0]), normalized_tags)
            except psycopg.errors.UniqueViolation:
                result = OperationResult(409, {"error": "group_name_conflict"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            except psycopg.errors.CheckViolation as error:
                if error.diag.constraint_name != "account_groups_limit":
                    raise
                result = OperationResult(422, {"error": "group_limit_reached"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            result = OperationResult(
                201,
                {
                    "group_id": str(public_id),
                    "name": name,
                    "tags": sorted(set(normalized_tags)),
                },
            )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def update_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    name: str,
    normalized_name: str,
    normalized_tags: list[str] | None,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            try:
                with connection.transaction():
                    group = connection.execute(
                        """
                        UPDATE account_groups
                        SET name = %s, normalized_name = %s,
                            updated_at = clock_timestamp()
                        WHERE public_id = %s AND account_id = %s
                        RETURNING id
                        """,
                        (name, normalized_name, group_id, binding.account_id),
                    ).fetchone()
                    if group is not None and normalized_tags is not None:
                        _replace_group_players(connection, int(group[0]), normalized_tags)
            except psycopg.errors.UniqueViolation:
                result = OperationResult(409, {"error": "group_name_conflict"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            if group is None:
                result = OperationResult(404, {"error": "group_not_found"})
            else:
                result = OperationResult(
                    200,
                    {
                        "group_id": group_id,
                        "name": name,
                        "tags": sorted(set(normalized_tags))
                        if normalized_tags is not None
                        else [player["tag"] for player in _group_players(
                            connection, int(group[0])
                        )],
                    },
                )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def delete_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            deleted = connection.execute(
                """
                DELETE FROM account_groups
                WHERE public_id = %s AND account_id = %s
                """,
                (group_id, binding.account_id),
            )
            if deleted.rowcount == 1:
                result = OperationResult(
                    200, {"deleted": True, "group_id": group_id}
                )
            else:
                result = OperationResult(404, {"error": "group_not_found"})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def add_group_player(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    normalized_tag: str,
    now: datetime | None = None,
) -> OperationResult:
    """Add one player the game has already confirmed exists."""
    now = now or datetime.now(UTC)
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            group = _lock_group(connection, binding, group_id)
            if group is None:
                result = OperationResult(404, {"error": "group_not_found"})
            else:
                # Same order as saving a whole group: the group row, then the tag.
                api_player_lookup.lock_tag(connection, normalized_tag)
                members = [
                    player["tag"]
                    for player in _group_players(connection, group, now=now)
                ]
                state = api_player_lookup._lookup(connection, normalized_tag)["state"]
                if normalized_tag in members:
                    result = OperationResult(409, {"error": "group_player_exists"})
                elif len(members) >= api_groups.MAX_COMPARED_MEMBERS:
                    result = OperationResult(422, {"error": "group_full"})
                elif state == api_player_lookup.LookupState.NOT_FOUND:
                    result = OperationResult(422, {"error": "player_not_found"})
                elif state not in api_player_lookup.CONFIRMED:
                    # Unchecked, still checking or a failed check: the caller
                    # starts or waits for the player lookup and tries again.
                    result = OperationResult(
                        409, {"error": "player_not_checked", "state": state}
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO account_group_players (group_id, player_id)
                        SELECT %s, id FROM players WHERE normalized_tag = %s
                        """,
                        (group, normalized_tag),
                    )
                    [player] = [
                        player
                        for player in _group_players(connection, group, now=now)
                        if player["tag"] == normalized_tag
                    ]
                    result = OperationResult(200, {"group_id": group_id, **player})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def remove_group_player(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    normalized_tag: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            group = _lock_group(connection, binding, group_id)
            if group is None:
                result = OperationResult(404, {"error": "group_not_found"})
            else:
                removed = connection.execute(
                    """
                    DELETE FROM account_group_players AS member
                    USING players AS player
                    WHERE member.group_id = %s
                      AND member.player_id = player.id
                      AND player.normalized_tag = %s
                    """,
                    (group, normalized_tag),
                )
                result = OperationResult(
                    200,
                    {
                        "group_id": group_id,
                        "tag": normalized_tag,
                        "removed": removed.rowcount == 1,
                    },
                )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def list_groups(
    database: ApiDatabase, account_id: int, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    with database.pool.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT group_row.public_id, group_row.name, player.normalized_tag,
                   player.active, COALESCE(accepted.name, latest.name),
                   accepted.trophies, accepted.current_league_season_id,
                   {_frozen_trophies_sql("player.id", "%s")},
                   {_opening_day_battles_sql("player.id", "%s")},
                   group_row.created_at
            FROM account_groups AS group_row
            LEFT JOIN account_group_players AS member ON member.group_id = group_row.id
            {_MEMBER_JOINS}
            WHERE group_row.account_id = %s
            ORDER BY group_row.normalized_name, group_row.public_id, player.normalized_tag
            LIMIT 10000
            """,
            (season_opening_reset(now), season_opening_reset(now), account_id),
        ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            public_id = str(row[0])
            # The dashboard shows the first group made, so each says when.
            group = groups.setdefault(
                public_id,
                {
                    "group_id": public_id,
                    "name": _text(row[1]),
                    "created_at": row[-1].astimezone(UTC).isoformat(),
                    "tags": [],
                    "players": [],
                },
            )
            if row[2] is not None:
                player = _group_player(connection, row[2:-1], now)
                group["tags"].append(player["tag"])
                group["players"].append(player)
        return list(groups.values())


def search_public_users(
    database: ApiDatabase, query: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("public user search limit is outside the supported range")
    query = query.strip().removeprefix("@")
    if not query:
        return []
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT account.normalized_username, account.display_name,
                   (SELECT count(*) FROM verified_player_links
                    WHERE account_id = account.id)
            FROM clash_lens_accounts AS account
            WHERE account.normalized_username ILIKE %s ESCAPE '\\'
               OR account.display_name ILIKE %s ESCAPE '\\'
               OR EXISTS (
                   SELECT 1 FROM verified_player_links AS link
                   JOIN players AS player ON player.id = link.player_id
                   LEFT JOIN LATERAL (
                       SELECT name, player_id FROM player_profile_versions
                       WHERE normalized_tag = player.normalized_tag
                       ORDER BY observed_at DESC, id DESC LIMIT 1
                   ) AS profile ON profile.player_id = player.id
                   WHERE link.account_id = account.id
                     AND (player.normalized_tag = %s OR
                          profile.name ILIKE %s ESCAPE '\\')
               )
            ORDER BY (account.normalized_username = lower(%s)) DESC,
                     account.normalized_username
            LIMIT %s
            """,
            (pattern, pattern, query.upper(), pattern, query, limit),
        ).fetchall()
    return [
        {
            "username": _text(row[0]),
            "display_name": _text(row[1]),
            "linked_player_count": int(row[2]),
        }
        for row in rows
    ]


def get_public_user(
    database: ApiDatabase,
    normalized_username: str,
    *,
    now: datetime,
    live_board: api_leaderboard.LiveBoardCache | None = None,
) -> dict[str, Any] | None:
    """The account's public name and linked players' cards; ranks come from
    ``live_board``'s shared read when one is given."""
    with database.pool.connection() as connection:
        account = connection.execute(
            """
            SELECT id, normalized_username, display_name
            FROM clash_lens_accounts
            WHERE normalized_username = %s
            """,
            (normalized_username,),
        ).fetchone()
        if account is None:
            return None
        # One read gives every linked player's card, never one per player.
        cards = api_players.player_cards(
            connection,
            _linked_players(connection, int(account[0])),
            now=now,
            ranks=live_board is None,
        )
    if live_board is not None:
        # Ranks from the shared Live Leaderboard, read outside a held
        # connection so a board read never waits for a second one.
        api_players.rank_cards(
            cards, lambda tags: live_board.current(database, now).positions(tags)
        )
    return {
        "username": _text(account[1]),
        "display_name": _text(account[2]),
        "verified_players": cards,
    }


def get_public_user_days(
    database: ApiDatabase, normalized_username: str, *, now: datetime
) -> dict[str, Any] | None:
    """What the public profile's player cards add to their numbers, for the
    same verified players as ``get_public_user``, at most as many: each one's
    place at the last Reset and this Season's days, read for every player at
    once (``api_leaderboard.linked_season_days``)."""
    with database.pool.connection() as connection:
        account = connection.execute(
            "SELECT id FROM clash_lens_accounts WHERE normalized_username = %s",
            (normalized_username,),
        ).fetchone()
        if account is None:
            return None
        tags = [
            _text(row[0])
            for row in connection.execute(
                """
                SELECT player.normalized_tag
                FROM verified_player_links AS link
                JOIN players AS player ON player.id = link.player_id
                WHERE link.account_id = %s
                ORDER BY player.normalized_tag
                LIMIT %s
                """,
                (int(account[0]), PUBLIC_PLAYER_LIMIT),
            ).fetchall()
        ]
        players = api_leaderboard.linked_season_days(connection, tags, now)
    return {
        "kind": "public-user-days",
        "season_start": ranked_day_for(now).season_start.isoformat(),
        "players": players,
    }


def get_multi_account_summary(database: ApiDatabase, account_id: int) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        account = connection.execute(
            """
            SELECT normalized_username, display_name
            FROM clash_lens_accounts
            WHERE id = %s
            """,
            (account_id,),
        ).fetchone()
        if account is None:
            return None
        default = default_player_tag(connection, account_id)
        return {
            "username": _text(account[0]),
            "display_name": _text(account[1]),
            "verified_players": _verified_players(connection, account_id),
            "default_player": default,
            # Who /stats uses once the default player is unlinked.
            "next_default_player": default_player_tag(connection, account_id, unlinked=default),
        }


def default_player_tag(
    connection: Any, account_id: int, *, unlinked: str | None = None
) -> str | None:
    """The account's chosen default player while it is still verified to it,
    otherwise its first verified player Clash Lens tracks; None when there is
    neither, so nothing points the bot at a player with no data. ``unlinked``
    gives the default once that player is unlinked."""
    row = connection.execute(
        """
        SELECT player.normalized_tag
        FROM verified_player_links AS link
        JOIN players AS player ON player.id = link.player_id
        LEFT JOIN account_default_players AS chosen
            ON chosen.account_id = link.account_id AND chosen.player_id = link.player_id
        WHERE link.account_id = %s AND player.normalized_tag IS DISTINCT FROM %s
          AND (chosen.player_id IS NOT NULL OR player.active)
        ORDER BY chosen.player_id IS NULL, link.verified_at, link.player_id
        LIMIT 1
        """,
        (account_id, unlinked),
    ).fetchone()
    return None if row is None else _text(row[0])


def set_default_player(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str
) -> OperationResult:
    """Choose the verified player the Discord bot uses when none is named."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            # Locking the ownership row holds off a transfer until this commits,
            # so the migration 0105 trigger then forgets the choice.
            link = connection.execute(
                """
                SELECT link.player_id, link.account_id
                FROM verified_player_links AS link
                JOIN players AS player ON player.id = link.player_id
                WHERE player.normalized_tag = %s
                FOR SHARE OF link
                """,
                (normalized_tag,),
            ).fetchone()
            if link is None or int(link[1]) != binding.account_id:
                result = OperationResult(404, {"error": "player_not_verified"})
            else:
                connection.execute(
                    """
                    INSERT INTO account_default_players (account_id, player_id)
                    VALUES (%s, %s)
                    ON CONFLICT (account_id) DO UPDATE
                        SET player_id = EXCLUDED.player_id,
                            updated_at = clock_timestamp()
                    """,
                    (binding.account_id, link[0]),
                )
                result = OperationResult(200, {"default_player": normalized_tag})
            api_db._complete_request(connection, binding.request_id, result)
            return result


class PlayerUnlink(StrEnum):
    """What unlinking one of the account's verified players did.

    - ``NOT_VERIFIED``: the player is not verified to this account (404).
    - ``HAND_OVER_FIRST``: it is the owner's only player in a crew they own,
      so nothing changed (409, naming the crews).
    - ``UNLINKED``: the verified link row is gone. In the same transaction
      its crew places go by cascade (migration 0095), and the migration
      0105, 0108 and 0112 triggers forget it as the default player and drop
      its Discord alerts and day summary. A support transfer
      (``api_verification.TransferCandidate``) fires the same triggers.

    The player row and its game data stay, so it can be linked again by a
    new verification. The answer is saved with the request, so a retry
    replays it.
    """

    NOT_VERIFIED = "player_not_verified"
    HAND_OVER_FIRST = "owner_must_hand_over"
    UNLINKED = "unlinked"


def unlink_player(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str
) -> OperationResult:
    """Unlink one of the account's own verified players: only the link goes
    (see ``PlayerUnlink``). Refused while it is the owner's only player in a
    crew they own: they hand over or delete it first, as when removing it
    from the crew."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            outcome, crews = _unlink_player(connection, binding.account_id, normalized_tag)
            if outcome is PlayerUnlink.UNLINKED:
                result = OperationResult(200, {"unlinked": normalized_tag})
            elif outcome is PlayerUnlink.HAND_OVER_FIRST:
                result = OperationResult(409, {"error": outcome, "crews": crews})
            else:
                result = OperationResult(404, {"error": outcome})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def _unlink_player(
    connection: Any, account_id: int | None, normalized_tag: str
) -> tuple[PlayerUnlink, list[dict[str, str]]]:
    link = connection.execute(
        """
        SELECT link.player_id
        FROM verified_player_links AS link
        JOIN players AS player ON player.id = link.player_id
        WHERE player.normalized_tag = %s AND link.account_id = %s
        FOR UPDATE OF link
        """,
        (normalized_tag, account_id),
    ).fetchone()
    if link is None:
        return PlayerUnlink.NOT_VERIFIED, []
    # Every crew write holds the crew row first (migration 0095).
    connection.execute(
        """
        SELECT 1 FROM crews
        WHERE id IN (SELECT crew_id FROM crew_players WHERE player_id = %s)
        ORDER BY id
        FOR UPDATE
        """,
        (link[0],),
    )
    owned = connection.execute(
        """
        SELECT crew.public_id, crew.name
        FROM crew_players AS place
        JOIN crews AS crew ON crew.id = place.crew_id
        JOIN crew_accounts AS member
          ON member.crew_id = place.crew_id
         AND member.account_id = place.account_id
        WHERE place.player_id = %s AND member.role = 'owner'
          AND NOT EXISTS (
              SELECT 1 FROM crew_players AS other
              WHERE other.crew_id = place.crew_id
                AND other.account_id = place.account_id
                AND other.player_id <> place.player_id
          )
        ORDER BY lower(crew.name), crew.id
        """,
        (link[0],),
    ).fetchall()
    if owned:
        crews = [{"crew_id": str(row[0]), "name": _text(row[1])} for row in owned]
        return PlayerUnlink.HAND_OVER_FIRST, crews
    removed = connection.execute(
        "DELETE FROM verified_player_links WHERE player_id = %s AND account_id = %s",
        (link[0], account_id),
    )
    return (PlayerUnlink.UNLINKED if removed.rowcount == 1 else PlayerUnlink.NOT_VERIFIED), []


def _replace_group_players(
    connection: Any,
    group_id: int,
    normalized_tags: list[str],
) -> None:
    tags = sorted(set(normalized_tags))
    # A player lookup takes the tag lock before touching the player row, so
    # take every tag lock first, in one order, before inserting players.
    for normalized_tag in tags:
        api_player_lookup.lock_tag(connection, normalized_tag)
    connection.execute(
        "DELETE FROM account_group_players WHERE group_id = %s",
        (group_id,),
    )
    for normalized_tag in tags:
        player_id = api_db._ensure_player(connection, normalized_tag)
        connection.execute(
            """
            INSERT INTO account_group_players (group_id, player_id)
            VALUES (%s, %s)
            """,
            (group_id, player_id),
        )
    # A tag Clash Lens has no profile for starts the same check a player page
    # visit starts, so the group shows it as being checked, not inactive.
    unchecked = connection.execute(
        """
        SELECT player.normalized_tag
        FROM account_group_players AS member
        JOIN players AS player ON player.id = member.player_id
        WHERE member.group_id = %s AND NOT player.active
          AND player.current_profile_version_id IS NULL
        ORDER BY player.normalized_tag
        LIMIT %s
        """,
        (group_id, GROUP_LOOKUP_LIMIT),
    ).fetchall()
    for (normalized_tag,) in unchecked:
        api_player_lookup.admit(connection, _text(normalized_tag))


def _lock_group(connection: Any, binding: RequestBinding, group_id: str) -> int | None:
    # Filtering by the account means another account's group reads as missing.
    row = connection.execute(
        """
        SELECT id FROM account_groups
        WHERE public_id = %s AND account_id = %s
        FOR UPDATE
        """,
        (group_id, binding.account_id),
    ).fetchone()
    return None if row is None else int(row[0])


# Trophies come only from accepted Legend statistics. The name comes from the
# accepted current profile, or else from the newest saved profile, whose name
# is validated separately from Legend season/tier evidence.
_MEMBER_JOINS = """
    LEFT JOIN players AS player ON player.id = member.player_id
    LEFT JOIN player_profile_versions AS accepted
        ON accepted.id = player.current_profile_version_id
       AND accepted.source_contract_state = 'accepted'
    LEFT JOIN LATERAL (
        SELECT name, player_id FROM player_profile_versions
        WHERE accepted.id IS NULL AND normalized_tag = player.normalized_tag
        ORDER BY observed_at DESC, id DESC LIMIT 1
    ) AS latest ON latest.player_id = player.id
"""


def _group_players(
    connection: Any, group_id: int, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Each member's tag with the stored in-game name and trophies, when known."""
    now = now or datetime.now(UTC)
    rows = connection.execute(
        f"""
        SELECT player.normalized_tag, player.active,
               COALESCE(accepted.name, latest.name), accepted.trophies,
               accepted.current_league_season_id,
               {_frozen_trophies_sql("player.id", "%s")},
               {_opening_day_battles_sql("player.id", "%s")}
        FROM account_group_players AS member
        {_MEMBER_JOINS}
        WHERE member.group_id = %s
        ORDER BY player.normalized_tag
        LIMIT 101
        """,
        (season_opening_reset(now), season_opening_reset(now), group_id),
    ).fetchall()
    return [_group_player(connection, row, now) for row in rows]


def _group_player(connection: Any, row: Any, now: datetime) -> dict[str, Any]:
    tag, active, name, trophies, season_id, frozen_trophies, day_battles = row
    pending = trophies is not None and awaits_season_reset(
        None if season_id is None else _text(season_id),
        int(trophies),
        frozen_trophies,
        day_battles,
        now,
    )
    return {
        "tag": _text(tag),
        "name": None if name is None else _text(name),
        "trophies": None if trophies is None or pending else int(trophies),
        "season_reset_pending": pending,
        "state": "tracking"
        if active
        else api_player_lookup._lookup(connection, _text(tag))["state"],
    }


def _verified_players(
    connection: Any, account_id: int, limit: int | None = 500
) -> list[dict[str, Any]]:
    return [
        {"tag": tag, "name": name}
        for _id, tag, name, _clan, _league in _linked_players(connection, account_id, limit)
    ]


def _linked_players(
    connection: Any, account_id: int, limit: int | None = PUBLIC_PLAYER_LIMIT
) -> list[tuple[int, str, str | None, str | None, str | None]]:
    """Each verified player's id, tag, and newest saved name, clan and league,
    the first `limit` by tag, or all of them when it is None."""
    rows = connection.execute(
        f"""
        SELECT {api_players.PLAYER_ROW_COLUMNS}
        FROM verified_player_links AS link
        JOIN players AS player ON player.id = link.player_id
        {api_players.NEWEST_PROFILE_JOIN}
        WHERE link.account_id = %s
        ORDER BY player.normalized_tag
        LIMIT %s
        """,
        (account_id, limit),
    ).fetchall()
    return [api_players.player_row(row) for row in rows]
