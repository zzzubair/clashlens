"""Attack alerts: a Discord DM each time one of a Clasher's players is attacked.

Clashers turn alerts on per verified player on the website. Every few seconds
the loop sends a short confirmation DM for each switch turned on in the last
15 minutes, then asks the database for defenses saved in the last 15 minutes
against those players, after their alerts went on, and sends each one once.
Older ones are skipped, so a bot that was down does not send a flood of stale
alerts when it starts again.

Each DM is written to the delivery log before it is sent and never sent
again, so a crash or a timeout can lose an alert but never send it twice.
Discord refusing a DM (bot not added, DMs closed, unknown user) is logged
with the reason, which the website shows with what the Clasher can do. That
DM is not tried again.
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

# Battles saved, or switches turned on, longer ago than this are never sent.
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
_CONFIRM_COLUMNS = (
    "account_id",
    "discord_user_id",
    "player_id",
    "tag",
    "name",
    "enabled_at",
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

    def confirmations_due(self) -> list[dict[str, Any]]:
        """Switches turned on within `WINDOW` whose confirmation is unsent,
        for accounts with Discord connected."""
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT alert.account_id, identity.provider_subject, alert.player_id,
                       player.normalized_tag, profile.name, alert.enabled_at
                FROM discord_attack_alerts AS alert
                JOIN verified_player_links AS link
                    ON link.player_id = alert.player_id AND link.account_id = alert.account_id
                JOIN account_provider_identities AS identity
                    ON identity.account_id = alert.account_id AND identity.provider = 'discord'
                JOIN players AS player ON player.id = alert.player_id
                LEFT JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE alert.enabled_at >= clock_timestamp() - %s
                  AND NOT EXISTS (
                      SELECT 1 FROM discord_attack_alert_deliveries AS sent
                      WHERE sent.account_id = alert.account_id
                        AND sent.player_id = alert.player_id
                        AND sent.confirms = alert.enabled_at
                  )
                ORDER BY alert.enabled_at
                """,
                (WINDOW,),
            ).fetchall()
        return [dict(zip(_CONFIRM_COLUMNS, row, strict=True)) for row in rows]

    def hit_rate(self, tag: str, now: datetime) -> str:
        """The player's hit rate this Season, read as /stats reads it."""
        page = self.store.player_page(tag, now)
        days = (
            None
            if page is None or "screen_ready" not in page
            else replies.season_days(page)
        )
        return "Unavailable" if days is None else replies.hit_rate(days)

    def claim(
        self,
        account_id: int,
        player_id: int,
        *,
        battle_id: int | None = None,
        confirms: datetime | None = None,
    ) -> int | None:
        """Log a DM, for a battle or confirming a switch, as being sent;
        returns its log id, or None when it was logged already."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO discord_attack_alert_deliveries
                    (account_id, player_id, battle_id, confirms, state)
                VALUES (%s, %s, %s, %s, 'sending')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (account_id, player_id, battle_id, confirms),
            ).fetchone()
        return None if row is None else int(row[0])

    def finish(self, delivery_id: int, state: str, reason: str | None) -> None:
        with self.database.pool.connection() as connection:
            connection.execute(
                """
                UPDATE discord_attack_alert_deliveries
                SET state = %s, reason = %s, finished_at = clock_timestamp()
                WHERE id = %s
                """,
                (state, reason, delivery_id),
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
        """Send every confirmation, then every alert, due now, one at a time;
        returns how many were sent."""
        now = self.now()
        clock = asyncio.get_running_loop().time()
        if self._pruned_at is None or clock - self._pruned_at >= 3600:
            await asyncio.to_thread(self.alerts.prune)
            self._pruned_at = clock
        sent = 0
        for switch in await asyncio.to_thread(self.alerts.confirmations_due):
            delivery = await asyncio.to_thread(
                self.alerts.claim,
                switch["account_id"],
                switch["player_id"],
                confirms=switch["enabled_at"],
            )
            if delivery is not None:
                reply = replies.alerts_on(self.site, switch)
                sent += await self._send(delivery, switch["discord_user_id"], reply)
        for alert in await asyncio.to_thread(self.alerts.due):
            rate = await asyncio.to_thread(
                self.alerts.hit_rate, alert["attacker_tag"], now
            )
            reply = replies.attack_alert(self.site, alert, rate)
            delivery = await asyncio.to_thread(
                self.alerts.claim,
                alert["account_id"],
                alert["defender_player_id"],
                battle_id=alert["battle_id"],
            )
            if delivery is not None:
                sent += await self._send(delivery, alert["discord_user_id"], reply)
        return sent

    async def _send(self, delivery: int, discord_user_id: str, reply: Reply) -> int:
        """Send one logged DM and log how it went; 1 when it was delivered."""
        state, reason = "sent", None
        try:
            await self.messenger.send_dm(discord_user_id, reply)
        except Refused as refusal:
            state, reason = "refused", refusal.reason
        except Unreachable:
            state = "failed"
        except Exception:
            log.exception("attack alert delivery %s failed", delivery)
            state = "failed"
        await asyncio.to_thread(self.alerts.finish, delivery, state, reason)
        log.info("attack alert delivery=%s outcome=%s", delivery, reason or state)
        return int(state == "sent")
