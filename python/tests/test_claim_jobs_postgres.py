from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from domain_test_support import apply_migration, domain_database, store_observation
from psycopg.conninfo import make_conninfo

from clashlens import ingestion, reconciliation_db, reset_baselines
from clashlens.archive import S3ArchiveReader
from clashlens.db import (
    POPULATION_BUILD_WORK_TYPES,
    RESPONSE_WORK_TYPES,
    Database,
    LeaseLost,
)
from clashlens.worker import (
    DERIVED_WITHOUT_BUILDS,
    ObservationProcessor,
    ProcessResult,
    process_concurrently,
)

PARSER_VERSION = "supercell-source-parser-v1"
PROCESSING_VERSION = "clashlens-domain-processing-v1"
DOMAIN_RULE_VERSION = "clashlens-domain-rules-v1"
ANALYTICS_RULE_VERSION = "legend-analytics-v1"

CURRENT_ANALYTICS_INPUT = {
    "snapshot_id": 1,
    "snapshot_version": 1,
    "snapshot_input_hash": "a" * 64,
    "source_ranked_day_version_id": 1,
    "generation": 1,
    "manifest_id": 1,
    "manifest_digest": "a" * 64,
}


@contextmanager
def _production_database(
    database_url: str, *, include_army_migrations: bool = False
) -> Iterator[str]:
    schema = f"claim_jobs_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            from pathlib import Path

            root = Path(__file__).parents[2]
            for migration in sorted((root / "deploy/migrations").glob("*.sql")):
                apply_migration(connection, migration.read_text(encoding="utf-8"))
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _insert_observation(connection: psycopg.Connection, *, occurrence_key: str) -> int:
    observed_at = datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC)
    player_id = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, next_due_at)
        VALUES ('#2PP', false, NULL)
        ON CONFLICT (normalized_tag) DO UPDATE
            SET active = EXCLUDED.active
        RETURNING id
        """
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO archive_instances (
            instance_id, endpoint, region, bucket, marker_key,
            marker_hash, marker_payload_version
        ) VALUES ('fixture-instance', 'archive.test:443', 'us-east-1',
                  'evidence', 'clashlens/archive-instance.json',
                  repeat('f', 64), 'v1')
        ON CONFLICT (instance_id) DO NOTHING
        """
    )
    digest = _hash(archive_reference := f"s3://evidence/{occurrence_key}")
    connection.execute(
        """
        INSERT INTO archive_catalogue (
            response_hash, archive_reference, byte_size, archive_instance_id
        ) VALUES (%s, %s, %s, 'fixture-instance')
        ON CONFLICT (response_hash, archive_reference) DO NOTHING
        """,
        (digest, archive_reference, 0),
    )
    observation_id = connection.execute(
        """
        INSERT INTO collector_observations (
            occurrence_key, player_id, scope, normalized_tag,
            endpoint, request_started_at, response_completed_at,
            http_status, response_hash, archive_reference, archive_catalogue_hash,
            collector_version, key_label, evidence_headers, source_adapter_version
        ) VALUES (
            %s, %s, 'player', '#2PP', 'profile', %s, %s, 200,
            %s, %s, %s,
            'collector-v1', 'normal-a', '{}'::jsonb, 'player-profile-v1'
        )
        RETURNING id
        """,
        (
            occurrence_key,
            player_id,
            observed_at,
            observed_at,
            digest,
            archive_reference,
            digest,
        ),
    ).fetchone()[0]
    return int(observation_id)


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_job(
    connection: psycopg.Connection,
    *,
    work_type: str,
    deduplication_key: str,
    input_json: dict,
    observation_id: int | None = None,
    replay_observation_id: int | None = None,
    priority: int = 100,
    due_at: str | None = None,
    parser_version: str = PARSER_VERSION,
    processing_version: str = PROCESSING_VERSION,
    domain_rule_version: str = DOMAIN_RULE_VERSION,
    analytics_rule_version: str = ANALYTICS_RULE_VERSION,
    max_attempts: int = 2,
) -> int:
    import json as json_module

    row = connection.execute(
        """
        INSERT INTO python_processing_jobs (
            work_type, observation_id, replay_observation_id, deduplication_key,
            input_json, priority, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version, max_attempts
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()),
            %s, %s, %s, %s, %s
        )
        RETURNING id
        """,
        (
            work_type,
            observation_id,
            replay_observation_id,
            deduplication_key,
            json_module.dumps(input_json),
            priority,
            due_at,
            parser_version,
            processing_version,
            domain_rule_version,
            analytics_rule_version,
            max_attempts,
        ),
    ).fetchone()
    connection.commit()
    return int(row[0])


def _processor(database: Database, archive_server) -> ObservationProcessor:
    return ObservationProcessor(
        database,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="fixture-access",
            secret_key="fixture-secret",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
@pytest.mark.parametrize("reject_failure_write", [False, True])
@pytest.mark.parametrize(
    ("refund_conflicts", "refund_outlasts_lease"),
    [(0, False), (2, False), (3, False), (3, True)],
)
def test_final_attempt_failure_write_conflict_recovers_after_expiry(
    database_url: str,
    archive_server,
    monkeypatch,
    sqlstate,
    reject_failure_write,
    refund_conflicts,
    refund_outlasts_lease,
) -> None:
    from pathlib import Path

    body = (
        Path(__file__).parents[1] / "testdata/legend_i_profile_v1.json"
    ).read_bytes()
    with domain_database(database_url) as connection_info:
        _, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="final-attempt-conflict",
            endpoint="profile",
            body=body,
            observed_at=datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC),
            normalized_tag="#2PP",
            max_attempts=3,
        )
        database = Database(connection_info)
        try:
            # Spend two ordinary slots through real claims and lease recovery.
            for _ in range(2):
                assert database.claim_job(owner="previous-worker", job_id=job_id)
                database.expire_lease(job_id)
                assert database.maintain_queue(max_jobs=1) == 1

            def reject_transaction(*_args, **_kwargs):
                with database.pool.connection() as connection:
                    connection.execute(
                        "DO $$ BEGIN RAISE EXCEPTION 'forced database conflict' "
                        f"USING ERRCODE = '{sqlstate}'; END $$"
                    )

            processor = _processor(database, archive_server)
            refund_claim_attempt = database.refund_claim_attempt

            def reject_refund(claim):
                nonlocal refund_conflicts
                if refund_conflicts:
                    refund_conflicts -= 1
                    if refund_outlasts_lease and not refund_conflicts:
                        # Stand in for conflicts delayed past the 60 s lease.
                        database.expire_lease(job_id)
                    reject_transaction()
                refund_claim_attempt(claim)

            with monkeypatch.context() as injected:
                injected.setattr(ingestion, "complete_profile", reject_transaction)
                if reject_failure_write:
                    injected.setattr(database, "refund_claim_attempt", reject_refund)
                    injected.setattr(
                        reset_baselines,
                        "_refresh_reset_baseline_evidence",
                        reject_transaction,
                    )
                result = processor.process_job(job_id, owner="conflicted-worker")

            assert result == ProcessResult(
                job_id,
                "retrying" if reject_failure_write else "failed",
                "database_deadlock",
            )
            assert database.scalar(
                "SELECT attempt_count FROM python_processing_jobs WHERE id = %s",
                (job_id,),
            ) == (2 if reject_failure_write else 3)
            assert database.scalar(
                "SELECT status FROM python_processing_jobs WHERE id = %s",
                (job_id,),
            ) == ("leased" if reject_failure_write else "failed")
            assert database.scalar("SELECT count(*) FROM player_profile_versions") == 0

            database.expire_lease(job_id)
            assert database.maintain_queue(max_jobs=1) == int(reject_failure_write)
            recovered = processor.process_job(job_id, owner="recovery-worker")
            if reject_failure_write:
                assert recovered == ProcessResult(job_id, "processed")
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "complete"
                )
                assert (
                    database.scalar(
                        "SELECT attempt_count FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == 3
                )
                assert (
                    database.scalar("SELECT count(*) FROM player_profile_versions") == 1
                )
                assert (
                    database.scalar(
                        "SELECT max(attempt_number) FROM python_processing_attempts "
                        "WHERE job_id = %s",
                        (job_id,),
                    )
                    == 4
                )
            else:
                assert recovered is None
                assert (
                    database.scalar(
                        "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "database_deadlock"
                )
        finally:
            database.close()


@pytest.mark.parametrize(
    "rejection",
    [
        "job_write",
        "job_and_failure_write",
        "failure_write_deadlock",
        "connection_lost",
    ],
)
def test_rejected_job_write_fails_only_that_job(
    database_url: str, archive_server, monkeypatch, rejection
) -> None:
    # On 2026-10-03 PostgreSQL refused one job's Reset evidence and the whole
    # worker exited. A refused write now fails or retries only its own job.
    from pathlib import Path

    body = (
        Path(__file__).parents[1] / "testdata/legend_i_profile_v1.json"
    ).read_bytes()
    message = "reset evidence does not match paired Reset observations"
    with domain_database(database_url) as connection_info:
        _, rejected_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="rejected-write",
            endpoint="profile",
            body=body,
            observed_at=datetime(2026, 8, 3, 19, 36, 1, tzinfo=UTC),
            normalized_tag="#2PP",
        )
        _, other_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="other-write",
            endpoint="profile",
            body=body,
            observed_at=datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC),
            normalized_tag="#2PP",
        )
        database = Database(connection_info)
        try:
            complete_profile = ingestion.complete_profile
            refresh_evidence = reset_baselines._refresh_reset_baseline_evidence

            def reject(*_args, **_kwargs):
                if rejection == "connection_lost":
                    raise psycopg.OperationalError("connection lost")
                with database.pool.connection() as connection:
                    connection.execute(
                        f"DO $$ BEGIN RAISE EXCEPTION '{message}'; END $$"
                    )

            def complete_or_reject(database_, claim, profile):
                if claim.job_id == rejected_job:
                    reject()
                complete_profile(database_, claim, profile)

            def refresh_or_reject(database_, connection, claim, **kwargs):
                if claim.job_id == rejected_job:
                    if rejection == "failure_write_deadlock":
                        raise psycopg.errors.DeadlockDetected("deadlock detected")
                    reject()
                refresh_evidence(database_, connection, claim, **kwargs)

            monkeypatch.setattr(ingestion, "complete_profile", complete_or_reject)
            if rejection in {"job_and_failure_write", "failure_write_deadlock"}:
                monkeypatch.setattr(
                    reset_baselines,
                    "_refresh_reset_baseline_evidence",
                    refresh_or_reject,
                )

            def run():
                return process_concurrently(
                    _processor(database, archive_server),
                    concurrency=2,
                    owner="worker",
                    max_jobs=10,
                )

            if rejection == "connection_lost":
                # Losing the database still stops the worker for a restart.
                with pytest.raises(RuntimeError):
                    run()
                return
            results = run()

            def job(column: str, job_id: int):
                return database.scalar(
                    f"SELECT {column} FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )

            assert (
                ProcessResult(rejected_job, "retrying", "database_rejected") in results
            )
            assert job("status", other_job) == "complete"
            if rejection == "job_write":
                assert job("status", rejected_job) == "waiting_retry"
                assert job("failure_category", rejected_job) == "database_rejected"
                assert job("failure_detail", rejected_job) == message
            else:
                # The lease runs out and queue maintenance retries the job.
                assert job("status", rejected_job) == "leased"
                database.expire_lease(rejected_job)
                assert database.maintain_queue(max_jobs=1) == 1
                assert job("status", rejected_job) == "pending"
        finally:
            database.close()


def test_expired_refund_cannot_change_a_job_another_worker_took(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="expired-refund-fence",
                input_json=CURRENT_ANALYTICS_INPUT,
                max_attempts=3,
            )
        database = Database(connection_info)
        try:
            expired = database.claim_job(owner="expired-worker", job_id=job_id)
            assert expired is not None
            database.expire_lease(job_id)
            assert database.claim_job(owner="next-worker", job_id=job_id)
            with pytest.raises(LeaseLost):
                database.refund_claim_attempt(expired)
            database.expire_lease(job_id)
            assert database.maintain_queue(max_jobs=1) == 1
            with pytest.raises(LeaseLost):
                database.refund_claim_attempt(expired)
            assert (
                database.scalar(
                    "SELECT attempt_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == 2
            )
        finally:
            database.close()


def test_queue_health_reports_an_empty_active_queue(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            assert database.queue_health() == {
                "pending": 0,
                "waiting_retry": 0,
                "waiting_dependency": 0,
                "leased": 0,
                "failed": 0,
                "failed_count_capped": False,
                "oldest_due_seconds": None,
            }
        finally:
            database.close()


@pytest.mark.parametrize(
    ("endpoint", "parser_version", "claimable"),
    [
        (endpoint, parser_version, claimable)
        for endpoint in ("profile", "battle_log", "global_player_rankings")
        for parser_version, claimable in (
            ("supercell-source-parser-v1", True),
            ("supercell-source-parser-v2", True),
            ("supercell-source-parser-v99", False),
            # The corrected battle parser reads battle logs only.
            ("supercell-battle-parser-v3", endpoint == "battle_log"),
        )
    ],
)
def test_claim_job_applies_each_endpoint_parser_contract(
    database_url: str,
    archive_server,
    endpoint: str,
    parser_version: str,
    claimable: bool,
) -> None:
    with domain_database(database_url) as connection_info:
        _observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key=f"claim-contract:{endpoint}:{parser_version}",
            endpoint=endpoint,
            body=b"{}",
            observed_at=datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC),
            normalized_tag=None if endpoint == "global_player_rankings" else "#2PP",
            parser_version=parser_version,
        )
        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="source-contract-test", job_id=job_id)

            assert (claim is not None) is claimable
            if claim is not None:
                assert claim.endpoint == endpoint
                assert claim.parser_version == parser_version
                # Older worker images claim classes 1-6 only.
                assert (
                    database.scalar(
                        "SELECT claim_compatibility_version"
                        " FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == 7
                ) is (parser_version == "supercell-battle-parser-v3")
            else:
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "pending"
                )
        finally:
            database.close()


def test_battle_parser_migration_is_repeatable_and_changes_no_saved_work(
    database_url: str, archive_server
) -> None:
    from pathlib import Path

    migration = (
        Path(__file__).parents[2] / "deploy/migrations/0060_battle_parser_v3.sql"
    ).read_text(encoding="utf-8")
    with domain_database(database_url) as connection_info:
        for index, parser_version in enumerate(
            ("supercell-source-parser-v2", "supercell-battle-parser-v3")
        ):
            store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"migration-0060-{index}",
                endpoint="battle_log",
                body=b"{}",
                observed_at=datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC),
                normalized_tag="#2PP",
                parser_version=parser_version,
            )
        query = """
            SELECT parser_version, claim_compatibility_version, status
            FROM python_processing_jobs ORDER BY id
        """
        with psycopg.connect(connection_info, autocommit=True) as connection:
            before = connection.execute(query).fetchall()
            connection.execute(migration)
            assert connection.execute(query).fetchall() == before
        assert before == [
            ("supercell-source-parser-v2", 2, "pending"),
            ("supercell-battle-parser-v3", 7, "pending"),
        ]


def test_replay_observation_claim_carries_source_metadata_and_processes(
    database_url: str, archive_server
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_id = _insert_observation(
                connection, occurrence_key="replay-source-observation"
            )
            connection.execute(
                """
                INSERT INTO archive_catalogue (
                    response_hash, archive_reference, byte_size, archive_instance_id
                ) VALUES (%s, %s, 0, 'fixture-instance')
                ON CONFLICT (response_hash, archive_reference) DO NOTHING
                """,
                (archive_server[2], archive_server[1]),
            )
            connection.execute(
                """
                UPDATE collector_observations
                SET response_hash = %s, archive_reference = %s,
                    archive_catalogue_hash = %s
                WHERE id = %s
                """,
                (
                    archive_server[2],
                    archive_server[1],
                    archive_server[2],
                    observation_id,
                ),
            )
            connection.commit()
            job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:source-observation:v1",
                input_json={"replay_request_id": 1},
                replay_observation_id=observation_id,
            )

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="replay-claim-test", lease_seconds=30)
            assert claim is not None
            assert claim.job_id == job_id
            assert claim.work_type == "replay_observation"
            assert claim.observation_id == observation_id
            assert claim.normalized_tag == "#2PP"
            assert claim.endpoint == "profile"
            assert claim.endpoint_version == "profile-v1"
            assert claim.schema_version == "profile-schema-v1"
            assert claim.response_hash == archive_server[2]
            assert claim.archive_reference == archive_server[1]
            assert claim.observed_at is not None
            assert claim.parser_version == PARSER_VERSION

            # Release the inspected claim so the processor can pick the same
            # job back up through its public claim path.
            database.expire_lease(job_id)
            result = _processor(database, archive_server).process_job(
                job_id, owner="replay-claim-test"
            )
            assert result is not None
            assert result.outcome == "processed"
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "complete"
            )
            assert database.scalar("SELECT count(*) FROM player_profile_versions") == 1
        finally:
            database.close()


def test_unsupported_work_types_stay_pending_and_are_not_claimed(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            # Unknown future work types cannot be inserted under the v2 check
            # constraints; drop them in this isolated schema so the claim
            # filter can be proven to ignore such rows.
            connection.execute(
                "ALTER TABLE python_processing_jobs "
                "DROP CONSTRAINT IF EXISTS python_processing_jobs_work_type_v2_check"
            )
            connection.execute(
                "ALTER TABLE python_processing_jobs "
                "DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v2_check"
            )
            connection.execute(
                "ALTER TABLE python_processing_jobs "
                "DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v5_check"
            )
            export_job_id = _insert_job(
                connection,
                work_type="build_export",
                deduplication_key="export:unsupported",
                input_json={"export_request_id": 7},
            )
            unknown_job_id = _insert_job(
                connection,
                work_type="future_work_type",
                deduplication_key="future:unsupported",
                input_json={},
            )
            observation_id = _insert_observation(
                connection, occurrence_key="supported-behind-unsupported"
            )
            supported_job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:supported-behind-unsupported",
                input_json={"replay_request_id": 2},
                replay_observation_id=observation_id,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-filter-test")
            assert claim is not None
            assert claim.job_id == supported_job_id
            for job_id in (export_job_id, unknown_job_id):
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "pending"
                )
        finally:
            database.close()


def test_direct_job_id_claim_is_subject_to_the_supported_contract_filter(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            export_job_id = _insert_job(
                connection,
                work_type="build_export",
                deduplication_key="export:direct",
                input_json={"export_request_id": 9},
            )
            observation_id = _insert_observation(
                connection, occurrence_key="direct-supported"
            )
            supported_job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:direct",
                input_json={"replay_request_id": 3},
                replay_observation_id=observation_id,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            denied = database.claim_job(
                owner="direct-unsupported", job_id=export_job_id
            )
            assert denied is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                == "pending"
            )
            allowed = database.claim_job(
                owner="direct-supported", job_id=supported_job_id
            )
            assert allowed is not None
            assert allowed.job_id == supported_job_id
        finally:
            database.close()


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("processing_version", "processing-v99"),
        ("domain_rule_version", "domain-rules-v99"),
        ("parser_version", "supercell-source-parser-v99"),
    ],
)
def test_claim_skips_observation_jobs_with_unsupported_versions(
    database_url: str,
    column: str,
    replacement: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_id = _insert_observation(
                connection, occurrence_key=f"unsupported-{column}"
            )
            job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key=f"unsupported:{column}",
                input_json={},
                observation_id=observation_id,
            )
            connection.execute(
                f"UPDATE python_processing_jobs SET {column} = %s WHERE id = %s",
                (replacement, job_id),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-version-test")
            assert claim is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_claim_skips_analytics_build_with_unsupported_analytics_rule_version(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:unsupported-version",
                input_json=CURRENT_ANALYTICS_INPUT,
                analytics_rule_version="analytics-v99",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-analytics-test")
            assert claim is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_claim_skips_reconciliation_with_unsupported_analytics_rule_version(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _insert_observation(
                connection, occurrence_key="reconcile-unsupported-analytics"
            )
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            job_id = _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:unsupported-analytics",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                analytics_rule_version="analytics-v99",
                due_at="2026-08-03T19:35:01+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-reconcile-test")
            assert claim is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_repair_outlasting_its_lease_completes_on_its_first_attempt(
    database_url: str, monkeypatch
) -> None:
    # On 2026-10-02 a repair ran for minutes with a 60 s lease. It was rolled
    # back at completion, retried from scratch and failed after three tries.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _insert_observation(connection, occurrence_key="long-repair-source")
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            job_id = _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:long-repair",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                due_at="2026-08-03T19:35:01+00:00",
                max_attempts=3,
            )

        database = Database(connection_info)
        try:
            taken_while_running: list[object] = []

            def slow_rebuild(*_args, **_kwargs) -> None:
                time.sleep(1.5)
                taken_while_running.append(database.maintain_queue(max_jobs=10))
                taken_while_running.append(
                    database.claim_job(owner="other-lane", job_id=job_id)
                )

            monkeypatch.setattr(
                reconciliation_db, "recalculate_ranked_day", slow_rebuild
            )
            result = ObservationProcessor(database, archive=object()).process_job(
                job_id, owner="slow-repair", lease_seconds=1
            )

            assert taken_while_running == [0, None]
            assert result == ProcessResult(job_id, "processed")
            assert (
                database.scalar(
                    "SELECT status || ':' || attempt_count FROM python_processing_jobs "
                    "WHERE id = %s",
                    (job_id,),
                )
                == "complete:1"
            )
        finally:
            database.close()


def test_stuck_repair_is_cancelled_and_retried_without_using_its_last_attempt(
    database_url: str, monkeypatch
) -> None:
    # The 2026-10-03 audit held a lock a repair needed: the repair waited
    # forever, kept its job row locked, and looked healthy the whole time.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _insert_observation(connection, occurrence_key="stuck-repair-source")
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            job_id = _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:stuck-repair",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                due_at="2026-08-03T19:35:01+00:00",
                max_attempts=1,
            )

        def stuck_rebuild(_database, connection, **_kwargs) -> None:
            connection.execute(
                "UPDATE players SET active = true WHERE id = %s", (player_id,)
            )
            connection.execute("SELECT pg_advisory_xact_lock(4242)")

        monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", stuck_rebuild)
        database = Database(connection_info, statement_timeout_seconds=1)
        results: list[ProcessResult | None] = []
        try:
            with psycopg.connect(connection_info, autocommit=True) as holder:
                holder.execute("SELECT pg_advisory_lock(4242)")
                lane = threading.Thread(
                    target=lambda: results.append(
                        ObservationProcessor(database, archive=object()).process_job(
                            job_id, owner="stuck-repair", lease_seconds=1
                        )
                    ),
                    daemon=True,
                )
                lane.start()
                lane.join(timeout=15)
                stuck = lane.is_alive()
            lane.join(timeout=15)

            assert not stuck, "the stuck repair was never cancelled"
            assert results == [ProcessResult(job_id, "retrying", "database_timeout")]
            # Its partial work was rolled back and its only attempt is unused.
            assert (
                database.scalar(
                    "SELECT active FROM players WHERE id = %s", (player_id,)
                )
                is False
            )
            time.sleep(1.1)
            assert database.maintain_queue(max_jobs=10) == 1
            assert (
                database.scalar(
                    "SELECT status || ':' || attempt_count FROM python_processing_jobs "
                    "WHERE id = %s",
                    (job_id,),
                )
                == "pending:0"
            )
        finally:
            database.close()


def test_reconcile_snapshot_and_analytics_jobs_with_current_versions_are_claimable(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _insert_observation(connection, occurrence_key="claim-build-source")
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:claimable",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                due_at="2026-08-03T19:35:01+00:00",
            )
            _insert_job(
                connection,
                work_type="build_snapshot",
                deduplication_key="snapshot:claimable",
                input_json={
                    "boundary_at": "2026-08-03T05:00:00Z",
                    "generation": 1,
                    "manifest_id": 1,
                    "manifest_digest": "a" * 64,
                },
                due_at="2026-08-03T19:35:02+00:00",
            )
            _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:claimable",
                input_json=CURRENT_ANALYTICS_INPUT,
                due_at="2026-08-03T19:35:03+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            first = database.claim_job(owner="claim-reconcile")
            assert first is not None and first.work_type == "reconcile_ranked_day"
            second = database.claim_job(owner="claim-snapshot")
            assert second is not None and second.work_type == "build_snapshot"
            third = database.claim_job(owner="claim-analytics")
            assert third is not None and third.work_type == "build_analytics"
        finally:
            database.close()


def test_legacy_analytics_job_with_relabeled_version_but_legacy_input_stays_pending(
    database_url: str,
) -> None:
    # The migration relabels legacy analytics jobs to the current analytics
    # rule version but leaves their legacy input_json shape untouched. Such
    # jobs must stay pending and unclaimed; only complete current-shape input
    # is claimable by this image.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            selection_legacy_job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:legacy-selection",
                input_json={
                    "selection": {"ranked_day_version_id": 1},
                    "generation": 1,
                    "manifest_id": 1,
                    "manifest_digest": "a" * 64,
                },
                analytics_rule_version=ANALYTICS_RULE_VERSION,
                due_at="2026-08-03T19:35:01+00:00",
            )
            v1_legacy_job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:legacy-v1",
                input_json={
                    "snapshot_id": 1,
                    "generation": 1,
                    "manifest_id": 1,
                    "manifest_digest": "a" * 64,
                },
                analytics_rule_version=ANALYTICS_RULE_VERSION,
                due_at="2026-08-03T19:35:02+00:00",
            )
            current_job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:current-shape",
                input_json=CURRENT_ANALYTICS_INPUT,
                analytics_rule_version=ANALYTICS_RULE_VERSION,
                due_at="2026-08-03T19:35:03+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="legacy-analytics-test")
            assert claim is not None
            assert claim.job_id == current_job_id
            for job_id in (selection_legacy_job_id, v1_legacy_job_id):
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "pending"
                )
        finally:
            database.close()


def test_explicit_live_class_outranks_aged_army_backfill(
    database_url: str,
) -> None:
    with _production_database(
        database_url, include_army_migrations=True
    ) as connection_info:
        with psycopg.connect(connection_info) as connection:
            backfill_job_id = _insert_job(
                connection,
                work_type="redecode_army",
                deduplication_key="historical-army-backfill",
                input_json={"battle_ids": [1]},
                priority=25,
                analytics_rule_version="army-analytics-v2",
            )
            connection.execute(
                "UPDATE python_processing_jobs "
                "SET created_at = clock_timestamp() - interval '2 hours' "
                "WHERE id = %s",
                (backfill_job_id,),
            )
            live_observation_id = _insert_observation(
                connection, occurrence_key="live-before-army-backfill"
            )
            live_job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="live-before-army-backfill",
                input_json={},
                observation_id=live_observation_id,
                priority=100,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            live_claim = database.claim_job(owner="live-before-backfill")
            assert live_claim is not None and live_claim.job_id == live_job_id
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (backfill_job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_low_priority_job_eventually_outranks_a_fresh_high_priority_job(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            old_observation_id = _insert_observation(
                connection, occurrence_key="priority-old"
            )
            fresh_observation_id = _insert_observation(
                connection, occurrence_key="priority-fresh"
            )
            old_job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="priority:old",
                input_json={},
                observation_id=old_observation_id,
                priority=100,
                due_at="2026-08-03T19:35:01+00:00",
            )
            fresh_job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="priority:fresh",
                input_json={},
                observation_id=fresh_observation_id,
                priority=300,
            )
            connection.execute(
                "UPDATE python_processing_jobs "
                "SET created_at = clock_timestamp() - interval '2 hours' "
                "WHERE id = %s",
                (old_job_id,),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            # An old low-priority job outranks a fresh high-priority job.
            first = database.claim_job(owner="priority-old-first")
            assert first is not None
            assert first.job_id == old_job_id
            # A fresh high-priority job still outranks any remaining fresh job.
            second = database.claim_job(owner="priority-fresh-second")
            assert second is not None
            assert second.job_id == fresh_job_id
        finally:
            database.close()


def test_claim_order_tie_breaks_by_due_at_then_id(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_ids = [
                _insert_observation(connection, occurrence_key=f"tie-{label}")
                for label in ("a", "b", "c")
            ]
            later_due = "2026-08-03T19:35:02+00:00"
            earlier_due = "2026-08-03T19:35:01+00:00"
            job_ids = [
                _insert_job(
                    connection,
                    work_type="process_observation",
                    deduplication_key=f"tie:{label}",
                    input_json={},
                    observation_id=observation_id,
                    due_at=due_at,
                )
                for label, observation_id, due_at in (
                    ("a", observation_ids[0], earlier_due),
                    ("b", observation_ids[1], later_due),
                    ("c", observation_ids[2], later_due),
                )
            ]
            # Equal creation time so the priority + age score ties.
            connection.execute(
                "UPDATE python_processing_jobs "
                "SET created_at = clock_timestamp() "
                "WHERE id = ANY(%s::bigint[])",
                (job_ids,),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            first = database.claim_job(owner="tie-one")
            assert first is not None and first.job_id == job_ids[0]
            second = database.claim_job(owner="tie-two")
            assert second is not None and second.job_id == job_ids[1]
            third = database.claim_job(owner="tie-three")
            assert third is not None and third.job_id == job_ids[2]
        finally:
            database.close()


def test_claim_does_not_sweep_expired_unsupported_lease_but_maintenance_does(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            export_job_id = _insert_job(
                connection,
                work_type="build_export",
                deduplication_key="export:expired-lease",
                input_json={"export_request_id": 11},
                max_attempts=2,
            )
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET status = 'leased', lease_owner = 'retired-worker',
                    lease_token = 'retired-token',
                    lease_expires_at = clock_timestamp() - interval '1 minute',
                    attempt_count = 2, updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (export_job_id,),
            )
            connection.execute(
                """
                INSERT INTO python_processing_attempts (
                    job_id, attempt_number, lease_owner, lease_token,
                    started_at, lease_expires_at, state
                ) VALUES (
                    %s, 1, 'retired-worker', 'retired-token',
                    clock_timestamp() - interval '2 minutes',
                    clock_timestamp() - interval '1 minute', 'running'
                )
                """,
                (export_job_id,),
            )
            observation_id = _insert_observation(
                connection, occurrence_key="cleanup-supported"
            )
            supported_job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:cleanup",
                input_json={"replay_request_id": 4},
                replay_observation_id=observation_id,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="cleanup-test")
            assert claim is not None
            assert claim.job_id == supported_job_id
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                == "leased"
            )
            assert database.maintain_queue(max_jobs=100) == 1
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                == "pending"
            )
            assert (
                database.scalar(
                    "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                is None
            )
            assert (
                database.scalar(
                    "SELECT lease_owner FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                is None
            )
            assert (
                database.scalar(
                    "SELECT state FROM python_processing_attempts WHERE job_id = %s",
                    (export_job_id,),
                )
                == "stale"
            )
        finally:
            database.close()


def test_limited_claims_never_take_or_skip_past_other_work(database_url: str) -> None:
    # Older responses in every claimable state sit ahead of one daily job.
    # A derived-only claim takes the daily job and leaves every response
    # untouched; a response-only claim then reaches each of them.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            response_ids = []
            for state, priority in (
                ("pending", 100),
                ("pending", 60),
                ("waiting_retry", 100),
                ("waiting_dependency", 100),
                ("leased", 100),
            ):
                key = f"limited:{state}:{priority}"
                response_ids.append(
                    _insert_job(
                        connection,
                        work_type="process_observation",
                        deduplication_key=key,
                        input_json={},
                        observation_id=_insert_observation(
                            connection, occurrence_key=key
                        ),
                        priority=priority,
                        due_at="2026-08-01T00:00:00Z",
                        max_attempts=3,
                    )
                )
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET status = %s, attempt_count = 1,
                        lease_owner = CASE WHEN %s = 'leased' THEN 'gone' END,
                        lease_token = CASE WHEN %s = 'leased' THEN 'gone' END,
                        lease_expires_at = CASE WHEN %s = 'leased'
                            THEN clock_timestamp() - interval '1 minute' END
                    WHERE id = %s
                    """,
                    (state, state, state, state, response_ids[-1]),
                )
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            daily_id = _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="limited:daily",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                due_at="2026-08-03T19:35:01+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            derived = database.claim_job(
                owner="derived-lane", work_types=DERIVED_WITHOUT_BUILDS
            )
            assert derived is not None and derived.job_id == daily_id
            assert (
                database.claim_job(
                    owner="build-lane", work_types=POPULATION_BUILD_WORK_TYPES
                )
                is None
            )
            assert (
                database.claim_job(
                    owner="build-lane",
                    job_id=response_ids[0],
                    work_types=POPULATION_BUILD_WORK_TYPES,
                )
                is None
            )
            assert database.scalar(
                """
                SELECT count(*) FROM python_processing_jobs AS job
                WHERE job.id = ANY(%s) AND (job.attempt_count <> 1
                   OR EXISTS (SELECT 1 FROM python_processing_attempts AS attempt
                              WHERE attempt.job_id = job.id))
                """,
                (response_ids,),
            ) == 0
            claimed = {
                claim.job_id
                for claim in (
                    database.claim_job(
                        owner="response-lane", work_types=RESPONSE_WORK_TYPES
                    )
                    for _ in response_ids
                )
                if claim is not None
            }
            assert claimed == set(response_ids)
        finally:
            database.close()
