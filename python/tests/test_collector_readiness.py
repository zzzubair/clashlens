from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from domain_test_support import domain_database
from psycopg_pool import PoolTimeout
from test_collector import _Client, _collector, _Spool, _Store

from clashlens.collector_db import CollectorDatabase
from clashlens.collector_http import ApiKey, KeyPool


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


def test_metrics_show_each_keys_health_and_rate() -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    labels = ["normal-1", "normal-2", "normal-3", "normal-4", "extra-1", "extra-2"]
    collector.regular_keys = KeyPool(
        [ApiKey(label, f"fixture-{label}") for label in labels],
        starts_per_second=25,
        concurrency_per_key=6,
    )
    collector.regular_keys.quarantine("extra-1")
    collector.regular_keys.pause("extra-2", 60)

    async def request(_key: ApiKey, start_request) -> None:
        await start_request()

    asyncio.run(collector.regular_keys.run(request))
    status, _content_type, body = asyncio.run(collector.health_response("/metrics"))
    metrics = body.decode()

    assert status == 200
    for label in labels:
        key = f'{{pool="regular",key="{label}"}}'
        assert f"clashlens_collector_key_rate_limit_per_second{key} 25\n" in metrics
        healthy = 0 if label == "extra-1" else 1
        assert f"clashlens_collector_key_healthy{key} {healthy}\n" in metrics
        paused = 1 if label == "extra-2" else 0
        assert f"clashlens_collector_key_paused{key} {paused}\n" in metrics
        started = 1 if label == "normal-1" else 0
        assert (
            f"clashlens_collector_key_requests_started_total{key} {started}\n"
            in metrics
        )
    assert 'key_healthy{pool="interactive",key="interactive-1"} 1\n' in metrics
    assert "fixture-" not in metrics


def test_metrics_cache_serializes_scrapes_and_retries_failed_refresh(monkeypatch):
    from types import SimpleNamespace

    import psycopg

    instant = [100.0]
    monkeypatch.setattr("clashlens.collector.time", SimpleNamespace(monotonic=lambda: instant[0]))

    class ChangingStore(_Store):
        calls = 0
        failed = False

        def health_metrics(self):
            self.calls += 1
            if self.failed:
                raise psycopg.OperationalError("unavailable")
            return {"check_age_p50_seconds": self.calls}

    spool = _Spool()
    store = ChangingStore(spool)
    collector = _collector(spool, store, _Client(spool))

    async def scrape():
        first = await asyncio.gather(*(collector.health_response("/metrics") for _ in range(8)))
        assert all(b"clashlens_collector_check_age_p50_seconds 1\n" in response[2] for response in first)
        assert store.calls == 1
        instant[0] = 129.99
        assert (await collector.health_response("/metrics"))[2] == first[0][2]
        instant[0] = 130
        store.failed = True
        assert (await collector.health_response("/metrics"))[0] == 503
        store.failed = False
        recovered = await collector.health_response("/metrics")
        assert recovered[0] == 200
        assert b"clashlens_collector_check_age_p50_seconds 3\n" in recovered[2]

    asyncio.run(scrape())
