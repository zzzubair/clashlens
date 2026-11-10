"""Shared setup for collector uploads postgres tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database
from psycopg import sql

from clashlens.collector_db import ResponseHandoff

NOW = datetime(2020, 9, 13, 4, 0, tzinfo=UTC)


@contextmanager
def upload_database(database_url: str) -> Iterator[str]:
    with domain_database(database_url) as connection_info:
        # Upload admission normally uses the database clock. Seed its due time
        # on the same controlled timeline as claim/renew/complete below, even
        # when the real clock has passed every date in this test file.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                sql.SQL(
                    "ALTER TABLE collector_response_uploads "
                    "ALTER COLUMN next_attempt_at SET DEFAULT {}"
                ).format(sql.Literal(NOW))
            )
        yield connection_info


def _archive_instance(connection_info: str) -> None:
    with psycopg.connect(connection_info) as connection:
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


def _handoff(
    *,
    occurrence_key: str,
    response_hash: str,
    player_id: int,
    tag: str = "#2PP",
    endpoint: str = "profile",
    completed_at: datetime = NOW,
    collector_work_id: int | None = None,
    content_fingerprint: str | None = None,
) -> ResponseHandoff:
    return ResponseHandoff(
        occurrence_key=occurrence_key,
        scope="player",
        identity_key=tag,
        endpoint=endpoint,
        player_id=player_id,
        normalized_tag=tag,
        request_started_at=completed_at - timedelta(seconds=1),
        response_completed_at=completed_at,
        http_status=200,
        response_hash=response_hash,
        content_fingerprint=content_fingerprint or response_hash,
        byte_size=1,
        spool_key=f"sha256/{response_hash[:2]}/{response_hash}",
        collector_version="python-collector-test",
        key_label="regular-a",
        evidence_headers={"content-type": "application/json"},
        collector_work_id=collector_work_id,
    )
