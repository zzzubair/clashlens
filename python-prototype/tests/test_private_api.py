from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient

from clashlens_prototype.api import create_app
from clashlens_prototype.api_db import ApiDatabase
from clashlens_prototype.hmac_proof import SigningInput, sign
from clashlens_prototype.verification import OfficialVerificationResponse
from test_api_db_public_ops import seed_profile
from test_api_migration import migrated_production_database

TS_CURRENT = bytes.fromhex("21" * 32)
TS_PREVIOUS = bytes.fromhex("22" * 32)
DISCORD_CURRENT = bytes.fromhex("31" * 32)
NOW_SECONDS = 1_807_000_000
NOW = datetime.fromtimestamp(NOW_SECONDS, tz=UTC)


def b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def signed_headers(
    target: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    caller: str = "typescript-website",
    key_id: str = "current",
    provider: str = "",
    subject: str = "",
    request_id: str | None = None,
) -> dict[str, str]:
    keys = {
        ("typescript-website", "current"): TS_CURRENT,
        ("typescript-website", "previous"): TS_PREVIOUS,
        ("discord-bot", "current"): DISCORD_CURRENT,
    }
    signing_input = SigningInput(
        proof_version="clashlens-hmac-v1",
        caller_b64url=b64(caller),
        key_id_b64url=b64(key_id),
        audience="clashlens-python-private-api",
        method=method,
        target_b64url=b64(target),
        body_sha256=hashlib.sha256(body).hexdigest(),
        issued_at=str(NOW_SECONDS),
        expires_at=str(NOW_SECONDS + 10),
        request_id=request_id or str(uuid4()),
        provider_b64url=b64(provider),
        provider_subject_b64url=b64(subject),
    )
    return {
        "X-ClashLens-Proof-Version": signing_input.proof_version,
        "X-ClashLens-Caller": signing_input.caller_b64url,
        "X-ClashLens-Key-Id": signing_input.key_id_b64url,
        "X-ClashLens-Issued-At": signing_input.issued_at,
        "X-ClashLens-Expires-At": signing_input.expires_at,
        "X-ClashLens-Request-Id": signing_input.request_id,
        "X-ClashLens-Provider": signing_input.provider_b64url,
        "X-ClashLens-Provider-Subject": signing_input.provider_subject_b64url,
        "X-ClashLens-Signature": sign(keys[(caller, key_id)], signing_input),
        "Content-Type": "application/json",
    }


def json_body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


@dataclass
class FakeOfficialVerifier:
    calls: int = 0

    def verify(self, normalized_tag: str, player_token: str) -> OfficialVerificationResponse:
        assert normalized_tag == "#2PP"
        assert player_token
        self.calls += 1
        return OfficialVerificationResponse(200, b'{"status":"ok"}')


def test_caller_operation_matrix_google_beta_and_complete_private_operations(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 6000)
        verifier = FakeOfficialVerifier()
        fingerprint = hashlib.sha256(b"safe-synthetic-interactive-key").hexdigest()
        database.register_official_credential(fingerprint)
        app = create_app(
            database=database,
            keys={
                ("typescript-website", "current"): TS_CURRENT,
                ("typescript-website", "previous"): TS_PREVIOUS,
                ("discord-bot", "current"): DISCORD_CURRENT,
            },
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            verification_client=verifier,
            official_credential_fingerprint=fingerprint,
        )

        with TestClient(app) as client:
            player_target = "/v1/players/%232PP"
            player = client.get(
                player_target,
                headers=signed_headers(player_target, key_id="previous"),
            )
            discord_player = client.get(
                player_target,
                headers=signed_headers(player_target, caller="discord-bot"),
            )
            leaderboard_target = "/v1/leaderboards/live?limit=100"
            leaderboard = client.get(
                leaderboard_target,
                headers=signed_headers(leaderboard_target),
            )
            analytics_target = "/v1/analytics/basic"
            analytics = client.get(
                analytics_target,
                headers=signed_headers(analytics_target),
            )
            public_user_target = "/v1/users/missinguser"
            public_user = client.get(
                public_user_target,
                headers=signed_headers(public_user_target),
            )
            anonymous_account = client.get(
                "/v1/account",
                headers=signed_headers("/v1/account"),
            )
            discord_identity = client.get(
                player_target,
                headers=signed_headers(
                    player_target,
                    caller="discord-bot",
                    provider="discord",
                    subject="123",
                ),
            )

            assert player.status_code == 200
            assert discord_player.status_code == 200
            assert leaderboard.status_code == 200
            assert analytics.status_code == 200
            assert public_user.status_code == 404
            assert anonymous_account.status_code == 403
            assert discord_identity.status_code == 403
            assert discord_identity.json() == {"error": "caller_operation_not_authorized"}

            create_target = "/v1/account"
            create_data = json_body(
                {"username": "ApiOwner", "display_name": "API Owner"}
            )
            created = client.post(
                create_target,
                content=create_data,
                headers=signed_headers(
                    create_target,
                    method="POST",
                    body=create_data,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            assert created.status_code == 201
            assert created.json()["username"] == "apiowner"

            account = client.get(
                create_target,
                headers=signed_headers(
                    create_target,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            assert account.status_code == 200

            saved_data = json_body({"tag": "#2PP"})
            saved = client.post(
                "/v1/account/saved-tags",
                content=saved_data,
                headers=signed_headers(
                    "/v1/account/saved-tags",
                    method="POST",
                    body=saved_data,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            group_data = json_body({"name": "Main", "tags": ["#2PP"]})
            group = client.post(
                "/v1/account/groups",
                content=group_data,
                headers=signed_headers(
                    "/v1/account/groups",
                    method="POST",
                    body=group_data,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            summary = client.get(
                "/v1/account/summary",
                headers=signed_headers(
                    "/v1/account/summary",
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            export_data = json_body({"format": "google_sheets_scaffold"})
            export = client.post(
                "/v1/account/exports",
                content=export_data,
                headers=signed_headers(
                    "/v1/account/exports",
                    method="POST",
                    body=export_data,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            assert saved.status_code == 200
            assert group.status_code == 201
            assert summary.status_code == 200
            assert export.status_code == 202

            token_request_id = str(uuid4())
            verify_target = "/v1/players/%232PP/verifytoken"
            first_token_body = json_body({"token": "one-time-secret"})
            verified = client.post(
                verify_target,
                content=first_token_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=first_token_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=token_request_id,
                ),
            )
            replay_body = json_body({"token": "different-new-secret"})
            replay = client.post(
                verify_target,
                content=replay_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=replay_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=token_request_id,
                ),
            )
            assert verified.status_code == 200
            assert verified.json() == {"status": "linked", "tag": "#2PP"}
            assert replay.status_code == 200
            assert replay.json() == verified.json()
            assert verifier.calls == 1

            invalid_body = json_body({"token": "must-not-leak", "extra": True})
            invalid = client.post(
                verify_target,
                content=invalid_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=invalid_body,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            assert invalid.status_code == 422
            assert invalid.json() == {"error": "invalid_request"}
            assert "must-not-leak" not in invalid.text

            captured = database.scalar(
                """
                SELECT string_agg(surface, ' ') FROM (
                    SELECT identity_json::text AS surface FROM private_api_requests
                    UNION ALL
                    SELECT response_json::text FROM private_api_requests
                    UNION ALL
                    SELECT outcome FROM player_link_verification_audits
                ) AS captured_surfaces
                """
            )
            assert "one-time-secret" not in captured
            assert "different-new-secret" not in captured
            assert "must-not-leak" not in captured
            assert "body_hash" not in captured


def test_public_player_read_p95_is_below_200_milliseconds(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 6000)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        target = "/v1/players/%232PP"
        durations = []
        with TestClient(app) as client:
            for _index in range(100):
                started = perf_counter()
                response = client.get(target, headers=signed_headers(target))
                durations.append((perf_counter() - started) * 1000)
                assert response.status_code == 200
        p95 = sorted(durations)[94]
        assert p95 < 200, f"saved player read p95 was {p95:.2f} ms"
