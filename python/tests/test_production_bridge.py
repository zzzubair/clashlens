from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from clashlens.db import Database


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
            for migration in sorted((root / "deploy" / "migrations").glob("*.sql")):
                connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute(
                "REVOKE ALL PRIVILEGES ON TABLE python_processing_jobs "
                "FROM clashlens_python_worker"
            )
            connection.execute(
                "GRANT SELECT (id, lease_generation) ON TABLE python_processing_jobs "
                "TO clashlens_python_worker"
            )
        yield connection_info, schema
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_worker_requires_the_production_queue_view(database_url: str) -> None:
    with _production_database(database_url) as (connection_info, _schema):
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute("DROP VIEW python_processing_jobs_worker")
        with pytest.raises(RuntimeError, match="python_processing_jobs_worker view"):
            Database(connection_info)


def test_worker_role_uses_only_the_production_queue_view(database_url: str) -> None:
    with _production_database(database_url) as (connection_info, _schema):
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute("SET ROLE clashlens_python_worker")
            current_user = connection.execute("SELECT current_user").fetchone()
            view_count = connection.execute(
                "SELECT count(*) FROM python_processing_jobs_worker"
            ).fetchone()
            assert current_user is not None
            assert _text(current_user[0]) == "clashlens_python_worker"
            assert view_count == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT status FROM python_processing_jobs LIMIT 1")
