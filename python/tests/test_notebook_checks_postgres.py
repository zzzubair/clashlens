"""The forgotten-base check and Today so far end to end: saved game data in,
the real worker counting Legend days, the Notebook out. See docs/testing.md.

Jo runs Ring on Main and Fort on Alt from Day 2, then stops opening Clash
Lens until Day 13. A run counts for 7 Legend days after it was last
confirmed, so Days 10 to 12 wait until Jo answers "Still on it?": Yes for
Ring brings them back, No from Day 11 for Fort keeps only the days before.
Sam opens Clash Lens on Days 6 and 12, so his Spire never waits.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import SEASON, _story, day, defenses

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for
from clashlens.league_week import week_start

JO, ALT, SAM = "#2PP", "#8PP", "#9PP"
NOTEBOOK = "/v1/account/notebook"


def test_a_forgotten_base_waits_until_its_clasher_says_they_are_still_on_it(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A defense losing 40 is a triple, 16 two stars, 10 one star, 0 none."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, JO, ALT, SAM)
        for number in range(2, 13):
            story.players[JO].battles += defenses(day(number), 40, 10)
            story.players[ALT].battles += defenses(day(number), 16)
            story.players[SAM].battles += defenses(day(number), 0)
        trophies = dict.fromkeys(story.players, 5000)
        for number in range(2, 14):
            story.reset(day(number), {tag: (total, SEASON) for tag, total in trophies.items()})
            for tag, player in story.players.items():
                trophies[tag] = 5000 + sum(
                    battle.trophies for battle in player.battles if battle.at < day(number + 1))
        noon = day(13) + timedelta(hours=7)
        # Each run counts one day, attacks first: 12 days of each take 3 calls.
        for _ in range(3):
            story.count_armies(noon)
        unfinished = story.unfinished_jobs()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        clock = [day(2) + timedelta(hours=7)]
        try:
            accounts = {name: _account(owner, "google", f"subject-{name}", name)
                        for name in ("joe", "sam")}
            for name, tag in (("joe", JO), ("joe", ALT), ("sam", SAM)):
                _verify(owner, accounts[name], tag)
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: clock[0],
                             dashboard_enabled=True, notebook_enabled=True)
            with TestClient(app) as client:

                def ok(user: str, method: str, target: str, value: dict | None = None) -> dict:
                    response = call(client, user, method, target, value)
                    assert response.status_code in (200, 201), response.json()
                    return response.json()

                ring = ok("joe", "POST", NOTEBOOK, {"action": "add", "name": "Ring",
                                                   "tag": JO, "day": 2})["base"]
                fort = ok("joe", "POST", NOTEBOOK, {"action": "add", "name": "Fort",
                                                   "tag": ALT, "day": 2})["base"]
                spire = ok("sam", "POST", NOTEBOOK, {"action": "add", "name": "Spire",
                                                     "tag": SAM, "day": 2})["base"]
                # Sam opens Clash Lens on Days 6 and 12; Jo not at all.
                for number in (6, 12):
                    clock[0] = day(number) + timedelta(hours=7)
                    ok("sam", "GET", "/v1/account")

                clock[0] = noon
                waiting = ok("joe", "GET", f"{NOTEBOOK}?base={ring}")
                sams = ok("sam", "GET", NOTEBOOK)
                ok("joe", "POST", NOTEBOOK, {"action": "confirm", "tag": JO})
                still_on = ok("joe", "GET", f"{NOTEBOOK}?base={ring}")
                ok("joe", "POST", NOTEBOOK, {"action": "pick", "tag": ALT, "day": 11,
                                            "base": None})
                changed = ok("joe", "GET", NOTEBOOK)
                stranger = call(client, "joe", "POST", NOTEBOOK,
                                {"action": "confirm", "tag": SAM})
        finally:
            owner.close()
            api.close()

    assert unfinished == []

    def record(days: int, defenses: int, held: int, stars: list[int], lost: int) -> dict:
        return {"days": days, "no_data": 0, "defenses": defenses, "held": held,
                "stars": stars, "trophies_lost": lost}

    def summary(page: dict) -> dict:
        return {base["id"]: (base["status"], base["record"]) for base in page["bases"]}

    # Day 13, Jo back after 11 days: Days 2 to 9 count, 10 to 12 wait, and
    # neither base is Running while today waits too.
    assert summary(waiting) == {
        ring: ("season", record(8, 16, 8, [0, 8, 0, 8], 400)),
        fort: ("season", record(8, 8, 8, [0, 0, 8, 0], 128)),
    }
    assert {player["tag"]: player["waiting"] for player in waiting["players"]} == {
        JO: {"base": ring, "seen": 2}, ALT: {"base": fort, "seen": 2}}
    assert waiting["base"]["today"] is None
    assert [(item["tag"], item["today"], item["tomorrow"])
            for item in waiting["base"]["copy"]] == [(JO, True, True), (ALT, True, True)]
    # Sam's visits kept Spire counting all 11 days.
    assert summary(sams) == {spire: ("running", record(11, 11, 11, [11, 0, 0, 0], 0))}
    assert [player["waiting"] for player in sams["players"]] == [None]
    # Yes: Ring's waiting days count, and today's two defenses show so far.
    assert summary(still_on)[ring] == ("running", record(11, 22, 11, [0, 11, 0, 11], 550))
    # No from Day 11: Fort keeps Days 2 to 10.
    assert summary(changed)[fort] == ("season", record(9, 9, 9, [0, 0, 9, 0], 144))
    assert [player["waiting"] for player in changed["players"]] == [None, None]
    assert (stranger.status_code, stranger.json()) == (404, {"error": "player_not_verified"})


def test_today_so_far_counts_todays_defenses_on_everyone_running_the_base(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Live logs publish only during their own Legend day, so this story is
    today's: Main and Alt run Ring from today and Sam runs another base.
    Main has held 1 of 2 so far and Alt 1 of 1: 2 of 3."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            today = ranked_day_for(connection.execute("SELECT clock_timestamp()").fetchone()[0])
        if today.day_number in (1, 28):
            pytest.skip(f"Season Day {today.day_number}")
        story = _story(connection_info, archive_server, monkeypatch, JO, ALT, SAM)
        story.players[JO].battles = defenses(today.start, 40, 10)
        story.players[ALT].battles = defenses(today.start, 16)
        story.players[SAM].battles = defenses(today.start, 40)
        season = today.season_start
        # This week's Monday Reset decides it Legend I.
        for start in sorted({week_start(today.start), today.start}):
            story.reset(start, dict.fromkeys(story.players, (5000, season)))
        afternoon = today.start + timedelta(hours=10)
        story.reads(afternoon, {JO: 4950, ALT: 4984, SAM: 4960}, season)
        story.run_worker()
        unfinished = story.unfinished_jobs()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            accounts = {name: _account(owner, "google", f"subject-{name}", name)
                        for name in ("joe", "sam")}
            for name, tag in (("joe", JO), ("joe", ALT), ("sam", SAM)):
                _verify(owner, accounts[name], tag)
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS,
                             now=lambda: afternoon + timedelta(minutes=5),
                             dashboard_enabled=True, notebook_enabled=True)
            with TestClient(app) as client:

                def ok(user: str, method: str, target: str, value: dict | None = None) -> dict:
                    response = call(client, user, method, target, value)
                    assert response.status_code in (200, 201), response.json()
                    return response.json()

                ring = ok("joe", "POST", NOTEBOOK, {"action": "add", "name": "Ring", "tag": JO,
                                                    "day": today.day_number})["base"]
                ok("joe", "POST", NOTEBOOK, {"action": "pick", "tag": ALT, "base": ring,
                                             "day": today.day_number})
                ok("sam", "POST", NOTEBOOK, {"action": "add", "name": "Other", "tag": SAM,
                                             "day": today.day_number})
                seen = ok("joe", "GET", f"{NOTEBOOK}?base={ring}")
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    assert seen["base"]["today"] == {"defenses": 3, "held": 2}
