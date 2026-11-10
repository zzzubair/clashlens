"""Shared setup for first battle log postgres tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import psycopg
from reconciliation_postgres_support import BATTLE_FIXTURE
from reset_settlement_state_postgres_support import (
    BOUNDARIES,
    NEW_SEASON,
    TAG,
    _season_profile,
)

from clashlens.domain import allocate_trophies

DAY_1 = BOUNDARIES["season"]


DAY_2 = DAY_1 + timedelta(days=1)


WIN = allocate_trophies(3, 100).attacker_gain


LOSS = allocate_trophies(2, 60).defender_loss


def _log(*battles: tuple[datetime, bool], filler: list[datetime] = ()) -> bytes:
    """A battle log: Legend battles (time, attack) won 3 stars as attacker and
    lost 2 stars at 60% as defender, plus multiplayer battles at ``filler``."""
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    items = [
        {
            **template, "attack": attack, "battleTime": 120,
            "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
            "stars": 3 if attack else 2, "destructionPercentage": 100 if attack else 60,
            "opponentPlayerTag": f"#{'89QGRJCUV'[index]}PP",
        }
        for index, (at, attack) in enumerate(battles)
    ] + [
        {**template, "battleType": "homeVillage", "battleTime": 120,
         "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
         "opponentPlayerTag": "#9PP"}
        for at in filler
    ]
    return json.dumps({"items": items}).encode()


def _new_season_profile(trophies: int, tag: str = TAG) -> bytes:
    payload = json.loads(_season_profile(trophies, NEW_SEASON))
    payload["tag"] = tag
    return json.dumps(payload).encode()


def _queued_priorities(connection_info: str, key_prefix: str) -> set[int]:
    with psycopg.connect(connection_info) as connection:
        return {
            row[0] for row in connection.execute(
                "SELECT DISTINCT priority FROM python_processing_jobs"
                " WHERE deduplication_key LIKE %s",
                (key_prefix + "%",),
            )
        }
