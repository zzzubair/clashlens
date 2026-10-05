"""Refuse Season closure while relevant work or promised history is missing.

Finalization stops corrections and retirement deletes detail, so both ask
this guard first. Any unfinished, failed, cancelled, unclassified or
unprovable work blocks unless its own dates prove it cannot touch the
Season; when it was fetched or requested never does. A missing relation,
failed query or timeout blocks too: an unanswered check is never
readiness. Each check is a bounded existence query returning at most a
few example ids, not a count.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg

GUARD_VERSION = "season-close-guard-v1"
EXAMPLE_LIMIT = 5
CHECK_TIMEOUT = "10s"
# Work from the Legend day before the Season can still feed its opening Reset.
SCOPE_LOOKBACK = timedelta(days=1)
# Completed results that prove the work applied. Superseded, gaps,
# non-success and unknown outcomes stay unresolved until accounted for.
SUCCESS_OUTCOMES = ("processed", "stale_superseded", "official_observed")
SETTLED_GENERATION_STATES = ("published", "superseded")
EXPANDED_HISTORY_UNAVAILABLE = "expanded_history_unavailable"
REQUIRED_RELATIONS = (
    "python_processing_jobs",
    "collector_observations",
    "observation_processing_outcomes",
    "python_replay_requests",
    "legend_battles",
    "boundary_publication_generations",
    "boundary_publication_corrections",
)

# Whether a job could still change or feed the Season. Responses count from
# the day before the Season however late they were fetched; Legend days from
# that day to the closing day, since the opening day reads the ending result;
# publications at either Season edge; army redecodes for battles on the
# Season's own days. Exports, unknown work types, missing sources and
# unreadable dates or battle ids cannot prove themselves out of scope.
_JOB_IN_SCOPE = """
CASE
    WHEN job.work_type IN ('process_observation', 'replay_observation')
        THEN COALESCE(observation.response_completed_at >= %(scope_start)s, true)
    WHEN job.work_type = 'redecode_army' THEN COALESCE((
        SELECT CASE WHEN count(*) > 0 AND count(*) = count(battle.id)
                    THEN bool_or(battle.ranked_day_start >= %(season_start)s
                                 AND battle.ranked_day_start < %(season_end)s) END
        FROM jsonb_array_elements_text(CASE
            WHEN jsonb_typeof(job.input_json -> 'battle_ids') = 'array'
                THEN job.input_json -> 'battle_ids'
            WHEN jsonb_typeof(job.input_json -> 'battle_id') = 'number'
                THEN jsonb_build_array(job.input_json -> 'battle_id')
            ELSE '[]'::jsonb
        END) AS requested(value)
        LEFT JOIN legend_battles AS battle ON battle.id = CASE
            WHEN pg_input_is_valid(requested.value, 'bigint')
                THEN requested.value::bigint END
    ), true)
    WHEN job.work_type IN (
        'reconcile_ranked_day', 'build_snapshot', 'build_analytics', 'build_army_analytics'
    ) THEN COALESCE((
        SELECT CASE WHEN bool_and(pg_input_is_valid(value, 'timestamptz')) THEN bool_or(
            CASE WHEN pg_input_is_valid(value, 'timestamptz') THEN
                value::timestamptz >= CASE kind WHEN 'day' THEN %(scope_start)s
                                                ELSE %(season_start)s END
                AND value::timestamptz <= %(season_end)s
            END) END
        FROM (VALUES ('day', job.input_json ->> 'ranked_day_start'),
                     ('boundary', job.input_json ->> 'boundary_at')) AS dates(kind, value)
        WHERE value IS NOT NULL
    ), true)
    ELSE true
END
"""

_CHECKS = {
    "processing_jobs": f"""
        SELECT job.id FROM python_processing_jobs AS job
        LEFT JOIN collector_observations AS observation
          ON observation.id = COALESCE(job.observation_id, job.replay_observation_id)
        WHERE NOT COALESCE(job.status = 'complete' AND job.outcome = ANY(%(success)s), false)
          AND ({_JOB_IN_SCOPE})
        ORDER BY job.id LIMIT %(limit)s
    """,
    # Finished-job cleanup removes completed jobs but keeps their outcome,
    # so the newest retained outcome is the proof, not a missing job.
    "unproven_observations": """
        SELECT observation.id FROM collector_observations AS observation
        WHERE COALESCE(observation.response_completed_at >= %(scope_start)s, true)
          AND COALESCE((
              SELECT outcome.outcome FROM observation_processing_outcomes AS outcome
              WHERE outcome.observation_id = observation.id
              ORDER BY outcome.created_at DESC, outcome.id DESC LIMIT 1
          ), '') <> 'processed'
        ORDER BY observation.id DESC LIMIT %(limit)s
    """,
    "replay_requests": """
        SELECT request.id FROM python_replay_requests AS request
        LEFT JOIN collector_observations AS observation
          ON observation.id = request.observation_id
        WHERE request.status <> 'complete'
          AND COALESCE(observation.response_completed_at >= %(scope_start)s, true)
        ORDER BY request.id LIMIT %(limit)s
    """,
    # Publications at both Season edges, the closing Reset included.
    "boundary_generations": """
        SELECT generation.id FROM boundary_publication_generations AS generation
        WHERE generation.boundary_at >= %(season_start)s
          AND generation.boundary_at <= %(season_end)s
          AND NOT (generation.snapshot_state = ANY(%(settled)s)
                   AND generation.army_state = ANY(%(settled)s))
        ORDER BY generation.id LIMIT %(limit)s
    """,
    # Later corrections can carry the Season's ending result forward. Only a
    # finalized correction whose generation published is resolved.
    "boundary_corrections": """
        SELECT correction.id FROM boundary_publication_corrections AS correction
        LEFT JOIN boundary_publication_generations AS generation
          ON generation.id = correction.generation_id
        WHERE correction.boundary_at >= %(season_start)s
          AND NOT COALESCE(correction.state = 'finalized'
                           AND generation.snapshot_state = ANY(%(settled)s)
                           AND generation.army_state = ANY(%(settled)s), false)
        ORDER BY correction.id LIMIT %(limit)s
    """,
    # A Reset's settlement check reads the two days before it, so it holds
    # their detail until it finishes, its responses are processed and it is
    # judged.
    "reset_settlement_checks": """
        SELECT settlement.id FROM reset_boundary_settlements AS settlement
        JOIN collector_work AS work ON work.id = settlement.delayed_work_id
        WHERE settlement.boundary_at > %(season_start)s
          AND settlement.boundary_at - interval '2 days' < %(season_end)s
          AND (work.status IN ('pending', 'waiting_retry')
               OR (settlement.state = 'provisional'
                   AND settlement.reasons <> '["new_reset_proofs_disabled"]'::jsonb)
               OR EXISTS (
                   SELECT 1 FROM collector_observations AS observed
                   WHERE observed.id IN (work.profile_observation_id,
                                         work.battle_log_observation_id)
                     AND NOT EXISTS (
                         SELECT 1 FROM observation_processing_outcomes AS outcome
                         WHERE outcome.observation_id = observed.id
                     )
               ))
        ORDER BY settlement.id LIMIT %(limit)s
    """,
}


def promised_history_gap(connection: Any, season_id: str) -> str | None:
    """Why the promised expanded history cannot be proven for the Season.

    Movement from the previous EOD, tracked final rank, the final Top 100
    and Clan Castle history have no accepted format yet, and the current
    player and army summary formats cannot stand in for them. Until a
    check for them exists, closure is always refused, including for a
    Season an older version already finalized.
    """
    del connection, season_id
    return EXPANDED_HISTORY_UNAVAILABLE


def _examples(connection: Any, sql: str, params: dict[str, Any]) -> list[Any]:
    previous = connection.execute("SELECT current_setting('statement_timeout')").fetchone()[0]
    with connection.transaction():
        connection.execute("SELECT set_config('statement_timeout', %s, true)", (CHECK_TIMEOUT,))
        rows = connection.execute(sql, params).fetchall()
        connection.execute("SELECT set_config('statement_timeout', %s, true)", (previous,))
    return [row[0] for row in rows]


def close_blockers(
    connection: Any, season_id: str, season_start: Any, season_end: Any
) -> dict[str, list[Any]]:
    """Map each reason the Season cannot close to a few examples.

    An empty result means no blocker was found. ``season_start`` and
    ``season_end`` are the exact Season window.
    """
    blockers: dict[str, list[Any]] = {}
    rows = connection.execute(
        "SELECT name FROM unnest(%s::text[]) AS name WHERE to_regclass(name) IS NULL ORDER BY name",
        (list(REQUIRED_RELATIONS),),
    ).fetchall()
    if rows:
        blockers["missing_relations"] = [row[0] for row in rows]
    else:
        params = {
            "scope_start": season_start - SCOPE_LOOKBACK,
            "season_start": season_start,
            "season_end": season_end,
            "success": list(SUCCESS_OUTCOMES),
            "settled": list(SETTLED_GENERATION_STATES),
            "limit": EXAMPLE_LIMIT,
        }
        for name, sql in _CHECKS.items():
            try:
                examples = _examples(connection, sql, params)
            except psycopg.Error as error:
                blockers.setdefault("failed_checks", []).append(
                    {"check": name, "error": type(error).__name__}
                )
                continue
            if examples:
                blockers[name] = examples
    gap = promised_history_gap(connection, season_id)
    if gap is not None:
        blockers["promised_history"] = [gap]
    return blockers
