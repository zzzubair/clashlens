from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from clashlens_prototype.api_db import ApiDatabase, RequestBinding
from clashlens_prototype.verification import KeyAction, VerificationOutcome
from test_api_migration import migrated_production_database

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def create_account(database: ApiDatabase, subject: str, username: str) -> int:
    result = database.create_account(
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="google",
            provider_subject=subject,
            account_id=None,
            operation="account.create",
            method="POST",
            request_target="/v1/account",
            identity={"username": username},
        ),
        username=username,
        normalized_username=username,
        display_name=username,
    )
    assert result.status_code == 201
    account = database.resolve_account("google", subject)
    assert account is not None
    return account.internal_id


def verification_binding(account_id: int, subject: str, tag: str) -> RequestBinding:
    return RequestBinding(
        request_id=str(uuid4()),
        caller="typescript-website",
        provider="google",
        provider_subject=subject,
        account_id=account_id,
        operation="player_links.verify",
        method="POST",
        request_target=f"/v1/players/{tag}/verifytoken",
        identity={"tag": tag},
    )


def test_shared_traffic_gate_enforces_non_borrowing_and_combined_budgets(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info, max_size=16)
        fingerprint = sha256(b"safe-synthetic-key").hexdigest()
        try:
            database.register_official_credential(fingerprint)

            def acquire_go(index: int):
                return database.acquire_official_permit(
                    fingerprint,
                    caller="go",
                    request_id=str(uuid4()),
                )

            with ThreadPoolExecutor(max_workers=12) as executor:
                go_results = list(executor.map(acquire_go, range(30)))

            python_first = database.acquire_official_permit(
                fingerprint,
                caller="python",
                request_id=str(uuid4()),
            )
            python_second = database.acquire_official_permit(
                fingerprint,
                caller="python",
                request_id=str(uuid4()),
            )

            assert sum(result.granted for result in go_results) == 29
            assert python_first.granted is True
            assert python_second.granted is False
            assert python_second.reason == "python_budget_exhausted"
            assert database.scalar(
                "SELECT count(*) FROM shared_api_permits"
            ) == 30
        finally:
            database.close()


def test_gate_cooldown_and_quarantine_persist_and_fail_closed(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        fingerprint = sha256(b"safe-synthetic-key-two").hexdigest()
        try:
            database.register_official_credential(fingerprint)
            database.apply_official_key_action(
                fingerprint,
                KeyAction.COOLDOWN,
                cooldown_seconds=30,
            )
            cooldown = database.acquire_official_permit(
                fingerprint, caller="python", request_id=str(uuid4())
            )
            database.apply_official_key_action(
                fingerprint,
                KeyAction.QUARANTINE,
                cooldown_seconds=30,
            )
            quarantined = database.acquire_official_permit(
                fingerprint, caller="python", request_id=str(uuid4())
            )

            assert cooldown.granted is False
            assert cooldown.reason == "credential_cooldown"
            assert quarantined.granted is False
            assert quarantined.reason == "credential_quarantined"
            assert database.scalar(
                "SELECT quarantine_reason FROM shared_api_credentials"
            ) == "verified_authentication_failure"
        finally:
            database.close()


def test_concurrent_verified_links_never_transfer_between_accounts_automatically(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info, max_size=8)
        try:
            first_account = create_account(database, "google-first", "firstowner")
            second_account = create_account(database, "google-second", "secondowner")
            first_binding = verification_binding(
                first_account, "google-first", "#2PP"
            )
            second_binding = verification_binding(
                second_account, "google-second", "#2PP"
            )
            assert database.reserve_verification(first_binding, normalized_tag="#2PP").fresh
            assert database.reserve_verification(second_binding, normalized_tag="#2PP").fresh

            def complete(item: tuple[RequestBinding, int]):
                request, account_id = item
                return database.complete_verification(
                    request,
                    normalized_tag="#2PP",
                    outcome=VerificationOutcome.VERIFIED,
                    account_id=account_id,
                    completed_at=NOW,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        complete,
                        [(first_binding, first_account), (second_binding, second_account)],
                    )
                )

            assert {result.payload["status"] for result in results} == {
                "linked",
                "support_required",
            }
            assert database.scalar("SELECT count(*) FROM verified_player_links") == 1
            assert database.scalar(
                "SELECT count(*) FROM support_player_link_transfer_candidates"
            ) == 1
            owner_before = database.scalar(
                "SELECT account_id FROM verified_player_links"
            )
            support_result = next(
                result for result in results if result.payload["status"] == "support_required"
            )
            transferred = database.apply_support_player_link_transfer(
                verification_request_id=support_result.payload["verification_request_id"],
                operator_identity="support-operator",
                reason="Fresh proof reviewed with the account owner.",
                now=NOW + timedelta(minutes=1),
            )
            owner_after = database.scalar("SELECT account_id FROM verified_player_links")

            assert transferred.payload == {"status": "transferred", "tag": "#2PP"}
            assert owner_after != owner_before
            assert database.scalar(
                "SELECT count(*) FROM support_player_link_transfer_audits"
            ) == 1
        finally:
            database.close()


def test_verification_request_replay_never_binds_or_persists_a_new_token(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_account(database, "google-replay", "replayowner")
            request = verification_binding(account_id, "google-replay", "#8PY")
            reservation = database.reserve_verification(request, normalized_tag="#8PY")
            completed = database.complete_verification(
                request,
                normalized_tag="#8PY",
                outcome=VerificationOutcome.INVALID_TOKEN,
                account_id=account_id,
                completed_at=NOW,
            )
            replay = database.reserve_verification(request, normalized_tag="#8PY")

            assert reservation.fresh is True
            assert completed.payload == {"status": "invalid_token", "tag": "#8PY"}
            assert replay.fresh is False
            assert replay.result is not None
            assert replay.result.status_code == completed.status_code
            assert replay.result.payload == completed.payload
            assert replay.result.replayed is True
            captured = database.scalar(
                """
                SELECT string_agg(value, ' ') FROM (
                    SELECT identity_json::text AS value FROM private_api_requests
                    UNION ALL
                    SELECT response_json::text FROM private_api_requests
                    UNION ALL
                    SELECT outcome FROM player_link_verification_audits
                ) AS safe_surfaces
                """
            )
            assert "one-time-secret" not in captured
            assert "body_hash" not in captured
            fingerprint = sha256(b"safe-conflict-key").hexdigest()
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO shared_api_credentials (credential_fingerprint)
                    VALUES (%s)
                    """,
                    (fingerprint,),
                )
                connection.commit()
            database.register_official_credential(fingerprint)
            assert database.scalar(
                "SELECT total_budget FROM shared_api_credentials WHERE credential_fingerprint = %s",
                (fingerprint,),
            ) == 30
        finally:
            database.close()
