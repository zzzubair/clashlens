from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from psycopg_pool import ConnectionPool

from .battle import SOURCE_PARSER_VERSION
from .operating import database_pool_health
from .source_observation_contract import SOURCE_OBSERVATION_CONTRACTS

PROCESSING_VERSION = "clashlens-domain-processing-v1"
DEFAULT_PARSER_VERSION = SOURCE_PARSER_VERSION
DOMAIN_RULE_VERSION = "clashlens-domain-rules-v1"
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
ARMY_ANALYTICS_RULE_VERSION = "army-analytics-v2"
CONTRACT_VERSION = 5
PYTHON_BACKFILL_PRIORITY = 25
PYTHON_LIVE_PRIORITY = 100
DEFAULT_POOL_SIZE = 4
MAX_POOL_SIZE = 64

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
) -> tuple[str, dict[str, Any]]:
    """Parameterized supported-job predicate for the claim SELECT.

    Identical contract to ``_supported_job_filter`` (used by the cleanup
    UPDATE paths, which have no observation join): both read the denormalized
    endpoint/schema contract columns on the job row itself, so neither needs
    to reference collector_observations and claim plans stay bounded.
    Parameters are named so the claim statement can
    also bind the claim time and direct job id.
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
    return (
        f"""(
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
        },
    )

# Claim probe candidate count per indexed range. The probes are refreshed on
# every claim, so a candidate locked by another lane is simply skipped; the
# next claim re-probes. Bounded like the collector claim statement.
# Cover the maximum supported in-process lane count. A smaller indexed window
# makes concurrent SKIP LOCKED claims collide on the same prefix and report an
# empty queue even while eligible work remains behind it.
_CLAIM_CANDIDATE_LIMIT = 32

# Priority classes that can appear in the Python queue. Backfill has its own
# indexed class so its probe stays bounded without sharing live work's class.
# The catch-all still claims other operator priorities.
_PYTHON_CLAIM_PRIORITIES = f"({PYTHON_BACKFILL_PRIORITY}), ({PYTHON_LIVE_PRIORITY})"
_PYTHON_CLAIM_PRIORITY_EXCLUSIONS = f"{PYTHON_BACKFILL_PRIORITY}, {PYTHON_LIVE_PRIORITY}"

def _claim_select_statement(
    jobs_relation: str,
    *,
    job_id: int | None = None,
    supports_dependency: bool = True,
    denormalized_contract: bool = True,
    supports_coordinator: bool = False,
) -> tuple[str, dict[str, Any]]:
    """The bounded claim SELECT and its named parameters.

    The candidate CTE probes indexed oldest-first ordinary and dependency
    ranges per declared priority, matching catch-all probes outside those classes
    (scored exactly like the known probes so ordering stays globally
    correct), and the indexed expired-lease set. Every probe applies the full
    supported claim filter against the already-joined observation, so an
    unsupported job at the head of a priority range never starves the
    supported jobs behind it, and locks the best still-available candidate
    with SKIP LOCKED. The candidate predicate is repeated at lock time so a
    row claimed by another lane between the probe and the lock is skipped,
    never double claimed. A direct ``job_id`` claim replaces the probes with
    a point lookup and still applies the same where and supported filters.
    """
    supported_filter, supported_params = _supported_claim_filter(
        "job",
        "source_observation",
        denormalized_contract=denormalized_contract,
        supports_coordinator=supports_coordinator,
    )
    params: dict[str, Any] = {**supported_params}
    if job_id is not None:
        params["job_id"] = job_id
    score = f"""CASE WHEN job.priority = {PYTHON_BACKFILL_PRIORITY}
        THEN 0 ELSE 1 END,
        job.priority + floor(extract(epoch FROM (statement_timestamp() - job.created_at))
        / 60)::integer * 10"""
    due = """(job.state IN ('pending', 'waiting_retry', 'waiting_dependency')
            AND job.due_at <= statement_timestamp())
        OR (job.state = 'leased'
            AND job.lease_expires_at <= statement_timestamp())"""
    # Generation 2 is the parser-v2 rollout fence. The previous image claims
    # only generation 1, so it cannot interpret new v2 rows with its old
    # adapter during a staggered deployment. This image retains generation 1
    # for queued work and deterministic v1 replay.
    dependency_filter = (
        "job.state = 'waiting_dependency' OR " if supports_dependency else ""
    )
    dependency_column = "job.dependency_deferral_count" if supports_dependency else "0"
    # Generation 6 is the league-history parser fence: older images cannot
    # interpret those rows, and this image keeps 1..5 for queued and replay work.
    claim_versions = "1, 2, 3, 4, 5, 6" if supports_coordinator else "1, 2, 3"
    job_filter = f"""job.claim_compatibility_version IN ({claim_versions})
        AND ({dependency_filter}job.attempt_count < job.max_attempts)
        AND {supported_filter}"""
    ordinary_job_filter = f"""job.claim_compatibility_version IN ({claim_versions})
        AND job.attempt_count < job.max_attempts
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
                          AND job.claim_compatibility_version IN ({claim_versions})
                          AND {supported_filter}
                        ORDER BY job.due_at, job.created_at, job.id
                        LIMIT {_CLAIM_CANDIDATE_LIMIT}
                    )
        """
    unknown_dependency_probe = ""
    if supports_dependency:
        unknown_dependency_probe = f"""
                UNION ALL
                (
                    SELECT job.id
                    FROM {jobs_relation} AS job
                    LEFT JOIN collector_observations AS source_observation
                        ON source_observation.id = COALESCE(
                            job.observation_id, job.replay_observation_id
                        )
                    WHERE job.state = 'waiting_dependency'
                      AND job.priority NOT IN ({_PYTHON_CLAIM_PRIORITY_EXCLUSIONS})
                      AND job.due_at <= statement_timestamp()
                      AND job.claim_compatibility_version IN ({claim_versions})
                      AND {supported_filter}
                    ORDER BY job.due_at, job.created_at, job.id
                    LIMIT {_CLAIM_CANDIDATE_LIMIT}
                )
        """
    if job_id is not None:
        probe = f"""
            SELECT job.id
            FROM {jobs_relation} AS job
            LEFT JOIN collector_observations AS source_observation
                ON source_observation.id = COALESCE(
                    job.observation_id, job.replay_observation_id
                )
            WHERE job.id = %(job_id)s
              AND ({due})
              AND {job_filter}
            ORDER BY ({score}) DESC, job.due_at, job.id
            FOR UPDATE OF job SKIP LOCKED
            LIMIT 1
        """
    else:
        probe = f"""
            SELECT pick.id
            FROM (
                SELECT claim_id.id
                FROM (VALUES {_PYTHON_CLAIM_PRIORITIES}) AS claim_priority (priority)
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
                UNION ALL
                (
                    SELECT job.id
                    FROM {jobs_relation} AS job
                    LEFT JOIN collector_observations AS source_observation
                        ON source_observation.id = COALESCE(
                            job.observation_id, job.replay_observation_id
                        )
                    WHERE job.state IN ('pending', 'waiting_retry')
                      AND job.priority NOT IN ({_PYTHON_CLAIM_PRIORITY_EXCLUSIONS})
                      AND job.due_at <= statement_timestamp()
                      AND {ordinary_job_filter}
                    ORDER BY job.due_at, job.created_at, job.id
                    LIMIT {_CLAIM_CANDIDATE_LIMIT}
                )
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
            WHERE ({due})
              AND {job_filter}
            ORDER BY ({score}) DESC, job.due_at, job.id
            FOR UPDATE OF job SKIP LOCKED
            LIMIT 1
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
        """,
        params,
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
    ) -> None:
        if max_size < 1:
            raise ValueError("database pool size must be positive")
        if max_size > MAX_POOL_SIZE:
            raise ValueError("database pool size exceeds the supported maximum")
        self.stage_metrics: Any | None = None
        self.player_discovery_enabled = player_discovery_enabled
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=max_size,
            open=True,
        )
        with self.pool.connection() as connection:
            worker_view = connection.execute(
                """
                SELECT c.relkind
                FROM pg_class AS c
                WHERE c.oid = to_regclass('python_processing_jobs_worker')
                """
            ).fetchone()
            missing_worker_view = worker_view is None or worker_view[0] not in {
                "v",
                b"v",
            }
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
            contract_version = connection.execute(
                "SELECT COALESCE((SELECT version FROM clash_lens_contract WHERE singleton), 0)"
            ).fetchone()[0]
        if missing_worker_view:
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
        self._supports_coordinator_contract = self._contract_version >= 4
        self._dependency_support_probed = True

    def assert_contract_version(self, expected_contract_version: int) -> None:
        if self._contract_version != expected_contract_version:
            raise RuntimeError(
                "compiled Python contract version does not match database"
            )

    def _ensure_dependency_support_probed(self) -> None:
        """Probe dependency-deferral support once, lazily.

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

    def queue_health(self) -> dict[str, bool | int | float | None]:
        with self.pool.connection() as connection:
            row = connection.execute(
                f"""
                WITH active AS MATERIALIZED (
                    SELECT state, due_at
                    FROM {self._jobs_relation}
                    WHERE state IN ('pending', 'waiting_retry', 'waiting_dependency', 'leased')
                ), failed AS (
                    SELECT count(*) AS failed_count
                    FROM (
                        SELECT 1
                        FROM {self._jobs_relation}
                        WHERE state = 'failed'
                        LIMIT 1001
                    ) AS bounded_failed
                )
                SELECT
                    count(*) FILTER (WHERE state = 'pending'),
                    count(*) FILTER (WHERE state = 'waiting_retry'),
                    count(*) FILTER (WHERE state = 'waiting_dependency'),
                    count(*) FILTER (WHERE state = 'leased'),
                    (SELECT failed_count FROM failed),
                    extract(
                        epoch FROM clock_timestamp() - min(due_at) FILTER (
                            WHERE state IN ('pending', 'waiting_retry', 'waiting_dependency')
                              AND due_at <= clock_timestamp()
                        )
                    )
                FROM active
                """
            ).fetchone()
        assert row is not None
        return {
            "pending": int(row[0]),
            "waiting_retry": int(row[1]),
            "waiting_dependency": int(row[2]),
            "leased": int(row[3]),
            "failed": int(row[4]),
            "failed_count_capped": int(row[4]) == 1001,
            "oldest_due_seconds": None if row[5] is None else max(0.0, float(row[5])),
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
    ) -> Claim | None:
        if not owner:
            raise ValueError("lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._timed_connection() as connection:
            with connection.transaction():
                self._ensure_dependency_support_probed()
                claim_statement, claim_params = _claim_select_statement(
                    self._jobs_relation,
                    job_id=job_id,
                    supports_dependency=self._supports_dependency_deferral,
                    denormalized_contract=self._supports_denormalized_contract,
                    supports_coordinator=getattr(
                        self, "_supports_coordinator_contract", False
                    ),
                )
                row = connection.execute(claim_statement, claim_params).fetchone()
                if row is None:
                    return None
                data = (
                    dict(row)
                    if isinstance(row, dict)
                    else {
                        "job_id": row[0],
                        "work_type": row[1],
                        "deduplication_key": row[2],
                        "input_json": row[3],
                        "observation_id": row[4],
                        "parser_version": row[5],
                        "processing_version": row[6],
                        "domain_rule_version": row[7],
                        "analytics_rule_version": row[8],
                        "attempt_count": row[9],
                        "max_attempts": row[10],
                        "state": row[11],
                        "dependency_deferral_count": row[12],
                        "normalized_tag": row[13],
                        "endpoint": row[14],
                        "endpoint_version": row[15],
                        "schema_version": row[16],
                        "response_observed_at": row[17],
                        "http_status": row[18],
                        "response_hash": row[19],
                        "archive_reference": row[20],
                    }
                )
                token = uuid4().hex
                dependency_claim = (
                    self._supports_dependency_deferral
                    and _text_value(data["state"]) == "waiting_dependency"
                )
                attempt_number = int(
                    connection.execute(
                        "SELECT COALESCE(max(attempt_number), 0) + 1 FROM python_processing_attempts WHERE job_id = %s",
                        (data["job_id"],),
                    ).fetchone()[0]
                )
                # Stale marking keys on the attempts sequence, not the retry
                # budget: dependency deferrals leave attempt_count untouched
                # but their abandoned running rows must still be closed out.
                if attempt_number > 1:
                    connection.execute(
                        """
                        UPDATE python_processing_attempts
                        SET state = 'stale', completed_at = clock_timestamp(),
                            failure_category = COALESCE(failure_category, 'lease_expired')
                        WHERE job_id = %s AND state = 'running'
                        """,
                        (data["job_id"],),
                    )
                leased = connection.execute(
                    f"""
                    UPDATE {self._jobs_relation}
                    SET state = 'leased', lease_owner = %s, lease_token = %s,
                        lease_expires_at = clock_timestamp() + (%s * interval '1 second'),
                        attempt_count = attempt_count + CASE WHEN %s THEN 0 ELSE 1 END,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                    RETURNING lease_expires_at
                    """,
                    (owner, token, lease_seconds, dependency_claim, data["job_id"]),
                ).fetchone()
                assert leased is not None
                attempt = connection.execute(
                    """
                    INSERT INTO python_processing_attempts (
                        job_id, attempt_number, lease_owner, lease_token,
                        started_at, lease_expires_at, state
                    ) VALUES (%s, %s, %s, %s, clock_timestamp(), %s, 'running')
                    RETURNING id, started_at, lease_expires_at
                    """,
                    (data["job_id"], attempt_number, owner, token, leased[0]),
                ).fetchone()
                assert attempt is not None
                return Claim(
                    job_id=int(data["job_id"]),
                    work_type=_text_value(data["work_type"]),
                    deduplication_key=_text_value(data["deduplication_key"]),
                    input_json=dict(data["input_json"]),
                    observation_id=(
                        int(data["observation_id"])
                        if data["observation_id"] is not None
                        else None
                    ),
                    attempt_id=int(attempt[0]),
                    attempt_number=attempt_number,
                    attempt_count=int(data["attempt_count"]),
                    is_dependency_resume=dependency_claim,
                    normalized_tag=(
                        _text_value(data["normalized_tag"])
                        if data["normalized_tag"] is not None
                        else None
                    ),
                    endpoint=(
                        _text_value(data["endpoint"])
                        if data["endpoint"] is not None
                        else None
                    ),
                    endpoint_version=(
                        _text_value(data["endpoint_version"])
                        if data["endpoint_version"] is not None
                        else None
                    ),
                    schema_version=(
                        _text_value(data["schema_version"])
                        if data["schema_version"] is not None
                        else None
                    ),
                    observed_at=data["response_observed_at"],
                    http_status=(
                        int(data["http_status"])
                        if data["http_status"] is not None
                        else None
                    ),
                    response_hash=(
                        _text_value(data["response_hash"])
                        if data["response_hash"] is not None
                        else None
                    ),
                    archive_reference=(
                        _text_value(data["archive_reference"])
                        if data["archive_reference"] is not None
                        else None
                    ),
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=leased[0],
                    parser_version=_text_value(data["parser_version"]),
                    processing_version=_text_value(data["processing_version"]),
                    domain_rule_version=_text_value(data["domain_rule_version"]),
                    analytics_rule_version=_text_value(data["analytics_rule_version"]),
                    max_attempts=int(data["max_attempts"]),
                )

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

    def renew_claim(self, claim: Claim, *, lease_seconds: int) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._timed_connection() as connection:
            with connection.transaction():
                renewed = connection.execute(
                    f"""
                    UPDATE {self._jobs_relation}
                    SET lease_expires_at = clock_timestamp() + (%s * interval '1 second'),
                        updated_at = clock_timestamp()
                    WHERE id = %s AND state = 'leased'
                      AND lease_owner = %s AND lease_token = %s
                      AND lease_expires_at > clock_timestamp()
                    RETURNING lease_expires_at
                    """,
                    (
                        lease_seconds,
                        claim.job_id,
                        claim.lease_owner,
                        claim.lease_token,
                    ),
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
                    (
                        renewed[0],
                        claim.attempt_id,
                        claim.job_id,
                        claim.lease_owner,
                        claim.lease_token,
                    ),
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

    def _lock_live_claim(self, connection: Any, claim: Claim) -> dict[str, Any]:
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
        completed = connection.execute(
            f"""
            UPDATE {self._jobs_relation}
            SET state = %s, outcome = %s, failure_category = NULL, failure_detail = NULL,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                completed_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id = %s AND state = 'leased'
              AND lease_owner = %s AND lease_token = %s
              AND lease_expires_at > clock_timestamp()
            """,
            (state, outcome, claim.job_id, claim.lease_owner, claim.lease_token),
        )
        if completed.rowcount != 1:
            raise LeaseLost("job completion fence was lost")

def _positive_int_input(values: dict[str, Any], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)

def _hash_input(value: Any, name: str) -> str:
    value = _text_value(value)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hash")
    return value

def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("analytics timestamps must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("analytics timestamps must include an offset")
    return parsed.astimezone(UTC)

def _snapshot_freshness(
    *, included_count: int, fresh_count: int, stale_count: int
) -> str:
    if included_count == 0 or stale_count == 0:
        return "fresh"
    if fresh_count == 0:
        return "stale"
    return "mixed"

def _text_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value
