"""Shared setup for discord alerts postgres tests."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import psycopg
from discord_bot_postgres_support import DISCORD_ID
from domain_test_support import seed_battles
from reset_scenarios_postgres_support import (
    SEASON,
)

from bot.alerts import Refused, Unreachable
from bot.replies import Reply, Site
from clashlens import api_discord_alerts
from clashlens.api_db import ApiDatabase
from clashlens.api_requests import RequestBinding

SITE = Site("https://clashlens.test")


class FakeDiscord:
    """Records each try by its nonce, each DM delivered, which is known by
    its nonce, and each picture added to one; refuses users in `refusing` as
    Discord would, and cannot be reached for the next `outages` tries, asking
    to wait `retry_after`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Reply]] = []
        self.pictures: list[tuple[int, bytes]] = []
        self.tries: list[int] = []
        self.refusing: dict[str, str] = {}
        self.outages = 0
        self.retry_after = 0.0

    async def send_dm(
        self, discord_user_id: str, reply: Reply, nonce: int, _time_left: float
    ) -> int:
        self.tries.append(nonce)
        if discord_user_id in self.refusing:
            raise Refused(self.refusing[discord_user_id])
        if self.outages:
            self.outages -= 1
            raise Unreachable(self.retry_after)
        self.sent.append((discord_user_id, reply))
        return nonce

    async def add_picture(self, message: int, png: bytes) -> None:
        self.pictures.append((message, png))


def _switch(database: ApiDatabase, account_id: int, tag: str, on: bool) -> int:
    """Turn alerts on or off as the website does, through the private API."""
    return api_discord_alerts.set_attack_alert(
        database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="discord",
            provider_subject=DISCORD_ID,
            account_id=account_id,
            operation="account.update",
            method="POST",
            request_target="/v1/account/discord-alerts",
            identity={"attack_alert": tag, "on": on},
        ),
        normalized_tag=tag,
        on=on,
    ).status_code


def _summary_switch(database: ApiDatabase, account_id: int, tag: str, on: bool) -> int:
    """Turn a player's Legend day summary on or off as the website does,
    through the private API."""
    return api_discord_alerts.set_day_summary(
        database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="discord",
            provider_subject=DISCORD_ID,
            account_id=account_id,
            operation="account.update",
            method="POST",
            request_target="/v1/account/discord-alerts",
            identity={"day_summary": tag, "on": on},
        ),
        normalized_tag=tag,
        on=on,
    ).status_code


def _defend(connection_info: str, player_id: int, *, saved_ago: timedelta = timedelta(0)) -> None:
    """A defense of the player by a new untracked opponent, saved `saved_ago` ago."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        seed_battles(connection, player_id, [(SEASON + timedelta(hours=3), -16)])
        connection.execute(
            "UPDATE legend_battles SET created_at = clock_timestamp() - %s"
            " WHERE id = (SELECT max(id) FROM legend_battles)",
            (saved_ago,),
        )
