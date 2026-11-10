"""Validated official past-Season results shared by player-history readers."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from .domain import SEASON_DURATION, DomainRuleError, validate_legend_season_start

_LEGEND_I_TIER_ID = 105000036


def official_history_rows(
    connection: Any,
    normalized_tag: str,
    *,
    season_id: str | None = None,
) -> list[dict[str, Any]]:
    filters = ["player.normalized_tag = %s", "history.league_tier_id = %s"]
    parameters: list[Any] = [normalized_tag, _LEGEND_I_TIER_ID]
    if season_id is not None:
        if not (season_id.isascii() and season_id.isdigit()):
            return []
        filters.append("history.league_season_id = %s")
        parameters.append(
            str(int(season_id) + int(SEASON_DURATION.total_seconds()))
        )
    rows = connection.execute(
        f"""
        SELECT history.league_season_id, history.observed_at,
               history.league_trophies, history.placement
        FROM player_league_history_entries AS history
        JOIN players AS player ON player.id = history.player_id
        WHERE {" AND ".join(filters)}
        ORDER BY history.league_season_id DESC
        """,
        parameters,
    ).fetchall()
    valid = []
    for row in rows:
        observed_at = row[1].astimezone(UTC)
        # League history names a Season by the Reset that ended it, which is
        # the next Season's start: the row for 5 Oct 2026 05:00 holds the
        # results of the Season that started 7 Sep 2026.
        try:
            season_end = validate_legend_season_start(
                row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0],
                observed_at=observed_at,
            )
        except DomainRuleError:
            # Old rows predate reader-side validation. They stay retained as
            # evidence, but an invalid boundary never reaches a season page.
            continue
        season_start = season_end - SEASON_DURATION
        trophies = None if row[2] is None else int(row[2])
        placement = None if row[3] is None else int(row[3])
        valid.append(
            {
                "official_season_id": str(int(season_start.timestamp())),
                "observed_at": observed_at,
                "season_start": season_start,
                "season_end": season_end,
                "eod_trophies": trophies
                if trophies is not None and trophies >= 0
                else None,
                "final_placement": (
                    placement if placement is not None and placement >= 1 else None
                ),
            }
        )
    return valid
