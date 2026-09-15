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
        assert after["last_success_age_seconds"] >= before["last_success_age_seconds"]
