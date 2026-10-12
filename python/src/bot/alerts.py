"""Attack alerts: a Discord DM each time one of a Clasher's players is attacked.

Clashers turn alerts on per verified player on the website. Every few seconds
the loop asks the database for defenses saved in the last 15 minutes against
those players, after their alerts went on, and sends each one once. Older
battles are skipped, so a bot that was down does not send a flood of stale
alerts when it starts again.

Each alert is written to the delivery log before it is sent and never sent
again, so a crash or a timeout can lose an alert but never send it twice.
Discord refusing a DM (no shared server, DMs closed, unknown user) is logged
with the reason, which the website shows with what the Clasher can do. That
battle is not tried again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from clashlens.api_db import ApiDatabase

from . import replies
from .replies import Reply, Site
from .store import Store

log = logging.getLogger("clashlens.bot.alerts")

# Battles saved longer ago than this are never sent.
WINDOW = timedelta(minutes=15)
# How often the loop looks for new battles.
INTERVAL_SECONDS = 10.0
# Delivery log rows are deleted after this; only the newest refusal is shown.
KEEP = timedelta(days=7)

_DUE_COLUMNS = (
    "account_id",
    "discord_user_id",
    "battle_id",
    "defender_player_id",
    "defender_tag",
    "defender_name",
    "attacker_tag",
    "attacker_name",
    "stars",
    "destruction_percentage",
    "trophies_lost",
    "saved_at",
)


class Refused(Exception):
    """Discord would not deliver the DM, for `reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Unreachable(Exception):
    """Discord could not be reached in time; the DM may or may not have gone."""


class Messenger(Protocol):
    async def send_dm(self, discord_user_id: str, reply: Reply) -> None: ...


class AlertStore:
    """The alert reads and the delivery log, the only table the bot writes."""

    def __init__(self, database: ApiDatabase, store: Store) -> None:
        self.database = database
        self.store = store

    def due(self) -> list[dict[str, Any]]:
        """Alerts for battles saved within `WINDOW`, by the database's clock,
        the clock that stamps each battle as saved."""
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM clashlens_discord_attack_alerts_due(clock_timestamp() - %s)",
                (WINDOW,),
            ).fetchall()
        return [dict(zip(_DUE_COLUMNS, row, strict=True)) for row in rows]

    def hit_rate(self, tag: str, now: datetime) -> str:
        """The player's hit rate this Season, read as /stats reads it."""
        page = self.store.player_page(tag, now)
        days = None if page is None or "screen_ready" not in page else replies.season_days(page)
        return "Unavailable" if days is None else replies.hit_rate(days)

    def claim(self, alert: dict[str, Any]) -> bool:
        """Log the alert as being sent; False when it was logged already."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO discord_attack_alert_deliveries
                    (account_id, battle_id, player_id, state)
                VALUES (%s, %s, %s, 'sending')
                ON CONFLICT (account_id, battle_id) DO NOTHING
                RETURNING battle_id
                """,
                (alert["account_id"], alert["battle_id"], alert["defender_player_id"]),
            ).fetchone()
        return row is not None

    def finish(self, alert: dict[str, Any], state: str, reason: str | None) -> None:
        with self.database.pool.connection() as connection:
            connection.execute(
                """
                UPDATE discord_attack_alert_deliveries
                SET state = %s, reason = %s, finished_at = clock_timestamp()
                WHERE account_id = %s AND battle_id = %s
                """,
                (state, reason, alert["account_id"], alert["battle_id"]),
            )

    def prune(self) -> None:
        with self.database.pool.connection() as connection:
            connection.execute(
                "DELETE FROM discord_attack_alert_deliveries"
                " WHERE created_at < clock_timestamp() - %s",
                (KEEP,),
            )


class AlertLoop:
    def __init__(
        self,
        alerts: AlertStore,
        messenger: Messenger,
        site: Site,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.alerts = alerts
        self.messenger = messenger
        self.site = site
        # Which Season the attacker's hit rate covers.
        self.now = now
        self._pruned_at: float | None = None

    async def run(self) -> None:
        """Look for new battles until cancelled; a failed round is logged and
        the next round tries again."""
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("attack alert round failed")
            await asyncio.sleep(INTERVAL_SECONDS)

    async def tick(self) -> int:
        """Send every alert due now, one at a time; returns how many were sent."""
        now = self.now()
        clock = asyncio.get_running_loop().time()
        if self._pruned_at is None or clock - self._pruned_at >= 3600:
            await asyncio.to_thread(self.alerts.prune)
            self._pruned_at = clock
        sent = 0
        for alert in await asyncio.to_thread(self.alerts.due):
            rate = await asyncio.to_thread(self.alerts.hit_rate, alert["attacker_tag"], now)
            reply = replies.attack_alert(self.site, alert, rate)
            if not await asyncio.to_thread(self.alerts.claim, alert):
                continue
            state, reason = "sent", None
            try:
                await self.messenger.send_dm(alert["discord_user_id"], reply)
                sent += 1
            except Refused as refusal:
                state, reason = "refused", refusal.reason
            except Unreachable:
                state = "failed"
            except Exception:
                log.exception("attack alert for battle %s failed", alert["battle_id"])
                state = "failed"
            await asyncio.to_thread(self.alerts.finish, alert, state, reason)
            log.info("attack alert battle=%s outcome=%s", alert["battle_id"], reason or state)
        return sent
