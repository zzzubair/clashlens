from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    _text_value,
)


def _refresh_reset_baseline_evidence(
    database: Database,
    connection: Any,
    claim: Claim,
    *,
    failure_category: str | None = None,
    failure_retryable: bool = False,
) -> None:
    if claim.observation_id is None:
        return
    context = _load_reset_baseline_context(connection, claim.observation_id)
    if context is None:
        return
    work_id, player_id, normalized_tag, sweep_id, boundary_at = context
    endpoints = {
        endpoint: _load_reset_endpoint_evidence(database, 
            connection,
            endpoint=endpoint,
            work_id=int(work_id),
            player_id=int(player_id),
            normalized_tag=_text_value(normalized_tag),
            boundary_at=boundary_at,
            parser_version=claim.parser_version,
            processing_version=claim.processing_version,
            claim=claim,
            failure_category=failure_category,
            failure_retryable=failure_retryable,
        )
        for endpoint in ("profile", "battle_log")
    }
    reasons = list(
        dict.fromkeys(
            reason
            for endpoint in endpoints.values()
            for reason in endpoint["reasons"]
        )
    )
    profile = endpoints["profile"]
    battle_log = endpoints["battle_log"]
    profile_valid = bool(profile["valid"])
    battle_log_valid = bool(battle_log["valid"])
    hard_failure = any(endpoint["hard_failure"] for endpoint in endpoints.values())
    if profile_valid and battle_log_valid and not hard_failure:
        state = "complete"
        reasons = []
    elif hard_failure:
        state = "failed"
    else:
        state = "partial"

    evidence_json = {
        "collector_work_id": int(work_id),
        "reset_sweep_id": int(sweep_id),
        "profile": {
            "observation_id": profile["observation_id"],
            "processing_outcome_id": profile["processing_outcome_id"],
            "processing_outcome": profile["processing_outcome"],
        },
        "battle_log": {
            "observation_id": battle_log["observation_id"],
            "processing_outcome_id": battle_log["processing_outcome_id"],
            "processing_outcome": battle_log["processing_outcome"],
        },
        "failure_reasons": reasons,
    }
    fingerprint = {
        **evidence_json,
        "profile_valid": profile_valid,
        "battle_log_valid": battle_log_valid,
        "state": state,
        "parser_version": claim.parser_version,
        "processing_version": claim.processing_version,
    }
    evidence_key = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"reset-baseline:{work_id}",),
    )
    existing = connection.execute(
        """
        SELECT id, version
        FROM reset_baseline_evidence
        WHERE collector_work_id = %s AND evidence_key = %s
        """,
        (work_id, evidence_key),
    ).fetchone()
    if existing is not None:
        evidence_id, version = int(existing[0]), int(existing[1])
    else:
        prior = connection.execute(
            """
            SELECT id, version
            FROM reset_baseline_evidence
            WHERE collector_work_id = %s
            ORDER BY version DESC, id DESC
            LIMIT 1
            FOR UPDATE
            """,
            (work_id,),
        ).fetchone()
        version = int(prior[1]) + 1 if prior is not None else 1
        inserted = connection.execute(
            """
            INSERT INTO reset_baseline_evidence (
                sweep_id, player_id, boundary_at, collector_work_id,
                profile_observation_id, battle_log_observation_id,
                profile_valid, battle_log_valid,
                profile_processing_outcome_id, battle_log_processing_outcome_id,
                parser_version, processing_version, version, supersedes_id,
                state, failure_reasons, evidence_json, evidence_key
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                sweep_id,
                player_id,
                boundary_at,
                work_id,
                profile["observation_id"],
                battle_log["observation_id"],
                profile_valid,
                battle_log_valid,
                profile["processing_outcome_id"],
                battle_log["processing_outcome_id"],
                claim.parser_version,
                claim.processing_version,
                version,
                prior[0] if prior is not None else None,
                state,
                Jsonb(reasons),
                Jsonb(evidence_json),
                evidence_key,
            ),
        ).fetchone()
        assert inserted is not None
        evidence_id = int(inserted[0])

    if state in {"complete", "failed"}:
        _record_boundary_baseline(database, 
            connection,
            boundary_at=boundary_at,
            reset_sweep_id=int(sweep_id),
            player_id=int(player_id),
            state=state,
        )
    if state == "complete":
        _enqueue_reset_reconciliation(
            connection,
            baseline_id=evidence_id,
            baseline_version=version,
            player_id=int(player_id),
            boundary_at=boundary_at,
        )


def _record_boundary_baseline(
    database: Database,
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
        SELECT id, member_ids
        FROM collector_reset_sweeps
        WHERE id = %s AND boundary_at = %s
        """,
        (reset_sweep_id, boundary_at),
    ).fetchone()
    if sweep is None:
        return
    member_ids = [int(value) for value in (sweep[1] or [])]
    if player_id not in member_ids:
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
        generation_id, _generation = boundary._create_boundary_generation(database, 
            connection,
            boundary_at=boundary_at,
            sweep_id=reset_sweep_id,
            player_ids=member_ids,
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
            boundary._boundary_snapshot_status(
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_version_id,
                boundary_at=boundary_at,
            )
            if ranked_version_id is not None
            else "pending"
        )
        army_status = (
            boundary._boundary_army_status(database, 
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
    boundary._try_enqueue_boundary_artifacts(database, 
        connection, boundary_at=boundary_at, generation_id=int(generation[0])
    )


def _load_reset_baseline_context(
    connection: Any,
    observation_id: int,
) -> tuple[Any, ...] | None:
    row = connection.execute(
        """
        SELECT work.id, work.player_id, work.normalized_tag,
               work.sweep_id, sweep.boundary_at
        FROM collector_work AS work
        JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
        WHERE work.kind = 'reset_baseline'
          AND (
              work.profile_observation_id = %s
              OR work.battle_log_observation_id = %s
          )
        """,
        (observation_id, observation_id),
    ).fetchone()
    return None if row is None else tuple(row)


def _load_reset_endpoint_evidence(
    database: Database,
    connection: Any,
    *,
    endpoint: str,
    work_id: int,
    player_id: int,
    normalized_tag: str,
    boundary_at: datetime,
    parser_version: str,
    processing_version: str,
    claim: Claim,
    failure_category: str | None,
    failure_retryable: bool,
) -> dict[str, Any]:
    profile_join = (
        """
        LEFT JOIN player_profile_effects AS profile_effect
          ON profile_effect.observation_id = observed.id
         AND profile_effect.parser_version = %s
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = profile_effect.profile_version_id
        """
        if getattr(database, "_supports_content_dedup", False)
        else """
        LEFT JOIN player_profile_versions AS profile
          ON profile.observation_id = observed.id
         AND profile.parser_version = %s
        """
    )
    endpoint_column = (
        "work.profile_observation_id"
        if endpoint == "profile"
        else "work.battle_log_observation_id"
    )
    endpoint_status = (
        "work.profile_status"
        if endpoint == "profile"
        else "work.battle_log_status"
    )
    row = connection.execute(
        f"""
        SELECT {endpoint_status}, observed.id, observed.player_id,
               observed.normalized_tag, observed.response_completed_at,
               observed.http_status, processing.id, processing.outcome,
               processing.failure_category, profile.id,
               profile.source_contract_state, profile.eligibility_state,
               battle_log.id, battle_log.has_row_gap, work.status,
               work.failure_category
        FROM collector_work AS work
        LEFT JOIN collector_observations AS observed
          ON observed.id = {endpoint_column}
        LEFT JOIN observation_processing_outcomes AS processing
          ON processing.observation_id = observed.id
         AND processing.parser_version = %s
         AND processing.processing_version = %s
        {profile_join}
        LEFT JOIN battle_log_observations AS battle_log
          ON battle_log.observation_id = observed.id
         AND battle_log.parser_version = %s
        WHERE work.id = %s
        """,
        (
            parser_version,
            processing_version,
            parser_version,
            parser_version,
            work_id,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("reset work disappeared while processing its evidence")

    observation_id = int(row[1]) if row[1] is not None else None
    processing_id = int(row[6]) if row[6] is not None else None
    processing_outcome = _text_value(row[7]) if row[7] is not None else None
    reasons: list[str] = []
    hard_failure = False
    missing = observation_id is None
    if missing:
        reasons.append(f"missing_{endpoint}_observation")
        hard_failure = _text_value(row[14]) == "failed"
    else:
        if int(row[2]) != player_id or _text_value(row[3]) != normalized_tag:
            reasons.append(f"{endpoint}_wrong_player")
            hard_failure = True
        if row[4] is None or row[4] < boundary_at:
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
                _text_value(row[8]) if row[8] is not None else processing_outcome
            )
            reasons.append(f"{endpoint}_{category}")
            hard_failure = True
        elif endpoint == "profile":
            if (
                row[9] is None
                or _text_value(row[10]) != "accepted"
                or _text_value(row[11]) != "eligible"
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
                    (player_id, player_id, boundary_at, boundary_at),
                ).fetchone()[0]
                if first_event is not None and row[4] >= first_event:
                    reasons.append("profile_after_first_event")
                    hard_failure = True
        elif row[12] is None or bool(row[13]):
            reasons.append("battle_log_malformed")
            hard_failure = True

    return {
        "observation_id": observation_id,
        "processing_outcome_id": processing_id,
        "processing_outcome": processing_outcome,
        "reasons": reasons,
        "hard_failure": hard_failure,
        "valid": not reasons and not missing and not hard_failure,
    }


def _load_reset_baseline(
    database: Database,
    connection: Any,
    player_id: int,
    boundary_at: datetime,
    parser_version: str,
    processing_version: str,
) -> dict[str, Any] | None:
    dedup = getattr(database, "_supports_content_dedup", False)
    profile_observed = "profile_effect.observed_at" if dedup else "profile.observed_at"
    profile_join = (
        """
        LEFT JOIN player_profile_effects AS profile_effect
          ON profile_effect.observation_id = evidence.profile_observation_id
         AND profile_effect.parser_version = evidence.parser_version
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = profile_effect.profile_version_id
        """
        if dedup
        else """
        LEFT JOIN player_profile_versions AS profile
          ON profile.observation_id = evidence.profile_observation_id
         AND profile.parser_version = evidence.parser_version
        """
    )
    row = connection.execute(
        f"""
        SELECT evidence.id, evidence.version, evidence.state,
               evidence.sweep_id, evidence.collector_work_id,
               evidence.profile_observation_id,
               evidence.battle_log_observation_id,
               evidence.profile_processing_outcome_id,
               evidence.battle_log_processing_outcome_id,
               evidence.profile_valid, evidence.battle_log_valid,
               evidence.failure_reasons, evidence.evidence_json,
               evidence.evidence_key, evidence.boundary_at,
               profile.id, profile.trophies, profile.eligibility_state,
               profile.source_contract_state, {profile_observed},
               battle_log.id, battle_log.row_count, battle_log.has_row_gap,
               profile_observation.response_hash,
               battle_observation.response_hash
        FROM reset_baseline_evidence AS evidence
        {profile_join}
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
    profile_valid = bool(row[9])
    battle_log_valid = bool(row[10])
    profile_accepted = row[15] is not None and _text_value(row[18]) == "accepted"
    profile_eligible = _text_value(row[17]) == "eligible"
    battle_log_valid_evidence = row[20] is not None and not bool(row[22])
    complete = bool(
        state == "complete"
        and row[4] is not None
        and profile_valid
        and battle_log_valid
        and profile_accepted
        and profile_eligible
        and battle_log_valid_evidence
        and all(row[index] is not None for index in (5, 6, 7, 8))
    )
    failure_reasons = row[11] if isinstance(row[11], list) else []
    stored_evidence = row[12] if isinstance(row[12], dict) else {}
    evidence = {
        "id": int(row[0]),
        "version": int(row[1]),
        "state": state,
        "sweep_id": int(row[3]) if row[3] is not None else None,
        "collector_work_id": int(row[4]) if row[4] is not None else None,
        "profile_observation_id": int(row[5]) if row[5] is not None else None,
        "battle_log_observation_id": int(row[6]) if row[6] is not None else None,
        "profile_processing_outcome_id": (
            int(row[7]) if row[7] is not None else None
        ),
        "battle_log_processing_outcome_id": (
            int(row[8]) if row[8] is not None else None
        ),
        "profile_valid": profile_valid,
        "battle_log_valid": battle_log_valid,
        "failure_reasons": list(failure_reasons),
        "evidence_key": _text_value(row[13]),
        "boundary_at": row[14].astimezone(UTC).isoformat(),
        "profile": {
            "id": int(row[15]) if row[15] is not None else None,
            "trophies": int(row[16]) if row[16] is not None else None,
            "eligibility_state": (
                _text_value(row[17]) if row[17] is not None else None
            ),
            "source_contract_state": (
                _text_value(row[18]) if row[18] is not None else None
            ),
            "observed_at": (
                row[19].astimezone(UTC).isoformat() if row[19] is not None else None
            ),
            "response_hash": _text_value(row[23]),
        },
        "battle_log": {
            "id": int(row[20]) if row[20] is not None else None,
            "row_count": int(row[21]) if row[21] is not None else None,
            "has_row_gap": bool(row[22]) if row[22] is not None else None,
            "response_hash": _text_value(row[24]),
        },
        "stored_evidence": stored_evidence,
    }
    return {
        "id": int(row[0]),
        "version": int(row[1]),
        "state": state,
        "complete": complete,
        "trophies": int(row[16]) if row[16] is not None else None,
        "eligibility_state": (
            _text_value(row[17]) if row[17] is not None else None
        ),
        "evidence": evidence,
    }


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


