from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from domain_test_support import domain_database
from psycopg_pool import PoolTimeout
from test_collector import _Client, _collector, _Spool, _Store

from clashlens.collector_db import CollectorDatabase


def test_slow_database_counts_do_not_slow_readiness() -> None:
    # Production counts took over 3 s on a busy database, so the 3 s health
    # check failed and Podman killed the collector every 10-20 minutes.
    class SlowCountStore(_Store):
        def health_metrics(self) -> dict[str, int]:
            time.sleep(5)
            return super().health_metrics()

    spool = _Spool()
    collector = _collector(spool, SlowCountStore(spool), _Client(spool))

    started = time.monotonic()
    ready = asyncio.run(collector.health_response("/readyz"))

    assert ready == (200, "text/plain", b"ready\n")
    assert time.monotonic() - started < 1


def test_unreachable_database_fails_readiness() -> None:
    spool = _Spool()
    store = _Store(spool)
    store.pool = MagicMock()
    store.pool.connection.side_effect = PoolTimeout("no connection")
    collector = _collector(spool, store, _Client(spool))

    assert asyncio.run(collector.health_response("/readyz")) == (
        503,
        "text/plain",
        b"database_unavailable\n",
    )


def test_readiness_reaches_a_real_database(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        spool = _Spool()
        database = CollectorDatabase(connection_info)
        try:
            collector = _collector(spool, database, _Client(spool))
            ready = asyncio.run(collector.health_response("/readyz"))
        finally:
            database.close()

    assert ready == (200, "text/plain", b"ready\n")
