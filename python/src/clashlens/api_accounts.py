from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from . import (
    api_db,
    api_leaderboard,
    api_players,
    api_requests,
)
from .api_db import ApiDatabase
from .api_players import PUBLIC_PLAYER_LIMIT
from .api_requests import AccountContext, OperationResult, RequestBinding
from .db_rows import text_value
from .domain import ranked_day_for


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
        return None if row is None else account_context(row)


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
            existing = api_requests.reserve_request(connection, binding)
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
                api_requests.complete_request(connection, binding.request_id, result)
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
            api_requests.complete_request(connection, binding.request_id, result)
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
            text_value(item[0])
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
            "username": text_value(row[0]),
            "display_name": text_value(row[1]),
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
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            account = connection.execute(
                "SELECT normalized_username FROM clash_lens_accounts WHERE id = %s FOR UPDATE",
                (binding.account_id,),
            ).fetchone()
            if account is None:
                result = OperationResult(404, {"error": "account_not_found"})
                api_requests.complete_request(connection, binding.request_id, result)
                return result
            if (
                normalized_username != text_value(account[0])
                or username.lower() != normalized_username
            ):
                result = OperationResult(409, {"error": "username_locked"})
                api_requests.complete_request(connection, binding.request_id, result)
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
                    text_value(row[0])
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
            api_requests.complete_request(connection, binding.request_id, result)
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
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            row = api_db.execute_without_waiting(
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
                (public_id, collector_work_id, normalized_tag, text_value(row[1])),
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
                    "outcome": text_value(refresh[1]),
                },
            )
            api_requests.complete_request(connection, binding.request_id, result)
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
            "tag": text_value(row[1]),
            "status": text_value(row[2]),
            "outcome": text_value(row[3]),
        }


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
            "username": text_value(row[0]),
            "display_name": text_value(row[1]),
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
            api_players.linked_players(connection, int(account[0])),
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
        "username": text_value(account[1]),
        "display_name": text_value(account[2]),
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
            text_value(row[0])
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
            "username": text_value(account[0]),
            "display_name": text_value(account[1]),
            "verified_players": api_players.verified_players(connection, account_id),
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
    return None if row is None else text_value(row[0])


def set_default_player(
    database: ApiDatabase, binding: RequestBinding, *, normalized_tag: str
) -> OperationResult:
    """Choose the verified player the Discord bot uses when none is named."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            # Locking the ownership row holds off a transfer until this commits,
            # so the migration 0105 trigger then forgets the choice.
            link = api_players.verified_player_owner(connection, normalized_tag)
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
            api_requests.complete_request(connection, binding.request_id, result)
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
            existing = api_requests.reserve_request(connection, binding)
            if existing is not None:
                return existing
            outcome, crews = _unlink_player(connection, binding.account_id, normalized_tag)
            if outcome is PlayerUnlink.UNLINKED:
                result = OperationResult(200, {"unlinked": normalized_tag})
            elif outcome is PlayerUnlink.HAND_OVER_FIRST:
                result = OperationResult(409, {"error": outcome, "crews": crews})
            else:
                result = OperationResult(404, {"error": outcome})
            api_requests.complete_request(connection, binding.request_id, result)
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
        crews = [{"crew_id": str(row[0]), "name": text_value(row[1])} for row in owned]
        return PlayerUnlink.HAND_OVER_FIRST, crews
    removed = connection.execute(
        "DELETE FROM verified_player_links WHERE player_id = %s AND account_id = %s",
        (link[0], account_id),
    )
    return (PlayerUnlink.UNLINKED if removed.rowcount == 1 else PlayerUnlink.NOT_VERIFIED), []


def account_context(row: Any) -> AccountContext:
    return AccountContext(
        internal_id=int(row[0]),
        public_id=str(row[1]),
        username=text_value(row[2]),
        display_name=text_value(row[3]),
    )
