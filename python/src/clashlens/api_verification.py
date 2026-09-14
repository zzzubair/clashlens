from __future__ import annotations

from datetime import datetime

from . import api_db
from .api_db import (
    ApiDatabase,
    OperationResult,
    PermitResult,
    RequestBinding,
    VerificationReservation,
    _text,
)
from .verification import KeyAction, VerificationOutcome


def register_official_credential(database: ApiDatabase, fingerprint: str) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO shared_api_credentials (credential_fingerprint)
                VALUES (%s)
                ON CONFLICT (credential_fingerprint) DO NOTHING
                """,
                (fingerprint,),
            )
            row = connection.execute(
                """
                SELECT collector_budget, python_budget, total_budget
                FROM shared_api_credentials
                WHERE credential_fingerprint = %s
                FOR UPDATE
                """,
                (fingerprint,),
            ).fetchone()
            if row is None or tuple(map(int, row)) != (29, 1, 30):
                raise RuntimeError("conflicting official credential registration")


def acquire_official_permit(
    database: ApiDatabase,
    fingerprint: str,
    *,
    request_id: str,
) -> PermitResult:
    del request_id  # The shared PostgreSQL gate owns the permit identity.
    with database.pool.connection() as connection:
        with connection.transaction():
            registered = connection.execute(
                """
                SELECT 1
                FROM shared_api_credentials
                WHERE credential_fingerprint = %s
                """,
                (fingerprint,),
            ).fetchone()
            if registered is None:
                return PermitResult(False, "credential_unknown")
            decision = connection.execute(
                """
                SELECT granted, credential_state
                FROM clashlens_acquire_shared_api_permit(%s, 'python')
                """,
                (fingerprint,),
            ).fetchone()
            assert decision is not None
            if bool(decision[0]):
                return PermitResult(True, "granted")
            state = _text(decision[1])
            if state == "quarantined":
                return PermitResult(False, "credential_quarantined")
            if state == "cooldown":
                return PermitResult(False, "credential_cooldown")
            if state != "active":
                return PermitResult(False, "credential_inactive")
            counts = connection.execute(
                """
                SELECT count(*) FILTER (WHERE caller = %s), count(*)
                FROM shared_api_permits
                WHERE credential_fingerprint = %s
                  AND permitted_at > clock_timestamp() - interval '1 second'
                """,
                ("python", fingerprint),
            ).fetchone()
            budgets = connection.execute(
                """
                SELECT credential.python_budget, credential.total_budget
                FROM shared_api_credentials AS credential
                WHERE credential.credential_fingerprint = %s
                """,
                (fingerprint,),
            ).fetchone()
            assert counts is not None and budgets is not None
            if int(counts[0]) >= int(budgets[0]):
                return PermitResult(False, "python_budget_exhausted")
            if int(counts[1]) >= int(budgets[1]):
                return PermitResult(False, "combined_budget_exhausted")
            return PermitResult(False, "credential_inactive")


def apply_official_key_action(
    database: ApiDatabase,
    fingerprint: str,
    action: KeyAction,
    *,
    cooldown_seconds: int,
) -> None:
    if not 1 <= cooldown_seconds <= 300:
        raise ValueError(
            "official credential cooldown is outside the supported range"
        )
    if action is KeyAction.NONE:
        return
    with database.pool.connection() as connection:
        with connection.transaction():
            if action is KeyAction.COOLDOWN:
                connection.execute(
                    """
                    UPDATE shared_api_credentials
                    SET state = CASE WHEN state = 'quarantined' THEN state ELSE 'cooldown' END,
                        cooldown_until = CASE
                            WHEN state = 'quarantined' THEN cooldown_until
                            ELSE GREATEST(
                                COALESCE(cooldown_until, '-infinity'::timestamptz),
                                clock_timestamp() + make_interval(secs => %s)
                            )
                        END,
                        updated_at = clock_timestamp()
                    WHERE credential_fingerprint = %s
                    """,
                    (cooldown_seconds, fingerprint),
                )
            elif action is KeyAction.QUARANTINE:
                connection.execute(
                    """
                    UPDATE shared_api_credentials
                    SET state = 'quarantined', cooldown_until = NULL,
                        quarantine_reason = 'verified_authentication_failure',
                        updated_at = clock_timestamp()
                    WHERE credential_fingerprint = %s
                    """,
                    (fingerprint,),
                )


def reserve_verification(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    normalized_tag: str,
) -> VerificationReservation:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, 
                connection,
                binding,
                recover_expired_verification=True,
            )
            if existing is not None:
                return VerificationReservation(False, existing)
            player_id = api_db._ensure_player(connection, normalized_tag)
            connection.execute(
                """
                INSERT INTO player_link_verification_audits (
                    request_id, account_id, player_id, outcome
                ) VALUES (%s, %s, %s, 'pending')
                """,
                (binding.request_id, binding.account_id, player_id),
            )
            return VerificationReservation(True, None)


def complete_verification(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    normalized_tag: str,
    outcome: VerificationOutcome,
    account_id: int,
    completed_at: datetime,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            api_db._assert_request_binding(connection, binding)
            player = connection.execute(
                """
                SELECT id FROM players WHERE normalized_tag = %s FOR UPDATE
                """,
                (normalized_tag,),
            ).fetchone()
            if player is None:
                raise RuntimeError("verification player reservation was lost")
            player_id = int(player[0])
            if outcome is VerificationOutcome.INVALID_TOKEN:
                audit_outcome = "invalid_token"
                result = OperationResult(
                    401, {"status": "invalid_token", "tag": normalized_tag}
                )
            elif outcome is VerificationOutcome.UNAVAILABLE:
                audit_outcome = "verification_unavailable"
                result = OperationResult(
                    503,
                    {"status": "verification_unavailable", "tag": normalized_tag},
                )
            else:
                link = connection.execute(
                    """
                    SELECT account_id FROM verified_player_links
                    WHERE player_id = %s FOR UPDATE
                    """,
                    (player_id,),
                ).fetchone()
                if link is None:
                    connection.execute(
                        """
                        INSERT INTO verified_player_links (
                            player_id, account_id, verification_request_id,
                            verified_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            player_id,
                            account_id,
                            binding.request_id,
                            completed_at,
                            completed_at,
                        ),
                    )
                    audit_outcome = "linked"
                    result = OperationResult(
                        200, {"status": "linked", "tag": normalized_tag}
                    )
                elif int(link[0]) == account_id:
                    connection.execute(
                        """
                        UPDATE verified_player_links
                        SET verification_request_id = %s, verified_at = %s,
                            updated_at = %s
                        WHERE player_id = %s
                        """,
                        (
                            binding.request_id,
                            completed_at,
                            completed_at,
                            player_id,
                        ),
                    )
                    audit_outcome = "already_linked"
                    result = OperationResult(
                        200, {"status": "already_linked", "tag": normalized_tag}
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO support_player_link_transfer_candidates (
                            verification_request_id, player_id, from_account_id,
                            to_account_id, verified_at, expires_at, state
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s + interval '15 minutes', 'pending'
                        )
                        """,
                        (
                            binding.request_id,
                            player_id,
                            int(link[0]),
                            account_id,
                            completed_at,
                            completed_at,
                        ),
                    )
                    audit_outcome = "support_required"
                    result = OperationResult(
                        409,
                        {
                            "status": "support_required",
                            "tag": normalized_tag,
                            "verification_request_id": binding.request_id,
                        },
                    )
            updated = connection.execute(
                """
                UPDATE player_link_verification_audits
                SET outcome = %s, completed_at = %s
                WHERE request_id = %s AND outcome = 'pending'
                """,
                (audit_outcome, completed_at, binding.request_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("verification audit reservation was lost")
            api_db._complete_request(connection, binding.request_id, result)
            return result


def complete_invalid_verification_request(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    completed_at: datetime,
) -> OperationResult:
    """Close a reserved request after safe body validation fails."""
    with database.pool.connection() as connection:
        with connection.transaction():
            api_db._assert_request_binding(connection, binding)
            result = OperationResult(422, {"error": "invalid_request"})
            updated = connection.execute(
                """
                UPDATE player_link_verification_audits
                SET outcome = 'invalid_request', completed_at = %s
                WHERE request_id = %s AND outcome = 'pending'
                """,
                (completed_at, binding.request_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("verification audit reservation was lost")
            api_db._complete_request(connection, binding.request_id, result)
            return result


