"""Attack alerts: a Discord DM each time one of a Clasher's players is attacked.

Clashers turn alerts on per verified player on the website. Every few seconds
the loop sends a short confirmation DM for each switch turned on, or Discord
connected again, in the last 15 minutes, then asks the database for defenses
saved in the last 15 minutes against those players, after their alerts went
on, and sends each one once. Older ones are skipped, so a bot that was down
does not send a flood of stale alerts when it starts again.

Each DM is written to the delivery log just before it is first sent, and
only while the switch is still on, the player still verified to the account
and Discord still connected as the same user; every try sends the same
Discord nonce, which Discord checks for repeats for a few minutes. A DM
Discord did not take, whatever the cause (a 5xx or 429 answer, a network
error, a timeout, an answer cut off), is tried again, waiting longer each
time and at least as long as Discord asks, while those still hold, within
the 15 minutes, and only within 2 minutes of its first try, when it was
logged. Each try, with discord.py's own waits and tries again, is cut off at
that limit, by a fresh clock, and a try that could only start after it is
not made; the DM is then logged as not delivered, never sent again. A crash
can lose an alert but never send it twice. Discord refusing a DM (bot not
added, DMs closed, unknown user) is logged with the reason, which the
website shows with what the Clasher can do. That DM is not tried again.

A delivered alert gets its picture of the defender's Legend day (see
`picture`) once Clash Lens has saved its battle into that day, usually
seconds after the alert, looked for in each round after the one that sent
it, for 5 minutes. The
picture is added by editing the DM, which sends no second notification, so
the phone's notification shows the DM's text rather than "Sent an image". A
picture that cannot be drawn or added is left out; the alert stays. Pictures
are added for at most 10 seconds a round, reading, drawing and editing
included, and stop before the next try of a DM waiting to be tried again,
which the loop wakes for; a picture cut off then is tried again next round.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for

from . import picture, replies
from .replies import Reply, Site
from .store import Store, link_colours

log = logging.getLogger("clashlens.bot.alerts")

# Battles saved, or confirmations asked for, longer ago than this are never
# sent, nor tried again.
WINDOW = timedelta(minutes=15)
# How often the loop looks for new battles.
INTERVAL_SECONDS = 10.0
# The wait before trying again a DM Discord could not take, doubling with
# each try up to the longest.
RETRY_SECONDS = 10.0
RETRY_LONGEST_SECONDS = 120.0
# A DM Discord did not take is tried again only this long after its first
# try, while Discord's nonce check still stops a repeat.
NONCE_SECONDS = 120.0
# Delivery log rows are deleted after this; only the newest refusal is shown.
KEEP = timedelta(days=7)
# How long a delivered alert's picture waits for its battle to be saved into
# the defender's Legend day, and the longest an edit adding it may take.
PICTURE_SECONDS = 300.0
PICTURE_EDIT_SECONDS = 30.0
# Pictures are added in a round for at most this long, so the next look for
# new battles is not held up.
PICTURE_ROUND_SECONDS = 10.0

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
    "ranked_day_start",
)
_CONFIRM_COLUMNS = (
    "account_id",
    "discord_user_id",
    "player_id",
    "tag",
    "name",
    "confirm_requested_at",
)
# Where a DM is still due: its switch on since before the battle was saved, or
# still asking for this confirmation, the player still verified to the account,
# Discord still connected as the same user, and within `WINDOW`.
_STILL_DUE = """
    FROM discord_attack_alerts AS alert
    JOIN verified_player_links AS link
        ON link.player_id = alert.player_id AND link.account_id = alert.account_id
    JOIN account_provider_identities AS identity
        ON identity.account_id = alert.account_id AND identity.provider = 'discord'
    WHERE alert.account_id = %(account_id)s AND alert.player_id = %(player_id)s
      AND identity.provider_subject = %(discord_user_id)s
      AND CASE WHEN %(battle_id)s::bigint IS NULL
               THEN alert.confirm_requested_at = %(since)s
               ELSE alert.enabled_at < %(since)s END
      AND clock_timestamp() < %(since)s + %(window)s
"""


def _coloured(connection: Any, rows: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    """`rows` with the colour of each one's player, named by its `tag` column."""
    accounts = {
        account_id: link_colours(connection, account_id)
        for account_id in {row["account_id"] for row in rows}
    }
    return [
        {**row, "colour": accounts[row["account_id"]].get(row[tag], replies.GOLD)}
        for row in rows
    ]


class Refused(Exception):
    """Discord would not deliver the DM, for `reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Unreachable(Exception):
    """Discord did not take the DM, and may still. `retry_after` is how many
    seconds Discord asked to wait, if it did."""

    def __init__(self, retry_after: float = 0.0) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class Messenger(Protocol):
    async def send_dm(
        self, discord_user_id: str, reply: Reply, nonce: int | str, time_left: float
    ) -> Any:
        """Send the DM with `nonce`, which Discord checks for repeats, raising
        Unreachable at once rather than wait out a rate limit longer than
        `time_left` seconds; returns the message sent."""

    async def add_picture(self, message: Any, picture: bytes) -> None:
        """Put the PNG `picture` at the bottom of the box of `message`, a
        message `send_dm` returned."""


@dataclass
class Delivery:
    """One DM: for a battle, or confirming a switch when `battle_id` is None."""

    account_id: int
    player_id: int
    discord_user_id: str
    battle_id: int | None
    # When its `WINDOW` started: the battle saved, or the confirmation asked for.
    since: datetime
    reply: Reply
    # The attack, for an attack alert, whose picture is added once delivered.
    alert: dict[str, Any] | None = None
    # Its delivery log id once claimed, also the nonce of every try, and the
    # loop's clock time then, just before its first try.
    id: int = 0
    claimed_at: float = 0.0
    # The loop's clock time of the next try, and the wait before it.
    retry_at: float = 0.0
    wait: float = 0.0

    def params(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "player_id": self.player_id,
            "discord_user_id": self.discord_user_id,
            "battle_id": self.battle_id,
            "since": self.since,
            "window": WINDOW,
        }

    def time_left(self) -> float:
        """Seconds until its last moment, `NONCE_SECONDS` after its first try,
        by the loop's clock now."""
        return self.claimed_at + NONCE_SECONDS - asyncio.get_running_loop().time()


class AlertStore:
    """The alert reads and the delivery log, the only table the bot writes."""

    def __init__(self, database: ApiDatabase, store: Store) -> None:
        self.database = database
        self.store = store

    def due(self) -> list[dict[str, Any]]:
        """Alerts for battles saved within `WINDOW`, by the database's clock,
        the clock that stamps each battle as saved, each with the defending
        player's colour and whether its account chose a dark picture."""
        with self.database.pool.connection() as connection:
            rows = [
                dict(zip(_DUE_COLUMNS, row, strict=True))
                for row in connection.execute(
                    "SELECT * FROM clashlens_discord_attack_alerts_due(clock_timestamp() - %s)",
                    (WINDOW,),
                ).fetchall()
            ]
            if not rows:
                return []
            dark = {
                row[0]
                for row in connection.execute(
                    "SELECT account_id FROM discord_alert_dark_picture WHERE account_id = ANY(%s)",
                    (sorted({row["account_id"] for row in rows}),),
                ).fetchall()
            }
            return _coloured(
                connection,
                [{**row, "dark_picture": row["account_id"] in dark} for row in rows],
                "defender_tag",
            )

    def confirmations_due(self) -> list[dict[str, Any]]:
        """Confirmations asked for within `WINDOW` and unsent, for accounts
        with Discord connected, each with the player's colour."""
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT alert.account_id, identity.provider_subject, alert.player_id,
                       player.normalized_tag, profile.name, alert.confirm_requested_at
                FROM discord_attack_alerts AS alert
                JOIN verified_player_links AS link
                    ON link.player_id = alert.player_id AND link.account_id = alert.account_id
                JOIN account_provider_identities AS identity
                    ON identity.account_id = alert.account_id AND identity.provider = 'discord'
                JOIN players AS player ON player.id = alert.player_id
                LEFT JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE alert.confirm_requested_at >= clock_timestamp() - %s
                  AND NOT EXISTS (
                      SELECT 1 FROM discord_attack_alert_deliveries AS sent
                      WHERE sent.account_id = alert.account_id
                        AND sent.player_id = alert.player_id
                        AND sent.confirms = alert.confirm_requested_at
                  )
                ORDER BY alert.confirm_requested_at
                """,
                (WINDOW,),
            ).fetchall()
            return _coloured(
                connection, [dict(zip(_CONFIRM_COLUMNS, row, strict=True)) for row in rows],
                "tag",
            )

    def attacker(self, tag: str, battle_day: datetime, now: datetime) -> dict[str, Any]:
        """The player's clan, trophies at the start of the battle's Legend
        day, live rank and hit rate in the Season of a battle on the Legend
        day starting `battle_day`, so far at `now`, or at that Season's end
        once it is over, read as /stats reads it. Each is None when unknown;
        the hit rate is "not tracked yet" when Clash Lens has saved none of
        their days, "Unavailable" when those it has saved cannot give it."""
        season = ranked_day_for(battle_day)
        at = min(max(now, season.season_start), season.season_end - timedelta(microseconds=1))
        page = self.store.player_page(tag, at) or {}
        found = {"clan": page.get("clan"), "rank": page.get("rank"), "start_trophies": None}
        if not page.get("screen_ready", {}).get("days"):
            return {**found, "hit_rate": "not tracked yet"}
        found["start_trophies"] = next(
            (
                day.get("start_trophies")
                for day in page["screen_ready"]["days"]
                if datetime.fromisoformat(day["ranked_day_start"]) == battle_day
            ),
            None,
        )
        days = replies.season_days(page)
        return {**found, "hit_rate": "Unavailable" if days is None else replies.hit_rate(days)}

    def legend_day(self, alert: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """The defender's Legend day of the alert's battle, read as the
        player page reads it, for the alert's picture; None until Clash Lens
        has saved that battle into the day."""
        page = self.store.player_page(alert["defender_tag"], now) or {}
        day = picture.legend_day(page, alert["ranked_day_start"], str(alert["battle_id"]))
        if day is None or not any(defense["new"] for defense in day["defenses"]):
            return None
        return day

    def claim(self, delivery: Delivery) -> int | None:
        """Log a DM as being sent while it is still due; returns its log id,
        or None when it is not due or was logged already."""
        with self.database.pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO discord_attack_alert_deliveries
                    (account_id, player_id, battle_id, confirms, state)
                SELECT alert.account_id, alert.player_id, %(battle_id)s::bigint,
                       CASE WHEN %(battle_id)s::bigint IS NULL THEN %(since)s END,
                       'sending'
                """
                + _STILL_DUE
                + """
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                delivery.params(),
            ).fetchone()
        return None if row is None else int(row[0])

    def still_due(self, delivery: Delivery) -> bool:
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT EXISTS (SELECT 1" + _STILL_DUE + ")", delivery.params()
            ).fetchone()
        return bool(row and row[0])

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
        # How far into the battle's Season the attacker's details go, and
        # when the defender's Legend day is read for the picture.
        self.now = now
        self._pruned_at: float | None = None
        # Claimed DMs Discord could not take yet, waiting for their next try.
        self._waiting: list[Delivery] = []
        # Delivered alerts waiting for their picture: the message, the
        # attack, and the loop's clock time to stop waiting.
        self._pictures: list[tuple[Any, dict[str, Any], float]] = []
        # Clear while a picture's read of its Legend day, cut off by the end
        # of its round, still runs; no other picture is read until it ends.
        self._read = threading.Event()
        self._read.set()

    async def run(self) -> None:
        """Look for new battles until cancelled, every `INTERVAL_SECONDS` or
        at the next try of a DM waiting to be tried again if sooner; a
        failed round is logged and the next round tries again."""
        while True:
            pause = INTERVAL_SECONDS
            try:
                await self.tick()
                now = asyncio.get_running_loop().time()
                pause = max(0.0, min([pause, *(d.retry_at - now for d in self._waiting)]))
            except Exception:
                log.exception("attack alert round failed")
            await asyncio.sleep(pause)

    async def tick(self) -> int:
        """Log as not delivered each waiting DM past its last try, and try
        again each other one whose time has come, then send every
        confirmation, then every alert, due now, one at a time; returns how
        many were sent. Then add the picture to each alert delivered in an
        earlier round whose battle is now saved into its Legend day, oldest
        first, until the round's end (`_add_pictures`)."""
        clock = asyncio.get_running_loop().time()
        pictures = list(self._pictures)
        if self._pruned_at is None or clock - self._pruned_at >= 3600:
            await asyncio.to_thread(self.alerts.prune)
            self._pruned_at = clock
        sent = 0
        for delivery in list(self._waiting):
            expired = delivery.time_left() <= 0
            if not expired and delivery.retry_at > asyncio.get_running_loop().time():
                continue
            if expired or not await asyncio.to_thread(self.alerts.still_due, delivery):
                await self._finish(delivery, "failed", None)
                self._waiting.remove(delivery)
            else:
                self._waiting.remove(delivery)
                sent += await self._send(delivery)
        for switch in await asyncio.to_thread(self.alerts.confirmations_due):
            sent += await self._claim(
                Delivery(
                    switch["account_id"],
                    switch["player_id"],
                    switch["discord_user_id"],
                    None,
                    switch["confirm_requested_at"],
                    replies.alerts_on(self.site, switch, switch["colour"]),
                )
            )
        for alert in await asyncio.to_thread(self.alerts.due):
            attacker = await asyncio.to_thread(
                self.alerts.attacker, alert["attacker_tag"], alert["ranked_day_start"], self.now()
            )
            sent += await self._claim(
                Delivery(
                    alert["account_id"],
                    alert["defender_player_id"],
                    alert["discord_user_id"],
                    alert["battle_id"],
                    alert["saved_at"],
                    replies.attack_alert(self.site, alert, attacker, alert["colour"]),
                    alert,
                )
            )
        await self._add_pictures(pictures)
        return sent

    async def _add_pictures(
        self, pictures: list[tuple[Any, dict[str, Any], float]]
    ) -> None:
        """Add each of `pictures` whose battle is saved into its Legend day,
        until the round's end: `PICTURE_ROUND_SECONDS` from now, or the next
        try of a DM waiting to be tried again if sooner. The end cuts off the
        read, the drawing and the edit alike; a picture cut off, not reached,
        or waiting for its read or drawing to have room, is tried again next
        round, until it stops waiting. One that fails is left out."""
        time = asyncio.get_running_loop().time
        end = min([time() + PICTURE_ROUND_SECONDS, *(d.retry_at for d in self._waiting)])
        for waiting in pictures:
            message, alert, give_up_at = waiting
            if time() >= end or not self._read.is_set():
                return
            if time() < give_up_at:
                try:
                    async with asyncio.timeout_at(end) as cut:
                        if not await self._add_picture(message, alert):
                            continue
                except picture.Busy:
                    return
                except TimeoutError:
                    if cut.expired():
                        log.info("attack alert picture for battle %s waits for next round",
                                 alert["battle_id"])
                        return
                    log.exception("attack alert picture for battle %s failed", alert["battle_id"])
                except Exception:
                    log.exception("attack alert picture for battle %s failed", alert["battle_id"])
            self._pictures.remove(waiting)

    async def _add_picture(self, message: Any, alert: dict[str, Any]) -> bool:
        """Read the alert's Legend day, draw it and edit it into `message`;
        False while its battle is not saved into the day yet."""
        now, read = self.now(), threading.Event()
        self._read = read

        def legend_day() -> dict[str, Any] | None:
            try:
                return self.alerts.legend_day(alert, now)
            finally:
                read.set()

        day = await asyncio.to_thread(legend_day)
        if day is None:
            return False
        drawn = await asyncio.wrap_future(picture.drawing(day, alert["dark_picture"]))
        await asyncio.wait_for(self.messenger.add_picture(message, drawn), PICTURE_EDIT_SECONDS)
        return True

    async def _claim(self, delivery: Delivery) -> int:
        claimed = await asyncio.to_thread(self.alerts.claim, delivery)
        if claimed is None:
            return 0
        delivery.id = claimed
        delivery.claimed_at = asyncio.get_running_loop().time()
        return await self._send(delivery)

    async def _send(self, delivery: Delivery) -> int:
        """Try one logged DM, cut off at its last moment, and log how it went
        or wait to try again if there is time; 1 when it was delivered."""
        left = delivery.time_left()
        if left <= 0:
            await self._finish(delivery, "failed", None)
            return 0
        state, reason = "sent", None
        try:
            message = await asyncio.wait_for(
                self.messenger.send_dm(
                    delivery.discord_user_id, delivery.reply, delivery.id, left
                ),
                left,
            )
        except Refused as refusal:
            state, reason = "refused", refusal.reason
        except Unreachable as outage:
            delivery.wait = min(max(2 * delivery.wait, RETRY_SECONDS), RETRY_LONGEST_SECONDS)
            wait = max(delivery.wait, outage.retry_after)
            if wait < delivery.time_left():
                delivery.retry_at = asyncio.get_running_loop().time() + wait
                self._waiting.append(delivery)
                log.info("attack alert delivery=%s waiting to try again", delivery.id)
                return 0
            state = "failed"
        except TimeoutError:
            state = "failed"
        except Exception:
            log.exception("attack alert delivery %s failed", delivery.id)
            state = "failed"
        await self._finish(delivery, state, reason)
        if state == "sent" and delivery.alert is not None:
            self._pictures.append(
                (message, delivery.alert, asyncio.get_running_loop().time() + PICTURE_SECONDS)
            )
        return int(state == "sent")

    async def _finish(self, delivery: Delivery, state: str, reason: str | None) -> None:
        await asyncio.to_thread(self.alerts.finish, delivery.id, state, reason)
        log.info("attack alert delivery=%s outcome=%s", delivery.id, reason or state)
