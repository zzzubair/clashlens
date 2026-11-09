"""Shared setup for claim jobs postgres tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from domain_test_support import apply_migration
from psycopg.conninfo import make_conninfo

from clashlens.archive import S3ArchiveReader
from clashlens.db import (
    Database,
)
from clashlens.worker import (
    ObservationProcessor,
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
