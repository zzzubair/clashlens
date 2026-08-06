from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).parents[2]


@contextmanager
def migrated_production_database(database_url: str) -> Iterator[str]:
    schema = f"python_api_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                (ROOT / "deploy/migrations/0001_collector.sql").read_text(
                    encoding="utf-8"
                )
            )
            migration_0002 = (
                ROOT / "deploy/migrations/0002_python_layer.sql"
            ).read_text(encoding="utf-8")
            connection.execute(migration_0002)
            # The production migration is the only authoritative 0002 path and
            # must be safe to apply again.
            connection.execute(migration_0002)
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_python_production_migration_is_reentrant_and_enforces_identity_uniqueness(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            first_account = connection.execute(
                """
                INSERT INTO clash_lens_accounts (
                    public_id, username, normalized_username, display_name
                ) VALUES (%s, 'PlayerOne', 'playerone', 'Player One')
                RETURNING id
                """,
                (uuid4(),),
            ).fetchone()[0]
            second_account = connection.execute(
                """
                INSERT INTO clash_lens_accounts (
                    public_id, username, normalized_username, display_name
                ) VALUES (%s, 'PlayerTwo', 'playertwo', 'Player Two')
                RETURNING id
                """,
                (uuid4(),),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO account_provider_identities (account_id, provider, provider_subject)
                VALUES (%s, 'google', 'google-subject-one')
                """,
                (first_account,),
            )
            connection.commit()

            with pytest.raises(psycopg.errors.UniqueViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO account_provider_identities (
                            account_id, provider, provider_subject
                        ) VALUES (%s, 'google', 'google-subject-one')
                        """,
                        (second_account,),
                    )

            columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name IN (
                          'private_api_requests',
                          'player_link_verification_audits'
                      )
                    """
                )
            }
            assert "player_token" not in columns
            assert "body_hash" not in columns
            assert "token_hash" not in columns


def test_python_job_observation_is_nullable_only_for_checked_non_observation_work(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    observation_id, work_type, deduplication_key, input_json
                ) VALUES (
                    NULL, 'build_export', 'export:00000000-0000-4000-8000-000000000029',
                    '{"export_request_id":29}'::jsonb
                )
                """
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO python_processing_jobs (
                            observation_id, work_type, deduplication_key
                        ) VALUES (NULL, 'process_observation', 'invalid-null-observation')
                        """
                    )
