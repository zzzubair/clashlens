"""Shared setup for bookkeeping storage postgres tests."""

from __future__ import annotations

from datetime import timedelta

from reconciliation_postgres_support import (
    DAY_END,
)

from clashlens import api_leaderboard, api_players
from clashlens.api_db import ApiDatabase

READ_AT = DAY_END + timedelta(minutes=10)


def _visible(api: ApiDatabase) -> tuple[object, object]:
    with api.pool.connection() as page_connection:
        page = api_players.read_player_page(
            page_connection, "#2PP", now=READ_AT, freshness_seconds=900
        )
    board = api_leaderboard.get_live_leaderboard(api, limit=50, now=READ_AT)
    assert page is not None and page["screen_ready"]["days"]
    assert board is not None
    return page, board
