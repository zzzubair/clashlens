from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from api_migration_support import (
    ROOT,
    migrated_production_database,
    text,
)
from domain_test_support import apply_migration
from psycopg import sql
from psycopg.conninfo import make_conninfo


def test_shared_key_rate_upgrade_preserves_state_and_outstanding_permissions(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            for migration in sorted((ROOT / "deploy/migrations").glob("*.sql")):
                version = int(migration.name.split("_", 1)[0])
                if 9 <= version <= 40:
                    connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute(
                """
                INSERT INTO shared_api_credentials
                    (credential_fingerprint, state, cooldown_until, quarantine_reason)
                VALUES (repeat('a', 64), 'active', NULL, NULL),
                       (repeat('b', 64), 'cooldown', clock_timestamp() + interval '1 minute', NULL),
                       (repeat('c', 64), 'quarantined', NULL, 'fixture'),
                       (repeat('d', 64), 'retired', NULL, NULL)
                """
            )
            connection.execute(
                """
                INSERT INTO shared_api_permits (credential_fingerprint, caller)
                SELECT repeat('a', 64), 'collector' FROM generate_series(1, 29)
                UNION ALL SELECT repeat('a', 64), 'python'
                """
            )
            connection.execute(
                (ROOT / "deploy/migrations/0041_shared_api_key_rate.sql").read_text(
                    encoding="utf-8"
                )
            )

            assert connection.execute(
                "SELECT count(*) FROM shared_api_credentials WHERE collector_budget = 24 AND python_budget = 1 AND total_budget = 25"
            ).fetchone() == (4,)
            assert connection.execute(
                "SELECT count(*) FROM shared_api_permits"
            ).fetchone() == (30,)
            for fingerprint, state in [
                ("a", "active"),
                ("b", "cooldown"),
                ("c", "quarantined"),
                ("d", "retired"),
            ]:
                assert connection.execute(
                    "SELECT granted, credential_state = %s FROM clashlens_acquire_shared_api_permit(%s, 'python')",
                    (state, fingerprint * 64),
                ).fetchone() == (False, True)
            with pytest.raises(psycopg.errors.CheckViolation):
                connection.execute(
                    "UPDATE shared_api_credentials SET total_budget = 30, collector_budget = 29"
                )
            connection.execute(
                "INSERT INTO shared_api_credentials (credential_fingerprint) VALUES (repeat('e', 64))"
            )
            assert connection.execute(
                "SELECT total_budget FROM shared_api_credentials WHERE credential_fingerprint = repeat('e', 64)"
            ).fetchone() == (25,)


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


def test_provider_identities_permit_discord_once_per_account_and_subject(
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
                INSERT INTO account_provider_identities (
                    account_id, provider, provider_subject
                ) VALUES (%s, 'google', 'google-subject-one')
                """,
                (first_account,),
            )
            connection.execute(
                """
                INSERT INTO account_provider_identities (
                    account_id, provider, provider_subject
                ) VALUES (%s, 'discord', 'discord-subject-two')
                """,
                (second_account,),
            )
            connection.commit()

            with pytest.raises(psycopg.errors.UniqueViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO account_provider_identities (
                            account_id, provider, provider_subject
                        ) VALUES (%s, 'discord', 'discord-subject-two')
                        """,
                        (first_account,),
                    )
            with pytest.raises(psycopg.errors.UniqueViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO account_provider_identities (
                            account_id, provider, provider_subject
                        ) VALUES (%s, 'discord', 'another-discord-subject')
                        """,
                        (second_account,),
                    )

            audit_grants = connection.execute(
                """
                SELECT privilege_type FROM information_schema.role_table_grants
                WHERE table_name = 'provider_identity_audits'
                  AND grantee = 'clashlens_python_api'
                ORDER BY privilege_type
                """
            ).fetchall()
            assert [row[0] for row in audit_grants] == ["INSERT", "SELECT"]

            # Unlink needs exactly one narrow DELETE on the identity table,
            # granted to the API role and to no other runtime role.
            delete_boundary = connection.execute(
                """
                SELECT role.rolname,
                       has_table_privilege(
                           role.rolname,
                           format('%I.%I', current_schema(),
                                  'account_provider_identities'),
                           'DELETE'
                       )
                FROM pg_roles AS role
                WHERE role.rolname IN (
                    'clashlens_python_api',
                    'clashlens_collector',
                    'clashlens_python_worker'
                )
                ORDER BY role.rolname
                """
            ).fetchall()
            assert delete_boundary == [
                ("clashlens_collector", False),
                ("clashlens_python_api", True),
                ("clashlens_python_worker", False),
            ]


def test_each_migration_records_exactly_its_filename_version(database_url: str) -> None:
    schema = f"python_api_migration_versions_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            versions: set[int] = set()
            for migration in sorted((ROOT / "deploy/migrations").glob("*.sql")):
                version = int(migration.name.split("_", 1)[0])
                assert version not in versions, migration.name
                apply_migration(connection, migration.read_text(encoding="utf-8"))
                recorded_versions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM clash_lens_schema_migrations"
                    )
                }
                assert recorded_versions == versions | {version}, migration.name
                versions = recorded_versions
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_privileged_functions_pin_search_path_with_pg_temp_last(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            schema = text(connection.execute("SELECT current_schema()").fetchone()[0])
            expected = f"search_path=pg_catalog, {schema}, pg_temp"

            def unpinned() -> list[str]:
                rows = connection.execute(
                    """
                    SELECT p.oid::regprocedure::text, COALESCE(p.proconfig, '{}')
                    FROM pg_proc AS p
                    WHERE p.prosecdef
                      AND p.pronamespace = to_regnamespace(current_schema())
                    """
                ).fetchall()
                assert len(rows) > 40
                return sorted(
                    text(name)
                    for name, settings in rows
                    if [text(setting) for setting in settings
                        if text(setting).startswith("search_path=")] != [expected]
                )

            # Checked after every migration ran once, as a deploy runs them,
            # so a later migration that replaces such a function fails here.
            assert unpinned() == []
            assert connection.execute(
                "SELECT has_database_privilege('public', current_database(), 'TEMP')"
            ).fetchone() == (False,)
            # Running the migration again changes nothing.
            apply_migration(
                connection,
                (
                    ROOT / "deploy/migrations/0145_pin_privileged_function_search_path.sql"
                ).read_text(encoding="utf-8"),
            )
            assert unpinned() == []
