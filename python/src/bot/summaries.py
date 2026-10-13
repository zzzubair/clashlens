"""Legend day summaries: one Discord DM a Legend day for each Clasher with
Discord connected, summing up the day just ended for each of their verified
players.

A player's summary is on unless the Clasher turned it off on the website's
Discord alerts page, separately from its attack alerts; each player turned
off has a discord_day_summary_off row (migration 0111).
Nothing is read for a Reset until its Daily board is published, so the
summaries never compete with the Reset's own work, and a late board only
makes them late.
They go out only during the Legend day that Reset starts: a bot that was
down for a whole day skips that day's summaries.

Each player's day is read with the website's own player page read, so the
numbers and the label (Verified, Calculated, Uncertain) are the ones their
page shows; the end of day is the day's own, before any weekly or Season
reset, which is shown beside it. Every place in a round's summaries is read
from one version of the Reset's Daily board. A player whose day is not saved
yet, or whom that board went without, is named as not finished; one with no
Legend day that day is left out, and a Clasher with no finished day gets no
DM.

Each summary is logged before its first DM and sent by the attack alert
loop, with the same tries again; so no Clasher is sent the same day's
summary twice, across restarts too. A summary too long for one DM goes as
several, each with its own nonce. It is logged only while every player it
names is still verified to the account with its summary on, else it is
read again next round; each DM is tried, and tried again, only while every
player that DM names still is, else it and the DMs after it are dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for

from . import replies
from .alerts import KEEP, Delivery
from .replies import Site
from .store import Store

log = logging.getLogger("clashlens.bot.summaries")

# Accounts read in one round, so attack alerts never wait long behind them.
BATCH = 10

# Where an account's summary, or one of its DMs, is still due: Discord still
# connected as the same user, and every player in %(tags)s still verified to
# the account with its summary on.
_STILL_ON = """
    FROM clash_lens_accounts AS account
    JOIN account_provider_identities AS identity
        ON identity.account_id = account.id AND identity.provider = 'discord'
    WHERE account.id = %(account_id)s AND identity.provider_subject = %(discord_user_id)s
      AND NOT EXISTS (
          SELECT 1 FROM unnest(%(tags)s::text[]) AS named (tag)
          WHERE NOT EXISTS (
              SELECT 1 FROM verified_player_links AS link
              JOIN players AS player ON player.id = link.player_id
              WHERE link.account_id = account.id AND player.normalized_tag = named.tag
                AND NOT EXISTS (
                    SELECT 1 FROM discord_day_summary_off AS off
                    WHERE off.account_id = link.account_id AND off.player_id = link.player_id
                )
          )
      )
"""


class Summaries:
    """The summary reads and the summary delivery log."""

    NONCE_PREFIX = "summary-"

    def __init__(
        self,
        database: ApiDatabase,
        store: Store,
        site: Site,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.database = database
        self.store = store
        self.site = site
        self.now = now

    def due(self) -> list[Delivery]:
        """Summaries of the Legend day the latest Reset ended, for up to
        `BATCH` accounts not yet logged for it, once its Daily board is
        published. An account with no finished day is logged as skipped."""
        now = self.now()
        reset = ranked_day_for(now).start
        with self.database.pool.connection() as connection:
            # The Reset's Daily board, as the player page finds it
            # (season_summaries.reset_board_ranks).
            board = connection.execute(
                """
                SELECT snapshot.id FROM leaderboard_snapshots AS snapshot
                WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
                  AND snapshot.boundary_at = %s
                  AND (NOT EXISTS (SELECT 1 FROM boundary_publication_generations AS g
                                   WHERE g.snapshot_id = snapshot.id)
                       OR EXISTS (SELECT 1 FROM boundary_publication_generations AS g
                                  WHERE g.snapshot_id = snapshot.id
                                    AND g.snapshot_state <> 'superseded'))
                ORDER BY snapshot.version DESC, snapshot.id DESC
                LIMIT 1
                """,
                (reset,),
            ).fetchone()
            if board is None:
                return []
            accounts = connection.execute(
                """
                SELECT account.id, identity.provider_subject,
                       array_agg(player.normalized_tag ORDER BY player.normalized_tag)
                FROM clash_lens_accounts AS account
                JOIN account_provider_identities AS identity
                    ON identity.account_id = account.id AND identity.provider = 'discord'
                JOIN verified_player_links AS link ON link.account_id = account.id
                JOIN players AS player ON player.id = link.player_id
                WHERE NOT EXISTS (
                      SELECT 1 FROM discord_day_summary_off AS off
                      WHERE off.account_id = link.account_id AND off.player_id = link.player_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM discord_day_summary_deliveries AS logged
                      WHERE logged.account_id = account.id AND logged.reset_at = %(reset)s
                  )
                GROUP BY account.id, identity.provider_subject
                ORDER BY account.id
                LIMIT %(batch)s
                """,
                {"reset": reset, "batch": BATCH},
            ).fetchall()
            # Each player's place on that board, and whether it went without
            # them as their day's result was not saved in time.
            standing = {
                tag: (position, unfinished)
                for tag, position, unfinished in connection.execute(
                    """
                    SELECT player.normalized_tag, entry.position, EXISTS (
                        SELECT 1 FROM boundary_publication_generations AS g
                        JOIN boundary_publication_generation_members AS member
                            ON member.generation_id = g.id
                        WHERE g.snapshot_id = %(board)s AND member.player_id = player.id
                          AND member.snapshot_status IN ('pending', 'unavailable', 'failed')
                    )
                    FROM players AS player
                    LEFT JOIN leaderboard_snapshot_entries AS entry
                        ON entry.snapshot_id = %(board)s AND entry.player_id = player.id
                    WHERE player.normalized_tag = ANY(%(tags)s::text[])
                    """,
                    {"board": board[0], "tags": [tag for _, _, tags in accounts for tag in tags]},
                ).fetchall()
            }
        due = []
        for account_id, discord_user_id, tags in accounts:
            try:
                days = [
                    day
                    for tag in tags
                    if (page := self.store.player_page(tag, now)) is not None
                    and (day := replies.summary_day(page, reset, *standing[tag])) is not None
                ]
            except Exception:
                # Unlogged, so the next round reads this account again.
                log.exception("Legend day summary for account %s could not be read", account_id)
                continue
            parts = replies.day_summary(self.site, reset, days)
            reply, named = parts[0] if parts else (replies.Reply(""), ())
            # A summary is for no one player, so it has no player id.
            delivery = Delivery(
                int(account_id), 0, str(discord_user_id), None, reset, reply,
                log=self, tags=named, more=parts[1:],
            )
            if parts:
                due.append(delivery)
            elif (skipped := self.claim(delivery)) is not None:
                self.finish(skipped, "skipped", None)
        return due

    def claim(self, delivery: Delivery) -> int | None:
        """Log the account's summary as being sent while it is still due for
        every player it names; its log id, or None when it is not due or was
        logged already."""
        named = delivery.tags + tuple(tag for _, tags in delivery.more for tag in tags)
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO discord_day_summary_deliveries (account_id, reset_at, state)
                SELECT account.id, %(reset)s, 'sending'
                """
                + _STILL_ON
                + """
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                self._params(delivery, named),
            ).fetchone()
        return None if row is None else int(row[0])

    def still_due(self, delivery: Delivery) -> bool:
        """Whether its DM now is still due for every player that DM names."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT EXISTS (SELECT 1" + _STILL_ON + ")",
                self._params(delivery, delivery.tags),
            ).fetchone()
        return bool(row and row[0])

    def finish(self, delivery_id: int, state: str, reason: str | None) -> None:
        with self.database.pool.connection() as connection:
            connection.execute(
                """
                UPDATE discord_day_summary_deliveries
                SET state = %s, reason = %s, finished_at = clock_timestamp()
                WHERE id = %s
                """,
                (state, reason, delivery_id),
            )

    def prune(self) -> None:
        with self.database.pool.connection() as connection:
            connection.execute(
                "DELETE FROM discord_day_summary_deliveries"
                " WHERE created_at < clock_timestamp() - %s",
                (KEEP,),
            )

    @staticmethod
    def _params(delivery: Delivery, tags: tuple[str, ...]) -> dict[str, Any]:
        return {
            "account_id": delivery.account_id,
            "discord_user_id": delivery.discord_user_id,
            "reset": delivery.since,
            "tags": list(tags),
        }
