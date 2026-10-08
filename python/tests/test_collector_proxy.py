from __future__ import annotations

import asyncio
import select
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from time import monotonic

import certifi
import pytest

from clashlens import cli
from clashlens.cli import build_parser
from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient, ProviderFailure
from clashlens.verification import (
    OfficialVerificationClient,
    VerificationTransportError,
)


@pytest.fixture()
def relay(tmp_path, monkeypatch):
    """Real CONNECT tunnel to a local, certificate-checked HTTPS origin."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    system_ca = certifi.where()
    monkeypatch.setattr(certifi, "where", lambda: str(cert))
    state = {"connects": [], "requests": [], "stall": False, "reject": False}
    entered, disconnected = threading.Event(), threading.Event()

    class Origin(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            state["requests"].append(
                (self.client_address, self.headers["Authorization"], monotonic())
            )
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            state["requests"].append(self.headers["Authorization"])
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"tag":"#2PP","token":"player-token","status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    origin.socket = ctx.wrap_socket(origin.socket, server_side=True)

    class Relay(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_CONNECT(self):
            state["connects"].append((self.path, dict(self.headers)))
            entered.set()
            self.close_connection = True
            if state["reject"]:
                self.send_error(403)
                return
            if state["stall"]:
                self.connection.settimeout(2)
                try:
                    if self.connection.recv(1) == b"":
                        disconnected.set()
                except OSError:
                    pass
                return
            assert self.path == f"localhost:{origin.server_port}"
            with socket.create_connection(
                ("127.0.0.1", origin.server_port)
            ) as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                peers = (self.connection, upstream)
                while True:
                    readable, _, _ = select.select(peers, [], [], 2)
                    if not readable:
                        return
                    for source in readable:
                        try:
                            data = source.recv(65536)
                            if not data:
                                return
                            (
                                upstream
                                if source is self.connection
                                else self.connection
                            ).sendall(data)
                        except OSError:
                            return

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True) for s in (origin, proxy)
    ]
    for thread in threads:
        thread.start()
    state.update(
        origin=f"https://localhost:{origin.server_port}",
        proxy=f"http://127.0.0.1:{proxy.server_port}",
        entered=entered,
        disconnected=disconnected,
        cert=cert,
        system_ca=system_ca,
    )
    try:
        yield state
    finally:
        for server in (proxy, origin):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


def key_pool():
    return KeyPool(
        [ApiKey("regular-1", "synthetic-secret")],
        starts_per_second=20,
        concurrency_per_key=1,
    )


@pytest.mark.parametrize("proxied", [False, True])
def test_collector_reuses_checked_connections_and_ignores_ambient_proxy(
    relay, monkeypatch, proxied
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    client = OfficialApiClient(
        relay["origin"], proxy_url=relay["proxy"] if proxied else "", max_connections=1
    )
    pool = key_pool()

    async def fetch():
        for _ in range(3):
            assert (await client.fetch_player(pool, "#2PP", "profile")).body == b"ok"

    try:
        asyncio.run(fetch())
    finally:
        client._http.clear()
        client._executor.shutdown()
    assert len({request[0] for request in relay["requests"]}) == 1
    assert [request[1] for request in relay["requests"]] == [
        "Bearer synthetic-secret"
    ] * 3
    starts = [request[2] for request in relay["requests"]]
    assert all(b - a >= 0.035 for a, b in pairwise(starts))
    assert len(relay["connects"]) == int(proxied)
    assert "synthetic-secret" not in str(relay["connects"])


@pytest.mark.parametrize(
    "failure", ["down", "denied", "bad_certificate", "untrusted_certificate"]
)
def test_relay_failure_never_falls_back_or_quarantines_key(relay, failure):
    proxy_url = relay["proxy"]
    if failure == "down":
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            proxy_url = f"http://127.0.0.1:{unused.getsockname()[1]}"
    relay["reject"] = failure == "denied"
    client = OfficialApiClient(relay["origin"], proxy_url=proxy_url)
    if failure == "bad_certificate":
        client._http.connection_pool_kw["assert_hostname"] = "wrong.invalid"
    if failure == "untrusted_certificate":
        client._http.connection_pool_kw["ca_certs"] = relay["system_ca"]
    pool = key_pool()

    async def fetch():
        for _ in range(2):
            with pytest.raises(ProviderFailure) as caught:
                await client.fetch_player(pool, "#2PP", "profile")
            assert caught.value.retryable
            assert caught.value.category != "no_healthy_api_key"
            assert caught.value.http_status is None

    try:
        asyncio.run(fetch())
    finally:
        client._http.clear()
        client._executor.shutdown()
    assert relay["requests"] == []


@pytest.mark.parametrize("cancel", [False, True])
def test_stalled_connect_is_interrupted_and_capacity_recovers(relay, cancel):
    relay["stall"] = True
    client = OfficialApiClient(
        relay["origin"],
        proxy_url=relay["proxy"],
        total_timeout_seconds=0.2,
        max_connections=1,
    )
    pool = key_pool()

    async def fetch():
        started = monotonic()
        task = asyncio.create_task(client.fetch_player(pool, "#2PP", "profile"))
        assert await asyncio.to_thread(relay["entered"].wait, 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ProviderFailure, match="timeout"):
                await task
        assert monotonic() - started < 0.7
        assert await asyncio.to_thread(relay["disconnected"].wait, 1)
        relay["stall"] = False
        assert (await client.fetch_player(pool, "#2PP", "profile")).body == b"ok"

    try:
        asyncio.run(fetch())
    finally:
        client._http.clear()
        client._executor.shutdown()
    assert len(relay["requests"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "socks5://relay:3128",
        "http://user:pass@relay",
        "http://relay/path",
        "http://relay?x=y",
        "http://relay#x",
        "http://relay:0",
        "http://relay:99999",
    ],
)
def test_invalid_proxy_is_rejected_before_any_request(url):
    with pytest.raises(ValueError):
        OfficialApiClient("https://api.clashofclans.com", proxy_url=url)


def test_collector_proxy_setting_defaults_direct_and_can_be_overridden(monkeypatch):
    monkeypatch.delenv("CLASHLENS_OFFICIAL_API_PROXY_URL", raising=False)
    assert build_parser().parse_args(["collector"]).official_proxy_url == ""
    monkeypatch.setenv("CLASHLENS_OFFICIAL_API_PROXY_URL", "http://relay:3128")
    assert (
        build_parser().parse_args(["collector"]).official_proxy_url
        == "http://relay:3128"
    )
    assert (
        build_parser()
        .parse_args(["collector", "--official-proxy-url", "http://other:3128"])
        .official_proxy_url
        == "http://other:3128"
    )


@pytest.mark.parametrize("denied", [False, True])
def test_private_verification_uses_same_relay_even_with_no_proxy(
    relay, monkeypatch, denied
):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("SSL_CERT_FILE", str(relay["cert"]))
    relay["reject"] = denied
    client = OfficialVerificationClient(
        api_key=b"synthetic-secret",
        proxy_url=relay["proxy"],
        api_origin=relay["origin"],
        allow_insecure_test_origin=True,
    )
    if denied:
        with pytest.raises(VerificationTransportError):
            client.verify("#2PP", "player-token")
        assert relay["requests"] == []
    else:
        assert client.verify("#2PP", "player-token").http_status == 200
        assert relay["requests"] == ["Bearer synthetic-secret"]
    assert len(relay["connects"]) == 1
    assert "synthetic-secret" not in str(relay["connects"])


@pytest.mark.parametrize("mode", ["fixture", "production"])
def test_deployment_routes_both_callers_and_keeps_fixture_direct(tmp_path, mode):
    ops = Path(__file__).resolve().parents[2] / "ops"
    subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1" help >/dev/null
STATE_DIR="$2"
load_fixture_config
MODE="$3"
if [[ "$MODE" == production ]]; then
  for name in ENDPOINT REGION BUCKET INSTANCE_ID MARKER_KEY MARKER_HASH MARKER_PAYLOAD_VERSION; do
    CONFIG[CLASHLENS_ARCHIVE_$name]=CHANGE_ME
  done
  CONFIG[CLASHLENS_OFFICIAL_API_ORIGIN]=https://api.example
  CONFIG[CLASHLENS_OFFICIAL_API_PROXY_URL]=http://100.64.0.1:3128
fi
write_environment
""",
            "test-ops-environment",
            str(ops),
            str(tmp_path),
            mode,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    collector = dict(
        line.split("=", 1)
        for line in (tmp_path / "env/collector.env").read_text().splitlines()
    )
    api = dict(
        line.split("=", 1)
        for line in (tmp_path / "env/api.env").read_text().splitlines()
    )
    expected = "http://100.64.0.1:3128" if mode == "production" else ""
    assert collector.get("CLASHLENS_OFFICIAL_API_PROXY_URL", "") == expected
    assert api.get("CLASHLENS_OFFICIAL_PROXY_URL", "") == expected


@pytest.mark.parametrize("failure", ["down", "denied"])
def test_a_relay_that_is_down_or_refuses_counts_as_a_relay_failure(relay, failure):
    # Timeouts alone could not tell a full or unreachable relay from a slow API.
    proxy_url = relay["proxy"]
    if failure == "down":
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            proxy_url = f"http://127.0.0.1:{unused.getsockname()[1]}"
    relay["reject"] = failure == "denied"
    client = OfficialApiClient(relay["origin"], proxy_url=proxy_url)

    async def fetch() -> str:
        with pytest.raises(ProviderFailure) as caught:
            await client.fetch_player(key_pool(), "#2PP", "profile")
        return caught.value.category

    try:
        assert asyncio.run(fetch()) == "proxy_failure"
    finally:
        client._http.clear()
        client._executor.shutdown()


@pytest.mark.parametrize(("concurrency", "refused"), [(7, True), (6, False)])
def test_collector_refuses_keys_that_could_overfill_the_relay(
    monkeypatch, concurrency, refused
):
    # Nine regular keys and the interactive one at seven requests each could
    # hold 70 relay connections; the collector's share of the relay's 96 is 64.
    monkeypatch.delenv("CLASHLENS_DATABASE_URL", raising=False)
    monkeypatch.delenv("CLASHLENS_DATABASE_URL_FILE", raising=False)
    keys = ",".join(f"normal-{index}=key-{index}" for index in range(1, 10))
    arguments = build_parser().parse_args(
        [
            "collector",
            "--official-proxy-url=http://127.0.0.1:9",
            f"--regular-api-keys={keys}",
            "--interactive-api-keys=interactive-1=key-0",
            f"--concurrency-per-key={concurrency}",
        ]
    )

    # Allowed settings go on to need a database, which this test has none of.
    message = "relay connections" if refused else "database URL is required"
    with pytest.raises(ValueError, match=message):
        cli._run_collector(arguments)
