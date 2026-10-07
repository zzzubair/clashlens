"""Clash Lens data for the bot, read with the private API's own code.

The bot connects as the API's database role and calls the same reads the
website's pages use, so a number in Discord matches the number on the site.
Its only writes are each account's main player. Every read takes the moment
of the command, so one reply never mixes two Legend days.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from clashlens import api_accounts, api_leaderboard, api_player_lookup, api_players
from clashlens.api_db import AccountContext, ApiDatabase

# The website's own freshness limit for a saved player profile.
FRESHNESS_SECONDS = 900


class _OneConnection:
    """The API database with every read on one open connection, so the
    website's own page read shares that connection's snapshot."""

    def __init__(self, database: ApiDatabase, connection: Any) -> None:
        self._database = database
        self._connection = connection
        self.pool = self

    @contextmanager
    def connection(self) -> Iterator[Any]:
        yield self._connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._database, name)


class Store:
    def __init__(self, database: ApiDatabase) -> None:
        self.database = database

    def account(self, discord_id: str) -> AccountContext | None:
        """The Clash Lens account this Discord user connected, if any."""
        return api_accounts.resolve_account(self.database, "discord", discord_id)

    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]:
        """Each verified player's card, read with the website's own code, and
        when its saved profile was last seen, both from one database snapshot
        so the time always belongs to the numbers."""
        with self.database.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cards = api_players.player_cards(
                connection,
                api_accounts._linked_players(connection, account.internal_id),
                now=now,
            )
            seen = dict(
                connection.execute(
                    """
                    SELECT normalized_tag,
                           GREATEST(current_observed_at, current_profile_confirmed_at)
                    FROM players
                    WHERE normalized_tag = ANY(%s)
                    """,
                    ([card["tag"] for card in cards],),
                ).fetchall()
            )
        for card in cards:
            card["observed_at"] = seen.get(card["tag"])
        return cards

    def main_tag(self, account: AccountContext) -> str | None:
        """The saved main; one no longer verified to this account is forgotten."""
        with self.database.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    DELETE FROM discord_bot_main_players AS main
                    WHERE main.account_id = %s
                      AND NOT EXISTS (
                          SELECT 1 FROM verified_player_links AS link
                          WHERE link.player_id = main.player_id
                            AND link.account_id = main.account_id
                      )
                    """,
                    (account.internal_id,),
                )
                row = connection.execute(
                    """
                    SELECT player.normalized_tag
                    FROM discord_bot_main_players AS main
                    JOIN players AS player ON player.id = main.player_id
                    WHERE main.account_id = %s
                    """,
                    (account.internal_id,),
                ).fetchone()
        return None if row is None else str(row[0])

    def set_main(self, account: AccountContext, tag: str) -> bool:
        """Save `tag` as the main; False when it is not this account's player."""
        with self.database.pool.connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    INSERT INTO discord_bot_main_players (account_id, player_id)
                    SELECT link.account_id, link.player_id
                    FROM verified_player_links AS link
                    JOIN players AS player ON player.id = link.player_id
                    WHERE link.account_id = %s AND player.normalized_tag = %s
                    ON CONFLICT (account_id) DO UPDATE
                        SET player_id = EXCLUDED.player_id,
                            updated_at = clock_timestamp()
                    RETURNING account_id
                    """,
                    (account.internal_id, tag),
                ).fetchone()
        return row is not None

    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None:
        """The website's player page with the player's lookup state and
        reason, all from one read-only database snapshot."""
        with self.database.pool.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            page = api_players.get_player_page(
                _OneConnection(self.database, connection),  # type: ignore[arg-type]
                tag,
                now=now,
                freshness_seconds=FRESHNESS_SECONDS,
            )
            if page is None:
                return None
            lookup = api_player_lookup._lookup(connection, tag)
        return {
            **page,
            "observed_at": datetime.fromisoformat(page["observed_at"]),
            "state": lookup["state"],
            "reason": lookup.get("reason"),
        }

    def live_rank(self, tag: str, now: datetime) -> int | None:
        with self.database.pool.connection() as connection:
            return api_leaderboard.live_positions(connection, [tag], now=now).get(tag)
