"""Request identity, reservation and replay for private API operations."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from .db_rows import text_value

REQUEST_RESERVATION_SECONDS = 45


@dataclass(frozen=True, slots=True)
class AccountContext:
    internal_id: int
    public_id: str
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RequestBinding:
    request_id: str
    caller: str
    provider: str
    provider_subject: str
    account_id: int | None
    operation: str
    method: str
    request_target: str
    identity: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OperationResult:
    status_code: int
    payload: dict[str, Any]
    replayed: bool = False


class RequestPhase(StrEnum):
    """Where one private API request ID stands (``private_api_requests``).

    The stored ``state`` is ``in_progress`` or ``complete``; these phases add
    what it does not say:

    - ``CONFLICT``: the ID is saved for a different caller, account,
      operation, target or body. Answer 409 ``request_id_conflict`` and
      change nothing.
    - ``IN_PROGRESS``: another attempt holds it. Answer 202 ``in_progress``.
    - ``DEADLINE_PASSED``: an in-progress player verification whose
      45-second reservation ran out (its attempt crashed or got stuck). The
      retry records the verification unavailable and saves that answer.
    - ``COMPLETE``: replay the saved response.

    Events: ``reserve_request`` saves a new ID as ``in_progress``; the
    operation then saves its response with ``complete_request`` in the same
    transaction, so a failure or crash before that rolls both back and a
    retry starts afresh. Only verification commits its reservation before
    calling the game, which is why only it can reach ``DEADLINE_PASSED``.
    """

    CONFLICT = "conflict"
    IN_PROGRESS = "in_progress"
    DEADLINE_PASSED = "deadline_passed"
    COMPLETE = "complete"

# The saved request's identity, state and deadline, in request_phase order.
_REQUEST_COLUMNS = """caller, provider, provider_subject, account_id, operation,
               method, request_target, identity_json, state, in_progress_until"""


def request_phase(
    row: Any, binding: RequestBinding, clock: Callable[[], datetime] | None = None
) -> RequestPhase:
    """The one place a saved request's phase is decided. ``clock`` reads the
    database time and is given only when a passed deadline may be recovered."""
    expected = (
        binding.caller,
        binding.provider,
        binding.provider_subject,
        binding.account_id,
        binding.operation,
        binding.method,
        binding.request_target,
        binding.identity,
    )
    if tuple(text_value(value) for value in row[:8]) != expected:
        return RequestPhase.CONFLICT
    state = text_value(row[8])
    if state == "complete":
        return RequestPhase.COMPLETE
    if clock is not None and state == "in_progress" and row[9] is not None and row[9] <= clock():
        return RequestPhase.DEADLINE_PASSED
    return RequestPhase.IN_PROGRESS


def reserve_request(
    connection: Any,
    binding: RequestBinding,
    *,
    recover_expired: Callable[[Any, RequestBinding], OperationResult | None] | None = None,
) -> OperationResult | None:
    """Save a new request ID and return None, or return the answer for one
    already saved (see ``RequestPhase``)."""
    inserted = connection.execute(
        """
        INSERT INTO private_api_requests (
            request_id, caller, provider, provider_subject, account_id,
            operation, method, request_target, identity_json, state,
            in_progress_until
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_progress',
            clock_timestamp() + make_interval(secs => %s)
        )
        ON CONFLICT (request_id) DO NOTHING
        RETURNING request_id
        """,
        (
            binding.request_id,
            binding.caller,
            binding.provider,
            binding.provider_subject,
            binding.account_id,
            binding.operation,
            binding.method,
            binding.request_target,
            Jsonb(binding.identity),
            REQUEST_RESERVATION_SECONDS,
        ),
    ).fetchone()
    if inserted is not None:
        return None
    row = connection.execute(
        f"""
        SELECT {_REQUEST_COLUMNS}, response_status, response_json
        FROM private_api_requests
        WHERE request_id = %s
        FOR UPDATE
        """,
        (binding.request_id,),
    ).fetchone()
    assert row is not None

    def database_time() -> datetime:
        return connection.execute("SELECT clock_timestamp()").fetchone()[0]

    phase = request_phase(
        row, binding, database_time if recover_expired is not None else None
    )
    if phase is RequestPhase.CONFLICT:
        return OperationResult(409, {"error": "request_id_conflict"}, replayed=True)
    if phase is RequestPhase.COMPLETE:
        return OperationResult(int(row[10]), dict(row[11]), replayed=True)
    if recover_expired is not None and phase is RequestPhase.DEADLINE_PASSED:
        result = recover_expired(connection, binding)
        if result is not None:
            return result
    return OperationResult(202, {"status": "in_progress"}, replayed=True)


def assert_request_binding(connection: Any, binding: RequestBinding) -> None:
    row = connection.execute(
        f"""
        SELECT {_REQUEST_COLUMNS}
        FROM private_api_requests
        WHERE request_id = %s
        FOR UPDATE
        """,
        (binding.request_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("private API request reservation is missing")
    if request_phase(row, binding) is not RequestPhase.IN_PROGRESS:
        raise RuntimeError("private API request binding is not active")


def complete_request(
    connection: Any,
    request_id: str,
    result: OperationResult,
) -> None:
    updated = connection.execute(
        """
        UPDATE private_api_requests
        SET state = 'complete', response_status = %s, response_json = %s,
            in_progress_until = NULL, completed_at = clock_timestamp()
        WHERE request_id = %s AND state = 'in_progress'
        """,
        (result.status_code, Jsonb(result.payload), request_id),
    )
    if updated.rowcount != 1:
        raise RuntimeError("private API request reservation was lost")
