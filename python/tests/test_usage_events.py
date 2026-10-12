from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import posthog
import pytest
from fastapi.testclient import TestClient
from test_api_db_public_ops import seed_profile
from test_api_migration import migrated_production_database
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT
from test_private_api_isolation import call

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.usage_events import UsageEvents

TOKEN = "phc_" + "A1b2C3d4E5" * 4


class Recorder:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.settings = (args, kwargs)
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def capture(self, event: str, **kwargs: Any) -> None:
        self.sent.append((event, kwargs))

    def shutdown(self) -> None:
        pass


@pytest.mark.parametrize(
    "environ",
    [{}, {"CLASHLENS_POSTHOG_TOKEN": "phx_personal"}],
)
def test_nothing_is_sent_without_a_project_token(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> None:
    made: list[Recorder] = []
    monkeypatch.setattr(posthog, "Posthog", lambda *a, **k: made.append(Recorder(*a, **k)))

    UsageEvents.from_environment(environ).capture("signup_completed", {"provider": "google"})

    assert made == []


def test_counts_go_to_the_eu_host_without_location_or_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[Recorder] = []

    def build(*args: Any, **kwargs: Any) -> Recorder:
        made.append(Recorder(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(posthog, "Posthog", build)
    events = UsageEvents.from_environment({"CLASHLENS_POSTHOG_TOKEN": TOKEN})
    events.capture("signup_completed", {"provider": "google"})
    events.capture("signup_completed", {"provider": "google"})
    events.capture("player_looked_up", {"tag": "#2PP"})

    assert made[0].settings == ((TOKEN,), {"host": "https://eu.i.posthog.com", "disable_geoip": True})
    sent = made[0].sent
    assert [event for event, _ in sent] == ["signup_completed", "signup_completed"]
    first, second = (kwargs["distinct_id"] for _, kwargs in sent)
    assert UUID(first) and first != second
    for _, kwargs in sent:
        assert kwargs["properties"] == {
            "provider": "google",
            "route": "account_setup",
            "$process_person_profile": False,
        }
        assert kwargs["disable_geoip"] is True


def test_each_committed_account_group_and_group_change_is_counted_once(
    database_url: str,
) -> None:
    recorder = Recorder()
    owner = {"provider": "discord", "subject": "usage-subject-1"}
    account = {"username": "usageowner", "display_name": "Usage Owner"}
    group = {"name": "Usage group", "tags": []}
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 5400)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            usage_events=UsageEvents(recorder),
        )
        with TestClient(app) as client:
            account_request, group_request = str(uuid4()), str(uuid4())
            # A retried request must not count the same change twice.
            for _ in range(2):
                call(client, owner, "POST", "/v1/account", account, request_id=account_request)
            assert call(client, owner, "GET", "/v1/account").status_code == 200
            taken = call(client, {**owner, "subject": "usage-subject-2"}, "POST", "/v1/account", account)
            assert taken.status_code == 409
            for _ in range(2):
                made = call(client, owner, "POST", "/v1/account/groups", group, request_id=group_request)
                assert made.status_code == 201
            players = f"/v1/account/groups/{made.json()['group_id']}/players"
            add_request, remove_request = str(uuid4()), str(uuid4())
            for _ in range(2):
                added = call(client, owner, "POST", players, {"tag": "#2PP"}, request_id=add_request)
                assert added.status_code == 200
            assert call(client, owner, "POST", players, {"tag": "#2PP"}).status_code == 409
            for _ in range(2):
                removed = call(client, owner, "DELETE", f"{players}/%232PP", request_id=remove_request)
                assert removed.json()["removed"] is True
            # Removing a player who is not there changes nothing.
            assert call(client, owner, "DELETE", f"{players}/%232PP").json()["removed"] is False

    # Each count names the website page where its action happens.
    assert [(event, kwargs["properties"]) for event, kwargs in recorder.sent] == [
        (
            "signup_completed",
            {"provider": "discord", "route": "account_setup", "$process_person_profile": False},
        ),
        ("group_created", {"route": "groups", "$process_person_profile": False}),
        ("group_used", {"action": "add", "route": "groups", "$process_person_profile": False}),
        ("group_used", {"action": "remove", "route": "groups", "$process_person_profile": False}),
    ]
