from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import uuid4

from psycopg_pool import ConnectionPool

from .background_pacing import DAY_RECHECK_PRIORITY, background_lanes
from .operating import database_pool_health
from .past_reset_pacing import (
    build_permit_busy,
    operator_correction_waits,
    past_reset_build_hold,
    take_build_permit,
)
from .source_observation_contract import SOURCE_OBSERVATION_CONTRACTS

PROCESSING_VERSION = "clashlens-domain-processing-v1"
# Label on reconcile, build and Reset-baseline work. It stays at
# source-parser-v2 when the battle parser moves on, so those jobs keep the
# claim class older workers already accept.
DEFAULT_PARSER_VERSION = "supercell-source-parser-v2"
DOMAIN_RULE_VERSION = "clashlens-domain-rules-v1"
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
ARMY_ANALYTICS_RULE_VERSION = "army-analytics-v2"
CONTRACT_VERSION = 5
PYTHON_BACKFILL_PRIORITY = 25
PYTHON_LIVE_PRIORITY = 100
# Reset readings, ended-day results, board builds; only other work gains 10 a minute,
# so live jobs in the claim window pass it at 20 minutes; retried live jobs may not.
PYTHON_RESET_PRIORITY = 300


def ended_day_priority(ranked_day_start: datetime) -> int:
    """Reset priority for a result of the Legend day the latest Reset ended."""
    ended_at = ranked_day_start + timedelta(days=1)
    if ended_at <= datetime.now(UTC) < ended_at + timedelta(days=1):
        return PYTHON_RESET_PRIORITY
    return PYTHON_LIVE_PRIORITY
DEFAULT_POOL_SIZE = 4
MAX_POOL_SIZE = 64
# The running worker cancels any one database statement, including time spent
# waiting for a lock, after 15 minutes. Its slowest statements since
# 2026-10-01 took under a minute, so only stuck work reaches this.
WORKER_STATEMENT_TIMEOUT_SECONDS = 900
# Work that can give up and retry waits at most this long for a busy Reset.
# Otherwise it keeps its own rows, and any earlier Resets it already took,
# locked for as long as a slow publication holds that Reset.
RESET_LOCK_WAIT = "250ms"

# Work types this worker image may claim. Unsupported work types (for example
# build_export) and unknown or future contracts stay pending and unclaimed so a
# later image that supports them can pick them up.
SUPPORTED_WORK_TYPES = (
    "process_observation",
    "replay_observation",
    "reconcile_ranked_day",
    "build_snapshot",
    "build_analytics",
    "build_army_analytics",
    "redecode_army",
)
# Work a worker slot may be limited to. Responses are the collector's saved
# API responses; population builds each read a whole Reset's players.
RESPONSE_WORK_TYPES = ("process_observation", "replay_observation")
POPULATION_BUILD_WORK_TYPES = (
    "build_snapshot",
    "build_analytics",
    "build_army_analytics",
)


def _supported_job_filter(
    alias: str,
    *,
    denormalized_contract: bool = True,
    supports_coordinator: bool = False,
) -> tuple[str, list[Any]]:
    """Parameterized SQL predicate for jobs this worker image may claim.

    Source jobs require an exact supported endpoint/schema contract and a
    parser version installed by the corresponding parser; unknown or future
    contracts stay unclaimed. The endpoint/schema contract is denormalized
    onto the job row by migration 0009, so this predicate is fully job-side. All supported work types require the current
    processing and domain rule versions. Reconciliation and analytics work
    additionally require the current analytics rule version, and analytics
    builds also require the complete current input shape so migration-style
    legacy analytics jobs stay pending and unclaimed.
    """
    source_clauses: list[str] = []
    params: list[Any] = []
    coordinator_input_shape = "TRUE"
    if supports_coordinator:
        coordinator_input_shape = f"""(
            {alias}.input_json ? 'generation' AND {alias}.input_json ? 'manifest_id'
        AND {alias}.input_json ? 'manifest_digest'
        AND ({alias}.input_json->>'generation') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_digest') ~ '^[0-9a-f]{{64}}$'
    )"""
    for contract in SOURCE_OBSERVATION_CONTRACTS:
        if denormalized_contract:
            source_clauses.append(
                f"""(
                    {alias}.parser_version = ANY(%s::text[])
                    AND {alias}.endpoint = %s
                    AND {alias}.endpoint_version = %s
                    AND {alias}.schema_version = %s
                )"""
            )
        else:
            # Pre-0009 schemas carry the contract only on the observation.
            source_clauses.append(
                f"""(
                    {alias}.parser_version = ANY(%s::text[])
                    AND EXISTS (
                        SELECT 1 FROM collector_observations AS source_observation
                        WHERE source_observation.id = COALESCE(
                            {alias}.observation_id, {alias}.replay_observation_id
                        )
                        AND source_observation.endpoint = %s
                        AND source_observation.endpoint_version = %s
                        AND source_observation.schema_version = %s
                    )
                )"""
            )
        params.extend(
            (
                sorted(contract.supported_parser_versions),
                contract.endpoint,
                contract.endpoint_version,
                contract.schema_version,
            )
        )
    source_contract = " OR ".join(source_clauses)
    analytics_input_shape = f"""(
        {alias}.input_json ? 'snapshot_id' AND {alias}.input_json ? 'snapshot_version'
        AND {alias}.input_json ? 'snapshot_input_hash' AND {alias}.input_json ? 'source_ranked_day_version_id'
        AND ({alias}.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'source_ranked_day_version_id') ~ '^[1-9][0-9]*$'
        AND length({alias}.input_json->>'snapshot_input_hash') > 0
    ) OR (
        {alias}.input_json ? 'snapshot_id' AND {alias}.input_json ? 'snapshot_version'
        AND {alias}.input_json ? 'snapshot_input_hash' AND {alias}.input_json ? 'generation'
        AND {alias}.input_json ? 'manifest_id' AND {alias}.input_json ? 'manifest_digest'
        AND ({alias}.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'generation') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_digest') ~ '^[0-9a-f]{{64}}$'
    )"""
    return (
        f"""(
            ({alias}.work_type = ANY(%s::text[])
                AND {alias}.processing_version = %s
                AND {alias}.domain_rule_version = %s
                AND ({source_contract}))
            OR ({alias}.work_type = ANY(%s::text[])
                AND {alias}.processing_version = %s
                AND {alias}.domain_rule_version = %s
                AND {alias}.analytics_rule_version = %s)
            OR ({alias}.work_type = ANY(%s::text[])
                AND {alias}.processing_version = %s
                AND {alias}.domain_rule_version = %s
                AND {alias}.analytics_rule_version = %s
                AND {coordinator_input_shape}
                AND (
                    {alias}.work_type = 'build_snapshot'
                    OR (
                        {alias}.work_type = 'build_analytics'
                        AND {analytics_input_shape}
                    )
                ))
            OR ({alias}.work_type = ANY(%s::text[])
                AND {alias}.processing_version = %s
                AND {alias}.domain_rule_version = %s
                AND {alias}.analytics_rule_version = %s
                AND (
                    {alias}.work_type = 'redecode_army'
                    OR (
                        {alias}.work_type = 'build_army_analytics'
                        AND {coordinator_input_shape}
                    )
                )))
        """,
        [
            list(SUPPORTED_WORK_TYPES[:2]),
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            *params,
            ["reconcile_ranked_day"],
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
            ["build_snapshot", "build_analytics"],
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
            ["build_army_analytics", "redecode_army"],
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ARMY_ANALYTICS_RULE_VERSION,
        ],
    )


def _supported_claim_filter(
    alias: str,
    observation_alias: str,
    *,
    denormalized_contract: bool = True,
    supports_coordinator: bool = False,
    past_reset_build_hold: str | None = None,
    operator_build_hold: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Parameterized supported-job predicate for the claim SELECT.

    The job-version checks match ``_supported_job_filter`` (used by cleanup
    UPDATE paths), with the claim compatibility fence also applied here. Both
    read the denormalized endpoint/schema contract columns on the job row.
    Parameters are named so the claim statement can
    also bind the claim time and direct job id. ``past_reset_build_hold`` is
    the newest Reset while past-Reset builds wait out the quiet
    window; those builds stay queued until it is None again.
    ``operator_build_hold`` holds operator corrections' leaderboard,
    statistics and army builds, the only builds at background priority,
    while it is True.
    """
    # The source contract is denormalized onto the job row by migration 0009
    # (trigger python_processing_jobs_set_source_contract_v3), so every
    # predicate here is job-side and the claim probes never need to touch
    # collector_observations. This keeps claim plans bounded at any depth.
    source_clauses: list[str] = []
    params: dict[str, Any] = {}
    coordinator_input_shape = "TRUE"
    if supports_coordinator:
        coordinator_input_shape = f"""(
            {alias}.input_json ? 'generation' AND {alias}.input_json ? 'manifest_id'
        AND {alias}.input_json ? 'manifest_digest'
        AND ({alias}.input_json->>'generation') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_digest') ~ '^[0-9a-f]{{64}}$'
    )"""
    for contract in SOURCE_OBSERVATION_CONTRACTS:
        prefix = f"source_contract_{len(params)}"
        if denormalized_contract:
            source_clauses.append(
                f"""(
                    {alias}.parser_version = ANY(%({prefix}_parsers)s::text[])
                    AND {alias}.endpoint = %({prefix}_endpoint)s
                    AND {alias}.endpoint_version = %({prefix}_endpoint_version)s
                    AND {alias}.schema_version = %({prefix}_schema)s
                )"""
            )
        else:
            source_clauses.append(
                f"""(
                    {alias}.parser_version = ANY(%({prefix}_parsers)s::text[])
                    AND {observation_alias}.endpoint = %({prefix}_endpoint)s
                    AND {observation_alias}.endpoint_version = %({prefix}_endpoint_version)s
                    AND {observation_alias}.schema_version = %({prefix}_schema)s
                )"""
            )
        params.update(
            {
                f"{prefix}_parsers": sorted(contract.supported_parser_versions),
                f"{prefix}_endpoint": contract.endpoint,
                f"{prefix}_endpoint_version": contract.endpoint_version,
                f"{prefix}_schema": contract.schema_version,
            }
        )
    source_contract = " OR ".join(source_clauses)
    analytics_input_shape = f"""(
        {alias}.input_json ? 'snapshot_id' AND {alias}.input_json ? 'snapshot_version'
        AND {alias}.input_json ? 'snapshot_input_hash' AND {alias}.input_json ? 'source_ranked_day_version_id'
        AND ({alias}.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'source_ranked_day_version_id') ~ '^[1-9][0-9]*$'
        AND length({alias}.input_json->>'snapshot_input_hash') > 0
    ) OR (
        {alias}.input_json ? 'snapshot_id' AND {alias}.input_json ? 'snapshot_version'
        AND {alias}.input_json ? 'snapshot_input_hash' AND {alias}.input_json ? 'generation'
        AND {alias}.input_json ? 'manifest_id' AND {alias}.input_json ? 'manifest_digest'
        AND ({alias}.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'generation') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_id') ~ '^[1-9][0-9]*$'
        AND ({alias}.input_json->>'manifest_digest') ~ '^[0-9a-f]{{64}}$'
    )"""
    # Claim generations fence staggered upgrades so older worker images cannot
    # interpret newer source contracts. Retain earlier generations for queued
    # work and explicit replay.
    claim_versions = "1, 2, 3, 4, 5, 6, 7" if supports_coordinator else "1, 2, 3"
    return (
        f"""({alias}.claim_compatibility_version IN ({claim_versions}) AND (
            ({alias}.work_type = ANY(%(source_work_types)s::text[])
                AND {alias}.processing_version = %(processing_version)s
                AND {alias}.domain_rule_version = %(domain_rule_version)s
                AND ({source_contract}))
            OR ({alias}.work_type = ANY(%(reconcile_work_types)s::text[])
                AND {alias}.processing_version = %(processing_version)s
                AND {alias}.domain_rule_version = %(domain_rule_version)s
                AND {alias}.analytics_rule_version = %(analytics_rule_version)s)
            OR ({alias}.work_type = ANY(%(build_work_types)s::text[])
                AND {alias}.processing_version = %(processing_version)s
                AND {alias}.domain_rule_version = %(domain_rule_version)s
                AND {alias}.analytics_rule_version = %(analytics_rule_version)s
                AND {coordinator_input_shape}
                AND (
                    {alias}.work_type = 'build_snapshot'
                    OR (
                        {alias}.work_type = 'build_analytics'
                        AND {analytics_input_shape}
                    )
                ))
            OR ({alias}.work_type = ANY(%(army_work_types)s::text[])
                AND {alias}.processing_version = %(processing_version)s
                AND {alias}.domain_rule_version = %(domain_rule_version)s
                AND {alias}.analytics_rule_version = %(army_analytics_rule_version)s
                AND (
                    {alias}.work_type = 'redecode_army'
                    OR (
                        {alias}.work_type = 'build_army_analytics'
                        AND {coordinator_input_shape}
                    )
                )))
            AND NOT COALESCE(
                {alias}.work_type IN ('build_snapshot', 'build_army_analytics')
                AND {alias}.input_json->>'boundary_at' < %(past_reset_build_hold)s::text,
                false)
            AND NOT (%(operator_build_hold)s
                AND {alias}.work_type IN
                    ('build_snapshot', 'build_analytics', 'build_army_analytics')
                AND {alias}.priority = {PYTHON_BACKFILL_PRIORITY}))
        """,
        {
            "source_work_types": list(SUPPORTED_WORK_TYPES[:2]),
            "processing_version": PROCESSING_VERSION,
            "domain_rule_version": DOMAIN_RULE_VERSION,
            **params,
            "reconcile_work_types": ["reconcile_ranked_day"],
            "analytics_rule_version": ANALYTICS_RULE_VERSION,
            "build_work_types": ["build_snapshot", "build_analytics"],
            "army_work_types": ["build_army_analytics", "redecode_army"],
            "army_analytics_rule_version": ARMY_ANALYTICS_RULE_VERSION,
            "past_reset_build_hold": past_reset_build_hold,
            "operator_build_hold": operator_build_hold,
        },
    )


# Claim probe candidate count per indexed range. The probes are refreshed on
# every claim, so a candidate locked by another lane is simply skipped; the
# next claim re-probes. Bounded like the collector claim statement.
# Cover the maximum supported in-process lane count. A smaller indexed window
# makes concurrent SKIP LOCKED claims collide on the same prefix and report an
# empty queue even while eligible work remains behind it.
_CLAIM_CANDIDATE_LIMIT = 32

# Priority classes that can appear in the Python queue. Backfill and day
# rechecks each have their own indexed class so their probes stay bounded
# without sharing live work's class, nor the catch-all's (migration 0093).
# The catch-all still claims other operator priorities. Its index also holds
# Reset work, so each side of Reset priority is its own probe that the index
# bounds by itself: NOT IN read 6,404 rows a claim at 05:50 on 8 Oct 2026.
_BACKGROUND = f"{PYTHON_BACKFILL_PRIORITY}, {DAY_RECHECK_PRIORITY}"
_PYTHON_CLAIM_PRIORITIES = (f"({PYTHON_BACKFILL_PRIORITY}), ({DAY_RECHECK_PRIORITY}),"
                            f" ({PYTHON_LIVE_PRIORITY}), ({PYTHON_RESET_PRIORITY})")
_PYTHON_OTHER_CLAIM_PRIORITIES = (
    f"job.priority NOT IN ({_BACKGROUND}, {PYTHON_LIVE_PRIORITY}) AND job.priority < {PYTHON_RESET_PRIORITY}",
    f"job.priority > {PYTHON_RESET_PRIORITY}",
)


def _claim_filters(
    *,
    supports_dependency: bool,
    denormalized_contract: bool,
    supports_coordinator: bool,
    work_types: Collection[str] | None,
    past_reset_build_hold: str | None,
    operator_build_hold: bool = False,
    backfill: Collection[int] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Claim alias ``job``'s supported filter, whole eligibility and parameters."""
    supported_filter, params = _supported_claim_filter(
        "job",
        "source_observation",
        denormalized_contract=denormalized_contract,
        supports_coordinator=supports_coordinator,
        past_reset_build_hold=past_reset_build_hold,
        operator_build_hold=operator_build_hold,
    )
    if work_types is not None:
        if not work_types or not set(work_types) <= set(SUPPORTED_WORK_TYPES):
            raise ValueError("claim work types must be supported work types")
        supported_filter = f"({supported_filter} AND job.work_type = ANY(%(claim_work_types)s::text[]))"
        params["claim_work_types"] = sorted(work_types)
    if backfill is not None:
        supported_filter = (f"({supported_filter} AND job.priority {'' if backfill else 'NOT '}"
                            f"IN ({', '.join(map(str, backfill)) or _BACKGROUND}))")
    dependency_filter = "job.state = 'waiting_dependency' OR " if supports_dependency else ""
    return supported_filter, f"""(((job.state IN ('pending', 'waiting_retry', 'waiting_dependency')
            AND job.due_at <= statement_timestamp())
        OR (job.state = 'leased'
            AND job.lease_expires_at <= statement_timestamp()))
        AND ({dependency_filter}job.attempt_count < job.max_attempts)
        AND {supported_filter})""", params


def _reset_waiting(jobs_relation: str, supported_filter: str) -> str:
    """Whether a claimable Reset-priority job passes ``supported_filter``.

    One check per claim index: one across all states read every finished
    Reset job first, 0.45 s a check at 05:47 on 8 Oct 2026.
    """
    due, tries = "job.due_at <= statement_timestamp()", "job.attempt_count < job.max_attempts"
    return "(" + " OR ".join(f"""EXISTS (SELECT FROM {jobs_relation} AS job
        LEFT JOIN collector_observations AS source_observation
            ON source_observation.id = COALESCE(job.observation_id, job.replay_observation_id)
        WHERE job.priority = {PYTHON_RESET_PRIORITY} AND {state} AND {supported_filter})""" for state in (
        f"job.state IN ('pending', 'waiting_retry') AND {due} AND {tries}",
        f"job.state = 'waiting_dependency' AND {due}",
        f"job.state = 'leased' AND job.lease_expires_at <= statement_timestamp() AND {tries}",
    )) + ")"


def _claim_select_statement(
    jobs_relation: str,
    *,
    job_id: int | None = None,
    job_ids: Collection[int] | None = None,
    limit: int = 1,
    planned: bool = False,
    supports_dependency: bool = True,
    denormalized_contract: bool = True,
    supports_coordinator: bool = False,
    work_types: Collection[str] | None = None,
    past_reset_build_hold: str | None = None,
    operator_build_hold: bool = False,
    reset_first: bool | None = None,
    backfill: Collection[int] | None = None,
) -> tuple[str, dict[str, Any]]:
    """The bounded claim SELECT for up to ``limit`` jobs and its named parameters.

    The candidate CTE probes indexed oldest-first ordinary and dependency
    ranges per declared priority, matching catch-all probes outside those classes
    (scored exactly like the known probes so ordering stays globally
    correct), and the indexed expired-lease set. Every probe applies the full
    supported claim filter against the already-joined observation, so an
    unsupported job at the head of a priority range never starves the
    supported jobs behind it, and locks the best still-available candidate
    with SKIP LOCKED. The candidate predicate is repeated at lock time so a
    row claimed by another lane between the probe and the lock is skipped,
    never double claimed. A direct ``job_ids`` claim replaces the probes with
    point lookups and still applies the same where and supported filters.
    ``work_types`` limits every probe and the lock-time recheck to those work
    types, so a limited worker never claims, and never skips over, other work.
    ``planned`` makes a ``job_ids`` claim refuse, and an ordinary one run only, while
    Reset-priority work it could take waits; ``reset_first`` puts that work first,
    False puts other due work but backfill before it, and None keeps waiting time.
    ``backfill`` probes and rechecks only those background priorities, which
    no other probe holds, and empty every other class.
    """
    supported_filter, claimable, params = _claim_filters(
        supports_dependency=supports_dependency,
        denormalized_contract=denormalized_contract,
        supports_coordinator=supports_coordinator,
        work_types=work_types,
        past_reset_build_hold=past_reset_build_hold,
        operator_build_hold=operator_build_hold,
        backfill=backfill,
    )
    priorities = _PYTHON_CLAIM_PRIORITIES if backfill is None else ", ".join(
        f"({priority})" for priority in backfill or (PYTHON_LIVE_PRIORITY, PYTHON_RESET_PRIORITY))
    params["claim_limit"] = limit
    if job_id is not None:
        job_ids = [job_id]
    if job_ids is not None:
        params["job_ids"] = list(job_ids)
    gate = "AND NOT " if job_ids is not None else "AND "
    reset_gate = gate + _reset_waiting(jobs_relation, supported_filter) if planned else ""
    first = {None: "", True: f"job.priority = {PYTHON_RESET_PRIORITY}, ", False: "job.priority"
             f" NOT IN ({PYTHON_RESET_PRIORITY}, {_BACKGROUND}), "}[reset_first]
    score = f"""{first}CASE WHEN job.priority IN ({_BACKGROUND})
        THEN 0 ELSE 1 END,
        CASE WHEN job.priority = {PYTHON_RESET_PRIORITY} THEN job.priority
        ELSE job.priority + floor(extract(epoch FROM (statement_timestamp() - job.created_at))
        / 60)::integer * 10 END"""
    dependency_column = "job.dependency_deferral_count" if supports_dependency else "0"
    ordinary_job_filter = f"""job.attempt_count < job.max_attempts
        AND {supported_filter}"""
    # Dependency resumptions do not consume the ordinary attempt budget. Keep
    # them in their own partial-index probe rather than expressing that rule as
    # an OR across every state, which makes PostgreSQL bitmap-scan the queue
    # before applying LIMIT.
    dependency_probe = ""
    if supports_dependency:
        dependency_probe = f"""
                    UNION ALL
                    (
                        SELECT job.id, job.due_at, job.created_at
                        FROM {jobs_relation} AS job
                        LEFT JOIN collector_observations AS source_observation
                            ON source_observation.id = COALESCE(
                                job.observation_id, job.replay_observation_id
                            )
                        WHERE job.state = 'waiting_dependency'
                          AND job.priority = claim_priority.priority
                          AND job.due_at <= statement_timestamp()
                          AND {supported_filter}
                        ORDER BY job.due_at, job.created_at, job.id
                        LIMIT {_CLAIM_CANDIDATE_LIMIT}
                    )
        """

    def other_priorities(state: str, job_filter: str) -> str:
        return "" if backfill else "".join(f"""
                UNION ALL
                (
                    SELECT job.id
                    FROM {jobs_relation} AS job
                    LEFT JOIN collector_observations AS source_observation
                        ON source_observation.id = COALESCE(
                            job.observation_id, job.replay_observation_id
                        )
                    WHERE {state}
                      AND {priority}
                      AND job.due_at <= statement_timestamp()
                      AND {job_filter}
                    ORDER BY job.due_at, job.created_at, job.id
                    LIMIT {_CLAIM_CANDIDATE_LIMIT}
                )""" for priority in _PYTHON_OTHER_CLAIM_PRIORITIES)

    unknown_dependency_probe = (
        other_priorities("job.state = 'waiting_dependency'", supported_filter)
        if supports_dependency else ""
    )
    if job_ids is not None:
        probe = f"""
            SELECT job.id
            FROM {jobs_relation} AS job
            LEFT JOIN collector_observations AS source_observation
                ON source_observation.id = COALESCE(
                    job.observation_id, job.replay_observation_id
                )
            WHERE job.id = ANY(%(job_ids)s::bigint[])
              AND {claimable}
              {reset_gate}
            ORDER BY ({score}) DESC, job.due_at, job.id
            FOR UPDATE OF job SKIP LOCKED
            LIMIT %(claim_limit)s
        """
    else:
        # The lock-time recheck is wrapped in IS TRUE so PostgreSQL cannot
        # answer it from a general queue index. Otherwise it may walk every
        # due-looking entry, including dead ones left before vacuum, instead
        # of looking up the few probed ids by primary key.
        probe = f"""
            SELECT pick.id
            FROM (
                SELECT claim_id.id
                FROM (VALUES {priorities}) AS claim_priority (priority)
                CROSS JOIN LATERAL (
                    SELECT eligible.id
                    FROM (
                        (
                            SELECT job.id, job.due_at, job.created_at
                            FROM {jobs_relation} AS job
                            LEFT JOIN collector_observations AS source_observation
                                ON source_observation.id = COALESCE(
                                    job.observation_id, job.replay_observation_id
                                )
                            WHERE job.state IN ('pending', 'waiting_retry')
                              AND job.priority = claim_priority.priority
                              AND job.due_at <= statement_timestamp()
                              AND {ordinary_job_filter}
                            ORDER BY job.due_at, job.created_at, job.id
                            LIMIT {_CLAIM_CANDIDATE_LIMIT}
                        )
                        {dependency_probe}
                    ) AS eligible
                    ORDER BY eligible.due_at, eligible.created_at, eligible.id
                    LIMIT {_CLAIM_CANDIDATE_LIMIT}
                ) AS claim_id
                {other_priorities("job.state IN ('pending', 'waiting_retry')", ordinary_job_filter)}
                {unknown_dependency_probe}
                UNION ALL
                (
                    SELECT job.id
                    FROM {jobs_relation} AS job
                    LEFT JOIN collector_observations AS source_observation
                        ON source_observation.id = COALESCE(
                            job.observation_id, job.replay_observation_id
                        )
                    WHERE job.state = 'leased'
                      AND job.lease_expires_at <= statement_timestamp()
                      AND {ordinary_job_filter}
                    ORDER BY job.lease_expires_at, job.due_at, job.created_at, job.id
                    LIMIT {_CLAIM_CANDIDATE_LIMIT}
                )
            ) AS pick
            JOIN {jobs_relation} AS job ON job.id = pick.id
            LEFT JOIN collector_observations AS source_observation
                ON source_observation.id = COALESCE(
                    job.observation_id, job.replay_observation_id
                )
            WHERE {claimable} IS TRUE
              {reset_gate}
            ORDER BY ({score}) DESC, job.due_at, job.id
            FOR UPDATE OF job SKIP LOCKED
            LIMIT %(claim_limit)s
        """
    return (
        f"""
        WITH candidate AS (
            {probe}
        )
        SELECT
            job.id AS job_id, job.work_type, job.deduplication_key,
            job.input_json,
            COALESCE(job.observation_id, job.replay_observation_id) AS observation_id,
            job.parser_version,
            job.processing_version, job.domain_rule_version,
            job.analytics_rule_version, job.attempt_count, job.max_attempts,
            job.state, {dependency_column},
            source_observation.normalized_tag, source_observation.endpoint,
            source_observation.endpoint_version, source_observation.schema_version,
            source_observation.response_observed_at, source_observation.http_status,
            source_observation.response_hash, source_observation.archive_reference
        FROM candidate
        JOIN {jobs_relation} AS job ON job.id = candidate.id
        LEFT JOIN collector_observations AS source_observation
            ON source_observation.id = COALESCE(
                job.observation_id, job.replay_observation_id
            )
        ORDER BY ({score}) DESC, job.due_at, job.id
        """,
        params,
    )


# The claim SELECT's columns in order, for tuple rows.
_CLAIM_COLUMNS = (
    "job_id", "work_type", "deduplication_key", "input_json", "observation_id",
    "parser_version", "processing_version", "domain_rule_version",
    "analytics_rule_version", "attempt_count", "max_attempts", "state",
    "dependency_deferral_count", "normalized_tag", "endpoint", "endpoint_version",
    "schema_version", "response_observed_at", "http_status", "response_hash",
    "archive_reference",
)


class LeaseLost(RuntimeError):
    """The claim no longer has a live owner/token fence."""


@dataclass(frozen=True, slots=True)
class Claim:
    job_id: int
    work_type: str
    deduplication_key: str
    input_json: dict[str, Any]
    observation_id: int | None
    attempt_id: int
    # attempt_number is the per-job python_processing_attempts sequence and
    # grows on every claim including dependency deferrals. attempt_count is
    # the ordinary retry budget counter; only it may exhaust max_attempts.
    attempt_number: int
    attempt_count: int
    normalized_tag: str | None
    endpoint: str | None
    endpoint_version: str | None
    schema_version: str | None
    observed_at: datetime | None
    http_status: int | None
    response_hash: str | None
    archive_reference: str | None
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    parser_version: str
    processing_version: str
    domain_rule_version: str
    analytics_rule_version: str
    max_attempts: int
    # True when this claim resumed waiting_dependency work: the run re-uses
    # its original ordinary slot instead of granting a new one.
    is_dependency_resume: bool = False


class Database:
    def __init__(
        self,
        database_url: str,
        *,
        max_size: int = DEFAULT_POOL_SIZE,
        expected_contract_version: int | None = None,
        player_discovery_enabled: bool = True,
        statement_timeout_seconds: int | None = None,
    ) -> None:
        if max_size < 1:
            raise ValueError("database pool size must be positive")
        if max_size > MAX_POOL_SIZE:
            raise ValueError("database pool size exceeds the supported maximum")
        self.stage_metrics: Any | None = None
        self.player_discovery_enabled = player_discovery_enabled

        def configure(connection: Any) -> None:
            connection.execute(
                f"SET statement_timeout = '{statement_timeout_seconds}s'"
            )
            connection.commit()

        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=max_size,
            open=True,
            configure=None if statement_timeout_seconds is None else configure,
        )
        with self.pool.connection() as connection:
            worker_view = connection.execute(
                """
                SELECT c.relkind
                FROM pg_class AS c
                WHERE c.oid = to_regclass('python_processing_jobs_worker')
                """
            ).fetchone()
            contract_version = connection.execute(
                "SELECT COALESCE((SELECT version FROM clash_lens_contract WHERE singleton), 0)"
            ).fetchone()[0]
        if worker_view is None or worker_view[0] not in {"v", b"v"}:
            self.pool.close()
            raise RuntimeError(
                "required python_processing_jobs_worker view is unavailable"
            )
        if (
            expected_contract_version is not None
            and int(contract_version) != expected_contract_version
        ):
            self.pool.close()
            raise RuntimeError(
                "compiled Python contract version does not match database"
            )
        self._jobs_relation = "python_processing_jobs_worker"
        self._contract_version = int(contract_version)
        self._ensure_dependency_support_probed()
        self._supports_coordinator_contract = self._contract_version >= 4

    def assert_contract_version(self, expected_contract_version: int) -> None:
        if self._contract_version != expected_contract_version:
            raise RuntimeError(
                "compiled Python contract version does not match database"
            )

    def _ensure_dependency_support_probed(self) -> None:
        """Probe dependency-deferral and storage-shape support once.

        Subclasses that replace ``__init__`` (for example the worker-role test
        database) skip the eager probe; claim paths resolve it on first use so
        they never touch a missing attribute.
        """
        if getattr(self, "_dependency_support_probed", False):
            return
        with self.pool.connection() as connection:
            dependency_column = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'python_processing_jobs_worker'
                      AND column_name = 'dependency_deferral_count'
                )
                """
            ).fetchone()[0]
            denormalized_contract = connection.execute(
                """
                SELECT count(*) = 3
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'python_processing_jobs_worker'
                  AND column_name IN ('endpoint', 'endpoint_version', 'schema_version')
                """
            ).fetchone()[0]
            content_dedup = connection.execute(
                """
                SELECT count(*) = 2
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'player_profile_versions'
                  AND column_name IN ('parsed_payload_id', 'semantic_projection')
                """
            ).fetchone()[0]
        self._supports_dependency_deferral = bool(dependency_column)
        self._supports_denormalized_contract = bool(denormalized_contract)
        self._supports_content_dedup = bool(content_dedup)
        with self.pool.connection() as connection:
            self._supports_compact_battles = connection.execute(
                "SELECT to_regclass('battle_payload_rows') IS NOT NULL"
            ).fetchone()[0]
            self._supports_season_summaries = connection.execute(
                "SELECT to_regclass('player_season_summaries') IS NOT NULL"
            ).fetchone()[0]
            self._supports_army_season_summaries = connection.execute(
                "SELECT to_regclass('army_season_summaries') IS NOT NULL"
            ).fetchone()[0]
        self._dependency_support_probed = True

    @contextmanager
    def _timed_connection(self):
        started_at = monotonic()
        with self.pool.connection() as connection:
            metrics = getattr(self, "stage_metrics", None)
            if metrics is not None:
                metrics.record("python_database_pool_acquire", monotonic() - started_at)
            yield connection

    def close(self) -> None:
        self.pool.close()

    def is_ready(self, *, expected_contract_version: int) -> bool:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT version
                FROM clash_lens_contract
                WHERE singleton = true
                """
            ).fetchone()
            return row is not None and int(row[0]) == expected_contract_version

    def validate_archive_instance(self, config: Any) -> bool:
        """Validate the immutable archive contract before local spool reuse."""
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT instance_id, endpoint, region, bucket, marker_key,
                       marker_hash, marker_payload_version
                FROM archive_instances
                WHERE instance_id = %s
                """,
                (config.instance_id,),
            ).fetchone()
        return row is not None and tuple(str(value) for value in row) == (
            config.instance_id,
            config.endpoint,
            config.region,
            config.bucket,
            config.marker_key,
            config.marker_hash,
            config.marker_payload_version,
        )

    def newest_job_plan(self, *, limit: int) -> list[int]:
        """Suggest newest eligible jobs using the rules in docs/architecture.md.

        ``claim_job(job_id=...)`` rechecks eligibility and lease state because
        a cached plan can become stale before its suggestions are claimed.
        """
        self._ensure_dependency_support_probed()
        supported_filter, supported_params = _supported_claim_filter(
            "job",
            "observation",
            denormalized_contract=self._supports_denormalized_contract,
            supports_coordinator=getattr(self, "_supports_coordinator_contract", False),
        )
        with self._timed_connection() as connection:
            rows = connection.execute(
                f"""
                WITH newest AS (
                    SELECT DISTINCT ON (observation.player_id, job.endpoint)
                           job.id, job.endpoint, observation.player_id
                    FROM {self._jobs_relation} AS job
                    JOIN collector_observations AS observation
                      ON observation.id = job.observation_id
                    WHERE job.state IN ('pending', 'waiting_retry')
                      AND job.priority = %(priority)s
                      AND job.due_at <= statement_timestamp()
                      AND job.work_type = 'process_observation'
                      AND job.endpoint IN ('profile', 'battle_log')
                      AND job.attempt_count < job.max_attempts
                      AND {supported_filter}
                    ORDER BY observation.player_id, job.endpoint,
                             observation.response_observed_at DESC, job.id DESC
                )
                SELECT newest.id
                FROM newest
                JOIN players AS player ON player.id = newest.player_id
                ORDER BY greatest(player.current_observed_at,
                                  player.current_profile_confirmed_at) NULLS FIRST,
                         player.id, newest.endpoint = 'profile' DESC
                LIMIT %(limit)s
                """,
                {**supported_params, "priority": PYTHON_LIVE_PRIORITY, "limit": limit},
            ).fetchall()
        return [int(row[0]) for row in rows]

    def queue_health(self) -> dict[str, Any]:
        # Overdue and scheduled-later work are counted apart: recalculations
        # queued a day ahead are not a backlog, and on 7 Oct 2026 21,887 of
        # them read as one. Overdue work is also counted by kind, so slow
        # daily results never hide behind fast responses; only overdue rows
        # are read for that, 53 ms on 8 Oct 2026.
        with self.pool.connection() as connection:
            row = connection.execute(
                f"""
                WITH active AS MATERIALIZED (
                    SELECT state, due_at, state <> 'leased' AND due_at <= clock_timestamp() AS overdue
                    FROM {self._jobs_relation}
                    WHERE state IN ('pending', 'waiting_retry', 'waiting_dependency', 'leased')
                ), failed AS (
                    SELECT count(*) AS failed_count
                    FROM (SELECT 1 FROM {self._jobs_relation} WHERE state = 'failed' LIMIT 1001) AS bounded_failed
                )
                SELECT
                    count(*) FILTER (WHERE state = 'pending'),
                    count(*) FILTER (WHERE state = 'waiting_retry'),
                    count(*) FILTER (WHERE state = 'waiting_dependency'),
                    count(*) FILTER (WHERE state = 'leased'),
                    (SELECT failed_count FROM failed),
                    extract(epoch FROM clock_timestamp() - min(due_at) FILTER (WHERE overdue)),
                    count(*) FILTER (WHERE overdue),
                    count(*) FILTER (WHERE state <> 'leased' AND NOT overdue)
                FROM active
                """
            ).fetchone()
            kinds = connection.execute(
                f"""
                SELECT CASE WHEN work_type = ANY(%s::text[]) THEN 'responses'
                            WHEN work_type = 'reconcile_ranked_day' THEN 'results'
                            WHEN work_type = ANY(%s::text[]) THEN 'builds'
                            ELSE 'other' END,
                       count(*), extract(epoch FROM clock_timestamp() - min(due_at))
                FROM {self._jobs_relation}
                WHERE state IN ('pending', 'waiting_retry', 'waiting_dependency')
                  AND due_at <= clock_timestamp()
                GROUP BY 1
                """,
                (list(RESPONSE_WORK_TYPES), list(POPULATION_BUILD_WORK_TYPES)),
            ).fetchall()
        assert row is not None
        return {
            "pending": int(row[0]),
            "waiting_retry": int(row[1]),
            "waiting_dependency": int(row[2]),
            "leased": int(row[3]),
            "failed": int(row[4]),
            "failed_count_capped": int(row[4]) == 1001,
            "oldest_due_seconds": None if row[5] is None else max(0.0, float(row[5])),
            "overdue": int(row[6]),
            "scheduled_later": int(row[7]),
            "kinds": {
                str(kind): {"overdue": int(count), "oldest_due_seconds": max(0.0, float(age))}
                for kind, count, age in kinds
            },
        }

    def pool_health(self) -> dict[str, int]:
        return database_pool_health(self.pool)

    def scalar(self, query: str, params: Iterable[Any] = ()) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return None if row is None else _text_value(row[0])

    def claim_job(
        self,
        *,
        owner: str,
        lease_seconds: int = 30,
        job_id: int | None = None,
        work_types: Collection[str] | None = None,
        planned: bool = False,
        reset_first: bool | None = None,
    ) -> Claim | None:
        """Claim the best due job, or ``job_id``; a ``planned`` one yields to Reset work."""
        claims = self.claim_jobs(
            owner=owner, lease_seconds=lease_seconds, work_types=work_types,
            job_ids=None if job_id is None else [job_id], planned=planned,
            reset_first=reset_first,
        )
        return claims[0] if claims else None

    def claim_jobs(
        self,
        *,
        owner: str,
        lease_seconds: int = 30,
        limit: int = 1,
        job_ids: Collection[int] | None = None,
        work_types: Collection[str] | None = None,
        planned: bool = False,
        reset_first: bool | None = None,
    ) -> list[Claim]:
        """Claim up to ``limit`` of the best due jobs, or of ``job_ids``, at once.

        One transaction leases them all, but each job gets its own token and
        attempt, exactly as if it were claimed alone. No population build is
        claimed while another runs or waits to start, in any worker process.
        """
        if not owner:
            raise ValueError("lease owner is required")
        if lease_seconds <= 0 or limit < 1:
            raise ValueError("lease duration and claim limit must be positive")
        with self._timed_connection() as connection, connection.transaction():
            self._ensure_dependency_support_probed()
            supports_coordinator = getattr(self, "_supports_coordinator_contract", False)
            builds = set(work_types or SUPPORTED_WORK_TYPES) & set(POPULATION_BUILD_WORK_TYPES)
            if builds and build_permit_busy(connection, self._jobs_relation, builds):
                work_types = [kind for kind in work_types or SUPPORTED_WORK_TYPES
                              if kind not in builds]
                if not work_types:
                    return []
            options = {
                "supports_dependency": self._supports_dependency_deferral,
                "denormalized_contract": self._supports_denormalized_contract,
                "supports_coordinator": supports_coordinator,
                "work_types": work_types,
                "reset_first": reset_first,
                "limit": limit,
                "past_reset_build_hold": (
                    past_reset_build_hold(connection) if supports_coordinator else None
                ),
                "operator_build_hold": (
                    supports_coordinator and operator_correction_waits(connection)
                ),
            }
            rows = [] if job_ids is None else connection.execute(*_claim_select_statement(
                self._jobs_relation, job_ids=job_ids, planned=planned, **options
            )).fetchall()
            if job_ids is None or (not rows and planned):
                rows = connection.execute(*_claim_select_statement(
                    self._jobs_relation, planned=planned, backfill=(), **options
                )).fetchall()
                # Backfill always comes last; see background_pacing.
                lanes = () if len(rows) >= limit or planned else background_lanes(
                    connection, self._jobs_relation, self._supports_denormalized_contract,
                    supports_coordinator, self._supports_dependency_deferral,
                )
                rows += connection.execute(*_claim_select_statement(
                    self._jobs_relation, backfill=lanes, **{**options, "limit": 1}
                )).fetchall() if lanes else []
            return self._lease_rows(connection, rows, owner, lease_seconds)

    def _lease_rows(
        self, connection: Any, rows: list[Any], owner: str, lease_seconds: int
    ) -> list[Claim]:
        if not rows:
            return []
        jobs = [dict(row) if isinstance(row, dict) else dict(zip(_CLAIM_COLUMNS, row))
                for row in rows]
        ids = [int(job["job_id"]) for job in jobs]
        tokens = [uuid4().hex for _ in jobs]
        # A dependency resumption reuses its ordinary attempt slot.
        resumes = [self._supports_dependency_deferral
                   and _text_value(job["state"]) == "waiting_dependency" for job in jobs]
        previous = dict(connection.execute(
            "SELECT job_id, max(attempt_number) FROM python_processing_attempts"
            " WHERE job_id = ANY(%s::bigint[]) GROUP BY job_id", (ids,),
        ).fetchall())
        # Stale marking keys on the attempts sequence, not the retry budget:
        # dependency deferrals leave attempt_count untouched but their
        # abandoned running rows must still be closed out.
        if previous:
            connection.execute(
                """
                UPDATE python_processing_attempts
                SET state = 'stale', completed_at = clock_timestamp(),
                    failure_category = COALESCE(failure_category, 'lease_expired')
                WHERE job_id = ANY(%s::bigint[]) AND state = 'running'
                """,
                (list(previous),),
            )
        expires = dict(connection.execute(
            f"""
            UPDATE {self._jobs_relation} AS job
            SET state = 'leased', lease_owner = %s, lease_token = claim.token,
                lease_expires_at = clock_timestamp() + (%s * interval '1 second'),
                attempt_count = job.attempt_count + CASE WHEN claim.resume THEN 0 ELSE 1 END,
                updated_at = clock_timestamp()
            FROM unnest(%s::bigint[], %s::text[], %s::boolean[]) AS claim (id, token, resume)
            WHERE job.id = claim.id
            RETURNING job.id, job.lease_expires_at
            """,
            (owner, lease_seconds, ids, tokens, resumes),
        ).fetchall())
        numbers = [int(previous.get(job_id) or 0) + 1 for job_id in ids]
        attempts = dict(connection.execute(
            """
            INSERT INTO python_processing_attempts (
                job_id, attempt_number, lease_owner, lease_token,
                started_at, lease_expires_at, state
            )
            SELECT claim.id, claim.number, %s, claim.token, clock_timestamp(),
                   claim.expires, 'running'
            FROM unnest(%s::bigint[], %s::integer[], %s::text[], %s::timestamptz[])
                AS claim (id, number, token, expires)
            RETURNING job_id, id
            """,
            (owner, ids, numbers, tokens, [expires[job_id] for job_id in ids]),
        ).fetchall())

        def optional(job: dict[str, Any], name: str, kind: Any = _text_value) -> Any:
            return None if job[name] is None else kind(job[name])

        return [
            Claim(
                job_id=job_id,
                work_type=_text_value(job["work_type"]),
                deduplication_key=_text_value(job["deduplication_key"]),
                input_json=dict(job["input_json"]),
                observation_id=optional(job, "observation_id", int),
                attempt_id=int(attempts[job_id]),
                attempt_number=number,
                attempt_count=int(job["attempt_count"]),
                is_dependency_resume=resume,
                normalized_tag=optional(job, "normalized_tag"),
                endpoint=optional(job, "endpoint"),
                endpoint_version=optional(job, "endpoint_version"),
                schema_version=optional(job, "schema_version"),
                observed_at=job["response_observed_at"],
                http_status=optional(job, "http_status", int),
                response_hash=optional(job, "response_hash"),
                archive_reference=optional(job, "archive_reference"),
                lease_owner=owner,
                lease_token=token,
                lease_expires_at=expires[job_id],
                parser_version=_text_value(job["parser_version"]),
                processing_version=_text_value(job["processing_version"]),
                domain_rule_version=_text_value(job["domain_rule_version"]),
                analytics_rule_version=_text_value(job["analytics_rule_version"]),
                max_attempts=int(job["max_attempts"]),
            )
            for job, job_id, token, resume, number in zip(
                jobs, ids, tokens, resumes, numbers, strict=True
            )
        ]

    def release_claims(self, claims: Collection[Claim]) -> int:
        """Give back claims whose work never started, as if never claimed.

        Each job returns to the state its claim found it in, keeps its
        attempt budget and is claimable at once; its attempt is marked stale.
        The owner and token fence it, so a job another worker or queue
        maintenance has taken since stays theirs.
        """
        if not claims:
            return 0
        with self._timed_connection() as connection, connection.transaction():
            released = connection.execute(
                f"""
                UPDATE {self._jobs_relation} AS job
                SET state = CASE WHEN claim.resume THEN 'waiting_dependency' ELSE 'pending' END,
                    attempt_count = claim.attempt_count,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::integer[], %s::boolean[])
                    AS claim (id, owner, token, attempt_count, resume)
                WHERE job.id = claim.id AND job.state = 'leased'
                  AND job.lease_owner = claim.owner AND job.lease_token = claim.token
                RETURNING job.id
                """,
                tuple(map(list, zip(*(
                    (claim.job_id, claim.lease_owner, claim.lease_token,
                     claim.attempt_count, claim.is_dependency_resume) for claim in claims
                ), strict=True))),
            ).fetchall()
            connection.execute(
                """
                UPDATE python_processing_attempts
                SET state = 'stale', completed_at = clock_timestamp(),
                    failure_category = 'claim_released'
                WHERE id = ANY(%s::bigint[]) AND state = 'running'
                """,
                ([claim.attempt_id for claim in claims
                  if (claim.job_id,) in released],),
            )
        return len(released)

    def refund_claim_attempt(self, claim: Claim) -> None:
        """Keep a conflicted failure write recoverable without releasing its lease.

        The owner and token fence the refund even after the lease expires: any
        new claim replaces the token and queue maintenance clears it, so an
        expired worker can only refund a job nobody else has taken.
        """
        with self._timed_connection() as connection:
            with connection.transaction():
                # Never queue behind a long holder of the job row; the lease
                # running out recovers the job instead.
                connection.execute("SET LOCAL lock_timeout = '1s'")
                refunded = connection.execute(
                    f"""
                    UPDATE {self._jobs_relation}
                    SET attempt_count = LEAST(attempt_count, %s),
                        updated_at = clock_timestamp()
                    WHERE id = %s AND state = 'leased'
                      AND lease_owner = %s AND lease_token = %s
                    """,
                    (
                        min(claim.attempt_count, claim.max_attempts - 1),
                        claim.job_id,
                        claim.lease_owner,
                        claim.lease_token,
                    ),
                )
                if refunded.rowcount != 1:
                    raise LeaseLost("job lease was lost while refunding its attempt")

    def finished_attempt(
        self, claim: Claim
    ) -> tuple[str, str | None, str | None] | None:
        """This claim's saved attempt state, outcome and failure category, or
        None while nothing committed it.

        A session that ends while committing leaves the outcome unknown, so
        the saved attempt decides whether its work may run again.
        """
        with self._timed_connection() as connection:
            row = connection.execute(
                """
                SELECT state, outcome, failure_category
                FROM python_processing_attempts
                WHERE id = %s AND job_id = %s AND lease_token = %s
                """,
                (claim.attempt_id, claim.job_id, claim.lease_token),
            ).fetchone()
            connection.commit()
        if row is None or row[0] == "running":
            return None
        return str(row[0]), row[1], row[2]

    def maintain_queue(self, *, max_jobs: int = 100) -> int:
        """Recover a bounded set of expired worker leases.

        This is intentionally separate from ``claim_job`` so ordinary claims
        stay constant-cost at queue depth. Unsupported work is released for a
        future worker image; supported work is requeued unless it exhausted
        its durable attempt limit, in which case it is terminalized.
        """
        if max_jobs < 1:
            raise ValueError("maintenance job limit must be positive")
        self._ensure_dependency_support_probed()
        supported_filter, supported_params = _supported_job_filter(
            "job",
            denormalized_contract=self._supports_denormalized_contract,
            supports_coordinator=getattr(self, "_supports_coordinator_contract", False),
        )
        with self._timed_connection() as connection:
            with connection.transaction():
                rows = connection.execute(
                    f"""
                    SELECT job.id, ({supported_filter}) AS supported,
                           job.attempt_count >= job.max_attempts AS exhausted
                    FROM {self._jobs_relation} AS job
                    WHERE job.state = 'leased'
                      AND job.lease_expires_at <= clock_timestamp()
                    ORDER BY job.lease_expires_at, job.id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT %s
                    """,
                    (*supported_params, max_jobs),
                ).fetchall()
                if not rows:
                    return 0
                job_ids = [int(row[0]) for row in rows]
                pending_ids = [
                    int(row[0]) for row in rows if not bool(row[1]) or not bool(row[2])
                ]
                failed_ids = [
                    int(row[0]) for row in rows if bool(row[1]) and bool(row[2])
                ]
                connection.execute(
                    """
                    UPDATE python_processing_attempts
                    SET state = 'stale', completed_at = clock_timestamp(),
                        failure_category = COALESCE(failure_category, 'lease_expired')
                    WHERE job_id = ANY(%s::bigint[]) AND state = 'running'
                    """,
                    (job_ids,),
                )
                if pending_ids:
                    connection.execute(
                        f"""
                        UPDATE {self._jobs_relation}
                        SET state = 'pending', lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, updated_at = clock_timestamp()
                        WHERE id = ANY(%s::bigint[])
                        """,
                        (pending_ids,),
                    )
                if failed_ids:
                    connection.execute(
                        f"""
                        UPDATE {self._jobs_relation}
                        SET state = 'failed', outcome = 'durable_failure',
                            failure_category = 'lease_expired_max_attempts',
                            failure_detail = 'lease expired after the configured attempt limit',
                            lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL,
                            completed_at = clock_timestamp(),
                            updated_at = clock_timestamp()
                        WHERE id = ANY(%s::bigint[])
                        """,
                        (failed_ids,),
                    )
                return len(job_ids)

    def renew_claim(self, claim: Claim, *, lease_seconds: int, always: bool = False) -> None:
        """Raise LeaseLost unless the claim still holds the job; extend it if due.

        Unless ``always``, a lease with at least half of ``lease_seconds`` left
        is only checked: each rewrite changed the job row and its lookup lists,
        and the worker renews a response's job just after claiming it.
        """
        if lease_seconds <= 0:
            raise ValueError("lease duration must be positive")
        live = """id = %s AND state = 'leased' AND lease_owner = %s
                      AND lease_token = %s AND lease_expires_at > clock_timestamp()"""
        fence = (claim.job_id, claim.lease_owner, claim.lease_token)
        with self._timed_connection() as connection:
            with connection.transaction():
                due = connection.execute(
                    f"""SELECT lease_expires_at < clock_timestamp() + (%s * interval '0.5 second')
                    FROM {self._jobs_relation} WHERE {live}""",
                    (lease_seconds, *fence),
                ).fetchone()
                if due is None:
                    raise LeaseLost("job lease could not be renewed")
                if not due[0] and not always:
                    return
                renewed = connection.execute(
                    f"""
                    UPDATE {self._jobs_relation}
                    SET lease_expires_at = clock_timestamp() + (%s * interval '1 second')
                    WHERE {live}
                    RETURNING lease_expires_at
                    """,
                    (lease_seconds, *fence),
                ).fetchone()
                if renewed is None:
                    raise LeaseLost("job lease could not be renewed")
                attempt = connection.execute(
                    """
                    UPDATE python_processing_attempts
                    SET lease_expires_at = %s
                    WHERE id = %s AND job_id = %s AND state = 'running'
                      AND lease_owner = %s AND lease_token = %s
                    """,
                    (renewed[0], claim.attempt_id, *fence),
                )
                if attempt.rowcount != 1:
                    raise LeaseLost("processing attempt lease could not be renewed")

    def requeue_completed_job(self, job_id: int) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                f"""
                UPDATE {self._jobs_relation}
                SET state = 'pending', due_at = clock_timestamp(), outcome = NULL,
                    failure_category = NULL, failure_detail = NULL,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    completed_at = NULL, updated_at = clock_timestamp()
                WHERE id = %s AND state = 'complete'
                """,
                (job_id,),
            )
            connection.commit()

    def expire_lease(self, job_id: int) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                f"""
                UPDATE {self._jobs_relation}
                SET lease_expires_at = clock_timestamp() - interval '1 second'
                WHERE id = %s AND state = 'leased'
                """,
                (job_id,),
            )
            connection.commit()

    def _lock_live_claim(
        self, connection: Any, claim: Claim, *, build: bool = False
    ) -> dict[str, Any]:
        """Lock a live claim's job row; a population ``build`` also takes the permit."""
        row = connection.execute(
            f"""
            SELECT id, attempt_count, max_attempts
            FROM {self._jobs_relation}
            WHERE id = %s AND state = 'leased'
              AND lease_owner = %s AND lease_token = %s
              AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (claim.job_id, claim.lease_owner, claim.lease_token),
        ).fetchone()
        if row is None:
            raise LeaseLost("job lease is missing, stale, or owned by another worker")
        if build:
            take_build_permit(connection)
        return {"id": row[0], "attempt_count": row[1], "max_attempts": row[2]}

    def _finish_claim(
        self,
        connection: Any,
        claim: Claim,
        job: dict[str, Any],
        *,
        state: str,
        outcome: str,
    ) -> None:
        attempt = connection.execute(
            """
            UPDATE python_processing_attempts
            SET state = %s, completed_at = clock_timestamp(), outcome = %s
            WHERE id = %s AND job_id = %s AND lease_owner = %s AND lease_token = %s
            """,
            (
                "complete" if state == "complete" else "failed",
                outcome,
                claim.attempt_id,
                claim.job_id,
                claim.lease_owner,
                claim.lease_token,
            ),
        )
        if attempt.rowcount != 1:
            raise LeaseLost("processing attempt fence was lost")
        # Every caller locked this row with _lock_live_claim while its lease
        # was live. Claims and queue maintenance skip locked rows, so nothing
        # can take the job while this transaction works, however long that
        # takes. Checking the clock here only threw away finished work.
        completed = connection.execute(
            f"""
            UPDATE {self._jobs_relation}
            SET state = %s, outcome = %s, failure_category = NULL, failure_detail = NULL,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                completed_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id = %s AND state = 'leased'
              AND lease_owner = %s AND lease_token = %s
            """,
            (state, outcome, claim.job_id, claim.lease_owner, claim.lease_token),
        )
        if completed.rowcount != 1:
            raise LeaseLost("job completion fence was lost")


@contextmanager
def lock_wait(connection: Any, wait: str | None) -> Iterator[None]:
    """Limit lock waits inside the block, then restore the caller's limit.

    A lock that is not free in time raises LockNotAvailable; rolling back
    the transaction then also restores the caller's limit.
    """
    if wait is None:
        yield
        return
    previous = connection.execute("SELECT current_setting('lock_timeout')").fetchone()[0]
    connection.execute("SELECT set_config('lock_timeout', %s, true)", (wait,))
    yield
    connection.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))


def enqueue_discovered_players(
    connection: Any, database: Database, claim: Claim, player_ids: Iterable[int]
) -> None:
    """Save each new player named by a battle log or ranking as due a profile check.

    Players already tracked or already given this week's check are skipped
    before anything else. The rest are saved as due (migration 0084); the
    collector turns due players into checks while fewer than 500 wait, so a
    full queue delays a player instead of dropping it. Waiting over a second
    for a player another job is updating raises LockNotAvailable, so the
    whole job rolls back and reruns.
    """
    if not database.player_discovery_enabled or claim.work_type != "process_observation":
        return
    # The week key matches clashlens_eligibility_week, which this role cannot run.
    candidates = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT player.id FROM players AS player
            WHERE player.id = ANY(%s::bigint[])
              AND (NOT player.active OR player.eligibility_state <> 'eligible')
              AND player.eligibility_due_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM collector_work AS work
                  WHERE work.kind = 'discovery_profile'
                    AND work.coalescing_key = 'discovery-profile:' || player.id || ':'
                      || (SELECT to_char(date_bin(interval '7 days', clock_timestamp(),
                                                  timestamptz '2000-01-03 05:00:00+00')
                                         AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')))
            ORDER BY player.id
            """,
            (sorted(set(player_ids)),),
        )
    ]
    if candidates:
        with lock_wait(connection, "1s"):
            connection.execute(
                "SELECT clashlens_mark_eligibility_due(%s::bigint[], clock_timestamp())",
                (candidates,),
            )


def _text_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value
