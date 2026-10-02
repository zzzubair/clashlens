"""Recalculate saved Legend days that miss a battle saved after their Reset.

The Clash API can serve a battle log cached for about 60 seconds, so a battle
in the last minute before the 05:00 UTC Reset can first be saved after its
Legend day's result was published. Once per Reset, after the Reset sweep has
finished and every response fetched before it finished has been processed,
this finds each player whose saved result for one of the previous 7 Legend
days misses such a report, or still holds the old agreement flag for its
battle. For each such player, in one transaction, it recalculates and
publishes that day and then every later saved day, in order, with the existing
ranked-day recalculation. The live recalculation and Reset paths are
unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from time import monotonic

from . import reconciliation_db
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
)
from .domain import ranked_day_for

# First check 30 minutes after the Reset, then every 10 minutes until every
# selected player has been corrected.
SWEEP_DELAY = timedelta(minutes=30)
CHECK_INTERVAL_SECONDS = 600
# A report saved this close before its day ended may have missed the live
# recalculation queued with it, so it is checked too.
SAVE_MARGIN = timedelta(minutes=5)
# Ended Legend days whose late battles are corrected. The recalculation
# supports only the current and previous Season; a late battle on an older day
# is logged and skipped.
WINDOW = timedelta(days=7)

# Every kept battle with a report saved late is checked against each reporting
# player's latest saved result: their own report and the battle's current
# agreement flag must both be listed. Days of a retired season cannot be
# recalculated and are skipped.
_STALE_DAYS = """
WITH late_battle AS (
    SELECT battle.id, battle.ranked_day_start,
           battle.attacker_player_id, battle.defender_player_id,
           battle.disagreement_state = 'disagreement' AS disagreement
    FROM legend_battles AS battle
    JOIN battle_perspectives AS perspective ON perspective.battle_id = battle.id
    JOIN battle_evidence AS evidence ON evidence.id = perspective.evidence_id
    WHERE battle.ranked_day_start < %(boundary)s
      AND evidence.created_at
          >= battle.ranked_day_start + interval '24 hours' - %(margin)s
    GROUP BY battle.id
), pair AS (
    SELECT CASE perspective.perspective
               WHEN 'attacker' THEN battle.attacker_player_id
               ELSE battle.defender_player_id
           END AS player_id,
           battle.ranked_day_start,
           jsonb_agg(jsonb_build_object(
               'source_evidence_id', perspective.evidence_id,
               'disagreement', battle.disagreement
           )) AS expected
    FROM late_battle AS battle
    JOIN battle_perspectives AS perspective ON perspective.battle_id = battle.id
    GROUP BY 1, 2
)
SELECT pair.player_id, pair.ranked_day_start
FROM pair
CROSS JOIN LATERAL (
    SELECT log.ranked_day_version_id
    FROM api_player_daily_logs AS log
    WHERE log.player_id = pair.player_id
      AND log.ranked_day_start = pair.ranked_day_start
    ORDER BY log.version DESC
    LIMIT 1
) AS published
LEFT JOIN ranked_day_versions AS version
  ON version.id = published.ranked_day_version_id
WHERE NOT coalesce(version.contribution_evidence @> pair.expected, false)
  AND NOT EXISTS (
      SELECT 1 FROM season_detail_retirements AS retirement
      WHERE retirement.status IN ('finalized', 'retired')
        AND (
            retirement.official_season_id = version.official_season_id
            OR (pair.ranked_day_start >= retirement.season_start
                AND pair.ranked_day_start < retirement.season_end)
        )
  )
ORDER BY pair.player_id, pair.ranked_day_start
"""


def sweep_late_battles(database: Database, *, now: datetime) -> tuple[int, int] | None:
    """Correct each selected player, or return None while the Reset is not ready.

    Returns how many players were corrected and how many failed. A failed
    player's changes are rolled back and retried at the next run.
    """

    boundary = ranked_day_for(now).start
    if now < boundary + SWEEP_DELAY:
        return None
    with database.pool.connection() as connection, connection.transaction():
        sweep = connection.execute(
            """
            SELECT GREATEST(sweep.created_at, max(work.updated_at)),
                   count(*) FILTER (WHERE work.status NOT IN (
                       'complete', 'failed', 'cancelled'
                   ))
            FROM collector_reset_sweeps AS sweep
            LEFT JOIN collector_work AS work
              ON work.sweep_id = sweep.id AND work.kind = 'reset_baseline'
            WHERE sweep.boundary_at = %s
            GROUP BY sweep.id
            """,
            (boundary,),
        ).fetchone()
        if sweep is None or sweep[1] > 0:
            return None
        responses_pending = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM python_processing_jobs
                WHERE work_type IN ('process_observation', 'replay_observation')
                  AND status IN (
                      'pending', 'leased', 'waiting_retry', 'waiting_dependency'
                  )
                  AND created_at <= %s
            )
            """,
            (sweep[0],),
        ).fetchone()[0]
        if responses_pending:
            return None
        stale_days = connection.execute(
            _STALE_DAYS, {"boundary": boundary, "margin": SAVE_MARGIN}
        ).fetchall()
    event = {"event": "late_battle_sweep", "boundary_at": boundary.isoformat()}
    first_days: dict[int, datetime] = {}
    skipped: list[str] = []
    for player_id, day_start in stale_days:
        if day_start < boundary - WINDOW:
            skipped.append(f"{player_id}:{day_start.astimezone(UTC).isoformat()}")
        else:
            first_days.setdefault(int(player_id), day_start)
    if skipped:
        print(
            json.dumps(
                {
                    **event,
                    "status": "skipped",
                    "reason": "older_than_correction_window",
                    "player_days": skipped,
                }
            ),
            flush=True,
        )
    failed = 0
    for player_id, first_day in first_days.items():
        try:
            with database.pool.connection() as connection, connection.transaction():
                saved_days = connection.execute(
                    """
                    SELECT DISTINCT ranked_day_start
                    FROM api_player_daily_logs
                    WHERE player_id = %s AND ranked_day_start >= %s
                    ORDER BY ranked_day_start
                    """,
                    (player_id, first_day),
                ).fetchall()
                # Each day's result reads the day before it, so they are
                # recalculated oldest first.
                for (day_start,) in saved_days:
                    reconciliation_db.recalculate_ranked_day(
                        database,
                        connection,
                        player_id=player_id,
                        day_start=day_start,
                        parser_version=DEFAULT_PARSER_VERSION,
                        processing_version=PROCESSING_VERSION,
                        domain_rule_version=DOMAIN_RULE_VERSION,
                        analytics_rule_version=ANALYTICS_RULE_VERSION,
                    )
        except Exception as error:  # noqa: BLE001 - rolled back, retried next run
            failed += 1
            print(
                json.dumps(
                    {
                        **event,
                        "status": "player_failed",
                        "player_id": player_id,
                        "error": repr(error)[:300],
                    }
                ),
                flush=True,
            )
    return len(first_days) - failed, failed


class LateBattleSweep:
    """Run the sweep from the worker loop until each Reset's corrections succeed."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.next_check_at = float("-inf")
        self.swept_boundary: datetime | None = None

    def run_when_due(self, now: datetime | None = None) -> None:
        if not isinstance(self.database, Database) or monotonic() < self.next_check_at:
            return
        self.next_check_at = monotonic() + CHECK_INTERVAL_SECONDS
        now = now or datetime.now(UTC)
        boundary = ranked_day_for(now).start
        if boundary == self.swept_boundary:
            return
        event = {"event": "late_battle_sweep", "boundary_at": boundary.isoformat()}
        try:
            result = sweep_late_battles(self.database, now=now)
        except Exception as error:  # noqa: BLE001 - retry at the next check
            print(
                json.dumps({**event, "status": "failed", "error": repr(error)[:300]}),
                flush=True,
            )
            return
        if result is None:
            return
        corrected, failed = result
        if failed == 0:
            self.swept_boundary = boundary
        print(
            json.dumps(
                {
                    **event,
                    "status": "complete" if failed == 0 else "retrying",
                    "corrected_players": corrected,
                    "failed_players": failed,
                }
            ),
            flush=True,
        )
