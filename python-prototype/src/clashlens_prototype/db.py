from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .profile import ParsedProfile

PROCESSING_VERSION = "python-processing-prototype-v1"
DEFAULT_PARSER_VERSION = "profile-parser-v1"
PROTOTYPE_CONTRACT_VERSION = 2


class LeaseLost(RuntimeError):
    """The claim no longer has a live owner/token fence."""


@dataclass(frozen=True, slots=True)
class Claim:
    job_id: int
    observation_id: int
    attempt_id: int
    attempt_number: int
    normalized_tag: str
    endpoint: str
    endpoint_version: str
    schema_version: str
    observed_at: datetime
    http_status: int
    response_hash: str
    archive_reference: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    parser_version: str
    processing_version: str
    max_attempts: int


class Database:
    def __init__(self, database_url: str, *, max_size: int = 4) -> None:
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=max_size,
            open=True,
        )

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

    def apply_schema(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.pool.connection() as connection:
            relation = connection.execute(
                "SELECT to_regclass('public.clash_lens_contract')"
            ).fetchone()
            if relation is not None and relation[0] is not None:
                contract = connection.execute(
                    "SELECT version FROM clash_lens_contract WHERE singleton = true"
                ).fetchone()
                if contract is None:
                    raise RuntimeError(
                        "existing Clash Lens contract has no singleton version row"
                    )
                version = int(contract[0])
                if version != PROTOTYPE_CONTRACT_VERSION:
                    raise RuntimeError(
                        f"refusing to alter existing Clash Lens contract version {version}; "
                        f"prototype requires version {PROTOTYPE_CONTRACT_VERSION}"
                    )
            connection.execute(schema)
            connection.commit()

    def clear_prototype_data(self) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                "TRUNCATE player_profile_effects, player_profile_versions, players, "
                "python_processing_attempts, python_processing_jobs, collector_observations "
                "RESTART IDENTITY CASCADE"
            )
            connection.commit()

    def scalar(self, query: str, params: Iterable[Any] = ()) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return None if row is None else _text_value(row[0])

    def insert_observation_and_job(
        self,
        *,
        occurrence_key: str,
        normalized_tag: str,
        endpoint: str,
        endpoint_version: str,
        schema_version: str,
        observed_at: datetime,
        http_status: int,
        response_hash: str,
        archive_reference: str,
        collector_version: str,
        max_attempts: int = 2,
    ) -> tuple[int, int]:
        with self.pool.connection() as connection:
            with connection.transaction():
                observation = connection.execute(
                    """
                    INSERT INTO collector_observations (
                        occurrence_key, normalized_tag, endpoint, endpoint_version,
                        schema_version, request_started_at, response_observed_at,
                        http_status, response_hash, archive_reference, collector_version
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s - interval '1 second', %s,
                        %s, %s, %s, %s
                    )
                    ON CONFLICT (occurrence_key) DO UPDATE
                        SET occurrence_key = EXCLUDED.occurrence_key
                    RETURNING id
                    """,
                    (
                        occurrence_key,
                        normalized_tag,
                        endpoint,
                        endpoint_version,
                        schema_version,
                        observed_at,
                        observed_at,
                        http_status,
                        response_hash,
                        archive_reference,
                        collector_version,
                    ),
                ).fetchone()
                assert observation is not None
                observation_id = int(observation[0])
                job = connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        observation_id, work_type, state, due_at,
                        parser_version, processing_version, max_attempts
                    ) VALUES (%s, 'process_observation', 'pending', clock_timestamp(), %s, %s, %s)
                    ON CONFLICT (observation_id) DO UPDATE
                        SET observation_id = EXCLUDED.observation_id
                    RETURNING id
                    """,
                    (
                        observation_id,
                        DEFAULT_PARSER_VERSION,
                        PROCESSING_VERSION,
                        max_attempts,
                    ),
                ).fetchone()
                assert job is not None
                return observation_id, int(job[0])

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
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE python_processing_attempts AS a
                    SET state = 'stale', completed_at = clock_timestamp(),
                        failure_category = COALESCE(a.failure_category, 'lease_expired')
                    FROM python_processing_jobs AS j
                    WHERE a.job_id = j.id AND a.state = 'running'
                      AND j.state = 'leased'
                      AND j.lease_expires_at <= clock_timestamp()
                      AND j.attempt_count >= j.max_attempts
                    """
                )
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET state = 'failed', outcome = 'durable_failure',
                        failure_category = 'lease_expired_max_attempts',
                        failure_detail = 'lease expired after the configured attempt limit',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                        completed_at = clock_timestamp(), updated_at = clock_timestamp()
                    WHERE state = 'leased'
                      AND lease_expires_at <= clock_timestamp()
                      AND attempt_count >= max_attempts
                    """
                )
                where = """
                    (
                        (j.state IN ('pending', 'waiting_retry') AND j.due_at <= clock_timestamp())
                        OR (j.state = 'leased' AND j.lease_expires_at <= clock_timestamp())
                    )
                    AND j.attempt_count < j.max_attempts
                """
                params: list[Any] = []
                if job_id is not None:
                    where += " AND j.id = %s"
                    params.append(job_id)
                row = connection.execute(
                    f"""
                    SELECT
                        j.id AS job_id, j.observation_id, j.parser_version,
                        j.processing_version, j.attempt_count, j.max_attempts,
                        o.normalized_tag, o.endpoint, o.endpoint_version,
                        o.schema_version, o.response_observed_at, o.http_status,
                        o.response_hash, o.archive_reference
                    FROM python_processing_jobs AS j
                    JOIN collector_observations AS o ON o.id = j.observation_id
                    WHERE {where}
                    ORDER BY j.due_at, j.id
                    FOR UPDATE OF j SKIP LOCKED
                    LIMIT 1
                    """,
                    tuple(params),
                ).fetchone()
                if row is None:
                    return None
                data = (
                    dict(row)
                    if isinstance(row, dict)
                    else {
                        "job_id": row[0],
                        "observation_id": row[1],
                        "parser_version": row[2],
                        "processing_version": row[3],
                        "attempt_count": row[4],
                        "max_attempts": row[5],
                        "normalized_tag": row[6],
                        "endpoint": row[7],
                        "endpoint_version": row[8],
                        "schema_version": row[9],
                        "response_observed_at": row[10],
                        "http_status": row[11],
                        "response_hash": row[12],
                        "archive_reference": row[13],
                    }
                )
                if int(data["attempt_count"]) > 0:
                    connection.execute(
                        """
                        UPDATE python_processing_attempts
                        SET state = 'stale', completed_at = clock_timestamp(),
                            failure_category = COALESCE(failure_category, 'lease_expired')
                        WHERE job_id = %s AND state = 'running'
                        """,
                        (data["job_id"],),
                    )
                token = uuid4().hex
                attempt_number = int(data["attempt_count"]) + 1
                leased = connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET state = 'leased', lease_owner = %s, lease_token = %s,
                        lease_expires_at = clock_timestamp() + (%s * interval '1 second'),
                        attempt_count = attempt_count + 1,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                    RETURNING lease_expires_at
                    """,
                    (owner, token, lease_seconds, data["job_id"]),
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
                    observation_id=int(data["observation_id"]),
                    attempt_id=int(attempt[0]),
                    attempt_number=attempt_number,
                    normalized_tag=_text_value(data["normalized_tag"]),
                    endpoint=_text_value(data["endpoint"]),
                    endpoint_version=_text_value(data["endpoint_version"]),
                    schema_version=_text_value(data["schema_version"]),
                    observed_at=data["response_observed_at"],
                    http_status=int(data["http_status"]),
                    response_hash=_text_value(data["response_hash"]),
                    archive_reference=_text_value(data["archive_reference"]),
                    lease_owner=owner,
                    lease_token=token,
                    lease_expires_at=leased[0],
                    parser_version=_text_value(data["parser_version"]),
                    processing_version=_text_value(data["processing_version"]),
                    max_attempts=int(data["max_attempts"]),
                )

    def complete_profile(self, claim: Claim, profile: ParsedProfile) -> None:
        with self.pool.connection() as connection:
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
                        eligibility_state, profile_json
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                        name = EXCLUDED.name,
                        trophies = EXCLUDED.trophies,
                        league_tier_id = EXCLUDED.league_tier_id,
                        league_tier_name = EXCLUDED.league_tier_name,
                        eligibility_state = EXCLUDED.eligibility_state,
                        profile_json = EXCLUDED.profile_json
                    RETURNING id
                    """,
                    (
                        player[0],
                        claim.observation_id,
                        profile.normalized_tag,
                        profile.endpoint_version,
                        profile.schema_version,
                        profile.parser_version,
                        profile.observed_at,
                        claim.http_status,
                        profile.name,
                        profile.trophies,
                        profile.league_tier_id,
                        profile.league_tier_name,
                        profile.eligibility_state,
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
                    (profile_version_id, claim.observation_id),
                )
                connection.execute(
                    """
                    UPDATE players
                    SET active = CASE
                            WHEN %s = 'eligible' THEN true
                            WHEN %s = 'ineligible' THEN false
                            ELSE active
                        END,
                        eligibility_state = %s,
                        current_profile_version_id = %s,
                        current_observed_at = %s,
                        updated_at = clock_timestamp()
                    WHERE id = %s
                      AND (current_observed_at IS NULL OR current_observed_at < %s)
                    """,
                    (
                        profile.eligibility_state,
                        profile.eligibility_state,
                        profile.eligibility_state,
                        profile_version_id,
                        profile.observed_at,
                        player[0],
                        profile.observed_at,
                    ),
                )
                self._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )

    def complete_classified(self, claim: Claim, *, outcome: str) -> None:
        with self.pool.connection() as connection:
            with connection.transaction():
                job = self._lock_live_claim(connection, claim)
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
                should_retry = retryable and claim.attempt_number < claim.max_attempts
                state = "waiting_retry" if should_retry else "failed"
                outcome = "retryable_failure" if should_retry else "durable_failure"
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
                completed = connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET state = %s,
                        due_at = CASE WHEN %s THEN clock_timestamp() + interval '1 second' ELSE due_at END,
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
                        should_retry,
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
                return state

    def requeue_completed_job(self, job_id: int) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                """
                UPDATE python_processing_jobs
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
                """
                UPDATE python_processing_jobs
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

    def _lock_live_claim(self, connection: Any, claim: Claim) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, attempt_count, max_attempts
            FROM python_processing_jobs
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
            """
            UPDATE python_processing_jobs
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


def _text_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value
