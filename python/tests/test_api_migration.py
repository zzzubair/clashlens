from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).parents[2]


def text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    assert isinstance(value, str)
    return value


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


@contextmanager
def migrated_populated_v1_production_database(database_url: str) -> Iterator[str]:
    schema = f"python_api_populated_v1_{uuid4().hex}"
    migration_0001 = (ROOT / "deploy/migrations/0001_collector.sql").read_text(
        encoding="utf-8"
    )
    migration_0002 = (ROOT / "deploy/migrations/0002_python_layer.sql").read_text(
        encoding="utf-8"
    )
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(migration_0001)
            player_id = connection.execute(
                """
                INSERT INTO players (normalized_tag, active)
                VALUES ('#2PP', true)
                RETURNING id
                """
            ).fetchone()[0]
            sweep_id = connection.execute(
                """
                INSERT INTO collector_reset_sweeps (boundary_at)
                VALUES ('2026-08-06T05:00:00Z')
                RETURNING id
                """
            ).fetchone()[0]
            job_id = connection.execute(
                """
                INSERT INTO collector_jobs (
                    work_type, player_id, normalized_tag, capacity_pool, priority,
                    due_at, coalescing_key, sweep_id, status
                ) VALUES (
                    'reset_profile', %s, '#2PP', 'normal', 10,
                    '2026-08-06T05:00:00Z', 'populated-v1-reset-profile', %s, 'complete'
                )
                RETURNING id
                """,
                (player_id, sweep_id),
            ).fetchone()[0]
            attempt_id = connection.execute(
                """
                INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
                VALUES (%s, 'complete', '2026-08-06T05:00:00Z', '2026-08-06T05:00:01Z')
                RETURNING id
                """,
                (job_id,),
            ).fetchone()[0]
            observation_id = connection.execute(
                """
                INSERT INTO collector_observations (
                    occurrence_key, collection_job_id, attempt_id, player_id,
                    normalized_tag, endpoint, request_started_at, response_completed_at,
                    http_status, response_hash, archive_reference, collector_version,
                    key_label, evidence_headers
                ) VALUES (
                    'populated-v1-observation', %s, %s, %s, '#2PP', 'profile',
                    '2026-08-06T05:00:00Z', '2026-08-06T05:00:01Z', 200,
                    %s, 's3://evidence/populated-v1', 'collector-v1', 'key-v1', '{}'::jsonb
                )
                RETURNING id
                """,
                (job_id, attempt_id, None, "a" * 64),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO collector_transport_failures (
                    collection_job_id, attempt_id, player_id, normalized_tag,
                    endpoint, request_started_at, failed_at, failure_category,
                    retry_state, key_label
                ) VALUES (
                    %s, %s, NULL, '#2PP', 'profile',
                    '2026-08-06T05:00:00Z', '2026-08-06T05:00:01Z',
                    'transport', 'waiting_retry', 'key-v1'
                )
                """,
                (job_id, attempt_id),
            )
            connection.execute(
                "INSERT INTO python_processing_jobs (observation_id) VALUES (%s)",
                (observation_id,),
            )
            connection.execute(migration_0002)
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

            support_role = connection.execute(
                """
                SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole,
                       rolcreatedb, rolreplication, rolbypassrls
                FROM pg_roles
                WHERE rolname = 'clashlens_support_transfer'
                """
            ).fetchone()
            assert support_role == (True, False, False, False, False, False, False)
            support_function_row = connection.execute(
                """
                SELECT to_regprocedure(
                    current_schema() || '.clashlens_support_transfer(uuid,text,uuid,uuid,text,text)'
                )
                """
            ).fetchone()
            assert support_function_row is not None
            support_function = text(support_function_row[0])
            assert (
                connection.execute(
                    """
                SELECT has_function_privilege(
                    'public', %s::regprocedure, 'EXECUTE'
                )
                """,
                    (support_function,),
                ).fetchone()[0]
                is False
            )
            support_privileges = connection.execute(
                """
                SELECT p.prosecdef,
                       owner.rolname,
                       p.proconfig,
                       has_function_privilege(
                           'clashlens_support_transfer', p.oid, 'EXECUTE'
                       ),
                       has_table_privilege(
                           'clashlens_support_transfer',
                           format('%%I.%%I', current_schema(), 'players'),
                           'SELECT'
                       ),
                       has_table_privilege(
                           'clashlens_support_transfer',
                           format('%%I.%%I', current_schema(), 'verified_player_links'),
                           'UPDATE'
                       ),
                       has_table_privilege(
                           'clashlens_support_transfer',
                           format(
                               '%%I.%%I',
                               current_schema(),
                               'support_player_link_transfer_candidates'
                           ),
                           'UPDATE'
                       ),
                       has_table_privilege(
                           'clashlens_support_transfer',
                           format(
                               '%%I.%%I',
                               current_schema(),
                               'support_player_link_transfer_audits'
                           ),
                           'INSERT'
                       )
                FROM pg_proc AS p
                JOIN pg_roles AS owner ON owner.oid = p.proowner
                WHERE p.oid = %s::regprocedure
                """,
                (support_function,),
            ).fetchone()
            assert support_privileges is not None
            assert support_privileges[0] is True
            assert text(support_privileges[1]) != "clashlens_support_transfer"
            function_settings = tuple(
                text(setting) for setting in (support_privileges[2] or [])
            )
            assert any(
                setting.startswith("search_path=pg_catalog")
                for setting in function_settings
            )
            assert all("$user" not in setting for setting in function_settings)
            assert support_privileges[3] is True
            assert support_privileges[4:] == (False, False, False, False)


def test_support_function_denies_runtime_role_families(database_url: str) -> None:
    role_names = tuple(
        f"clashlens_{name}_test_{uuid4().hex[:8]}"
        for name in ("browser", "api", "worker", "bot", "collector")
    )
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            function_row = connection.execute(
                """
                SELECT to_regprocedure(
                    current_schema() || '.clashlens_support_transfer(uuid,text,uuid,uuid,text,text)'
                )
                """
            ).fetchone()
            assert function_row is not None
            support_function = text(function_row[0])
            for role_name in role_names:
                connection.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role_name))
                )
            connection.commit()
            try:
                privileges = [
                    connection.execute(
                        """
                        SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')
                        """,
                        (role_name, support_function),
                    ).fetchone()
                    for role_name in role_names
                ]
                assert all(row is not None and row[0] is False for row in privileges)
            finally:
                connection.rollback()
                for role_name in role_names:
                    connection.execute(
                        sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name))
                    )
                connection.commit()


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


def test_python_migration_repeats_after_populated_version_one_rows(
    database_url: str,
) -> None:
    with migrated_populated_v1_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT version FROM clash_lens_contract WHERE singleton"
                ).fetchone()[0]
                == 2
            )
            job = connection.execute(
                """
                SELECT work_type, scope, reset_baseline_sweep_id, player_id
                FROM collector_jobs
                WHERE coalescing_key = 'populated-v1-reset-profile'
                """
            ).fetchone()
            assert job is not None
            assert (text(job[0]), text(job[1])) == ("legacy_reset_profile", "player")
            assert job[2] is not None
            observation_player_id = connection.execute(
                """
                SELECT player_id
                FROM collector_observations
                WHERE occurrence_key = 'populated-v1-observation'
                """
            ).fetchone()[0]
            failure_player_id = connection.execute(
                """
                SELECT player_id
                FROM collector_transport_failures
                WHERE normalized_tag = '#2PP'
                """
            ).fetchone()[0]
            assert observation_player_id == job[3]
            assert failure_player_id == observation_player_id
            processing = connection.execute(
                """
                SELECT work_type, deduplication_key, input_json
                FROM python_processing_jobs
                """
            ).fetchone()
            assert processing is not None
            assert text(processing[0]) == "process_observation"
            assert isinstance(text(processing[1]), str)
            assert text(processing[1]).startswith("process-observation:")
            assert processing[2] == {}
