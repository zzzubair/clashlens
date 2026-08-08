from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo

from clashlens import cli
from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor


def _text(value: object) -> object:
    return value.decode("utf-8") if isinstance(value, bytes) else value


@contextmanager
def _production_database(database_url: str) -> Iterator[tuple[str, str]]:
    schema = f"production_bridge_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            root = Path(__file__).parents[2]
            for name in ("0001_collector.sql", "0002_python_layer.sql"):
                migration = (root / "deploy" / "migrations" / name).read_text(
                    encoding="utf-8"
                )
                connection.execute(migration)
        yield connection_info, schema
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _seed_production_profile(
    connection_info: str,
    *,
    archive_reference: str,
    response_hash: str,
) -> int:
    observed_at = datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC)
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            """
            INSERT INTO players (normalized_tag, active, next_due_at)
            VALUES ('#2PP', false, NULL)
            RETURNING id
            """
        ).fetchone()[0]
        collector_job_id = connection.execute(
            """
            INSERT INTO collector_jobs (
                work_type, player_id, normalized_tag, capacity_pool,
                priority, due_at, coalescing_key, status
            ) VALUES (
                'initial_collection', %s, '#2PP', 'interactive',
                300, %s, 'production-profile-bridge', 'complete'
            )
            RETURNING id
            """,
            (player_id, observed_at),
        ).fetchone()[0]
        attempt_id = connection.execute(
            """
            INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
            VALUES (%s, 'complete', %s, %s)
            RETURNING id
            """,
            (collector_job_id, observed_at, observed_at),
        ).fetchone()[0]
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, collection_job_id, attempt_id, player_id,
                normalized_tag, endpoint, request_started_at, response_completed_at,
                http_status, response_hash, archive_reference, collector_version,
                key_label, evidence_headers
            ) VALUES (
                'production-profile-bridge:profile', %s, %s, %s,
                '#2PP', 'profile', %s, %s, 200, %s, %s,
                'collector-v1', 'normal-a', '{}'::jsonb
            )
            RETURNING id
            """,
            (
                collector_job_id,
                attempt_id,
                player_id,
                observed_at,
                observed_at,
                response_hash,
                archive_reference,
            ),
        ).fetchone()[0]
        job_id = connection.execute(
            """
            INSERT INTO python_processing_jobs (observation_id)
            VALUES (%s)
            RETURNING id
            """,
            (observation_id,),
        ).fetchone()[0]
        connection.commit()
        return int(job_id)


def test_production_profile_job_activates_legend_player_and_makes_it_due(
    database_url: str,
    archive_server,
) -> None:
    with _production_database(database_url) as (connection_info, _schema):
        readiness = cli.main(
            [
                "ready",
                "--database-url",
                connection_info,
                "--expected-contract-version",
                "2",
                "--archive-endpoint",
                archive_server[0],
                "--archive-bucket",
                "evidence",
                "--archive-access-key",
                "fixture-access",
                "--archive-secret-key",
                "fixture-secret",
                "--archive-insecure-test-only",
            ]
        )
        assert readiness == 0

        job_id = _seed_production_profile(
            connection_info,
            archive_reference=archive_server[1],
            response_hash=archive_server[2],
        )
        database = Database(connection_info)
        try:
            processor = ObservationProcessor(
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
            result = processor.process_once(owner="production-profile-test")
            assert result is not None
            assert result.job_id == job_id
            assert result.outcome == "processed"

            with database.pool.connection() as connection:
                player = connection.execute(
                    """
                    SELECT active, eligibility_state, next_due_at,
                           current_profile_version_id, current_observed_at
                    FROM players
                    WHERE normalized_tag = '#2PP'
                    """
                ).fetchone()
                job = connection.execute(
                    """
                    SELECT status, outcome, lease_owner, lease_token, lease_expires_at
                    FROM python_processing_jobs
                    WHERE id = %s
                    """,
                    (job_id,),
                ).fetchone()
                versions = connection.execute(
                    "SELECT count(*) FROM player_profile_versions"
                ).fetchone()[0]
                effects = connection.execute(
                    "SELECT count(*) FROM player_profile_effects"
                ).fetchone()[0]
                attempts = connection.execute(
                    """
                    SELECT count(*)
                    FROM python_processing_attempts
                    WHERE job_id = %s AND state = 'complete'
                    """,
                    (job_id,),
                ).fetchone()[0]

            assert player is not None
            assert player[0] is True
            assert _text(player[1]) == "eligible"
            assert player[2] is not None
            assert player[3] is not None
            assert player[4] is not None
            assert _text(job[0]) == "complete"
            assert _text(job[1]) == "processed"
            assert job[2:] == (None, None, None)
            assert versions == 1
            assert effects == 1
            assert attempts == 1
        finally:
            database.close()
