"""Shared setup for league history tests."""

from __future__ import annotations

import json


def _payload(*items: object) -> bytes:
    return json.dumps({"items": list(items)}).encode()


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "leagueSeasonId": "1781499600",
        "leagueTrophies": 5812,
        "leagueTierId": 105000036,
        "placement": 12,
        "attackWins": 6,
        "attackLosses": 2,
        "attackStars": 21,
        "defenseWins": 5,
        "defenseLosses": 3,
        "defenseStars": 18,
        "maxBattles": 8,
    }
    entry.update(overrides)
    return entry
