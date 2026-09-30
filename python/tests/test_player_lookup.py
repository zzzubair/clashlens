from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from test_api_security import FakeDatabase, _app, _signed_headers
from test_collector import _Client, _collector, _Spool, _Store
from test_private_api import NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens import api_player_lookup
from clashlens.api import create_app
from clashlens.api_db import OperationResult
from clashlens.collector_db import CollectorIntent


def lookup_database(*rows):
    database = MagicMock()
    connection = database.pool.connection.return_value.__enter__.return_value
    connection.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=row)) for row in rows
    ]
    return database


@pytest.mark.parametrize(
    ("active", "eligibility", "confirmed", "state"),
    [
        (True, "eligible", False, "tracking"),
        (True, "eligible", True, "tracking"),
        (False, "ineligible", True, "not_in_legend"),
        (False, "uncertain", True, "uncertain"),
    ],
)
def test_confirmed_player_lookup_needs_no_collection_history(
    active, eligibility, confirmed, state
):
    database = lookup_database((1, active, eligibility, confirmed))
    assert api_player_lookup.get_lookup(database, "#2PP") == {
        "tag": "#2PP",
        "state": state,
    }


@pytest.mark.parametrize("work", ["failed", "cancelled"])
@pytest.mark.parametrize(
    "processing", ["pending", "leased", "waiting_retry", "waiting_dependency"]
)
def test_profile_processing_keeps_lookup_checking_after_collection_failure(
    work, processing
):
    database = lookup_database(
        (1, False, "uncertain", False), (work, "provider_failure", processing)
    )
    assert api_player_lookup.get_lookup(database, "#2PP")["state"] == "checking"


@pytest.mark.parametrize(
    "processing", ["pending", "leased", "waiting_retry", "waiting_dependency"]
)
def test_explicit_not_found_remains_terminal_while_profile_processing_is_outstanding(
    processing,
):
    database = lookup_database(
        (1, False, "uncertain", False), ("failed", "player_not_found", processing)
    )
    assert api_player_lookup.get_lookup(database, "#2PP")["state"] == "not_found"


def test_initial_collection_uses_interactive_key_for_profile_battles_and_history():
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    result = asyncio.run(
        collector.collect_intent(
            CollectorIntent(
                "initial_collection",
                datetime.now(UTC),
                1,
                "#2PP",
                work_id=9,
                league_history_required=True,
            )
        )
    )
    assert result == "complete"
    assert {handoff.endpoint for handoff in store.handoffs} == {
        "profile",
        "battle_log",
        "league_history",
    }
    assert client.seen_pools == [collector.interactive_keys] * 3


@pytest.mark.parametrize(
    "state",
    [
        "unknown",
        "checking",
        "not_found",
        "not_in_legend",
        "uncertain",
        "failed",
        "tracking",
    ],
)
def test_lookup_read_is_public_but_requires_signed_website_request(monkeypatch, state):
    monkeypatch.setattr(
        api_player_lookup, "get_lookup", lambda _db, tag: {"tag": tag, "state": state}
    )
    target = "/v1/players/%232PP/lookup"
    with TestClient(_app(FakeDatabase())) as client:
        assert client.get(target).status_code == 401
        response = client.get(target, headers=_signed_headers(target))
    assert response.status_code == 200
    assert response.json() == {"tag": "#2PP", "state": state}


def test_lookup_submission_is_anonymous_and_rejects_invalid_tags_and_bodies(
    monkeypatch,
):
    submitted = []

    def submit(_db, binding, *, normalized_tag):
        submitted.append((binding.account_id, normalized_tag))
        return OperationResult(200, {"tag": normalized_tag, "state": "checking"})

    monkeypatch.setattr(api_player_lookup, "submit_lookup", submit)
    app = create_app(
        FakeDatabase(),
        keys={("typescript-website", "current"): TS_CURRENT},
        clock=lambda: NOW_SECONDS,
    )
    target = "/v1/players/%232pp/lookup"
    with TestClient(app) as client:
        response = client.post(target, headers=signed_headers(target, method="POST"))
        assert response.json() == {"tag": "#2PP", "state": "checking"}
        invalid = "/v1/players/INVALID/lookup"
        assert (
            client.post(
                invalid, headers=signed_headers(invalid, method="POST")
            ).status_code
            == 422
        )
        assert (
            client.post(
                target,
                content=b"{}",
                headers=signed_headers(target, method="POST", body=b"{}"),
            ).status_code
            == 422
        )
    assert submitted == [(None, "#2PP")]
