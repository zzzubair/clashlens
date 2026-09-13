from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb


def text(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def enable_direct_army_fixture(database: Any) -> None:
    """Exercise historical army calculation through a v5-shaped fixture job."""
    original_complete = database.complete_army_analytics

    def enqueue(connection: Any, *, ranked_day_start: datetime) -> None:
        if getattr(database, "_suppress_fixture_enqueue", False):
            return
        ranked_day_start = ranked_day_start.astimezone(UTC)
        completed = connection.execute(
            """
            SELECT id, official_season_id
            FROM ranked_day_versions
            WHERE ranked_day_start = %s AND state = 'Complete' AND coverage_complete
            ORDER BY id DESC LIMIT 1
            """,
            (ranked_day_start,),
        ).fetchone()
        if completed is None:
            return
        decode_generation = connection.execute(
            """
            SELECT COALESCE(max(decode.id), 0)
            FROM legend_battles AS battle
            LEFT JOIN battle_army_decodes AS decode
              ON decode.battle_id = battle.id AND decode.is_active
             AND decode.decoder_version = 'army-decoder-v2'
             AND decode.catalog_version = 'unit-catalog-v1'
            WHERE battle.ranked_day_start = %s
            """,
            (ranked_day_start,),
        ).fetchone()[0]
        day_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                work_type, deduplication_key, input_json,
                processing_version, domain_rule_version,
                analytics_rule_version, due_at
            ) VALUES ('build_army_analytics', %s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                f"build_army_analytics:{day_text}:{completed[0]}:{decode_generation}",
                Jsonb(
                    {
                        "ranked_day_start": day_text,
                        "official_season_id": str(completed[1]),
                        "generation": 1,
                        "manifest_id": 1,
                        "manifest_digest": "a" * 64,
                    }
                ),
                "clashlens-domain-processing-v1",
                "clashlens-domain-rules-v1",
                "army-analytics-v2",
            ),
        )

    def complete(claim: Any) -> None:
        direct_input = {
            key: value
            for key, value in claim.input_json.items()
            if key not in {"generation", "manifest_id", "manifest_digest"}
        }
        original_complete(replace(claim, input_json=direct_input))

    database._enqueue_army_analytics = enqueue
    database.complete_army_analytics = complete


@contextmanager
def domain_database(
    database_url: str, *, include_coordinator: bool = False
) -> Iterator[str]:
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
            ON CONFLICT (response_hash, archive_reference) DO NOTHING
            """,
            (digest, reference, len(body)),
        )
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, player_id, scope, normalized_tag,
                endpoint, request_started_at, response_completed_at, http_status,
                response_hash, archive_reference, archive_catalogue_hash,
                collector_version, key_label, evidence_headers
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s - interval '1 second', %s, %s, %s, %s, %s,
                'collector-v2', 'normal-a', '{}'::jsonb
            )
            RETURNING id
            """,
            (
                occurrence_key,
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
