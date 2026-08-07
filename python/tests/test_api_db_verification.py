from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import psycopg
import pytest
from test_api_migration import migrated_production_database

from clashlens.api_db import ApiDatabase, RequestBinding
from clashlens.verification import KeyAction, VerificationOutcome

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SUPPORT_ROLE = "clashlens_support_transfer"


def support_connection(connection_info: str) -> psycopg.Connection:
    connection = psycopg.connect(connection_info, autocommit=True)
    try:
        connection.execute("SET SESSION AUTHORIZATION clashlens_support_transfer")
    except psycopg.errors.InsufficientPrivilege:
        connection.close()
        pytest.skip("support transfer integration requires SET SESSION AUTHORIZATION")
    return connection


def call_support_transfer(
    connection: psycopg.Connection,
    *,
    verification_request_id: str,
    player_tag: str,
    from_account_public_id: str,
    to_account_public_id: str,
    operator_identity: str,
    reason: str,
) -> tuple[str, str | None]:
    row = connection.execute(
        """
        SELECT status, tag
        FROM clashlens_support_transfer(%s, %s, %s, %s, %s, %s)
        """,
        (
            verification_request_id,
            player_tag,
            from_account_public_id,
            to_account_public_id,
            operator_identity,
            reason,
        ),
    ).fetchone()
    assert row is not None
    status = row[0].decode("utf-8") if isinstance(row[0], bytes) else str(row[0])
    tag = None if row[1] is None else (
        row[1].decode("utf-8") if isinstance(row[1], bytes) else str(row[1])
    )
    return status, tag


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
            candidate = database.scalar(
                """
                SELECT state
                FROM support_player_link_transfer_candidates
                WHERE verification_request_id = %s
                """,
                (support_result.payload["verification_request_id"],),
            )

            assert candidate == "pending"
            assert database.scalar("SELECT account_id FROM verified_player_links") == owner_before
            assert database.scalar(
                "SELECT count(*) FROM support_player_link_transfer_audits"
            ) == 0
        finally:
            database.close()


def test_support_transfer_is_atomic_restricted_and_idempotent(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info, max_size=8)
        try:
            assert not hasattr(database, "apply_support_player_link_transfer")
            first_account = create_account(database, "google-support-first", "supportfirst")
            second_account = create_account(database, "google-support-second", "supportsecond")
            first_context = database.resolve_account("google", "google-support-first")
            second_context = database.resolve_account("google", "google-support-second")
            assert first_context is not None and second_context is not None
            completed_at = datetime.now(UTC)
            first_binding = verification_binding(
                first_account, "google-support-first", "#2PP"
            )
            assert database.reserve_verification(first_binding, normalized_tag="#2PP").fresh
            assert database.complete_verification(
                first_binding,
                normalized_tag="#2PP",
                outcome=VerificationOutcome.VERIFIED,
                account_id=first_account,
                completed_at=completed_at,
            ).payload == {"status": "linked", "tag": "#2PP"}
            second_binding = verification_binding(
                second_account, "google-support-second", "#2PP"
            )
            assert database.reserve_verification(second_binding, normalized_tag="#2PP").fresh
            support_required = database.complete_verification(
                second_binding,
                normalized_tag="#2PP",
                outcome=VerificationOutcome.VERIFIED,
                account_id=second_account,
                completed_at=completed_at,
            )
            assert support_required.payload["status"] == "support_required"
            candidate_id = support_required.payload["verification_request_id"]
            assert database.scalar("SELECT account_id FROM verified_player_links") == first_account
            assert database.scalar(
                """
                SELECT expires_at - verified_at = interval '15 minutes'
                FROM support_player_link_transfer_candidates
                WHERE verification_request_id = %s
                """,
                (candidate_id,),
            ) is True

            with support_connection(connection_info) as support:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    support.execute("SELECT account_id FROM verified_player_links")
                assert call_support_transfer(
                    support,
                    verification_request_id=candidate_id,
                    player_tag="#8PY",
                    from_account_public_id=first_context.public_id,
                    to_account_public_id=second_context.public_id,
                    operator_identity="sudo:operator:1000",
                    reason="Fresh verification was reviewed.",
                ) == ("transfer_conflict", None)

            def transfer_once() -> tuple[str, str | None]:
                with support_connection(connection_info) as support:
                    return call_support_transfer(
                        support,
                        verification_request_id=candidate_id,
                        player_tag="#2PP",
                        from_account_public_id=first_context.public_id,
                        to_account_public_id=second_context.public_id,
                        operator_identity="sudo:operator:1000",
                        reason="Fresh verification was reviewed.",
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                transfer_results = list(executor.map(lambda _item: transfer_once(), range(2)))
            assert transfer_results == [("transferred", "#2PP"), ("transferred", "#2PP")]
            assert database.scalar("SELECT account_id FROM verified_player_links") == second_account
            assert database.scalar(
                "SELECT state FROM support_player_link_transfer_candidates WHERE verification_request_id = %s",
                (candidate_id,),
            ) == "consumed"
            with database.pool.connection() as connection:
                audit = connection.execute(
                    """
                    SELECT operator_identity, reason
                    FROM support_player_link_transfer_audits
                    WHERE verification_request_id = %s
                    """,
                    (candidate_id,),
                ).fetchone()
            assert audit is not None
            assert tuple(
                value.decode("utf-8") if isinstance(value, bytes) else value
                for value in audit
            ) == ("sudo:operator:1000", "Fresh verification was reviewed.")
            assert transfer_once() == ("transferred", "#2PP")
            with support_connection(connection_info) as support:
                assert call_support_transfer(
                    support,
                    verification_request_id=candidate_id,
                    player_tag="#2PP",
                    from_account_public_id=first_context.public_id,
                    to_account_public_id=second_context.public_id,
                    operator_identity="sudo:operator:1000",
                    reason="A different reason must not replay the transfer.",
                ) == ("transfer_conflict", None)

            third_account = create_account(database, "google-support-third", "supportthird")
            third_context = database.resolve_account("google", "google-support-third")
            assert third_context is not None
            third_binding = verification_binding(
                third_account, "google-support-third", "#2PP"
            )
            assert database.reserve_verification(third_binding, normalized_tag="#2PP").fresh
            expired_support = database.complete_verification(
                third_binding,
                normalized_tag="#2PP",
                outcome=VerificationOutcome.VERIFIED,
                account_id=third_account,
                completed_at=datetime.now(UTC),
            )
            expired_candidate_id = expired_support.payload["verification_request_id"]
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE support_player_link_transfer_candidates
                    SET verified_at = clock_timestamp() - interval '16 minutes',
                        expires_at = clock_timestamp() - interval '1 second'
                    WHERE verification_request_id = %s
                    """,
                    (expired_candidate_id,),
                )
                connection.commit()
            with support_connection(connection_info) as support:
                assert call_support_transfer(
                    support,
                    verification_request_id=expired_candidate_id,
                    player_tag="#2PP",
                    from_account_public_id=second_context.public_id,
                    to_account_public_id=third_context.public_id,
                    operator_identity="sudo:operator:1000",
                    reason="Fresh verification was reviewed.",
                ) == ("fresh_verification_required", None)
            assert database.scalar("SELECT account_id FROM verified_player_links") == second_account
            assert database.scalar(
                """
                SELECT state FROM support_player_link_transfer_candidates
                WHERE verification_request_id = %s
                """,
                (expired_candidate_id,),
            ) == "expired"
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
                UNION ALL
                SELECT state FROM support_player_link_transfer_candidates
                UNION ALL
                SELECT operator_identity FROM support_player_link_transfer_audits
                UNION ALL
                SELECT reason FROM support_player_link_transfer_audits
                UNION ALL
                SELECT input_json::text FROM python_processing_jobs
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


def test_verification_reservation_has_recovery_state_and_stale_reuse_fails_closed(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_account(database, "google-crash", "crashowner")
            request = verification_binding(account_id, "google-crash", "#9PY")
            reservation = database.reserve_verification(request, normalized_tag="#9PY")

            assert reservation.fresh is True
            assert database.scalar(
                "SELECT state FROM private_api_requests WHERE request_id = %s",
                (request.request_id,),
            ) == "in_progress"

            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE private_api_requests
                    SET in_progress_until = %s, created_at = %s
                    WHERE request_id = %s
                    """,
                    (NOW - timedelta(seconds=1), NOW - timedelta(minutes=5), request.request_id),
                )
                connection.commit()

            reused = database.reserve_verification(request, normalized_tag="#9PY")

            assert reused.fresh is False
            assert reused.result is not None
            assert reused.result.status_code == 503
            assert reused.result.payload == {
                "status": "verification_unavailable",
                "tag": "#9PY",
            }
            assert database.scalar(
                "SELECT outcome FROM player_link_verification_audits WHERE request_id = %s",
                (request.request_id,),
            ) == "verification_unavailable"
        finally:
            database.close()
