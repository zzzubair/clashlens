from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).parents[2]


@contextmanager
def contract_two_database(database_url: str):
    schema = f"boundary_contract_two_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            for name in ("0001_collector.sql", "0002_python_layer.sql"):
                connection.execute(
                    (ROOT / "deploy/migrations" / name).read_text(encoding="utf-8")
                )
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@contextmanager
def populated_0010_database(database_url: str):
    schema = f"boundary_upgrade_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            for version in range(1, 11):
                path = next(ROOT.glob(f"deploy/migrations/{version:04d}_*.sql"))
                connection.execute(path.read_text(encoding="utf-8"))
            player_id = connection.execute(
                """
                INSERT INTO players (normalized_tag, active)
                VALUES ('#UPGRADE', true)
                RETURNING id
                """
            ).fetchone()[0]
            sweep_id = connection.execute(
                """
                INSERT INTO collector_reset_sweeps (boundary_at)
                VALUES ('2026-08-05T05:00:00Z')
                RETURNING id
                """
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO collector_reset_sweep_members (sweep_id, player_id)
                VALUES (%s, %s)
                """,
                (sweep_id, player_id),
            )
            generation_id = connection.execute(
                """
                INSERT INTO boundary_publication_generations (
                    boundary_at, generation, sweep_id, ordering_rule_version,
                    freshness_rule_version, expected_population_count,
                    expected_population_hash
                ) VALUES ('2026-08-05T05:00:00Z', 1, %s,
                          'legend-snapshot-order-v1',
                          'legend-profile-freshness-v1', 1, repeat('a', 64))
                RETURNING id
                """,
                (sweep_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO boundary_publication_generation_members
                    (generation_id, player_id)
                VALUES (%s, %s)
                """,
                (generation_id, player_id),
            )
            connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, status,
                    due_at, analytics_rule_version
                ) VALUES
                    ('build_snapshot', 'build_snapshot:ranked-day-version:legacy',
                     '{"boundary_at":"2026-08-05T05:00:00Z"}', 'pending',
                     clock_timestamp(), 'legend-analytics-v1'),
                    ('build_analytics', 'analytics:legacy',
                     '{"snapshot_id":1}', 'waiting_retry',
                     clock_timestamp(), 'analytics-v1'),
                    ('build_army_analytics', 'build_army_analytics:legacy',
                     '{"ranked_day_start":"2026-08-04T05:00:00Z", "official_season_id":"2026-08"}',
                     'pending', clock_timestamp(), 'army-analytics-v2')
                """
            )
        yield connection_info, generation_id
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_worker_startup_refuses_contract_two(database_url: str) -> None:
    with contract_two_database(database_url) as connection_info:
        with pytest.raises(RuntimeError, match="compiled Python contract version"):
            from clashlens.db import Database

            Database(connection_info, expected_contract_version=4)


def test_queued_correction_lifecycle_states_are_durable(database_url: str) -> None:
    with contract_two_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            for name in (
                "0003_regular_poll_dedup.sql",
                "0004_source_parser_v2.sql",
                "0005_army_decoding.sql",
                "0006_provider_identities.sql",
                "0007_player_discovery.sql",
                "0008_public_army_analytics.sql",
                "0009_raw_evidence.sql",
                "0010_boundary_publication_coordinator.sql",
                "0011_boundary_publication_contract.sql",
            ):
                connection.execute(
                    (ROOT / "deploy/migrations" / name).read_text(encoding="utf-8")
                )
            sweep_id = connection.execute(
                "INSERT INTO collector_reset_sweeps (boundary_at) VALUES ('2026-08-05T05:00:00Z') RETURNING id"
            ).fetchone()[0]
            generation_id = connection.execute(
                """
                INSERT INTO boundary_publication_generations (
                    boundary_at, generation, sweep_id, ordering_rule_version,
                    freshness_rule_version, expected_population_count,
                    expected_population_hash, snapshot_rule_version,
                    army_rule_version, target_rule, target_at
                ) VALUES ('2026-08-05T05:00:00Z', 1, %s, 'order', 'freshness',
                          0, repeat('a', 64), 'snapshot', 'army', 'target',
                          '2026-08-05T05:05:00Z')
                RETURNING id
                """,
                (sweep_id,),
            ).fetchone()[0]
            correction_id = connection.execute(
                """
                INSERT INTO boundary_publication_corrections
                    (boundary_at, source_generation_id, pending_inputs)
                VALUES ('2026-08-05T05:00:00Z', %s, '[{"kind":"decode"}]')
                RETURNING id
                """,
                (generation_id,),
            ).fetchone()[0]
            for state in ("pending_inputs", "activation", "inheritance", "active"):
                connection.execute(
                    "UPDATE boundary_publication_corrections SET state = %s WHERE id = %s",
                    (state, correction_id),
                )
            assert (
                connection.execute(
                    "SELECT state FROM boundary_publication_corrections WHERE id = %s",
                    (correction_id,),
                ).fetchone()[0]
                == "active"
            )


def test_0011_upgrades_populated_0010_and_fences_legacy_jobs(database_url: str) -> None:
    with populated_0010_database(database_url) as (connection_info, generation_id):
        migration = (
            ROOT / "deploy/migrations/0011_boundary_publication_contract.sql"
        ).read_text(encoding="utf-8")
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(migration)
            assert (
                connection.execute(
                    "SELECT version FROM clash_lens_contract WHERE singleton"
                ).fetchone()[0]
                == 4
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s",
                    (generation_id,),
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    """
                SELECT count(*) FROM python_processing_jobs
                WHERE status = 'cancelled'
                  AND failure_category = 'coordinator_contract_required'
                """
                ).fetchone()[0]
                == 3
            )
            legacy_shapes = {
                "build_snapshot": '{"ranked_day_version_id":1}',
                "build_analytics": '{"snapshot_id":1}',
                "build_army_analytics": '{"ranked_day_start":"2026-08-04T05:00:00Z"}',
            }
            for work_type, input_json in legacy_shapes.items():
                for status in (
                    "pending",
                    "waiting_retry",
                    "waiting_dependency",
                    "leased",
                ):
                    lease = (
                        "'probe', 'probe-token', clock_timestamp() + interval '1 hour'"
                        if status == "leased"
                        else "NULL, NULL, NULL"
                    )
                    with pytest.raises(psycopg.errors.RaiseException):
                        connection.execute(
                            f"""
                            INSERT INTO python_processing_jobs (
                                work_type, deduplication_key, input_json, status, due_at,
                                lease_owner, lease_token, lease_expires_at
                            ) VALUES (
                                %s, %s, %s, %s, clock_timestamp(), {lease}
                            )
                            """,
                            (
                                work_type,
                                f"legacy-probe:{work_type}:{status}",
                                input_json,
                                status,
                            ),
                        )
                    connection.rollback()
            connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, status, due_at
                ) VALUES (
                    'build_snapshot', 'legacy-terminal-readable', '{}', 'complete', clock_timestamp()
                )
                """
            )
            assert (
                connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE deduplication_key = 'legacy-terminal-readable'"
                ).fetchone()[0]
                == "complete"
            )
            connection.execute(migration)
            assert (
                connection.execute(
                    "SELECT count(*) FROM boundary_publication_legacy_job_migrations"
                ).fetchone()[0]
                == 3
            )
