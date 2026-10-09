from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from fractions import Fraction
from time import monotonic
from typing import Any

from .domain import ranked_day_for
from .profile import normalize_player_tag

# v2 orders equal trophies by the shared tie order below; v1 by SHA-256 of
# the tag alone.
SNAPSHOT_ORDERING_RULE_VERSION = "tracked-player-order-v2"
FRESHNESS_RULE_VERSION = "profile-freshness-10m-v1"
PROFILE_FRESHNESS_SECONDS = 600
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
CLASSIFICATION_VERSION = "army-classifier-unavailable-v1"
CLASSIFICATION_CONFIDENCE = "unclassified"


def deterministic_tag_hash(tag: str) -> str:
    """The tag hash both boards break their last ties with: MD5 of the
    normalized tag, as PostgreSQL's md5() gives it."""
    normalized_tag = normalize_player_tag(tag)
    return hashlib.md5(normalized_tag.encode("ascii")).hexdigest()


# The owner's order for equal trophies on the Live and Daily boards (8
# October 2026): the higher Season average attack destruction first,
# destruction over attacks made, zero-star attacks included, compared as
# exact fractions, with players who have made no attack last; then more
# attacks; then the tag hash; then the tag. The game's own Season-end
# placements agree with average destruction on all 274 equal-trophy pairs
# whose attacks Clash Lens fully recorded (firstmate's tie-break research).
def tie_order_key(attacks: int, destruction: int, tag: str) -> tuple[Any, ...]:
    """Sort key among equal trophies, smallest first."""
    if attacks <= 0:
        return (True, Fraction(0), 0, deterministic_tag_hash(tag), tag)
    return (
        False, -Fraction(destruction, attacks), -attacks, deterministic_tag_hash(tag), tag,
    )


def tie_order_sql(attacks: str, destruction: str, tag: str) -> str:
    """SQL ORDER BY terms for ``tie_order_key``, after trophies."""
    return (
        f"COALESCE({attacks}, 0) <= 0, "
        f"{destruction}::numeric / NULLIF({attacks}, 0) DESC NULLS LAST, "
        f"COALESCE({attacks}, 0) DESC, md5({tag}) COLLATE \"C\", {tag} COLLATE \"C\""
    )


# Each attacker's attacks and summed attack destruction on the battle days
# from %(season_start)s up to %(cutoff)s, from their own battle log's record
# of each attack. On 8 October 2026, 37 of 311,182 recorded battles of the
# Season had no record from the attacker's side.
_SEASON_ATTACKS_SQL = """
SELECT battle.attacker_player_id AS player_id,
       count(*)::integer AS attacks,
       sum(evidence.destruction_percentage)::integer AS destruction
FROM legend_battles AS battle
JOIN battle_perspectives AS side
  ON side.battle_id = battle.id AND side.perspective = 'attacker'
JOIN battle_evidence AS evidence ON evidence.id = side.evidence_id
WHERE battle.ranked_day_start >= %(season_start)s
  AND battle.ranked_day_start < %(cutoff)s
  AND (%(player_ids)s::bigint[] IS NULL
       OR battle.attacker_player_id = ANY(%(player_ids)s::bigint[]))
GROUP BY battle.attacker_player_id
"""


def season_attack_tallies(
    connection: Any,
    *,
    season_start: datetime,
    cutoff: datetime,
    player_ids: list[int] | None = None,
) -> dict[int, tuple[int, int]]:
    """Each player's (attacks, destruction) in the Season before ``cutoff``."""
    return {
        int(row[0]): (int(row[1]), int(row[2]))
        for row in connection.execute(
            _SEASON_ATTACKS_SQL,
            {"season_start": season_start, "cutoff": cutoff, "player_ids": player_ids},
        ).fetchall()
    }


# The Live board reads these counts from live_attack_tallies, recounted from
# the current Season's recorded battles every LIVE_TALLY_SECONDS (about 5
# seconds at a Season's end on 8 October 2026 data), so its tie order can lag
# new attacks by that long.
LIVE_TALLY_SECONDS = 300
_next_live_tally_at = float("-inf")


def refresh_live_attack_tallies(database: Any, *, now: datetime | None = None) -> None:
    """Recount the current Season's attacks when due; a failure waits a turn."""
    global _next_live_tally_at
    started = monotonic()
    if started < _next_live_tally_at:
        return
    _next_live_tally_at = started + LIVE_TALLY_SECONDS
    day = ranked_day_for(now or datetime.now(UTC))
    try:
        with database.pool.connection() as connection, connection.transaction():
            changed = connection.execute(
                f"""
                WITH counted AS ({_SEASON_ATTACKS_SQL}), upserted AS (
                    INSERT INTO live_attack_tallies AS tally
                        (player_id, official_season_id, attacks, destruction)
                    SELECT player_id, %(season_id)s, attacks, destruction FROM counted
                    ON CONFLICT (player_id) DO UPDATE
                    SET official_season_id = excluded.official_season_id,
                        attacks = excluded.attacks,
                        destruction = excluded.destruction,
                        refreshed_at = clock_timestamp()
                    WHERE (tally.official_season_id, tally.attacks, tally.destruction)
                          IS DISTINCT FROM (excluded.official_season_id,
                                            excluded.attacks, excluded.destruction)
                    RETURNING 1
                ), emptied AS (
                    -- A player whose counted battles all moved out of the
                    -- Season has none left.
                    UPDATE live_attack_tallies AS tally
                    SET attacks = 0, destruction = 0, refreshed_at = clock_timestamp()
                    WHERE tally.official_season_id = %(season_id)s
                      AND tally.attacks > 0
                      AND NOT EXISTS (
                          SELECT 1 FROM counted WHERE counted.player_id = tally.player_id
                      )
                    RETURNING 1
                )
                SELECT (SELECT count(*) FROM upserted) + (SELECT count(*) FROM emptied)
                """,
                {
                    "season_start": day.season_start,
                    "cutoff": day.season_end,
                    "player_ids": None,
                    "season_id": day.official_season_id,
                },
            ).fetchone()[0]
    except Exception as error:  # noqa: BLE001 - tried again at the next turn
        print(
            json.dumps(
                {
                    "event": "live_attack_tallies",
                    "status": "failed",
                    "error": type(error).__name__,
                }
            ),
            flush=True,
        )
        return
    print(
        json.dumps(
            {
                "event": "live_attack_tallies",
                "status": "complete",
                "changed": int(changed),
                "seconds": round(monotonic() - started, 1),
            }
        ),
        flush=True,
    )
