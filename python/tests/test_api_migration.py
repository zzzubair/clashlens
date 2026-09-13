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
def migrated_production_database(
    database_url: str,
    *,
    include_migration_0003: bool = True,
    include_migration_0004: bool = True,
    include_compact_collector: bool = False,
) -> Iterator[str]:
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
            if include_migration_0003:
                connection.execute(
                    (ROOT / "deploy/migrations/0003_regular_poll_dedup.sql").read_text(
                        encoding="utf-8"
                    )
                )
                if include_migration_0004:
                    connection.execute(
                        (
                            ROOT / "deploy/migrations/0004_source_parser_v2.sql"
                        ).read_text(encoding="utf-8")
                    )
                    connection.execute(
                        (ROOT / "deploy/migrations/0005_army_decoding.sql").read_text(
                            encoding="utf-8"
                        )
                    )
                    connection.execute(
                        (
                            ROOT / "deploy/migrations/0006_provider_identities.sql"
                        ).read_text(encoding="utf-8")
                    )
                    connection.execute(
                        (
                            ROOT / "deploy/migrations/0007_player_discovery.sql"
                        ).read_text(encoding="utf-8")
                    )
                    connection.execute(
                        (
                            ROOT / "deploy/migrations/0008_public_army_analytics.sql"
                        ).read_text(encoding="utf-8")
                    )
            if include_compact_collector:
                migrations = sorted((ROOT / "deploy/migrations").glob("*.sql"))
                for migration in migrations:
                    version = int(migration.name.split("_", 1)[0])
                    if 9 <= version <= 26:
                        connection.execute(migration.read_text(encoding="utf-8"))
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


def test_source_parser_v2_migration_advances_defaults_and_keeps_v1_replayable(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            defaults = {
                text(row[0]): text(row[1])
                for row in connection.execute(
                    """
                    SELECT table_name, column_default
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND column_name = 'parser_version'
                      AND table_name IN (
                          'python_processing_jobs',
                          'ranked_day_versions',
                          'reset_baseline_evidence'
                      )
                    ORDER BY table_name
                    """
                )
            }
            replay_definition = text(
                connection.execute(
                    """
                    SELECT pg_get_functiondef(
                        'clashlens_request_python_replay_v2(bigint, text, text, text, text, text, text)'::regprocedure
                    )
                    """
                ).fetchone()[0]
            )
            migration_count = connection.execute(
                """
                SELECT count(*)
                FROM clash_lens_schema_migrations
                WHERE version = 4
                """
            ).fetchone()[0]

    assert defaults == {
        "python_processing_jobs": "'supercell-source-parser-v2'::text",
        "ranked_day_versions": "'supercell-source-parser-v2'::text",
        "reset_baseline_evidence": "'supercell-source-parser-v2'::text",
    }
    assert "supercell-source-parser-v1" in replay_definition
    assert "supercell-source-parser-v2" in replay_definition
    assert migration_count == 1


def test_provider_identities_migration_permits_discord_and_stays_reentrant(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            migration_0006 = (
                ROOT / "deploy/migrations/0006_provider_identities.sql"
            ).read_text(encoding="utf-8")
            # Reapplication is a stable no-op.
            connection.execute(migration_0006)

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

            applied = connection.execute(
                "SELECT true FROM clash_lens_schema_migrations WHERE version = 6"
            ).fetchone()
            assert applied is not None

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


def test_public_army_migration_is_forward_only_and_reentrant(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        migration = (
            ROOT / "deploy/migrations/0008_public_army_analytics.sql"
        ).read_text(encoding="utf-8")
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(migration)
            assert (
                connection.execute(
                    "SELECT true FROM clash_lens_schema_migrations WHERE version = 8"
                ).fetchone()
                is not None
            )
            columns = connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'battle_army_decodes'
                  AND column_name IN ('perspective', 'unresolved_components')
                ORDER BY column_name
                """
            ).fetchall()
            assert [row[0] for row in columns] == [
                "perspective",
                "unresolved_components",
            ]
            assert (
                connection.execute(
                    "SELECT to_regclass('army_analytics_publications')"
                ).fetchone()[0]
                is None
            )
            assert (
                connection.execute(
                    "SELECT has_table_privilege('clashlens_python_api', "
                    "'army_analytics_battle_facts', 'INSERT')"
                ).fetchone()[0]
                is False
            )


def test_public_army_migration_cancels_leased_v1_job_and_clears_lease(
    database_url: str,
) -> None:
    schema = f"python_api_leased_v1_{uuid4().hex}"
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
            connection.execute(migration_0002)
            connection.execute(
                (ROOT / "deploy/migrations/0003_regular_poll_dedup.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (ROOT / "deploy/migrations/0004_source_parser_v2.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (ROOT / "deploy/migrations/0005_army_decoding.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (ROOT / "deploy/migrations/0006_provider_identities.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (ROOT / "deploy/migrations/0007_player_discovery.sql").read_text(
                    encoding="utf-8"
                )
            )
            job_id = connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json,
                    processing_version, domain_rule_version, analytics_rule_version,
                    status, lease_owner, lease_token, lease_expires_at, due_at
                ) VALUES (
                    'build_army_analytics', 'build_army_analytics:test-leased-v1',
                    '{"ranked_day_start":"2026-08-04T05:00:00Z","official_season_id":"1783918800"}'::jsonb,
                    'clashlens-domain-processing-v1','clashlens-domain-rules-v1','legend-analytics-v1',
                    'leased','test-owner','test-token', clock_timestamp() + interval '5 minutes',
                    clock_timestamp()
                ) RETURNING id
                """
            ).fetchone()[0]
            before = connection.execute(
                "SELECT status, lease_owner, lease_token, lease_expires_at FROM python_processing_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            assert before[0] == "leased"
            assert before[1] == "test-owner"
            connection.execute(
                (ROOT / "deploy/migrations/0008_public_army_analytics.sql").read_text(
                    encoding="utf-8"
                )
            )
            after = connection.execute(
                "SELECT status, lease_owner, lease_token, lease_expires_at, failure_category FROM python_processing_jobs WHERE id = %s",
                (job_id,),
            ).fetchone()
            assert after[0] == "cancelled"
            assert after[1] is None
            assert after[2] is None
            assert after[3] is None
            assert after[4] == "superseded_analytics_rule_version"
            # migration must remain valid under lease check
            connection.execute("SELECT 1")
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
