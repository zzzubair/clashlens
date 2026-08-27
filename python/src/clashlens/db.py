from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .analytics import (
    CLASSIFICATION_CONFIDENCE,
    CLASSIFICATION_VERSION,
    FRESHNESS_RULE_VERSION,
    PROFILE_FRESHNESS_SECONDS,
    SNAPSHOT_ORDERING_RULE_VERSION,
    deterministic_tag_hash,
)
from .army_decoder import (
    DECODER_VERSION,
    DecodedArmy,
    DecodeFailure,
    decode_army_share_code,
)
from .battle import (
    SOURCE_PARSER_VERSION,
    ParsedBattleLog,
)
from .catalog import CATALOG_HASH, CATALOG_VERSION
from .domain import (
    SEASON_ANCHOR_RULE_VERSION,
    DomainRuleError,
    ranked_day_for,
    validate_season_anchor,
)
from .profile import ParsedProfile, normalize_player_tag
from .rankings import ParsedOfficialRankings
from .reconciliation import (
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    ReconciliationResult,
    reconcile_ranked_day,
    serialize_ranked_day_battles,
)
from .source_observation_contract import SOURCE_OBSERVATION_CONTRACTS

PROCESSING_VERSION = "clashlens-domain-processing-v1"
DEFAULT_PARSER_VERSION = SOURCE_PARSER_VERSION
DOMAIN_RULE_VERSION = "clashlens-domain-rules-v1"
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
ARMY_ANALYTICS_RULE_VERSION = "army-analytics-v2"
CONTRACT_VERSION = 4
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

# Priority classes that can appear in the Python queue, discovered from every
# enqueue site: db.py and api_db.py inserts, the Go collector handoff, and
# the migration column default all use priority 100. The claim statement
# probes one indexed range per declared priority; a priority outside this
# list is still claimed through the catch-all probe, so this list is a
# fast-path declaration, not a claimability gate. Add a new enqueue priority
# here (and to the catch-all partial index in migration 0003) so its claims
# stay on the fast path. TestDeclaredClaimPrioritiesMatchEnqueueSites pins
# this list to the enqueue sites.
_PYTHON_CLAIM_PRIORITIES = "(100)"
_PYTHON_CLAIM_PRIORITY_EXCLUSIONS = "100"


def _claim_select_statement(
    jobs_relation: str,
    *,
    job_id: int | None = None,
    supports_dependency: bool = True,
    denormalized_contract: bool = True,
    supports_coordinator: bool = False,
) -> tuple[str, dict[str, Any]]:
    """The bounded claim SELECT and its named parameters.

    The candidate CTE probes one indexed oldest-first range per declared
    priority, a catch-all probe for priorities outside the declared classes
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
    score = """job.priority + floor(extract(epoch FROM (statement_timestamp() - job.created_at))
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
    claim_versions = "1, 2, 3, 4" if supports_coordinator else "1, 2, 3"
    job_filter = f"""job.claim_compatibility_version IN ({claim_versions})
        AND ({dependency_filter}job.attempt_count < job.max_attempts)
        AND {supported_filter}"""
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
                    SELECT job.id
                    FROM {jobs_relation} AS job
                    LEFT JOIN collector_observations AS source_observation
                        ON source_observation.id = COALESCE(
                            job.observation_id, job.replay_observation_id
                        )
                    WHERE job.state IN ('pending', 'waiting_retry', 'waiting_dependency')
                      AND job.priority = claim_priority.priority
                      AND job.due_at <= statement_timestamp()
                      AND {job_filter}
                    ORDER BY job.due_at, job.created_at, job.id
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
                    WHERE job.state IN ('pending', 'waiting_retry', 'waiting_dependency')
                      AND job.priority NOT IN ({_PYTHON_CLAIM_PRIORITY_EXCLUSIONS})
                      AND job.due_at <= statement_timestamp()
                      AND {job_filter}
                    ORDER BY job.due_at, job.created_at, job.id
                    LIMIT {_CLAIM_CANDIDATE_LIMIT}
                )
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
                      AND {job_filter}
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
    ) -> None:
        if max_size < 1:
            raise ValueError("database pool size must be positive")
        if max_size > MAX_POOL_SIZE:
            raise ValueError("database pool size exceeds the supported maximum")
        self.stage_metrics: Any | None = None
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
        self._supports_dependency_deferral = bool(dependency_column)
        self._supports_denormalized_contract = bool(denormalized_contract)
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
        stats = self.pool.get_stats()
        return {
            key: int(stats[key])
            for key in (
                "pool_min",
                "pool_max",
                "pool_size",
                "pool_available",
                "requests_waiting",
                "requests_num",
                "requests_queued",
                "requests_wait_ms",
                "usage_ms",
            )
            if key in stats
        }

    def scalar(self, query: str, params: Iterable[Any] = ()) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return None if row is None else _text_value(row[0])

    def enqueue_reconciliation(
        self,
        *,
        player_tag: str,
        day_start: datetime,
        now: datetime,
        request_key: str,
    ) -> int:
        del now  # Production jobs use the worker's current time at claim.
        normalized_tag = normalize_player_tag(player_tag)
        ranked_day = ranked_day_for(day_start)
        ranked_day_start = ranked_day.start.astimezone(UTC)
        ranked_day_start_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        deduplication_key = (
            f"reconcile:{normalized_tag}:{ranked_day_start_text}:{request_key}"
        )
        with self.pool.connection() as connection:
            player = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s",
                (normalized_tag,),
            ).fetchone()
            if player is None:
                raise ValueError(f"unknown reconciliation player {normalized_tag}")
            player_id = int(player[0])
            row = connection.execute(
                """
                INSERT INTO python_processing_jobs_worker (
                    observation_id, work_type, deduplication_key, input_json,
                    state, due_at, parser_version, processing_version,
                    domain_rule_version, analytics_rule_version
                ) VALUES (
                    NULL, 'reconcile_ranked_day', %s, %s,
                    'pending', clock_timestamp(), %s, %s, %s, %s
                )
                ON CONFLICT (deduplication_key) DO UPDATE SET
                    deduplication_key = EXCLUDED.deduplication_key
                RETURNING id
                """,
                (
                    deduplication_key,
                    Jsonb(
                        {
                            "player_id": player_id,
                            "ranked_day_start": ranked_day_start_text,
                        }
                    ),
                    DEFAULT_PARSER_VERSION,
                    PROCESSING_VERSION,
                    DOMAIN_RULE_VERSION,
                    ANALYTICS_RULE_VERSION,
                ),
            ).fetchone()
            connection.commit()
            assert row is not None
            return int(row[0])

    def enqueue_current_season_republication(
        self,
        *,
        max_jobs: int = 100,
    ) -> list[int]:
        """Queue a bounded batch of published current-season days missing v3.

        This rebuilds derived ranked-day publications from canonical database
        evidence; it does not replay archived source observations. Repeating
        the call advances past already queued targets, so an operator can drain
        a season in measured batches without an unbounded deployment action.
        """

        if isinstance(max_jobs, bool) or not 1 <= max_jobs <= 1000:
            raise ValueError("current-season republication batch must be 1 to 1000")
        with self.pool.connection() as connection:
            with connection.transaction():
                candidates = connection.execute(
                    """
                    WITH current_anchor AS (
                        SELECT current_league_season_id, current_start
                        FROM legend_season_anchors
                        WHERE state = 'confirmed'
                          AND anchor_rule_version = %s
                        ORDER BY current_start DESC
                        LIMIT 1
                    ), published AS (
                        SELECT DISTINCT
                               log.player_id,
                               log.ranked_day_start,
                               anchor.current_league_season_id
                        FROM api_player_daily_logs AS log
                        JOIN current_anchor AS anchor
                          ON log.official_season_id =
                             anchor.current_league_season_id
                        WHERE log.ranked_day_start >= anchor.current_start
                          AND log.ranked_day_start <
                              anchor.current_start + interval '28 days'
                    )
                    SELECT player_id, ranked_day_start,
                           current_league_season_id
                    FROM published
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM ranked_day_versions AS version
                        WHERE version.player_id = published.player_id
                          AND version.ranked_day_start =
                              published.ranked_day_start
                          AND version.reconciliation_rule_version = %s
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM python_processing_jobs_worker AS job
                        WHERE job.work_type = 'reconcile_ranked_day'
                          AND job.state IN (
                              'pending', 'waiting_retry', 'waiting_dependency', 'leased'
                          )
                          AND (job.input_json ->> 'player_id')::bigint =
                              published.player_id
                          AND job.input_json ->> 'ranked_day_start' =
                              to_char(
                                  published.ranked_day_start AT TIME ZONE 'UTC',
                                  'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                              )
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM python_processing_jobs_worker AS job
                        WHERE job.deduplication_key =
                            'reconcile:current-season:'
                            || published.player_id::text || ':'
                            || to_char(
                                published.ranked_day_start AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                            ) || ':' || %s
                    )
                    ORDER BY player_id, ranked_day_start
                    LIMIT %s
                    """,
                    (
                        SEASON_ANCHOR_RULE_VERSION,
                        RECONCILIATION_RULE_VERSION,
                        RECONCILIATION_RULE_VERSION,
                        max_jobs,
                    ),
                ).fetchall()
                job_ids: list[int] = []
                for player_id, ranked_day_start, official_season_id in candidates:
                    ranked_day_start_text = ranked_day_start.astimezone(UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    deduplication_key = (
                        f"reconcile:current-season:{int(player_id)}:"
                        f"{ranked_day_start_text}:{RECONCILIATION_RULE_VERSION}"
                    )
                    row = connection.execute(
                        """
                        INSERT INTO python_processing_jobs_worker (
                            observation_id, work_type, deduplication_key,
                            input_json, state, due_at, parser_version,
                            processing_version, domain_rule_version,
                            analytics_rule_version
                        ) VALUES (
                            NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                            clock_timestamp(), %s, %s, %s, %s
                        )
                        ON CONFLICT (deduplication_key) DO NOTHING
                        RETURNING id
                        """,
                        (
                            deduplication_key,
                            Jsonb(
                                {
                                    "player_id": int(player_id),
                                    "ranked_day_start": ranked_day_start_text,
                                    "official_season_id": _text_value(
                                        official_season_id
                                    ),
                                    "trigger": "current_season_republication",
                                }
                            ),
                            DEFAULT_PARSER_VERSION,
                            PROCESSING_VERSION,
                            DOMAIN_RULE_VERSION,
                            ANALYTICS_RULE_VERSION,
                        ),
                    ).fetchone()
                    if row is not None:
                        job_ids.append(int(row[0]))
                return job_ids

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

    def complete_profile(self, claim: Claim, profile: ParsedProfile) -> None:
        (
            observation_id,
            http_status,
            response_hash,
            _observed_at,
            endpoint,
            schema_version,
        ) = self._observation_source(claim)
        with self._timed_connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                player = connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (normalized_tag) DO UPDATE
                        SET updated_at = clock_timestamp()
                    RETURNING id
                    """,
                    (
                        profile.normalized_tag,
                        profile.eligibility_state == "eligible",
                        profile.eligibility_state,
                    ),
                ).fetchone()
                assert player is not None
                profile_version = connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag, endpoint_version,
                        schema_version, parser_version, observed_at, source_http_status,
                        name, trophies, league_tier_id, league_tier_name,
                        eligibility_state, current_league_season_id,
                        previous_league_season_id, eligibility_reason,
                        source_contract_state, season_anchor_state, profile_json
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                        name = EXCLUDED.name,
                        trophies = EXCLUDED.trophies,
                        league_tier_id = EXCLUDED.league_tier_id,
                        league_tier_name = EXCLUDED.league_tier_name,
                        eligibility_state = EXCLUDED.eligibility_state,
                        current_league_season_id = EXCLUDED.current_league_season_id,
                        previous_league_season_id = EXCLUDED.previous_league_season_id,
                        eligibility_reason = EXCLUDED.eligibility_reason,
                        source_contract_state = EXCLUDED.source_contract_state,
                        season_anchor_state = EXCLUDED.season_anchor_state,
                        profile_json = EXCLUDED.profile_json
                    RETURNING id
                    """,
                    (
                        player[0],
                        observation_id,
                        profile.normalized_tag,
                        profile.endpoint_version,
                        profile.schema_version,
                        profile.parser_version,
                        profile.observed_at,
                        http_status,
                        profile.name,
                        profile.trophies,
                        profile.league_tier_id,
                        profile.league_tier_name,
                        profile.eligibility_state,
                        profile.current_league_season_id,
                        profile.previous_league_season_id,
                        profile.eligibility_reason,
                        profile.source_contract_state,
                        profile.season_anchor_state,
                        Jsonb(profile.profile_json),
                    ),
                ).fetchone()
                assert profile_version is not None
                profile_version_id = int(profile_version[0])
                connection.execute(
                    """
                    INSERT INTO player_profile_effects (profile_version_id, observation_id, effect_kind)
                    VALUES (%s, %s, 'current_profile')
                    ON CONFLICT (observation_id, effect_kind) DO NOTHING
                    """,
                    (profile_version_id, observation_id),
                )
                anchor_outcome = self._record_season_anchor(
                    connection, profile_version_id, profile
                )
                connection.execute(
                    """
                    WITH candidate AS (
                        SELECT v.id AS profile_version_id, v.observed_at,
                               v.eligibility_state,
                               v.eligibility_state IN ('eligible', 'ineligible')
                               AND NOT EXISTS (
                                   SELECT 1
                                   FROM player_profile_versions AS newer
                                   WHERE newer.player_id = v.player_id
                                     AND newer.eligibility_state
                                         IN ('eligible', 'ineligible')
                                     AND (newer.observed_at, newer.id)
                                         > (v.observed_at, v.id)
                               ) AS update_eligibility,
                               v.source_contract_state = 'accepted'
                               AND (
                                   p.current_observed_at IS NULL
                                   OR p.current_observed_at < v.observed_at
                                   OR (
                                       p.current_observed_at = v.observed_at
                                       AND (
                                           p.current_profile_version_id IS NULL
                                           OR p.current_profile_version_id < v.id
                                       )
                                   )
                               ) AS update_profile
                        FROM player_profile_versions AS v
                        JOIN players AS p ON p.id = v.player_id
                        WHERE p.id = %s AND v.id = %s
                    )
                    UPDATE players AS p
                    SET active = CASE
                            WHEN candidate.update_eligibility
                                 AND candidate.eligibility_state = 'eligible' THEN true
                            WHEN candidate.update_eligibility
                                 AND candidate.eligibility_state = 'ineligible' THEN false
                            ELSE p.active
                        END,
                        next_due_at = CASE
                            WHEN candidate.update_eligibility
                                 AND candidate.eligibility_state = 'eligible'
                                THEN COALESCE(p.next_due_at, clock_timestamp())
                            WHEN candidate.update_eligibility
                                 AND candidate.eligibility_state = 'ineligible' THEN NULL
                            ELSE p.next_due_at
                        END,
                        eligibility_state = CASE
                            WHEN candidate.update_eligibility
                                THEN candidate.eligibility_state
                            ELSE p.eligibility_state
                        END,
                        current_profile_version_id = CASE
                            WHEN candidate.update_profile
                                THEN candidate.profile_version_id
                            ELSE p.current_profile_version_id
                        END,
                        current_observed_at = CASE
                            WHEN candidate.update_profile THEN candidate.observed_at
                            ELSE p.current_observed_at
                        END,
                        updated_at = clock_timestamp()
                    FROM candidate
                    WHERE p.id = %s
                      AND (candidate.update_eligibility OR candidate.update_profile)
                    """,
                    (player[0], profile_version_id, player[0]),
                )
                self._record_parsed_payload(
                    connection,
                    endpoint=endpoint,
                    response_hash=response_hash,
                    parser_version=profile.parser_version,
                    schema_version=schema_version,
                    parse_outcome="valid",
                    parsed_json=profile.profile_json,
                )
                self._record_processing_outcome(
                    connection,
                    claim,
                    outcome="processed",
                    failure_category=(
                        "season_anchor_conflict"
                        if anchor_outcome == "conflict"
                        else None
                    ),
                )
                self._refresh_reset_baseline_evidence(connection, claim)
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    def complete_battle_log(self, claim: Claim, battle_log: ParsedBattleLog) -> None:
        (
            observation_id,
            _http_status,
            response_hash,
            _observed_at,
            endpoint,
            schema_version,
        ) = self._observation_source(claim)
        with self._timed_connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                valid_rows = [row for row in battle_log.rows if row.battle is not None]
                player_tags = {battle_log.normalized_tag}
                for row in valid_rows:
                    assert row.battle is not None
                    player_tags.add(row.battle.attacker_tag)
                    player_tags.add(row.battle.defender_tag)
                player_rows = connection.execute(
                    """
                    WITH requested (normalized_tag) AS (
                        SELECT DISTINCT unnest(%s::text[])
                    )
                    INSERT INTO players (
                        normalized_tag, active, eligibility_state
                    )
                    SELECT normalized_tag, false, 'unknown'
                    FROM requested
                    ORDER BY normalized_tag
                    ON CONFLICT (normalized_tag) DO UPDATE
                        SET updated_at = clock_timestamp()
                    RETURNING id, normalized_tag
                    """,
                    (sorted(player_tags),),
                ).fetchall()
                player_ids = {_text_value(row[1]): int(row[0]) for row in player_rows}
                reporter_id = player_ids[battle_log.normalized_tag]
                log_row = connection.execute(
                    """
                    INSERT INTO battle_log_observations (
                        observation_id, player_id, parser_version, observed_at,
                        row_count, has_row_gap
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                        row_count = EXCLUDED.row_count,
                        has_row_gap = EXCLUDED.has_row_gap
                    RETURNING id
                    """,
                    (
                        observation_id,
                        reporter_id,
                        battle_log.parser_version,
                        battle_log.observed_at,
                        battle_log.row_count,
                        battle_log.has_row_gap,
                    ),
                ).fetchone()
                assert log_row is not None
                log_id = int(log_row[0])
                source_rows = connection.execute(
                    """
                    WITH input AS (
                        SELECT * FROM jsonb_to_recordset(%s::jsonb) AS row_data (
                            source_row_index integer,
                            outcome text,
                            failure_category text,
                            source_json jsonb
                        )
                    )
                    INSERT INTO battle_source_rows (
                        battle_log_observation_id, source_row_index, outcome,
                        failure_category, source_json
                    )
                    SELECT %s, source_row_index, outcome,
                           failure_category, source_json
                    FROM input
                    ON CONFLICT (battle_log_observation_id, source_row_index)
                    DO UPDATE SET
                        outcome = EXCLUDED.outcome,
                        failure_category = EXCLUDED.failure_category,
                        source_json = EXCLUDED.source_json
                    RETURNING id, source_row_index
                    """,
                    (
                        Jsonb(
                            [
                                {
                                    "source_row_index": row.source_row_index,
                                    "outcome": row.outcome,
                                    "failure_category": row.failure_category,
                                    "source_json": row.source_json,
                                }
                                for row in battle_log.rows
                            ]
                        ),
                        log_id,
                    ),
                ).fetchall()
                source_row_ids = {int(row[1]): int(row[0]) for row in source_rows}
                affected_battle_ids: set[int] = set()
                shared_state_changed_battle_ids: set[int] = set()

                if valid_rows:
                    battles = []
                    discoveries = []
                    for row in valid_rows:
                        battle = row.battle
                        assert battle is not None
                        attacker_id = player_ids[battle.attacker_tag]
                        defender_id = player_ids[battle.defender_tag]
                        battles.append(
                            {
                                "ranked_day_start": battle.ranked_day_start.isoformat(),
                                "attacker_player_id": attacker_id,
                                "defender_player_id": defender_id,
                            }
                        )
                        discoveries.append(
                            {
                                "player_id": (
                                    defender_id
                                    if battle.perspective == "attacker"
                                    else attacker_id
                                ),
                                "source_row_index": row.source_row_index,
                            }
                        )
                    connection.execute(
                        """
                        WITH input AS (
                            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS discovery (
                                player_id bigint, source_row_index integer
                            )
                        )
                        INSERT INTO known_player_discoveries (
                            player_id, observation_id, source_row_index,
                            source_kind, discovered_at
                        )
                        SELECT player_id, %s, source_row_index,
                               'battle_opponent', %s
                        FROM input
                        ORDER BY player_id, source_row_index
                        ON CONFLICT DO NOTHING
                        """,
                        (Jsonb(discoveries), observation_id, battle_log.observed_at),
                    )
                    if claim.work_type == "process_observation":
                        connection.execute(
                            "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                            (sorted({item["player_id"] for item in discoveries}),),
                        )
                    canonical_rows = connection.execute(
                        """
                        WITH input AS (
                            SELECT DISTINCT *
                            FROM jsonb_to_recordset(%s::jsonb) AS battle (
                                ranked_day_start timestamptz,
                                attacker_player_id bigint,
                                defender_player_id bigint
                            )
                        )
                        INSERT INTO legend_battles (
                            ranked_day_start, attacker_player_id,
                            defender_player_id
                        )
                        SELECT ranked_day_start, attacker_player_id,
                               defender_player_id
                        FROM input
                        ORDER BY ranked_day_start, attacker_player_id,
                                 defender_player_id
                        ON CONFLICT (
                            ranked_day_start, attacker_player_id,
                            defender_player_id
                        ) DO UPDATE SET updated_at = clock_timestamp()
                        RETURNING id, ranked_day_start,
                                  attacker_player_id, defender_player_id
                        """,
                        (Jsonb(battles),),
                    ).fetchall()
                    canonical_ids = {
                        (row[1], int(row[2]), int(row[3])): int(row[0])
                        for row in canonical_rows
                    }
                    evidence_input = []
                    for row in valid_rows:
                        battle = row.battle
                        assert battle is not None
                        attacker_id = player_ids[battle.attacker_tag]
                        defender_id = player_ids[battle.defender_tag]
                        evidence_input.append(
                            {
                                "battle_id": canonical_ids[
                                    (
                                        battle.ranked_day_start,
                                        attacker_id,
                                        defender_id,
                                    )
                                ],
                                "source_row_id": source_row_ids[row.source_row_index],
                                "perspective": battle.perspective,
                                "battle_timestamp": battle.battle_timestamp.isoformat(),
                                "stars": battle.stars,
                                "destruction_percentage": battle.destruction_percentage,
                                "army_share_code": battle.army_share_code,
                                "reporter_trophies": battle.reporter_trophies,
                                "opponent_trophies": battle.opponent_trophies,
                                "attacker_gain": battle.attacker_gain,
                                "defender_loss": battle.defender_loss,
                                "trophy_rule_version": battle.trophy_rule_version,
                            }
                        )
                    evidence_rows = connection.execute(
                        """
                        WITH input AS (
                            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS evidence (
                                battle_id bigint, source_row_id bigint,
                                perspective text, battle_timestamp timestamptz,
                                stars integer, destruction_percentage integer,
                                army_share_code text, reporter_trophies integer,
                                opponent_trophies integer, attacker_gain integer,
                                defender_loss integer, trophy_rule_version text
                            )
                        )
                        INSERT INTO battle_evidence (
                            battle_id, source_row_id, observation_id,
                            reporting_player_id, perspective, battle_timestamp,
                            stars, destruction_percentage, army_share_code,
                            reporter_trophies, opponent_trophies, attacker_gain,
                            defender_loss, trophy_rule_version, source_observed_at,
                            parser_version
                        )
                        SELECT battle_id, source_row_id, %s, %s, perspective,
                               battle_timestamp, stars, destruction_percentage,
                               army_share_code, reporter_trophies, opponent_trophies,
                               attacker_gain, defender_loss, trophy_rule_version,
                               %s, %s
                        FROM input
                        ON CONFLICT (source_row_id) DO UPDATE SET
                            source_row_id = EXCLUDED.source_row_id
                        RETURNING id, battle_id, perspective, source_observed_at
                        """,
                        (
                            Jsonb(evidence_input),
                            observation_id,
                            reporter_id,
                            battle_log.observed_at,
                            battle_log.parser_version,
                        ),
                    ).fetchall()
                    perspectives = [
                        {
                            "evidence_id": int(row[0]),
                            "battle_id": int(row[1]),
                            "perspective": _text_value(row[2]),
                            "source_observed_at": row[3].isoformat(),
                        }
                        for row in evidence_rows
                    ]
                    affected_battle_ids.update(int(row[1]) for row in evidence_rows)
                    previous_disagreement_states = {
                        int(row[0]): _text_value(row[1])
                        for row in connection.execute(
                            """
                            SELECT id, disagreement_state
                            FROM legend_battles
                            WHERE id = ANY(%s::bigint[])
                            """,
                            (sorted(affected_battle_ids),),
                        ).fetchall()
                    }
                    connection.execute(
                        """
                        WITH input AS (
                            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS perspective (
                                evidence_id bigint, battle_id bigint,
                                perspective text, source_observed_at timestamptz
                            )
                        ), chosen AS (
                            SELECT DISTINCT ON (battle_id, perspective) *
                            FROM input
                            ORDER BY battle_id, perspective,
                                     source_observed_at DESC, evidence_id DESC
                        )
                        INSERT INTO battle_perspectives (
                            battle_id, perspective, evidence_id,
                            source_observed_at
                        )
                        SELECT battle_id, perspective, evidence_id,
                               source_observed_at
                        FROM chosen
                        ON CONFLICT (battle_id, perspective) DO UPDATE SET
                            evidence_id = EXCLUDED.evidence_id,
                            source_observed_at = EXCLUDED.source_observed_at,
                            updated_at = clock_timestamp()
                        WHERE EXCLUDED.source_observed_at
                                  > battle_perspectives.source_observed_at
                           OR (
                               EXCLUDED.source_observed_at
                                   = battle_perspectives.source_observed_at
                               AND EXCLUDED.evidence_id
                                   > battle_perspectives.evidence_id
                           )
                        """,
                        (Jsonb(perspectives),),
                    )
                    self._refresh_battle_disagreements(
                        connection,
                        sorted(affected_battle_ids),
                    )
                    current_disagreement_states = {
                        int(row[0]): _text_value(row[1])
                        for row in connection.execute(
                            """
                            SELECT id, disagreement_state
                            FROM legend_battles
                            WHERE id = ANY(%s::bigint[])
                            """,
                            (sorted(affected_battle_ids),),
                        ).fetchall()
                    }
                    shared_state_changed_battle_ids.update(
                        battle_id
                        for battle_id, state in current_disagreement_states.items()
                        if previous_disagreement_states.get(battle_id) != state
                    )
                    self._upsert_army_decodes(connection, sorted(affected_battle_ids))

                self._record_parsed_payload(
                    connection,
                    endpoint=endpoint,
                    response_hash=response_hash,
                    parser_version=battle_log.parser_version,
                    schema_version=schema_version,
                    parse_outcome=(
                        "valid_with_gaps" if battle_log.has_row_gap else "valid"
                    ),
                    parsed_json={"items": [row.source_json for row in battle_log.rows]},
                )
                outcome = (
                    "processed_with_gaps" if battle_log.has_row_gap else "processed"
                )
                self._record_processing_outcome(connection, claim, outcome=outcome)
                self._refresh_reset_baseline_evidence(connection, claim)
                ranked_day = ranked_day_for(battle_log.observed_at)
                live_player_ids = {reporter_id}
                if shared_state_changed_battle_ids:
                    perspective_players = connection.execute(
                        """
                        SELECT DISTINCT CASE p.perspective
                            WHEN 'attacker' THEN battle.attacker_player_id
                            ELSE battle.defender_player_id
                        END AS player_id
                        FROM legend_battles AS battle
                        JOIN battle_perspectives AS p ON p.battle_id = battle.id
                        WHERE battle.id = ANY(%s::bigint[])
                          AND battle.ranked_day_start = %s
                        """,
                        (sorted(shared_state_changed_battle_ids), ranked_day.start),
                    ).fetchall()
                    live_player_ids.update(int(row[0]) for row in perspective_players)
                source_failures = sorted(
                    (
                        row.outcome,
                        row.failure_category or "",
                    )
                    for row in battle_log.rows
                    if row.outcome not in {"valid_legend", "ignored_non_legend"}
                )
                source_quality = (
                    {
                        "has_row_gap": battle_log.has_row_gap,
                        "failures": source_failures,
                    }
                    if battle_log.has_row_gap or source_failures
                    else None
                )
                for live_player_id in sorted(live_player_ids):
                    self._enqueue_live_reconciliation(
                        connection,
                        player_id=live_player_id,
                        ranked_day_start=ranked_day.start,
                        source_quality=(
                            source_quality if live_player_id == reporter_id else None
                        ),
                    )
                self._finish_claim(
                    connection, claim, job, state="complete", outcome=outcome
                )

    def complete_rankings(
        self,
        claim: Claim,
        rankings: ParsedOfficialRankings,
    ) -> None:
        (
            observation_id,
            _http_status,
            response_hash,
            observed_at,
            endpoint,
            schema_version,
        ) = self._observation_source(claim)
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                attempt = connection.execute(
                    """
                    INSERT INTO official_top200_attempts (
                        observation_id, parser_version, outcome, failure_reasons,
                        observed_at, season_provenance, official_season_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                        outcome = EXCLUDED.outcome,
                        failure_reasons = EXCLUDED.failure_reasons
                    RETURNING id
                    """,
                    (
                        observation_id,
                        rankings.parser_version,
                        rankings.outcome,
                        Jsonb(list(rankings.failure_reasons)),
                        observed_at,
                        rankings.season_provenance,
                        rankings.official_season_id,
                    ),
                ).fetchone()
                assert attempt is not None
                player_ids: dict[str, int] = {}
                if rankings.entries:
                    rows = connection.execute(
                        """
                        WITH input AS (
                            SELECT normalized_tag, min(source_row_index) AS source_row_index
                            FROM unnest(%s::text[], %s::integer[])
                                AS item(normalized_tag, source_row_index)
                            GROUP BY normalized_tag
                        ), players_upserted AS (
                            INSERT INTO players (normalized_tag, active, eligibility_state)
                            SELECT normalized_tag, false, 'unknown' FROM input
                            ON CONFLICT (normalized_tag) DO UPDATE
                                SET updated_at = clock_timestamp()
                            RETURNING id, normalized_tag
                        ), discoveries AS (
                            INSERT INTO known_player_discoveries (
                                player_id, observation_id, source_row_index,
                                source_kind, discovered_at
                            )
                            SELECT player.id, %s, input.source_row_index,
                                   'official_ranking', %s
                            FROM players_upserted AS player
                            JOIN input USING (normalized_tag)
                            ON CONFLICT DO NOTHING
                        )
                        SELECT normalized_tag, id FROM players_upserted
                        """,
                        (
                            [entry.normalized_tag for entry in rankings.entries],
                            [entry.source_row_index for entry in rankings.entries],
                            observation_id,
                            observed_at,
                        ),
                    ).fetchall()
                    player_ids = {
                        _text_value(tag): int(player_id) for tag, player_id in rows
                    }
                if claim.work_type == "process_observation" and player_ids:
                    discovery_ids = sorted(set(player_ids.values()))
                    for offset in range(0, len(discovery_ids), 500):
                        connection.execute(
                            "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                            (discovery_ids[offset : offset + 500],),
                        )
                if rankings.outcome == "official_observed":
                    version = connection.execute(
                        """
                        INSERT INTO official_top200_versions (
                            attempt_id, observation_id, observed_at, parser_version
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (attempt_id) DO UPDATE
                            SET attempt_id = EXCLUDED.attempt_id
                        RETURNING id
                        """,
                        (
                            attempt[0],
                            observation_id,
                            observed_at,
                            rankings.parser_version,
                        ),
                    ).fetchone()
                    assert version is not None
                    connection.execute(
                        """
                        INSERT INTO official_top200_entries (
                            version_id, rank, player_id, normalized_tag, source_json
                        )
                        SELECT %s, entry.rank, entry.player_id,
                               entry.normalized_tag, entry.source_json
                        FROM jsonb_to_recordset(%s::jsonb) AS entry(
                            rank integer, player_id bigint,
                            normalized_tag text, source_json jsonb
                        )
                        ON CONFLICT (version_id, rank) DO UPDATE SET
                            player_id = EXCLUDED.player_id,
                            normalized_tag = EXCLUDED.normalized_tag,
                            source_json = EXCLUDED.source_json
                        """,
                        (
                            version[0],
                            Jsonb(
                                [
                                    {
                                        "rank": entry.rank,
                                        "player_id": player_ids[entry.normalized_tag],
                                        "normalized_tag": entry.normalized_tag,
                                        "source_json": entry.source_json,
                                    }
                                    for entry in rankings.entries
                                ]
                            ),
                        ),
                    )
                self._record_parsed_payload(
                    connection,
                    endpoint=endpoint,
                    response_hash=response_hash,
                    parser_version=rankings.parser_version,
                    schema_version=schema_version,
                    parse_outcome="valid",
                    parsed_json={
                        "items": [entry.source_json for entry in rankings.entries],
                        "paging": {"cursors": {}},
                    },
                )
                self._record_processing_outcome(
                    connection,
                    claim,
                    outcome="processed",
                    failure_category=(
                        None
                        if rankings.outcome == "official_observed"
                        else rankings.outcome
                    ),
                )
                self._finish_claim(
                    connection,
                    claim,
                    job,
                    state="complete",
                    outcome=rankings.outcome,
                )

    def complete_reconciliation(self, claim: Claim) -> None:
        player_id = int(claim.input_json["player_id"])
        day_start = datetime.fromisoformat(str(claim.input_json["ranked_day_start"]))
        ranked_day = ranked_day_for(day_start)
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                # Different source changes can enqueue distinct jobs for one
                # player-day. Serialize their version/publication writes while
                # allowing unrelated player-days to reconcile concurrently.
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"ranked-day:{player_id}:{ranked_day.start.isoformat()}",),
                )
                now_row = connection.execute("SELECT clock_timestamp()").fetchone()
                assert now_row is not None
                now = now_row[0]
                player = connection.execute(
                    "SELECT id, normalized_tag FROM players WHERE id = %s",
                    (player_id,),
                ).fetchone()
                if player is None:
                    raise ValueError(f"unknown reconciliation player id {player_id}")

                start_baseline = self._load_reset_baseline(
                    connection,
                    player_id,
                    ranked_day.start,
                    claim.parser_version,
                    claim.processing_version,
                )
                end_baseline = self._load_reset_baseline(
                    connection,
                    player_id,
                    ranked_day.end,
                    claim.parser_version,
                    claim.processing_version,
                )
                start_battle_log_observation_id = (
                    int(start_baseline["evidence"]["battle_log_observation_id"])
                    if start_baseline is not None
                    and start_baseline["evidence"]["battle_log_observation_id"]
                    is not None
                    else None
                )
                end_battle_log_observation_id = (
                    int(end_baseline["evidence"]["battle_log_observation_id"])
                    if end_baseline is not None
                    and end_baseline["evidence"]["battle_log_observation_id"]
                    is not None
                    else None
                )
                coverage_rows = connection.execute(
                    """
                    SELECT
                        blo.observation_id,
                        blo.observed_at,
                        blo.row_count,
                        blo.has_row_gap,
                        COALESCE(evidence.battle_identities, ARRAY[]::text[]),
                        COALESCE(evidence.source_row_ids, ARRAY[]::bigint[]),
                        COALESCE(row_flags.malformed_count, 0),
                        COALESCE(row_flags.unclassified_count, 0),
                        COALESCE(processing.outcome = 'processed', false),
                        observed.response_hash,
                        blo.parser_version,
                        processing.processing_version
                    FROM battle_log_observations AS blo
                    JOIN collector_observations AS observed
                      ON observed.id = blo.observation_id
                    LEFT JOIN observation_processing_outcomes AS processing
                      ON processing.observation_id = blo.observation_id
                     AND processing.parser_version = blo.parser_version
                    LEFT JOIN LATERAL (
                        SELECT
                            array_agg(be.battle_id::text ORDER BY be.id)
                                FILTER (WHERE be.battle_id IS NOT NULL)
                                AS battle_identities,
                            array_agg(sr.id ORDER BY sr.id)
                                FILTER (WHERE sr.id IS NOT NULL)
                                AS source_row_ids
                        FROM battle_source_rows AS sr
                        LEFT JOIN battle_evidence AS be
                          ON be.source_row_id = sr.id
                        WHERE sr.battle_log_observation_id = blo.id
                    ) AS evidence ON true
                    LEFT JOIN LATERAL (
                        SELECT
                            count(*) FILTER (
                                WHERE sr.outcome = 'malformed_legend_row'
                                   OR sr.failure_category LIKE 'malformed%%'
                                   OR sr.failure_category LIKE 'unsupported%%'
                                   OR sr.failure_category LIKE 'identity%%'
                            ) AS malformed_count,
                            count(*) FILTER (
                                WHERE sr.failure_category LIKE 'unclassified%%'
                            ) AS unclassified_count
                        FROM battle_source_rows AS sr
                        WHERE sr.battle_log_observation_id = blo.id
                    ) AS row_flags ON true
                    WHERE blo.player_id = %s
                      AND blo.observed_at >= COALESCE(
                          (SELECT start_blo.observed_at
                             FROM battle_log_observations AS start_blo
                            WHERE start_blo.observation_id = %s),
                          %s
                      )
                      AND blo.observed_at <= COALESCE(
                          (SELECT end_blo.observed_at
                             FROM battle_log_observations AS end_blo
                            WHERE end_blo.observation_id = %s),
                          %s
                      )
                    ORDER BY blo.observed_at, blo.id
                    """,
                    (
                        player_id,
                        start_battle_log_observation_id,
                        ranked_day.start,
                        end_battle_log_observation_id,
                        ranked_day.end,
                    ),
                ).fetchall()
                if start_battle_log_observation_id is not None:
                    start_index = next(
                        (
                            index
                            for index, row in enumerate(coverage_rows)
                            if int(row[0]) == start_battle_log_observation_id
                        ),
                        None,
                    )
                    if start_index is not None:
                        coverage_rows = coverage_rows[start_index:]
                if end_battle_log_observation_id is not None:
                    end_index = next(
                        (
                            index
                            for index, row in enumerate(coverage_rows)
                            if int(row[0]) == end_battle_log_observation_id
                        ),
                        None,
                    )
                    if end_index is not None:
                        coverage_rows = coverage_rows[: end_index + 1]
                coverage = tuple(
                    CoverageObservation(
                        observation_id=int(row[0]),
                        observed_at=row[1],
                        row_count=int(row[2]),
                        has_row_gap=bool(row[3]),
                        battle_identities=tuple(str(value) for value in row[4]),
                        source_row_ids=tuple(int(value) for value in row[5]),
                        malformed_row_count=int(row[6]),
                        unclassified_row_count=int(row[7]),
                        valid=bool(row[8]),
                        response_hash=_text_value(row[9]),
                        parser_version=_text_value(row[10]),
                        processing_version=(
                            _text_value(row[11]) if row[11] is not None else None
                        ),
                    )
                    for row in coverage_rows
                )
                contribution_rows = connection.execute(
                    """
                    SELECT
                        b.id,
                        p.perspective,
                        e.id,
                        e.source_row_id,
                        e.observation_id,
                        e.source_observed_at,
                        e.battle_timestamp,
                        e.stars,
                        e.destruction_percentage,
                        e.army_share_code,
                        e.attacker_gain,
                        e.defender_loss,
                        e.trophy_rule_version,
                        b.disagreement_state,
                        source_row.outcome,
                        source_row.failure_category,
                        CASE e.parser_version
                            WHEN 'supercell-source-parser-v2'
                                THEN source_row.source_json ->> 'opponentPlayerTag'
                            ELSE source_row.source_json -> 'opponent' ->> 'tag'
                        END,
                        CASE e.parser_version
                            WHEN 'supercell-source-parser-v2'
                                THEN source_row.source_json ->> 'opponentName'
                            ELSE source_row.source_json -> 'opponent' ->> 'name'
                        END
                    FROM legend_battles AS b
                    JOIN battle_perspectives AS p ON p.battle_id = b.id
                    JOIN battle_evidence AS e ON e.id = p.evidence_id
                    JOIN battle_source_rows AS source_row
                      ON source_row.id = e.source_row_id
                    WHERE e.battle_timestamp >= %s
                      AND e.battle_timestamp < %s
                      AND (
                          (p.perspective = 'attacker' AND b.attacker_player_id = %s)
                          OR
                          (p.perspective = 'defender' AND b.defender_player_id = %s)
                      )
                    ORDER BY b.id, p.perspective
                    """,
                    (ranked_day.start, ranked_day.end, player_id, player_id),
                ).fetchall()
                contributions = tuple(
                    BattleContribution(
                        battle_identity=str(row[0]),
                        lens=(
                            "offense"
                            if _text_value(row[1]) == "attacker"
                            else "defense"
                        ),
                        trophy_amount=int(
                            row[10] if _text_value(row[1]) == "attacker" else row[11]
                        ),
                        source_rule_version=_text_value(row[12]),
                        valid=_text_value(row[14]) == "valid_legend",
                        failure_reason=(
                            _text_value(row[15]) if row[15] is not None else None
                        ),
                        disagreement=_text_value(row[13]) == "disagreement",
                        source_observation_id=int(row[4]),
                        source_evidence_id=int(row[2]),
                        source_row_id=int(row[3]),
                        source_observed_at=row[5],
                        battle_timestamp=row[6],
                        stars=int(row[7]),
                        destruction_percentage=int(row[8]),
                        army_share_code=_text_value(row[9]),
                        attacker_gain=int(row[10]),
                        defender_loss=int(row[11]),
                        opponent_tag=(
                            _text_value(row[16]) if row[16] is not None else None
                        ),
                        opponent_name=(
                            _text_value(row[17]) if row[17] is not None else None
                        ),
                    )
                    for row in contribution_rows
                )
                previous_row = connection.execute(
                    """
                    SELECT
                        id,
                        state,
                        confidence,
                        defense_count,
                        observed_defense_loss,
                        coverage_complete,
                        shield_state,
                        shield_duration_days,
                        input_hash
                    FROM ranked_day_versions
                    WHERE player_id = %s AND ranked_day_start = %s
                      AND reconciliation_rule_version = %s
                    ORDER BY version DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        player_id,
                        ranked_day.start - timedelta(days=1),
                        RECONCILIATION_RULE_VERSION,
                    ),
                ).fetchone()
                previous = (
                    PreviousRankedDay(
                        complete=(
                            _text_value(previous_row[1]) == "Complete"
                            and bool(previous_row[5])
                        ),
                        observed_defense_count=int(previous_row[3]),
                        observed_defense_loss=int(previous_row[4]),
                        shield_run_length=(
                            int(previous_row[7] or 0)
                            if _text_value(previous_row[6]) == "inferred_shielded"
                            else 0
                        ),
                        coverage_complete=bool(previous_row[5]),
                        shield_state=_text_value(previous_row[6]),
                        version_id=int(previous_row[0]),
                        ranked_day_start=ranked_day.start - timedelta(days=1),
                        state=_text_value(previous_row[1]),
                        confidence=_text_value(previous_row[2]),
                        input_hash=(
                            _text_value(previous_row[8])
                            if previous_row[8] is not None
                            else None
                        ),
                    )
                    if previous_row is not None
                    else None
                )
                anchor = connection.execute(
                    """
                    SELECT current_league_season_id, previous_league_season_id,
                           current_start, previous_start
                    FROM legend_season_anchors
                    WHERE state = 'confirmed' AND anchor_rule_version = %s
                    """,
                    (SEASON_ANCHOR_RULE_VERSION,),
                ).fetchone()
                anchor_valid = anchor is not None and ranked_day.start >= anchor[3]
                if anchor is None:
                    official_season_id = "unknown"
                    season_start = ranked_day.start
                elif ranked_day.start >= anchor[2]:
                    official_season_id = _text_value(anchor[0])
                    season_start = anchor[2]
                else:
                    official_season_id = _text_value(anchor[1])
                    season_start = anchor[3]
                season_day_number = (ranked_day.start - season_start).days + 1
                boundary_kind = None
                if anchor is not None and ranked_day.end == anchor[2]:
                    boundary_kind = "season"
                elif ranked_day.end.weekday() == 0:
                    boundary_kind = "weekly"

                trophy_rule_versions = tuple(
                    sorted(
                        {
                            contribution.source_rule_version
                            for contribution in contributions
                            if contribution.source_rule_version is not None
                        }
                    )
                )
                baseline_eligibility = tuple(
                    value
                    for value in (
                        start_baseline.get("eligibility_state")
                        if start_baseline is not None
                        else None,
                        end_baseline.get("eligibility_state")
                        if end_baseline is not None
                        else None,
                    )
                    if value is not None
                )
                player_eligible = bool(baseline_eligibility) and all(
                    value == "eligible" for value in baseline_eligibility
                )
                malformed_evidence = any(
                    observation.malformed_row_count > 0 for observation in coverage
                )
                unclassified_evidence = any(
                    observation.unclassified_row_count > 0 for observation in coverage
                )
                perspective_disagreement = any(
                    contribution.disagreement for contribution in contributions
                )
                result = reconcile_ranked_day(
                    ReconciliationInput(
                        ranked_day=ranked_day,
                        now=now,
                        start_baseline_id=(
                            int(start_baseline["id"])
                            if start_baseline is not None
                            else None
                        ),
                        end_baseline_id=(
                            int(end_baseline["id"])
                            if end_baseline is not None
                            else None
                        ),
                        start_trophies=(
                            int(start_baseline["trophies"])
                            if start_baseline is not None
                            and start_baseline["trophies"] is not None
                            else None
                        ),
                        next_start_trophies=(
                            int(end_baseline["trophies"])
                            if end_baseline is not None
                            and end_baseline["trophies"] is not None
                            else None
                        ),
                        start_baseline_battle_log_observation_id=(
                            start_battle_log_observation_id
                        ),
                        end_baseline_battle_log_observation_id=(
                            end_battle_log_observation_id
                        ),
                        coverage_observations=coverage,
                        contributions=contributions,
                        previous_day=previous,
                        boundary_kind=boundary_kind,
                        season_anchor_valid=anchor_valid,
                        start_baseline_complete=(
                            bool(start_baseline["complete"])
                            if start_baseline is not None
                            else False
                        ),
                        end_baseline_complete=(
                            bool(end_baseline["complete"])
                            if end_baseline is not None
                            else False
                        ),
                        player_eligible=player_eligible,
                        perspective_disagreement=perspective_disagreement,
                        malformed_evidence=malformed_evidence,
                        unclassified_evidence=unclassified_evidence,
                        start_baseline_evidence=(
                            start_baseline["evidence"]
                            if start_baseline is not None
                            else {}
                        ),
                        end_baseline_evidence=(
                            end_baseline["evidence"] if end_baseline is not None else {}
                        ),
                        parser_version=claim.parser_version,
                        processing_version=claim.processing_version,
                        domain_rule_version=claim.domain_rule_version,
                        season_anchor_rule_version=SEASON_ANCHOR_RULE_VERSION,
                        trophy_allocation_rule_versions=trophy_rule_versions,
                    )
                )
                result_data = {
                    "state": result.state,
                    "confidence": result.confidence,
                    "failure_reasons": list(result.failure_reasons),
                    "start_trophies": (
                        int(start_baseline["trophies"])
                        if start_baseline is not None
                        and start_baseline["trophies"] is not None
                        else None
                    ),
                    "next_start_trophies": (
                        int(end_baseline["trophies"])
                        if end_baseline is not None
                        and end_baseline["trophies"] is not None
                        else None
                    ),
                    "attack_count": result.attack_count,
                    "defense_count": result.defense_count,
                    "attack_gain": result.attack_trophy_gain,
                    "observed_defense_loss": result.observed_defense_loss,
                    "automatic_defense_loss": result.automatic_defense_loss,
                    "automatic_defense_evidence_state": (
                        result.automatic_defense_evidence_state
                    ),
                    "net_trophy_change": result.net_trophy_change,
                    "observed_trophy_change": result.observed_trophy_change,
                    "final_trophies_before_reset": result.final_trophies_before_reset,
                    "boundary_adjustment": result.boundary_adjustment,
                    "boundary_adjustment_type": result.boundary_adjustment_type,
                    "observed_boundary_adjustment": result.observed_boundary_adjustment,
                    "expected_next_start_trophies": (
                        result.expected_next_start_trophies
                    ),
                    "unexplained_residual": result.unexplained_residual,
                    "shield_state": result.shield_state,
                    "shield_duration_days": result.shield_duration_days,
                    "coverage_complete": result.coverage_complete,
                    "formula_components": result.formula_components,
                    "input_evidence": result.input_evidence,
                    "shield_evidence": result.shield_evidence,
                }
                rule_versions = {
                    "parser_version": claim.parser_version,
                    "processing_version": claim.processing_version,
                    "domain_rule_version": claim.domain_rule_version,
                    "season_anchor_rule_version": SEASON_ANCHOR_RULE_VERSION,
                    "reconciliation_rule_version": RECONCILIATION_RULE_VERSION,
                    "trophy_allocation_rule_versions": list(trophy_rule_versions),
                }
                input_payload = {
                    "player_id": player_id,
                    "ranked_day_start": ranked_day.start.isoformat(),
                    "ranked_day_end": ranked_day.end.isoformat(),
                    "official_season_id": official_season_id,
                    "season_day_number": season_day_number,
                    "boundary_kind": boundary_kind,
                    "rule_versions": rule_versions,
                    "input_evidence": result.input_evidence,
                }
                input_hash = hashlib.sha256(
                    json.dumps(
                        input_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                result_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "input_hash": input_hash,
                            "result": result_data,
                            "rule_versions": rule_versions,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                input_evidence = result.input_evidence
                coverage_evidence = input_evidence.get("coverage_observations", [])
                contribution_evidence = input_evidence.get("contributions", [])
                existing = connection.execute(
                    """
                    SELECT id, version FROM ranked_day_versions
                    WHERE player_id = %s AND ranked_day_start = %s
                      AND reconciliation_rule_version = %s AND result_hash = %s
                    """,
                    (
                        player_id,
                        ranked_day.start,
                        RECONCILIATION_RULE_VERSION,
                        result_hash,
                    ),
                ).fetchone()
                if existing is None:
                    previous_version = connection.execute(
                        """
                        SELECT id, version FROM ranked_day_versions
                        WHERE player_id = %s AND ranked_day_start = %s
                          AND reconciliation_rule_version = %s
                        ORDER BY version DESC LIMIT 1
                        FOR UPDATE
                        """,
                        (player_id, ranked_day.start, RECONCILIATION_RULE_VERSION),
                    ).fetchone()
                    previous_publication = connection.execute(
                        """
                        SELECT max(version)
                        FROM api_player_daily_logs
                        WHERE player_id = %s AND ranked_day_start = %s
                        """,
                        (player_id, ranked_day.start),
                    ).fetchone()
                    next_ranked_day_version = (
                        int(previous_version[1]) + 1
                        if previous_version is not None
                        else 1
                    )
                    next_publication_version = (
                        int(previous_publication[0]) + 1
                        if previous_publication is not None
                        and previous_publication[0] is not None
                        else 1
                    )
                    # ``api_player_daily_logs.version`` predates the
                    # reconciliation-rule version and has a global per-day
                    # uniqueness constraint. Continue above any v2 publication
                    # when the first v3 republication is written, while
                    # retaining idempotence for the same v3 result.
                    version_number = max(
                        next_ranked_day_version, next_publication_version
                    )
                    evidence_complete = bool(
                        result.coverage_complete
                        and start_baseline is not None
                        and start_baseline["complete"]
                        and end_baseline is not None
                        and end_baseline["complete"]
                    )
                    version = connection.execute(
                        """
                        INSERT INTO ranked_day_versions (
                            player_id, ranked_day_start, ranked_day_end,
                            official_season_id, season_day_number,
                            season_anchor_rule_version, reconciliation_rule_version,
                            result_hash, input_hash,
                            parser_version, processing_version, domain_rule_version,
                            analytics_rule_version, trophy_allocation_rule_versions,
                            version, replaces_version_id, state, confidence,
                            failure_reasons, start_trophies,
                            final_trophies_before_reset, next_start_trophies,
                            expected_next_start_trophies,
                            attack_count, defense_count, attack_gain,
                            observed_defense_loss, automatic_defense_loss,
                            automatic_defense_evidence_state, net_trophy_change,
                            observed_trophy_change, boundary_adjustment,
                            boundary_adjustment_type, observed_boundary_adjustment,
                            unexplained_residual, formula_components,
                            input_evidence, coverage_evidence,
                            contribution_evidence, shield_evidence,
                            evidence_complete, coverage_complete, reconciled,
                            shield_state, shield_duration_days,
                            start_baseline_id, end_baseline_id
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        ) RETURNING id
                        """,
                        (
                            player_id,
                            ranked_day.start,
                            ranked_day.end,
                            official_season_id,
                            season_day_number,
                            SEASON_ANCHOR_RULE_VERSION,
                            RECONCILIATION_RULE_VERSION,
                            result_hash,
                            input_hash,
                            claim.parser_version,
                            claim.processing_version,
                            claim.domain_rule_version,
                            claim.analytics_rule_version,
                            Jsonb(trophy_rule_versions),
                            version_number,
                            previous_version[0]
                            if previous_version is not None
                            else None,
                            result.state,
                            result.confidence,
                            Jsonb(list(result.failure_reasons)),
                            result_data["start_trophies"],
                            result.final_trophies_before_reset,
                            result_data["next_start_trophies"],
                            result.expected_next_start_trophies,
                            result.attack_count,
                            result.defense_count,
                            result.attack_trophy_gain,
                            result.observed_defense_loss,
                            result.automatic_defense_loss,
                            result.automatic_defense_evidence_state,
                            result.net_trophy_change,
                            result.observed_trophy_change,
                            result.boundary_adjustment,
                            result.boundary_adjustment_type,
                            result.observed_boundary_adjustment,
                            result.unexplained_residual,
                            Jsonb(result.formula_components),
                            Jsonb(input_evidence),
                            Jsonb(coverage_evidence),
                            Jsonb(contribution_evidence),
                            Jsonb(result.shield_evidence),
                            evidence_complete,
                            result.coverage_complete,
                            result.state == "Complete",
                            result.shield_state,
                            result.shield_duration_days,
                            (
                                start_baseline["id"]
                                if start_baseline is not None
                                else None
                            ),
                            (end_baseline["id"] if end_baseline is not None else None),
                        ),
                    ).fetchone()
                    assert version is not None
                    version_id = int(version[0])
                    self._store_ranked_day_adjustments(connection, version_id, result)
                else:
                    version_id = int(existing[0])
                    version_number = int(existing[1])
                self._publish_player_daily_log(
                    connection,
                    player_id=player_id,
                    ranked_day_start=ranked_day.start,
                    ranked_day_end=ranked_day.end,
                    official_season_id=official_season_id,
                    season_day_number=season_day_number,
                    version_number=version_number,
                    ranked_day_version_id=version_id,
                    result=result,
                    contribution_evidence=contribution_evidence,
                )
                if existing is None:
                    # A reset sweep is the sole source of expected population.
                    # No population-wide job is created for an uncoordinated
                    # legacy fixture or a late/discovered player.
                    self._record_boundary_generation(
                        connection,
                        boundary_at=ranked_day.end,
                        player_id=player_id,
                        ranked_day_version_id=version_id,
                        ranked_day_input_hash=input_hash,
                    )
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    def complete_snapshot(self, claim: Claim) -> None:
        generation_input = claim.input_json.get("generation")
        generation_number = (
            int(generation_input) if generation_input is not None else None
        )
        ranked_day_version_id = (
            int(claim.input_json["ranked_day_version_id"])
            if generation_number is None
            else None
        )
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                boundary_text = claim.input_json.get("boundary_at")
                if boundary_text is None:
                    raise ValueError("snapshot boundary is required")
                boundary_at = datetime.fromisoformat(str(boundary_text)).astimezone(UTC)
                generation_row = None
                manifest_digest_value = None
                if generation_number is not None:
                    generation_row = connection.execute(
                        """
                        SELECT id, snapshot_state, expected_population_count,
                               expected_population_hash, snapshot_manifest_id,
                               target_at, target_rule, snapshot_rule_version
                        FROM boundary_publication_generations
                        WHERE boundary_at = %s AND generation = %s
                        FOR UPDATE
                        """,
                        (boundary_at, generation_number),
                    ).fetchone()
                    if generation_row is None:
                        raise ValueError(
                            "boundary publication generation does not exist"
                        )
                    if _text_value(generation_row[1]) in {"superseded", "published"}:
                        self._finish_claim(
                            connection,
                            claim,
                            job,
                            state="complete",
                            outcome="stale_superseded",
                        )
                        return
                    if _text_value(generation_row[1]) != "ready":
                        raise ValueError("boundary snapshot is not ready")
                    target_at = generation_row[5]
                    if _text_value(generation_row[6]) != "boundary-delay-v1":
                        raise ValueError("unsupported boundary target rule")
                    if _text_value(generation_row[7]) != ANALYTICS_RULE_VERSION:
                        raise ValueError("unsupported boundary snapshot rule")
                    now_row = connection.execute("SELECT clock_timestamp()").fetchone()
                    assert now_row is not None
                    if now_row[0] < target_at:
                        raise ValueError(
                            "snapshot publication target dependency is not ready"
                        )
                    manifest_id = (
                        int(generation_row[4])
                        if generation_row[4] is not None
                        else None
                    )
                    if manifest_id is None:
                        raise ValueError("boundary snapshot manifest is not frozen")
                    if int(claim.input_json.get("manifest_id", 0)) != manifest_id:
                        raise ValueError(
                            "boundary snapshot manifest identity does not match"
                        )
                    manifest_digest = connection.execute(
                        "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                        (manifest_id,),
                    ).fetchone()
                    if manifest_digest is None or _text_value(
                        manifest_digest[0]
                    ) != claim.input_json.get("manifest_digest"):
                        raise ValueError(
                            "boundary snapshot manifest digest does not match"
                        )
                    manifest_digest_value = _text_value(manifest_digest[0])
                    official_identity = connection.execute(
                        """
                        SELECT DISTINCT input_identity->>'official_top200_version_id'
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s
                        """,
                        (manifest_id,),
                    ).fetchall()
                    official_version_input = next(
                        (
                            int(row[0])
                            for row in official_identity
                            if row[0] is not None
                        ),
                        None,
                    )
                    source_row = connection.execute(
                        """
                        SELECT ranked.id, ranked.ranked_day_start,
                               ranked.ranked_day_end, ranked.input_hash, ranked.version
                        FROM boundary_publication_manifest_rows AS manifest
                        JOIN ranked_day_versions AS ranked
                          ON ranked.id = manifest.ranked_day_version_id
                        WHERE manifest.manifest_id = %s
                        ORDER BY ranked.id DESC
                        LIMIT 1
                        """,
                        (manifest_id,),
                    ).fetchone()
                    if source_row is None:
                        ranked_day_version_id = None
                        ranked_day = (
                            boundary_at - timedelta(days=1),
                            boundary_at,
                            "",
                            0,
                        )
                    else:
                        ranked_day_version_id = int(source_row[0])
                        ranked_day = source_row[1:]
                    if ranked_day[1] != boundary_at:
                        raise ValueError("snapshot boundary does not match generation")
                    connection.execute(
                        """
                        UPDATE boundary_publication_generations
                        SET snapshot_state = 'building', updated_at = clock_timestamp()
                        WHERE id = %s AND snapshot_state = 'ready'
                        """,
                        (generation_row[0],),
                    )
                else:
                    official_version_input = None
                    assert ranked_day_version_id is not None
                    ranked_day = connection.execute(
                        """
                        SELECT ranked_day_start, ranked_day_end, input_hash, version
                        FROM ranked_day_versions WHERE id = %s
                        """,
                        (ranked_day_version_id,),
                    ).fetchone()
                    if ranked_day is None:
                        raise ValueError(
                            "ranked-day version for snapshot does not exist"
                        )
                    if boundary_at != ranked_day[1]:
                        raise ValueError(
                            "snapshot boundary does not match ranked-day boundary"
                        )

                profile_version_ids: dict[int, int] = {}
                if generation_row is None:
                    # Direct snapshots use the newest accepted historical
                    # profile. Coordinated snapshots never execute this query:
                    # their manifest is the complete profile input.
                    profile_rows = connection.execute(
                        """
                        WITH accepted_profiles AS (
                            SELECT DISTINCT ON (v.player_id)
                                   p.id, p.normalized_tag, v.trophies,
                                   v.observation_id, v.observed_at,
                                   v.eligibility_state
                            FROM player_profile_versions AS v
                            JOIN players AS p ON p.id = v.player_id
                            WHERE v.observed_at <= %s
                              AND v.source_contract_state = 'accepted'
                            ORDER BY v.player_id, v.observed_at DESC, v.id DESC
                        )
                        SELECT id, normalized_tag, trophies, observation_id,
                               observed_at, eligibility_state
                        FROM accepted_profiles
                        WHERE eligibility_state = 'eligible'
                        ORDER BY id
                        """,
                        (boundary_at,),
                    ).fetchall()
                else:
                    manifest_profiles = connection.execute(
                        """
                        SELECT player_id, input_identity->'profile_snapshot',
                               input_identity->>'profile_version_id'
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s
                        """,
                        (generation_row[4],),
                    ).fetchall()
                    profile_version_ids = {
                        int(row[0]): int(row[2])
                        for row in manifest_profiles
                        if row[2] is not None
                    }
                    profile_rows = [
                        (
                            int(row[0]),
                            _text_value(row[1]["tag"]),
                            int(row[1]["trophies"]),
                            int(row[1]["observation_id"]),
                            datetime.fromisoformat(
                                str(row[1]["observed_at"])
                            ).astimezone(UTC),
                            _text_value(row[1]["eligibility_state"]),
                        )
                        for row in manifest_profiles
                        if isinstance(row[1], dict)
                        and _text_value(row[1].get("eligibility_state")) == "eligible"
                    ]

                official_rows = connection.execute(
                    """
                    WITH complete_versions AS (
                        SELECT v.id, v.observed_at
                        FROM official_top200_versions AS v
                        JOIN official_top200_attempts AS a ON a.id = v.attempt_id
                        JOIN official_top200_entries AS e ON e.version_id = v.id
                        WHERE a.outcome = 'official_observed'
                          AND v.observed_at <= %s
                          AND (%s::bigint IS NULL OR v.id = %s)
                        GROUP BY v.id, v.observed_at
                        HAVING count(*) = 200
                           AND count(DISTINCT e.rank) = 200
                           AND min(e.rank) = 1
                           AND max(e.rank) = 200
                    ), latest_complete AS (
                        SELECT id
                        FROM complete_versions
                        ORDER BY observed_at DESC, id DESC
                        LIMIT 1
                    )
                    SELECT e.player_id, e.rank, v.id, v.observed_at
                    FROM official_top200_entries AS e
                    JOIN official_top200_versions AS v ON v.id = e.version_id
                    JOIN latest_complete AS latest ON latest.id = v.id
                    """,
                    (boundary_at, official_version_input, official_version_input),
                ).fetchall()
                official_by_player = {
                    int(row[0]): (int(row[1]), int(row[2]), row[3])
                    for row in official_rows
                }
                if generation_row is not None:
                    manifest_officials = connection.execute(
                        """
                        SELECT player_id, input_identity->>'official_rank',
                               input_identity->>'official_rank_observed_at',
                               input_identity->>'official_top200_version_id'
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s
                        """,
                        (generation_row[4],),
                    ).fetchall()
                    official_by_player = {
                        int(row[0]): (
                            int(row[1]),
                            int(row[3]),
                            datetime.fromisoformat(str(row[2])).astimezone(UTC),
                        )
                        for row in manifest_officials
                        if row[1] is not None
                        and row[2] is not None
                        and row[3] is not None
                    }

                entries: list[dict[str, Any]] = []
                for row in profile_rows:
                    age_seconds = int((boundary_at - row[4]).total_seconds())
                    if age_seconds < 0:
                        raise ValueError("snapshot selected future profile evidence")
                    freshness = (
                        "fresh" if age_seconds <= PROFILE_FRESHNESS_SECONDS else "stale"
                    )
                    entries.append(
                        {
                            "player_id": int(row[0]),
                            "tag": _text_value(row[1]),
                            "trophies": int(row[2]),
                            "observation_id": int(row[3]),
                            "profile_version_id": profile_version_ids.get(int(row[0])),
                            "observed_at": row[4],
                            "age_seconds": age_seconds,
                            "freshness": freshness,
                            "confidence": "confirmed",
                            "tie_hash": deterministic_tag_hash(_text_value(row[1])),
                            "official": official_by_player.get(int(row[0])),
                        }
                    )
                entries.sort(
                    key=lambda item: (
                        -int(item["trophies"]),
                        str(item["tie_hash"]),
                        str(item["tag"]),
                    )
                )

                quality_row = connection.execute(
                    """
                    WITH known_players AS (
                        SELECT id
                        FROM players
                        WHERE active = true
                        UNION
                        SELECT DISTINCT v.player_id
                        FROM player_profile_versions AS v
                        WHERE v.observed_at <= %s
                        UNION
                        SELECT DISTINCT o.player_id
                        FROM collector_observations AS o
                        WHERE o.endpoint = 'profile'
                          AND o.player_id IS NOT NULL
                          AND o.response_completed_at <= %s
                    ), latest_accepted AS (
                        SELECT DISTINCT ON (v.player_id)
                               v.player_id, v.trophies, v.observed_at,
                               v.eligibility_state
                        FROM player_profile_versions AS v
                        WHERE v.observed_at <= %s
                          AND v.source_contract_state = 'accepted'
                        ORDER BY v.player_id, v.observed_at DESC, v.id DESC
                    ), latest_any AS (
                        SELECT DISTINCT ON (v.player_id)
                               v.player_id, v.eligibility_state,
                               v.source_contract_state
                        FROM player_profile_versions AS v
                        WHERE v.observed_at <= %s
                        ORDER BY v.player_id, v.observed_at DESC, v.id DESC
                    ), latest_profile_job AS (
                        SELECT DISTINCT ON (o.player_id)
                               o.player_id, j.failure_category, j.outcome
                        FROM collector_observations AS o
                        JOIN python_processing_jobs_worker AS j
                          ON j.observation_id = o.id
                        WHERE o.endpoint = 'profile'
                          AND o.player_id IS NOT NULL
                          AND o.response_completed_at <= %s
                        ORDER BY o.player_id, o.response_completed_at DESC, o.id DESC
                    ), classified AS (
                        SELECT k.id,
                               accepted.trophies,
                               accepted.observed_at,
                               accepted.eligibility_state AS accepted_state,
                               any_profile.source_contract_state AS any_source_state,
                               job.failure_category
                        FROM known_players AS k
                        LEFT JOIN latest_accepted AS accepted
                          ON accepted.player_id = k.id
                        LEFT JOIN latest_any AS any_profile
                          ON any_profile.player_id = k.id
                        LEFT JOIN latest_profile_job AS job
                          ON job.player_id = k.id
                    )
                    SELECT
                        count(*) FILTER (
                            WHERE accepted_state = 'eligible'
                        ),
                        count(*) FILTER (
                            WHERE accepted_state = 'eligible'
                              AND trophies IS NOT NULL
                        ),
                        count(*) FILTER (
                            WHERE accepted_state = 'eligible'
                              AND %s - observed_at > make_interval(secs => %s)
                        ),
                        count(*) FILTER (
                            WHERE accepted_state = 'eligible'
                              AND %s - observed_at <= make_interval(secs => %s)
                        ),
                        count(*) FILTER (
                            WHERE accepted_state IS NULL
                              AND any_source_state IS NULL
                              AND failure_category IS NULL
                        ),
                        count(*) FILTER (
                            WHERE accepted_state = 'uncertain'
                        ),
                        count(*) FILTER (
                            WHERE accepted_state IS NULL
                              AND any_source_state IS NULL
                              AND failure_category IN (
                                  'malformed_json',
                                  'unsupported_profile_schema',
                                  'source_identity_mismatch',
                                  'invalid_player_tag'
                              )
                        ),
                        count(*) FILTER (
                            WHERE accepted_state IS NULL
                              AND any_source_state = 'conflict'
                        )
                    FROM classified
                    WHERE (%s::bigint IS NULL OR id IN (
                        SELECT player_id
                        FROM boundary_publication_generation_members
                        WHERE generation_id = %s
                    ))
                    """,
                    (
                        boundary_at,
                        boundary_at,
                        boundary_at,
                        boundary_at,
                        boundary_at,
                        boundary_at,
                        PROFILE_FRESHNESS_SECONDS,
                        boundary_at,
                        PROFILE_FRESHNESS_SECONDS,
                        generation_row[0] if generation_row is not None else None,
                        generation_row[0] if generation_row is not None else None,
                    ),
                ).fetchone()
                assert quality_row is not None
                if generation_row is not None:
                    manifest_quality = connection.execute(
                        """
                        SELECT player_id, classification
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s
                        """,
                        (generation_row[4],),
                    ).fetchall()
                    included_players = {int(entry["player_id"]) for entry in entries}
                    by_classification: dict[str, int] = {}
                    excluded_classification: dict[str, int] = {}
                    for player_id, classification in manifest_quality:
                        name = _text_value(classification)
                        by_classification[name] = by_classification.get(name, 0) + 1
                        if int(player_id) not in included_players:
                            excluded_classification[name] = (
                                excluded_classification.get(name, 0) + 1
                            )
                    if sum(by_classification.values()) != int(generation_row[2]):
                        raise ValueError(
                            "snapshot manifest coverage does not match expected population"
                        )
                    if len(entries) + sum(excluded_classification.values()) != int(
                        generation_row[2]
                    ):
                        raise ValueError("snapshot output coverage is not reconciled")
                    quality = {
                        "expected_population_count": int(generation_row[2]),
                        "classification_counts": by_classification,
                        "excluded_classification_counts": excluded_classification,
                        "eligible_population_count": int(generation_row[2]),
                        "included_entry_count": len(entries),
                        "stale_entry_count": sum(
                            entry["freshness"] == "stale" for entry in entries
                        ),
                        "fresh_entry_count": sum(
                            entry["freshness"] == "fresh" for entry in entries
                        ),
                        "excluded_missing_count": excluded_classification.get(
                            "Missing", 0
                        ),
                        "excluded_unavailable_count": excluded_classification.get(
                            "Unavailable", 0
                        )
                        + excluded_classification.get("Failed", 0),
                        "excluded_invalid_count": 0,
                        "excluded_malformed_count": excluded_classification.get(
                            "Malformed", 0
                        ),
                        "excluded_conflicting_count": 0,
                        "excluded_partial_count": excluded_classification.get(
                            "Partial", 0
                        ),
                        "excluded_inconsistent_count": excluded_classification.get(
                            "Inconsistent", 0
                        ),
                    }
                else:
                    quality = {
                        "eligible_population_count": int(quality_row[0]),
                        "included_entry_count": int(quality_row[1]),
                        "stale_entry_count": int(quality_row[2]),
                        "fresh_entry_count": int(quality_row[3]),
                        "excluded_missing_count": int(quality_row[4]),
                        "excluded_unavailable_count": 0,
                        "excluded_invalid_count": int(quality_row[5]),
                        "excluded_malformed_count": int(quality_row[6]),
                        "excluded_conflicting_count": int(quality_row[7]),
                        "excluded_partial_count": 0,
                        "excluded_inconsistent_count": 0,
                    }
                if quality["included_entry_count"] != len(entries):
                    raise ValueError("snapshot quality count does not match entries")
                coverage = (
                    quality["included_entry_count"]
                    / quality["eligible_population_count"]
                    if quality["eligible_population_count"]
                    else 0.0
                )
                hash_entries = [
                    {
                        "player_id": entry["player_id"],
                        "tag": entry["tag"],
                        "trophies": entry["trophies"],
                        "profile_observation_id": entry["observation_id"],
                        "profile_observed_at": entry["observed_at"]
                        .astimezone(UTC)
                        .isoformat(),
                        "profile_age_seconds": entry["age_seconds"],
                        "profile_freshness": entry["freshness"],
                        "profile_confidence": entry["confidence"],
                        "tie_hash": entry["tie_hash"],
                        "official_rank": (
                            entry["official"][0]
                            if entry["official"] is not None
                            else None
                        ),
                        "official_rank_version_id": (
                            entry["official"][1]
                            if entry["official"] is not None
                            else None
                        ),
                        "official_rank_observed_at": (
                            entry["official"][2].astimezone(UTC).isoformat()
                            if entry["official"] is not None
                            else None
                        ),
                    }
                    for entry in entries
                ]
                if generation_row is not None:
                    hash_payload = {
                        "manifest_digest": manifest_digest_value,
                        "rule_versions": {
                            "ordering_rule_version": SNAPSHOT_ORDERING_RULE_VERSION,
                            "freshness_rule_version": FRESHNESS_RULE_VERSION,
                            "analytics_rule_version": _text_value(generation_row[7]),
                        },
                        "entries": hash_entries,
                        "quality": quality,
                    }
                else:
                    hash_payload = {
                        "boundary_at": boundary_at.astimezone(UTC).isoformat(),
                        "source_ranked_day_version_id": ranked_day_version_id,
                        "source_ranked_day_version": int(ranked_day[3]),
                        "source_ranked_day_input_hash": _text_value(ranked_day[2]),
                        "ordering_rule_version": SNAPSHOT_ORDERING_RULE_VERSION,
                        "freshness_rule_version": FRESHNESS_RULE_VERSION,
                        "entries": hash_entries,
                        "quality": quality,
                    }
                input_hash = hashlib.sha256(
                    json.dumps(
                        hash_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                frozen_snapshot_id, frozen_snapshot_version = (
                    self._publish_snapshot_kind(
                        connection,
                        snapshot_kind="frozen",
                        boundary_at=boundary_at,
                        ranked_day_version_id=(
                            None
                            if generation_row is not None
                            else ranked_day_version_id
                        ),
                        entries=entries,
                        coverage=coverage,
                        quality=quality,
                        input_hash=input_hash,
                        publish=False,
                    )
                )
                self._publish_snapshot_kind(
                    connection,
                    snapshot_kind="live",
                    boundary_at=boundary_at,
                    ranked_day_version_id=(
                        None if generation_row is not None else ranked_day_version_id
                    ),
                    entries=entries,
                    coverage=coverage,
                    quality=quality,
                    input_hash=input_hash,
                    publish=True,
                )
                if generation_row is not None:
                    connection.execute(
                        """
                        UPDATE boundary_publication_generations
                        SET snapshot_id = %s, snapshot_input_hash = %s,
                            snapshot_coverage = %s,
                            updated_at = clock_timestamp()
                        WHERE id = %s AND snapshot_state = 'building'
                        """,
                        (
                            frozen_snapshot_id,
                            input_hash,
                            Jsonb(quality),
                            generation_row[0],
                        ),
                    )
                self._enqueue_snapshot_analytics(
                    connection,
                    snapshot_id=frozen_snapshot_id,
                    snapshot_version=frozen_snapshot_version,
                    snapshot_input_hash=input_hash,
                    ranked_day_version_id=(
                        None if generation_row is not None else ranked_day_version_id
                    ),
                    generation_number=generation_number,
                    manifest_id=(
                        int(generation_row[4]) if generation_row is not None else None
                    ),
                    manifest_digest=(
                        _text_value(
                            connection.execute(
                                "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                                (generation_row[4],),
                            ).fetchone()[0]
                        )
                        if generation_row is not None
                        else None
                    ),
                    boundary_at=(boundary_at if generation_row is not None else None),
                    period_start=ranked_day[0],
                    period_end=ranked_day[1],
                )
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    def reevaluate_boundary_publications(self) -> int:
        """Recover ready coordinator generations after worker restart/handoff."""
        with self.pool.connection() as connection:
            if (
                connection.execute(
                    "SELECT to_regclass(current_schema() || '.boundary_publication_generations')"
                ).fetchone()[0]
                is None
            ):
                return 0
            with connection.transaction():
                generations = connection.execute(
                    """
                    SELECT id, boundary_at
                    FROM boundary_publication_generations
                    WHERE snapshot_state IN ('pending', 'ready')
                       OR army_state IN ('pending', 'ready')
                    ORDER BY boundary_at, generation
                    FOR UPDATE
                    """
                ).fetchall()
                for generation_id, boundary_at in generations:
                    self._try_enqueue_boundary_artifacts(
                        connection,
                        boundary_at=boundary_at,
                        generation_id=int(generation_id),
                    )
                corrections = connection.execute(
                    """
                    SELECT source_generation_id
                    FROM boundary_publication_corrections
                    WHERE state IN ('queued', 'pending_inputs')
                    ORDER BY requested_at, id
                    FOR UPDATE SKIP LOCKED
                    """
                ).fetchall()
                for (source_generation_id,) in corrections:
                    self._maybe_emit_boundary_signal(
                        connection, generation_id=int(source_generation_id)
                    )
                return len(generations) + len(corrections)

    @staticmethod
    def _create_boundary_artifact_identity(
        connection: Any,
        *,
        generation_id: int,
        artifact_kind: str,
        manifest_id: int,
        input_hash: str,
        source_identity: dict[str, Any],
    ) -> int:
        row = connection.execute(
            """
            INSERT INTO boundary_publication_artifact_identities
                (generation_id, artifact_kind, manifest_id, input_hash, source_identity)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (generation_id, artifact_kind, manifest_id) DO NOTHING
            RETURNING id
            """,
            (
                generation_id,
                artifact_kind,
                manifest_id,
                input_hash,
                Jsonb(source_identity),
            ),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT id, input_hash, source_identity
                FROM boundary_publication_artifact_identities
                WHERE generation_id = %s AND artifact_kind = %s AND manifest_id = %s
                FOR UPDATE
                """,
                (generation_id, artifact_kind, manifest_id),
            ).fetchone()
            if (
                row is None
                or _text_value(row[1]) != input_hash
                or dict(row[2]) != source_identity
            ):
                raise ValueError("boundary publication artifact identity conflict")
        return int(row[0])

    def _maybe_emit_boundary_signal(self, connection: Any, generation_id: int) -> None:
        boundary_row = connection.execute(
            "SELECT boundary_at FROM boundary_publication_generations WHERE id = %s",
            (generation_id,),
        ).fetchone()
        if boundary_row is None:
            return
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"boundary-publication:{boundary_row[0].astimezone(UTC).isoformat()}",),
        )
        row = connection.execute(
            """
            SELECT boundary_at, generation, snapshot_id,
                   snapshot_input_hash, snapshot_analytics_publication_id,
                   army_publication_id, supersedes_id,
                   snapshot_manifest_id, army_manifest_id,
                   correction_state, army_input_hash,
                   snapshot_rule_version, army_rule_version,
                   ordering_rule_version, freshness_rule_version
            FROM boundary_publication_generations
            WHERE id = %s
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if row is None or any(value is None for value in row[2:6]):
            return
        identities = connection.execute(
            """
            SELECT count(*)
            FROM boundary_publication_artifact_identities
            WHERE (id, artifact_kind, manifest_id, input_hash) IN (
                (%s, 'analytics', %s, %s),
                (%s, 'army', %s, %s)
            )
            """,
            (row[4], row[7], _text_value(row[3]), row[5], row[8], _text_value(row[10])),
        ).fetchone()
        if identities is None or int(identities[0]) != 2:
            return
        connection.execute(
            """
            INSERT INTO boundary_publication_events (
                boundary_at, generation, snapshot_id, snapshot_input_hash,
                snapshot_analytics_publication_id, army_publication_id,
                superseded_generation, manifest_ids, rule_versions
            ) VALUES (%s, %s, %s, %s, %s, %s,
                      (SELECT generation FROM boundary_publication_generations WHERE id = %s),
                      %s, %s)
            ON CONFLICT (boundary_at, generation) DO NOTHING
            """,
            (
                row[0],
                int(row[1]),
                int(row[2]),
                _text_value(row[3]),
                int(row[4]),
                int(row[5]),
                row[6],
                Jsonb(
                    {
                        "snapshot": int(row[7]) if row[7] is not None else None,
                        "army": int(row[8]) if row[8] is not None else None,
                    }
                ),
                Jsonb(
                    {
                        "ordering_rule_version": _text_value(row[13]),
                        "freshness_rule_version": _text_value(row[14]),
                        "analytics_rule_version": _text_value(row[11]),
                        "army_analytics_rule_version": _text_value(row[12]),
                    }
                ),
            ),
        )
        if _text_value(row[9]) in {"none", "active", "finalized"}:
            connection.execute(
                "UPDATE boundary_publication_generations SET correction_state = 'finalized', updated_at = clock_timestamp() WHERE id = %s",
                (generation_id,),
            )
            connection.execute(
                """
                UPDATE boundary_publication_corrections
                SET state = 'finalized', finalized_at = clock_timestamp()
                WHERE generation_id = %s AND state = 'active'
                """,
                (generation_id,),
            )
            queued = connection.execute(
                """
                SELECT id, affected_artifacts, pending_inputs
                FROM boundary_publication_corrections
                WHERE boundary_at = %s AND state IN ('queued', 'pending_inputs')
                ORDER BY requested_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (row[0],),
            ).fetchone()
            if queued is not None:
                source = connection.execute(
                    """
                    SELECT sweep_id, expected_population_count, expected_population_hash,
                           ordering_rule_version, freshness_rule_version,
                           generation + 1, membership_rule_version,
                           snapshot_rule_version, army_rule_version,
                           target_rule, target_at
                    FROM boundary_publication_generations
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (generation_id,),
                ).fetchone()
                assert source is not None
                pending_inputs = queued[2] if isinstance(queued[2], list) else []
                pending_ready = True
                for pending in pending_inputs:
                    pending_version = (
                        pending.get("ranked_day_version_id")
                        if isinstance(pending, dict)
                        else None
                    )
                    if pending_version is None:
                        continue
                    pending_state = connection.execute(
                        "SELECT state FROM ranked_day_versions WHERE id = %s",
                        (pending_version,),
                    ).fetchone()
                    if pending_state is None or _text_value(pending_state[0]) not in {
                        "Complete",
                        "Partial",
                        "Failed",
                        "Missing",
                        "Unavailable",
                        "Inconsistent",
                        "Malformed",
                    }:
                        pending_ready = False
                        break
                if not pending_ready:
                    connection.execute(
                        "UPDATE boundary_publication_corrections SET state = 'pending_inputs' WHERE id = %s",
                        (queued[0],),
                    )
                    return
                connection.execute(
                    "UPDATE boundary_publication_corrections SET state = 'activation' WHERE id = %s",
                    (queued[0],),
                )
                new = connection.execute(
                    """
                    INSERT INTO boundary_publication_generations (
                        boundary_at, generation, sweep_id, ordering_rule_version,
                        freshness_rule_version, expected_population_count,
                        expected_population_hash, membership_rule_version,
                        snapshot_rule_version, army_rule_version,
                        target_rule, target_at, supersedes_id, source_generation_id,
                        correction_state, affected_artifacts
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
                    RETURNING id
                    """,
                    (
                        row[0],
                        int(source[5]),
                        source[0],
                        source[3],
                        source[4],
                        source[1],
                        source[2],
                        source[6],
                        source[7],
                        source[8],
                        source[9],
                        source[10],
                        generation_id,
                        generation_id,
                        queued[1],
                    ),
                ).fetchone()
                assert new is not None
                new_id = int(new[0])
                connection.execute(
                    "UPDATE boundary_publication_corrections SET state = 'inheritance' WHERE id = %s",
                    (queued[0],),
                )
                connection.execute(
                    """
                    INSERT INTO boundary_publication_generation_members
                        (generation_id, player_id, ranked_day_version_id,
                         ranked_day_input_hash, status, snapshot_status, army_status)
                    SELECT %s, player_id, ranked_day_version_id,
                           ranked_day_input_hash, status, snapshot_status, army_status
                    FROM boundary_publication_generation_members
                    WHERE generation_id = %s
                    """,
                    (new_id, generation_id),
                )
                for pending in queued[2] if isinstance(queued[2], list) else []:
                    pending_version = pending.get("ranked_day_version_id")
                    pending_snapshot_status = self._boundary_snapshot_status(
                        connection,
                        player_id=int(pending["player_id"]),
                        ranked_day_version_id=int(pending_version),
                        boundary_at=row[0],
                    )
                    pending_army_status = self._boundary_army_status(
                        connection,
                        player_id=int(pending["player_id"]),
                        ranked_day_version_id=int(pending_version),
                        snapshot_status=pending_snapshot_status,
                    )
                    connection.execute(
                        """
                        UPDATE boundary_publication_generation_members
                        SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                            status = 'terminal', snapshot_status = %s,
                            army_status = %s, updated_at = clock_timestamp()
                        WHERE generation_id = %s AND player_id = %s
                        """,
                        (
                            pending_version,
                            pending.get("input_hash"),
                            pending_snapshot_status,
                            pending_army_status,
                            new_id,
                            pending.get("player_id"),
                        ),
                    )
                connection.execute(
                    "UPDATE boundary_publication_generations SET membership_captured_at = clock_timestamp() WHERE id = %s",
                    (new_id,),
                )
                affected = [_text_value(value) for value in (queued[1] or [])]
                if "snapshot" not in affected:
                    connection.execute(
                        """
                        UPDATE boundary_publication_generations AS target
                        SET snapshot_state = source.snapshot_state,
                            snapshot_id = source.snapshot_id,
                            snapshot_input_hash = source.snapshot_input_hash,
                            snapshot_manifest_id = source.snapshot_manifest_id,
                            snapshot_analytics_publication_id = source.snapshot_analytics_publication_id,
                            snapshot_coverage = source.snapshot_coverage
                        FROM boundary_publication_generations AS source
                        WHERE target.id = %s AND source.id = %s
                        """,
                        (new_id, generation_id),
                    )
                if "army" not in affected:
                    connection.execute(
                        """
                        UPDATE boundary_publication_generations AS target
                        SET army_state = source.army_state,
                            army_input_hash = source.army_input_hash,
                            army_manifest_id = source.army_manifest_id,
                            army_publication_id = source.army_publication_id,
                            army_coverage = source.army_coverage
                        FROM boundary_publication_generations AS source
                        WHERE target.id = %s AND source.id = %s
                        """,
                        (new_id, generation_id),
                    )
                # Unaffected artifacts retain the exact immutable publication
                # identity. Only the affected artifact receives a new identity.
                connection.execute(
                    "UPDATE boundary_publication_corrections SET state = 'active', generation_id = %s, started_at = clock_timestamp() WHERE id = %s",
                    (new_id, queued[0]),
                )
                self._try_enqueue_boundary_artifacts(
                    connection, boundary_at=row[0], generation_id=new_id
                )

    def complete_analytics(self, claim: Claim) -> None:
        snapshot_id = _positive_int_input(claim.input_json, "snapshot_id")
        snapshot_version = _positive_int_input(claim.input_json, "snapshot_version")
        source_value = claim.input_json.get("source_ranked_day_version_id")
        generation_input = claim.input_json.get("generation")
        source_ranked_day_version_id = (
            _positive_int_input(claim.input_json, "source_ranked_day_version_id")
            if source_value is not None and generation_input is None
            else None
        )
        snapshot_input_hash = _hash_input(
            claim.input_json.get("snapshot_input_hash"), "snapshot_input_hash"
        )
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("leaderboard-snapshot-v2:frozen:analytics:" + str(snapshot_id),),
                )
                snapshot = connection.execute(
                    """
                    SELECT id, boundary_at, version, correction_of_id, state,
                           source_ranked_day_version_id, input_hash,
                           measured_coverage, stale_entry_count,
                           fresh_entry_count, included_entry_count
                    FROM leaderboard_snapshots
                    WHERE id = %s AND snapshot_kind = 'frozen'
                    FOR UPDATE
                    """,
                    (snapshot_id,),
                ).fetchone()
                if snapshot is None:
                    raise ValueError("frozen snapshot dependency is not complete")
                boundary_generation = connection.execute(
                    """
                    SELECT id, generation, snapshot_state
                    FROM boundary_publication_generations
                    WHERE snapshot_id = %s
                    ORDER BY generation DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (snapshot_id,),
                ).fetchone()
                if boundary_generation is not None and _text_value(
                    boundary_generation[2]
                ) in {"superseded", "published"}:
                    self._finish_claim(
                        connection,
                        claim,
                        job,
                        state="complete",
                        outcome="stale_superseded",
                    )
                    return
                if int(snapshot[2]) != snapshot_version:
                    raise ValueError(
                        "analytics snapshot version does not match its input"
                    )
                if (
                    source_ranked_day_version_id is not None
                    and snapshot[5] is not None
                    and int(snapshot[5]) != source_ranked_day_version_id
                ):
                    raise ValueError(
                        "analytics ranked-day source does not match its snapshot"
                    )
                if _text_value(snapshot[6]) != snapshot_input_hash:
                    raise ValueError(
                        "analytics snapshot input hash does not match its input"
                    )

                existing_summary_count = connection.execute(
                    """
                    SELECT count(DISTINCT s.lens), count(*),
                           count(*) FILTER (WHERE b.summary_id IS NOT NULL)
                    FROM analytics_summaries AS s
                    LEFT JOIN analytics_breakdowns AS b
                      ON b.summary_id = s.id
                     AND b.army_archetype = 'Unclassified'
                    WHERE s.snapshot_id = %s
                    """,
                    (snapshot_id,),
                ).fetchone()
                assert existing_summary_count is not None
                if _text_value(snapshot[4]) == "published":
                    if tuple(int(value) for value in existing_summary_count) != (
                        2,
                        2,
                        2,
                    ):
                        raise ValueError(
                            "published frozen snapshot has incomplete analytics"
                        )
                    if boundary_generation is not None:
                        self._maybe_emit_boundary_signal(
                            connection, int(boundary_generation[0])
                        )
                    self._finish_claim(
                        connection, claim, job, state="complete", outcome="processed"
                    )
                    return
                if _text_value(snapshot[4]) != "building":
                    raise ValueError("frozen snapshot is not available for analytics")

                ranked_day = None
                if source_ranked_day_version_id is not None:
                    ranked_day = connection.execute(
                        "SELECT ranked_day_start, ranked_day_end FROM ranked_day_versions WHERE id = %s",
                        (source_ranked_day_version_id,),
                    ).fetchone()
                if ranked_day is None and boundary_generation is not None:
                    ranked_day = connection.execute(
                        "SELECT boundary_at - interval '1 day', boundary_at FROM boundary_publication_generations WHERE id = %s",
                        (boundary_generation[0],),
                    ).fetchone()
                if ranked_day is None:
                    raise ValueError("ranked-day version for analytics does not exist")
                period_start = ranked_day[0]
                period_end = ranked_day[1]
                input_period_start = claim.input_json.get("period_start")
                input_period_end = claim.input_json.get("period_end")
                if (
                    input_period_start is not None
                    and _parse_utc(input_period_start) != period_start
                ):
                    raise ValueError("analytics period start does not match ranked day")
                if (
                    input_period_end is not None
                    and _parse_utc(input_period_end) != period_end
                ):
                    raise ValueError("analytics period end does not match ranked day")
                population_filter = claim.input_json.get(
                    "population_filter", {"population": "tracked_players"}
                )
                if population_filter != {"population": "tracked_players"}:
                    raise ValueError(
                        "analytics population filter is not the tracked population"
                    )
                entry_count = connection.execute(
                    """
                    SELECT count(*)
                    FROM leaderboard_snapshot_entries
                    WHERE snapshot_id = %s
                    """,
                    (snapshot_id,),
                ).fetchone()
                assert entry_count is not None
                if int(entry_count[0]) != int(snapshot[10]):
                    raise ValueError("frozen snapshot entries are incomplete")

                freshness = _snapshot_freshness(
                    included_count=int(snapshot[10]),
                    fresh_count=int(snapshot[9]),
                    stale_count=int(snapshot[8]),
                )
                prior_snapshot_id = (
                    int(snapshot[3]) if snapshot[3] is not None else None
                )
                for lens, perspective in (
                    ("offense", "attacker"),
                    ("defense", "defender"),
                ):
                    sample_rows = connection.execute(
                        """
                        SELECT *
                        FROM (
                            SELECT DISTINCT ON (b.id)
                                   b.id AS battle_id, b.disagreement_state, e.stars,
                                   e.army_share_code, e.id AS evidence_id, e.source_row_id,
                                   e.observation_id, e.source_observed_at,
                                   e.battle_timestamp
                            FROM battle_perspectives AS p
                            JOIN legend_battles AS b ON b.id = p.battle_id
                            JOIN battle_evidence AS e ON e.id = p.evidence_id
                            JOIN battle_source_rows AS source_row
                              ON source_row.id = e.source_row_id
                            JOIN leaderboard_snapshot_entries AS se
                              ON se.snapshot_id = %s
                             AND se.player_id = CASE
                                 WHEN p.perspective = 'attacker'
                                     THEN b.attacker_player_id
                                 ELSE b.defender_player_id
                             END
                            WHERE p.perspective = %s
                              AND e.reporting_player_id = CASE
                                 WHEN p.perspective = 'attacker'
                                     THEN b.attacker_player_id
                                 ELSE b.defender_player_id
                              END
                              AND source_row.outcome = 'valid_legend'
                              AND e.army_share_code IS NOT NULL
                              AND e.army_share_code <> ''
                              AND e.battle_timestamp >= %s
                              AND e.battle_timestamp < %s
                            ORDER BY b.id, p.source_observed_at DESC, e.id DESC
                        ) AS latest
                        ORDER BY latest.battle_timestamp, latest.battle_id
                        """,
                        (snapshot_id, perspective, period_start, period_end),
                    ).fetchall()
                    quality = connection.execute(
                        """
                        SELECT
                            0,
                            count(*) FILTER (
                                WHERE source_row.outcome = 'malformed_legend_row'
                                   OR (
                                      source_row.outcome = 'valid_legend'
                                      AND (
                                          NOT (source_row.source_json ? 'armyShareCode')
                                          OR source_row.source_json ->> 'armyShareCode' IS NULL
                                          OR source_row.source_json ->> 'armyShareCode' = ''
                                      )
                                   )
                            )
                        FROM battle_log_observations AS log
                        JOIN battle_source_rows AS source_row
                          ON source_row.battle_log_observation_id = log.id
                        JOIN leaderboard_snapshot_entries AS se
                          ON se.snapshot_id = %s AND se.player_id = log.player_id
                        WHERE log.observed_at >= %s
                          AND log.observed_at < %s
                          AND (
                              (
                                  log.parser_version = 'supercell-source-parser-v2'
                                  AND source_row.source_json ->> 'attack' = %s
                              )
                              OR (
                                  log.parser_version = 'supercell-source-parser-v1'
                                  AND source_row.source_json ->> 'attackOrDefense' = %s
                              )
                              OR source_row.outcome <> 'valid_legend'
                              OR (
                                  source_row.outcome = 'valid_legend'
                                  AND (
                                      NOT (source_row.source_json ? 'armyShareCode')
                                      OR source_row.source_json ->> 'armyShareCode' IS NULL
                                      OR source_row.source_json ->> 'armyShareCode' = ''
                                  )
                              )
                          )
                        """,
                        (
                            snapshot_id,
                            period_start,
                            period_end,
                            "true" if perspective == "attacker" else "false",
                            "attack" if perspective == "attacker" else "defense",
                        ),
                    ).fetchone()
                    assert quality is not None
                    missing_code_count = int(quality[0])
                    malformed_code_count = int(quality[1])
                    sample_size = len(sample_rows)
                    three_star_count = sum(int(row[2]) == 3 for row in sample_rows)
                    disagreement_count = sum(
                        _text_value(row[1]) == "disagreement" for row in sample_rows
                    )
                    sample_payload = [
                        {
                            "battle_id": int(row[0]),
                            "evidence_id": int(row[4]),
                            "source_row_id": int(row[5]),
                            "observation_id": int(row[6]),
                            "source_observed_at": row[7].astimezone(UTC).isoformat(),
                            "battle_timestamp": row[8].astimezone(UTC).isoformat(),
                            "stars": int(row[2]),
                            "army_share_code": _text_value(row[3]),
                            "disagreement": _text_value(row[1]) == "disagreement",
                        }
                        for row in sample_rows
                    ]
                    analytics_input = {
                        "snapshot_id": snapshot_id,
                        "snapshot_version": snapshot_version,
                        "snapshot_input_hash": snapshot_input_hash,
                        **(
                            {
                                "source_ranked_day_version_id": source_ranked_day_version_id
                            }
                            if source_ranked_day_version_id is not None
                            else {}
                        ),
                        **(
                            {"generation": int(boundary_generation[1])}
                            if boundary_generation is not None
                            else {}
                        ),
                        "lens": lens,
                        "population_filter": population_filter,
                        "period_start": period_start.astimezone(UTC).isoformat(),
                        "period_end": period_end.astimezone(UTC).isoformat(),
                        "sample": sample_payload,
                        "missing_code_count": missing_code_count,
                        "malformed_code_count": malformed_code_count,
                        "analytics_rule_version": ANALYTICS_RULE_VERSION,
                        "classification_version": CLASSIFICATION_VERSION,
                    }
                    analytics_input_hash = hashlib.sha256(
                        json.dumps(
                            analytics_input,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    correction_of_id = None
                    if prior_snapshot_id is not None:
                        correction = connection.execute(
                            """
                            SELECT id
                            FROM analytics_summaries
                            WHERE snapshot_id = %s
                              AND lens = %s
                              AND population_filter = %s
                              AND period_start = %s
                              AND period_end = %s
                              AND analytics_rule_version = %s
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (
                                prior_snapshot_id,
                                lens,
                                Jsonb(population_filter),
                                period_start,
                                period_end,
                                ANALYTICS_RULE_VERSION,
                            ),
                        ).fetchone()
                        if correction is not None:
                            correction_of_id = int(correction[0])
                    summary = connection.execute(
                        """
                        INSERT INTO analytics_summaries (
                            snapshot_id, snapshot_version,
                            source_ranked_day_version_id, correction_of_id,
                            lens, population_filter,
                            period_start, period_end, sample_size,
                            measured_coverage, freshness, classification_version,
                            classification_confidence, unclassified_count,
                            disagreement_count, missing_code_count,
                            malformed_code_count, analytics_rule_version, input_hash
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (
                            snapshot_id, lens, population_filter,
                            period_start, period_end, analytics_rule_version
                        ) DO NOTHING
                        RETURNING id
                        """,
                        (
                            snapshot_id,
                            snapshot_version,
                            source_ranked_day_version_id,
                            correction_of_id,
                            lens,
                            Jsonb(population_filter),
                            period_start,
                            period_end,
                            sample_size,
                            snapshot[7],
                            freshness,
                            CLASSIFICATION_VERSION,
                            CLASSIFICATION_CONFIDENCE,
                            sample_size,
                            disagreement_count,
                            missing_code_count,
                            malformed_code_count,
                            ANALYTICS_RULE_VERSION,
                            analytics_input_hash,
                        ),
                    ).fetchone()
                    if summary is None:
                        summary = connection.execute(
                            """
                            SELECT id, input_hash
                            FROM analytics_summaries
                            WHERE snapshot_id = %s
                              AND lens = %s
                              AND population_filter = %s
                              AND period_start = %s
                              AND period_end = %s
                              AND analytics_rule_version = %s
                            """,
                            (
                                snapshot_id,
                                lens,
                                Jsonb(population_filter),
                                period_start,
                                period_end,
                                ANALYTICS_RULE_VERSION,
                            ),
                        ).fetchone()
                        if (
                            summary is None
                            or _text_value(summary[1]) != analytics_input_hash
                        ):
                            raise ValueError(
                                "analytics replay has an immutable input conflict"
                            )
                    summary_id = int(summary[0])
                    evidence_json = {
                        "army_share_codes": [
                            _text_value(row[3]) for row in sample_rows
                        ],
                        "battle_ids": [int(row[0]) for row in sample_rows],
                        "evidence_ids": [int(row[4]) for row in sample_rows],
                        "source_row_ids": [int(row[5]) for row in sample_rows],
                    }
                    connection.execute(
                        """
                        INSERT INTO analytics_breakdowns (
                            summary_id, army_archetype, attack_count,
                            three_star_count, usage_rate, three_star_rate,
                            evidence_json
                        ) VALUES (%s, 'Unclassified', %s, %s, %s, %s, %s)
                        ON CONFLICT (summary_id, army_archetype) DO NOTHING
                        """,
                        (
                            summary_id,
                            sample_size,
                            three_star_count,
                            1.0 if sample_size else None,
                            three_star_count / sample_size if sample_size else None,
                            Jsonb(evidence_json),
                        ),
                    )

                complete = connection.execute(
                    """
                    SELECT count(DISTINCT s.lens), count(*),
                           count(*) FILTER (WHERE b.summary_id IS NOT NULL)
                    FROM analytics_summaries AS s
                    LEFT JOIN analytics_breakdowns AS b
                      ON b.summary_id = s.id
                     AND b.army_archetype = 'Unclassified'
                    WHERE s.snapshot_id = %s
                    """,
                    (snapshot_id,),
                ).fetchone()
                assert complete is not None
                if tuple(int(value) for value in complete) != (2, 2, 2):
                    raise ValueError("analytics publication is incomplete")
                published = connection.execute(
                    """
                    UPDATE leaderboard_snapshots
                    SET state = 'published', published_at = clock_timestamp()
                    WHERE id = %s AND state = 'building'
                    """,
                    (snapshot_id,),
                )
                if published.rowcount != 1:
                    raise ValueError("frozen snapshot publication fence was lost")
                connection.execute(
                    """
                    UPDATE leaderboard_snapshots
                    SET state = 'superseded'
                    WHERE snapshot_kind = 'frozen'
                      AND boundary_at = %s
                      AND id <> %s
                      AND state = 'published'
                    """,
                    (snapshot[1], snapshot_id),
                )
                if boundary_generation is not None:
                    summary_ids = [
                        int(row[0])
                        for row in connection.execute(
                            "SELECT id FROM analytics_summaries WHERE snapshot_id = %s ORDER BY lens, id",
                            (snapshot_id,),
                        ).fetchall()
                    ]
                    analytics_identity = self._create_boundary_artifact_identity(
                        connection,
                        generation_id=int(boundary_generation[0]),
                        artifact_kind="analytics",
                        manifest_id=int(
                            connection.execute(
                                "SELECT snapshot_manifest_id FROM boundary_publication_generations WHERE id = %s",
                                (boundary_generation[0],),
                            ).fetchone()[0]
                        ),
                        input_hash=_text_value(snapshot[6]),
                        source_identity={
                            "snapshot_id": snapshot_id,
                            "summary_ids": summary_ids,
                        },
                    )
                    published_generation = connection.execute(
                        """
                        UPDATE boundary_publication_generations
                        SET snapshot_state = 'published',
                            snapshot_analytics_publication_id = %s,
                            updated_at = clock_timestamp()
                        WHERE id = %s AND snapshot_state = 'building'
                          AND snapshot_manifest_id = %s
                        RETURNING id
                        """,
                        (
                            analytics_identity,
                            boundary_generation[0],
                            int(claim.input_json["manifest_id"]),
                        ),
                    ).fetchone()
                    if published_generation is None:
                        raise ValueError(
                            "boundary publication generation fence was lost"
                        )
                    self._maybe_emit_boundary_signal(
                        connection, int(boundary_generation[0])
                    )
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    def complete_classified(self, claim: Claim, *, outcome: str) -> None:
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                self._record_processing_outcome(
                    connection,
                    claim,
                    outcome="non_success"
                    if outcome == "source_non_success"
                    else outcome,
                )
                self._refresh_reset_baseline_evidence(connection, claim)
                if claim.endpoint == "global_player_rankings":
                    self._record_official_failed_attempt(
                        connection, claim, outcome="non_success", category="non_success"
                    )
                self._finish_claim(
                    connection, claim, job, state="complete", outcome=outcome
                )

    def fail_claim(
        self,
        claim: Claim,
        *,
        category: str,
        detail: str,
        retryable: bool,
    ) -> str:
        safe_detail = detail[:500]
        with self.pool.connection() as connection:
            with connection.transaction():
                self._lock_live_claim(connection, claim)
                self._ensure_dependency_support_probed()
                dependency = (
                    self._supports_dependency_deferral
                    and retryable
                    and category
                    in {
                        "archive_unavailable",
                        "archive_missing",
                        "spool_io_failed",
                        "degraded_capacity",
                        "archive_network_uncertain",
                    }
                )
                # Ordinary budget ordinal: a resumed dependency claim re-runs
                # its original slot, so it does not advance the counter.
                effective_attempt = claim.attempt_count + (
                    0 if claim.is_dependency_resume else 1
                )
                should_retry = dependency or (
                    retryable and effective_attempt < claim.max_attempts
                )
                state = (
                    "waiting_dependency"
                    if dependency
                    else ("waiting_retry" if should_retry else "failed")
                )
                outcome = (
                    "dependency_deferred"
                    if dependency
                    else ("retryable_failure" if should_retry else "durable_failure")
                )
                connection.execute(
                    """
                    UPDATE python_processing_attempts
                    SET state = %s, completed_at = clock_timestamp(),
                        outcome = %s, failure_category = %s
                    WHERE id = %s AND job_id = %s AND lease_token = %s
                    """,
                    (
                        state,
                        outcome,
                        category,
                        claim.attempt_id,
                        claim.job_id,
                        claim.lease_token,
                    ),
                )
                dependency_set = (
                    """
                        due_at = CASE WHEN %s THEN clock_timestamp() + (LEAST(300, (2 ^ LEAST(dependency_deferral_count, 8))) * interval '1 second') ELSE due_at END,
                        dependency_deferral_count = dependency_deferral_count + CASE WHEN %s THEN 1 ELSE 0 END,"""
                    if self._supports_dependency_deferral
                    else """
                        due_at = CASE WHEN %s THEN clock_timestamp() + interval '1 second' ELSE due_at END,"""
                )
                dependency_params = (
                    (should_retry, dependency)
                    if self._supports_dependency_deferral
                    else (should_retry,)
                )
                completed = connection.execute(
                    f"""
                    UPDATE {self._jobs_relation}
                    SET state = %s,
                        {dependency_set}
                        outcome = %s, failure_category = %s, failure_detail = %s,
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        updated_at = clock_timestamp(),
                        completed_at = CASE WHEN %s THEN NULL ELSE clock_timestamp() END
                    WHERE id = %s AND state = 'leased'
                      AND lease_owner = %s AND lease_token = %s
                      AND lease_expires_at > clock_timestamp()
                    """,
                    (
                        state,
                        *dependency_params,
                        outcome,
                        category,
                        safe_detail,
                        should_retry,
                        claim.job_id,
                        claim.lease_owner,
                        claim.lease_token,
                    ),
                )
                if completed.rowcount != 1:
                    raise LeaseLost("job lease was lost while recording failure")
                if claim.observation_id is not None:
                    failure_outcome = self._failure_outcome(category)
                    if not should_retry:
                        self._record_processing_outcome(
                            connection,
                            claim,
                            outcome=failure_outcome,
                            failure_category=category,
                        )
                    self._refresh_reset_baseline_evidence(
                        connection,
                        claim,
                        failure_category=category,
                        failure_retryable=should_retry,
                    )
                    if not should_retry and claim.endpoint == "global_player_rankings":
                        self._record_official_failed_attempt(
                            connection,
                            claim,
                            outcome=failure_outcome,
                            category=category,
                        )
                return state

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

    def get_player(self, normalized_tag: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    p.normalized_tag, p.active, p.eligibility_state,
                    v.name, v.trophies, v.league_tier_id, v.league_tier_name,
                    v.observed_at, v.endpoint_version, v.schema_version,
                    v.parser_version, v.source_http_status
                FROM players AS p
                JOIN player_profile_versions AS v ON v.id = p.current_profile_version_id
                WHERE p.normalized_tag = %s
                """,
                (normalized_tag,),
            ).fetchone()
            if row is None:
                return None
            return {
                "normalized_tag": _text_value(row[0]),
                "active": row[1],
                "eligibility_state": _text_value(row[2]),
                "name": _text_value(row[3]),
                "trophies": row[4],
                "league_tier_id": row[5],
                "league_tier_name": _text_value(row[6]),
                "observed_at": row[7],
                "endpoint_version": _text_value(row[8]),
                "schema_version": _text_value(row[9]),
                "parser_version": _text_value(row[10]),
                "source_http_status": row[11],
            }

    def _refresh_reset_baseline_evidence(
        self,
        connection: Any,
        claim: Claim,
        *,
        failure_category: str | None = None,
        failure_retryable: bool = False,
    ) -> None:
        if claim.observation_id is None:
            return
        context = self._load_reset_baseline_context(connection, claim.observation_id)
        if context is None:
            return

        (
            root_job_id,
            root_work_type,
            root_player_id,
            root_tag,
            reset_sweep_id,
            baseline_id,
            expected_attempt_id,
            boundary_at,
            evidence_kind,
        ) = context
        root_work_type = _text_value(root_work_type)
        evidence_kind = _text_value(evidence_kind)
        endpoint_details: dict[str, dict[str, Any]] = {}
        for endpoint in ("profile", "battle_log"):
            endpoint_details[endpoint] = self._load_reset_endpoint_evidence(
                connection,
                endpoint=endpoint,
                root_job_id=int(root_job_id),
                root_player_id=int(root_player_id),
                root_tag=_text_value(root_tag),
                expected_attempt_id=(
                    int(expected_attempt_id)
                    if expected_attempt_id is not None
                    else None
                ),
                boundary_at=boundary_at,
                parser_version=claim.parser_version,
                processing_version=claim.processing_version,
                claim=claim,
                failure_category=failure_category,
                failure_retryable=failure_retryable,
            )

        reasons: list[str] = []
        for endpoint in ("profile", "battle_log"):
            reasons.extend(endpoint_details[endpoint]["reasons"])
        if root_work_type != "reset_baseline" or evidence_kind != "paired_v2":
            reasons.append("legacy_profile_only")
        reasons = list(dict.fromkeys(reasons))

        profile_valid = bool(endpoint_details["profile"]["valid"])
        battle_log_valid = bool(endpoint_details["battle_log"]["valid"])
        hard_failure = bool(
            root_work_type != "reset_baseline"
            or evidence_kind != "paired_v2"
            or any(
                endpoint_details[endpoint]["hard_failure"]
                for endpoint in endpoint_details
            )
        )
        if profile_valid and battle_log_valid and not hard_failure:
            state = "complete"
            reasons = []
        elif hard_failure:
            state = "failed"
        else:
            state = "partial"

        profile = endpoint_details["profile"]
        battle_log = endpoint_details["battle_log"]
        evidence_json = {
            "reset_baseline_sweep_id": int(baseline_id),
            "reset_sweep_id": int(reset_sweep_id),
            "collection_job_id": int(root_job_id),
            "attempt_id": (
                int(expected_attempt_id) if expected_attempt_id is not None else None
            ),
            "profile": {
                "observation_id": profile["observation_id"],
                "processing_outcome_id": profile["processing_outcome_id"],
                "collector_outcome": profile["collector_outcome"],
                "processing_outcome": profile["processing_outcome"],
            },
            "battle_log": {
                "observation_id": battle_log["observation_id"],
                "processing_outcome_id": battle_log["processing_outcome_id"],
                "collector_outcome": battle_log["collector_outcome"],
                "processing_outcome": battle_log["processing_outcome"],
            },
            "failure_reasons": reasons,
        }
        fingerprint_data = {
            "baseline_id": int(baseline_id),
            "root_job_id": int(root_job_id),
            "attempt_id": (
                int(expected_attempt_id) if expected_attempt_id is not None else None
            ),
            "profile_observation_id": profile["observation_id"],
            "battle_log_observation_id": battle_log["observation_id"],
            "profile_processing_outcome_id": profile["processing_outcome_id"],
            "battle_log_processing_outcome_id": battle_log["processing_outcome_id"],
            "profile_valid": profile_valid,
            "battle_log_valid": battle_log_valid,
            "state": state,
            "failure_reasons": reasons,
            "parser_version": claim.parser_version,
            "processing_version": claim.processing_version,
        }
        evidence_key = hashlib.sha256(
            json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        if getattr(self, "_supports_coordinator_contract", False):
            current_member = connection.execute(
                """
                SELECT 1
                FROM collector_reset_sweep_members
                WHERE sweep_id = %s AND player_id = %s
                """,
                (reset_sweep_id, root_player_id),
            ).fetchone()
            if current_member is None:
                return
        locked = connection.execute(
            "SELECT clashlens_lock_reset_baseline_v2(%s)",
            (baseline_id,),
        ).fetchone()
        if locked is None or not locked[0]:
            raise RuntimeError("reset baseline sweep is unavailable")
        existing = connection.execute(
            """
            SELECT id, version
            FROM reset_baseline_evidence
            WHERE reset_baseline_sweep_id = %s AND evidence_key = %s
            """,
            (baseline_id, evidence_key),
        ).fetchone()
        if existing is not None:
            baseline_evidence_id = int(existing[0])
            baseline_version = int(existing[1])
        else:
            prior = connection.execute(
                """
                SELECT id, version
                FROM reset_baseline_evidence
                WHERE reset_baseline_sweep_id = %s
                ORDER BY version DESC, id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (baseline_id,),
            ).fetchone()
            baseline_version = int(prior[1]) + 1 if prior is not None else 1
            inserted = connection.execute(
                """
                INSERT INTO reset_baseline_evidence (
                    sweep_id, player_id, boundary_at,
                    profile_observation_id, battle_log_observation_id,
                    profile_valid, battle_log_valid, legacy_profile_only,
                    reset_baseline_sweep_id, collection_job_id, attempt_id,
                    profile_processing_outcome_id, battle_log_processing_outcome_id,
                    parser_version, processing_version, version, supersedes_id,
                    state, failure_reasons, evidence_json, evidence_key
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    int(reset_sweep_id),
                    int(root_player_id),
                    boundary_at,
                    profile["observation_id"],
                    battle_log["observation_id"],
                    profile_valid,
                    battle_log_valid,
                    root_work_type != "reset_baseline" or evidence_kind != "paired_v2",
                    int(baseline_id),
                    int(root_job_id),
                    expected_attempt_id,
                    profile["processing_outcome_id"],
                    battle_log["processing_outcome_id"],
                    claim.parser_version,
                    claim.processing_version,
                    baseline_version,
                    prior[0] if prior is not None else None,
                    state,
                    Jsonb(reasons),
                    Jsonb(evidence_json),
                    evidence_key,
                ),
            ).fetchone()
            assert inserted is not None
            baseline_evidence_id = int(inserted[0])

        if (
            state in {"complete", "failed"}
            and root_work_type == "reset_baseline"
            and evidence_kind == "paired_v2"
        ):
            self._record_boundary_baseline(
                connection,
                boundary_at=boundary_at,
                reset_sweep_id=int(reset_sweep_id),
                player_id=int(root_player_id),
                state=state,
            )
        if state == "complete":
            self._enqueue_reset_reconciliation(
                connection,
                baseline_id=int(baseline_evidence_id),
                baseline_version=baseline_version,
                player_id=int(root_player_id),
                boundary_at=boundary_at,
            )

    def _record_boundary_baseline(
        self,
        connection: Any,
        *,
        boundary_at: datetime,
        reset_sweep_id: int,
        player_id: int,
        state: str,
    ) -> None:
        """Record one terminal reset result and reevaluate both artifacts."""
        boundary_at = boundary_at.astimezone(UTC)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"boundary-publication:{boundary_at.isoformat()}",),
        )
        sweep = connection.execute(
            """
            SELECT id
            FROM collector_reset_sweeps
            WHERE id = %s AND boundary_at = %s
            """,
            (reset_sweep_id, boundary_at),
        ).fetchone()
        if sweep is None:
            return
        member = connection.execute(
            """
            SELECT 1
            FROM collector_reset_sweep_members
            WHERE sweep_id = %s AND player_id = %s
            """,
            (reset_sweep_id, player_id),
        ).fetchone()
        if member is None:
            return
        generation = connection.execute(
            """
            SELECT id, snapshot_state, army_state
            FROM boundary_publication_generations
            WHERE boundary_at = %s AND generation = 1
            FOR UPDATE
            """,
            (boundary_at,),
        ).fetchone()
        if generation is None:
            population_rows = connection.execute(
                """
                SELECT player_id
                FROM collector_reset_sweep_members
                WHERE sweep_id = %s
                ORDER BY player_id
                """,
                (reset_sweep_id,),
            ).fetchall()
            generation_id, _generation = self._create_boundary_generation(
                connection,
                boundary_at=boundary_at,
                sweep_id=reset_sweep_id,
                player_ids=[int(row[0]) for row in population_rows],
                generation=1,
                supersedes_id=None,
            )
            generation = (generation_id, "pending", "pending")
        if (
            _text_value(generation[1]) == "superseded"
            and _text_value(generation[2]) == "superseded"
        ):
            return

        if state == "failed":
            snapshot_status = army_status = "unavailable"
        else:
            ranked_state = connection.execute(
                """
                SELECT ranked.id
                FROM boundary_publication_generation_members AS member
                JOIN ranked_day_versions AS ranked
                  ON ranked.id = member.ranked_day_version_id
                WHERE member.generation_id = %s AND member.player_id = %s
                """,
                (generation[0], player_id),
            ).fetchone()
            ranked_version_id = int(ranked_state[0]) if ranked_state else None
            snapshot_status = (
                self._boundary_snapshot_status(
                    connection,
                    player_id=player_id,
                    ranked_day_version_id=ranked_version_id,
                    boundary_at=boundary_at,
                )
                if ranked_version_id is not None
                else "pending"
            )
            army_status = (
                self._boundary_army_status(
                    connection,
                    player_id=player_id,
                    ranked_day_version_id=ranked_version_id,
                    snapshot_status=snapshot_status,
                )
                if ranked_version_id is not None
                else "pending"
            )
        member_status = (
            "unavailable"
            if snapshot_status == "unavailable"
            else ("terminal" if snapshot_status != "pending" else "pending")
        )
        connection.execute(
            """
            UPDATE boundary_publication_generation_members
            SET status = %s, snapshot_status = %s, army_status = %s,
                updated_at = clock_timestamp()
            WHERE generation_id = %s AND player_id = %s
            """,
            (member_status, snapshot_status, army_status, generation[0], player_id),
        )
        self._try_enqueue_boundary_artifacts(
            connection, boundary_at=boundary_at, generation_id=int(generation[0])
        )

    @staticmethod
    def _load_reset_baseline_context(
        connection: Any,
        observation_id: int,
    ) -> tuple[Any, ...] | None:
        row = connection.execute(
            """
            SELECT root.id, root.work_type, root.player_id, root.normalized_tag,
                   root.sweep_id, root.reset_baseline_sweep_id,
                   COALESCE(root.result_attempt_id, observed.attempt_id),
                   baseline.boundary_at, baseline.evidence_kind
            FROM collector_observations AS observed
            LEFT JOIN collector_attempts AS source_attempt
              ON source_attempt.id = observed.attempt_id
            LEFT JOIN collector_jobs AS attempt_job
              ON attempt_job.id = source_attempt.job_id
            LEFT JOIN collector_jobs AS source_job
              ON source_job.id = observed.collection_job_id
            JOIN collector_jobs AS root
              ON root.id = CASE
                    WHEN attempt_job.work_type IN ('reset_baseline', 'legacy_reset_profile')
                        THEN attempt_job.id
                    WHEN source_job.work_type IN ('reset_baseline', 'legacy_reset_profile')
                        THEN source_job.id
                    ELSE NULL
                 END
            JOIN collector_reset_baseline_sweeps AS baseline
              ON baseline.id = root.reset_baseline_sweep_id
            WHERE observed.id = %s
            """,
            (observation_id,),
        ).fetchone()
        return None if row is None else tuple(row)

    @staticmethod
    def _load_reset_endpoint_evidence(
        connection: Any,
        *,
        endpoint: str,
        root_job_id: int,
        root_player_id: int,
        root_tag: str,
        expected_attempt_id: int | None,
        boundary_at: datetime,
        parser_version: str,
        processing_version: str,
        claim: Claim,
        failure_category: str | None,
        failure_retryable: bool,
    ) -> dict[str, Any]:
        row = None
        if expected_attempt_id is not None:
            row = connection.execute(
                """
                SELECT result.outcome, result.observation_id,
                       observed.attempt_id, observed.collection_job_id,
                       observed.player_id, observed.normalized_tag,
                       observed.response_completed_at, observed.http_status,
                       processing.id, processing.outcome, processing.failure_category,
                       profile.id, profile.source_contract_state,
                       profile.eligibility_state, battle_log.id, battle_log.has_row_gap
                FROM collector_endpoint_results AS result
                LEFT JOIN collector_observations AS observed
                  ON observed.id = result.observation_id
                LEFT JOIN observation_processing_outcomes AS processing
                  ON processing.observation_id = observed.id
                 AND processing.parser_version = %s
                 AND processing.processing_version = %s
                LEFT JOIN player_profile_versions AS profile
                  ON profile.observation_id = observed.id
                 AND profile.parser_version = %s
                LEFT JOIN battle_log_observations AS battle_log
                  ON battle_log.observation_id = observed.id
                 AND battle_log.parser_version = %s
                WHERE result.attempt_id = %s AND result.endpoint = %s
                """,
                (
                    parser_version,
                    processing_version,
                    parser_version,
                    parser_version,
                    expected_attempt_id,
                    endpoint,
                ),
            ).fetchone()

        observation_id = int(row[1]) if row is not None and row[1] is not None else None
        processing_id = int(row[8]) if row is not None and row[8] is not None else None
        collector_outcome = _text_value(row[0]) if row is not None else None
        processing_outcome = (
            _text_value(row[9]) if row is not None and row[9] is not None else None
        )
        reasons: list[str] = []
        hard_failure = False
        missing = False

        if row is None or observation_id is None:
            observation_id = (
                int(claim.observation_id)
                if claim.endpoint == endpoint and claim.observation_id is not None
                else None
            )
            if claim.endpoint == endpoint and claim.observation_id == observation_id:
                processing = connection.execute(
                    """
                    SELECT id, outcome, failure_category
                    FROM observation_processing_outcomes
                    WHERE observation_id = %s
                      AND parser_version = %s
                      AND processing_version = %s
                    """,
                    (observation_id, parser_version, processing_version),
                ).fetchone()
                if processing is not None:
                    processing_id = int(processing[0])
                    processing_outcome = _text_value(processing[1])
            missing = True
            reasons.append(f"missing_{endpoint}_observation")
            if collector_outcome in {"failed", "storage_failed"}:
                hard_failure = True
            elif collector_outcome is not None:
                reasons.append(f"collector_{endpoint}_{collector_outcome}")
        else:
            observed_attempt_id = int(row[2]) if row[2] is not None else None
            observed_player_id = int(row[4]) if row[4] is not None else None
            observed_tag = _text_value(row[5]) if row[5] is not None else None
            if observed_attempt_id != expected_attempt_id:
                reasons.append(f"{endpoint}_wrong_attempt")
                hard_failure = True
            if observed_player_id != root_player_id or observed_tag != root_tag:
                reasons.append(f"{endpoint}_wrong_player")
                hard_failure = True
            in_lineage = connection.execute(
                "SELECT clashlens_reset_job_lineage_v2(%s, %s)",
                (row[3], root_job_id),
            ).fetchone()[0]
            if not in_lineage:
                reasons.append(f"{endpoint}_outside_sweep")
                hard_failure = True
            if row[6] is None or row[6] < boundary_at:
                reasons.append(f"{endpoint}_stale")
                hard_failure = True

            if processing_outcome is None:
                missing = True
                if (
                    claim.endpoint == endpoint
                    and claim.observation_id == observation_id
                    and failure_category is not None
                ):
                    suffix = f"_{failure_category}"
                    reasons.append(
                        f"{endpoint}{suffix}{'_retrying' if failure_retryable else ''}"
                    )
                    hard_failure = not failure_retryable
                else:
                    reasons.append(f"unprocessed_{endpoint}")
            elif processing_outcome == "non_success":
                reasons.append(f"{endpoint}_non_success")
                hard_failure = True
            elif processing_outcome != "processed":
                category = (
                    _text_value(row[10]) if row[10] is not None else processing_outcome
                )
                reasons.append(f"{endpoint}_{category}")
                hard_failure = True
            elif endpoint == "profile":
                if (
                    row[11] is None
                    or _text_value(row[12]) != "accepted"
                    or _text_value(row[13]) != "eligible"
                ):
                    reasons.append("profile_invalid")
                    hard_failure = True
                else:
                    first_event = connection.execute(
                        """
                        SELECT min(evidence.battle_timestamp)
                        FROM battle_evidence AS evidence
                        JOIN legend_battles AS battle
                          ON battle.id = evidence.battle_id
                        WHERE (
                                battle.attacker_player_id = %s
                                OR battle.defender_player_id = %s
                            )
                          AND evidence.battle_timestamp >= %s
                          AND evidence.battle_timestamp < %s + interval '1 day'
                        """,
                        (root_player_id, root_player_id, boundary_at, boundary_at),
                    ).fetchone()[0]
                    if first_event is not None and row[6] >= first_event:
                        reasons.append("profile_after_first_event")
                        hard_failure = True
            elif row[14] is None or bool(row[15]):
                reasons.append("battle_log_malformed")
                hard_failure = True

        valid = not reasons and not missing and not hard_failure
        return {
            "observation_id": observation_id,
            "processing_outcome_id": processing_id,
            "collector_outcome": collector_outcome,
            "processing_outcome": processing_outcome,
            "reasons": reasons,
            "hard_failure": hard_failure,
            "valid": valid,
        }

    @staticmethod
    def _enqueue_reset_reconciliation(
        connection: Any,
        *,
        baseline_id: int,
        baseline_version: int,
        player_id: int,
        boundary_at: datetime,
    ) -> None:
        boundary_text = boundary_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ranked_day_start = boundary_at - timedelta(days=1)
        ranked_day_start_text = ranked_day_start.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        deduplication_key = (
            f"reconcile:reset-baseline:{baseline_id}:v{baseline_version}"
        )
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (
                NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
                %s, %s, %s, %s
            )
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                deduplication_key,
                Jsonb(
                    {
                        "player_id": int(player_id),
                        "ranked_day_start": ranked_day_start_text,
                        "boundary_at": boundary_text,
                        "reset_baseline_id": int(baseline_id),
                        "reset_baseline_version": int(baseline_version),
                    }
                ),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        )

    @staticmethod
    def _enqueue_live_reconciliation(
        connection: Any,
        *,
        player_id: int,
        ranked_day_start: datetime,
        source_quality: dict[str, Any] | None,
    ) -> None:
        ranked_day = ranked_day_for(ranked_day_start)
        ranked_day_start = ranked_day.start.astimezone(UTC)
        live = connection.execute(
            "SELECT clock_timestamp() >= %s AND clock_timestamp() < %s",
            (ranked_day.start, ranked_day.end),
        ).fetchone()
        assert live is not None
        if not bool(live[0]):
            return
        ranked_day_start_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = connection.execute(
            """
            SELECT
                battle.id,
                p.perspective,
                evidence.battle_timestamp,
                evidence.stars,
                evidence.destruction_percentage,
                evidence.army_share_code,
                evidence.reporter_trophies,
                evidence.opponent_trophies,
                evidence.attacker_gain,
                evidence.defender_loss,
                evidence.trophy_rule_version,
                battle.disagreement_state,
                source_row.outcome,
                source_row.failure_category,
                jsonb_build_object(
                    'tag', CASE evidence.parser_version
                        WHEN 'supercell-source-parser-v2'
                            THEN source_row.source_json -> 'opponentPlayerTag'
                        ELSE source_row.source_json -> 'opponent' -> 'tag'
                    END,
                    'name', CASE evidence.parser_version
                        WHEN 'supercell-source-parser-v2'
                            THEN source_row.source_json -> 'opponentName'
                        ELSE source_row.source_json -> 'opponent' -> 'name'
                    END
                )
            FROM legend_battles AS battle
            JOIN battle_perspectives AS p ON p.battle_id = battle.id
            JOIN battle_evidence AS evidence ON evidence.id = p.evidence_id
            JOIN battle_source_rows AS source_row
              ON source_row.id = evidence.source_row_id
            WHERE battle.ranked_day_start = %s
              AND evidence.battle_timestamp >= %s
              AND evidence.battle_timestamp < %s
              AND (
                  (p.perspective = 'attacker'
                   AND battle.attacker_player_id = %s)
                  OR
                  (p.perspective = 'defender'
                   AND battle.defender_player_id = %s)
              )
            ORDER BY battle.id, p.perspective
            """,
            (
                ranked_day.start,
                ranked_day.start,
                ranked_day.end,
                player_id,
                player_id,
            ),
        ).fetchall()
        projection_rows = [
            {
                "battle_id": int(row[0]),
                "perspective": _text_value(row[1]),
                "battle_timestamp": row[2].astimezone(UTC).isoformat(),
                "stars": int(row[3]),
                "destruction_percentage": int(row[4]),
                "army_share_code": _text_value(row[5]),
                "reporter_trophies": (None if row[6] is None else int(row[6])),
                "opponent_trophies": (None if row[7] is None else int(row[7])),
                "attacker_gain": int(row[8]),
                "defender_loss": int(row[9]),
                "trophy_rule_version": _text_value(row[10]),
                "disagreement_state": _text_value(row[11]),
                "source_outcome": _text_value(row[12]),
                "failure_category": (None if row[13] is None else _text_value(row[13])),
                "opponent": row[14],
            }
            for row in rows
        ]
        projection = {
            "events": projection_rows,
            "source_quality": source_quality,
        }
        projection_hash = hashlib.sha256(
            json.dumps(
                projection,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        deduplication_key = (
            f"reconcile:live:{player_id}:{ranked_day_start_text}:"
            f"{RECONCILIATION_RULE_VERSION}:{projection_hash}"
        )
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (
                NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
                %s, %s, %s, %s
            )
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                deduplication_key,
                Jsonb(
                    {
                        "player_id": int(player_id),
                        "ranked_day_start": ranked_day_start_text,
                        "trigger": "live_battle_projection",
                        "projection_hash": projection_hash,
                    }
                ),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        )

    @staticmethod
    def _load_reset_baseline(
        connection: Any,
        player_id: int,
        boundary_at: datetime,
        parser_version: str,
        processing_version: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT
                evidence.id,
                evidence.version,
                evidence.state,
                evidence.sweep_id,
                evidence.reset_baseline_sweep_id,
                evidence.collection_job_id,
                evidence.attempt_id,
                evidence.profile_observation_id,
                evidence.battle_log_observation_id,
                evidence.profile_processing_outcome_id,
                evidence.battle_log_processing_outcome_id,
                evidence.profile_valid,
                evidence.battle_log_valid,
                evidence.legacy_profile_only,
                evidence.failure_reasons,
                evidence.evidence_json,
                evidence.evidence_key,
                evidence.boundary_at,
                baseline.evidence_kind,
                profile.id,
                profile.trophies,
                profile.eligibility_state,
                profile.source_contract_state,
                profile.observed_at,
                battle_log.id,
                battle_log.row_count,
                battle_log.has_row_gap,
                profile_observation.response_hash,
                battle_observation.response_hash
            FROM reset_baseline_evidence AS evidence
            LEFT JOIN collector_reset_baseline_sweeps AS baseline
              ON baseline.id = evidence.reset_baseline_sweep_id
            LEFT JOIN player_profile_versions AS profile
              ON profile.observation_id = evidence.profile_observation_id
             AND profile.parser_version = evidence.parser_version
            LEFT JOIN collector_observations AS profile_observation
              ON profile_observation.id = evidence.profile_observation_id
             AND profile_observation.endpoint = 'profile'
            LEFT JOIN battle_log_observations AS battle_log
              ON battle_log.observation_id = evidence.battle_log_observation_id
             AND battle_log.parser_version = evidence.parser_version
            LEFT JOIN collector_observations AS battle_observation
              ON battle_observation.id = evidence.battle_log_observation_id
             AND battle_observation.endpoint = 'battle_log'
            WHERE evidence.player_id = %s
              AND evidence.boundary_at = %s
              AND evidence.parser_version = %s
              AND evidence.processing_version = %s
            ORDER BY evidence.version DESC, evidence.id DESC
            LIMIT 1
            """,
            (player_id, boundary_at, parser_version, processing_version),
        ).fetchone()
        if row is None:
            return None

        state = _text_value(row[2])
        profile_valid = bool(row[11])
        battle_log_valid = bool(row[12])
        legacy_profile_only = bool(row[13])
        profile_accepted = (
            row[19] is not None
            and _text_value(row[22]) == "accepted"
            and _text_value(row[22]) is not None
        )
        profile_eligible = _text_value(row[21]) == "eligible"
        battle_log_valid_evidence = row[24] is not None and not bool(row[26])
        complete = bool(
            state == "complete"
            and profile_valid
            and battle_log_valid
            and not legacy_profile_only
            and profile_accepted
            and profile_eligible
            and battle_log_valid_evidence
            and row[7] is not None
            and row[8] is not None
            and row[9] is not None
            and row[10] is not None
        )
        failure_reasons = row[14] if isinstance(row[14], list) else []
        evidence_json = row[15] if isinstance(row[15], dict) else {}
        evidence = {
            "id": int(row[0]),
            "version": int(row[1]),
            "state": state,
            "sweep_id": int(row[3]) if row[3] is not None else None,
            "reset_baseline_sweep_id": (int(row[4]) if row[4] is not None else None),
            "collection_job_id": (int(row[5]) if row[5] is not None else None),
            "attempt_id": int(row[6]) if row[6] is not None else None,
            "profile_observation_id": (int(row[7]) if row[7] is not None else None),
            "battle_log_observation_id": (int(row[8]) if row[8] is not None else None),
            "profile_processing_outcome_id": (
                int(row[9]) if row[9] is not None else None
            ),
            "battle_log_processing_outcome_id": (
                int(row[10]) if row[10] is not None else None
            ),
            "profile_valid": profile_valid,
            "battle_log_valid": battle_log_valid,
            "legacy_profile_only": legacy_profile_only,
            "failure_reasons": list(failure_reasons),
            "evidence_key": _text_value(row[16]),
            "boundary_at": row[17].astimezone(UTC).isoformat(),
            "evidence_kind": (_text_value(row[18]) if row[18] is not None else None),
            "profile": {
                "id": int(row[19]) if row[19] is not None else None,
                "trophies": int(row[20]) if row[20] is not None else None,
                "eligibility_state": (
                    _text_value(row[21]) if row[21] is not None else None
                ),
                "source_contract_state": (
                    _text_value(row[22]) if row[22] is not None else None
                ),
                "observed_at": (
                    row[23].astimezone(UTC).isoformat() if row[23] is not None else None
                ),
                "response_hash": _text_value(row[27]),
            },
            "battle_log": {
                "id": int(row[24]) if row[24] is not None else None,
                "row_count": int(row[25]) if row[25] is not None else None,
                "has_row_gap": bool(row[26]) if row[26] is not None else None,
                "response_hash": _text_value(row[28]),
            },
            "stored_evidence": evidence_json,
        }
        return {
            "id": int(row[0]),
            "version": int(row[1]),
            "state": state,
            "complete": complete,
            "trophies": int(row[20]) if row[20] is not None else None,
            "eligibility_state": (
                _text_value(row[21]) if row[21] is not None else None
            ),
            "evidence": evidence,
        }

    def _publish_snapshot_kind(
        self,
        connection: Any,
        *,
        snapshot_kind: str,
        boundary_at: datetime,
        ranked_day_version_id: int | None,
        entries: list[dict[str, Any]],
        coverage: float,
        quality: dict[str, int],
        input_hash: str,
        publish: bool,
    ) -> tuple[int, int]:
        """Assemble one immutable snapshot version.

        Frozen snapshots stop at ``building``. The analytics transaction is the
        only writer that changes a frozen snapshot to ``published``. Live
        snapshots keep their independent publication path.
        """
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                "leaderboard-snapshot-v2:"
                + snapshot_kind
                + ":"
                + boundary_at.isoformat(),
            ),
        )
        existing = connection.execute(
            """
            SELECT id, version, state
            FROM leaderboard_snapshots
            WHERE snapshot_kind = %s
              AND boundary_at = %s
              AND input_hash = %s
            ORDER BY id DESC
            LIMIT 1
            FOR UPDATE
            """,
            (snapshot_kind, boundary_at, input_hash),
        ).fetchone()
        if existing is not None and _text_value(existing[2]) != "building":
            return int(existing[0]), int(existing[1])

        prior = connection.execute(
            """
            SELECT id, version
            FROM leaderboard_snapshots
            WHERE snapshot_kind = %s
              AND boundary_at = %s
              AND state = 'published'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (snapshot_kind, boundary_at),
        ).fetchone()
        if existing is not None:
            snapshot_id = int(existing[0])
            snapshot_version = int(existing[1])
        else:
            next_version_row = connection.execute(
                """
                SELECT COALESCE(max(version), 0) + 1
                FROM leaderboard_snapshots
                WHERE snapshot_kind = %s AND boundary_at = %s
                """,
                (snapshot_kind, boundary_at),
            ).fetchone()
            assert next_version_row is not None
            snapshot_version = int(next_version_row[0])
            if getattr(self, "_supports_coordinator_contract", False):
                snapshot = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version, correction_of_id,
                        ordering_rule_version, freshness_rule_version, state,
                        source_ranked_day_version_id, measured_coverage,
                        stale_entry_count, input_hash,
                        eligible_population_count, included_entry_count,
                        fresh_entry_count, excluded_missing_count,
                        excluded_unavailable_count, excluded_invalid_count,
                        excluded_malformed_count, excluded_partial_count,
                        excluded_inconsistent_count, excluded_conflicting_count
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, 'building', %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        snapshot_kind,
                        boundary_at,
                        snapshot_version,
                        prior[0] if prior is not None else None,
                        SNAPSHOT_ORDERING_RULE_VERSION,
                        FRESHNESS_RULE_VERSION,
                        ranked_day_version_id,
                        coverage,
                        quality["stale_entry_count"],
                        input_hash,
                        quality["eligible_population_count"],
                        quality["included_entry_count"],
                        quality["fresh_entry_count"],
                        quality["excluded_missing_count"],
                        quality["excluded_unavailable_count"],
                        quality["excluded_invalid_count"],
                        quality["excluded_malformed_count"],
                        quality["excluded_partial_count"],
                        quality["excluded_inconsistent_count"],
                        quality["excluded_conflicting_count"],
                    ),
                ).fetchone()
            else:
                snapshot = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version, correction_of_id,
                        ordering_rule_version, freshness_rule_version, state,
                        source_ranked_day_version_id, measured_coverage,
                        stale_entry_count, input_hash,
                        eligible_population_count, included_entry_count,
                        fresh_entry_count, excluded_missing_count,
                        excluded_invalid_count, excluded_malformed_count,
                        excluded_conflicting_count
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, 'building', %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        snapshot_kind,
                        boundary_at,
                        snapshot_version,
                        prior[0] if prior is not None else None,
                        SNAPSHOT_ORDERING_RULE_VERSION,
                        FRESHNESS_RULE_VERSION,
                        ranked_day_version_id,
                        coverage,
                        quality["stale_entry_count"],
                        input_hash,
                        quality["eligible_population_count"],
                        quality["included_entry_count"],
                        quality["fresh_entry_count"],
                        quality["excluded_missing_count"],
                        quality["excluded_invalid_count"],
                        quality["excluded_malformed_count"],
                        quality["excluded_conflicting_count"],
                    ),
                ).fetchone()
            assert snapshot is not None
            snapshot_id = int(snapshot[0])
        entry_rows = [
            {
                "position": position,
                "player_id": int(entry["player_id"]),
                "profile_version_id": entry.get("profile_version_id"),
                "trophies": int(entry["trophies"]),
                "observation_id": int(entry["observation_id"]),
                "observed_at": entry["observed_at"].astimezone(UTC).isoformat(),
                "age_seconds": int(entry["age_seconds"]),
                "freshness": entry["freshness"],
                "confidence": entry["confidence"],
                "tie_hash": entry["tie_hash"],
                "official_rank": (
                    entry["official"][0] if entry["official"] is not None else None
                ),
                "official_rank_version_id": (
                    entry["official"][1] if entry["official"] is not None else None
                ),
                "official_rank_observed_at": (
                    entry["official"][2].astimezone(UTC).isoformat()
                    if entry["official"] is not None
                    else None
                ),
            }
            for position, entry in enumerate(entries, start=1)
        ]
        if getattr(self, "_supports_coordinator_contract", False):
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, profile_version_id, trophies,
                    trophy_observation_id, trophy_observed_at,
                    observation_age_seconds, freshness, confidence, tie_hash,
                    profile_observation_id, profile_observed_at,
                    profile_age_seconds, profile_freshness, profile_confidence,
                    official_rank, official_rank_version_id,
                    official_rank_observed_at
                )
                SELECT %s, row.position, row.player_id, row.profile_version_id,
                       row.trophies, row.observation_id, row.observed_at,
                       row.age_seconds, row.freshness, row.confidence, row.tie_hash,
                       row.observation_id, row.observed_at, row.age_seconds,
                       row.freshness, row.confidence, row.official_rank,
                       row.official_rank_version_id, row.official_rank_observed_at
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    position integer, player_id bigint, profile_version_id bigint,
                    trophies integer, observation_id bigint, observed_at timestamptz,
                    age_seconds integer, freshness text, confidence text,
                    tie_hash text, official_rank integer,
                    official_rank_version_id bigint,
                    official_rank_observed_at timestamptz
                )
                ON CONFLICT (snapshot_id, position) DO NOTHING
                """,
                (snapshot_id, Jsonb(entry_rows)),
            )
        else:
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, trophies,
                    trophy_observation_id, trophy_observed_at,
                    observation_age_seconds, freshness, confidence, tie_hash,
                    profile_observation_id, profile_observed_at,
                    profile_age_seconds, profile_freshness, profile_confidence,
                    official_rank, official_rank_version_id,
                    official_rank_observed_at
                )
                SELECT %s, row.position, row.player_id, row.trophies,
                       row.observation_id, row.observed_at, row.age_seconds,
                       row.freshness, row.confidence, row.tie_hash,
                       row.observation_id, row.observed_at, row.age_seconds,
                       row.freshness, row.confidence, row.official_rank,
                       row.official_rank_version_id, row.official_rank_observed_at
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    position integer, player_id bigint, trophies integer,
                    observation_id bigint, observed_at timestamptz,
                    age_seconds integer, freshness text, confidence text,
                    tie_hash text, official_rank integer,
                    official_rank_version_id bigint,
                    official_rank_observed_at timestamptz
                )
                ON CONFLICT (snapshot_id, position) DO NOTHING
                """,
                (snapshot_id, Jsonb(entry_rows)),
            )
        if publish:
            connection.execute(
                """
                UPDATE leaderboard_snapshots
                SET state = 'published', published_at = clock_timestamp()
                WHERE id = %s AND state = 'building'
                """,
                (snapshot_id,),
            )
            if prior is not None and int(prior[0]) != snapshot_id:
                connection.execute(
                    """
                    UPDATE leaderboard_snapshots
                    SET state = 'superseded'
                    WHERE id = %s AND state = 'published'
                    """,
                    (prior[0],),
                )
        return snapshot_id, snapshot_version

    @staticmethod
    def _enqueue_snapshot_analytics(
        connection: Any,
        *,
        snapshot_id: int,
        snapshot_version: int,
        snapshot_input_hash: str,
        ranked_day_version_id: int | None,
        period_start: datetime,
        period_end: datetime,
        generation_number: int | None = None,
        manifest_id: int | None = None,
        manifest_digest: str | None = None,
        boundary_at: datetime | None = None,
    ) -> None:
        deduplication_key = (
            f"build_analytics:snapshot:{int(snapshot_id)}:v{int(snapshot_version)}:"
            f"input:{snapshot_input_hash}"
        )
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (NULL, 'build_analytics', %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                deduplication_key,
                Jsonb(
                    {
                        "snapshot_id": int(snapshot_id),
                        "snapshot_version": int(snapshot_version),
                        "snapshot_input_hash": snapshot_input_hash,
                        **(
                            {"source_ranked_day_version_id": int(ranked_day_version_id)}
                            if ranked_day_version_id is not None
                            else {}
                        ),
                        **(
                            {
                                "generation": int(generation_number),
                                "manifest_id": int(manifest_id),
                                "manifest_digest": manifest_digest,
                                "boundary_at": boundary_at.astimezone(UTC).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                ),
                            }
                            if generation_number is not None
                            and manifest_id is not None
                            and manifest_digest is not None
                            and boundary_at is not None
                            else {}
                        ),
                        "period_start": period_start.astimezone(UTC).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "period_end": period_end.astimezone(UTC).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                        "population_filter": {"population": "tracked_players"},
                    }
                ),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        )

    @staticmethod
    def _store_ranked_day_adjustments(
        connection: Any,
        ranked_day_version_id: int,
        result: ReconciliationResult,
    ) -> None:
        if result.automatic_defense_loss is not None:
            connection.execute(
                """
                INSERT INTO ranked_day_adjustments (
                    ranked_day_version_id, adjustment_type, amount,
                    evidence_state, rule_version, evidence_json
                ) VALUES (%s, 'automatic_defense', %s, %s, %s, %s)
                """,
                (
                    ranked_day_version_id,
                    -result.automatic_defense_loss,
                    result.automatic_defense_evidence_state,
                    RECONCILIATION_RULE_VERSION,
                    Jsonb(
                        {
                            "defense_count": result.defense_count,
                            "observed_defense_loss": result.observed_defense_loss,
                        }
                    ),
                ),
            )
        if result.boundary_adjustment_type is not None:
            connection.execute(
                """
                INSERT INTO ranked_day_adjustments (
                    ranked_day_version_id, adjustment_type, amount,
                    evidence_state, rule_version, evidence_json
                ) VALUES (%s, %s, %s, 'official_rule', %s, '{}'::jsonb)
                """,
                (
                    ranked_day_version_id,
                    result.boundary_adjustment_type,
                    result.boundary_adjustment,
                    RECONCILIATION_RULE_VERSION,
                ),
            )

    def _publish_player_daily_log(
        self,
        connection: Any,
        *,
        player_id: int,
        ranked_day_start: datetime,
        ranked_day_end: datetime,
        official_season_id: str,
        season_day_number: int,
        version_number: int,
        ranked_day_version_id: int,
        result: ReconciliationResult,
        contribution_evidence: list[dict[str, Any]],
    ) -> None:
        adjustment_rows = connection.execute(
            """
            SELECT adjustment_type, amount, evidence_state, rule_version,
                   evidence_json
            FROM ranked_day_adjustments
            WHERE ranked_day_version_id = %s
            ORDER BY id
            """,
            (ranked_day_version_id,),
        ).fetchall()
        adjustments = [
            {
                "type": _text_value(row[0]),
                "amount": int(row[1]),
                "evidence_state": _text_value(row[2]),
                "rule_version": _text_value(row[3]),
                "evidence": row[4],
            }
            for row in adjustment_rows
        ]
        canonical_events = serialize_ranked_day_battles(contribution_evidence)
        events_by_battle_id = {
            str(event["battle_id"]): event for event in canonical_events
        }
        battles: list[dict[str, Any]] = []
        for item in contribution_evidence:
            if item.get("included") is not True:
                continue
            battle_id = item.get("battle_identity")
            # Keep the frozen reconciliation evidence (source and decision
            # fields) and add the canonical screen-event projection alongside
            # it. Evidence that cannot produce a screen event is still kept
            # here for the existing private/audit contract; the API mapper
            # excludes it from offense/defense event arrays.
            event = (
                events_by_battle_id.get(str(battle_id))
                if battle_id is not None
                else None
            )
            battles.append({**item, **event} if event is not None else dict(item))
        attack_three_star_count = sum(
            item.get("lens") == "offense" and item.get("stars") == 3 for item in battles
        )
        defense_three_star_count = sum(
            item.get("lens") == "defense" and item.get("stars") == 3 for item in battles
        )
        # Automatic reset loss is published in adjustments, not attributed to
        # an opponent battle. Keep this aggregate equal to the defense events.
        defense_loss = result.observed_defense_loss
        public_state = (
            result.state
            if result.state in {"Live", "Complete", "Partial"}
            else "Partial"
        )
        partial_reasons = list(result.failure_reasons)
        if public_state != result.state:
            partial_reasons.append(f"ranked_day_state:{result.state}")
        offense_events = [
            item
            for item in battles
            if item.get("lens") == "offense" and "battle_id" in item
        ]
        defense_events = [
            item
            for item in battles
            if item.get("lens") == "defense" and "battle_id" in item
        ]
        projection_consistent = (
            len(offense_events) == result.attack_count
            and len(defense_events) == result.defense_count
            and sum(item.get("stars") == 3 for item in offense_events)
            == attack_three_star_count
            and sum(item.get("stars") == 3 for item in defense_events)
            == defense_three_star_count
            and sum(int(item["trophy_change"]) for item in offense_events)
            == result.attack_trophy_gain
            and abs(sum(int(item["trophy_change"]) for item in defense_events))
            == result.observed_defense_loss
        )
        if not projection_consistent:
            if public_state == "Complete":
                public_state = "Partial"
            partial_reasons.append("battle_event_projection_incomplete")
        daily_log_values = (
            player_id,
            ranked_day_start,
            version_number,
            public_state,
            "complete" if result.coverage_complete else "partial",
            Jsonb(adjustments),
            Jsonb(battles),
            Jsonb(partial_reasons),
            ranked_day_end,
            official_season_id,
            season_day_number,
            result.confidence,
            result.attack_count,
            attack_three_star_count,
            result.attack_trophy_gain,
            result.defense_count,
            defense_three_star_count,
            defense_loss,
            result.net_trophy_change,
        )
        if getattr(self, "_supports_coordinator_contract", False):
            connection.execute(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, ranked_day_version_id, version, state, coverage,
                    adjustments, battles, partial_reasons, ranked_day_end,
                    official_season_id, season_day_number, confidence,
                    attack_count, attack_three_star_count, attack_gain,
                    defense_count, defense_three_star_count, defense_loss,
                    net_trophy_change
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (player_id, ranked_day_start, version) DO NOTHING
                """,
                (
                    player_id,
                    ranked_day_start,
                    ranked_day_version_id,
                    *daily_log_values[2:],
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, version, state, coverage,
                    adjustments, battles, partial_reasons, ranked_day_end,
                    official_season_id, season_day_number, confidence,
                    attack_count, attack_three_star_count, attack_gain,
                    defense_count, defense_three_star_count, defense_loss,
                    net_trophy_change
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (player_id, ranked_day_start, version) DO NOTHING
                """,
                daily_log_values,
            )

    @staticmethod
    def _boundary_population_hash(player_ids: list[int]) -> str:
        return hashlib.sha256(
            json.dumps(sorted(player_ids), separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _create_boundary_generation(
        self,
        connection: Any,
        *,
        boundary_at: datetime,
        sweep_id: int,
        player_ids: list[int],
        generation: int,
        supersedes_id: int | None,
        pending_inputs: list[dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        population_hash = self._boundary_population_hash(player_ids)
        target_at = boundary_at + timedelta(
            minutes=10 if boundary_at.weekday() == 0 else 5
        )
        row = connection.execute(
            """
            INSERT INTO boundary_publication_generations (
                boundary_at, generation, sweep_id, ordering_rule_version,
                freshness_rule_version, expected_population_count,
                expected_population_hash, membership_rule_version,
                snapshot_rule_version, army_rule_version, target_rule, target_at,
                supersedes_id, source_generation_id,
                correction_state, affected_artifacts
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, generation
            """,
            (
                boundary_at,
                generation,
                sweep_id,
                SNAPSHOT_ORDERING_RULE_VERSION,
                FRESHNESS_RULE_VERSION,
                len(player_ids),
                population_hash,
                "active-members-v1",
                ANALYTICS_RULE_VERSION,
                ARMY_ANALYTICS_RULE_VERSION,
                "boundary-delay-v1",
                target_at,
                supersedes_id,
                supersedes_id,
                "active" if supersedes_id is not None else "none",
                ["snapshot", "army"] if supersedes_id is not None else [],
            ),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT id, generation, expected_population_count,
                       expected_population_hash, sweep_id
                FROM boundary_publication_generations
                WHERE boundary_at = %s AND generation = %s
                FOR UPDATE
                """,
                (boundary_at, generation),
            ).fetchone()
        assert row is not None
        generation_id = int(row[0])
        if len(row) > 2 and (
            int(row[2]) != len(player_ids)
            or _text_value(row[3]) != population_hash
            or int(row[4]) != sweep_id
        ):
            raise ValueError(
                "boundary generation inputs conflict with captured membership"
            )
        if supersedes_id is None:
            connection.execute(
                """
                INSERT INTO boundary_publication_generation_members
                    (generation_id, player_id)
                SELECT %s, player_id
                FROM collector_reset_sweep_members
                WHERE sweep_id = %s
                ON CONFLICT DO NOTHING
                """,
                (generation_id, sweep_id),
            )
        else:
            connection.execute(
                """
                INSERT INTO boundary_publication_generation_members (
                    generation_id, player_id, ranked_day_version_id,
                    ranked_day_input_hash, status, snapshot_status, army_status
                )
                SELECT %s, player_id, ranked_day_version_id,
                       ranked_day_input_hash, status, snapshot_status, army_status
                FROM boundary_publication_generation_members
                WHERE generation_id = %s
                ON CONFLICT DO NOTHING
                """,
                (generation_id, supersedes_id),
            )
        for pending in pending_inputs or []:
            pending_version = pending.get("ranked_day_version_id")
            pending_snapshot_status = self._boundary_snapshot_status(
                connection,
                player_id=int(pending["player_id"]),
                ranked_day_version_id=int(pending_version),
                boundary_at=boundary_at,
            )
            pending_army_status = self._boundary_army_status(
                connection,
                player_id=int(pending["player_id"]),
                ranked_day_version_id=int(pending_version),
                snapshot_status=pending_snapshot_status,
            )
            connection.execute(
                """
                UPDATE boundary_publication_generation_members
                SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                    status = 'terminal', snapshot_status = %s,
                    army_status = %s, updated_at = clock_timestamp()
                WHERE generation_id = %s AND player_id = %s
                """,
                (
                    pending_version,
                    pending.get("input_hash"),
                    pending_snapshot_status,
                    pending_army_status,
                    generation_id,
                    pending.get("player_id"),
                ),
            )
        connection.execute(
            """
            UPDATE boundary_publication_generations
            SET membership_captured_at = COALESCE(membership_captured_at, clock_timestamp())
            WHERE id = %s
            """,
            (generation_id,),
        )
        return generation_id, int(row[1])

    def _freeze_boundary_manifest(
        self,
        connection: Any,
        *,
        generation_id: int,
        artifact_kind: str,
    ) -> tuple[int, str] | None:
        """Freeze one sorted coordinator input before creating its job.

        The row and digest are persisted together. Once inserted, the manifest
        trigger makes both the population and its selected inputs immutable;
        retrying callers therefore reuse the same identity instead of
        reconstructing inputs from live eligibility tables.
        """
        existing = connection.execute(
            """
            SELECT id, digest
            FROM boundary_publication_manifests
            WHERE generation_id = %s AND artifact_kind = %s
            FOR UPDATE
            """,
            (generation_id, artifact_kind),
        ).fetchone()
        if existing is not None:
            return int(existing[0]), _text_value(existing[1])
        generation = connection.execute(
            """
            SELECT boundary_at, generation, ordering_rule_version, freshness_rule_version,
                   snapshot_rule_version, army_rule_version
            FROM boundary_publication_generations
            WHERE id = %s
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if generation is None:
            return None
        official_version_id = None
        if artifact_kind == "snapshot":
            official_version = connection.execute(
                """
                SELECT v.id
                FROM official_top200_versions AS v
                JOIN official_top200_attempts AS a ON a.id = v.attempt_id
                JOIN official_top200_entries AS e ON e.version_id = v.id
                WHERE a.outcome = 'official_observed' AND v.observed_at <= %s
                GROUP BY v.id, v.observed_at
                HAVING count(*) = 200 AND count(DISTINCT e.rank) = 200
                   AND min(e.rank) = 1 AND max(e.rank) = 200
                ORDER BY v.observed_at DESC, v.id DESC
                LIMIT 1
                """,
                (generation[0],),
            ).fetchone()
            official_version_id = int(official_version[0]) if official_version else None
        members = connection.execute(
            """
            SELECT player_id, ranked_day_version_id, ranked_day_input_hash,
                   snapshot_status, army_status
            FROM boundary_publication_generation_members
            WHERE generation_id = %s
            ORDER BY player_id, ranked_day_version_id NULLS FIRST, ranked_day_input_hash NULLS FIRST
            """,
            (generation_id,),
        ).fetchall()
        manifest_rows: list[dict[str, Any]] = []
        for ordinal, row in enumerate(members, start=1):
            player_id = int(row[0])
            version_id = int(row[1]) if row[1] is not None else None
            input_hash = _text_value(row[2]) if row[2] is not None else None
            status = _text_value(row[3] if artifact_kind == "snapshot" else row[4])
            classification = {
                "complete": "Complete",
                "partial": "Partial",
                "failed": "Failed",
                "missing": "Missing",
                "unavailable": "Unavailable",
                "inconsistent": "Inconsistent",
                "malformed": "Malformed",
            }.get(status, "Pending")
            if (
                status in {"complete", "partial", "inconsistent", "malformed"}
                and version_id is not None
            ):
                if artifact_kind == "snapshot":
                    classification = {
                        "complete": "Complete",
                        "partial": "Partial",
                        "missing": "Missing",
                        "unavailable": "Unavailable",
                        "inconsistent": "Inconsistent",
                        "malformed": "Malformed",
                    }.get(
                        self._boundary_snapshot_status(
                            connection,
                            player_id=player_id,
                            ranked_day_version_id=version_id,
                            boundary_at=generation[0],
                        ),
                        "Missing",
                    )
                elif status == "partial":
                    classification = "Partial"
                else:
                    state_row = connection.execute(
                        "SELECT state FROM ranked_day_versions WHERE id = %s",
                        (version_id,),
                    ).fetchone()
                    state = (
                        _text_value(state_row[0])
                        if state_row is not None
                        else "Malformed"
                    )
                    classification = (
                        state
                        if state in {"Complete", "Partial", "Malformed", "Inconsistent"}
                        else "Partial"
                    )
            identity = {
                "artifact_kind": artifact_kind,
                "generation": int(generation[1]),
                "player_id": player_id,
                "ranked_day_version_id": version_id,
                "input_hash": input_hash,
                "classification": classification,
            }
            if artifact_kind == "snapshot":
                identity["official_top200_version_id"] = official_version_id
                official_entry = None
                if official_version_id is not None:
                    official_entry = connection.execute(
                        """
                        SELECT rank, observed_at
                        FROM official_top200_entries
                        WHERE version_id = %s AND player_id = %s
                        """,
                        (official_version_id, player_id),
                    ).fetchone()
                identity["official_rank"] = (
                    int(official_entry[0]) if official_entry else None
                )
                identity["official_rank_observed_at"] = (
                    official_entry[1].astimezone(UTC).isoformat()
                    if official_entry
                    else None
                )
                profile = connection.execute(
                    """
                    SELECT id, observation_id, observed_at, profile_json, normalized_tag,
                           name, trophies, eligibility_state
                    FROM player_profile_versions
                    WHERE player_id = %s AND observed_at <= %s
                      AND source_contract_state = 'accepted'
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 1
                    """,
                    (player_id, generation[0]),
                ).fetchone()
                if profile is not None:
                    identity["profile_version_id"] = int(profile[0])
                    identity["profile_input_hash"] = hashlib.sha256(
                        json.dumps(
                            profile[3], sort_keys=True, separators=(",", ":")
                        ).encode("utf-8")
                    ).hexdigest()
                    identity["profile_snapshot"] = {
                        "observation_id": int(profile[1]),
                        "tag": _text_value(profile[4]),
                        "name": profile[5],
                        "trophies": int(profile[6]),
                        "observed_at": profile[2].astimezone(UTC).isoformat(),
                        "eligibility_state": _text_value(profile[7]),
                        "profile_json": profile[3],
                    }
                else:
                    identity["profile_version_id"] = None
                    identity["profile_input_hash"] = None
            if artifact_kind == "army" and version_id is not None:
                ranked_identity = connection.execute(
                    """
                    SELECT input_evidence, coverage_evidence, start_baseline_id,
                           end_baseline_id
                    FROM ranked_day_versions WHERE id = %s
                    """,
                    (version_id,),
                ).fetchone()
                if ranked_identity is not None:
                    identity.update(
                        {
                            "input_evidence": ranked_identity[0],
                            "coverage_evidence": ranked_identity[1],
                            "start_baseline_id": ranked_identity[2],
                            "end_baseline_id": ranked_identity[3],
                        }
                    )
                daily_log = connection.execute(
                    "SELECT id, battles FROM api_player_daily_logs WHERE ranked_day_version_id = %s ORDER BY id DESC LIMIT 1",
                    (version_id,),
                ).fetchone()
                identity["daily_log_id"] = int(daily_log[0]) if daily_log else None
                battle_ids: list[int] = []
                decode_ids: list[int] = []
                if daily_log is not None and isinstance(daily_log[1], list):
                    battle_ids = [
                        int(event["battle_id"])
                        for event in daily_log[1]
                        if isinstance(event, dict)
                        and str(event.get("battle_id", "")).isdigit()
                    ]
                    if battle_ids:
                        decode_ids = [
                            int(row[0])
                            for row in connection.execute(
                                """
                                SELECT id FROM battle_army_decodes
                                WHERE battle_id = ANY(%s::bigint[]) AND is_active
                                  AND decoder_version = %s AND catalog_version = %s
                                ORDER BY id
                                """,
                                (battle_ids, DECODER_VERSION, CATALOG_VERSION),
                            ).fetchall()
                        ]
                evidence_ids: list[int] = []
                if daily_log is not None and isinstance(daily_log[1], list):
                    for event in daily_log[1]:
                        if (
                            not isinstance(event, dict)
                            or not str(event.get("battle_id", "")).isdigit()
                        ):
                            continue
                        perspective = (
                            "attacker" if event.get("lens") == "offense" else "defender"
                        )
                        evidence_row = connection.execute(
                            """
                            SELECT perspective.evidence_id
                            FROM battle_perspectives AS perspective
                            WHERE perspective.battle_id = %s
                              AND perspective.perspective = %s
                            """,
                            (int(event["battle_id"]), perspective),
                        ).fetchone()
                        if evidence_row is not None:
                            evidence_ids.append(int(evidence_row[0]))
                identity["battle_ids"] = sorted(set(battle_ids))
                identity["decode_ids"] = decode_ids
                identity["evidence_ids"] = sorted(set(evidence_ids))
            manifest_rows.append(identity)
        season_inputs = None
        if artifact_kind == "army":
            season_row = connection.execute(
                """
                SELECT official_season_id
                FROM ranked_day_versions
                WHERE id = ANY(%s::bigint[])
                ORDER BY id
                LIMIT 1
                """,
                ([row[1] for row in members if row[1] is not None],),
            ).fetchone()
            if season_row is not None:
                season_versions = connection.execute(
                    """
                    SELECT id
                    FROM ranked_day_versions
                    WHERE official_season_id = %s
                      AND state = 'Complete' AND coverage_complete
                    ORDER BY id
                    """,
                    (season_row[0],),
                ).fetchall()
                season_version_ids = [int(row[0]) for row in season_versions]
                season_logs = connection.execute(
                    """
                    SELECT DISTINCT ON (ranked_day_version_id) id, battles
                    FROM api_player_daily_logs
                    WHERE ranked_day_version_id = ANY(%s::bigint[])
                      AND state = 'Complete' AND coverage = 'complete'
                    ORDER BY ranked_day_version_id, version DESC, id DESC
                    """,
                    (season_version_ids,),
                ).fetchall()
                season_daily_log_ids = [int(row[0]) for row in season_logs]
                season_battle_ids = sorted(
                    {
                        int(event["battle_id"])
                        for row in season_logs
                        for event in (row[1] if isinstance(row[1], list) else [])
                        if isinstance(event, dict)
                        and str(event.get("battle_id", "")).isdigit()
                    }
                )
                season_decode_ids = [
                    int(row[0])
                    for row in connection.execute(
                        """
                        SELECT id
                        FROM battle_army_decodes
                        WHERE battle_id = ANY(%s::bigint[])
                          AND decoder_version = %s AND catalog_version = %s
                          AND is_active
                        ORDER BY id
                        """,
                        (season_battle_ids, DECODER_VERSION, CATALOG_VERSION),
                    ).fetchall()
                ]
                season_evidence_ids = [
                    int(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT evidence_id
                        FROM battle_perspectives
                        WHERE battle_id = ANY(%s::bigint[])
                        ORDER BY evidence_id
                        """,
                        (season_battle_ids,),
                    ).fetchall()
                ]
                season_inputs = {
                    "ranked_version_ids": season_version_ids,
                    "daily_log_ids": season_daily_log_ids,
                    "battle_ids": season_battle_ids,
                    "decode_ids": season_decode_ids,
                    "evidence_ids": season_evidence_ids,
                }
        rule_versions = {
            "ordering_rule_version": _text_value(generation[2]),
            "freshness_rule_version": _text_value(generation[3]),
            "analytics_rule_version": (
                _text_value(generation[5])
                if artifact_kind == "army"
                else _text_value(generation[4])
            ),
            **({"season_inputs": season_inputs} if season_inputs is not None else {}),
        }
        digest = hashlib.sha256(
            json.dumps(
                {
                    "generation": int(generation[1]),
                    "artifact_kind": artifact_kind,
                    "rule_versions": rule_versions,
                    "rows": manifest_rows,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        manifest = connection.execute(
            """
            INSERT INTO boundary_publication_manifests
                (generation_id, artifact_kind, rule_versions, digest)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (generation_id, artifact_kind, Jsonb(rule_versions), digest),
        ).fetchone()
        assert manifest is not None
        manifest_id = int(manifest[0])
        for ordinal, identity in enumerate(manifest_rows, start=1):
            connection.execute(
                """
                INSERT INTO boundary_publication_manifest_rows
                    (manifest_id, ordinal, player_id, ranked_day_version_id,
                     input_hash, classification, unavailable_reason, input_identity)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    manifest_id,
                    ordinal,
                    identity["player_id"],
                    identity["ranked_day_version_id"],
                    identity["input_hash"],
                    identity["classification"],
                    "reset_baseline_failed"
                    if identity["classification"] == "Unavailable"
                    else None,
                    Jsonb(identity),
                ),
            )
        connection.execute(
            """
            UPDATE boundary_publication_manifests
            SET rows_sealed = true, frozen_at = clock_timestamp(),
                enqueued_at = clock_timestamp()
            WHERE id = %s AND NOT rows_sealed
            """,
            (manifest_id,),
        )
        column = (
            "snapshot_manifest_id"
            if artifact_kind == "snapshot"
            else "army_manifest_id"
        )
        connection.execute(
            f"UPDATE boundary_publication_generations SET {column} = %s, updated_at = clock_timestamp() WHERE id = %s",
            (manifest_id, generation_id),
        )
        return manifest_id, digest

    def _inherit_deferred_army_successor_snapshot(
        self, connection: Any, *, generation_id: int
    ) -> bool:
        target = connection.execute(
            """
            SELECT source_generation_id, snapshot_state, snapshot_manifest_id,
                   affected_artifacts
            FROM boundary_publication_generations
            WHERE id = %s
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if target is None or _text_value(target[1]) != "pending":
            return False
        if [_text_value(value) for value in (target[3] or [])] != ["army"]:
            return False
        if target[0] is None or target[2] is not None:
            return False
        if (
            connection.execute(
                """
            SELECT 1
            FROM boundary_publication_corrections
            WHERE source_generation_id = %s
              AND state IN ('queued', 'pending_inputs', 'active')
            LIMIT 1
            """,
                (generation_id,),
            ).fetchone()
            is not None
        ):
            return False
        source = connection.execute(
            """
            SELECT snapshot_state, snapshot_id, snapshot_input_hash,
                   snapshot_manifest_id, snapshot_analytics_publication_id,
                   snapshot_coverage
            FROM boundary_publication_generations
            WHERE id = %s
            FOR UPDATE
            """,
            (target[0],),
        ).fetchone()
        if (
            source is None
            or _text_value(source[0]) not in {"published", "superseded"}
            or any(value is None for value in source[1:5])
        ):
            return False
        connection.execute(
            """
            UPDATE boundary_publication_generations
            SET snapshot_state = %s,
                snapshot_id = %s,
                snapshot_input_hash = %s,
                snapshot_manifest_id = %s,
                snapshot_analytics_publication_id = %s,
                snapshot_coverage = %s,
                updated_at = clock_timestamp()
            WHERE id = %s
              AND snapshot_state = 'pending'
              AND snapshot_manifest_id IS NULL
              AND affected_artifacts = ARRAY['army']::text[]
            """,
            ("published", *source[1:5], Jsonb(source[5]), generation_id),
        )
        return True

    def _try_enqueue_boundary_artifacts(
        self, connection: Any, *, boundary_at: datetime, generation_id: int
    ) -> None:
        self._inherit_deferred_army_successor_snapshot(
            connection, generation_id=generation_id
        )
        generation = connection.execute(
            """
            SELECT id, generation, sweep_id, snapshot_state, army_state,
                   expected_population_count, affected_artifacts, target_at, target_rule
            FROM boundary_publication_generations
            WHERE id = %s
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if generation is None:
            return
        generation_number = int(generation[1])
        sweep_id = int(generation[2]) if generation[2] is not None else None
        if sweep_id is None:
            return

        # The collector owns this handoff. Test fixtures that do not create a
        # gate row retain the historical direct coordinator seam.
        handoff = connection.execute(
            "SELECT safe_handoff FROM collector_boundary_admission WHERE boundary_at = %s",
            (boundary_at,),
        ).fetchone()
        if handoff is None:
            if getattr(self, "_supports_coordinator_contract", False):
                return
        elif not bool(handoff[0]):
            return
        classifications = connection.execute(
            """
            SELECT count(*) AS member_count,
                   count(*) FILTER (WHERE snapshot_status = 'pending') AS snapshot_pending,
                   count(*) FILTER (WHERE army_status = 'pending') AS army_pending,
                   count(*) FILTER (WHERE snapshot_status NOT IN
                       ('complete','partial','failed','missing','unavailable','inconsistent','malformed','pending')) AS bad_snapshot,
                   count(*) FILTER (WHERE army_status NOT IN
                       ('complete','partial','failed','missing','unavailable','inconsistent','malformed','pending')) AS bad_army
            FROM boundary_publication_generation_members
            WHERE generation_id = %s
            """,
            (generation_id,),
        ).fetchone()
        assert classifications is not None
        member_count, snapshot_pending, army_pending, bad_snapshot, bad_army = (
            int(value) for value in classifications
        )
        if member_count != int(generation[5]) or bad_snapshot or bad_army:
            return
        boundary_text = boundary_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if _text_value(generation[8]) != "boundary-delay-v1":
            return
        target = generation[7]
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if now < target:
            return
        affected_artifacts = [_text_value(value) for value in (generation[6] or [])]
        if snapshot_pending == 0 and _text_value(generation[3]) == "pending":
            connection.execute(
                "UPDATE boundary_publication_generations SET snapshot_state = 'ready', updated_at = clock_timestamp() WHERE id = %s AND snapshot_state = 'pending'",
                (generation_id,),
            )
        if army_pending == 0 and _text_value(generation[4]) == "pending":
            connection.execute(
                "UPDATE boundary_publication_generations SET army_state = 'ready', updated_at = clock_timestamp() WHERE id = %s AND army_state = 'pending'",
                (generation_id,),
            )
        snapshot = connection.execute(
            "SELECT snapshot_state, snapshot_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
            (generation_id,),
        ).fetchone()
        if (
            snapshot_pending == 0
            and snapshot is not None
            and _text_value(snapshot[0]) == "ready"
            and (not affected_artifacts or "snapshot" in affected_artifacts)
        ):
            manifest = self._freeze_boundary_manifest(
                connection, generation_id=generation_id, artifact_kind="snapshot"
            )
            assert manifest is not None
            connection.execute(
                """
                INSERT INTO python_processing_jobs_worker (
                    observation_id, work_type, deduplication_key, input_json,
                    state, due_at, parser_version, processing_version,
                    domain_rule_version, analytics_rule_version
                ) VALUES (NULL, 'build_snapshot', %s, %s, 'pending', %s, %s, %s, %s, %s)
                ON CONFLICT (deduplication_key) DO NOTHING
                """,
                (
                    f"build_snapshot:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}",
                    Jsonb(
                        {
                            "boundary_at": boundary_text,
                            "generation": generation_number,
                            "manifest_id": manifest[0],
                            "manifest_digest": manifest[1],
                        }
                    ),
                    target,
                    DEFAULT_PARSER_VERSION,
                    PROCESSING_VERSION,
                    DOMAIN_RULE_VERSION,
                    ANALYTICS_RULE_VERSION,
                ),
            )
        army = connection.execute(
            "SELECT army_state, army_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
            (generation_id,),
        ).fetchone()
        if (
            army_pending == 0
            and army is not None
            and _text_value(army[0]) == "ready"
            and (not affected_artifacts or "army" in affected_artifacts)
        ):
            manifest = self._freeze_boundary_manifest(
                connection, generation_id=generation_id, artifact_kind="army"
            )
            assert manifest is not None
            connection.execute(
                """
                INSERT INTO python_processing_jobs_worker (
                    observation_id, work_type, deduplication_key, input_json,
                    state, due_at, parser_version, processing_version,
                    domain_rule_version, analytics_rule_version
                ) VALUES (NULL, 'build_army_analytics', %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)
                ON CONFLICT (deduplication_key) DO NOTHING
                """,
                (
                    f"build_army_analytics:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}",
                    Jsonb(
                        {
                            "boundary_at": boundary_text,
                            "generation": generation_number,
                            "manifest_id": manifest[0],
                            "manifest_digest": manifest[1],
                        }
                    ),
                    DEFAULT_PARSER_VERSION,
                    PROCESSING_VERSION,
                    DOMAIN_RULE_VERSION,
                    ARMY_ANALYTICS_RULE_VERSION,
                ),
            )

    @staticmethod
    def _boundary_snapshot_status(
        connection: Any,
        *,
        player_id: int,
        ranked_day_version_id: int,
        boundary_at: datetime | None = None,
    ) -> str:
        ranked = connection.execute(
            "SELECT state FROM ranked_day_versions WHERE id = %s AND player_id = %s",
            (ranked_day_version_id, player_id),
        ).fetchone()
        status = {
            "Complete": "complete",
            "Partial": "partial",
            "Inconsistent": "inconsistent",
            "Malformed": "malformed",
        }.get(_text_value(ranked[0]) if ranked else "", "pending")
        if status != "complete":
            return status
        profile = connection.execute(
            """
            SELECT 1
            FROM player_profile_versions
            WHERE player_id = %s
              AND source_contract_state = 'accepted'
              AND (%s::timestamptz IS NULL OR observed_at <= %s)
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (player_id, boundary_at, boundary_at),
        ).fetchone()
        return "complete" if profile is not None else "missing"

    def _boundary_army_status(
        self,
        connection: Any,
        *,
        player_id: int,
        ranked_day_version_id: int,
        snapshot_status: str,
    ) -> str:
        if snapshot_status in {"pending", "unavailable", "missing", "failed"}:
            return (
                "unavailable"
                if snapshot_status in {"unavailable", "missing", "failed"}
                else "pending"
            )
        daily_log = connection.execute(
            """
            SELECT battles, state, coverage
            FROM api_player_daily_logs
            WHERE player_id = %s AND ranked_day_version_id = %s
            ORDER BY version DESC LIMIT 1
            """,
            (player_id, ranked_day_version_id),
        ).fetchone()
        # Coordinated publication requires daily-log evidence before army
        # readiness; direct legacy reconciliation retains its old fallback.
        if daily_log is None:
            return (
                "pending"
                if getattr(self, "_supports_coordinator_contract", False)
                else snapshot_status
            )
        daily_state = _text_value(daily_log[1])
        daily_coverage = _text_value(daily_log[2])
        if daily_state == "Partial":
            return "partial"
        if daily_state != "Complete" or daily_coverage != "complete":
            return "pending"
        battle_ids = [
            int(event["battle_id"])
            for event in (daily_log[0] if isinstance(daily_log[0], list) else [])
            if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
        ]
        if not battle_ids:
            return "complete"
        decoded = connection.execute(
            """
            SELECT count(DISTINCT battle_id)
            FROM battle_army_decodes
            WHERE battle_id = ANY(%s::bigint[]) AND is_active
              AND decoder_version = %s AND catalog_version = %s
            """,
            (battle_ids, DECODER_VERSION, CATALOG_VERSION),
        ).fetchone()
        return (
            snapshot_status
            if decoded is not None and int(decoded[0]) == len(set(battle_ids))
            else "pending"
        )

    def _record_boundary_generation(
        self,
        connection: Any,
        *,
        boundary_at: datetime,
        player_id: int,
        ranked_day_version_id: int,
        ranked_day_input_hash: str,
    ) -> bool:
        """Record one member result and enqueue each ready artifact once.

        The collector's sweep membership is the frozen population authority.
        A generation is reused until either artifact has frozen; after that a
        changed member starts one superseding generation and later corrections
        coalesce into it.
        """
        boundary_at = boundary_at.astimezone(UTC)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"boundary-publication:{boundary_at.isoformat()}",),
        )
        sweep = connection.execute(
            "SELECT id FROM collector_reset_sweeps WHERE boundary_at = %s",
            (boundary_at,),
        ).fetchone()
        if sweep is None:
            return False
        sweep_id = int(sweep[0])
        member_exists = connection.execute(
            """
            SELECT 1
            FROM collector_reset_sweep_members
            WHERE sweep_id = %s AND player_id = %s
            """,
            (sweep_id, player_id),
        ).fetchone()
        if member_exists is None:
            return True
        current = connection.execute(
            """
            SELECT id, generation, snapshot_state, army_state,
                   correction_state, snapshot_manifest_id, army_manifest_id,
                   affected_artifacts
            FROM boundary_publication_generations
            WHERE boundary_at = %s
              AND snapshot_state <> 'superseded'
              AND army_state <> 'superseded'
            ORDER BY generation DESC
            LIMIT 1
            FOR UPDATE
            """,
            (boundary_at,),
        ).fetchone()
        if current is None:
            population_rows = connection.execute(
                """
                SELECT player_id
                FROM collector_reset_sweep_members
                WHERE sweep_id = %s
                ORDER BY player_id
                """,
                (sweep_id,),
            ).fetchall()
            generation_id, generation = self._create_boundary_generation(
                connection,
                boundary_at=boundary_at,
                sweep_id=sweep_id,
                player_ids=[int(row[0]) for row in population_rows],
                generation=1,
                supersedes_id=None,
            )
        else:
            generation_id, generation = int(current[0]), int(current[1])
            prior_member = connection.execute(
                """
                SELECT ranked_day_version_id, ranked_day_input_hash
                FROM boundary_publication_generation_members
                WHERE generation_id = %s AND player_id = %s
                FOR UPDATE
                """,
                (generation_id, player_id),
            ).fetchone()
            # A generation's expected membership is immutable. A player that
            # appears in the sweep after generation capture is not inserted,
            # including after a frozen publication.
            if prior_member is None:
                return True
            changed = (
                prior_member[0] is None
                or int(prior_member[0]) != int(ranked_day_version_id)
                or _text_value(prior_member[1]) != ranked_day_input_hash
            )
            frozen_artifacts = [
                kind
                for kind, manifest_id in (
                    ("snapshot", current[5]),
                    ("army", current[6]),
                )
                if manifest_id is not None
            ]
            correction_active = _text_value(current[4]) == "active"
            if correction_active and current[7] and not (changed and frozen_artifacts):
                affected = {_text_value(value) for value in current[7]}
                frozen_artifacts = [
                    kind for kind in frozen_artifacts if kind in affected
                ]
            elif changed and frozen_artifacts:
                # A ranked-day source feeds both artifacts. Once either
                # manifest has frozen, reprocess both instead of inheriting an
                # army identity built from the previous source version.
                frozen_artifacts = ["snapshot", "army"]
            fully_published = (
                _text_value(current[2]) == "published"
                and _text_value(current[3]) == "published"
            )
            active_target_correction = connection.execute(
                """
                SELECT id
                FROM boundary_publication_corrections
                WHERE generation_id = %s AND state = 'active'
                FOR UPDATE
                """,
                (generation_id,),
            ).fetchone()
            if (
                changed
                and active_target_correction is not None
                and not frozen_artifacts
            ):
                snapshot_status = self._boundary_snapshot_status(
                    connection,
                    player_id=player_id,
                    ranked_day_version_id=ranked_day_version_id,
                    boundary_at=boundary_at,
                )
                army_status = self._boundary_army_status(
                    connection,
                    player_id=player_id,
                    ranked_day_version_id=ranked_day_version_id,
                    snapshot_status=snapshot_status,
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generation_members
                    SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                        status = %s, snapshot_status = %s, army_status = %s,
                        updated_at = clock_timestamp()
                    WHERE generation_id = %s AND player_id = %s
                    """,
                    (
                        ranked_day_version_id,
                        ranked_day_input_hash,
                        "terminal" if snapshot_status != "pending" else "pending",
                        snapshot_status,
                        army_status,
                        generation_id,
                        player_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET affected_artifacts = ARRAY(
                            SELECT DISTINCT unnest(affected_artifacts || ARRAY['snapshot']::text[])
                        ),
                        updated_at = clock_timestamp()
                    WHERE id = %s
                    """,
                    (generation_id,),
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_corrections
                    SET affected_artifacts = ARRAY(
                            SELECT DISTINCT unnest(affected_artifacts || ARRAY['snapshot']::text[])
                        )
                    WHERE id = %s
                    """,
                    (active_target_correction[0],),
                )
                self._try_enqueue_boundary_artifacts(
                    connection, boundary_at=boundary_at, generation_id=generation_id
                )
                return True
            if (
                changed
                and frozen_artifacts
                and (correction_active or not fully_published)
            ):
                # Keep the changed input only in the correction queue. The
                # captured generation member remains immutable; the queued
                # successor applies this input before it seals inheritance.
                pending_input = {
                    "player_id": player_id,
                    "ranked_day_version_id": ranked_day_version_id,
                    "input_hash": ranked_day_input_hash,
                }
                queued = connection.execute(
                    """
                    SELECT id
                    FROM boundary_publication_corrections
                    WHERE boundary_at = %s AND source_generation_id = %s
                      AND state IN ('queued', 'pending_inputs')
                    ORDER BY id DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (boundary_at, generation_id),
                ).fetchone()
                correction_artifacts = frozen_artifacts
                if queued is None:
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_corrections
                            (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            boundary_at,
                            generation_id,
                            correction_artifacts,
                            Jsonb([pending_input]),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE boundary_publication_corrections
                        SET affected_artifacts = ARRAY(
                                SELECT DISTINCT unnest(affected_artifacts || %s::text[])
                            ),
                            pending_inputs = pending_inputs || %s::jsonb
                        WHERE id = %s
                        """,
                        (correction_artifacts, Jsonb([pending_input]), queued[0]),
                    )
                return True
            if (
                changed
                and not correction_active
                and frozen_artifacts
                and fully_published
            ):
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_state = 'superseded', army_state = 'superseded',
                        correction_state = 'finalized', updated_at = clock_timestamp()
                    WHERE id = %s
                    """,
                    (generation_id,),
                )
                frozen_members = connection.execute(
                    """
                    SELECT player_id
                    FROM boundary_publication_generation_members
                    WHERE generation_id = %s
                    ORDER BY player_id
                    """,
                    (generation_id,),
                ).fetchall()
                generation_id, generation = self._create_boundary_generation(
                    connection,
                    boundary_at=boundary_at,
                    sweep_id=sweep_id,
                    player_ids=[int(row[0]) for row in frozen_members],
                    generation=generation + 1,
                    supersedes_id=int(current[0]),
                    pending_inputs=[
                        {
                            "player_id": player_id,
                            "ranked_day_version_id": ranked_day_version_id,
                            "input_hash": ranked_day_input_hash,
                        }
                    ],
                )
                affected = frozen_artifacts or ["snapshot", "army"]
                connection.execute(
                    "UPDATE boundary_publication_generations SET affected_artifacts = %s WHERE id = %s",
                    (affected, generation_id),
                )
                connection.execute(
                    """
                    INSERT INTO boundary_publication_corrections
                        (boundary_at, source_generation_id, generation_id,
                         affected_artifacts, state, started_at)
                    VALUES (%s, %s, %s, %s, 'active', clock_timestamp())
                    """,
                    (boundary_at, current[0], generation_id, affected),
                )
        snapshot_status = self._boundary_snapshot_status(
            connection,
            player_id=player_id,
            ranked_day_version_id=ranked_day_version_id,
            boundary_at=boundary_at,
        )
        army_status = self._boundary_army_status(
            connection,
            player_id=player_id,
            ranked_day_version_id=ranked_day_version_id,
            snapshot_status=snapshot_status,
        )
        member_status = "terminal" if snapshot_status != "pending" else "pending"
        updated_member = connection.execute(
            """
            UPDATE boundary_publication_generation_members
            SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                status = %s, snapshot_status = %s, army_status = %s,
                updated_at = clock_timestamp()
            WHERE generation_id = %s AND player_id = %s
            """,
            (
                ranked_day_version_id,
                ranked_day_input_hash,
                member_status,
                snapshot_status,
                army_status,
                generation_id,
                player_id,
            ),
        )
        if updated_member.rowcount == 0:
            connection.execute(
                """
                INSERT INTO boundary_publication_generation_members (
                    generation_id, player_id, ranked_day_version_id,
                    ranked_day_input_hash, status, snapshot_status, army_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    generation_id,
                    player_id,
                    ranked_day_version_id,
                    ranked_day_input_hash,
                    member_status,
                    snapshot_status,
                    army_status,
                ),
            )
        self._try_enqueue_boundary_artifacts(
            connection, boundary_at=boundary_at, generation_id=generation_id
        )
        return True

    @staticmethod
    def _observation_source(
        claim: Claim,
    ) -> tuple[int, int, str, datetime, str, str]:
        values = (
            claim.observation_id,
            claim.http_status,
            claim.response_hash,
            claim.observed_at,
            claim.endpoint,
            claim.schema_version,
        )
        if any(value is None for value in values):
            raise ValueError("observation work is missing its archived source metadata")
        assert claim.observation_id is not None
        assert claim.http_status is not None
        assert claim.response_hash is not None
        assert claim.observed_at is not None
        assert claim.endpoint is not None
        assert claim.schema_version is not None
        return (
            claim.observation_id,
            claim.http_status,
            claim.response_hash,
            claim.observed_at,
            claim.endpoint,
            claim.schema_version,
        )

    @staticmethod
    def _upsert_player(connection: Any, normalized_tag: str, *, active: bool) -> int:
        row = connection.execute(
            """
            INSERT INTO players (normalized_tag, active, eligibility_state)
            VALUES (%s, %s, 'unknown')
            ON CONFLICT (normalized_tag) DO UPDATE
                SET updated_at = clock_timestamp()
            RETURNING id
            """,
            (normalized_tag, active),
        ).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _refresh_battle_disagreements(connection: Any, battle_ids: list[int]) -> None:
        if not battle_ids:
            return
        connection.execute(
            """
            WITH target AS (
                SELECT unnest(%s::bigint[]) AS battle_id
            ), normalized AS (
                SELECT p.battle_id, p.perspective,
                       e.battle_timestamp, e.stars,
                       e.destruction_percentage, e.army_share_code,
                       CASE WHEN p.perspective = 'attacker'
                           THEN e.reporter_trophies ELSE e.opponent_trophies
                       END AS attacker_trophies,
                       CASE WHEN p.perspective = 'attacker'
                           THEN e.opponent_trophies ELSE e.reporter_trophies
                       END AS defender_trophies,
                       e.attacker_gain, e.defender_loss
                FROM battle_perspectives AS p
                JOIN battle_evidence AS e ON e.id = p.evidence_id
                JOIN target ON target.battle_id = p.battle_id
            ), paired AS (
                SELECT target.battle_id, count(normalized.battle_id) AS evidence_count,
                       max(battle_timestamp) FILTER (WHERE perspective = 'attacker') AS a_timestamp,
                       max(battle_timestamp) FILTER (WHERE perspective = 'defender') AS d_timestamp,
                       max(stars) FILTER (WHERE perspective = 'attacker') AS a_stars,
                       max(stars) FILTER (WHERE perspective = 'defender') AS d_stars,
                       max(destruction_percentage) FILTER (WHERE perspective = 'attacker') AS a_destruction,
                       max(destruction_percentage) FILTER (WHERE perspective = 'defender') AS d_destruction,
                       max(army_share_code) FILTER (WHERE perspective = 'attacker') AS a_army,
                       max(army_share_code) FILTER (WHERE perspective = 'defender') AS d_army,
                       max(attacker_trophies) FILTER (WHERE perspective = 'attacker') AS a_attacker_trophies,
                       max(attacker_trophies) FILTER (WHERE perspective = 'defender') AS d_attacker_trophies,
                       max(defender_trophies) FILTER (WHERE perspective = 'attacker') AS a_defender_trophies,
                       max(defender_trophies) FILTER (WHERE perspective = 'defender') AS d_defender_trophies,
                       max(attacker_gain) FILTER (WHERE perspective = 'attacker') AS a_gain,
                       max(attacker_gain) FILTER (WHERE perspective = 'defender') AS d_gain,
                       max(defender_loss) FILTER (WHERE perspective = 'attacker') AS a_loss,
                       max(defender_loss) FILTER (WHERE perspective = 'defender') AS d_loss
                FROM target
                LEFT JOIN normalized ON normalized.battle_id = target.battle_id
                GROUP BY target.battle_id
            ), classified AS (
                SELECT battle_id, evidence_count,
                       array_remove(ARRAY[
                           CASE WHEN a_timestamp IS DISTINCT FROM d_timestamp THEN 'battle_timestamp' END,
                           CASE WHEN a_stars IS DISTINCT FROM d_stars THEN 'stars' END,
                           CASE WHEN a_destruction IS DISTINCT FROM d_destruction THEN 'destruction_percentage' END,
                           CASE WHEN a_army IS DISTINCT FROM d_army THEN 'army_share_code' END,
                           CASE WHEN a_attacker_trophies IS NOT NULL
                                      AND d_attacker_trophies IS NOT NULL
                                      AND a_attacker_trophies IS DISTINCT FROM d_attacker_trophies
                                THEN 'attacker_trophies' END,
                           CASE WHEN a_defender_trophies IS NOT NULL
                                      AND d_defender_trophies IS NOT NULL
                                      AND a_defender_trophies IS DISTINCT FROM d_defender_trophies
                                THEN 'defender_trophies' END,
                           CASE WHEN a_gain IS DISTINCT FROM d_gain THEN 'attacker_gain' END,
                           CASE WHEN a_loss IS DISTINCT FROM d_loss THEN 'defender_loss' END
                       ], NULL)::text[] AS fields
                FROM paired
            )
            UPDATE legend_battles AS battle
            SET disagreement_state = CASE
                    WHEN classified.evidence_count < 2 THEN 'single_perspective'
                    WHEN cardinality(classified.fields) = 0 THEN 'agreed'
                    ELSE 'disagreement'
                END,
                disagreement_fields = CASE
                    WHEN classified.evidence_count < 2 THEN ARRAY[]::text[]
                    ELSE classified.fields
                END,
                updated_at = clock_timestamp()
            FROM classified
            WHERE battle.id = classified.battle_id
            """,
            (battle_ids,),
        )

    def _upsert_army_decodes(self, connection: Any, battle_ids: list[int]) -> None:
        if not battle_ids:
            return
        exists = connection.execute(
            "SELECT to_regclass('battle_army_decodes')"
        ).fetchone()
        if exists is None or exists[0] is None:
            return
        catalog = connection.execute(
            "SELECT content_hash FROM unit_catalog_versions WHERE version = %s",
            (CATALOG_VERSION,),
        ).fetchone()
        catalog_ready = catalog is not None and _text_value(catalog[0]) == CATALOG_HASH
        rows = connection.execute(
            """
            SELECT b.id, p.evidence_id, p.perspective, e.army_share_code,
                   source_row.source_json
            FROM legend_battles AS b
            JOIN battle_perspectives AS p ON p.battle_id = b.id
            JOIN battle_evidence AS e ON e.id = p.evidence_id
            JOIN battle_source_rows AS source_row ON source_row.id = e.source_row_id
            WHERE b.id = ANY(%s::bigint[])
            """,
            (battle_ids,),
        ).fetchall()
        for battle_id, evidence_id, perspective, raw_code, source_json in rows:
            perspective = _text_value(perspective)
            source_code = (
                source_json.get("armyShareCode")
                if isinstance(source_json, dict)
                else None
            )
            if source_code is not None and not isinstance(source_code, str):
                decoded: DecodedArmy | DecodeFailure = DecodeFailure(
                    None,
                    "malformed",
                    "armyShareCode must be text",
                    DECODER_VERSION,
                    CATALOG_VERSION,
                    CATALOG_HASH,
                )
            elif catalog_ready:
                decoded = decode_army_share_code(raw_code)
            else:
                decoded = DecodeFailure(
                    raw_code,
                    "catalog_version_unavailable",
                    "pinned unit catalog is unavailable or has the wrong hash",
                    DECODER_VERSION,
                    CATALOG_VERSION,
                    CATALOG_HASH,
                )
            is_decoded = isinstance(decoded, DecodedArmy)
            if is_decoded:
                exact_army_id = None
                existing = None
                if decoded.identity_hash is not None:
                    existing = connection.execute(
                        "SELECT id FROM exact_armies WHERE identity_hash = %s",
                        (decoded.identity_hash,),
                    ).fetchone()
                if decoded.identity_hash is not None and existing is None:
                    troop_quantities: dict[str, int] = {}
                    for fact in decoded.home_troops:
                        troop_quantities[fact.typed_id] = (
                            troop_quantities.get(fact.typed_id, 0) + fact.quantity
                        )
                    spell_quantities: dict[str, int] = {}
                    for fact in decoded.spells:
                        spell_quantities[fact.typed_id] = (
                            spell_quantities.get(fact.typed_id, 0) + fact.quantity
                        )
                    inserted = connection.execute(
                        """
                        INSERT INTO exact_armies (identity_hash, decoder_version, catalog_version, catalog_hash, home_troops, spells, heroes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (identity_hash) DO UPDATE SET identity_hash = EXCLUDED.identity_hash
                        RETURNING id
                        """,
                        (
                            decoded.identity_hash,
                            decoded.decoder_version,
                            decoded.catalog_version,
                            decoded.catalog_hash,
                            Jsonb(sorted(troop_quantities.items())),
                            Jsonb(sorted(spell_quantities.items())),
                            Jsonb(
                                [
                                    {
                                        "hero": h.hero_typed_id,
                                        "pet": h.pet_typed_id,
                                        "equipment": list(h.equipment_typed_ids),
                                    }
                                    for h in decoded.heroes
                                ]
                            ),
                        ),
                    ).fetchone()
                    assert inserted is not None
                    exact_army_id = int(inserted[0])
                elif existing is not None:
                    exact_army_id = int(existing[0])
                active = connection.execute(
                    "SELECT id, evidence_id, raw_code, identity_hash, status FROM battle_army_decodes WHERE battle_id = %s AND perspective = %s AND decoder_version = %s AND catalog_version = %s AND is_active = true",
                    (
                        battle_id,
                        perspective,
                        decoded.decoder_version,
                        decoded.catalog_version,
                    ),
                ).fetchone()
                raw_cmp_equal = False
                if active is not None:
                    active_raw = active[2]
                    if (active_raw is None and raw_code is None) or (
                        active_raw is not None
                        and raw_code is not None
                        and _text_value(active_raw) == _text_value(raw_code)
                    ):
                        raw_cmp_equal = True
                    if (
                        int(active[1]) == int(evidence_id)
                        and raw_cmp_equal
                        and (
                            active[3] is None
                            if decoded.identity_hash is None
                            else _text_value(active[3]) == decoded.identity_hash
                        )
                        and _text_value(active[4]) == decoded.status
                    ):
                        continue
                    connection.execute(
                        "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                        (active[0],),
                    )
                    supersedes = int(active[0])
                else:
                    supersedes = None
                connection.execute(
                    """
                    INSERT INTO battle_army_decodes (battle_id, evidence_id, perspective, raw_code, decoder_version, catalog_version, catalog_hash, status, exact_army_id, identity_hash, home_troops, spells, home_spells, cc_spells, siege, cc_troops, heroes, raw_m, unresolved_components, is_active, supersedes_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s)
                    """,
                    (
                        battle_id,
                        evidence_id,
                        perspective,
                        raw_code,
                        decoded.decoder_version,
                        decoded.catalog_version,
                        decoded.catalog_hash,
                        decoded.status,
                        exact_army_id,
                        decoded.identity_hash,
                        Jsonb(
                            [
                                (f.typed_id, f.quantity, f.origin)
                                for f in decoded.home_troops
                            ]
                        ),
                        Jsonb(
                            [(f.typed_id, f.quantity, f.origin) for f in decoded.spells]
                        ),
                        Jsonb(
                            [
                                (f.typed_id, f.quantity, f.origin)
                                for f in decoded.home_spells_raw
                            ]
                        ),
                        Jsonb(
                            [
                                (f.typed_id, f.quantity, f.origin)
                                for f in decoded.cc_spells_raw
                            ]
                        ),
                        Jsonb(
                            [(f.typed_id, f.quantity, f.origin) for f in decoded.siege]
                        ),
                        Jsonb(
                            [
                                (f.typed_id, f.quantity, f.origin)
                                for f in decoded.cc_troops
                            ]
                        ),
                        Jsonb(
                            [
                                {
                                    "hero": h.hero_typed_id,
                                    "pet": h.pet_typed_id,
                                    "equipment": list(h.equipment_typed_ids),
                                    "raw_m": h.raw_m,
                                }
                                for h in decoded.heroes
                            ]
                        ),
                        Jsonb([h.raw_m for h in decoded.heroes if h.raw_m]),
                        Jsonb(
                            [
                                {
                                    "numeric_id": fact.numeric_id,
                                    "quantity": fact.quantity,
                                    "section": fact.section,
                                    "origin": fact.origin,
                                }
                                for fact in decoded.unknown
                            ]
                        ),
                        supersedes,
                    ),
                )
            else:
                failure: DecodeFailure = decoded  # type: ignore[assignment]
                active = connection.execute(
                    "SELECT id, evidence_id, raw_code, failure_category FROM battle_army_decodes WHERE battle_id = %s AND perspective = %s AND decoder_version = %s AND catalog_version = %s AND is_active = true",
                    (
                        battle_id,
                        perspective,
                        failure.decoder_version,
                        failure.catalog_version,
                    ),
                ).fetchone()
                raw_cmp_equal = False
                if active is not None:
                    active_raw = active[2]
                    if (active_raw is None and raw_code is None) or (
                        active_raw is not None
                        and raw_code is not None
                        and _text_value(active_raw) == _text_value(raw_code)
                    ):
                        raw_cmp_equal = True
                if (
                    active is not None
                    and int(active[1]) == int(evidence_id)
                    and raw_cmp_equal
                    and _text_value(active[3]) == failure.category
                ):
                    continue
                if active is not None:
                    connection.execute(
                        "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                        (active[0],),
                    )
                    supersedes = int(active[0])
                else:
                    supersedes = None
                connection.execute(
                    """
                    INSERT INTO battle_army_decodes (battle_id, evidence_id, perspective, raw_code, decoder_version, catalog_version, catalog_hash, status, failure_category, failure_detail, is_active, supersedes_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'failed', %s, %s, true, %s)
                    """,
                    (
                        battle_id,
                        evidence_id,
                        perspective,
                        raw_code,
                        failure.decoder_version,
                        failure.catalog_version,
                        failure.catalog_hash,
                        failure.category,
                        failure.detail,
                        supersedes,
                    ),
                )
        day_rows = connection.execute(
            "SELECT DISTINCT ranked_day_start FROM legend_battles WHERE id = ANY(%s::bigint[])",
            (battle_ids,),
        ).fetchall()
        for (day_start,) in day_rows:
            self._enqueue_army_analytics(connection, ranked_day_start=day_start)

    def _boundary_army_manifest_needs_correction(
        self, connection: Any, *, manifest_id: int
    ) -> bool:
        rows = connection.execute(
            """
            SELECT player_id, ranked_day_version_id, input_identity
            FROM boundary_publication_manifest_rows
            WHERE manifest_id = %s
            ORDER BY ordinal
            """,
            (manifest_id,),
        ).fetchall()
        for player_id, version_id, identity in rows:
            expected = {
                int(value)
                for value in (
                    identity.get("decode_ids", []) if isinstance(identity, dict) else []
                )
                if str(value).isdigit()
            }
            expected_battles = {
                int(value)
                for value in (
                    identity.get("battle_ids", []) if isinstance(identity, dict) else []
                )
                if str(value).isdigit()
            }
            actual: set[int] = set()
            if version_id is not None:
                daily = connection.execute(
                    """
                    SELECT battles
                    FROM api_player_daily_logs
                    WHERE player_id = %s AND ranked_day_version_id = %s
                    ORDER BY version DESC LIMIT 1
                    """,
                    (player_id, version_id),
                ).fetchone()
                for event in daily[0] if daily and isinstance(daily[0], list) else []:
                    if not isinstance(event, dict) or event.get("included") is False:
                        continue
                    battle_id = event.get("battle_id")
                    lens = event.get("lens")
                    if (
                        not str(battle_id).isdigit()
                        or int(battle_id) not in expected_battles
                        or lens not in {"offense", "defense"}
                    ):
                        continue
                    perspective = "attacker" if lens == "offense" else "defender"
                    decode = connection.execute(
                        """
                        SELECT id
                        FROM battle_army_decodes
                        WHERE battle_id = %s AND perspective = %s
                          AND is_active AND decoder_version = %s AND catalog_version = %s
                        """,
                        (int(battle_id), perspective, DECODER_VERSION, CATALOG_VERSION),
                    ).fetchone()
                    if decode is not None:
                        actual.add(int(decode[0]))
            if actual != expected:
                return True
        return False

    def _queue_boundary_army_correction(
        self,
        connection: Any,
        *,
        boundary_at: datetime,
        generation_id: int,
        defer_inheritance: bool = False,
    ) -> None:
        affected = ["army"]
        queued = connection.execute(
            """
            SELECT id
            FROM boundary_publication_corrections
            WHERE boundary_at = %s AND source_generation_id = %s
              AND state IN ('queued', 'pending_inputs')
            ORDER BY id DESC LIMIT 1
            FOR UPDATE
            """,
            (boundary_at, generation_id),
        ).fetchone()
        if queued is not None:
            connection.execute(
                """
                UPDATE boundary_publication_corrections
                SET affected_artifacts = ARRAY(
                        SELECT DISTINCT unnest(affected_artifacts || %s::text[])
                    )
                WHERE id = %s
                """,
                (affected, queued[0]),
            )
            connection.execute(
                "UPDATE boundary_publication_corrections SET pending_inputs = pending_inputs || %s::jsonb WHERE id = %s",
                (Jsonb([{"kind": "decode"}]), queued[0]),
            )
            return
        current = connection.execute(
            """
            SELECT snapshot_state, army_state, army_manifest_id, generation
            FROM boundary_publication_generations
            WHERE id = %s
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if (
            current is None
            or current[2] is None
            or _text_value(current[1]) == "superseded"
        ):
            return
        if (
            _text_value(current[0]) == "published"
            and _text_value(current[1]) == "published"
        ):
            connection.execute(
                """
                UPDATE boundary_publication_generations
                SET snapshot_state = 'superseded', army_state = 'superseded',
                    correction_state = 'finalized', updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (generation_id,),
            )
            player_ids = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT player_id
                    FROM boundary_publication_generation_members
                    WHERE generation_id = %s ORDER BY player_id
                    """,
                    (generation_id,),
                ).fetchall()
            ]
            new_id, _new_generation = self._create_boundary_generation(
                connection,
                boundary_at=boundary_at,
                sweep_id=int(
                    connection.execute(
                        "SELECT sweep_id FROM boundary_publication_generations WHERE id = %s",
                        (generation_id,),
                    ).fetchone()[0]
                ),
                player_ids=player_ids,
                generation=int(current[3]) + 1,
                supersedes_id=generation_id,
            )
            connection.execute(
                "UPDATE boundary_publication_generations SET affected_artifacts = %s WHERE id = %s",
                (affected, new_id),
            )
            if not defer_inheritance:
                connection.execute(
                    """
                    UPDATE boundary_publication_generations AS target
                    SET snapshot_state = 'published',
                        snapshot_id = source.snapshot_id,
                        snapshot_input_hash = source.snapshot_input_hash,
                        snapshot_manifest_id = source.snapshot_manifest_id,
                        snapshot_analytics_publication_id = source.snapshot_analytics_publication_id,
                        snapshot_coverage = source.snapshot_coverage
                    FROM boundary_publication_generations AS source
                    WHERE target.id = %s AND source.id = %s
                    """,
                    (new_id, generation_id),
                )
            connection.execute(
                """
                INSERT INTO boundary_publication_corrections
                    (boundary_at, source_generation_id, generation_id,
                     affected_artifacts, state, started_at)
                VALUES (%s, %s, %s, %s, 'active', clock_timestamp())
                """,
                (boundary_at, generation_id, new_id, affected),
            )
            return
        connection.execute(
            """
            INSERT INTO boundary_publication_corrections
                (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
            VALUES (%s, %s, %s, %s)
            """,
            (boundary_at, generation_id, affected, Jsonb([{"kind": "decode"}])),
        )

    def _enqueue_army_analytics(
        self, connection: Any, *, ranked_day_start: datetime
    ) -> None:
        ranked_day_start = ranked_day_start.astimezone(UTC)
        coordinator = None
        boundary_at = ranked_day_start + timedelta(days=1)
        if getattr(self, "_supports_coordinator_contract", False):
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"boundary-publication:{boundary_at.isoformat()}",),
            )
            coordinator = connection.execute(
                """
                SELECT id, generation, snapshot_state, army_state,
                       army_manifest_id
                FROM boundary_publication_generations
                WHERE boundary_at = %s
                  AND snapshot_state <> 'superseded'
                  AND army_state <> 'superseded'
                ORDER BY generation DESC
                LIMIT 1
                FOR UPDATE
                """,
                (boundary_at,),
            ).fetchone()
        if coordinator is not None:
            generation_id = int(coordinator[0])
            members = connection.execute(
                """
                SELECT player_id, ranked_day_version_id, snapshot_status
                FROM boundary_publication_generation_members
                WHERE generation_id = %s AND ranked_day_version_id IS NOT NULL
                FOR UPDATE
                """,
                (generation_id,),
            ).fetchall()
            for player_id, version_id, snapshot_status in members:
                army_status = self._boundary_army_status(
                    connection,
                    player_id=int(player_id),
                    ranked_day_version_id=int(version_id),
                    snapshot_status=_text_value(snapshot_status),
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generation_members
                    SET army_status = %s, updated_at = clock_timestamp()
                    WHERE generation_id = %s AND player_id = %s
                    """,
                    (army_status, generation_id, int(player_id)),
                )
            if (
                not getattr(self, "_disable_decode_corrections", False)
                and _text_value(coordinator[3]) in {"ready", "building", "published"}
                and coordinator[4] is not None
                and self._boundary_army_manifest_needs_correction(
                    connection, manifest_id=int(coordinator[4])
                )
            ):
                self._queue_boundary_army_correction(
                    connection,
                    boundary_at=boundary_at,
                    generation_id=generation_id,
                    defer_inheritance=True,
                )
            self._try_enqueue_boundary_artifacts(
                connection,
                boundary_at=boundary_at,
                generation_id=generation_id,
            )
            return
        # Decode-driven historical/test work without a reset coordinator
        # retains its existing per-day shape. Coordinated boundaries return
        # above and can only enqueue manifest-identified army work.
        completed = connection.execute(
            """
            SELECT id, official_season_id
            FROM ranked_day_versions
            WHERE ranked_day_start = %s
              AND state = 'Complete'
              AND coverage_complete
            ORDER BY id DESC
            LIMIT 1
            """,
            (ranked_day_start,),
        ).fetchone()
        if completed is None:
            return
        ranked_day_version_id = int(completed[0])
        season_id = _text_value(completed[1])
        latest_decode = connection.execute(
            """
            SELECT COALESCE(max(decode.id), 0)
            FROM legend_battles AS battle
            LEFT JOIN battle_army_decodes AS decode
              ON decode.battle_id = battle.id
             AND decode.is_active
             AND decode.decoder_version = %s
             AND decode.catalog_version = %s
            WHERE battle.ranked_day_start = %s
            """,
            (DECODER_VERSION, CATALOG_VERSION, ranked_day_start),
        ).fetchone()
        decode_generation = int(latest_decode[0]) if latest_decode else 0
        day_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        generation = f"{ranked_day_version_id}:{decode_generation}"
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                work_type, deduplication_key, input_json,
                processing_version, domain_rule_version,
                analytics_rule_version, due_at
            ) VALUES (
                'build_army_analytics', %s, %s, %s, %s, %s,
                clock_timestamp()
            )
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                f"build_army_analytics:{day_text}:{generation}:{ARMY_ANALYTICS_RULE_VERSION}:{DECODER_VERSION}:{CATALOG_VERSION}",
                Jsonb(
                    {
                        "ranked_day_start": day_text,
                        "official_season_id": season_id,
                    }
                ),
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ARMY_ANALYTICS_RULE_VERSION,
            ),
        )

    def complete_army_analytics(self, claim: Claim) -> None:
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                generation_input = claim.input_json.get("generation")
                generation_row = None
                season_id_row = None
                if generation_input is not None:
                    boundary_text = claim.input_json.get("boundary_at")
                    if boundary_text is None:
                        raise ValueError("army boundary is required")
                    boundary_at = datetime.fromisoformat(str(boundary_text)).astimezone(
                        UTC
                    )
                    generation_row = connection.execute(
                        """
                        SELECT id, generation, sweep_id, snapshot_state, army_state,
                               army_manifest_id, army_rule_version, target_at, target_rule
                        FROM boundary_publication_generations
                        WHERE boundary_at = %s AND generation = %s
                        FOR UPDATE
                        """,
                        (boundary_at, int(generation_input)),
                    ).fetchone()
                    if generation_row is None:
                        raise ValueError(
                            "boundary publication generation does not exist"
                        )
                    if _text_value(generation_row[4]) in {"superseded", "published"}:
                        self._finish_claim(
                            connection,
                            claim,
                            job,
                            state="complete",
                            outcome="stale_superseded",
                        )
                        return
                    if _text_value(generation_row[4]) not in {"ready", "building"}:
                        raise ValueError(
                            "boundary army generation is stale or superseded"
                        )
                    if _text_value(generation_row[3]) == "superseded":
                        raise ValueError(
                            "boundary publication generation is superseded"
                        )
                    manifest_id = (
                        int(generation_row[5])
                        if generation_row[5] is not None
                        else None
                    )
                    if (
                        manifest_id is None
                        or int(claim.input_json.get("manifest_id", 0)) != manifest_id
                    ):
                        raise ValueError(
                            "boundary army manifest identity does not match"
                        )
                    manifest_digest = connection.execute(
                        "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                        (manifest_id,),
                    ).fetchone()
                    if manifest_digest is None or _text_value(
                        manifest_digest[0]
                    ) != claim.input_json.get("manifest_digest"):
                        raise ValueError("boundary army manifest digest does not match")
                    if _text_value(generation_row[6]) != claim.analytics_rule_version:
                        raise ValueError("boundary army rule version does not match")
                    if _text_value(generation_row[8]) != "boundary-delay-v1":
                        raise ValueError("unsupported boundary target rule")
                    now_row = connection.execute("SELECT clock_timestamp()").fetchone()
                    assert now_row is not None
                    if now_row[0] < generation_row[7]:
                        raise ValueError(
                            "army publication target dependency is not ready"
                        )
                    manifest_digest_value = _text_value(manifest_digest[0])
                    connection.execute(
                        """
                        UPDATE boundary_publication_generations
                        SET army_state = 'building', updated_at = clock_timestamp()
                        WHERE id = %s AND army_state = 'ready'
                        """,
                        (generation_row[0],),
                    )
                    pending_members = connection.execute(
                        """
                        SELECT 1
                        FROM boundary_publication_generation_members
                        WHERE generation_id = %s AND army_status = 'pending'
                        LIMIT 1
                        """,
                        (generation_row[0],),
                    ).fetchone()
                    if pending_members is not None:
                        raise ValueError(
                            "boundary army publication dependency is not terminal"
                        )
                    ranked_day_str = (boundary_at - timedelta(days=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    season_id_row = connection.execute(
                        """
                        SELECT ranked.official_season_id
                        FROM boundary_publication_generation_members AS member
                        JOIN ranked_day_versions AS ranked
                          ON ranked.id = member.ranked_day_version_id
                        WHERE member.generation_id = %s
                        ORDER BY ranked.id DESC
                        LIMIT 1
                        """,
                        (generation_row[0],),
                    ).fetchone()
                    if season_id_row is None:
                        season_id, _season_day = self._season_metadata_for_ranked_day(
                            connection, boundary_at - timedelta(days=1)
                        )
                    else:
                        season_id = _text_value(season_id_row[0])
                else:
                    ranked_day_str = claim.input_json.get("ranked_day_start")
                    season_id = claim.input_json.get("official_season_id")
                if ranked_day_str is None or season_id is None:
                    raise ValueError(
                        "army analytics requires ranked-day and season inputs"
                    )
                manifest_members = None
                manifest_versions = None
                manifest_decodes = None
                manifest_daily_logs = None
                manifest_battles = None
                if generation_row is not None:
                    manifest_rows = connection.execute(
                        "SELECT player_id, ranked_day_version_id, input_identity->'decode_ids', input_identity FROM boundary_publication_manifest_rows WHERE manifest_id = %s ORDER BY ordinal",
                        (manifest_id,),
                    ).fetchall()
                    manifest_members = [int(row[0]) for row in manifest_rows]
                    manifest_versions = [
                        int(row[1]) for row in manifest_rows if row[1] is not None
                    ]
                    manifest_decodes = [
                        int(decode_id)
                        for row in manifest_rows
                        for decode_id in (row[2] or [])
                    ]
                    manifest_daily_logs = [
                        int(row[3]["daily_log_id"])
                        for row in manifest_rows
                        if isinstance(row[3], dict)
                        and row[3].get("daily_log_id") is not None
                    ]
                    manifest_battles = [
                        int(battle_id)
                        for row in manifest_rows
                        for battle_id in (
                            row[3].get("battle_ids", [])
                            if isinstance(row[3], dict)
                            else []
                        )
                    ]
                    manifest_evidence = [
                        int(evidence_id)
                        for row in manifest_rows
                        for evidence_id in (
                            row[3].get("evidence_ids", [])
                            if isinstance(row[3], dict)
                            else []
                        )
                    ]
                else:
                    manifest_evidence = None
                ranked_day_start = datetime.fromisoformat(
                    str(ranked_day_str)
                ).astimezone(UTC)
                season_id = self._ensure_army_day_dependency(
                    connection,
                    ranked_day_start,
                    ranked_version_ids=manifest_versions,
                    allow_empty=generation_row is not None,
                    official_season_id=str(season_id),
                )
                self._build_army_facts(
                    connection,
                    str(ranked_day_str),
                    member_ids=manifest_members,
                    ranked_version_ids=manifest_versions,
                    decode_ids=manifest_decodes,
                    evidence_ids=manifest_evidence,
                    daily_log_ids=manifest_daily_logs,
                    battle_ids=manifest_battles,
                )
                if generation_row is not None:
                    marker = connection.execute(
                        """
                        SELECT fact_input_hash
                        FROM army_analytics_completed_days
                        WHERE ranked_day_start = %s
                        """,
                        (ranked_day_start,),
                    ).fetchone()
                    army_hash = hashlib.sha256(
                        json.dumps(
                            {
                                "manifest_digest": manifest_digest_value,
                                "rule_versions": {
                                    "army_rule_version": _text_value(generation_row[6]),
                                    "decoder_version": DECODER_VERSION,
                                    "catalog_version": CATALOG_VERSION,
                                },
                                "output": {
                                    "day_fact_input_hash": (
                                        _text_value(marker[0])
                                        if marker is not None
                                        else None
                                    ),
                                },
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    army_identity = self._create_boundary_artifact_identity(
                        connection,
                        generation_id=int(generation_row[0]),
                        artifact_kind="army",
                        manifest_id=int(manifest_id),
                        input_hash=army_hash,
                        source_identity={
                            "ranked_day_start": str(ranked_day_str),
                            "decoder_version": DECODER_VERSION,
                            "catalog_version": CATALOG_VERSION,
                        },
                    )
                    published_army = connection.execute(
                        """
                        UPDATE boundary_publication_generations
                        SET army_state = 'published', army_input_hash = %s,
                            army_publication_id = %s,
                            army_coverage = jsonb_build_object(
                                'expected', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s),
                                'included', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s AND army_status IN ('complete','partial')),
                                'excluded', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s AND army_status NOT IN ('complete','partial')),
                                'terminal', (SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s AND army_status <> 'pending'),
                                'classifications', (SELECT COALESCE(jsonb_object_agg(army_status, count), '{}'::jsonb) FROM (SELECT army_status, count(*) FROM boundary_publication_generation_members WHERE generation_id = %s GROUP BY army_status) AS counts)
                            ),
                            updated_at = clock_timestamp()
                        WHERE id = %s AND army_state = 'building'
                          AND army_manifest_id = %s
                        RETURNING id
                        """,
                        (
                            army_hash,
                            army_identity,
                            generation_row[0],
                            generation_row[0],
                            generation_row[0],
                            generation_row[0],
                            generation_row[0],
                            generation_row[0],
                            manifest_id,
                        ),
                    ).fetchone()
                    if published_army is None:
                        raise ValueError("boundary army publication fence was lost")
                    self._maybe_emit_boundary_signal(connection, int(generation_row[0]))
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    def _ensure_army_day_dependency(
        self,
        connection: Any,
        ranked_day_start: datetime,
        *,
        ranked_version_ids: list[int] | None,
        allow_empty: bool,
        official_season_id: str,
    ) -> str:
        """Keep the completed-day gate without rebuilding legacy rollups."""
        ranked_day_start = ranked_day_start.astimezone(UTC)
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if ranked_day_start >= now or ranked_day_start + timedelta(days=1) > now:
            raise ValueError(
                "dependency_not_ready: army analytics day is not completed"
            )
        completed_filter = "AND state = 'Complete' AND coverage_complete"
        completed_params: tuple[Any, ...] = (ranked_day_start,)
        if ranked_version_ids is not None:
            completed_filter = "AND id = ANY(%s::bigint[])"
            completed_params = (ranked_day_start, ranked_version_ids)
        completed = connection.execute(
            f"""
            SELECT official_season_id
            FROM ranked_day_versions
            WHERE ranked_day_start = %s
              {completed_filter}
            ORDER BY version DESC
            LIMIT 1
            """,
            completed_params,
        ).fetchone()
        if completed is None:
            if not allow_empty:
                raise ValueError("dependency_not_ready: ranked day is not complete")
            return official_season_id
        return _text_value(completed[0])

    def _build_army_facts(
        self,
        connection: Any,
        ranked_day_str: str,
        *,
        member_ids: list[int] | None = None,
        ranked_version_ids: list[int] | None = None,
        decode_ids: list[int] | None = None,
        evidence_ids: list[int] | None = None,
        daily_log_ids: list[int] | None = None,
        battle_ids: list[int] | None = None,
    ) -> None:
        ranked_day_start = datetime.fromisoformat(ranked_day_str).astimezone(UTC)
        # Published daily logs own the canonical per-lens battle events;
        # ranked_day_versions contributes the battle-time starting trophies.
        version_filter = ""
        version_params: tuple[Any, ...] = ()
        daily_log_filter = ""
        daily_log_params: tuple[Any, ...] = ()
        ranked_state_filter = "AND rv.state = 'Complete' AND rv.coverage_complete"
        daily_state_filter = "AND d.state = 'Complete' AND d.coverage = 'complete'"
        if daily_log_ids is not None:
            daily_log_filter = "AND d.id = ANY(%s::bigint[])"
            daily_log_params = (daily_log_ids,)
            ranked_state_filter = ""
            daily_state_filter = ""
        if getattr(self, "_supports_coordinator_contract", False):
            version_filter = "AND (%s::bigint[] IS NULL OR d.ranked_day_version_id = ANY(%s::bigint[]))"
            version_params = (ranked_version_ids, ranked_version_ids)
        versions = connection.execute(
            f"""
            SELECT DISTINCT ON (d.player_id)
                   rv.id, d.player_id, d.battles, d.official_season_id,
                   d.season_day_number, rv.start_trophies
            FROM api_player_daily_logs AS d
            JOIN ranked_day_versions AS rv
              ON rv.player_id = d.player_id
             AND rv.ranked_day_start = d.ranked_day_start
             {ranked_state_filter}
            WHERE d.ranked_day_start = %s
              {daily_state_filter}
              AND (%s::bigint[] IS NULL OR d.player_id = ANY(%s::bigint[]))
              {version_filter}
              {daily_log_filter}
              AND (%s::bigint[] IS NULL OR rv.id = ANY(%s::bigint[]))
            ORDER BY d.player_id, d.version DESC, rv.version DESC
            """,
            (
                ranked_day_start,
                member_ids,
                member_ids,
                *version_params,
                *daily_log_params,
                ranked_version_ids,
                ranked_version_ids,
            ),
        ).fetchall()
        # Load every input relation once. The Python pass below only preserves
        # the existing event ordering and input-hash semantics; all writes are
        # bulk statements outside the per-event loop.
        streams: list[tuple[Any, ...]] = []
        pinned_battle_ids = set(battle_ids) if battle_ids is not None else None
        selected_battle_ids: set[int] = set()
        perspectives: set[str] = set()
        for version_id, player_id, battles, season_id, day_number, start_trophies in versions:
            if not isinstance(battles, list):
                continue
            events = [
                event
                for event in battles
                if isinstance(event, dict)
                and event.get("included") is not False
                and event.get("lens") in {"offense", "defense"}
                and str(event.get("battle_id", "")).isdigit()
                and (
                    pinned_battle_ids is None
                    or int(event["battle_id"]) in pinned_battle_ids
                )
            ]
            events.sort(
                key=lambda event: (
                    str(event.get("battle_timestamp", "")),
                    int(event["battle_id"]),
                )
            )
            streams.append(
                (version_id, player_id, season_id, day_number, start_trophies, events)
            )
            for event in events:
                selected_battle_ids.add(int(event["battle_id"]))
                perspectives.add(
                    "attacker" if event["lens"] == "offense" else "defender"
                )
        battle_id_values = sorted(selected_battle_ids)
        perspective_values = sorted(perspectives)
        lens_values = [
            "offense" if perspective == "attacker" else "defense"
            for perspective in perspective_values
        ]
        decode_filter = "AND is_active"
        decode_params: tuple[Any, ...] = ()
        if decode_ids is not None:
            decode_filter = "AND id = ANY(%s::bigint[])"
            decode_params = (decode_ids,)
        decode_rows = connection.execute(
            f"""
            SELECT battle_id, perspective, id, evidence_id, status, failure_category,
                   home_troops, spells, siege, cc_troops, heroes, unresolved_components
            FROM battle_army_decodes
            WHERE battle_id = ANY(%s::bigint[])
              AND perspective = ANY(%s::text[])
              AND decoder_version = %s AND catalog_version = %s
              {decode_filter}
            """,
            (
                battle_id_values,
                perspective_values,
                DECODER_VERSION,
                CATALOG_VERSION,
                *decode_params,
            ),
        ).fetchall()
        decodes = {
            (
                int(row[0]),
                "offense" if _text_value(row[1]) == "attacker" else "defense",
            ): row
            for row in decode_rows
        }
        evidence_rows = connection.execute(
            """
            SELECT p.battle_id, p.perspective, p.evidence_id,
                   b.disagreement_state
            FROM legend_battles AS b
            JOIN battle_perspectives AS p ON p.battle_id = b.id
            WHERE b.id = ANY(%s::bigint[])
              AND p.perspective = ANY(%s::text[])
              AND (%s::bigint[] IS NULL OR p.evidence_id = ANY(%s::bigint[]))
            """,
            (battle_id_values, perspective_values, evidence_ids, evidence_ids),
        ).fetchall()
        evidence = {
            (
                int(row[0]),
                "offense" if _text_value(row[1]) == "attacker" else "defense",
            ): row
            for row in evidence_rows
        }
        fact_rows_by_key = connection.execute(
            """
            SELECT fact.battle_id, fact.lens, max(fact.version),
                   current_fact.id, current_fact.input_hash
            FROM army_analytics_battle_facts AS fact
            LEFT JOIN army_analytics_battle_facts AS current_fact
              ON current_fact.battle_id = fact.battle_id
             AND current_fact.lens = fact.lens
             AND current_fact.is_current
            WHERE fact.battle_id = ANY(%s::bigint[])
              AND fact.lens = ANY(%s::text[])
            GROUP BY fact.battle_id, fact.lens, current_fact.id,
                     current_fact.input_hash
            """,
            (battle_id_values, lens_values),
        ).fetchall()
        fact_history = {
            (int(row[0]), _text_value(row[1])): {
                "latest_version": int(row[2]),
                "current": (
                    {
                        "id": int(row[3]),
                        "input_hash": _text_value(row[4]),
                    }
                    if row[3] is not None
                    else None
                ),
            }
            for row in fact_rows_by_key
        }
        active_keys: set[tuple[int, str]] = set()
        superseded_ids: list[int] = []
        fact_rows: list[dict[str, Any]] = []
        for version_id, player_id, season_id, day_number, start_trophies, events in streams:
            trophies = int(start_trophies) if start_trophies is not None else None
            for event in events:
                battle_id = int(event["battle_id"])
                lens = str(event["lens"])
                key = (battle_id, lens)
                evidence_row = evidence.get(key)
                if evidence_row is None:
                    continue
                decode = decodes.get(key)
                state = (
                    _text_value(decode[5])
                    if decode and _text_value(decode[4]) == "failed" and decode[5]
                    else _text_value(decode[4]) if decode else "decode_missing"
                )
                disagreement = _text_value(evidence_row[3]) == "disagreement"
                payload = {
                    "source_ranked_day_version_id": int(version_id),
                    "battle_id": battle_id,
                    "lens": lens,
                    "battle_time_trophies": trophies,
                    "event": event,
                    "decode_id": int(decode[2]) if decode else None,
                    "perspective_disagreement": disagreement,
                }
                input_hash = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                active_keys.add(key)
                history = fact_history.get(key)
                existing = history["current"] if history is not None else None
                if existing is not None and existing["input_hash"] == input_hash:
                    change = event.get("trophy_change")
                    if isinstance(change, bool) or not isinstance(change, int):
                        trophies = None
                    elif trophies is not None:
                        trophies += change
                    continue
                supersedes = existing["id"] if existing is not None else None
                if supersedes is not None:
                    superseded_ids.append(supersedes)
                fact_rows.append(
                    {
                        "battle_id": battle_id,
                        "evidence_id": int(evidence_row[2]),
                        "decode_id": int(decode[2]) if decode else None,
                        "source_ranked_day_version_id": int(version_id),
                        "official_season_id": _text_value(season_id),
                        "season_day_number": int(day_number),
                        "lens": lens,
                        "population_player_id": int(player_id),
                        "battle_time_trophies": trophies,
                        "stars": int(event.get("stars") or 0),
                        "destruction_percentage": int(
                            event.get("destruction_percentage") or 0
                        ),
                        "army_state": state,
                        "failure_reason": (
                            _text_value(decode[5]) if decode and decode[5] else None
                        ),
                        "home_troops": (decode[6] or []) if decode else [],
                        "spells": (decode[7] or []) if decode else [],
                        "siege": (decode[8] or []) if decode else [],
                        "cc_troops": (decode[9] or []) if decode else [],
                        "heroes": (decode[10] or []) if decode else [],
                        "unresolved_components": (
                            (decode[11] or []) if decode else []
                        ),
                        "perspective_disagreement": disagreement,
                        "input_hash": input_hash,
                        "version": (
                            history["latest_version"] + 1
                            if history is not None
                            else 1
                        ),
                        "supersedes_id": supersedes,
                    }
                )
                change = event.get("trophy_change")
                if isinstance(change, bool) or not isinstance(change, int):
                    trophies = None
                elif trophies is not None:
                    trophies += change
        if superseded_ids:
            connection.execute(
                "UPDATE army_analytics_battle_facts SET is_current=false WHERE id = ANY(%s::bigint[])",
                (superseded_ids,),
            )
        if fact_rows:
            connection.execute(
                """
                INSERT INTO army_analytics_battle_facts (
                    battle_id, evidence_id, decode_id, source_ranked_day_version_id,
                    ranked_day_start, official_season_id, season_day_number, lens,
                    population_player_id, battle_time_trophies, stars,
                    destruction_percentage, army_state, failure_reason, home_troops,
                    spells, siege, cc_troops, heroes, unresolved_components,
                    perspective_disagreement, input_hash, version, supersedes_id
                )
                SELECT row.battle_id, row.evidence_id, row.decode_id,
                       row.source_ranked_day_version_id, %s, row.official_season_id,
                       row.season_day_number, row.lens, row.population_player_id,
                       row.battle_time_trophies, row.stars, row.destruction_percentage,
                       row.army_state, row.failure_reason, row.home_troops,
                       row.spells, row.siege, row.cc_troops, row.heroes,
                       row.unresolved_components, row.perspective_disagreement,
                       row.input_hash, row.version, row.supersedes_id
                FROM jsonb_to_recordset(%s::jsonb) AS row(
                    battle_id bigint, evidence_id bigint, decode_id bigint,
                    source_ranked_day_version_id bigint, official_season_id text,
                    season_day_number integer, lens text, population_player_id bigint,
                    battle_time_trophies integer, stars integer,
                    destruction_percentage integer, army_state text,
                    failure_reason text, home_troops jsonb, spells jsonb,
                    siege jsonb, cc_troops jsonb, heroes jsonb,
                    unresolved_components jsonb, perspective_disagreement boolean,
                    input_hash text, version integer, supersedes_id bigint
                )
                """,
                (ranked_day_start, Jsonb(fact_rows)),
            )
        connection.execute(
            """
            UPDATE army_analytics_battle_facts AS fact
            SET is_current = false
            WHERE fact.ranked_day_start = %s AND fact.is_current
              AND NOT EXISTS (
                  SELECT 1
                  FROM unnest(%s::bigint[], %s::text[]) AS active(battle_id, lens)
                  WHERE active.battle_id = fact.battle_id
                    AND active.lens = fact.lens
              )
            """,
            (
                ranked_day_start,
                [battle_id for battle_id, _lens in active_keys],
                [lens for _battle_id, lens in active_keys],
            ),
        )
        # Durable per-day completion marker, atomic with the facts above.
        marker_filter = "AND state = 'Complete' AND coverage = 'complete'"
        marker_params: tuple[Any, ...] = (ranked_day_start,)
        if ranked_version_ids is not None:
            marker_filter = "AND ranked_day_version_id = ANY(%s::bigint[])"
            marker_params = (ranked_day_start, ranked_version_ids)
        marker_day = connection.execute(
            f"""
            SELECT DISTINCT official_season_id, season_day_number
            FROM api_player_daily_logs
            WHERE ranked_day_start = %s
              {marker_filter}
            LIMIT 1
            """,
            marker_params,
        ).fetchone()
        if marker_day is not None:
            marker_rows = connection.execute(
                """
                SELECT battle_id, lens, input_hash
                FROM army_analytics_battle_facts
                WHERE ranked_day_start = %s AND is_current
                ORDER BY battle_id, lens
                """,
                (ranked_day_start,),
            ).fetchall()
            marker_hash = hashlib.sha256(
                json.dumps(
                    [
                        [int(r[0]), _text_value(r[1]), _text_value(r[2])]
                        for r in marker_rows
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO army_analytics_completed_days (
                    ranked_day_start, official_season_id, season_day_number,
                    fact_input_hash
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (ranked_day_start) DO UPDATE SET
                    official_season_id = EXCLUDED.official_season_id,
                    season_day_number = EXCLUDED.season_day_number,
                    fact_input_hash = EXCLUDED.fact_input_hash,
                    completed_at = clock_timestamp()
                """,
                (
                    ranked_day_start,
                    _text_value(marker_day[0]),
                    int(marker_day[1]),
                    marker_hash,
                ),
            )

    def _season_metadata_for_ranked_day(
        self, connection: Any, ranked_day_start: datetime
    ) -> tuple[str, int]:
        ranked_day_start = ranked_day_start.astimezone(UTC)
        anchor = connection.execute(
            """
            SELECT current_league_season_id, previous_league_season_id,
                   current_start, previous_start
            FROM legend_season_anchors
            WHERE state = 'confirmed' AND anchor_rule_version = %s
            """,
            (SEASON_ANCHOR_RULE_VERSION,),
        ).fetchone()
        if anchor is None or ranked_day_start < anchor[3]:
            raise ValueError(
                "dependency_not_ready: confirmed season anchor is unavailable"
            )
        if ranked_day_start >= anchor[2]:
            season_id, season_start = _text_value(anchor[0]), anchor[2]
        else:
            season_id, season_start = _text_value(anchor[1]), anchor[3]
        season_day = (ranked_day_start - season_start).days + 1
        if not 1 <= season_day <= 28:
            raise ValueError(
                "dependency_not_ready: ranked day is outside confirmed season"
            )
        return season_id, season_day

    def _build_army_day(
        self,
        connection: Any,
        claim: Claim,
        ranked_day_str: str,
        *,
        member_ids: list[int] | None = None,
        ranked_version_ids: list[int] | None = None,
        decode_ids: list[int] | None = None,
        evidence_ids: list[int] | None = None,
        allow_empty: bool = False,
        official_season_id: str | None = None,
    ) -> None:
        from collections import defaultdict

        ranked_day_start = datetime.fromisoformat(str(ranked_day_str))
        if ranked_day_start.tzinfo is None:
            ranked_day_start = ranked_day_start.replace(tzinfo=UTC)
        ranked_day_start = ranked_day_start.astimezone(UTC)
        ranked_day_end = ranked_day_start + timedelta(days=1)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"army-day:{ranked_day_start.isoformat()}:{claim.analytics_rule_version}:{CATALOG_VERSION}:{DECODER_VERSION}",
            ),
        )
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if ranked_day_start >= now or ranked_day_end > now:
            raise ValueError(
                "dependency_not_ready: army analytics day is not completed"
            )
        # coverage gate: must have at least one Complete version or no pending collection
        decode_filter = "AND bad.is_active"
        decode_params: tuple[Any, ...] = ()
        if decode_ids is not None:
            decode_filter = "AND bad.id = ANY(%s::bigint[])"
            decode_params = (decode_ids,)
        completed_filter = "AND state = 'Complete' AND coverage_complete"
        completed_params: tuple[Any, ...] = (ranked_day_start,)
        lateral_version_filter = "AND state = 'Complete' AND coverage_complete"
        if ranked_version_ids is not None:
            completed_filter = ""
            lateral_version_filter = ""
            completed_filter = "AND id = ANY(%s::bigint[])"
            completed_params = (ranked_day_start, ranked_version_ids)
        completed = connection.execute(
            f"""
            SELECT official_season_id
            FROM ranked_day_versions
            WHERE ranked_day_start = %s
              {completed_filter}
            ORDER BY version DESC
            LIMIT 1
            """,
            completed_params,
        ).fetchone()
        if completed is None:
            if not allow_empty or official_season_id is None:
                raise ValueError("dependency_not_ready: ranked day is not complete")
        else:
            official_season_id = _text_value(completed[0])

        rows = connection.execute(
            f"""
            SELECT b.id, bad.id, be.stars, be.destruction_percentage,
                   bad.status, bad.failure_category, bad.home_troops,
                   bad.spells, bad.heroes, bad.siege, bad.cc_troops,
                   bad.home_spells, bad.cc_spells, rdv.start_trophies
            FROM legend_battles b
            JOIN battle_perspectives p ON p.battle_id=b.id AND p.perspective='attacker'
            JOIN battle_evidence be ON be.id=p.evidence_id
            LEFT JOIN battle_army_decodes bad ON bad.battle_id=b.id AND bad.perspective='attacker' AND bad.decoder_version=%s AND bad.catalog_version=%s {decode_filter}
            LEFT JOIN LATERAL (
                SELECT start_trophies
                FROM ranked_day_versions
                WHERE player_id = b.attacker_player_id
                  AND ranked_day_start = b.ranked_day_start
                  {lateral_version_filter}
                  AND (%s::bigint[] IS NULL OR ranked_day_versions.id = ANY(%s::bigint[]))
                ORDER BY version DESC
                LIMIT 1
            ) rdv ON true
            WHERE b.ranked_day_start=%s
              AND (%s::bigint[] IS NULL OR b.attacker_player_id = ANY(%s::bigint[]))
              AND (%s::bigint[] IS NULL OR be.id = ANY(%s::bigint[]))
            """,
            (
                DECODER_VERSION,
                CATALOG_VERSION,
                *decode_params,
                ranked_version_ids,
                ranked_version_ids,
                ranked_day_start,
                member_ids,
                member_ids,
                evidence_ids,
                evidence_ids,
            ),
        ).fetchall()

        all_battles: list[dict[str, Any]] = []
        cohorts: dict[int, list[dict[str, Any]]] = defaultdict(list)
        overall_decoded: list[dict[str, Any]] = []

        for r in rows:
            (
                bid,
                decode_id,
                stars,
                destr,
                status,
                fail_cat,
                home_troops,
                spells,
                heroes,
                siege,
                cc_troops,
                home_spells,
                cc_spells,
                start_trophies,
            ) = r
            rec: dict[str, Any] = {
                "battle_id": int(bid),
                "decode_id": int(decode_id) if decode_id is not None else None,
                "stars": int(stars) if stars is not None else 0,
                "destruction": int(destr) if destr is not None else 0,
                "status": _text_value(status) if status else None,
                "failure": _text_value(fail_cat) if fail_cat else None,
                "home_troops": home_troops
                if isinstance(home_troops, list)
                else (
                    json.loads(home_troops)
                    if isinstance(home_troops, str)
                    else home_troops or []
                ),
                "spells": spells
                if isinstance(spells, list)
                else (json.loads(spells) if isinstance(spells, str) else spells or []),
                "heroes": heroes
                if isinstance(heroes, list)
                else (json.loads(heroes) if isinstance(heroes, str) else heroes or []),
                "siege": siege
                if isinstance(siege, list)
                else (json.loads(siege) if isinstance(siege, str) else siege or []),
                "cc_troops": cc_troops
                if isinstance(cc_troops, list)
                else (
                    json.loads(cc_troops)
                    if isinstance(cc_troops, str)
                    else cc_troops or []
                ),
                "home_spells": home_spells
                if isinstance(home_spells, list)
                else (
                    json.loads(home_spells)
                    if isinstance(home_spells, str)
                    else home_spells or []
                ),
                "cc_spells": cc_spells
                if isinstance(cc_spells, list)
                else (
                    json.loads(cc_spells)
                    if isinstance(cc_spells, str)
                    else cc_spells or []
                ),
                "trophies": int(start_trophies) if start_trophies is not None else None,
            }
            all_battles.append(rec)
            if rec["trophies"] is not None:
                cohorts[int(rec["trophies"])].append(rec)
            if rec["status"] in {"decoded", "partial"}:
                overall_decoded.append(rec)

        n_overall = len(overall_decoded)
        input_payload = {
            "ranked_day_start": ranked_day_start.isoformat(),
            "official_season_id": official_season_id,
            "decoder_version": DECODER_VERSION,
            "catalog_version": CATALOG_VERSION,
            "analytics_rule_version": claim.analytics_rule_version,
            "battles": sorted(all_battles, key=lambda battle: battle["battle_id"]),
        }
        input_hash = hashlib.sha256(
            json.dumps(input_payload, sort_keys=True).encode()
        ).hexdigest()

        # helper to upsert summary with version preservation
        def upsert_summary(
            exact_trophies: int,
            battles_in_scope: list[dict[str, Any]],
            decoded_in_scope: list[dict[str, Any]],
        ) -> int:
            scope_total = len(battles_in_scope)
            scope_n = len(decoded_in_scope)
            scope_excluded = scope_total - scope_n
            # breakdown for excluded in this scope
            scope_failures: dict[str, int] = defaultdict(int)
            for b in battles_in_scope:
                if b["status"] not in {"decoded", "partial"}:
                    scope_failures[b["failure"] or "unknown"] += 1
            result_payload = {
                "trophies": exact_trophies,
                "battles": sorted(
                    battles_in_scope, key=lambda battle: battle["battle_id"]
                ),
                "failures": dict(scope_failures),
            }
            result_hash = hashlib.sha256(
                json.dumps(result_payload, sort_keys=True).encode()
            ).hexdigest()
            existing = connection.execute(
                "SELECT id, version, result_hash, is_published FROM army_analytics_day_summaries WHERE ranked_day_start=%s AND exact_trophies=%s AND decoder_version=%s AND catalog_version=%s AND analytics_rule_version=%s ORDER BY version DESC LIMIT 1",
                (
                    ranked_day_start,
                    exact_trophies,
                    DECODER_VERSION,
                    CATALOG_VERSION,
                    claim.analytics_rule_version,
                ),
            ).fetchone()
            if (
                existing is not None
                and existing[3]
                and _text_value(existing[2]) == result_hash
            ):
                return int(existing[0])
            # supersede old
            if existing is not None:
                if existing[3]:
                    connection.execute(
                        "UPDATE army_analytics_day_summaries SET is_published=false WHERE id=%s",
                        (existing[0],),
                    )
                new_version = int(existing[1]) + 1
                supersedes = int(existing[0])
            else:
                new_version = 1
                supersedes = None
            # keep history: old breakdowns remain; they are not deleted
            summary_id = connection.execute(
                """
                INSERT INTO army_analytics_day_summaries (ranked_day_start, official_season_id, exact_trophies, total_attacks, sample_size, excluded_attacks, excluded_breakdown, decoder_version, catalog_version, catalog_hash, analytics_rule_version, result_hash, input_hash, version, is_published, supersedes_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s) RETURNING id
                """,
                (
                    ranked_day_start,
                    official_season_id,
                    exact_trophies,
                    scope_total,
                    scope_n,
                    scope_excluded,
                    Jsonb(dict(scope_failures)),
                    DECODER_VERSION,
                    CATALOG_VERSION,
                    CATALOG_HASH,
                    claim.analytics_rule_version,
                    result_hash,
                    input_hash,
                    new_version,
                    supersedes,
                ),
            ).fetchone()[0]
            return int(summary_id)

        # Overall summary sentinel -1
        overall_id = upsert_summary(-1, all_battles, overall_decoded)
        self._insert_army_breakdowns(
            connection,
            summary_kind="day",
            summary_id=overall_id,
            battles=overall_decoded,
            n=n_overall,
        )

        # per-trophy cohorts (only known trophies)
        for trophies, all_for_trophy in cohorts.items():
            decoded_for_trophy = [
                b for b in all_for_trophy if b["status"] in {"decoded", "partial"}
            ]
            cid = upsert_summary(trophies, all_for_trophy, decoded_for_trophy)
            self._insert_army_breakdowns(
                connection,
                summary_kind="day",
                summary_id=cid,
                battles=decoded_for_trophy,
                n=len(decoded_for_trophy),
            )
        connection.execute(
            """
            UPDATE army_analytics_day_summaries
            SET is_published = false
            WHERE ranked_day_start = %s
              AND decoder_version = %s
              AND catalog_version = %s
              AND analytics_rule_version = %s
              AND is_published
              AND NOT (exact_trophies = ANY(%s::integer[]))
            """,
            (
                ranked_day_start,
                DECODER_VERSION,
                CATALOG_VERSION,
                claim.analytics_rule_version,
                [-1, *cohorts],
            ),
        )

    def _build_army_season(
        self,
        connection: Any,
        claim: Claim,
        season_id: str,
        *,
        ranked_version_ids: list[int] | None = None,
        daily_log_ids: list[int] | None = None,
        decode_ids: list[int] | None = None,
        evidence_ids: list[int] | None = None,
        battle_ids: list[int] | None = None,
    ) -> None:
        from collections import defaultdict

        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"army-season:{season_id}:{claim.analytics_rule_version}:{CATALOG_VERSION}:{DECODER_VERSION}",
            ),
        )
        # Coordinated season inputs are pinned in the army manifest. Direct
        # historical work retains the live completed-day selector.
        if ranked_version_ids is not None:
            day_rows = connection.execute(
                """
                SELECT DISTINCT ranked_day_start
                FROM ranked_day_versions
                WHERE id = ANY(%s::bigint[])
                ORDER BY ranked_day_start
                """,
                (ranked_version_ids,),
            ).fetchall()
        else:
            day_rows = connection.execute(
                "SELECT DISTINCT ranked_day_start FROM ranked_day_versions WHERE official_season_id=%s AND state='Complete' AND coverage_complete=true ORDER BY ranked_day_start",
                (season_id,),
            ).fetchall()
        if not day_rows and ranked_version_ids is None:
            season_start_row = connection.execute(
                "SELECT current_start FROM legend_season_anchors WHERE current_league_season_id=%s",
                (season_id,),
            ).fetchone()
            if season_start_row and season_start_row[0]:
                season_start = season_start_row[0]
                day_rows = connection.execute(
                    """
                    SELECT DISTINCT ranked_day_start
                    FROM ranked_day_versions
                    WHERE ranked_day_start >= %s
                      AND ranked_day_start < %s + interval '28 days'
                      AND state = 'Complete'
                      AND coverage_complete
                    ORDER BY ranked_day_start
                    """,
                    (season_start, season_start),
                ).fetchall()
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        for (d,) in day_rows:
            if d + timedelta(days=1) > now:
                raise ValueError(
                    "dependency_not_ready: army season not completed active day"
                )
        if not day_rows:
            # no completed days, nothing to publish
            return
        # Fetch only the rows pinned by a coordinator manifest. Direct
        # historical work retains the live completed-day selectors.
        season_decode_filter = "AND bad.is_active"
        season_decode_params: tuple[Any, ...] = ()
        season_ranked_state_filter = "AND state = 'Complete' AND coverage_complete"
        season_version_filter = ""
        season_version_params: tuple[Any, ...] = ()
        season_battle_filter = ""
        season_battle_params: tuple[Any, ...] = ()
        season_evidence_filter = ""
        season_evidence_params: tuple[Any, ...] = ()
        if decode_ids is not None:
            season_decode_filter = "AND bad.id = ANY(%s::bigint[])"
            season_decode_params = (decode_ids,)
        if ranked_version_ids is not None:
            season_version_filter = "AND ranked_day_versions.id = ANY(%s::bigint[])"
            season_version_params = (ranked_version_ids,)
            season_ranked_state_filter = ""
        if battle_ids is not None:
            season_battle_filter = "AND b.id = ANY(%s::bigint[])"
            season_battle_params = (battle_ids,)
        if evidence_ids is not None:
            season_evidence_filter = "AND be.id = ANY(%s::bigint[])"
            season_evidence_params = (evidence_ids,)
        rows = connection.execute(
            f"""
            SELECT b.id, bad.id, be.stars, be.destruction_percentage,
                   bad.status, bad.failure_category, bad.home_troops,
                   bad.spells, bad.heroes, bad.siege, bad.cc_troops,
                   bad.home_spells, bad.cc_spells, rdv.start_trophies
            FROM legend_battles b
            JOIN battle_perspectives p ON p.battle_id=b.id AND p.perspective='attacker'
            JOIN battle_evidence be ON be.id=p.evidence_id
            LEFT JOIN battle_army_decodes bad ON bad.battle_id=b.id AND bad.perspective='attacker' AND bad.decoder_version=%s AND bad.catalog_version=%s {season_decode_filter}
            LEFT JOIN LATERAL (
                SELECT start_trophies
                FROM ranked_day_versions
                WHERE player_id = b.attacker_player_id
                  AND ranked_day_start = b.ranked_day_start
                  {season_ranked_state_filter}
                  {season_version_filter}
                ORDER BY version DESC
                LIMIT 1
            ) rdv ON true
            WHERE b.ranked_day_start = ANY(%s::timestamptz[])
              {season_battle_filter}
              {season_evidence_filter}
            """,
            (
                DECODER_VERSION,
                CATALOG_VERSION,
                *season_decode_params,
                *season_version_params,
                [r[0] for r in day_rows],
                *season_battle_params,
                *season_evidence_params,
            ),
        ).fetchall()

        all_battles: list[dict[str, Any]] = []
        cohorts: dict[int, list[dict[str, Any]]] = defaultdict(list)
        overall_decoded: list[dict[str, Any]] = []
        for r in rows:
            (
                bid,
                decode_id,
                stars,
                destr,
                status,
                fail_cat,
                home_troops,
                spells,
                heroes,
                siege,
                cc_troops,
                home_spells,
                cc_spells,
                start_trophies,
            ) = r
            rec: dict[str, Any] = {
                "battle_id": int(bid),
                "decode_id": int(decode_id) if decode_id is not None else None,
                "stars": int(stars) if stars is not None else 0,
                "destruction": int(destr) if destr is not None else 0,
                "status": _text_value(status) if status else None,
                "failure": _text_value(fail_cat) if fail_cat else None,
                "home_troops": home_troops
                if isinstance(home_troops, list)
                else (
                    json.loads(home_troops)
                    if isinstance(home_troops, str)
                    else home_troops or []
                ),
                "spells": spells
                if isinstance(spells, list)
                else (json.loads(spells) if isinstance(spells, str) else spells or []),
                "heroes": heroes
                if isinstance(heroes, list)
                else (json.loads(heroes) if isinstance(heroes, str) else heroes or []),
                "siege": siege
                if isinstance(siege, list)
                else (json.loads(siege) if isinstance(siege, str) else siege or []),
                "cc_troops": cc_troops
                if isinstance(cc_troops, list)
                else (
                    json.loads(cc_troops)
                    if isinstance(cc_troops, str)
                    else cc_troops or []
                ),
                "home_spells": home_spells
                if isinstance(home_spells, list)
                else (
                    json.loads(home_spells)
                    if isinstance(home_spells, str)
                    else home_spells or []
                ),
                "cc_spells": cc_spells
                if isinstance(cc_spells, list)
                else (
                    json.loads(cc_spells)
                    if isinstance(cc_spells, str)
                    else cc_spells or []
                ),
                "trophies": int(start_trophies) if start_trophies is not None else None,
            }
            all_battles.append(rec)
            if rec["trophies"] is not None:
                cohorts[int(rec["trophies"])].append(rec)
            if rec["status"] in {"decoded", "partial"}:
                overall_decoded.append(rec)

        def upsert_season_summary(
            exact_trophies: int,
            battles_in_scope: list[dict[str, Any]],
            decoded_in_scope: list[dict[str, Any]],
        ) -> int:
            scope_total = len(battles_in_scope)
            scope_n = len(decoded_in_scope)
            scope_failures: dict[str, int] = defaultdict(int)
            for b in battles_in_scope:
                if b["status"] not in {"decoded", "partial"}:
                    scope_failures[b["failure"] or "unknown"] += 1
            result_payload = {
                "trophies": exact_trophies,
                "battles": sorted(
                    battles_in_scope, key=lambda battle: battle["battle_id"]
                ),
                "failures": dict(scope_failures),
            }
            result_hash = hashlib.sha256(
                json.dumps(result_payload, sort_keys=True).encode()
            ).hexdigest()
            input_payload = {
                "season": season_id,
                "trophies": exact_trophies,
                "decoder": DECODER_VERSION,
                "catalog": CATALOG_VERSION,
                "battles": sorted(
                    battles_in_scope, key=lambda battle: battle["battle_id"]
                ),
            }
            input_hash = hashlib.sha256(
                json.dumps(input_payload, sort_keys=True).encode()
            ).hexdigest()
            existing = connection.execute(
                "SELECT id, version, result_hash, is_published FROM army_analytics_season_summaries WHERE official_season_id=%s AND exact_trophies=%s AND decoder_version=%s AND catalog_version=%s AND analytics_rule_version=%s ORDER BY version DESC LIMIT 1",
                (
                    season_id,
                    exact_trophies,
                    DECODER_VERSION,
                    CATALOG_VERSION,
                    claim.analytics_rule_version,
                ),
            ).fetchone()
            if (
                existing is not None
                and existing[3]
                and _text_value(existing[2]) == result_hash
            ):
                return int(existing[0])
            if existing is not None:
                if existing[3]:
                    connection.execute(
                        "UPDATE army_analytics_season_summaries SET is_published=false WHERE id=%s",
                        (existing[0],),
                    )
                new_version = int(existing[1]) + 1
                supersedes = int(existing[0])
            else:
                new_version = 1
                supersedes = None
            summary_id = connection.execute(
                """
                INSERT INTO army_analytics_season_summaries (official_season_id, exact_trophies, total_attacks, sample_size, excluded_attacks, excluded_breakdown, decoder_version, catalog_version, catalog_hash, analytics_rule_version, result_hash, input_hash, version, is_published, supersedes_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s) RETURNING id
                """,
                (
                    season_id,
                    exact_trophies,
                    scope_total,
                    scope_n,
                    scope_total - scope_n,
                    Jsonb(dict(scope_failures)),
                    DECODER_VERSION,
                    CATALOG_VERSION,
                    CATALOG_HASH,
                    claim.analytics_rule_version,
                    result_hash,
                    input_hash,
                    new_version,
                    supersedes,
                ),
            ).fetchone()[0]
            return int(summary_id)

        overall_id = upsert_season_summary(-1, all_battles, overall_decoded)
        self._insert_army_breakdowns(
            connection,
            summary_kind="season",
            summary_id=overall_id,
            battles=overall_decoded,
            n=len(overall_decoded),
        )
        for trophies, all_for_trophy in cohorts.items():
            decoded_for_trophy = [
                b for b in all_for_trophy if b["status"] in {"decoded", "partial"}
            ]
            sid = upsert_season_summary(trophies, all_for_trophy, decoded_for_trophy)
            self._insert_army_breakdowns(
                connection,
                summary_kind="season",
                summary_id=sid,
                battles=decoded_for_trophy,
                n=len(decoded_for_trophy),
            )
        connection.execute(
            """
            UPDATE army_analytics_season_summaries
            SET is_published = false
            WHERE official_season_id = %s
              AND decoder_version = %s
              AND catalog_version = %s
              AND analytics_rule_version = %s
              AND is_published
              AND NOT (exact_trophies = ANY(%s::integer[]))
            """,
            (
                season_id,
                DECODER_VERSION,
                CATALOG_VERSION,
                claim.analytics_rule_version,
                [-1, *cohorts],
            ),
        )

    def _insert_army_breakdowns(
        self,
        connection: Any,
        *,
        summary_kind: str,
        summary_id: int,
        battles: list[dict[str, Any]],
        n: int,
    ) -> None:
        from collections import Counter, defaultdict

        if n == 0:
            return
        # clear previous breakdowns for this summary (if reusing same summary id via upsert with same hash we already returned, so this is new summary)
        # For new summary we have no breakdowns yet; but if we superseded, old breakdowns remain with old summary
        # Ensure we delete any stray breakdowns for this summary id (idempotent)
        connection.execute(
            "DELETE FROM army_analytics_breakdowns WHERE summary_kind=%s AND summary_id=%s",
            (summary_kind, summary_id),
        )

        def star_stats(
            matching: list[dict[str, Any]],
        ) -> tuple[
            dict[str, int], dict[str, float], float | None, float | None, float | None
        ]:
            cnt = Counter()
            total_dest = 0
            three = 0
            for b in matching:
                cnt[int(b["stars"])] += 1
                total_dest += int(b["destruction"])
                if int(b["stars"]) == 3:
                    three += 1
            star_counts = {str(k): cnt.get(k, 0) for k in range(4)}
            star_rates = {
                str(k): (cnt.get(k, 0) / len(matching) if matching else 0)
                for k in range(4)
            }
            avg_dest = (total_dest / len(matching)) if matching else None
            three_star_rate = (three / len(matching)) if matching else None
            avg_stars = (
                sum(stars * count for stars, count in cnt.items()) / len(matching)
                if matching
                else None
            )
            return star_counts, star_rates, avg_dest, three_star_rate, avg_stars

        troop_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        spell_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        hero_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        pet_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        equip_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        equip_for_hero: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        siege_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        cc_troop_to_battles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        hero_pet: dict[str, list[dict[str, Any]]] = defaultdict(list)
        hero_equip: dict[str, list[dict[str, Any]]] = defaultdict(list)
        cc_composition: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for b in battles:
            seen_troops: set[str] = set()
            for t in b["home_troops"] or []:
                if isinstance(t, (list, tuple)) and len(t) >= 1:
                    tid = str(t[0])
                elif isinstance(t, dict):
                    tid = str(t.get("typed_id") or t.get(0))
                else:
                    continue
                if tid not in seen_troops:
                    troop_to_battles[tid].append(b)
                    seen_troops.add(tid)
            seen_spells: set[str] = set()
            for s in b["spells"] or []:
                if isinstance(s, (list, tuple)) and len(s) >= 1:
                    tid = str(s[0])
                elif isinstance(s, dict):
                    tid = str(s.get("typed_id"))
                else:
                    continue
                if tid not in seen_spells:
                    spell_to_battles[tid].append(b)
                    seen_spells.add(tid)
            for h in b["heroes"] or []:
                if isinstance(h, dict):
                    hid = str(h.get("hero") or h.get("hero_typed_id") or "")
                    if not hid:
                        continue
                    pet = h.get("pet")
                    equips = h.get("equipment") or []
                else:
                    continue
                hero_to_battles[hid].append(b)
                if pet:
                    pet_to_battles[str(pet)].append(b)
                    hero_pet[f"{hid}+{pet}"].append(b)
                for eq in equips or []:
                    equip_to_battles[str(eq)].append(b)
                    equip_for_hero[(hid, str(eq))].append(b)
                if isinstance(equips, list) and len(equips) == 2:
                    key = f"{hid}+{','.join(sorted([str(x) for x in equips]))}"
                    hero_equip[key].append(b)
            for s in b["siege"] or []:
                if isinstance(s, (list, tuple)) and len(s) >= 1:
                    tid = str(s[0])
                elif isinstance(s, dict):
                    tid = str(s.get("typed_id"))
                else:
                    continue
                siege_to_battles[tid].append(b)
            seen_cc: set[str] = set()
            for ct in b["cc_troops"] or []:
                if isinstance(ct, (list, tuple)) and len(ct) >= 1:
                    tid = str(ct[0])
                elif isinstance(ct, dict):
                    tid = str(ct.get("typed_id"))
                else:
                    continue
                if tid not in seen_cc:
                    cc_troop_to_battles[tid].append(b)
                    seen_cc.add(tid)
            comp_list: list[tuple[str, int]] = []
            for ct in b["cc_troops"] or []:
                if isinstance(ct, (list, tuple)) and len(ct) >= 2:
                    try:
                        comp_list.append((str(ct[0]), int(ct[1])))
                    except (ValueError, TypeError):
                        continue
                elif isinstance(ct, dict):
                    try:
                        comp_list.append(
                            (str(ct.get("typed_id")), int(ct.get("quantity") or 1))
                        )
                    except (ValueError, TypeError):
                        continue
            comp_sorted = tuple(sorted(comp_list))
            if comp_sorted:
                key = json.dumps(comp_sorted, separators=(",", ":"))
                cc_composition[key].append(b)

        def insert(
            category: str,
            typed_id: str | None,
            hero_typed_id: str | None,
            combination_key: str | None,
            matching: list[dict[str, Any]],
        ) -> None:
            usage = len(matching)
            if category == "equipment_for_hero" and hero_typed_id:
                hero_cnt = len(hero_to_battles.get(hero_typed_id, []))
                usage_rate = (usage / hero_cnt) if hero_cnt else None
            else:
                usage_rate = (usage / n) if n else None
            star_counts, star_rates, avg_dest, three_star_rate, avg_stars = star_stats(
                matching
            )
            connection.execute(
                """
                INSERT INTO army_analytics_breakdowns (summary_kind, summary_id, category, typed_id, hero_typed_id, combination_key, usage_count, usage_rate, star_counts, star_rates, avg_destruction, three_star_rate, avg_stars, usage_denominator, evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)
                ON CONFLICT (summary_kind, summary_id, category, COALESCE(typed_id,''), COALESCE(hero_typed_id,''), COALESCE(combination_key,'')) DO UPDATE SET usage_count=EXCLUDED.usage_count, usage_rate=EXCLUDED.usage_rate, star_counts=EXCLUDED.star_counts, star_rates=EXCLUDED.star_rates, avg_destruction=EXCLUDED.avg_destruction, three_star_rate=EXCLUDED.three_star_rate, avg_stars=EXCLUDED.avg_stars, usage_denominator=EXCLUDED.usage_denominator
                """,
                (
                    summary_kind,
                    summary_id,
                    category,
                    typed_id,
                    hero_typed_id,
                    combination_key,
                    usage,
                    usage_rate,
                    Jsonb(star_counts),
                    Jsonb(star_rates),
                    avg_dest,
                    three_star_rate,
                    avg_stars,
                    hero_cnt
                    if category == "equipment_for_hero" and hero_typed_id
                    else n,
                ),
            )

        for tid, lst in troop_to_battles.items():
            insert("home_troop", tid, None, None, lst)
        for tid, lst in spell_to_battles.items():
            insert("spell", tid, None, None, lst)
        for tid, lst in hero_to_battles.items():
            insert("hero", tid, None, None, lst)
        for tid, lst in pet_to_battles.items():
            insert("pet", tid, None, None, lst)
        for tid, lst in equip_to_battles.items():
            insert("equipment", tid, None, None, lst)
        for (hero, eq), lst in equip_for_hero.items():
            insert("equipment_for_hero", eq, hero, None, lst)
        for tid, lst in siege_to_battles.items():
            insert("siege", tid, None, None, lst)
        for tid, lst in cc_troop_to_battles.items():
            insert("cc_troop", tid, None, None, lst)
        for key, lst in hero_pet.items():
            insert("hero_pet", None, None, key, lst)
        for key, lst in hero_equip.items():
            insert("hero_equipment", None, None, key, lst)
        for key, lst in cc_composition.items():
            insert("cc_composition", None, None, key, lst)

    def complete_army_redecode(self, claim: Claim) -> None:
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"army-redecode:{claim.job_id}",),
                )
                battle_ids: list[int] = []
                bid = claim.input_json.get("battle_id")
                if bid is not None:
                    battle_ids.append(int(bid))
                bids = claim.input_json.get("battle_ids")
                if isinstance(bids, list) and bids:
                    for x in bids[:100]:
                        try:
                            battle_ids.append(int(x))
                        except (TypeError, ValueError) as e:
                            raise ValueError(f"battle_ids must be integers: {e}") from e
                if not battle_ids:
                    raise ValueError("redecode requires battle_id or battle_ids")
                if len(battle_ids) > 100:
                    raise ValueError("redecode batch limited to 100")
                self._upsert_army_decodes(connection, battle_ids)
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    @staticmethod
    def _record_parsed_payload(
        connection: Any,
        *,
        endpoint: str,
        response_hash: str,
        parser_version: str,
        schema_version: str,
        parse_outcome: str,
        parsed_json: Any,
    ) -> None:
        connection.execute(
            """
            INSERT INTO parsed_source_payloads (
                endpoint, response_hash, parser_version, schema_version,
                parse_outcome, parsed_json
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (endpoint, response_hash, parser_version) DO UPDATE SET
                parse_outcome = EXCLUDED.parse_outcome,
                parsed_json = EXCLUDED.parsed_json
            """,
            (
                endpoint,
                response_hash,
                parser_version,
                schema_version,
                parse_outcome,
                Jsonb(parsed_json),
            ),
        )

    @staticmethod
    def _record_processing_outcome(
        connection: Any,
        claim: Claim,
        *,
        outcome: str,
        failure_category: str | None = None,
    ) -> None:
        observation_id, http_status, response_hash, observed_at, endpoint, _schema = (
            Database._observation_source(claim)
        )
        connection.execute(
            """
            INSERT INTO observation_processing_outcomes (
                observation_id, parser_version, processing_version, endpoint,
                response_hash, source_http_status, source_observed_at,
                outcome, failure_category
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (observation_id, parser_version, processing_version)
            DO UPDATE SET
                outcome = EXCLUDED.outcome,
                failure_category = EXCLUDED.failure_category
            """,
            (
                observation_id,
                claim.parser_version,
                claim.processing_version,
                endpoint,
                response_hash,
                http_status,
                observed_at,
                outcome,
                failure_category,
            ),
        )

    @staticmethod
    def _failure_outcome(category: str) -> str:
        if category.startswith("archive_") or category == "checksum_mismatch":
            return "integrity_failure"
        if category.startswith("unsupported_") or category.endswith("_schema"):
            return "unsupported"
        return "malformed"

    @staticmethod
    def _record_official_failed_attempt(
        connection: Any,
        claim: Claim,
        *,
        outcome: str,
        category: str,
    ) -> None:
        observation_id, _status, _hash, observed_at, _endpoint, _schema = (
            Database._observation_source(claim)
        )
        connection.execute(
            """
            INSERT INTO official_top200_attempts (
                observation_id, parser_version, outcome, failure_reasons,
                observed_at, season_provenance
            ) VALUES (%s, %s, %s, %s, %s, 'not_supplied')
            ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                outcome = EXCLUDED.outcome,
                failure_reasons = EXCLUDED.failure_reasons
            """,
            (
                observation_id,
                claim.parser_version,
                outcome,
                Jsonb([category]),
                observed_at,
            ),
        )

    @staticmethod
    def _record_season_anchor(
        connection: Any,
        profile_version_id: int,
        profile: ParsedProfile,
    ) -> str:
        outcome = "not_applicable"
        failure_reason: str | None = None
        anchor = None
        if profile.eligibility_state == "eligible":
            try:
                if (
                    profile.current_league_season_id is None
                    or profile.previous_league_season_id is None
                ):
                    raise DomainRuleError(
                        "invalid_season_anchor", "profile season values are missing"
                    )
                anchor = validate_season_anchor(
                    profile.current_league_season_id,
                    profile.previous_league_season_id,
                )
                outcome = "accepted"
            except DomainRuleError as error:
                outcome = "conflict"
                failure_reason = error.category

        if anchor is not None:

            def read_current(*, for_update: bool) -> Any:
                lock_clause = "FOR UPDATE OF a" if for_update else ""
                return connection.execute(
                    f"""
                    SELECT a.id, a.current_league_season_id,
                           a.previous_league_season_id, a.current_start,
                           v.observed_at
                    FROM legend_season_anchors AS a
                    JOIN player_profile_versions AS v
                      ON v.id = a.source_profile_version_id
                    WHERE a.state = 'confirmed'
                      AND a.anchor_rule_version = %s
                    {lock_clause}
                    """,
                    (SEASON_ANCHOR_RULE_VERSION,),
                ).fetchone()

            def matches_or_is_not_newer(current: Any) -> bool:
                return current is not None and (
                    (
                        _text_value(current[1]) == anchor.current_id
                        and _text_value(current[2]) == anchor.previous_id
                    )
                    or profile.observed_at <= current[4]
                )

            current = read_current(for_update=False)
            if matches_or_is_not_newer(current):
                pass
            else:
                # A possible transition must re-read under the row lock. The
                # unlocked read is only an optimization for the common no-op
                # path and is never used to advance confirmed state.
                current = read_current(for_update=True)

            inserted_initial_anchor = False
            if current is None:
                inserted = connection.execute(
                    """
                    INSERT INTO legend_season_anchors (
                        current_league_season_id, previous_league_season_id,
                        current_start, previous_start, anchor_rule_version,
                        source_profile_version_id, state
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        anchor.current_id,
                        anchor.previous_id,
                        anchor.current_start,
                        anchor.previous_start,
                        SEASON_ANCHOR_RULE_VERSION,
                        profile_version_id,
                    ),
                ).fetchone()
                inserted_initial_anchor = inserted is not None
                if not inserted_initial_anchor:
                    current = read_current(for_update=True)
                    assert current is not None
            if inserted_initial_anchor or matches_or_is_not_newer(current):
                pass
            elif anchor.current_start > current[3]:
                connection.execute(
                    "UPDATE legend_season_anchors SET state = 'superseded' WHERE id = %s",
                    (current[0],),
                )
                connection.execute(
                    """
                    INSERT INTO legend_season_anchors (
                        current_league_season_id, previous_league_season_id,
                        current_start, previous_start, anchor_rule_version,
                        source_profile_version_id, state
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
                    ON CONFLICT (current_league_season_id, anchor_rule_version)
                    DO UPDATE SET
                        source_profile_version_id = EXCLUDED.source_profile_version_id,
                        state = 'confirmed',
                        confirmed_at = clock_timestamp()
                    """,
                    (
                        anchor.current_id,
                        anchor.previous_id,
                        anchor.current_start,
                        anchor.previous_start,
                        SEASON_ANCHOR_RULE_VERSION,
                        profile_version_id,
                    ),
                )
            else:
                outcome = "conflict"
                failure_reason = "season_anchor_disagreement"

        connection.execute(
            """
            INSERT INTO season_anchor_evidence (
                profile_version_id, current_league_season_id,
                previous_league_season_id, current_start, previous_start,
                anchor_rule_version, outcome, failure_reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (profile_version_id) DO UPDATE SET
                outcome = EXCLUDED.outcome,
                failure_reason = EXCLUDED.failure_reason
            """,
            (
                profile_version_id,
                profile.current_league_season_id,
                profile.previous_league_season_id,
                anchor.current_start if anchor is not None else None,
                anchor.previous_start if anchor is not None else None,
                SEASON_ANCHOR_RULE_VERSION,
                outcome,
                failure_reason,
            ),
        )
        if outcome == "conflict":
            connection.execute(
                """
                UPDATE player_profile_versions
                SET source_contract_state = 'conflict', season_anchor_state = 'conflict'
                WHERE id = %s
                """,
                (profile_version_id,),
            )
        return outcome

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
