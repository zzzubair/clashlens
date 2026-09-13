from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from time import monotonic

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

    async def request(key: ApiKey) -> None:
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

    async def request(_key: ApiKey) -> None:
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

    async def request(key: ApiKey) -> None:
        used.append(key.label)

    asyncio.run(pool.run(request))

    assert used == ["regular-2"]


class _OfficialHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    authorization = ""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        type(self).authorization = self.headers.get("Authorization", "")
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
