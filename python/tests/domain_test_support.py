from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo


def text(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value


@contextmanager
def domain_database(database_url: str) -> Iterator[str]:
    schema = f"python_domain_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        root = Path(__file__).parents[2]
        migrations_dir = root / "deploy" / "migrations"
        sql_files = sorted(migrations_dir.glob("*.sql"))
        with psycopg.connect(connection_info, autocommit=True) as connection:
            for path in sql_files:
                connection.execute(path.read_text(encoding="utf-8"))
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@contextmanager
def _connection_scope(connection_info: str, existing: Any | None):
    if existing is not None:
        yield existing
    else:
        with psycopg.connect(connection_info) as connection:
            yield connection


def store_observation(
    connection_info: str,
    archive_server: tuple[str, str, str, Any],
    *,
    occurrence_key: str,
    endpoint: str,
    body: bytes,
    observed_at: datetime,
    normalized_tag: str | None,
    http_status: int = 200,
    parser_version: str | None = None,
    processing_version: str | None = None,
    domain_rule_version: str | None = None,
    work_type: str = "process_observation",
    deduplication_key: str | None = None,
    input_json: str = "{}",
    max_attempts: int = 3,
    existing_connection: Any | None = None,
    commit: bool = True,
) -> tuple[int, int]:
    digest = hashlib.sha256(body).hexdigest()
    key = f"sha256/{digest[:2]}/{digest}"
    archive_server[3].objects[key] = body
    reference = f"s3://evidence/{key}"
    global_scope = endpoint == "global_player_rankings"
    collector_work_type = (
        "global_player_rankings" if global_scope else "initial_collection"
    )
    scope = "global" if global_scope else "player"
    with _connection_scope(connection_info, existing_connection) as connection:
        player_id = None
        if normalized_tag is not None:
            player_id = connection.execute(
                """
                INSERT INTO players (normalized_tag, active, next_due_at)
                VALUES (%s, false, NULL)
                ON CONFLICT (normalized_tag) DO UPDATE
                    SET normalized_tag = EXCLUDED.normalized_tag
                RETURNING id
                """,
                (normalized_tag,),
            ).fetchone()[0]
        collector_job_id = connection.execute(
            """
            INSERT INTO collector_jobs (
                work_type, player_id, normalized_tag, scope, capacity_pool, priority,
                due_at, coalescing_key, status, required_endpoint
            ) VALUES (%s, %s, %s, %s, 'normal', 300, %s, %s, 'complete', %s)
            RETURNING id
            """,
            (
                collector_work_type,
                player_id,
                normalized_tag,
                scope,
                observed_at,
                occurrence_key,
                endpoint,
            ),
        ).fetchone()[0]
        attempt_id = connection.execute(
            """
            INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
            VALUES (%s, 'complete', %s, %s)
            RETURNING id
            """,
            (collector_job_id, observed_at, observed_at),
        ).fetchone()[0]
        # Migration 0009 requires every new observation to reference a verified
        # catalogue row, so the fixture emulates a contract-v3 collector.
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
        connection.execute(
            """
            INSERT INTO archive_catalogue (
                response_hash, archive_reference, byte_size, archive_instance_id
            ) VALUES (%s, %s, %s, 'fixture-instance')
            ON CONFLICT (response_hash) DO NOTHING
            """,
            (digest, reference, len(body)),
        )
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, collection_job_id, attempt_id, player_id,
                scope, normalized_tag, endpoint, request_started_at, response_completed_at,
                http_status, response_hash, archive_reference, archive_catalogue_hash,
                collector_version, key_label, evidence_headers
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s - interval '1 second', %s,
                %s, %s, %s, %s, 'collector-v2', 'normal-a', '{}'::jsonb
            )
            RETURNING id
            """,
            (
                occurrence_key,
                collector_job_id,
                attempt_id,
                player_id,
                scope,
                normalized_tag,
                endpoint,
                observed_at,
                observed_at,
                http_status,
                digest,
                reference,
                digest,
            ),
        ).fetchone()[0]
        columns = [
            "observation_id",
            "work_type",
            "input_json",
            "max_attempts",
        ]
        values: list[Any] = [observation_id, work_type, input_json, max_attempts]
        if deduplication_key is not None:
            columns.append("deduplication_key")
            values.append(deduplication_key)
        if parser_version is not None:
            columns.append("parser_version")
            values.append(parser_version)
        if processing_version is not None:
            columns.append("processing_version")
            values.append(processing_version)
        if domain_rule_version is not None:
            columns.append("domain_rule_version")
            values.append(domain_rule_version)
        placeholders = ", ".join(["%s"] * len(values))
        job_id = connection.execute(
            f"""
            INSERT INTO python_processing_jobs ({", ".join(columns)})
            VALUES ({placeholders})
            RETURNING id
            """,
            tuple(values),
        ).fetchone()[0]
        if commit:
            connection.commit()
    return int(observation_id), int(job_id)
