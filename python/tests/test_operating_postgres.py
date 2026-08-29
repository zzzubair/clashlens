from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import psycopg
from domain_test_support import domain_database

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
    assert [item["version"] for item in snapshot["migrations"]] == list(range(1, 14))
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
