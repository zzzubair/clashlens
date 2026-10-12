from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
from psycopg.errors import (
    DeadlockDetected,
    IdleInTransactionSessionTimeout,
    LockNotAvailable,
    QueryCanceled,
    SerializationFailure,
    TransactionTimeout,
)
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout, TooManyRequests

from . import reset_baselines
from .db import PYTHON_RESET_PRIORITY, Claim, Database, LeaseLost, _text_value

# A job a busy lock or the statement deadline rolled back waits out of its
# lease, costing no attempt: 30 s, then 60, 120 and 240, then every 5 minutes,
# a Reset job at most a minute, each plus up to 9 s by job so a crowd spreads.
# On 12 Oct 2026 131 Season-rule rebuilds met Reset locks that board builds
# held for 6 to 16 minutes. Each kept its lease, so it came back at the head
# of the queue every minute, 57 to 167 times, about one run in three was
# wasted, and the leases counted as late live work, pausing background work.
DEFERRED_CONFLICTS = frozenset({"database_lock_busy", "database_timeout"})
CONFLICT_RETRY_FIRST_SECONDS = 30
CONFLICT_RETRY_MAX_SECONDS = 300
RESET_CONFLICT_RETRY_MAX_SECONDS = 60


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


def _record_parsed_payload(
    connection: Any,
    *,
    endpoint: str,
    response_hash: str,
    parser_version: str,
    schema_version: str,
    parse_outcome: str,
    parsed_json: Any,
    representation: str | None = None,
) -> int:
    stored_json = {"representation": representation} if representation else parsed_json
    inserted = connection.execute(
        """
        INSERT INTO parsed_source_payloads (
            endpoint, response_hash, parser_version, schema_version,
            parse_outcome, parsed_json
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (endpoint, response_hash, parser_version) DO NOTHING
        RETURNING id
        """,
        (
            endpoint,
            response_hash,
            parser_version,
            schema_version,
            parse_outcome,
            Jsonb(stored_json),
        ),
    ).fetchone()
    if inserted is not None:
        return int(inserted[0])
    existing = connection.execute(
        """
        SELECT id, schema_version, parse_outcome, parsed_json
        FROM parsed_source_payloads
        WHERE endpoint = %s AND response_hash = %s AND parser_version = %s
        """,
        (endpoint, response_hash, parser_version),
    ).fetchone()
    if existing is None:
        raise RuntimeError("canonical parsed payload disappeared")
    if (
        _text_value(existing[1]) != schema_version
        or _text_value(existing[2]) != parse_outcome
        or existing[3] not in (parsed_json, stored_json)
    ):
        raise ValueError("canonical parsed payload identity conflict")
    return int(existing[0])


def _record_processing_outcome(
    database: Database,
    connection: Any,
    claim: Claim,
    *,
    outcome: str,
    failure_category: str | None = None,
    parsed_payload_id: int | None = None,
) -> int:
    observation_id, http_status, response_hash, observed_at, endpoint, _schema = (
        _observation_source(claim)
    )
    if not getattr(database, "_supports_content_dedup", False):
        row = connection.execute(
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
            RETURNING id
            """,
            (
                observation_id, claim.parser_version, claim.processing_version,
                endpoint, response_hash, http_status, observed_at, outcome,
                failure_category,
            ),
        ).fetchone()
    else:
        row = connection.execute(
            """
            INSERT INTO observation_processing_outcomes (
                observation_id, attempt_id, parser_version, processing_version, endpoint,
                response_hash, source_http_status, source_observed_at,
                outcome, failure_category, parsed_payload_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (observation_id, parser_version, processing_version)
            DO UPDATE SET
                outcome = EXCLUDED.outcome,
                failure_category = EXCLUDED.failure_category,
                attempt_id = EXCLUDED.attempt_id,
                parsed_payload_id = EXCLUDED.parsed_payload_id
            RETURNING id
            """,
            (
                observation_id, claim.attempt_id, claim.parser_version,
                claim.processing_version, endpoint, response_hash, http_status,
                observed_at, outcome, failure_category, parsed_payload_id,
            ),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _failure_outcome(category: str) -> str:
    if category.startswith("archive_") or category == "checksum_mismatch":
        return "integrity_failure"
    if category.startswith("unsupported_") or category.endswith("_schema"):
        return "unsupported"
    return "malformed"


def _record_official_failed_attempt(
    connection: Any,
    claim: Claim,
    *,
    outcome: str,
    category: str,
) -> None:
    observation_id, _status, _hash, observed_at, _endpoint, _schema = (
        _observation_source(claim)
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


def fail_claim(
    database: Database,
    claim: Claim,
    *,
    category: str,
    detail: str,
    retryable: bool,
) -> str:
    safe_detail = detail[:500]
    with database.pool.connection() as connection:
        with connection.transaction():
            database._lock_live_claim(connection, claim)
            database._ensure_dependency_support_probed()
            dependency = (
                database._supports_dependency_deferral
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
                if database._supports_dependency_deferral
                else """
                    due_at = CASE WHEN %s THEN clock_timestamp() + interval '1 second' ELSE due_at END,"""
            )
            dependency_params = (
                (should_retry, dependency)
                if database._supports_dependency_deferral
                else (should_retry,)
            )
            completed = connection.execute(
                f"""
                UPDATE {database._jobs_relation}
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
                failure_outcome = _failure_outcome(category)
                if not should_retry:
                    _record_processing_outcome(database, 
                        connection,
                        claim,
                        outcome=failure_outcome,
                        failure_category=category,
                    )
                reset_baselines._refresh_reset_baseline_evidence(database, 
                    connection,
                    claim,
                    failure_category=category,
                    failure_retryable=should_retry,
                )
                if not should_retry and claim.endpoint == "global_player_rankings":
                    _record_official_failed_attempt(
                        connection,
                        claim,
                        outcome=failure_outcome,
                        category=category,
                    )
            return state


def release_conflicted(database: Database, claim: Claim, reason: str) -> None:
    """Hand back a job whose transaction a conflict rolled back unrecorded.

    A busy lock or the statement deadline defers it (``DEFERRED_CONFLICTS``).
    Otherwise, or when that cannot commit, its retry slot is restored and its
    lease kept, so queue maintenance recovers it later rather than failing it
    on its last attempt, even if conflicts outlast the lease. Raises LeaseLost
    once another worker or maintenance took the job.
    """
    if reason in DEFERRED_CONFLICTS:
        try:
            if _defer_conflicted(database, claim, reason):
                return
        except psycopg.Error:
            pass  # Restore the retry slot instead.
    while True:
        try:
            database.refund_claim_attempt(claim)
            return
        except (DeadlockDetected, SerializationFailure, QueryCanceled):
            continue
        except (PoolTimeout, TooManyRequests, LockNotAvailable,
                IdleInTransactionSessionTimeout, TransactionTimeout):
            # No connection, or the refund's own short lock wait or session
            # ran out. Leave the lease to run out so queue maintenance
            # retries the job or fails its last try.
            return


def _defer_conflicted(database: Database, claim: Claim, reason: str) -> bool:
    """Release the job to run again after its conflict wait; False where the
    schema has no dependency deferral."""
    if not isinstance(database, Database):
        return False
    database._ensure_dependency_support_probed()
    if not database._supports_dependency_deferral:
        return False
    with database._timed_connection() as connection, connection.transaction():
        # Never queue behind a long holder of the job row.
        connection.execute("SET LOCAL lock_timeout = '1s'")
        # The owner and token fence it even after the lease expired, as the
        # refund does: only a job nobody else has taken is deferred.
        deferred = connection.execute(
            f"""
            UPDATE {database._jobs_relation}
            SET state = 'waiting_dependency',
                due_at = clock_timestamp() + (LEAST(
                    CASE WHEN priority = %(reset)s THEN %(reset_max)s ELSE %(max)s END,
                    %(first)s * 2 ^ LEAST(dependency_deferral_count, 4)) + id %% 10)
                    * interval '1 second',
                dependency_deferral_count = dependency_deferral_count + 1,
                outcome = 'dependency_deferred', failure_category = %(reason)s,
                failure_detail = %(reason)s,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                updated_at = clock_timestamp()
            WHERE id = %(job)s AND state = 'leased'
              AND lease_owner = %(owner)s AND lease_token = %(token)s
            """,
            {"reset": PYTHON_RESET_PRIORITY, "reset_max": RESET_CONFLICT_RETRY_MAX_SECONDS,
             "max": CONFLICT_RETRY_MAX_SECONDS, "first": CONFLICT_RETRY_FIRST_SECONDS,
             "reason": reason, "job": claim.job_id, "owner": claim.lease_owner,
             "token": claim.lease_token},
        )
        if deferred.rowcount != 1:
            raise LeaseLost("job lease was lost while deferring its conflict")
        connection.execute(
            """
            UPDATE python_processing_attempts
            SET state = 'waiting_dependency', completed_at = clock_timestamp(),
                outcome = 'dependency_deferred', failure_category = %s
            WHERE id = %s AND job_id = %s AND lease_token = %s
            """,
            (reason, claim.attempt_id, claim.job_id, claim.lease_token),
        )
    return True


def complete_terminal(database: Database, claim: Claim, *, outcome: str) -> None:
    """Finish a claimed job after a domain fence made its work obsolete."""
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            database._finish_claim(
                connection, claim, job, state="complete", outcome=outcome
            )


def complete_superseded(
    database: Database, claim: Claim, covered: Callable[[Any], bool]
) -> bool:
    """Finish a regular check's job unapplied when newer evidence covers it.

    Only ordinary queued checks qualify. Reset, Refresh, first lookup and
    discovery responses belong to collector work whose own completion reads
    their processing outcome, and replays are explicit requests, so those
    always run. The raw response and its observation stay for replay.
    """
    if claim.work_type != "process_observation" or claim.observation_id is None:
        return False
    with database._timed_connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            if not covered(connection):
                return False
            owned = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM collector_work
                    WHERE profile_observation_id = %s
                ) OR EXISTS (
                    SELECT 1 FROM collector_work
                    WHERE battle_log_observation_id = %s
                ) OR EXISTS (
                    SELECT 1 FROM collector_work
                    WHERE league_history_observation_id = %s
                )
                """,
                (claim.observation_id,) * 3,
            ).fetchone()
            if owned[0]:
                return False
            database._finish_claim(
                connection, claim, job, state="complete", outcome="superseded"
            )
            return True


def complete_classified(database: Database, claim: Claim, *, outcome: str) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            _record_processing_outcome(database, 
                connection,
                claim,
                outcome="non_success"
                if outcome == "source_non_success"
                else outcome,
            )
            reset_baselines._refresh_reset_baseline_evidence(database, connection, claim)
            if claim.endpoint == "global_player_rankings":
                _record_official_failed_attempt(
                    connection, claim, outcome="non_success", category="non_success"
                )
            database._finish_claim(
                connection, claim, job, state="complete", outcome=outcome
            )

