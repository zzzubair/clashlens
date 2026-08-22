from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from test_api_db_public_ops import seed_profile
from test_api_migration import migrated_production_database

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase, RequestBinding
from clashlens.hmac_proof import SigningInput, sign
from clashlens.verification import OfficialVerificationResponse

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

    def verify(
        self, normalized_tag: str, player_token: str
    ) -> OfficialVerificationResponse:
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
            search_target = "/v1/players/search?q=Player"
            search = client.get(
                search_target,
                headers=signed_headers(search_target),
            )
            discord_search = client.get(
                search_target,
                headers=signed_headers(search_target, caller="discord-bot"),
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
            assert discord_player.status_code == 403
            assert discord_player.json() == {"error": "caller_operation_not_authorized"}
            assert search.status_code == 200
            assert search.json() == {
                "query": "Player",
                "known_only": True,
                "results": [
                    {
                        "tag": "#2PP",
                        "name": "Player #2PP",
                        "clan": None,
                        "trophies": 6000,
                        "freshness": "stale",
                        "age_seconds": 20_982_400,
                        "observed_at": "2026-08-06T12:00:00+00:00",
                        "public_confidence": "high",
                    }
                ],
            }
            assert discord_search.status_code == 403
            assert discord_search.json() == {"error": "caller_operation_not_authorized"}
            assert leaderboard.status_code == 200
            assert analytics.status_code == 200
            assert public_user.status_code == 404
            assert anonymous_account.status_code == 403
            assert discord_identity.status_code == 403
            assert discord_identity.json() == {
                "error": "caller_operation_not_authorized"
            }

            create_target = "/v1/account"
            create_data = json_body(
                {"username": "ApiOwner", "display_name": "API Owner"}
            )
            discord_data = json_body(
                {"username": "DiscordOwner", "display_name": "Discord Owner"}
            )
            discord_create = client.post(
                create_target,
                content=discord_data,
                headers=signed_headers(
                    create_target,
                    method="POST",
                    body=discord_data,
                    provider="discord",
                    subject="discord-api-owner",
                ),
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
            # Phase 1 login providers are exactly Google and Discord; either
            # identity may create an independent Clash Lens account.
            assert discord_create.status_code == 201
            assert discord_create.json() == {
                "username": "discordowner",
                "display_name": "Discord Owner",
                "preferences": {},
                "providers": ["discord"],
            }
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
            assert export.status_code == 403
            assert export.json() == {"error": "caller_operation_not_authorized"}
            assert database.scalar("SELECT count(*) FROM account_export_requests") == 0
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_export'"
                )
                == 0
            )

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
            replay_body = b"not-json-different-new-secret"
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
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            changed_target = "/v1/players/%238PY/verifytoken"
            changed_binding = client.post(
                changed_target,
                content=b"not-json-changed-binding-secret",
                headers=signed_headers(
                    changed_target,
                    method="POST",
                    body=b"not-json-changed-binding-secret",
                    provider="google",
                    subject="google-api-owner",
                    request_id=token_request_id,
                ),
            )
            assert changed_binding.status_code == 409
            assert changed_binding.json() == {"error": "request_id_conflict"}
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            invalid_body = json_body({"token": "must-not-leak", "extra": True})
            invalid_request_id = str(uuid4())
            invalid = client.post(
                verify_target,
                content=invalid_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=invalid_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=invalid_request_id,
                ),
            )
            assert invalid.status_code == 422
            assert invalid.json() == {"error": "invalid_request"}
            assert "must-not-leak" not in invalid.text
            invalid_retry_body = json_body({"token": "must-not-call-source"})
            invalid_retry = client.post(
                verify_target,
                content=invalid_retry_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=invalid_retry_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=invalid_request_id,
                ),
            )
            assert invalid_retry.status_code == 422
            assert invalid_retry.json() == {"error": "invalid_request"}
            assert "must-not-call-source" not in invalid_retry.text
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            owner_context = database.resolve_account("google", "google-api-owner")
            assert owner_context is not None
            crashed_request_id = str(uuid4())
            crashed_binding = RequestBinding(
                request_id=crashed_request_id,
                caller="typescript-website",
                provider="google",
                provider_subject="google-api-owner",
                account_id=owner_context.internal_id,
                operation="player_links.verify",
                method="POST",
                request_target=verify_target,
                identity={"tag": "#2PP"},
            )
            assert database.reserve_verification(
                crashed_binding, normalized_tag="#2PP"
            ).fresh
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE private_api_requests
                    SET in_progress_until = clock_timestamp() - interval '1 second'
                    WHERE request_id = %s
                    """,
                    (crashed_request_id,),
                )
                connection.commit()
            crashed_body = json_body({"token": "crashed-source-secret"})
            crashed_reuse = client.post(
                verify_target,
                content=crashed_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=crashed_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=crashed_request_id,
                ),
            )
            assert crashed_reuse.status_code == 503
            assert crashed_reuse.json() == {
                "status": "verification_unavailable",
                "tag": "#2PP",
            }
            assert "crashed-source-secret" not in crashed_reuse.text
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            captured = database.scalar(
                """
                SELECT string_agg(surface, ' ') FROM (
                    SELECT identity_json::text AS surface FROM private_api_requests
                    UNION ALL
                    SELECT response_json::text FROM private_api_requests
                    UNION ALL
                    SELECT outcome FROM player_link_verification_audits
                    UNION ALL
                    SELECT state FROM support_player_link_transfer_candidates
                    UNION ALL
                    SELECT operator_identity FROM support_player_link_transfer_audits
                    UNION ALL
                    SELECT reason FROM support_player_link_transfer_audits
                    UNION ALL
                    SELECT input_json::text FROM python_processing_jobs
                ) AS captured_surfaces
                """
            )
            assert "one-time-secret" not in captured
            assert "different-new-secret" not in captured
            assert "must-not-leak" not in captured
            assert "must-not-call-source" not in captured
            assert "changed-binding-secret" not in captured
            assert "crashed-source-secret" not in captured
            assert "body_hash" not in captured
            assert hashlib.sha256(first_token_body).hexdigest() not in captured
            assert hashlib.sha256(replay_body).hexdigest() not in captured
            assert hashlib.sha256(invalid_body).hexdigest() not in captured


def test_linked_elsewhere_requires_one_bounded_support_candidate(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 6000)
        verifier = FakeOfficialVerifier()
        fingerprint = hashlib.sha256(b"safe-support-candidate-key").hexdigest()
        database.register_official_credential(fingerprint)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            verification_client=verifier,
            official_credential_fingerprint=fingerprint,
        )
        try:
            with TestClient(app) as client:
                create_target = "/v1/account"
                first_body = json_body(
                    {"username": "FirstOwner", "display_name": "First Owner"}
                )
                second_body = json_body(
                    {"username": "SecondOwner", "display_name": "Second Owner"}
                )
                for body, subject in (
                    (first_body, "google-first-owner"),
                    (second_body, "google-second-owner"),
                ):
                    response = client.post(
                        create_target,
                        content=body,
                        headers=signed_headers(
                            create_target,
                            method="POST",
                            body=body,
                            provider="google",
                            subject=subject,
                        ),
                    )
                    assert response.status_code == 201
                first = database.resolve_account("google", "google-first-owner")
                second = database.resolve_account("google", "google-second-owner")
                assert first is not None and second is not None
                verify_target = "/v1/players/%232PP/verifytoken"
                first_token = json_body({"token": "first-owner-secret"})
                first_verified = client.post(
                    verify_target,
                    content=first_token,
                    headers=signed_headers(
                        verify_target,
                        method="POST",
                        body=first_token,
                        provider="google",
                        subject="google-first-owner",
                    ),
                )
                assert first_verified.json() == {"status": "linked", "tag": "#2PP"}
                with database.pool.connection() as connection:
                    connection.execute(
                        "DELETE FROM shared_api_permits WHERE credential_fingerprint = %s",
                        (fingerprint,),
                    )
                    connection.commit()
                second_token = json_body({"token": "second-owner-secret"})
                second_verified = client.post(
                    verify_target,
                    content=second_token,
                    headers=signed_headers(
                        verify_target,
                        method="POST",
                        body=second_token,
                        provider="google",
                        subject="google-second-owner",
                    ),
                )

                assert second_verified.status_code == 409
                support_required = second_verified.json()
                assert set(support_required) == {
                    "status",
                    "tag",
                    "verification_request_id",
                }
                assert support_required["status"] == "support_required"
                assert support_required["tag"] == "#2PP"
                assert (
                    str(UUID(support_required["verification_request_id"]))
                    == support_required["verification_request_id"]
                )
                assert (
                    database.scalar("SELECT account_id FROM verified_player_links")
                    == first.internal_id
                )
                with database.pool.connection() as connection:
                    candidate = connection.execute(
                        """
                        SELECT state, from_account_id, to_account_id,
                               expires_at - verified_at = interval '15 minutes'
                        FROM support_player_link_transfer_candidates
                        WHERE verification_request_id = %s
                        """,
                        (support_required["verification_request_id"],),
                    ).fetchone()
                    candidate_count_row = connection.execute(
                        "SELECT count(*) FROM support_player_link_transfer_candidates"
                    ).fetchone()
                assert candidate is not None and candidate_count_row is not None
                candidate_state = (
                    candidate[0].decode("utf-8")
                    if isinstance(candidate[0], bytes)
                    else candidate[0]
                )
                assert candidate_state == "pending"
                assert candidate[1:] == (first.internal_id, second.internal_id, True)
                assert candidate_count_row[0] == 1
                assert verifier.calls == 2
        finally:
            database.close()


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


def test_leaderboard_rejects_misaligned_selectors_and_missing_pages(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        try:
            with TestClient(app) as client:
                for target in (
                    "/v1/leaderboards/live?limit=100&offset=1",
                    "/v1/leaderboards/frozen?official_season_id=2026-08",
                ):
                    assert client.get(target, headers=signed_headers(target)).status_code == 422
                missing = "/v1/leaderboards/live?limit=100&offset=100"
                assert client.get(missing, headers=signed_headers(missing)).status_code == 404
        finally:
            database.close()
