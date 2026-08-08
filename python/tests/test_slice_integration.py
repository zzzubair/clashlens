from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from clashlens.api import create_app
from clashlens.archive import S3ArchiveReader
from clashlens.db import Database, LeaseLost
from clashlens.hmac_proof import SigningInput, sign
from clashlens.profile import parse_profile
from clashlens.worker import ObservationProcessor

FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
FIXED_NOW = 1_785_844_800


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def _signed_headers(
    target: str,
    key: bytes,
    *,
    caller: str = "typescript-website",
    key_id: str = "current",
    now: int = FIXED_NOW,
) -> dict[str, str]:
    request_id = str(uuid4())
    value = SigningInput(
        proof_version="clashlens-hmac-v1",
        caller_b64url=_b64(caller),
        key_id_b64url=_b64(key_id),
        audience="clashlens-python-private-api",
        method="GET",
        target_b64url=base64.urlsafe_b64encode(target.encode()).rstrip(b"=").decode(),
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=str(now),
        expires_at=str(now + 10),
        request_id=request_id,
        provider_b64url="",
        provider_subject_b64url="",
    )
    return {
        "X-ClashLens-Proof-Version": value.proof_version,
        "X-ClashLens-Caller": value.caller_b64url,
        "X-ClashLens-Key-Id": value.key_id_b64url,
        "X-ClashLens-Issued-At": value.issued_at,
        "X-ClashLens-Expires-At": value.expires_at,
        "X-ClashLens-Request-Id": value.request_id,
        "X-ClashLens-Provider": "",
        "X-ClashLens-Provider-Subject": "",
        "X-ClashLens-Signature": sign(key, value),
    }


def _seed(
    db: Database,
    archive_server,
    *,
    max_attempts: int = 2,
) -> tuple[int, str]:
    _endpoint, reference, digest, _handler = archive_server
    observed_at = datetime(2026, 8, 4, 11, 59, tzinfo=UTC)
    observation_id, job_id = db.insert_observation_and_job(
        occurrence_key="synthetic-profile-1",
        normalized_tag="#2PP",
        endpoint="profile",
        endpoint_version="profile-v1",
        schema_version="profile-schema-v1",
        observed_at=observed_at,
        http_status=200,
        response_hash=digest,
        archive_reference=reference,
        collector_version="collector-prototype-v1",
        max_attempts=max_attempts,
    )
    assert observation_id > 0
    return job_id, reference


def test_schema_refuses_to_upgrade_an_existing_contract_v1_database(
    database_url: str,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    with db.pool.connection() as connection:
        connection.execute("UPDATE clash_lens_contract SET version = 1")
        connection.commit()
    try:
        with pytest.raises(RuntimeError, match="contract version 1"):
            db.apply_schema()
        assert db.scalar("SELECT version FROM clash_lens_contract") == 1
    finally:
        with db.pool.connection() as connection:
            connection.execute("UPDATE clash_lens_contract SET version = 2")
            connection.commit()
        db.close()


def test_real_postgres_job_claim_profile_effects_and_idempotent_rerun(
    database_url: str, archive_server
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    job_id, _reference = _seed(db, archive_server)
    processor = ObservationProcessor(
        db,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )

    first = processor.process_once(owner="worker-a")

    assert first is not None
    assert first.outcome == "processed"
    assert (
        db.scalar("SELECT status FROM python_processing_jobs WHERE id = %s", (job_id,))
        == "complete"
    )
    assert db.scalar("SELECT count(*) FROM player_profile_versions") == 1
    assert db.scalar("SELECT count(*) FROM player_profile_effects") == 1
    assert (
        db.scalar("SELECT parser_version FROM player_profile_versions")
        == "supercell-source-parser-v1"
    )
    assert (
        db.scalar("SELECT endpoint_version FROM player_profile_versions")
        == "profile-v1"
    )
    assert (
        db.scalar("SELECT schema_version FROM player_profile_versions")
        == "profile-schema-v1"
    )

    db.requeue_completed_job(job_id)
    second = processor.process_once(owner="worker-b")

    assert second is not None
    assert second.outcome == "processed"
    assert db.scalar("SELECT count(*) FROM player_profile_versions") == 1
    assert db.scalar("SELECT count(*) FROM player_profile_effects") == 1
    assert (
        db.scalar(
            "SELECT count(*) FROM python_processing_attempts WHERE job_id = %s",
            (job_id,),
        )
        == 2
    )
    db.close()


def test_equal_time_uncertain_profile_does_not_replace_confirmed_current_state(
    database_url: str,
    archive_server,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    _seed(db, archive_server)
    processor = ObservationProcessor(
        db,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )
    first = processor.process_once(owner="current-state-one")
    assert first is not None and first.outcome == "processed"

    uncertain_payload = json.loads(FIXTURE.read_bytes())
    uncertain_payload["name"] = "Equal-time uncertain evidence"
    uncertain_payload["leagueTier"] = {"id": 999999999, "name": "Unexpected Tier"}
    uncertain_body = json.dumps(uncertain_payload, separators=(",", ":")).encode(
        "utf-8"
    )
    uncertain_digest = hashlib.sha256(uncertain_body).hexdigest()
    uncertain_key = f"sha256/{uncertain_digest[:2]}/{uncertain_digest}"
    archive_server[3].objects[uncertain_key] = uncertain_body
    db.insert_observation_and_job(
        occurrence_key="equal-time-uncertain-profile",
        normalized_tag="#2PP",
        endpoint="profile",
        endpoint_version="profile-v1",
        schema_version="profile-schema-v1",
        observed_at=datetime(2026, 8, 4, 11, 59, tzinfo=UTC),
        http_status=200,
        response_hash=uncertain_digest,
        archive_reference=f"s3://evidence/{uncertain_key}",
        collector_version="collector-prototype-v1",
    )

    second = processor.process_once(owner="current-state-two")
    current = db.get_player("#2PP")

    assert second is not None and second.outcome == "processed"
    assert current is not None
    assert current["name"] == "Synthetic Legend I"
    assert current["eligibility_state"] == "eligible"
    assert current["active"] is True
    assert db.scalar("SELECT count(*) FROM player_profile_versions") == 2
    db.close()


def test_expired_or_wrong_lease_cannot_write_product_effects_or_complete(
    database_url: str, archive_server
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    job_id, _reference = _seed(db, archive_server)
    claim = db.claim_job(owner="stale-worker", lease_seconds=30)
    assert claim is not None and claim.job_id == job_id
    db.expire_lease(job_id)
    profile = parse_profile(
        FIXTURE.read_bytes(),
        expected_tag="#2PP",
        observed_at=datetime(2026, 8, 4, 11, 59, tzinfo=UTC),
        endpoint_version="profile-v1",
    )

    with pytest.raises(LeaseLost):
        db.complete_profile(claim, profile)

    assert db.scalar("SELECT count(*) FROM player_profile_versions") == 0
    assert (
        db.scalar("SELECT status FROM python_processing_jobs WHERE id = %s", (job_id,))
        == "leased"
    )
    db.close()


def test_expired_lease_stops_after_the_configured_attempt_limit(
    database_url: str,
    archive_server,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    job_id, _reference = _seed(db, archive_server, max_attempts=1)
    claim = db.claim_job(owner="failed-worker", lease_seconds=30)
    assert claim is not None and claim.job_id == job_id
    db.expire_lease(job_id)

    replacement = db.claim_job(owner="replacement-worker", lease_seconds=30)

    assert replacement is None
    assert (
        db.scalar("SELECT status FROM python_processing_jobs WHERE id = %s", (job_id,))
        == "failed"
    )
    assert (
        db.scalar(
            "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
            (job_id,),
        )
        == "lease_expired_max_attempts"
    )
    assert (
        db.scalar(
            "SELECT state FROM python_processing_attempts WHERE job_id = %s", (job_id,)
        )
        == "stale"
    )
    db.close()


def test_missing_archive_retries_once_then_becomes_a_durable_integrity_failure(
    database_url: str,
    archive_server,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    job_id, reference = _seed(db, archive_server, max_attempts=2)
    archive_server[3].objects.pop(reference.removeprefix("s3://evidence/"))
    processor = ObservationProcessor(
        db,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )

    first = processor.process_once(owner="archive-retry-one")
    with db.pool.connection() as connection:
        connection.execute(
            "UPDATE python_processing_jobs SET due_at = clock_timestamp() WHERE id = %s",
            (job_id,),
        )
        connection.commit()
    second = processor.process_once(owner="archive-retry-two")

    assert first is not None and first.outcome == "retrying"
    assert second is not None and second.outcome == "failed"
    assert second.category == "archive_missing"
    assert (
        db.scalar("SELECT status FROM python_processing_jobs WHERE id = %s", (job_id,))
        == "failed"
    )
    assert db.scalar("SELECT count(*) FROM player_profile_effects") == 0
    db.close()


def test_malformed_success_and_non_success_are_distinct_classified_outcomes(
    database_url: str,
    archive_server,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    endpoint, _reference, _digest, handler = archive_server
    malformed = b'{"tag":"#2PP"'
    malformed_digest = hashlib.sha256(malformed).hexdigest()
    malformed_key = f"sha256/{malformed_digest[:2]}/{malformed_digest}"
    handler.objects[malformed_key] = malformed
    _observation_id, malformed_job_id = db.insert_observation_and_job(
        occurrence_key="malformed-profile",
        normalized_tag="#2PP",
        endpoint="profile",
        endpoint_version="profile-v1",
        schema_version="profile-schema-v1",
        observed_at=datetime(2026, 8, 4, 11, 59, tzinfo=UTC),
        http_status=200,
        response_hash=malformed_digest,
        archive_reference=f"s3://evidence/{malformed_key}",
        collector_version="collector-prototype-v1",
    )
    processor = ObservationProcessor(
        db,
        S3ArchiveReader(
            endpoint=endpoint,
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )

    malformed_result = processor.process_once(owner="malformed-worker")

    assert malformed_result is not None
    assert malformed_result.outcome == "failed"
    assert malformed_result.category == "malformed_json"
    assert (
        db.scalar(
            "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
            (malformed_job_id,),
        )
        == "malformed_json"
    )
    db.clear_prototype_data()
    non_success_job_id, _reference = _seed(db, archive_server)
    with db.pool.connection() as connection:
        connection.execute(
            """
            UPDATE collector_observations
            SET http_status = 404
            WHERE id = (SELECT observation_id FROM python_processing_jobs WHERE id = %s)
            """,
            (non_success_job_id,),
        )
        connection.commit()
    before_archive_reads = handler.get_count

    non_success_result = processor.process_once(owner="non-success-worker")

    assert non_success_result is not None
    assert non_success_result.outcome == "classified"
    assert non_success_result.category == "non_success"
    assert db.scalar("SELECT status FROM python_processing_jobs") == "complete"
    assert db.scalar("SELECT count(*) FROM player_profile_effects") == 0
    assert handler.get_count == before_archive_reads + 1
    db.close()


@pytest.mark.parametrize(
    "column",
    [
        "parser_version",
        "processing_version",
        "domain_rule_version",
    ],
)
def test_worker_leaves_jobs_with_unsupported_mutable_versions_unclaimed(
    database_url: str,
    archive_server,
    column: str,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    job_id, _reference = _seed(db, archive_server)
    with db.pool.connection() as connection:
        connection.execute(
            f"UPDATE python_processing_jobs SET {column} = %s WHERE id = %s",
            ("unsupported-v99", job_id),
        )
        connection.commit()
    before_archive_reads = archive_server[3].get_count
    processor = ObservationProcessor(
        db,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )

    result = processor.process_once(owner="worker-version-check")

    assert result is None
    assert (
        db.scalar("SELECT status FROM python_processing_jobs WHERE id = %s", (job_id,))
        == "pending"
    )
    assert db.scalar("SELECT count(*) FROM player_profile_effects") == 0
    assert archive_server[3].get_count == before_archive_reads
    db.close()


def test_database_rejects_unsupported_collector_endpoint_before_generated_versions(
    database_url: str,
    archive_server,
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    _job_id, _reference = _seed(db, archive_server)
    observation_id = db.scalar(
        "SELECT id FROM collector_observations WHERE occurrence_key = %s",
        ("synthetic-profile-1",),
    )
    with db.pool.connection() as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            with connection.transaction():
                connection.execute(
                    "UPDATE collector_observations SET endpoint = %s WHERE id = %s",
                    ("unsupported-v99", observation_id),
                )
    db.close()


def test_signed_api_reads_saved_data_with_freshness_and_eligibility_without_archive_read(
    database_url: str, archive_server
) -> None:
    db = Database(database_url)
    db.apply_schema()
    db.clear_prototype_data()
    _seed(db, archive_server)
    processor = ObservationProcessor(
        db,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )
    assert processor.process_once(owner="worker-api") is not None
    before_api_reads = archive_server[3].get_count

    key = bytes.fromhex("11" * 32)
    previous_key = bytes.fromhex("22" * 32)
    future_key = bytes.fromhex("33" * 32)
    app = create_app(
        database_url,
        keys={
            ("typescript-website", "current"): key,
            ("typescript-website", "previous"): previous_key,
            ("future-integration", "current"): future_key,
        },
        clock=lambda: FIXED_NOW,
    )
    target = "/v1/players/%232PP?view=summary&view=live"
    with TestClient(app) as client:
        response = client.get(target, headers=_signed_headers(target, key))
        previous_response = client.get(
            target,
            headers=_signed_headers(target, previous_key, key_id="previous"),
        )
        denied_response = client.get(
            target,
            headers=_signed_headers(
                target,
                future_key,
                caller="future-integration",
            ),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["tag"] == "#2PP"
    assert payload["name"] == "Synthetic Legend I"
    assert payload["freshness"] == "fresh"
    assert payload["eligibility"] == "eligible"
    assert payload["coverage"] == "profile"
    assert payload["parser_version"] == "supercell-source-parser-v1"
    assert "id" not in payload
    assert previous_response.status_code == 200
    assert denied_response.status_code == 403

    retired_app = create_app(
        database_url,
        keys={("typescript-website", "current"): key},
        clock=lambda: FIXED_NOW,
    )
    with TestClient(retired_app) as client:
        retired_response = client.get(
            target,
            headers=_signed_headers(target, previous_key, key_id="previous"),
        )
    assert retired_response.status_code == 401
    assert archive_server[3].get_count == before_api_reads
    db.close()
