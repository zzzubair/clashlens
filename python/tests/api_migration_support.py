"""Shared setup for api migration tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from domain_test_support import apply_migration
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
                    if version >= 9:
                        apply_migration(
                            connection, migration.read_text(encoding="utf-8")
                        )
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
