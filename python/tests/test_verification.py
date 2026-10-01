from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clashlens.verification import (
    KeyAction,
    OfficialVerificationClient,
    OfficialVerificationResponse,
    VerificationOutcome,
    VerificationTransportError,
    classify_official_response,
    classify_transport_ambiguity,
    load_official_api_key_file,
)

FIXTURES = Path(__file__).parents[1] / "testdata" / "player_token_verification"


@pytest.mark.parametrize("status", [200, 301, 302, 303, 307, 308])
def test_verification_sends_one_request_even_when_redirected(status: int) -> None:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.command, self.path))
            response_status = 200 if self.path == "/redirected" else status
            body = (
                b'{"tag":"#2PP","token":"fixture-token","status":"ok"}'
                if response_status == 200
                else b""
            )
            self.send_response(response_status)
            self.send_header("Content-Length", str(len(body)))
            if response_status != 200:
                self.send_header("Location", "/redirected")
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = OfficialVerificationClient(
            api_key=b"fixture-key",
            proxy_url="",
            api_origin=f"http://127.0.0.1:{server.server_port}",
            allow_insecure_test_origin=True,
        )
        response = client.verify("#2PP", "fixture-token")

        assert response.http_status == status
        assert requests == [("POST", "/v1/players/%232PP/verifytoken")]
        assert classify_official_response(status, response.body).outcome is (
            VerificationOutcome.VERIFIED
            if status == 200
            else VerificationOutcome.UNAVAILABLE
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize(
    "fixture_name",
    [
        "valid_token.json",
        "invalid_token.json",
        "api_key_authentication_failure.json",
        "fixed_egress_authentication_failure.json",
        "rate_limit.json",
        "malformed_response.json",
        "provider_failure.json",
    ],
)
def test_sanitized_official_response_fixtures_have_exact_classification(
    fixture_name: str,
) -> None:
    fixture = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    body = (
        fixture["body_text"].encode("utf-8")
        if "body_text" in fixture
        else json.dumps(fixture["body"], separators=(",", ":")).encode("utf-8")
    )

    result = classify_official_response(fixture["http_status"], body)

    assert result.outcome == VerificationOutcome(fixture["expected_outcome"])
    assert result.key_action == KeyAction(fixture["expected_key_action"])


def test_unknown_authentication_response_does_not_quarantine_by_guess() -> None:
    result = classify_official_response(
        403,
        b'{"reason":"accessDenied","message":"Changed provider wording"}',
    )

    assert result.outcome is VerificationOutcome.UNAVAILABLE
    assert result.key_action is KeyAction.NONE


def test_changed_success_response_is_not_guessed_as_valid_or_invalid() -> None:
    result = classify_official_response(200, b'{"status":"OK"}')

    assert result.outcome is VerificationOutcome.UNAVAILABLE
    assert result.key_action is KeyAction.NONE


def test_transport_ambiguity_fixture_fails_closed_without_key_state_change() -> None:
    fixture = json.loads(
        (FIXTURES / "transport_ambiguity.json").read_text(encoding="utf-8")
    )

    result = classify_transport_ambiguity()

    assert result.outcome == VerificationOutcome(fixture["expected_outcome"])
    assert result.key_action == KeyAction(fixture["expected_key_action"])


@pytest.mark.parametrize("suffix", [b"", b"\n", b"\r\n"])
def test_official_api_key_file_accepts_exact_ascii_with_one_optional_line_ending(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    key_file = tmp_path / "interactive.key"
    key_file.write_bytes(b"safe-synthetic-api-key" + suffix)

    assert load_official_api_key_file(key_file) == b"safe-synthetic-api-key"


@pytest.mark.parametrize(
    "content",
    [
        b" safe-synthetic-api-key",
        b"safe-synthetic-api-key ",
        b"safe-synthetic-api-key\n\n",
        b"safe-synthetic-api-key\r",
        b"safe\nsynthetic",
        b"\xff",
    ],
)
def test_official_api_key_file_rejects_noncanonical_secret_bytes(
    tmp_path: Path,
    content: bytes,
) -> None:
    key_file = tmp_path / "interactive.key"
    key_file.write_bytes(content)

    with pytest.raises(ValueError, match="official API key file"):
        load_official_api_key_file(key_file)


def test_official_client_calls_only_verifytoken_through_the_fixed_proxy() -> None:
    captured: list[object] = []

    def transport(request: object) -> OfficialVerificationResponse:
        captured.append(request)
        return OfficialVerificationResponse(
            http_status=200,
            body=b'{"tag":"#2PP","token":"one-time-token","status":"ok"}',
        )

    client = OfficialVerificationClient(
        api_key=b"safe-synthetic-api-key",
        proxy_url="http://fixed-egress.internal:3128",
        transport=transport,
    )

    result = client.verify("#2PP", "one-time-token")

    assert result == OfficialVerificationResponse(
        http_status=200,
        body=b'{"status":"ok"}',
    )
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"  # type: ignore[attr-defined]
    assert request.url == (  # type: ignore[attr-defined]
        "https://api.clashofclans.com/v1/players/%232PP/verifytoken"
    )
    assert request.proxy_url == "http://fixed-egress.internal:3128"  # type: ignore[attr-defined]
    assert request.body == b'{"token":"one-time-token"}'  # type: ignore[attr-defined]
    assert request.authorization == "Bearer safe-synthetic-api-key"  # type: ignore[attr-defined]


def test_official_client_can_use_the_loopback_development_fixture() -> None:
    captured: list[object] = []

    def transport(request: object) -> OfficialVerificationResponse:
        captured.append(request)
        return OfficialVerificationResponse(
            http_status=200,
            body=b'{"tag":"#2PP","token":"VERIFY-2PP","status":"ok"}',
        )

    client = OfficialVerificationClient(
        api_key=b"safe-synthetic-api-key",
        proxy_url="",
        api_origin="http://127.0.0.1:8080",
        allow_insecure_test_origin=True,
        transport=transport,
    )

    client.verify("#2PP", "VERIFY-2PP")

    request = captured[0]
    assert request.url == (  # type: ignore[attr-defined]
        "http://127.0.0.1:8080/v1/players/%232PP/verifytoken"
    )
    assert request.proxy_url == ""  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("status", "expected"),
    [("ok", VerificationOutcome.VERIFIED), ("invalid", VerificationOutcome.INVALID_TOKEN)],
)
def test_official_client_classifies_live_response_without_returning_echoed_token(
    status: str, expected: VerificationOutcome
) -> None:
    def transport(_request: object) -> OfficialVerificationResponse:
        return OfficialVerificationResponse(
            200,
            json.dumps(
                {"tag": "#2PP", "token": "private-player-token", "status": status}
            ).encode(),
        )

    client = OfficialVerificationClient(
        api_key=b"safe-synthetic-api-key",
        proxy_url="http://fixed-egress.internal:3128",
        transport=transport,
    )
    response = client.verify("#2PP", "private-player-token")

    assert classify_official_response(response.http_status, response.body).outcome is expected
    assert b"private-player-token" not in response.body


@pytest.mark.parametrize(
    ("tag", "token", "status"),
    [
        ("#2PQ", "private-player-token", "ok"),
        ("#2PP", "another-token", "ok"),
        ("#2PP", "private-player-token", "OK"),
    ],
)
def test_official_client_rejects_echoes_for_another_request_or_unknown_status(
    tag: str, token: str, status: str
) -> None:
    client = OfficialVerificationClient(
        api_key=b"safe-synthetic-api-key",
        proxy_url="http://fixed-egress.internal:3128",
        transport=lambda _request: OfficialVerificationResponse(
            200, json.dumps({"tag": tag, "token": token, "status": status}).encode()
        ),
    )

    with pytest.raises(VerificationTransportError):
        client.verify("#2PP", "private-player-token")


def test_official_client_rejects_insecure_origin_without_test_opt_in() -> None:
    with pytest.raises(ValueError, match="requires test opt-in"):
        OfficialVerificationClient(
            api_key=b"safe-synthetic-api-key",
            proxy_url="",
            api_origin="http://127.0.0.1:8080",
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"ok"}',
        b'{"tag":"#2PP","status":"ok"}',
        b'{"tag":"#2PP","token":"private-player-token","status":"ok","extra":true}',
        b"not JSON",
    ],
)
def test_official_client_rejects_unbound_success(body: bytes) -> None:
    client = OfficialVerificationClient(
        api_key=b"safe-synthetic-api-key",
        proxy_url="http://fixed-egress.internal:3128",
        transport=lambda _request: OfficialVerificationResponse(200, body),
    )

    with pytest.raises(VerificationTransportError):
        client.verify("#2PP", "private-player-token")


def test_official_client_rejects_non_loopback_insecure_test_origin() -> None:
    with pytest.raises(ValueError, match="must be loopback"):
        OfficialVerificationClient(
            api_key=b"safe-synthetic-api-key",
            proxy_url="http://fixed-egress.internal:3128",
            api_origin="http://example.test",
            allow_insecure_test_origin=True,
        )


@pytest.mark.parametrize(
    "proxy_url",
    ["", "fixed-egress.internal:3128", "http://user:password@fixed-egress:3128"],
)
def test_official_client_rejects_missing_or_credentialed_fixed_egress(
    proxy_url: str,
) -> None:
    with pytest.raises(ValueError, match="fixed-egress proxy"):
        OfficialVerificationClient(
            api_key=b"safe-synthetic-api-key",
            proxy_url=proxy_url,
        )
