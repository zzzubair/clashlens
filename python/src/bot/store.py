"""Clash Lens data for the bot, read with the private API's own code.

The bot connects as the API's database role and calls the same reads the
website's pages use, so a number in Discord matches the number on the site.
Its only write is each account's main player.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from clashlens import api_accounts, api_leaderboard, api_players
from clashlens.api_db import AccountContext, ApiDatabase

# The website's own freshness limit for a saved player profile.
FRESHNESS_SECONDS = 900


class Store:
    def __init__(
        self,
        database: ApiDatabase,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.now = now

    def account(self, discord_id: str) -> AccountContext | None:
        """The Clash Lens account this Discord user connected, if any."""
        return api_accounts.resolve_account(self.database, "discord", discord_id)

    def players(self, account: AccountContext) -> list[dict[str, Any]]:
        """Each verified player's trophies, board position and day so far."""
        user = api_accounts.get_public_user(
            self.database, account.username, now=self.now()
        )
        return [] if user is None else user["verified_players"]

    def main_tag(self, account: AccountContext) -> str | None:
        """The saved main, only while it is still verified to this account."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT player.normalized_tag
                FROM discord_bot_main_players AS main
                JOIN verified_player_links AS link
                    ON link.player_id = main.player_id
                   AND link.account_id = main.account_id
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
                    RETURNING player_id
                    """,
                    (account.internal_id, tag),
                ).fetchone()
        return row is not None

    def player_page(self, tag: str) -> dict[str, Any] | None:
        return api_players.get_player_page(
            self.database, tag, now=self.now(), freshness_seconds=FRESHNESS_SECONDS
        )

    def live_rank(self, tag: str) -> int | None:
        with self.database.pool.connection() as connection:
            return api_leaderboard.live_positions(connection, [tag], now=self.now()).get(tag)
