from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import psycopg
from domain_test_support import domain_database
from test_boundary_publication_postgres import BOUNDARY, _sweep_with_members

from clashlens.db import Database

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "operating_check_postgres", ROOT / "scripts/operating_check.py"
)
assert SPEC and SPEC.loader
operating_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operating_check)


def test_operating_database_snapshot_executes_against_current_migrations(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            cursor = connection.execute(operating_check.DATABASE_SQL)
            row = None
            while True:
                if cursor.description is not None:
                    row = cursor.fetchone()
                if not cursor.nextset():
                    break

    assert row is not None
    snapshot = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    assert snapshot["contract_version"] == 5
    assert [item["version"] for item in snapshot["migrations"]] == list(range(1, 16))
    assert snapshot["identity"]["system_identifier"].isdigit()
    assert snapshot["identity"]["database_oid"] > 0
    assert snapshot["queues"]["collector"]["by_status"] == {
        "pending": 0,
        "leased": 0,
        "waiting_retry": 0,
        "waiting_dependency": 0,
        "complete": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert snapshot["queues"]["collector"]["oldest_due_seconds"] is None
    assert snapshot["queues"]["python"]["by_status"] == {
        "pending": 0,
        "leased": 0,
        "waiting_retry": 0,
        "waiting_dependency": 0,
        "complete": 0,
        "failed": 0,
        "cancelled": 0,
    }
    assert snapshot["queues"]["python"]["oldest_due_seconds"] is None
    assert snapshot["boundary"] == {"active_count": 0, "artifacts": []}


def test_operating_snapshot_keeps_boundary_progress_at_its_captured_database_state(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = int(
                    connection.execute(
                        "INSERT INTO players (normalized_tag, active) "
                        "VALUES ('#OPERATING', true) RETURNING id"
                    ).fetchone()[0]
                )
                sweep_id = _sweep_with_members(connection, [player_id])
                database._create_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    sweep_id=sweep_id,
                    player_ids=[player_id],
                    generation=1,
                    supersedes_id=None,
                )
                baseline_id = int(
                    connection.execute(
                        """
                        INSERT INTO collector_reset_baseline_sweeps (
                            reset_sweep_id, player_id, boundary_at,
                            evidence_kind, state
                        ) VALUES (%s, %s, %s, 'paired_v2', 'pending')
                        RETURNING id
                        """,
                        (sweep_id, player_id, BOUNDARY),
                    ).fetchone()[0]
                )
                job_id = int(
                    connection.execute(
                        """
                        INSERT INTO collector_jobs (
                            work_type, scope, player_id, normalized_tag,
                            capacity_pool, priority, due_at, coalescing_key,
                            sweep_id, reset_baseline_sweep_id, status
                        ) VALUES (
                            'reset_baseline', 'player', %s, '#OPERATING',
                            'normal', 400, clock_timestamp() + interval '1 hour',
                            'operating-future-reset', %s, %s, 'pending'
                        ) RETURNING id
                        """,
                        (player_id, sweep_id, baseline_id),
                    ).fetchone()[0]
                )
        finally:
            database.close()

        transaction_sql = operating_check.DATABASE_SQL.strip()
        begin = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
        assert transaction_sql.startswith(begin)
        assert transaction_sql.endswith("COMMIT;")
        select_sql = transaction_sql.removeprefix(begin).removesuffix("COMMIT;").strip()
        with psycopg.connect(connection_info, autocommit=True) as captured:
            captured.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            captured.execute("SELECT count(*) FROM collector_jobs").fetchone()
            with psycopg.connect(connection_info) as writer:
                writer.execute(
                    "UPDATE collector_jobs "
                    "SET due_at = clock_timestamp() - interval '1 second' "
                    "WHERE id = %s",
                    (job_id,),
                )
            stale = captured.execute(select_sql).fetchone()[0]
            captured.execute("COMMIT")

        with psycopg.connect(connection_info, autocommit=True) as fresh_connection:
            cursor = fresh_connection.execute(operating_check.DATABASE_SQL)
            fresh = None
            while True:
                if cursor.description is not None:
                    fresh = cursor.fetchone()[0]
                if not cursor.nextset():
                    break

    stale = json.loads(stale) if isinstance(stale, str) else stale
    fresh = json.loads(fresh) if isinstance(fresh, str) else fresh
    assert stale["queues"]["collector"]["by_status"]["pending"] == 1
    assert stale["queues"]["collector"]["oldest_due_seconds"] is None
    assert stale["boundary"]["active_count"] == 2
    assert {
        (item["artifact"], item["queued_work"], item["coordinator_transition"])
        for item in stale["boundary"]["artifacts"]
    } == {("snapshot", 0, False), ("army", 0, False)}
    assert fresh["queues"]["collector"]["by_status"]["pending"] == 1
    assert fresh["queues"]["collector"]["oldest_due_seconds"] >= 0
    assert fresh["boundary"]["active_count"] == 2
    assert {
        (item["artifact"], item["queued_work"], item["coordinator_transition"])
        for item in fresh["boundary"]["artifacts"]
    } == {("snapshot", 1, True), ("army", 1, True)}
