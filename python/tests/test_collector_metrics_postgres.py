from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database

from clashlens.collector_db import CollectorDatabase, ResponseHandoff
from clashlens.collector_uploads import claim_upload, fail_upload


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
            assert waiting["oldest_pending_processing_age_seconds"] >= 2400
            assert waiting["oldest_pending_upload_age_seconds"] >= 7200
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
                assert metrics["oldest_pending_processing_age_seconds"] >= 3600
                assert "last_success_age_seconds" not in metrics
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
