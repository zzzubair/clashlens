from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_collector import _Client, _collector, _Spool, _Store

from clashlens.collector_db import CollectorWork
from clashlens.collector_http import (
    ApiKey,
    KeyPool,
    OfficialApiClient,
    ProviderFailure,
    ProviderOutage,
)


class _Provider(BaseHTTPRequestHandler):
    """A fake official API whose answer the test switches while it runs."""

    protocol_version = "HTTP/1.1"
    status = 503
    redirect = False
    requests = 0
    lock = threading.Lock()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        with type(self).lock:
            type(self).requests += 1
        if type(self).redirect and not self.path.endswith("?hop"):
            self.send_response(307)
            self.send_header("Location", self.path + "?hop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = type(self).status
        body = json.dumps({"tag": "#2PP"}).encode() if status == 200 else b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def provider():
    _Provider.status = 503
    _Provider.redirect = False
    _Provider.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _pool(label: str = "regular-1") -> KeyPool:
    return KeyPool(
        [ApiKey(label, "secret")], starts_per_second=25, concurrency_per_key=6
    )


def test_fast_server_errors_pause_requests_instead_of_keeping_the_normal_rate(
    provider: str,
) -> None:
    client = OfficialApiClient(provider, allow_insecure_test_origin=True)
    pool = _pool()

    async def run() -> None:
        fetches = [
            asyncio.create_task(client.fetch_player(pool, "#2PP", "profile"))
            for _ in range(30)
        ]
        await asyncio.sleep(1.5)
        for fetch in fetches:
            fetch.cancel()
        await asyncio.gather(*fetches, return_exceptions=True)

    asyncio.run(run())

    # One key allows 25 starts a second, so without a pause all 30 go out.
    # The pause starts after 10 failures in a row; six may already be going.
    assert _Provider.requests <= 10 + 6
    # A provider outage is not a key problem.
    assert pool.health() == {"configured": 1, "healthy": 1, "paused": 0}


def test_recovery_probe_resumes_every_waiting_request(provider: str) -> None:
    client = OfficialApiClient(provider, allow_insecure_test_origin=True)
    client.provider_outage = ProviderOutage(threshold=3, base_delay=0.2, max_delay=0.4)
    pool = _pool()

    async def run() -> list[int]:
        fetches = [
            asyncio.create_task(client.fetch_player(pool, "#2PP", "profile"))
            for _ in range(20)
        ]
        await asyncio.sleep(1.2)
        during_outage = _Provider.requests
        # Probes go out at most every 0.4 seconds while the provider is down.
        assert 3 <= during_outage <= 3 + 6 + 4
        assert client.provider_outage.active
        _Provider.status = 200
        responses = await asyncio.wait_for(asyncio.gather(*fetches), 5)
        return [response.http_status for response in responses]

    statuses = asyncio.run(run())

    assert statuses.count(200) >= 20 - 13
    assert set(statuses) <= {200, 503}
    assert not client.provider_outage.active
    assert pool.health() == {"configured": 1, "healthy": 1, "paused": 0}


def test_recovery_probe_follows_its_own_redirect(provider: str) -> None:
    client = OfficialApiClient(provider, allow_insecure_test_origin=True)
    client.provider_outage = ProviderOutage(threshold=1, base_delay=0.1, max_delay=0.1)
    pool = _pool()

    async def run() -> int:
        await client.fetch_player(pool, "#2PP", "profile")
        assert client.provider_outage.active
        # The provider is back but answers the probe through a redirect.
        _Provider.status = 200
        _Provider.redirect = True
        response = await asyncio.wait_for(
            client.fetch_player(pool, "#2PP", "profile"), 5
        )
        return response.http_status

    assert asyncio.run(run()) == 200
    assert not client.provider_outage.active
    assert _Provider.requests == 3


@pytest.mark.parametrize("status", [401, 403, 429])
def test_rejected_keys_and_rate_limits_are_not_a_provider_outage(
    provider: str, status: int
) -> None:
    _Provider.status = status
    client = OfficialApiClient(provider, allow_insecure_test_origin=True)
    client.provider_outage = ProviderOutage(threshold=2)
    pool = KeyPool(
        [ApiKey(f"regular-{index}", "secret") for index in range(4)],
        starts_per_second=25,
        concurrency_per_key=1,
    )

    async def run() -> None:
        for _ in range(4):
            await client.fetch_player(pool, "#2PP", "profile")

    asyncio.run(run())

    assert not client.provider_outage.active
    health = pool.health()
    if status == 429:
        assert health == {"configured": 4, "healthy": 4, "paused": 4}
    else:
        assert health["healthy"] == 0


def test_network_failures_pause_and_shutdown_releases_waiting_requests() -> None:
    # Nothing listens on this port, so every request fails to connect.
    client = OfficialApiClient("http://127.0.0.1:9", allow_insecure_test_origin=True)
    client.provider_outage = ProviderOutage(threshold=3, base_delay=30)
    pool = _pool()

    async def run() -> None:
        for _ in range(3):
            with pytest.raises(ProviderFailure) as failure:
                await client.fetch_player(pool, "#2PP", "profile")
            assert failure.value.retryable
        assert client.provider_outage.active
        waiting = asyncio.create_task(client.fetch_player(pool, "#2PP", "profile"))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        client.provider_outage.stop()
        with pytest.raises(ProviderFailure) as failure:
            await asyncio.wait_for(waiting, 1)
        assert failure.value.category == "provider_outage"
        assert failure.value.retryable

    asyncio.run(run())

    assert pool.health() == {"configured": 1, "healthy": 1, "paused": 0}


def test_regular_checks_admitted_just_before_the_reset_drain_during_an_outage() -> None:
    # 256 regular checks admitted at 04:54:59 share six keys of six requests
    # each, and the provider stops answering: nothing listens on this port.
    spool = _Spool()
    client = OfficialApiClient(
        "http://127.0.0.1:9", allow_insecure_test_origin=True, total_timeout_seconds=0.5
    )
    client.provider_outage = ProviderOutage(base_delay=30)
    collector = _collector(spool, _Store(spool), client)
    collector.regular_keys = KeyPool(
        [ApiKey(f"regular-{index}", "secret") for index in range(6)],
        starts_per_second=25,
        concurrency_per_key=6,
    )
    checks = [
        CollectorWork(index, f"#{index}", datetime.now(UTC)) for index in range(256)
    ]

    async def run() -> None:
        admitted = asyncio.gather(
            *(collector.collect_player(work, lane="ordinary") for work in checks)
        )
        # Queued checks give up within the request timeout instead of
        # waiting out the 30-second pause, so none is in flight at 05:00.
        outcomes = await asyncio.wait_for(admitted, 10)
        assert client.provider_outage.paused
        assert not any("recorded" in outcome for outcome in outcomes)
        # A check after the pause began waits as paused work, not in flight.
        later = collector.collect_player(checks[0], lane="ordinary")
        assert await asyncio.wait_for(later, 1) == ["capacity_paused"] * 2

    asyncio.run(run())


def test_a_cancelled_request_is_a_retryable_failure_not_a_collector_stop() -> None:
    spool = _Spool()
    store = _Store(spool)

    class CancelledClient(_Client):
        async def fetch_player(self, _pool, _tag, _endpoint):
            # What a request cancelled underneath the collector raises.
            raise asyncio.CancelledError

    collector = _collector(spool, store, CancelledClient(spool))

    outcomes = asyncio.run(
        collector.collect_player(
            CollectorWork(1, "#2PP", datetime.now(UTC)),
            lane="ordinary",
            endpoints=("profile",),
        )
    )

    assert outcomes == ["transient"]
    assert [failure.failure_category for failure in store.failures] == [
        "request_cancelled"
    ]


def test_paused_regular_work_does_not_hold_a_reset_or_run_once_admission_closes() -> (
    None
):
    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, _Client(spool))
    work = CollectorWork(1, "#2PP", datetime.now(UTC))
    claims = [[work]]
    store.claim_due_players = lambda **_kwargs: claims.pop() if claims else []  # type: ignore[attr-defined]
    # The Reset closed regular admission while the work was paused.
    store.regular_admission_open = lambda _now: False  # type: ignore[attr-defined]
    started: list[CollectorWork] = []

    async def collect_player(item: CollectorWork, **_kwargs: object) -> list[str]:
        started.append(item)
        return ["capacity_paused"]

    collector.collect_player = collect_player  # type: ignore[method-assign]

    async def run() -> int:
        stop = asyncio.Event()
        loop = asyncio.create_task(collector._regular_loop(stop, 0.001))
        await asyncio.sleep(1.5)
        in_flight = collector.regular_inflight
        stop.set()
        await asyncio.wait_for(loop, 2)
        return in_flight

    assert asyncio.run(run()) == 0
    # The paused check waits for its next pass instead of running mid-Reset.
    assert started == [work]
