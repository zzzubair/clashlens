from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from fractions import Fraction
from time import monotonic
from typing import Any

from .domain import SEASON_START_TROPHIES, ranked_day_for
from .profile import normalize_player_tag

# v3 orders equal trophies by the latest trophy change first; v2 by Season
# average attack destruction; v1 by SHA-256 of the tag alone.
SNAPSHOT_ORDERING_RULE_VERSION = "tracked-player-order-v3"
SEASON_DESTRUCTION_ORDERING_RULE_VERSION = "tracked-player-order-v2"
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


# The owner's order for equal trophies on the Live board, and on Daily
# boards frozen before the latest-change order (8 October 2026): the higher Season average attack destruction first,
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


# The order for equal trophies on Daily boards (owner, 11 October 2026:
# "based on most accurate"): the player whose trophies changed latest in the
# ended Legend day comes first, players with no change that day last, then
# the tag hash, then the tag. At the 11 October 2026 Reset the game's own
# Reset ranks agreed with this on 70.4% of 1,237 equal-trophy pairs, against
# 48.1% for Season average attack destruction and 53.8% for the tag hash.
def latest_change_order_key(
    latest_change_at: datetime | None, tag: str
) -> tuple[Any, ...]:
    """Sort key among equal trophies, smallest first."""
    return (
        latest_change_at is None,
        -latest_change_at.timestamp() if latest_change_at else 0.0,
        deterministic_tag_hash(tag),
        tag,
    )


# Each player's latest trophy change in the Legend day starting
# %(day_start)s, before %(cutoff)s: the end of their newest battle of the day
# that moved their trophies. A battle ends at the attacker's report time; with
# only the defender's report, 155 seconds after it, the median attack length
# (999 battles on 11 October 2026). Each side's change comes from its own
# report when there is one.
_LATEST_CHANGE_SQL = """
SELECT side.player_id, max(ended.at) AS latest_change_at
FROM legend_battles AS battle
LEFT JOIN battle_perspectives AS attacker_side
  ON attacker_side.battle_id = battle.id AND attacker_side.perspective = 'attacker'
LEFT JOIN battle_evidence AS attacker ON attacker.id = attacker_side.evidence_id
LEFT JOIN battle_perspectives AS defender_side
  ON defender_side.battle_id = battle.id AND defender_side.perspective = 'defender'
LEFT JOIN battle_evidence AS defender ON defender.id = defender_side.evidence_id
CROSS JOIN LATERAL (
    SELECT COALESCE(attacker.battle_timestamp,
                    defender.battle_timestamp + interval '155 seconds') AS at
) AS ended
CROSS JOIN LATERAL (VALUES
    (battle.attacker_player_id,
     COALESCE(attacker.attacker_gain, defender.attacker_gain)),
    (battle.defender_player_id,
     COALESCE(defender.defender_loss, attacker.defender_loss))
) AS side (player_id, change)
WHERE battle.ranked_day_start = %(day_start)s
  AND (battle.attacker_player_id = ANY(%(player_ids)s::bigint[])
       OR battle.defender_player_id = ANY(%(player_ids)s::bigint[]))
  AND side.player_id = ANY(%(player_ids)s::bigint[])
  AND side.change <> 0
  AND ended.at < %(cutoff)s
GROUP BY side.player_id
"""


def latest_trophy_changes(
    connection: Any, *, day_start: datetime, cutoff: datetime, player_ids: list[int]
) -> dict[int, datetime]:
    """Each player's latest trophy change in the day before ``cutoff``."""
    return {
        int(row[0]): row[1]
        for row in connection.execute(
            _LATEST_CHANGE_SQL,
            {"day_start": day_start, "cutoff": cutoff, "player_ids": player_ids},
        ).fetchall()
    }


def unplayed_profile_sql(profile: str) -> str:
    """SQL true when the profile version ``profile`` can show a player who
    has not played yet: Legend I at 5,000, and accepted or naming Season 0,
    which the game sends before sign-up. Any other rejected profile, such as
    one with a malformed Season, stays rejected."""
    return f"""({profile}.eligibility_state = 'eligible'
        AND {profile}.trophies = {SEASON_START_TROPHIES}
        AND ({profile}.source_contract_state = 'accepted'
             OR {profile}.current_league_season_id = '0'))"""


def unplayed_sql(player_id: str, profile: str, season_start: str, before: str) -> str:
    """SQL true when the profile version ``profile`` can show a player who
    has not played yet (``unplayed_profile_sql``) and the player's saved
    Legend days from ``season_start`` to ``before`` hold no battle: both
    boards rank them at 5,000."""
    # A one-row lookup rather than NOT EXISTS, which the planner may answer
    # by reading every saved day of the Season with its battles: on 11
    # October 2026 that took the Live Leaderboard past its 10 second limit.
    return f"""({unplayed_profile_sql(profile)} AND (
        SELECT 1 FROM api_player_daily_logs AS played
        WHERE played.player_id = {player_id}
          AND played.ranked_day_start >= {season_start}
          AND played.ranked_day_start < {before}
          AND jsonb_array_length(played.battles) > 0
          -- A battle a newer result moved out of this Season does not count.
          AND NOT EXISTS (
              SELECT 1 FROM api_player_daily_logs AS newer
              WHERE newer.player_id = played.player_id
                AND newer.ranked_day_start = played.ranked_day_start
                AND newer.version > played.version)
        LIMIT 1) IS NULL)"""


def unplayed_since_sql(player_id: str, season_start: str, day: str) -> str:
    """SQL for when the player's "has not played yet" is counted from for
    the Legend day starting at ``day``: the Season's start ``season_start``
    or, if later, the Monday Reset of their newest week up to that day that
    the weekly league record says they were promoted or first seen in
    Legend I. So a player who played earlier in the Season, dropped and was
    promoted back, or whose lower-league profiles named another total
    before their promotion, has not played since. Daily boards and Reset
    readings both count from it."""
    return f"""GREATEST({season_start}::timestamptz, (
        SELECT max(week.week_start) FROM player_league_weeks AS week
        WHERE week.player_id = {player_id}
          AND week.week_start <= {day}
          AND week.league = 'legend_i'
          AND week.entry IN ('promoted', 'first_seen')))"""


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
