from __future__ import annotations

import asyncio
import gzip
import json
import socket
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from time import monotonic, sleep
from typing import ClassVar

import pytest

from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient, ProviderFailure


def test_regular_keys_limit_starts_and_concurrency_per_key() -> None:
    pool = KeyPool(
        [ApiKey("regular-1", "one"), ApiKey("regular-2", "two")],
        starts_per_second=20,
        concurrency_per_key=2,
    )
    starts: dict[str, list[float]] = defaultdict(list)
    active: dict[str, int] = defaultdict(int)
    maximum: dict[str, int] = defaultdict(int)

    async def request(key: ApiKey, start_request) -> None:
        await start_request()
        starts[key.label].append(monotonic())
        active[key.label] += 1
        maximum[key.label] = max(maximum[key.label], active[key.label])
        await asyncio.sleep(0.08)
        active[key.label] -= 1

    async def run_requests() -> None:
        await asyncio.gather(*(pool.run(request) for _ in range(12)))

    asyncio.run(run_requests())

    assert set(starts) == {"regular-1", "regular-2"}
    assert maximum == {"regular-1": 2, "regular-2": 2}
    for key_starts in starts.values():
        assert all(later - earlier >= 0.04 for earlier, later in pairwise(key_starts))


def test_shared_permit_is_taken_before_each_interactive_start() -> None:
    events: list[str] = []

    async def permit() -> None:
        events.append("permit")

    pool = KeyPool(
        [ApiKey("interactive-1", "one")],
        starts_per_second=30,
        concurrency_per_key=1,
        before_start=permit,
    )

    async def request(_key: ApiKey, start_request) -> None:
        await start_request()
        events.append("request")

    asyncio.run(pool.run(request))

    assert events == ["permit", "request"]


def test_paused_key_is_skipped_while_another_key_is_ready() -> None:
    pool = KeyPool(
        [ApiKey("regular-1", "one"), ApiKey("regular-2", "two")],
        starts_per_second=30,
        concurrency_per_key=1,
    )
    pool.pause("regular-1", 60)
    used: list[str] = []

    async def request(key: ApiKey, start_request) -> None:
        await start_request()
        used.append(key.label)

    asyncio.run(pool.run(request))

    assert used == ["regular-2"]


@pytest.mark.parametrize("state_change", ["quarantine", "pause"])
def test_key_state_is_rechecked_after_waiting_to_start(state_change: str) -> None:
    pool = KeyPool(
        [ApiKey("regular-1", "one")],
        starts_per_second=30,
        concurrency_per_key=2,
    )
    starts: list[float] = []

    async def request(_key: ApiKey, start_request) -> None:
        await start_request()
        starts.append(monotonic())

    async def run() -> float:
        await pool.run(request)
        started = monotonic()
        waiting = asyncio.create_task(pool.run(request))
        await asyncio.sleep(0.005)
        if state_change == "quarantine":
            pool.quarantine("regular-1")
            with pytest.raises(ProviderFailure, match="no_healthy_api_key"):
                await waiting
        else:
            pool.pause("regular-1", 1)
            await waiting
        return monotonic() - started

    elapsed = asyncio.run(run())

    if state_change == "quarantine":
        assert len(starts) == 1
    else:
        assert len(starts) == 2
        assert elapsed >= 0.9


class _OfficialHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    authorization = ""
    paths_started: ClassVar[dict[str, list[float]]] = defaultdict(list)
    cancel_calls = 0
    cancel_started = threading.Event()
    state_lock = threading.Lock()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).authorization = self.headers.get("Authorization", "")
        with type(self).state_lock:
            type(self).paths_started[self.path].append(monotonic())
        if self.path.endswith("%23SLOWHEADERS"):
            try:
                self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(10):
                    self.connection.sendall(b"x")
                    sleep(0.06)
            except OSError:
                pass
            return
        if self.path.endswith("%23SLOWBODY"):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            try:
                for _ in range(10):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    sleep(0.06)
            except OSError:
                pass
            return
        if self.path.endswith("%23REDIRECTONE"):
            sleep(0.18)
            self.send_response(302)
            self.send_header("Location", "/v1/players/%23REDIRECTTWO")
            self.send_header("Content-Length", "5")
            self.end_headers()
            return
        if self.path.endswith("%23REDIRECTTWO"):
            sleep(0.18)
            body = b"redirected"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("%23REDIRECT"):
            self.send_response(302)
            self.send_header("Location", "/v1/players/%23SMALL")
            self.send_header("Content-Length", "5")
            self.end_headers()
            return
        if self.path.endswith("%23SMALL"):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.endswith("%23CHUNKED"):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"b\r\nxxxxxxxxxxx\r\n0\r\n\r\n")
            return
        if self.path.endswith("%23COMPRESSED"):
            body = gzip.compress(b'{"tag":"#COMPRESSED"}', mtime=0)
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.endswith("%23CANCEL"):
            with type(self).state_lock:
                type(self).cancel_calls += 1
                call_number = type(self).cancel_calls
            if call_number == 1:
                type(self).cancel_started.set()
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"x")
                self.wfile.flush()
                sleep(0.6)
                try:
                    self.wfile.write(b"y")
                except OSError:
                    pass
                return
        body = (
            b"x" * 11
            if self.path.endswith("/battlelog")
            else json.dumps({"tag": "#2PP"}).encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("ETag", '"fixture"')
        self.send_header("Last-Modified", "Sat, 13 Sep 2026 00:00:00 GMT")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def official_server():
    _OfficialHandler.paths_started = defaultdict(list)
    _OfficialHandler.cancel_calls = 0
    _OfficialHandler.cancel_started = threading.Event()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OfficialHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_official_client_returns_exact_bytes_and_safe_request_proof(
    official_server: str,
) -> None:
    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        max_body_bytes=1024,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )

    response = asyncio.run(client.fetch_player(pool, "#2PP", "profile"))

    assert response.body == b'{"tag": "#2PP"}'
    assert response.http_status == 200
    assert response.key_label == "regular-1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"] == "application/json"
    assert response.headers["etag"] == '"fixture"'
    assert response.headers["last-modified"] == "Sat, 13 Sep 2026 00:00:00 GMT"
    assert set(response.headers) <= {
        "cache-control",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "retry-after",
    }
    assert _OfficialHandler.authorization == "Bearer secret"


def test_official_client_rejects_a_response_over_the_raw_response_limit(
    official_server: str,
) -> None:
    client = OfficialApiClient(
        official_server, allow_insecure_test_origin=True, max_body_bytes=10
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )

    with pytest.raises(ProviderFailure, match="response_too_large"):
        asyncio.run(client.fetch_player(pool, "#2PP", "battle_log"))


@pytest.mark.parametrize("tag", ["#SLOWHEADERS", "#SLOWBODY"])
def test_official_client_enforces_total_deadline_while_bytes_keep_arriving(
    official_server: str, tag: str
) -> None:
    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        connection_timeout_seconds=0.2,
        response_timeout_seconds=0.12,
        total_timeout_seconds=0.25,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )

    started = monotonic()
    with pytest.raises(ProviderFailure, match="timeout") as raised:
        asyncio.run(client.fetch_player(pool, tag, "profile"))

    assert raised.value.retryable is True
    assert monotonic() - started < 0.6


def test_redirects_share_one_total_deadline(official_server: str) -> None:
    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        connection_timeout_seconds=0.25,
        response_timeout_seconds=0.25,
        total_timeout_seconds=0.3,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )

    with pytest.raises(ProviderFailure, match="timeout"):
        asyncio.run(client.fetch_player(pool, "#REDIRECTONE", "profile"))

    assert "/v1/players/%23REDIRECTTWO" in _OfficialHandler.paths_started


def test_redirect_hop_uses_another_rate_and_shared_permit(
    official_server: str,
) -> None:
    permit_times: list[float] = []

    async def permit() -> None:
        permit_times.append(monotonic())

    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        max_connections=1,
    )
    pool = KeyPool(
        [ApiKey("interactive-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
        before_start=permit,
    )

    response = asyncio.run(client.fetch_player(pool, "#REDIRECT", "profile"))

    redirect_started = _OfficialHandler.paths_started["/v1/players/%23REDIRECT"][0]
    target_started = _OfficialHandler.paths_started["/v1/players/%23SMALL"][0]
    assert response.body == b"ok"
    assert len(permit_times) == 2
    assert target_started - redirect_started >= 0.025


def test_unread_responses_do_not_poison_or_drain_single_connection_pool(
    official_server: str,
) -> None:
    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        max_body_bytes=10,
        max_connections=1,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )

    with pytest.raises(ProviderFailure, match="response_too_large"):
        asyncio.run(client.fetch_player(pool, "#2PP", "battle_log"))
    with pytest.raises(ProviderFailure, match="response_too_large"):
        asyncio.run(client.fetch_player(pool, "#CHUNKED", "profile"))
    redirected = asyncio.run(client.fetch_player(pool, "#REDIRECT", "profile"))
    following = asyncio.run(client.fetch_player(pool, "#SMALL", "profile"))

    assert redirected.body == b"ok"
    assert following.body == b"ok"


def test_official_client_keeps_content_encoded_response_bytes_exact(
    official_server: str,
) -> None:
    client = OfficialApiClient(official_server, allow_insecure_test_origin=True)
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )

    response = asyncio.run(client.fetch_player(pool, "#COMPRESSED", "profile"))

    assert response.body == gzip.compress(b'{"tag":"#COMPRESSED"}', mtime=0)


def test_cancellation_keeps_key_permit_until_blocking_request_ends(
    official_server: str,
) -> None:
    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        response_timeout_seconds=1,
        total_timeout_seconds=0.25,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )
    original_request = client._request
    request_unwinding = threading.Event()
    allow_request_end = threading.Event()
    request_ended_at: list[float] = []

    def tracked_request(*args: object):
        try:
            return original_request(*args)
        finally:
            request_unwinding.set()
            allow_request_end.wait(1)
            request_ended_at.append(monotonic())

    client._request = tracked_request

    async def run() -> None:
        first = asyncio.create_task(client.fetch_player(pool, "#CANCEL", "profile"))
        await asyncio.to_thread(_OfficialHandler.cancel_started.wait, 1)
        first.cancel()
        await asyncio.to_thread(request_unwinding.wait, 1)
        first.cancel()
        second = asyncio.create_task(client.fetch_player(pool, "#CANCEL", "profile"))
        await asyncio.sleep(0.05)
        assert len(_OfficialHandler.paths_started["/v1/players/%23CANCEL"]) == 1
        allow_request_end.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        response = await second
        assert response.body == b'{"tag": "#2PP"}'

    asyncio.run(run())

    starts = _OfficialHandler.paths_started["/v1/players/%23CANCEL"]
    assert len(starts) == 2
    assert starts[1] >= request_ended_at[0]


def test_actual_network_starts_stay_limited_when_default_executor_is_busy(
    official_server: str,
) -> None:
    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        max_connections=3,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=3,
    )
    blockers_started = 0
    blockers_lock = threading.Lock()
    blocker_release = threading.Event()

    async def run() -> None:
        nonlocal blockers_started
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=3))

        def block_executor() -> None:
            nonlocal blockers_started
            with blockers_lock:
                blockers_started += 1
            blocker_release.wait(1)

        blockers = [
            asyncio.create_task(asyncio.to_thread(block_executor)) for _ in range(3)
        ]
        while blockers_started < 3:
            await asyncio.sleep(0.001)
        try:
            responses = await asyncio.gather(
                *(client.fetch_player(pool, "#SMALL", "profile") for _ in range(3))
            )
            assert [response.body for response in responses] == [b"ok"] * 3
        finally:
            blocker_release.set()
            await asyncio.gather(*blockers)

    asyncio.run(run())

    starts = _OfficialHandler.paths_started["/v1/players/%23SMALL"]
    assert len(starts) == 3
    assert all(later - earlier >= 0.025 for earlier, later in pairwise(starts))


def test_shared_permits_are_serialized_next_to_actual_network_starts(
    official_server: str,
) -> None:
    permit_times: list[float] = []

    async def permit() -> None:
        permit_times.append(monotonic())

    client = OfficialApiClient(
        official_server,
        allow_insecure_test_origin=True,
        max_connections=3,
    )
    pool = KeyPool(
        [ApiKey("interactive-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=3,
        before_start=permit,
    )

    async def run() -> None:
        responses = await asyncio.gather(
            *(client.fetch_player(pool, "#SMALL", "profile") for _ in range(3))
        )
        assert [response.body for response in responses] == [b"ok"] * 3

    asyncio.run(run())

    starts = _OfficialHandler.paths_started["/v1/players/%23SMALL"]
    assert len(permit_times) == len(starts) == 3
    assert all(later - earlier >= 0.025 for earlier, later in pairwise(permit_times))
    assert all(0 <= start - permit < 0.025 for permit, start in zip(permit_times, starts))


def test_dns_past_deadline_cannot_start_network_and_keeps_capacity(
    official_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = official_server.replace("127.0.0.1", "localhost")
    client = OfficialApiClient(
        origin,
        allow_insecure_test_origin=True,
        total_timeout_seconds=0.1,
        max_connections=1,
    )
    pool = KeyPool(
        [ApiKey("regular-1", "secret")],
        starts_per_second=30,
        concurrency_per_key=1,
    )
    resolver_started = threading.Event()
    release_resolver = threading.Event()
    original_getaddrinfo = socket.getaddrinfo
    first_resolution = True

    def blocked_getaddrinfo(*args: object, **kwargs: object):
        nonlocal first_resolution
        if first_resolution:
            first_resolution = False
            resolver_started.set()
            release_resolver.wait(1)
        return original_getaddrinfo(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)

    async def run() -> float:
        started = monotonic()
        first = asyncio.create_task(client.fetch_player(pool, "#SMALL", "profile"))
        while not resolver_started.is_set():
            await asyncio.sleep(0.001)
        with pytest.raises(ProviderFailure, match="timeout"):
            await first
        elapsed = monotonic() - started
        second = asyncio.create_task(client.fetch_player(pool, "#SMALL", "profile"))
        await asyncio.sleep(0.05)
        assert not second.done()
        assert "/v1/players/%23SMALL" not in _OfficialHandler.paths_started
        release_resolver.set()
        response = await second
        assert response.body == b"ok"
        return elapsed

    elapsed = asyncio.run(run())

    assert elapsed < 0.3
    assert len(_OfficialHandler.paths_started["/v1/players/%23SMALL"]) == 1
