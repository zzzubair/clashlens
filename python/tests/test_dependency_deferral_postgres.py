"""Issue #64: archive/capacity deferrals are non-consuming and claimable."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain_test_support import domain_database, store_observation

from clashlens.db import Database

DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)


def _seed_pair(connection_info: str, archive_server) -> tuple[int, int]:
    observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key="dependency-deferral",
        endpoint="profile",
        body=b"{}",
        observed_at=DAY_START + timedelta(hours=1),
        normalized_tag="#2PP",
    )
    return observation_id, job_id


def test_archive_deferral_is_nonconsuming_and_claimable(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            _observation_id, job_id = _seed_pair(connection_info, archive_server)
            claim = database.claim_job(owner="lane-defer", lease_seconds=30)
            assert claim is not None and claim.job_id == job_id

            outcome = database.fail_claim(
                claim,
                category="archive_unavailable",
                detail="provider outage",
                retryable=True,
            )
            assert outcome == "waiting_dependency"

            with database.pool.connection() as connection:
                state, attempt_count, deferrals, due_at = connection.execute(
                    """
                    SELECT status AS state, attempt_count, dependency_deferral_count, due_at
                    FROM python_processing_jobs WHERE id = %s
                    """,
                    (job_id,),
                ).fetchone()
            assert state == "waiting_dependency"
            assert attempt_count == 1, "deferrals must not consume the retry budget beyond the live attempt"
            assert deferrals >= 1
            assert due_at is not None, "deferred work must re-due after backoff"

            # A second deferral keeps accumulating without consuming attempts.
            claim = database.claim_job(owner="lane-defer", lease_seconds=30)
            if claim is None:
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE python_processing_jobs SET due_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                        (job_id,),
                    )
                claim = database.claim_job(owner="lane-defer", lease_seconds=30)
            assert claim is not None and claim.job_id == job_id
            outcome = database.fail_claim(
                claim,
                category="degraded_capacity",
                detail="spool full",
                retryable=True,
            )
            assert outcome == "waiting_dependency"
            with database.pool.connection() as connection:
                deferrals, attempt_count = connection.execute(
                    "SELECT dependency_deferral_count, attempt_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()
            assert deferrals == 2
            assert attempt_count == 1, "dependency deferrals do not consume the ordinary attempt budget"

            # Even after the ordinary retry budget is exhausted, dependency
            # work remains claimable and records another distinct attempt row.
            # The exhausted budget itself stays untouched: a dependency claim
            # neither consumes nor replenishes ordinary attempts.
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET attempt_count = max_attempts, due_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                    (job_id,),
                )
                exhausted = connection.execute(
                    "SELECT attempt_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()[0]
            assert exhausted == 3
            claim = database.claim_job(owner="lane-defer", lease_seconds=30)
            assert claim is not None and claim.job_id == job_id
            assert claim.attempt_number >= 3
            with database.pool.connection() as connection:
                attempt_count, deferrals = connection.execute(
                    "SELECT attempt_count, dependency_deferral_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()
            assert attempt_count == 3, "dependency claims leave the exhausted ordinary budget untouched"
            assert deferrals == 2
        finally:
            database.close()


def test_dependency_deferrals_do_not_exhaust_the_ordinary_retry_budget(
    database_url: str, archive_server
) -> None:
    """The ordinary budget counts ordinary attempt slots only.

    With max_attempts=3 and two dependency deferrals interleaved before the
    second ordinary slot is granted, every run still receives the full three
    ordinary slots: a resumed dependency run re-uses its original slot, so an
    ordinary failure after two deferrals must stay retryable. A decision based
    on attempt_number would have failed that run early (3 < 3 is false).
    """
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            _observation_id, job_id = _seed_pair(connection_info, archive_server)
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET max_attempts = 3 WHERE id = %s",
                    (job_id,),
                )

            def re_due() -> None:
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE python_processing_jobs SET due_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                        (job_id,),
                    )

            # Ordinary slot 1 (attempt_count -> 1), then deferred.
            claim = database.claim_job(owner="lane-budget", lease_seconds=30)
            assert claim is not None and claim.job_id == job_id
            assert not claim.is_dependency_resume
            with database.pool.connection() as connection:
                count = connection.execute(
                    "SELECT attempt_count FROM python_processing_jobs WHERE id = %s", (job_id,)
                ).fetchone()[0]
            assert count == 1
            assert (
                database.fail_claim(
                    claim,
                    category="archive_unavailable",
                    detail="outage",
                    retryable=True,
                )
                == "waiting_dependency"
            )

            # Deferred resume 1: no budget change, attempts sequence advances.
            re_due()
            claim = database.claim_job(owner="lane-budget", lease_seconds=30)
            assert claim is not None and claim.is_dependency_resume
            assert claim.attempt_count == 1 and claim.attempt_number == 2
            assert (
                database.fail_claim(
                    claim,
                    category="degraded_capacity",
                    detail="spool full",
                    retryable=True,
                )
                == "waiting_dependency"
            )

            # Deferred resume 2 fails with an ORDINARY error: it re-uses slot
            # 1's ordinal, so with two slots remaining it must stay retryable.
            re_due()
            claim = database.claim_job(owner="lane-budget", lease_seconds=30)
            assert claim is not None and claim.is_dependency_resume
            assert claim.attempt_count == 1 and claim.attempt_number == 3
            assert (
                database.fail_claim(
                    claim,
                    category="transient_parse_failure",
                    detail="parser hiccup",
                    retryable=True,
                )
                == "waiting_retry"
            )

            # Ordinary slots 2 and 3 are still granted in full.
            re_due()
            claim = database.claim_job(owner="lane-budget", lease_seconds=30)
            assert claim is not None and not claim.is_dependency_resume
            assert claim.attempt_count == 1
            assert (
                database.fail_claim(
                    claim,
                    category="transient_parse_failure",
                    detail="parser hiccup",
                    retryable=True,
                )
                == "waiting_retry"
            )
            re_due()
            claim = database.claim_job(owner="lane-budget", lease_seconds=30)
            assert claim is not None and not claim.is_dependency_resume
            assert (
                database.fail_claim(
                    claim,
                    category="transient_parse_failure",
                    detail="parser hiccup",
                    retryable=True,
                )
                == "failed"
            )

            with database.pool.connection() as connection:
                state, attempt_count, deferrals, attempt_rows = connection.execute(
                    """
                    SELECT status AS state, attempt_count,
                           dependency_deferral_count,
                           (SELECT count(*) FROM python_processing_attempts WHERE job_id = %s)
                    FROM python_processing_jobs WHERE id = %s
                    """,
                    (job_id, job_id),
                ).fetchone()
            assert state == "failed"
            assert attempt_count == 3, "exactly the full ordinary budget was consumed"
            assert deferrals == 2
            assert attempt_rows == 5, "every run including deferrals is recorded"
        finally:
            database.close()


def test_terminal_category_still_consumes_retry_budget(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            _observation_id, job_id = _seed_pair(connection_info, archive_server)
            claim = database.claim_job(owner="lane-term", lease_seconds=30)
            assert claim is not None and claim.job_id == job_id

            result = database.fail_claim(
                claim,
                category="malformed_json",
                detail="bad body",
                retryable=False,
            )
            assert result == "failed"
            with database.pool.connection() as connection:
                state, attempt_count = connection.execute(
                    "SELECT status AS state, attempt_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()
            assert state == "failed"
            assert attempt_count >= 1
        finally:
            database.close()
