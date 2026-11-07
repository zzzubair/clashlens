from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

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

# New verification attempts one account may start in any minute.
VERIFICATION_ATTEMPTS_PER_MINUTE = 6
# How long a support transfer candidate can be used after its verification.
TRANSFER_CANDIDATE_MINUTES = 15


class CredentialState(StrEnum):
    """Health of one shared official API key (``shared_api_credentials.state``).

    States and events:
    - ``ACTIVE``: requests may start, within the per-second budgets.
    - ``COOLDOWN``: a rate-limit or overload answer (``KeyAction.COOLDOWN``)
      set it, or pushed ``cooldown_until`` later; the first permit request
      after ``cooldown_until`` makes it ``ACTIVE`` again.
    - ``QUARANTINED``: a verified authentication failure
      (``KeyAction.QUARANTINE``) set it. It never leaves on its own, and a
      later cooldown leaves it quarantined.
    - ``RETIRED``: allowed by the database, never set by the current code;
      such a key is refused as ``credential_inactive``.

    Owner: the state lives in PostgreSQL so website verification and
    interactive collection share it; ``apply_official_key_action`` moves it
    into ``COOLDOWN`` or ``QUARANTINED`` and the SQL function
    ``clashlens_acquire_shared_api_permit`` is the only place it returns to
    ``ACTIVE`` and the only place permits are granted. A restart keeps it.
    """

    ACTIVE = "active"
    COOLDOWN = "cooldown"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class PermitAnswer(StrEnum):
    """Why one request for a permit to call the official API was or wasn't
    granted (``PermitResult.reason``). A refused caller does not start a
    request; budgets are per second, so the next try may be granted."""

    GRANTED = "granted"
    CREDENTIAL_UNKNOWN = "credential_unknown"
    CREDENTIAL_QUARANTINED = "credential_quarantined"
    CREDENTIAL_COOLDOWN = "credential_cooldown"
    CREDENTIAL_INACTIVE = "credential_inactive"
    PYTHON_BUDGET_EXHAUSTED = "python_budget_exhausted"
    COMBINED_BUDGET_EXHAUSTED = "combined_budget_exhausted"


def permit_answer(
    granted: bool, state: str, budget: Callable[[], tuple[int, int, int, int]]
) -> PermitAnswer:
    """Name the shared gate's decision. ``budget`` reads this caller's and
    every caller's permits in the last second and their two budgets; it is
    read only for an active key that was refused."""
    if granted:
        return PermitAnswer.GRANTED
    if state == CredentialState.QUARANTINED:
        return PermitAnswer.CREDENTIAL_QUARANTINED
    if state == CredentialState.COOLDOWN:
        return PermitAnswer.CREDENTIAL_COOLDOWN
    if state != CredentialState.ACTIVE:
        return PermitAnswer.CREDENTIAL_INACTIVE
    python_count, total_count, python_budget, total_budget = budget()
    if python_count >= python_budget:
        return PermitAnswer.PYTHON_BUDGET_EXHAUSTED
    if total_count >= total_budget:
        return PermitAnswer.COMBINED_BUDGET_EXHAUSTED
    return PermitAnswer.CREDENTIAL_INACTIVE


class VerificationAnswer(StrEnum):
    """One player-verification request, as
    ``player_link_verification_audits.outcome`` records it.

    ``reserve_verification`` saves it ``PENDING`` with the request's
    ``private_api_requests`` row (``api_db.RequestPhase``). Exactly one final
    answer then replaces ``PENDING``, in the transaction that saves the
    request's response: ``complete_verification`` (``verification_answer``
    picks it), ``complete_invalid_verification_request``, or, once the
    45-second reservation has passed without an answer (a crashed or stuck
    request), the next retry of the same request ID, which records
    ``UNAVAILABLE``. A retry after that replays the saved response. A
    verified player owned by another account is never moved here:
    ``SUPPORT_REQUIRED`` saves a ``TransferCandidate`` instead. ``VERIFIED``
    is allowed by the database and never written by the current code.
    """

    PENDING = "pending"
    INVALID_REQUEST = "invalid_request"
    INVALID_TOKEN = "invalid_token"
    UNAVAILABLE = "verification_unavailable"
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    SUPPORT_REQUIRED = "support_required"
    VERIFIED = "verified"


def verification_answer(
    outcome: VerificationOutcome, owner_account_id: int | None, account_id: int
) -> VerificationAnswer:
    """The final answer for an official check, given who owns the player's
    verified link now (None when nobody does)."""
    if outcome is VerificationOutcome.INVALID_TOKEN:
        return VerificationAnswer.INVALID_TOKEN
    if outcome is VerificationOutcome.UNAVAILABLE:
        return VerificationAnswer.UNAVAILABLE
    if owner_account_id is None:
        return VerificationAnswer.LINKED
    if owner_account_id == account_id:
        return VerificationAnswer.ALREADY_LINKED
    return VerificationAnswer.SUPPORT_REQUIRED


_ANSWER_STATUS = {
    VerificationAnswer.INVALID_TOKEN: 401,
    VerificationAnswer.UNAVAILABLE: 503,
    VerificationAnswer.LINKED: 200,
    VerificationAnswer.ALREADY_LINKED: 200,
    VerificationAnswer.SUPPORT_REQUIRED: 409,
}


class TransferCandidate(StrEnum):
    """A request to move a verified player to the account that just verified
    it while another account owns it
    (``support_player_link_transfer_candidates.state``).

    - ``PENDING``: saved by ``complete_verification`` with the
      ``SUPPORT_REQUIRED`` answer; usable for ``TRANSFER_CANDIDATE_MINUTES``.
    - ``CONSUMED``: a maintainer's transfer moved the link to the new owner.
      ``COMPLETED`` is an older name for the same end and is read the same.
    - ``EXPIRED``: a transfer was tried after the candidate's time ran out;
      the Clasher must verify again.

    Owner: the SQL function ``clashlens_support_transfer`` (migration 0094),
    run only by the support role, is the only place a candidate moves. It
    rechecks the current owner under lock (``TransferOutcome.LINK_OWNER_CHANGED``
    leaves the candidate ``PENDING``) and replays ``TRANSFERRED`` to the same
    operator and reason, so a retried transfer moves nothing twice. A pending
    candidate nobody uses just stays ``PENDING`` and can no longer transfer.
    Moving the link fires the same triggers as unlinking (``PlayerUnlink``).
    """

    PENDING = "pending"
    CONSUMED = "consumed"
    COMPLETED = "completed"
    EXPIRED = "expired"


class TransferOutcome(StrEnum):
    """The status ``clashlens_support_transfer`` returns."""

    TRANSFERRED = "transferred"
    TRANSFER_NOT_FOUND = "transfer_not_found"
    TRANSFER_CONFLICT = "transfer_conflict"
    FRESH_VERIFICATION_REQUIRED = "fresh_verification_required"
    # Unreachable while the table's state check allows only the four states.
    TRANSFER_NOT_PENDING = "transfer_not_pending"
    LINK_OWNER_CHANGED = "link_owner_changed"


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
                return PermitResult(False, PermitAnswer.CREDENTIAL_UNKNOWN)
            decision = connection.execute(
                """
                SELECT granted, credential_state
                FROM clashlens_acquire_shared_api_permit(%s, 'python')
                """,
                (fingerprint,),
            ).fetchone()
            assert decision is not None

            def budget() -> tuple[int, int, int, int]:
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
                return int(counts[0]), int(counts[1]), int(budgets[0]), int(budgets[1])

            answer = permit_answer(bool(decision[0]), _text(decision[1]), budget)
            return PermitResult(answer is PermitAnswer.GRANTED, answer)


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
            # One account's attempts take turns, so simultaneous ones see each other.
            connection.execute(
                "SELECT 1 FROM clash_lens_accounts WHERE id = %s FOR NO KEY UPDATE",
                (binding.account_id,),
            )
            # A retry of a recorded request replays its answer without spending an attempt.
            recorded = connection.execute(
                "SELECT 1 FROM private_api_requests WHERE request_id = %s",
                (binding.request_id,),
            ).fetchone()
            if recorded is None:
                attempts, retry_after = connection.execute(
                    """
                    SELECT count(*),
                           ceil(extract(epoch FROM
                               min(created_at) + interval '1 minute' - statement_timestamp()
                           ))::int
                    FROM player_link_verification_audits
                    WHERE account_id = %s
                      AND created_at > statement_timestamp() - interval '1 minute'
                    """,
                    (binding.account_id,),
                ).fetchone()
                if attempts >= VERIFICATION_ATTEMPTS_PER_MINUTE:
                    return VerificationReservation(
                        False,
                        OperationResult(
                            429,
                            {
                                "error": "rate_limited",
                                "retry_after_seconds": max(1, retry_after),
                            },
                        ),
                    )
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
                ) VALUES (%s, %s, %s, %s)
                """,
                (binding.request_id, binding.account_id, player_id, VerificationAnswer.PENDING),
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
            api_db._lock_api_player(connection, normalized_tag)
            player = connection.execute(
                """
                SELECT id FROM players WHERE normalized_tag = %s
                FOR NO KEY UPDATE NOWAIT
                """,
                (normalized_tag,),
            ).fetchone()
            if player is None:
                raise RuntimeError("verification player reservation was lost")
            player_id = int(player[0])
            link = (
                None
                if outcome is not VerificationOutcome.VERIFIED
                else connection.execute(
                    """
                    SELECT account_id FROM verified_player_links
                    WHERE player_id = %s FOR UPDATE
                    """,
                    (player_id,),
                ).fetchone()
            )
            answer = verification_answer(
                outcome, None if link is None else int(link[0]), account_id
            )
            payload: dict[str, str] = {"status": answer, "tag": normalized_tag}
            if answer is VerificationAnswer.LINKED:
                connection.execute(
                    """
                    INSERT INTO verified_player_links (
                        player_id, account_id, verification_request_id,
                        verified_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (player_id, account_id, binding.request_id, completed_at, completed_at),
                )
            elif answer is VerificationAnswer.ALREADY_LINKED:
                connection.execute(
                    """
                    UPDATE verified_player_links
                    SET verification_request_id = %s, updated_at = %s
                    WHERE player_id = %s
                    """,
                    (binding.request_id, completed_at, player_id),
                )
            elif answer is VerificationAnswer.SUPPORT_REQUIRED:
                assert link is not None
                connection.execute(
                    """
                    INSERT INTO support_player_link_transfer_candidates (
                        verification_request_id, player_id, from_account_id,
                        to_account_id, verified_at, expires_at, state
                    ) VALUES (%s, %s, %s, %s, %s, %s + make_interval(mins => %s), %s)
                    """,
                    (
                        binding.request_id,
                        player_id,
                        int(link[0]),
                        account_id,
                        completed_at,
                        completed_at,
                        TRANSFER_CANDIDATE_MINUTES,
                        TransferCandidate.PENDING,
                    ),
                )
                payload["verification_request_id"] = binding.request_id
            result = OperationResult(_ANSWER_STATUS[answer], payload)
            if answer in (VerificationAnswer.LINKED, VerificationAnswer.ALREADY_LINKED):
                # Check a linked player's profile at once, as a player page
                # visit does, so its name and league show and its own profile
                # decides whether it is tracked.
                connection.execute(
                    "SELECT 1 FROM clashlens_enqueue_interactive('initial_collection', %s, 30, true)",
                    (normalized_tag,),
                ).fetchone()
            updated = connection.execute(
                """
                UPDATE player_link_verification_audits
                SET outcome = %s, completed_at = %s
                WHERE request_id = %s AND outcome = %s
                """,
                (answer, completed_at, binding.request_id, VerificationAnswer.PENDING),
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
                SET outcome = %s, completed_at = %s
                WHERE request_id = %s AND outcome = %s
                """,
                (
                    VerificationAnswer.INVALID_REQUEST,
                    completed_at,
                    binding.request_id,
                    VerificationAnswer.PENDING,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("verification audit reservation was lost")
            api_db._complete_request(connection, binding.request_id, result)
            return result


