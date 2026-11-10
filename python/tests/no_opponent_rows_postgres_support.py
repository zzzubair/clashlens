"""Shared setup for no opponent rows postgres tests."""

from __future__ import annotations

# Live logs keep this row for days: no opponent, no battle.
NO_OPPONENT_ROW = {
    "battleType": "legend",
    "attack": False,
    "battleTime": 0,
    "battleTimestamp": "20260803T110000.000Z",
    "stars": 0,
    "destructionPercentage": 0,
    "opponentPlayerTag": None,
    "opponentName": None,
    "armyShareCode": None,
}
