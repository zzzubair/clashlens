"""Clash Lens data for the bot, read with the private API's own code.

The bot connects as the API's database role and calls the same reads the
website's pages use, so a number in Discord matches the number on the site.
Its only write is each account's main player. Every read takes the moment of
the command, so one reply never mixes two Legend days.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from clashlens import api_accounts, api_leaderboard, api_players
from clashlens.api_db import AccountContext, ApiDatabase

# The website's own freshness limit for a saved player profile.
FRESHNESS_SECONDS = 900


class Store:
    def __init__(self, database: ApiDatabase) -> None:
        self.database = database

    def account(self, discord_id: str) -> AccountContext | None:
        """The Clash Lens account this Discord user connected, if any."""
        return api_accounts.resolve_account(self.database, "discord", discord_id)

    def players(self, account: AccountContext, now: datetime) -> list[dict[str, Any]]:
        """Each verified player's trophies, board position, day so far and how
        old its saved profile is, the same age the player page shows. The age
        is read first: a profile saved in between only makes it look older."""
        with self.database.pool.connection() as connection:
            seen = dict(
                connection.execute(
                    """
                    SELECT player.normalized_tag,
                           GREATEST(player.current_observed_at,
                                    player.current_profile_confirmed_at)
                    FROM verified_player_links AS link
                    JOIN players AS player ON player.id = link.player_id
                    WHERE link.account_id = %s
                    """,
                    (account.internal_id,),
                ).fetchall()
            )
        user = api_accounts.get_public_user(self.database, account.username, now=now)
        if user is None:
            return []
        cards = user["verified_players"]
        for card in cards:
            at = seen.get(card["tag"])
            card["age_seconds"] = (
                None if at is None else max(0, int((now - at).total_seconds()))
            )
        return cards

    def main_tag(self, account: AccountContext) -> str | None:
        """The saved main, only while the verification it was chosen under stands."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT player.normalized_tag
                FROM discord_bot_main_players AS main
                JOIN verified_player_links AS link
                    ON link.verification_request_id = main.verification_request_id
                   AND link.account_id = main.account_id
                JOIN players AS player ON player.id = link.player_id
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
                    INSERT INTO discord_bot_main_players (account_id, verification_request_id)
                    SELECT link.account_id, link.verification_request_id
                    FROM verified_player_links AS link
                    JOIN players AS player ON player.id = link.player_id
                    WHERE link.account_id = %s AND player.normalized_tag = %s
                    ON CONFLICT (account_id) DO UPDATE
                        SET verification_request_id = EXCLUDED.verification_request_id,
                            updated_at = clock_timestamp()
                    RETURNING account_id
                    """,
                    (account.internal_id, tag),
                ).fetchone()
        return row is not None

    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None:
        return api_players.get_player_page(
            self.database, tag, now=now, freshness_seconds=FRESHNESS_SECONDS
        )

    def live_rank(self, tag: str, now: datetime) -> int | None:
        with self.database.pool.connection() as connection:
            return api_leaderboard.live_positions(connection, [tag], now=now).get(tag)
