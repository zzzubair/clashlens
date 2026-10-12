from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import posthog
import pytest
from fastapi.testclient import TestClient
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
    [
        {},
        {"CLASHLENS_POSTHOG_TOKEN": TOKEN},
        {"CLASHLENS_POSTHOG_HOST": "https://eu.i.posthog.com"},
        {"CLASHLENS_POSTHOG_TOKEN": TOKEN, "CLASHLENS_POSTHOG_HOST": "http://eu.i.posthog.com"},
        {"CLASHLENS_POSTHOG_TOKEN": "phx_personal", "CLASHLENS_POSTHOG_HOST": "https://eu.i.posthog.com"},
    ],
)
def test_nothing_is_sent_without_a_project_token_and_https_host(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]
) -> None:
    made: list[Recorder] = []
    monkeypatch.setattr(posthog, "Posthog", lambda *a, **k: made.append(Recorder(*a, **k)))

    UsageEvents.from_environment(environ).capture("account_created", {"provider": "google"})

    assert made == []


def test_counts_go_to_the_configured_host_without_location_or_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[Recorder] = []

    def build(*args: Any, **kwargs: Any) -> Recorder:
        made.append(Recorder(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(posthog, "Posthog", build)
    events = UsageEvents.from_environment(
        {"CLASHLENS_POSTHOG_TOKEN": TOKEN, "CLASHLENS_POSTHOG_HOST": "https://eu.i.posthog.com/"}
    )
    events.capture("account_created", {"provider": "google"})
    events.capture("account_created", {"provider": "google"})
    events.capture("player_looked_up", {"tag": "#2PP"})

    assert made[0].settings == ((TOKEN,), {"host": "https://eu.i.posthog.com", "disable_geoip": True})
    sent = made[0].sent
    assert [event for event, _ in sent] == ["account_created", "account_created"]
    first, second = (kwargs["distinct_id"] for _, kwargs in sent)
    assert UUID(first) and first != second
    for _, kwargs in sent:
        assert kwargs["properties"] == {"provider": "google", "$process_person_profile": False}
        assert kwargs["disable_geoip"] is True


def test_a_created_account_and_group_are_each_counted_once(database_url: str) -> None:
    recorder = Recorder()
    owner = {"provider": "discord", "subject": "usage-subject-1"}
    account = {"username": "usageowner", "display_name": "Usage Owner"}
    group = {"name": "Usage group", "tags": ["#2PP"]}
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            usage_events=UsageEvents(recorder),
        )
        with TestClient(app) as client:
            account_request, group_request = str(uuid4()), str(uuid4())
            # A retried request must not count the same account twice.
            for _ in range(2):
                call(client, owner, "POST", "/v1/account", account, request_id=account_request)
            assert call(client, owner, "GET", "/v1/account").status_code == 200
            taken = call(client, {**owner, "subject": "usage-subject-2"}, "POST", "/v1/account", account)
            assert taken.status_code == 409
            for _ in range(2):
                made = call(client, owner, "POST", "/v1/account/groups", group, request_id=group_request)
                assert made.status_code == 201

    assert [(event, kwargs["properties"]) for event, kwargs in recorder.sent] == [
        ("account_created", {"provider": "discord", "$process_person_profile": False}),
        ("group_created", {"$process_person_profile": False}),
    ]
