from datetime import UTC, datetime

import psycopg
import pytest
from claim_jobs_postgres_support import (
    CURRENT_ANALYTICS_INPUT,
    _insert_job,
    _processor,
    _production_database,
)
from domain_test_support import domain_database, store_observation

from clashlens import claimed_jobs, ingestion, reset_baselines
from clashlens.db import Database
from clashlens.worker import ProcessResult, process_concurrently


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
@pytest.mark.parametrize("reject_failure_write", [False, True])
@pytest.mark.parametrize(
    ("refund_conflicts", "refund_outlasts_lease"),
    [(0, False), (2, False), (2, True), (3, False), (3, True)],
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

    refund_exhausted = reject_failure_write and refund_conflicts == 3
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
            refund_claim_attempt = claimed_jobs.refund_claim_attempt

            def reject_refund(connection, claim):
                nonlocal refund_conflicts
                if refund_conflicts:
                    refund_conflicts -= 1
                    if refund_outlasts_lease and not refund_conflicts:
                        # Stand in for conflicts delayed past the 60 s lease.
                        database.expire_lease(job_id)
                    reject_transaction()
                refund_claim_attempt(connection, claim)

            with monkeypatch.context() as injected:
                injected.setattr(ingestion, "complete_profile", reject_transaction)
                if reject_failure_write:
                    injected.setattr(
                        claimed_jobs, "refund_claim_attempt", reject_refund
                    )
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
            ) == (2 if reject_failure_write and not refund_exhausted else 3)
            assert database.scalar(
                "SELECT status FROM python_processing_jobs WHERE id = %s",
                (job_id,),
            ) == ("leased" if reject_failure_write else "failed")
            assert database.scalar("SELECT count(*) FROM player_profile_versions") == 0

            database.expire_lease(job_id)
            assert database.maintain_queue(max_jobs=1) == int(reject_failure_write)
            recovered = processor.process_job(job_id, owner="recovery-worker")
            if refund_exhausted:
                assert recovered is None
                assert (
                    database.scalar(
                        "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "lease_expired_max_attempts"
                )
            elif reject_failure_write:
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


def test_refund_conflicts_stop_and_leave_the_lease_to_recovery(
    database_url, monkeypatch
):
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            job_id = _insert_job(
                connection,
                work_type="build_analytics",
                input_json=CURRENT_ANALYTICS_INPUT,
                deduplication_key="bounded-conflict-refund",
                max_attempts=3,
            )
        database = Database(connection_info)
        try:
            for _ in range(2):
                assert database.claim_job(owner="previous", job_id=job_id)
                database.expire_lease(job_id)
                assert database.maintain_queue() == 1
            claim = database.claim_job(owner="conflicted", job_id=job_id)
            assert claim is not None
            refund = claimed_jobs.refund_claim_attempt
            conflicts = 0

            def reject_refund(connection, claim):
                nonlocal conflicts
                conflicts += 1
                if conflicts <= 3:
                    raise psycopg.errors.DeadlockDetected()
                refund(connection, claim)

            monkeypatch.setattr(claimed_jobs, "refund_claim_attempt", reject_refund)
            claimed_jobs.release_conflicted(database, claim, "database_deadlock")
            assert (
                database.scalar(
                    "SELECT attempt_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == 3
            )
            database.expire_lease(job_id)
            assert database.maintain_queue() == 1
            assert (
                database.scalar(
                    "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "lease_expired_max_attempts"
            )
            assert database.claim_job(owner="recovery", job_id=job_id) is None
        finally:
            database.close()
