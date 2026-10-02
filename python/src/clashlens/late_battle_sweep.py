"""Recalculate saved Legend days that miss a battle saved after their Reset.

The Clash API can serve a battle log cached for about 60 seconds, so a battle
in the last minute before the 05:00 UTC Reset can first be saved after its
Legend day's result was published. Once per Reset, after the Reset sweep has
finished and every response fetched before it finished has been processed,
this queues the existing ranked-day recalculation for each such day and every
later saved day of that player. The live recalculation and Reset paths are
unchanged; the worker does the recalculation and publication.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from time import monotonic

from psycopg.types.json import Jsonb

from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
)
from .domain import ranked_day_for

# First check 30 minutes after the Reset, then every 10 minutes until it runs.
SWEEP_DELAY = timedelta(minutes=30)
CHECK_INTERVAL_SECONDS = 600
# Ended Legend days searched for late battles. Covers a week-long outage; a
# battle log keeps only the latest 50 battles.
WINDOW = timedelta(days=7)
# A report saved this close before its day ended may have missed the live
# recalculation queued with it, so it is checked too.
SAVE_MARGIN = timedelta(minutes=5)
# Each later day is due this long after the day before it, because each
# recalculation reads the previous day's saved result. A worker running more
# than this far behind can still recalculate a later day first.
DAY_SPACING = timedelta(minutes=5)


def sweep_late_battles(database: Database, *, now: datetime) -> list[int] | None:
    """Queue recalculations for the latest Reset, or None while not ready."""

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
        # A player's own selected report is what their daily result uses.
        late_rows = connection.execute(
            """
            WITH late AS (
                SELECT CASE perspective.perspective
                           WHEN 'attacker' THEN battle.attacker_player_id
                           ELSE battle.defender_player_id
                       END AS player_id,
                       battle.ranked_day_start,
                       perspective.evidence_id
                FROM legend_battles AS battle
                JOIN battle_perspectives AS perspective
                  ON perspective.battle_id = battle.id
                JOIN battle_evidence AS evidence
                  ON evidence.id = perspective.evidence_id
                WHERE battle.ranked_day_start >= %s
                  AND battle.ranked_day_start < %s
                  AND evidence.created_at
                      >= battle.ranked_day_start + interval '24 hours' - %s
            )
            SELECT late.player_id, late.ranked_day_start, late.evidence_id
            FROM late
            CROSS JOIN LATERAL (
                SELECT log.ranked_day_version_id
                FROM api_player_daily_logs AS log
                WHERE log.player_id = late.player_id
                  AND log.ranked_day_start = late.ranked_day_start
                ORDER BY log.version DESC
                LIMIT 1
            ) AS published
            WHERE NOT EXISTS (
                SELECT 1 FROM ranked_day_versions AS version
                WHERE version.id = published.ranked_day_version_id
                  AND version.contribution_evidence @> jsonb_build_array(
                      jsonb_build_object('source_evidence_id', late.evidence_id)
                  )
            )
            ORDER BY late.player_id, late.ranked_day_start, late.evidence_id
            """,
            (boundary - WINDOW, boundary, SAVE_MARGIN),
        ).fetchall()
        # Rows are ordered by day, so each player keeps their earliest day.
        late_by_player: dict[int, tuple[datetime, list[int]]] = {}
        for player_id, day_start, evidence_id in late_rows:
            late_by_player.setdefault(int(player_id), (day_start, []))[1].append(
                int(evidence_id)
            )
        job_ids: list[int] = []
        for player_id, (first_day, evidence_ids) in late_by_player.items():
            # The job key records which late reports this batch handles, so
            # a re-run for the same reports queues nothing.
            digest = hashlib.sha256(
                ",".join(str(value) for value in sorted(evidence_ids)).encode()
            ).hexdigest()
            saved_days = connection.execute(
                """
                SELECT DISTINCT ranked_day_start
                FROM api_player_daily_logs
                WHERE player_id = %s AND ranked_day_start >= %s
                ORDER BY ranked_day_start
                """,
                (player_id, first_day),
            ).fetchall()
            for position, (day_start,) in enumerate(saved_days):
                day_text = day_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                row = connection.execute(
                    """
                    INSERT INTO python_processing_jobs_worker (
                        observation_id, work_type, deduplication_key,
                        input_json, state, due_at, parser_version,
                        processing_version, domain_rule_version,
                        analytics_rule_version
                    ) VALUES (
                        NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                        transaction_timestamp() + %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (deduplication_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        f"reconcile:late-battle:{player_id}:{day_text}:{digest}",
                        Jsonb(
                            {
                                "player_id": player_id,
                                "ranked_day_start": day_text,
                                "trigger": "late_battle_sweep",
                                "late_evidence_ids": sorted(evidence_ids),
                            }
                        ),
                        position * DAY_SPACING,
                        DEFAULT_PARSER_VERSION,
                        PROCESSING_VERSION,
                        DOMAIN_RULE_VERSION,
                        ANALYTICS_RULE_VERSION,
                    ),
                ).fetchone()
                if row is not None:
                    job_ids.append(int(row[0]))
        return job_ids


class LateBattleSweep:
    """Run the sweep from the worker loop once per Reset."""

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
            job_ids = sweep_late_battles(self.database, now=now)
        except Exception as error:  # noqa: BLE001 - retry at the next check
            print(
                json.dumps({**event, "status": "failed", "error": repr(error)[:300]}),
                flush=True,
            )
            return
        if job_ids is None:
            return
        self.swept_boundary = boundary
        print(
            json.dumps({**event, "status": "complete", "queued_jobs": len(job_ids)}),
            flush=True,
        )
