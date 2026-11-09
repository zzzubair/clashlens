from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from psycopg_pool import ConnectionPool

from .db import CONTRACT_VERSION
from .db_rows import text_value
from .operating import database_pool_health

# Player writes that a worker may also lock fail at once with a retryable 503.
# Any other lock wait gives up after this long instead of holding a pool
# connection behind a long worker transaction until every page times out.
API_LOCK_TIMEOUT = "2s"
# The website gives up on a request after 5 seconds. A query still running
# well after that is cancelled so it stops holding one of the pool's
# connections. Army reads keep their own shorter transaction limit.
API_STATEMENT_TIMEOUT = "10s"


class ApiDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        timeout_seconds: float = 5.0,
    ) -> None:
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("API database pool bounds are invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("API database pool timeout is invalid")

        def configure(connection: Any) -> None:
            connection.execute(f"SET lock_timeout = '{API_LOCK_TIMEOUT}'")
            connection.execute(f"SET statement_timeout = '{API_STATEMENT_TIMEOUT}'")
            # Compiling a page query to machine code cost more than it saved:
            # about 0.1 s per Live Leaderboard read and 0.76 s per name search.
            connection.execute("SET jit = off")
            connection.commit()

        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_seconds,
            open=True,
            configure=configure,
        )

    def close(self) -> None:
        self.pool.close()

    def pool_health(self) -> dict[str, int]:
        return database_pool_health(self.pool)

    def is_ready(
        self, *, expected_contract_version: int = CONTRACT_VERSION
    ) -> bool:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT version,
                       to_regclass('clash_lens_accounts') IS NOT NULL,
                       to_regclass('private_api_requests') IS NOT NULL,
                       to_regclass('shared_api_credentials') IS NOT NULL
                FROM clash_lens_contract
                WHERE singleton = true
                """
            ).fetchone()
            return bool(
                row is not None
                and int(row[0]) == expected_contract_version
                and row[1]
                and row[2]
                and row[3]
            )

    def scalar(self, query: str, params: Iterable[Any] = ()) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return None if row is None else text_value(row[0])


def lock_api_player(connection: Any, normalized_tag: str) -> None:
    """Queue behind other site requests for this player, never behind a worker.

    Workers never take this lock, so once it is held, any lock on the player
    that is still busy belongs to a worker and a no-wait step fails at once.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('api-player:' || %s, 0))",
        (normalized_tag,),
    )


def execute_without_waiting(
    connection: Any, normalized_tag: str, query: str, params: Any
) -> Any:
    """Run one statement in a transaction, failing instead of waiting for a lock."""
    lock_api_player(connection, normalized_tag)
    connection.execute("SET LOCAL lock_timeout = '1ms'")
    cursor = connection.execute(query, params)
    connection.execute(f"SET LOCAL lock_timeout = '{API_LOCK_TIMEOUT}'")
    return cursor
