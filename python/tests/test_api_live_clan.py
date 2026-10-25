"""The player page's live clan, read through the real client from the fake game."""

from __future__ import annotations

import hashlib
import runpy
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_api_migration import migrated_production_database
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens import api_live_clan, api_verification
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.collector_db import CollectorDatabase
from clashlens.verification import OfficialVerificationClient

FIXTURES = runpy.run_path(str(Path(__file__).parents[2] / "development/fixtures.py"))
TAGS = FIXTURES["tags_for"](8)
FINGERPRINT = hashlib.sha256(b"safe-synthetic-live-clan-key").hexdigest()


class FakeGame(FIXTURES["ClashHandler"]):
    """The development fake game, with a per-tag way to answer a profile read."""

    population = TAGS
    tag_indexes: ClassVar[dict[str, int]] = {tag: index for index, tag in enumerate(TAGS)}
    answers: ClassVar[dict[str, str]] = {}
    profile_reads: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        tag = self.path.removeprefix("/v1/players/").replace("%23", "#")
        type(self).profile_reads.append(tag)
        answer = self.answers.get(tag)
        if answer == "slow":
            time.sleep(3)
        elif answer == "maintenance":
            self.send_json(503, {"reason": "inMaintenance"})
            return
        elif answer == "throttled":
            self.send_json(429, {"reason": "requestThrottled"})
            return
        elif answer == "dropped":
            self.close_connection = True
            self.connection.close()
            return
        elif answer == "no_clan":
            payload = FIXTURES["profile_payload"](tag, self.tag_indexes[tag])
            del payload["clan"]
            self.send_json(200, payload)
            return
        super().do_GET()


@contextmanager
def live_clan_api(
    database_url: str,
) -> Iterator[tuple[TestClient, ApiDatabase, CollectorDatabase, str]]:
    FakeGame.answers = {}
    FakeGame.profile_reads = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGame)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with migrated_production_database(
            database_url, include_compact_collector=True
        ) as connection_info:
            database = ApiDatabase(connection_info)
            collector_database = CollectorDatabase(connection_info)
            api_verification.register_official_credential(database, FINGERPRINT)
            collector_database.register_interactive_key(FINGERPRINT, starts_per_second=25)
            origin = f"http://127.0.0.1:{server.server_port}"
            app = create_app(
                database=database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
                verification_client=game_client(origin),
                official_credential_fingerprint=FINGERPRINT,
            )
            try:
                with TestClient(app) as test_client:
                    yield test_client, database, collector_database, origin
            finally:
                collector_database.close()
                database.close()
    finally:
        server.shutdown()
        server.server_close()


def game_client(origin: str) -> OfficialVerificationClient:
    return OfficialVerificationClient(
        api_key=b"synthetic-live-clan-key",
        proxy_url="",
        api_origin=origin,
        allow_insecure_test_origin=True,
    )


def live_clan(client: TestClient, tag: str) -> dict[str, object]:
    target = f"/v1/players/{tag.replace('#', '%23')}/clan"
    response = client.get(target, headers=signed_headers(target))
    assert response.status_code == 200, response.text
    return response.json()


def test_the_games_clan_shows_and_a_repeat_view_costs_no_request(database_url: str) -> None:
    with live_clan_api(database_url) as (client, database, _collector, _origin):
        first = live_clan(client, TAGS[0])
        second = live_clan(client, TAGS[0])

        assert first == second == {
            "tag": TAGS[0],
            "live": True,
            "clan": {"name": "Synthetic Clan", "tag": "#2CLAN"},
        }
        assert FakeGame.profile_reads == [TAGS[0]]
        assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1
        # Shown only: nothing about the player is saved.
        assert database.scalar("SELECT count(*) FROM players") == 0
        assert database.scalar("SELECT count(*) FROM player_profile_versions") == 0


def test_a_player_in_no_clan_now_shows_no_clan(database_url: str) -> None:
    with live_clan_api(database_url) as (client, _database, _collector, _origin):
        FakeGame.answers[TAGS[1]] = "no_clan"

        assert live_clan(client, TAGS[1]) == {"tag": TAGS[1], "live": True, "clan": None}


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("slow", "timeout"),
        ("maintenance", "http_503"),
        ("throttled", "http_429"),
        ("dropped", "other"),
    ],
)
def test_no_answer_from_the_game_keeps_the_saved_clan(
    database_url: str, answer: str, reason: str
) -> None:
    with live_clan_api(database_url) as (client, database, _collector, _origin):
        FakeGame.answers[TAGS[2]] = answer
        started = time.monotonic()

        fallback = {"tag": TAGS[2], "live": False, "reason": reason}
        assert live_clan(client, TAGS[2]) == fallback
        assert time.monotonic() - started < 2.5
        # The failed answer is remembered too, so the game is asked once a minute.
        assert live_clan(client, TAGS[2]) == fallback
        assert FakeGame.profile_reads == [TAGS[2]]
        state = database.scalar("SELECT state FROM shared_api_credentials")
        assert state == ("cooldown" if answer == "throttled" else "active")


def test_no_free_start_keeps_the_saved_clan_without_asking_the_game(
    database_url: str,
) -> None:
    with live_clan_api(database_url) as (client, _database, collector, _origin):
        # Collection is using half of its 24 starts this second.
        for _ in range(12):
            assert collector.acquire_collector_permit(FINGERPRINT).granted

        assert live_clan(client, TAGS[3]) == {"tag": TAGS[3], "live": False, "reason": "cap"}
        assert FakeGame.profile_reads == []
        # Not remembered: the next view asks again once a start is free.
        time.sleep(1.1)
        assert live_clan(client, TAGS[3])["live"] is True
        assert FakeGame.profile_reads == [TAGS[3]]


def test_live_reads_leave_verification_its_start_and_collection_half_its_share(
    database_url: str,
) -> None:
    with live_clan_api(database_url) as (_client, database, collector, _origin):
        # The counts hold only when every request lands in one 1-second window,
        # so a slow run waits for the window to empty and tries again.
        for _attempt in range(3):
            started = time.monotonic()
            live = [
                api_live_clan.acquire_live_read_permit(database, FINGERPRINT) for _ in range(30)
            ]
            collection = [
                collector.acquire_collector_permit(FINGERPRINT).granted for _ in range(30)
            ]
            verification = api_verification.acquire_official_permit(
                database, FINGERPRINT, request_id=str(uuid4())
            )
            if time.monotonic() - started < 0.8:
                break
            time.sleep(1.1)

        assert sum(refused is None for refused in live) == 12
        assert set(live) == {None, "cap"}
        assert sum(collection) == 12
        assert verification.granted


def test_a_remembered_answer_lasts_a_minute(database_url: str) -> None:
    with live_clan_api(database_url) as (_client, database, _collector, origin):
        clock = [0.0]
        clans = api_live_clan.LiveClans(
            database, game_client(origin), FINGERPRINT, cooldown_seconds=5, clock=lambda: clock[0]
        )

        clans.read(TAGS[4])
        clock[0] = 59.0
        clans.read(TAGS[4])
        clock[0] = 61.0
        clans.read(TAGS[4])

        assert FakeGame.profile_reads == [TAGS[4], TAGS[4]]


def test_views_of_one_player_at_once_share_one_request(database_url: str) -> None:
    with live_clan_api(database_url) as (_client, database, _collector, origin):
        FakeGame.answers[TAGS[5]] = "slow"
        clans = api_live_clan.LiveClans(
            database, game_client(origin), FINGERPRINT, cooldown_seconds=5
        )
        answers: list[api_live_clan.LiveClan] = []
        views = [
            threading.Thread(target=lambda: answers.append(clans.read(TAGS[5])))
            for _ in range(3)
        ]
        for view in views:
            view.start()
        time.sleep(0.3)
        # Another player is read meanwhile, not held behind the slow one.
        started = time.monotonic()
        assert clans.read(TAGS[6]).live
        assert time.monotonic() - started < 1.0
        for view in views:
            view.join()

        assert answers == [api_live_clan.LiveClan(False, reason="timeout")] * 3
        assert sorted(FakeGame.profile_reads) == sorted([TAGS[5], TAGS[6]])
