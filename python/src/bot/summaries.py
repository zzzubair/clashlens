"""Legend day summaries: one Discord DM a Legend day for each Clasher with
Discord connected, summing up the day just ended for each of their verified
players with the summary turned on.

A player's summary is off until the Clasher turns it on on the website's
Discord alerts page, separately from its attack alerts; each player turned
on has a discord_day_summary_on row (migration 0112).
Nothing is read for a Reset until its Daily board is published, so the
summaries never compete with the Reset's own work, and a late board only
makes them late.
They go out only during the Legend day that Reset starts: no summary is
logged, nor any of its DMs tried, from the next Reset, and a bot that was
down for a whole day skips that day's summaries.

Each player's day is read with the website's own player page read, so the
numbers and the label (Verified, Calculated, Uncertain) are the ones their
page shows; the end of day is the one the worker saved with that version of
the day, as its Season page shows it, before any weekly or Season reset,
which is shown beside it, and unavailable when the worker saved none. Every
place in a round's summaries is read from one version of the Reset's Daily
board, and each place moved from that player's place on the Daily board of
the Reset before, unless the day was a Season's first. A player whose day
is not saved yet, or whom that board went without, is named as not
finished; one with no Legend day that day is left out, and a Clasher with
no finished day gets no DM.

Summaries are sent by their own loop, beside the attack alert loop and
through the same Discord messenger, so a slow summary never holds up an
attack alert. Each summary is logged before its first DM, so no Clasher is
sent the same day's summary twice, across restarts too. A summary too long
for one DM goes as several, in order, each sent as soon as the one before is
delivered, with its own nonce. Each DM Discord did not take is tried again
as an attack alert DM is, within 2 minutes of its own first try. A summary
is logged only while every player it names is still verified to the account
with its summary on, else it is read again next round; each later DM, and
each try again, goes only while every player that DM names still is, else
it and the DMs after it are dropped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for

from . import replies
from .alerts import (
    KEEP,
    NONCE_SECONDS,
    RETRY_LONGEST_SECONDS,
    RETRY_SECONDS,
    Messenger,
    Refused,
    Unreachable,
)
from .replies import Reply, Site
from .store import Store, link_colours

log = logging.getLogger("clashlens.bot.summaries")

# Accounts read in one round.
BATCH = 10
# How often the loop looks for summaries due.
SUMMARY_SECONDS = 60.0

# Where an account's summary, or one of its DMs, is still due: before the
# Reset closing its Legend day, Discord still connected as the same user, and
# every player in %(tags)s still verified to the account with its summary on.
_STILL_ON = """
    FROM clash_lens_accounts AS account
    JOIN account_provider_identities AS identity
        ON identity.account_id = account.id AND identity.provider = 'discord'
    WHERE account.id = %(account_id)s AND identity.provider_subject = %(discord_user_id)s
      AND %(now)s < %(until)s
      AND NOT EXISTS (
          SELECT 1 FROM unnest(%(tags)s::text[]) AS named (tag)
          WHERE NOT EXISTS (
              SELECT 1 FROM verified_player_links AS link
              JOIN players AS player ON player.id = link.player_id
              JOIN discord_day_summary_on AS summary_on
                  ON summary_on.account_id = link.account_id
                 AND summary_on.player_id = link.player_id
              WHERE link.account_id = account.id AND player.normalized_tag = named.tag
          )
      )
"""


@dataclass
class Summary:
    """One account's summary of the Legend day that ended at `reset`: its DMs
    in order, each with the tags of the players it names."""

    account_id: int
    discord_user_id: str
    reset: datetime
    parts: list[tuple[Reply, tuple[str, ...]]]
    # Its delivery log id once claimed.
    id: int = 0

    @property
    def until(self) -> datetime:
        """The next Reset, from which it is no longer due."""
        return self.reset + timedelta(days=1)


def _board(connection: Any, reset: datetime) -> int | None:
    """The id of the Reset's published Daily board, as the player page finds
    it (season_summaries.reset_board_ranks); None before it is published."""
    row = connection.execute(
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
    return None if row is None else int(row[0])


class Summaries:
    """The summary reads and the summary delivery log."""

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

    def due(self) -> list[Summary]:
        """Summaries of the Legend day the latest Reset ended, for up to
        `BATCH` accounts not yet logged for it, once its Daily board is
        published. An account with no finished day is logged as skipped."""
        now = self.now()
        reset = ranked_day_for(now).start
        with self.database.pool.connection() as connection:
            board = _board(connection, reset)
            if board is None:
                return []
            # The Daily board of the Reset before, for each place moved.
            previous = _board(connection, reset - timedelta(days=1))
            accounts = connection.execute(
                """
                SELECT account.id, identity.provider_subject,
                       array_agg(player.normalized_tag ORDER BY player.normalized_tag)
                FROM clash_lens_accounts AS account
                JOIN account_provider_identities AS identity
                    ON identity.account_id = account.id AND identity.provider = 'discord'
                JOIN verified_player_links AS link ON link.account_id = account.id
                JOIN discord_day_summary_on AS summary_on
                    ON summary_on.account_id = link.account_id
                   AND summary_on.player_id = link.player_id
                JOIN players AS player ON player.id = link.player_id
                WHERE NOT EXISTS (
                      SELECT 1 FROM discord_day_summary_deliveries AS logged
                      WHERE logged.account_id = account.id AND logged.reset_at = %(reset)s
                  )
                GROUP BY account.id, identity.provider_subject
                ORDER BY account.id
                LIMIT %(batch)s
                """,
                {"reset": reset, "batch": BATCH},
            ).fetchall()
            # Each player's place on that board, whether it went without them
            # as their day's result was not saved in time, and their place on
            # the board before.
            standing = {
                tag: (position, unfinished, earlier)
                for tag, position, unfinished, earlier in connection.execute(
                    """
                    SELECT player.normalized_tag, entry.position, EXISTS (
                        SELECT 1 FROM boundary_publication_generations AS g
                        JOIN boundary_publication_generation_members AS member
                            ON member.generation_id = g.id
                        WHERE g.snapshot_id = %(board)s AND member.player_id = player.id
                          AND member.snapshot_status IN ('pending', 'unavailable', 'failed')
                    ), earlier.position
                    FROM players AS player
                    LEFT JOIN leaderboard_snapshot_entries AS entry
                        ON entry.snapshot_id = %(board)s AND entry.player_id = player.id
                    LEFT JOIN leaderboard_snapshot_entries AS earlier
                        ON earlier.snapshot_id = %(previous)s AND earlier.player_id = player.id
                    WHERE player.normalized_tag = ANY(%(tags)s::text[])
                    """,
                    {"board": board, "previous": previous,
                     "tags": [tag for _, _, tags in accounts for tag in tags]},
                ).fetchall()
            }
        due = []
        for account_id, discord_user_id, tags in accounts:
            try:
                with self.database.pool.connection() as connection:
                    colours = link_colours(connection, account_id)
                days = [
                    day
                    for tag in tags
                    if (page := self.store.player_page(tag, now)) is not None
                    and (day := replies.summary_day(page, reset, *standing[tag])) is not None
                ]
                self._saved_ends(reset, days)
            except Exception:
                # Unlogged, so the next round reads this account again.
                log.exception("Legend day summary for account %s could not be read", account_id)
                continue
            summary = Summary(
                int(account_id), str(discord_user_id), reset,
                replies.day_summary(self.site, reset, days, colours),
            )
            if summary.parts:
                due.append(summary)
            elif (skipped := self.claim(summary)) is not None:
                self.finish(skipped, "skipped", None)
        return due

    def _saved_ends(self, reset: datetime, days: list[dict[str, Any]]) -> None:
        """Set each finished day's end of day to the one the worker saved
        with the version of the day its page showed; None when it saved none."""
        finished = {(day["tag"], day["version"]): day for day in days if day["finished"]}
        if not finished:
            return
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT player.normalized_tag, daily.version, ranked.final_trophies_before_reset
                FROM api_player_daily_logs AS daily
                JOIN players AS player ON player.id = daily.player_id
                JOIN ranked_day_versions AS ranked ON ranked.id = daily.ranked_day_version_id
                WHERE player.normalized_tag = ANY(%s::text[]) AND daily.ranked_day_start = %s
                """,
                ([tag for tag, _ in finished], reset - timedelta(days=1)),
            ).fetchall()
        for tag, version, end in rows:
            if (day := finished.get((tag, version))) is not None:
                day["end"] = end

    def claim(self, summary: Summary) -> int | None:
        """Log the account's summary as being sent while it is still due for
        every player it names; its log id, or None when it is not due or was
        logged already."""
        named = tuple(tag for _, tags in summary.parts for tag in tags)
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
                self._params(summary, named),
            ).fetchone()
        return None if row is None else int(row[0])

    def still_due(self, summary: Summary, tags: tuple[str, ...]) -> bool:
        """Whether a DM of the summary naming `tags` is still due now."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT EXISTS (SELECT 1" + _STILL_ON + ")", self._params(summary, tags)
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

    def _params(self, summary: Summary, tags: tuple[str, ...]) -> dict[str, Any]:
        return {
            "account_id": summary.account_id,
            "discord_user_id": summary.discord_user_id,
            "reset": summary.reset,
            "now": self.now(),
            "until": summary.until,
            "tags": list(tags),
        }


class SummaryLoop:
    """Sends the summaries due, apart from the attack alert loop."""

    def __init__(self, summaries: Summaries, messenger: Messenger) -> None:
        self.summaries = summaries
        self.messenger = messenger
        self._pruned_at: float | None = None

    async def run(self) -> None:
        """Look for summaries due until cancelled; a failed round is logged
        and the next round tries again."""
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Legend day summary round failed")
            await asyncio.sleep(SUMMARY_SECONDS)

    async def tick(self) -> int:
        """Send each summary due now, one at a time; the DMs delivered."""
        clock = asyncio.get_running_loop().time()
        if self._pruned_at is None or clock - self._pruned_at >= 3600:
            await asyncio.to_thread(self.summaries.prune)
            self._pruned_at = clock
        sent = 0
        for summary in await asyncio.to_thread(self.summaries.due):
            sent += await self.send(summary)
        return sent

    async def send(self, summary: Summary) -> int:
        """Log the summary, then send its DMs in order, each later one only
        while still due for the players it names, and log how it went; the
        DMs delivered."""
        claimed = await asyncio.to_thread(self.summaries.claim, summary)
        if claimed is None:
            return 0
        summary.id = claimed
        sent, state = 0, "sent"
        reason: str | None = None
        for part, (reply, tags) in enumerate(summary.parts):
            if part and not await asyncio.to_thread(self.summaries.still_due, summary, tags):
                state = "failed"
                break
            state, reason = await self._deliver(summary, part, reply, tags)
            if state != "sent":
                break
            sent += 1
        await asyncio.to_thread(self.summaries.finish, summary.id, state, reason)
        log.info("summary delivery=%s outcome=%s", summary.id, reason or state)
        return sent

    async def _deliver(
        self, summary: Summary, part: int, reply: Reply, tags: tuple[str, ...]
    ) -> tuple[str, str | None]:
        """Try one DM until Discord takes it, cut off within `NONCE_SECONDS`
        of its first try and at the next Reset, trying again after a wait
        while it is still due; how it went."""
        clock = asyncio.get_running_loop()
        last = clock.time() + NONCE_SECONDS

        def left() -> float:
            return min(last - clock.time(), (summary.until - self.summaries.now()).total_seconds())

        wait = 0.0
        while (time_left := left()) > 0:
            try:
                await asyncio.wait_for(
                    self.messenger.send_dm(
                        summary.discord_user_id, reply, f"summary-{summary.id}-{part}", time_left
                    ),
                    time_left,
                )
                return "sent", None
            except Refused as refusal:
                return "refused", refusal.reason
            except Unreachable as outage:
                wait = min(max(2 * wait, RETRY_SECONDS), RETRY_LONGEST_SECONDS)
                pause = max(wait, outage.retry_after)
                if pause >= left():
                    return "failed", None
                log.info("summary delivery=%s part=%s waiting to try again", summary.id, part)
                await asyncio.sleep(pause)
                if not await asyncio.to_thread(self.summaries.still_due, summary, tags):
                    return "failed", None
            except TimeoutError:
                return "failed", None
            except Exception:
                log.exception("summary delivery %s part %s failed", summary.id, part)
                return "failed", None
        return "failed", None
