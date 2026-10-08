from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database

from clashlens.collector_db import CollectorDatabase, ResponseHandoff
from clashlens.collector_uploads import claim_upload, complete_upload, fail_upload
from clashlens.db import (
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)
from clashlens.operator_recovery import retry_failed_item


def test_health_metrics_survive_restart_and_separate_failed_uploads(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                """INSERT INTO players (normalized_tag, active, next_due_at)
                VALUES ('#2PP', true, clock_timestamp() + interval '1 minute')
                RETURNING id"""
            ).fetchone()[0]

        completed_at = datetime.now(UTC) - timedelta(seconds=2)
        response_hash = hashlib.sha256(b"body").hexdigest()
        database = CollectorDatabase(connection_info)
        success = ResponseHandoff(
            occurrence_key="metrics-response",
            scope="player",
            identity_key="#2PP",
            endpoint="profile",
            player_id=int(player_id),
            normalized_tag="#2PP",
            request_started_at=completed_at - timedelta(seconds=1),
            response_completed_at=completed_at,
            http_status=200,
            response_hash=response_hash,
            content_fingerprint=response_hash,
            byte_size=4,
            spool_key=f"sha256/{response_hash[:2]}/{response_hash}",
            collector_version="metrics-test",
            key_label="regular-a",
            evidence_headers={"content-type": "application/json"},
        )
        database.record_response(success)
        failed_hash = hashlib.sha256(b"provider failure").hexdigest()
        database.record_response(
            replace(
                success,
                occurrence_key="metrics-provider-failure",
                request_started_at=completed_at + timedelta(seconds=1),
                response_completed_at=completed_at + timedelta(seconds=2),
                http_status=403,
                response_hash=failed_hash,
                content_fingerprint=failed_hash,
                byte_size=16,
                spool_key=f"sha256/{failed_hash[:2]}/{failed_hash}",
            )
        )

        before = database.health_metrics()
        assert before["pending_uploads"] == 2
        assert before["failed_uploads"] == 0
        assert before["last_success_age_seconds"] >= 1
        assert before["oldest_pending_processing_age_seconds"] < 600
        assert before["oldest_pending_upload_age_seconds"] < 600
        assert "newest_failed_processing_age_seconds" not in before
        assert "newest_failed_upload_age_seconds" not in before
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                UPDATE collector_observations
                SET created_at = created_at - CASE WHEN http_status = 200
                    THEN interval '40 minutes' ELSE interval '20 minutes' END
                """
            )
            connection.execute(
                "UPDATE collector_response_uploads SET created_at = created_at - interval '2 hours'"
            )
        for status in ("pending", "waiting_retry", "waiting_dependency", "leased"):
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET status = %s, due_at = clock_timestamp() + interval '5 minutes',
                        lease_owner = CASE WHEN %s THEN 'metrics-test' END,
                        lease_token = CASE WHEN %s THEN 'metrics-token' END,
                        lease_expires_at = CASE WHEN %s THEN clock_timestamp() + interval '1 minute' END
                    """,
                    (status, status == "leased", status == "leased", status == "leased"),
                )
            waiting = database.health_metrics()
            assert waiting["pending_processing"] == 2
            if status == "pending":
                # Not due for five minutes, so not waiting yet.
                assert waiting["oldest_pending_processing_age_seconds"] == 0
            else:
                assert waiting["oldest_pending_processing_age_seconds"] >= 2400
            assert waiting["oldest_pending_upload_age_seconds"] >= 7200
        # Once due, a pending job waits from its due time, not from when it was saved.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET status = 'pending', due_at = clock_timestamp() - interval '10 minutes',
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                """
            )
        due = database.health_metrics()
        assert 600 <= due["oldest_pending_processing_age_seconds"] < 1200
        for status in ("complete", "failed", "cancelled"):
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET status = %s, lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL
                    """,
                    (status,),
                )
            finished = database.health_metrics()
            assert finished["pending_processing"] == 0
            assert finished["oldest_pending_processing_age_seconds"] == 0
            if status == "failed":
                assert finished["newest_failed_processing_age_seconds"] < 600

        claim = claim_upload(database, owner="metrics-test")
        assert claim is not None
        fail_upload(database, claim, category="archive_unavailable", detail="offline")
        retryable = database.health_metrics()
        assert retryable["pending_uploads"] == 2
        assert retryable["failed_uploads"] == 0

        terminal = claim_upload(
            database,
            owner="metrics-terminal",
            now=datetime.now(UTC) + timedelta(seconds=6),
        )
        assert terminal is not None
        fail_upload(
            database,
            terminal,
            category="archive_checksum_mismatch",
            detail="terminal",
            retryable=False,
        )
        database.close()

        reopened = CollectorDatabase(connection_info)
        after = reopened.health_metrics()
        reopened.close()
        assert after["pending_uploads"] == 1
        assert after["failed_uploads"] == 1
        assert after["newest_failed_upload_age_seconds"] < 600
        assert after["oldest_pending_upload_age_seconds"] >= 7200
        assert after["last_success_age_seconds"] >= before["last_success_age_seconds"]


def test_metrics_include_jobs_without_observations_or_successful_fetches(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = CollectorDatabase(connection_info)
        try:
            metrics = database.health_metrics()
            assert "active_players" in metrics
            assert "last_success_age_seconds" not in metrics
            assert metrics["oldest_pending_processing_age_seconds"] == 0
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag) VALUES ('#2PP')"
                )
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json, created_at, due_at
                    )
                    SELECT 'reconcile_ranked_day', 'metrics-without-observation',
                           jsonb_build_object('player_id', id,
                               'ranked_day_start', '2026-10-01T05:00:00Z'),
                           clock_timestamp() - interval '1 hour',
                           clock_timestamp() + interval '5 minutes'
                    FROM players
                    """
                )
            for status in ("pending", "waiting_retry", "waiting_dependency", "leased"):
                with psycopg.connect(connection_info) as connection:
                    connection.execute(
                        """
                        UPDATE python_processing_jobs
                        SET status = %s,
                            lease_owner = CASE WHEN %s THEN 'metrics-test' END,
                            lease_token = CASE WHEN %s THEN 'metrics-token' END,
                            lease_expires_at = CASE WHEN %s THEN clock_timestamp() + interval '1 minute' END
                        """,
                        (status, status == "leased", status == "leased", status == "leased"),
                    )
                metrics = database.health_metrics()
                assert metrics["pending_processing"] == 1
                if status == "pending":
                    # Not due for five minutes, so it is not waiting yet.
                    assert metrics["oldest_pending_processing_age_seconds"] == 0
                    assert metrics["oldest_job_reconcile_ranked_day_age_seconds"] == 0
                else:
                    assert metrics["oldest_pending_processing_age_seconds"] >= 3600
                    assert metrics["oldest_job_reconcile_ranked_day_age_seconds"] >= 3600
                assert "last_success_age_seconds" not in metrics
            # Once due, a pending job waits from its due time, not its creation.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET status = 'pending', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        due_at = clock_timestamp() - interval '20 minutes'
                    """
                )
            metrics = database.health_metrics()
            assert 1200 <= metrics["oldest_job_reconcile_ranked_day_age_seconds"] < 3600
            # An older build is left out of the processing age.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json, created_at, due_at,
                        processing_version, domain_rule_version,
                        analytics_rule_version, parser_version
                    ) VALUES ('build_army_analytics', 'metrics-build',
                              %s::jsonb, clock_timestamp() - interval '2 hours',
                              clock_timestamp() - interval '2 hours',
                              %s, %s, %s, 'supercell-source-parser-v1')
                    """,
                    (
                        json.dumps(
                            {"generation": 1, "manifest_id": 1, "manifest_digest": "a" * 64}
                        ),
                        PROCESSING_VERSION,
                        DOMAIN_RULE_VERSION,
                        ARMY_ANALYTICS_RULE_VERSION,
                    ),
                )
            metrics = database.health_metrics()
            assert metrics["pending_processing"] == 2
            assert 1200 <= metrics["oldest_pending_processing_age_seconds"] < 3600
            assert metrics["oldest_job_build_army_analytics_age_seconds"] >= 7200
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET status = 'complete', lease_owner = NULL,
                        lease_token = NULL, lease_expires_at = NULL
                    """
                )
            metrics = database.health_metrics()
            assert metrics["pending_processing"] == 0
            assert metrics["oldest_pending_processing_age_seconds"] == 0
        finally:
            database.close()


def test_upload_clocks_ignore_repeat_sightings_and_restart_for_fresh_uploads(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "INSERT INTO players (normalized_tag) VALUES ('#2PP') RETURNING id"
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
        response_hash = hashlib.sha256(b"clock body").hexdigest()
        database = CollectorDatabase(connection_info)

        def sight(label: str) -> None:
            completed_at = datetime.now(UTC)
            fingerprint = hashlib.sha256(label.encode()).hexdigest()
            database.record_response(
                ResponseHandoff(
                    occurrence_key=label,
                    scope="player",
                    identity_key="#2PP",
                    endpoint="profile",
                    player_id=int(player_id),
                    normalized_tag="#2PP",
                    request_started_at=completed_at - timedelta(seconds=1),
                    response_completed_at=completed_at,
                    http_status=200,
                    response_hash=response_hash,
                    content_fingerprint=fingerprint,
                    byte_size=10,
                    spool_key=f"sha256/{response_hash[:2]}/{response_hash}",
                    collector_version="metrics-test",
                    key_label="regular-a",
                    evidence_headers={"content-type": "application/json"},
                )
            )

        def backdate(column: str, days: int) -> None:
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    f"UPDATE collector_response_uploads SET {column} = "
                    f"{column} - %s * interval '1 day'",
                    (days,),
                )

        try:
            sight("clock-first")
            claim = claim_upload(database, owner="clock-test")
            assert claim is not None
            fail_upload(
                database,
                claim,
                category="archive_unavailable",
                detail="offline",
                retryable=False,
            )
            backdate("updated_at", 2)
            backdate("created_at", 2)
            # Seeing a two-day-old failure's bytes again is not a new failure.
            sight("clock-failed-again")
            metrics = database.health_metrics()
            assert metrics["failed_uploads"] == 1
            assert metrics["newest_failed_upload_age_seconds"] >= 86400

            # An operator retry keeps the upload's original wait.
            with psycopg.connect(connection_info) as connection:
                retried = retry_failed_item(
                    connection, upload_hash=response_hash, apply=True
                )
            assert retried["outcome"] == "requeued"
            metrics = database.health_metrics()
            assert metrics["pending_uploads"] == 1
            assert metrics["oldest_pending_upload_age_seconds"] >= 2 * 86400

            # Bytes first saved 96 days ago return after their archive copy
            # retired; the fresh upload has not waited 96 days.
            claim = claim_upload(database, owner="clock-test")
            assert claim is not None
            complete_upload(
                database,
                claim,
                archive_reference="s3://evidence/clock",
                archive_instance_id="fixture-instance",
            )
            backdate("created_at", 96)
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE archive_catalogue SET availability = 'expired'"
                )
            sight("clock-retired")
            metrics = database.health_metrics()
            assert metrics["pending_uploads"] == 1
            assert metrics["oldest_pending_upload_age_seconds"] < 600
        finally:
            database.close()


def test_health_metrics_count_saved_responses(database_url: str) -> None:
    # The alert check turns this into responses saved a minute in the Reset
    # hour; on 7 Oct 2026 a stalled collector saved 17-44 a minute.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                """INSERT INTO players (normalized_tag, active, next_due_at)
                VALUES ('#2PP', true, clock_timestamp() + interval '1 minute')
                RETURNING id"""
            ).fetchone()[0]
        database = CollectorDatabase(connection_info)
        try:
            assert database.health_metrics()["responses_saved_last_minute"] == 0
            completed_at = datetime.now(UTC)
            for index in range(3):
                body_hash = hashlib.sha256(f"body {index}".encode()).hexdigest()
                database.record_response(
                    ResponseHandoff(
                        occurrence_key=f"saved-{index}",
                        scope="player",
                        identity_key="#2PP",
                        endpoint="profile",
                        player_id=int(player_id),
                        normalized_tag="#2PP",
                        request_started_at=completed_at - timedelta(seconds=1),
                        response_completed_at=completed_at,
                        http_status=200,
                        response_hash=body_hash,
                        content_fingerprint=body_hash,
                        byte_size=6,
                        spool_key=f"sha256/{body_hash[:2]}/{body_hash}",
                        collector_version="metrics-test",
                        key_label="regular-a",
                        evidence_headers={"content-type": "application/json"},
                    )
                )
                if index == 0:
                    # Saves that roll back still use up ids, as on 7 Oct 2026.
                    with psycopg.connect(connection_info) as connection:
                        connection.execute(
                            "SELECT nextval(pg_get_serial_sequence('collector_observations', 'id'))"
                            " FROM generate_series(1, 100)"
                        )
            assert database.health_metrics()["responses_saved_last_minute"] == 3
        finally:
            database.close()


def test_metrics_show_finished_work_reset_work_left_and_old_failures(
    database_url: str,
) -> None:
    # The early warning needs to see work that waits while nothing of its kind
    # finishes, how much Reset work is left, and failures nobody has fixed.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = CollectorDatabase(connection_info)
        try:
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag) VALUES ('#2PP'), ('#8QQ'), ('#9RR')"
                )
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json, priority, created_at, due_at
                    )
                    SELECT 'reconcile_ranked_day', 'metrics-progress-' || normalized_tag,
                           jsonb_build_object('player_id', id,
                               'ranked_day_start', '2026-10-01T05:00:00Z'),
                           300, clock_timestamp(), clock_timestamp()
                    FROM players
                    """
                )
            metrics = database.health_metrics()
            assert metrics["reset_work_remaining"] == 3
            assert metrics["waiting_job_reconcile_ranked_day_age_seconds"] < 60
            assert metrics["completed_jobs_2m"] == 0
            assert "completed_job_reconcile_ranked_day_2m" not in metrics
            assert "oldest_failed_processing_age_seconds" not in metrics

            # Running, retried, waiting for the archive or retried by an
            # operator, the work still waits: its clock runs from when it was
            # created, not from its claim or its next try.
            for state, lease, due in (
                ("leased", "1 hour", "-1 minute"),
                ("waiting_retry", None, "5 minutes"),
                ("waiting_dependency", None, "5 minutes"),
                ("pending", None, "0 minutes"),
            ):
                with psycopg.connect(connection_info) as connection:
                    connection.execute(
                        """
                        UPDATE python_processing_jobs
                        SET status = %(state)s, attempt_count = 1,
                            lease_owner = CASE WHEN %(lease)s::interval IS NOT NULL
                                               THEN 'busy-lane' END,
                            lease_token = CASE WHEN %(lease)s::interval IS NOT NULL
                                               THEN 'busy-token' END,
                            lease_expires_at = clock_timestamp() + %(lease)s::interval,
                            created_at = clock_timestamp() - interval '10 minutes',
                            due_at = clock_timestamp() + %(due)s::interval
                        """,
                        {"state": state, "lease": lease, "due": due},
                    )
                metrics = database.health_metrics()
                assert metrics["waiting_job_reconcile_ranked_day_age_seconds"] >= 600

            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs
                    SET status = CASE deduplication_key
                        WHEN 'metrics-progress-#2PP' THEN 'complete' ELSE 'failed' END,
                        updated_at = CASE deduplication_key
                        WHEN 'metrics-progress-#2PP' THEN clock_timestamp()
                        WHEN 'metrics-progress-#8QQ' THEN clock_timestamp() - interval '3 days'
                        ELSE clock_timestamp() - interval '1 hour' END
                    """
                )
            metrics = database.health_metrics()
            assert metrics["reset_work_remaining"] == 0
            assert "waiting_job_reconcile_ranked_day_age_seconds" not in metrics
            assert metrics["completed_jobs_2m"] == 1
            assert metrics["completed_job_reconcile_ranked_day_2m"] == 1
            assert metrics["failed_processing"] == 2
            assert metrics["oldest_failed_processing_age_seconds"] >= 3 * 86400
            assert 3600 <= metrics["newest_failed_processing_age_seconds"] < 7200

            # Finished three minutes ago is no longer recent progress.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET updated_at = clock_timestamp()"
                    " - interval '3 minutes' WHERE status = 'complete'"
                )
            metrics = database.health_metrics()
            assert metrics["completed_jobs_2m"] == 0
            assert "completed_job_reconcile_ranked_day_2m" not in metrics
        finally:
            database.close()


def test_a_failed_job_whose_response_a_replay_processed_stays_repaired(
    database_url: str,
) -> None:
    # The failed job keeps its state; only the outstanding count drops.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = CollectorDatabase(connection_info)
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute("SET session_replication_role = replica")
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json, observation_id,
                        replay_observation_id, status
                    ) VALUES
                        ('process_observation', 'failed-9100', '{}', 9100, NULL, 'failed'),
                        ('process_observation', 'failed-9101', '{}', 9101, NULL, 'failed')
                    """
                )
            assert database.health_metrics()["failed_processing"] == 2
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute("SET session_replication_role = replica")
                # The replay finishes its job and records a processed result.
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json, replay_observation_id,
                        status, completed_at
                    ) VALUES ('replay_observation', 'replay-9100',
                              '{"replay_request_id": 1}', 9100, 'complete', clock_timestamp())
                    """
                )
                connection.execute(
                    """
                    INSERT INTO observation_processing_outcomes (
                        observation_id, parser_version, processing_version, endpoint,
                        response_hash, source_http_status, source_observed_at, outcome
                    ) SELECT 9100, parser_version, processing_version, 'profile',
                             repeat('a', 64), 200, clock_timestamp(), 'processed'
                    FROM python_processing_jobs WHERE deduplication_key = 'replay-9100'
                    """
                )
            assert database.health_metrics()["failed_processing"] == 1
            # The finished-job cleanup removes the replay job; the repair stays.
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute("SET session_replication_role = replica")
                connection.execute(
                    "DELETE FROM python_processing_jobs WHERE deduplication_key = 'replay-9100'"
                )
                states = connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE observation_id = 9100"
                ).fetchall()
            assert states == [("failed",)]
            assert database.health_metrics()["failed_processing"] == 1
        finally:
            database.close()
