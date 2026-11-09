"""Installed job contracts shared by queue selection, dispatch and accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .source_observation_contract import (
    SOURCE_OBSERVATION_CONTRACTS,
    validate_source_observation_contract,
)


@dataclass(frozen=True, slots=True)
class JobContract:
    handler: str
    analytics_version: str | None = None
    input_shape: str | None = None
    source: bool = False
    population_build: bool = False


JOB_CONTRACTS = {
    "process_observation": JobContract("source", source=True),
    "replay_observation": JobContract("source", source=True),
    "reconcile_ranked_day": JobContract("reconcile", "ANALYTICS_RULE_VERSION"),
    "build_snapshot": JobContract(
        "snapshot", "ANALYTICS_RULE_VERSION", "coordinator", population_build=True
    ),
    "build_analytics": JobContract(
        "analytics", "ANALYTICS_RULE_VERSION", "analytics", population_build=True
    ),
    "build_army_analytics": JobContract(
        "army_analytics",
        "ARMY_ANALYTICS_RULE_VERSION",
        "coordinator",
        population_build=True,
    ),
    "redecode_army": JobContract("army_redecode", "ARMY_ANALYTICS_RULE_VERSION"),
    "reconcile_pass": JobContract("reconcile_pass", "ANALYTICS_RULE_VERSION"),
}
SUPPORTED_WORK_TYPES = tuple(JOB_CONTRACTS)
RESPONSE_WORK_TYPES = tuple(kind for kind, spec in JOB_CONTRACTS.items() if spec.source)
POPULATION_BUILD_WORK_TYPES = tuple(
    kind for kind, spec in JOB_CONTRACTS.items() if spec.population_build
)
DATABASE_CONFLICT_RETRIES = 3


def claim_contract_error(claim: Any) -> str | None:
    from . import db

    spec = JOB_CONTRACTS.get(claim.work_type)
    if spec is None:
        return "unsupported_work_type"
    if spec.source:
        error = validate_source_observation_contract(
            claim.endpoint,
            claim.endpoint_version,
            claim.schema_version,
            claim.parser_version,
        )
        if error is not None:
            return error
    checks = [
        ("processing_version", db.PROCESSING_VERSION),
        ("domain_rule_version", db.DOMAIN_RULE_VERSION),
    ]
    if spec.analytics_version:
        checks.append(("analytics_rule_version", getattr(db, spec.analytics_version)))
    for field, expected in checks:
        if getattr(claim, field) != expected:
            return f"unsupported_{field}"
    return None


def supported_job_filter(
    alias: str,
    *,
    claim: bool = False,
    past_reset_build_hold: str | None = None,
    operator_build_hold: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Installed contracts; claims also fence generations and scheduling holds.

    Source contracts are denormalized on jobs, keeping queue probes independent
    of response history. Cleanup uses the same contracts without scheduling
    holds, so temporarily paused supported jobs still exhaust their budget.
    """
    from . import db

    source_clauses: list[str] = []
    params: dict[str, Any] = {}
    coordinator_input_shape = f"""(
        {alias}.input_json ? 'generation' AND {alias}.input_json ? 'manifest_id'
    AND {alias}.input_json ? 'manifest_digest'
    AND ({alias}.input_json->>'generation') ~ '^[1-9][0-9]*$'
    AND ({alias}.input_json->>'manifest_id') ~ '^[1-9][0-9]*$'
    AND ({alias}.input_json->>'manifest_digest') ~ '^[0-9a-f]{{64}}$'
)"""
    for contract in SOURCE_OBSERVATION_CONTRACTS:
        prefix = f"source_contract_{len(params)}"
        source_clauses.append(
            f"""(
                {alias}.parser_version = ANY(%({prefix}_parsers)s::text[])
                AND {alias}.endpoint = %({prefix}_endpoint)s
                AND {alias}.endpoint_version = %({prefix}_endpoint_version)s
                AND {alias}.schema_version = %({prefix}_schema)s
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
    shapes = {
        "coordinator": coordinator_input_shape,
        "analytics": f"({coordinator_input_shape} AND ({analytics_input_shape}))",
    }
    # Kinds with the same checks share one clause, so each check (above all
    # the source contract) appears once in the statement the planner reads.
    groups: dict[tuple[Any, ...], list[str]] = {}
    for kind, spec in JOB_CONTRACTS.items():
        signature = (spec.analytics_version, spec.source, spec.input_shape)
        groups.setdefault(signature, []).append(kind)
    clauses = []
    for number, (kinds, spec) in enumerate(
        (kinds, JOB_CONTRACTS[kinds[0]]) for kinds in groups.values()
    ):
        key = f"kinds_{number}"
        checks = [
            f"{alias}.work_type = ANY(%({key})s::text[])",
            f"{alias}.processing_version = %(processing_version)s",
            f"{alias}.domain_rule_version = %(domain_rule_version)s",
        ]
        params[key] = kinds
        if spec.analytics_version:
            checks.append(
                f"{alias}.analytics_rule_version = %({spec.analytics_version})s"
            )
            params[spec.analytics_version] = getattr(db, spec.analytics_version)
        if spec.source:
            checks.append(f"({source_contract})")
        if spec.input_shape:
            checks.append(shapes[spec.input_shape])
        clauses.append("(" + " AND ".join(checks) + ")")
    params.update(
        processing_version=db.PROCESSING_VERSION,
        domain_rule_version=db.DOMAIN_RULE_VERSION,
    )
    predicate = "(" + " OR ".join(clauses) + ")"
    if claim:
        # Keep earlier claim generations for queued work and explicit replay.
        # The build kinds are written out, as constants the planner can read.
        builds = ", ".join(f"'{kind}'" for kind in POPULATION_BUILD_WORK_TYPES)
        predicate = f"""{alias}.claim_compatibility_version IN (1, 2, 3, 4, 5, 6, 7)
            AND {predicate}
            AND NOT COALESCE(
                {alias}.work_type IN ({builds})
                AND {alias}.input_json->>'boundary_at' < %(past_reset_build_hold)s::text, false)
            AND NOT (%(operator_build_hold)s
                AND {alias}.work_type IN ({builds})
                AND {alias}.priority = {db.PYTHON_BACKFILL_PRIORITY})
        """
        params.update(
            past_reset_build_hold=past_reset_build_hold,
            operator_build_hold=operator_build_hold,
        )
    return f"({predicate})", params
