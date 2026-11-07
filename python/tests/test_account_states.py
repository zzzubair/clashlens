"""The named states of accounts, private requests, verification and lookups."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens.api_db import RequestBinding, RequestPhase, _request_phase
from clashlens.api_player_lookup import RECHECKABLE, LookupState, _check_progress
from clashlens.api_verification import (
    CredentialState,
    PermitAnswer,
    TransferCandidate,
    VerificationAnswer,
    permit_answer,
    verification_answer,
)
from clashlens.shared_copy import CopyState, SharedCopy
from clashlens.verification import VerificationOutcome

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)
BINDING = RequestBinding(
    "request-1", "website", "google", "g-1", 7, "players.verify", "POST",
    "/v1/players/verify", {"tag": "#2PP"},
)
SAVED = ("website", "google", "g-1", 7, "players.verify", "POST",
         "/v1/players/verify", {"tag": "#2PP"})


@pytest.mark.parametrize(
    ("table", "column", "states"),
    [
        ("shared_api_credentials", "state", CredentialState),
        ("player_link_verification_audits", "outcome", VerificationAnswer),
        ("support_player_link_transfer_candidates", "state", TransferCandidate),
    ],
)
def test_each_named_state_list_matches_the_database(
    database_url: str, table: str, column: str, states: type[StrEnum]
) -> None:
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        allowed = {
            value
            for (definition,) in connection.execute(
                """
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = %s::regclass AND contype = 'c'
                """,
                (table,),
            )
            if re.match(rf"CHECK \(\({column} = ANY \(ARRAY\[", definition)
            for value in re.findall(r"'([a-z_]+)'::text", definition)
        }
    assert allowed == {state.value for state in states}


def test_a_saved_request_id_conflicts_waits_replays_or_recovers() -> None:
    def phase(saved=SAVED, state="in_progress", until=None, clock=None):
        return _request_phase((*saved, state, until), BINDING, clock)

    assert phase(state="complete") is RequestPhase.COMPLETE
    assert phase() is RequestPhase.IN_PROGRESS
    # Another body under the same ID never replays.
    other = (*SAVED[:7], {"tag": "#8QQ"})
    assert phase(other, "complete") is RequestPhase.CONFLICT
    # A passed deadline counts only where the caller can recover it.
    passed = NOW - timedelta(seconds=1)
    assert phase(until=passed) is RequestPhase.IN_PROGRESS
    assert phase(until=passed, clock=lambda: NOW) is RequestPhase.DEADLINE_PASSED
    assert phase(until=NOW + timedelta(seconds=44), clock=lambda: NOW) is (
        RequestPhase.IN_PROGRESS
    )


def test_a_permit_refusal_names_the_key_state_or_the_spent_budget() -> None:
    def never() -> tuple[int, int, int, int]:
        raise AssertionError("budgets are read only for a refused active key")

    assert permit_answer(True, "active", never) is PermitAnswer.GRANTED
    assert permit_answer(False, "quarantined", never) is PermitAnswer.CREDENTIAL_QUARANTINED
    assert permit_answer(False, "cooldown", never) is PermitAnswer.CREDENTIAL_COOLDOWN
    assert permit_answer(False, "active", lambda: (2, 3, 2, 10)) is (
        PermitAnswer.PYTHON_BUDGET_EXHAUSTED
    )
    assert permit_answer(False, "active", lambda: (1, 10, 2, 10)) is (
        PermitAnswer.COMBINED_BUDGET_EXHAUSTED
    )
    assert permit_answer(False, "active", lambda: (0, 0, 2, 10)) is (
        PermitAnswer.CREDENTIAL_INACTIVE
    )


def test_a_verified_player_owned_elsewhere_needs_support_not_a_move() -> None:
    verified = VerificationOutcome.VERIFIED
    assert verification_answer(verified, None, 7) is VerificationAnswer.LINKED
    assert verification_answer(verified, 7, 7) is VerificationAnswer.ALREADY_LINKED
    assert verification_answer(verified, 8, 7) is VerificationAnswer.SUPPORT_REQUIRED
    # A failed check never looks at the owner.
    assert verification_answer(VerificationOutcome.INVALID_TOKEN, 8, 7) is (
        VerificationAnswer.INVALID_TOKEN
    )
    assert verification_answer(VerificationOutcome.UNAVAILABLE, None, 7) is (
        VerificationAnswer.UNAVAILABLE
    )


@pytest.mark.parametrize(
    ("work", "failure", "processing", "state"),
    [
        (None, None, None, LookupState.UNKNOWN),
        ("leased", None, None, LookupState.UNKNOWN),
        ("pending", None, None, LookupState.CHECKING),
        ("complete", None, "leased", LookupState.CHECKING),
        # Collection finished before processing started.
        ("complete", None, None, LookupState.CHECKING),
        # Processed without a profile.
        ("complete", None, "complete", LookupState.FAILED),
        ("failed", "player_not_found", None, LookupState.NOT_FOUND),
        ("cancelled", None, None, LookupState.FAILED),
    ],
)
def test_a_lookup_without_a_profile_follows_its_newest_check(
    work: str | None, failure: str | None, processing: str | None, state: LookupState
) -> None:
    assert _check_progress(work, failure, processing) is state
    assert (state in RECHECKABLE) is (
        state in {LookupState.UNKNOWN, LookupState.FAILED, LookupState.NOT_FOUND}
    )


def test_a_shared_copy_ages_from_fresh_to_stale_to_expired() -> None:
    clock = [0.0]
    copy = SharedCopy(
        lambda _database, now: now.day, key=lambda now: now.date(), seconds=10,
        longest=30, name="test", clock=lambda: clock[0], start=lambda work: None,
    )
    assert copy._state(NOW.date()) == (CopyState.MISSING, None)
    assert copy.current(None, NOW) == 12
    assert copy._state(NOW.date()) == (CopyState.FRESH, 12)
    clock[0] = 10
    assert copy._state(NOW.date()) == (CopyState.STALE, 12)
    clock[0] = 30
    assert copy._state(NOW.date()) == (CopyState.EXPIRED, None)
    # Another Legend day's copy is never served.
    assert copy._state((NOW + timedelta(days=1)).date())[0] is CopyState.MISSING
