"""The Tomorrow screen end to end: saved game data in, the real worker
counting Legend days by army and saving the Notebook's meta table, and the
bases a Clasher can reach ranked by holds of 8 out. See docs/testing.md.

Joe asks what Main should run tomorrow, Day 7, a Sunday. Sam runs Ring,
which is in Titans, on Days 2 to 5; Joe's Alt runs Box, in Joe's own
notebook, on Days 4 and 5; Spire was never run. Every defense is a Thrower
attack, and each day's attacks win back what its defenses lose, so every
player starts every day on 5,000 and each defense counts fully.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import (
    SEASON,
    THROWER,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

MAIN, ALT, SAM = "#2PP", "#8PP", "#9PP"
NOTEBOOK = "/v1/account/notebook"
TOMORROW = f"{NOTEBOOK}/tomorrow"
CREWS = "/v1/account/crews"


def test_the_tomorrow_screen_ranks_reachable_bases_by_holds_against_the_meta_near_you(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A defense losing 40 is a triple and 10 one star. Ring holds 6 of 8 a
    day, Box 2 of 8."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, MAIN, ALT, SAM)
        for number in range(2, 6):
            story.players[SAM].battles += (
                defenses(day(number), 40, 40, *[10] * 6, army=THROWER)
                + attacks(day(number) + timedelta(hours=9), 40, 40, 40, 20))
        for number in (4, 5):
            story.players[ALT].battles += (
                defenses(day(number), *[40] * 6, 10, 10, army=THROWER)
                + attacks(day(number) + timedelta(hours=9), *[40] * 6, 20))
        for number in range(2, 7):
            story.reset(day(number), dict.fromkeys(story.players, (5000, SEASON)))
        noon = day(6) + timedelta(hours=7)
        for _ in range(2):
            story.count_armies(noon)
        unfinished = story.unfinished_jobs()
        with psycopg.connect(connection_info) as connection:
            meta = connection.execute(
                "SELECT ranked_day_start, type_id, step, defenses, three_star, trophies_lost"
                " FROM notebook_meta_counts ORDER BY 1").fetchall()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            accounts = {name: _account(owner, "google", f"subject-{name}", name)
                        for name in ("joe", "sam")}
            for name, tag in (("joe", MAIN), ("joe", ALT), ("sam", SAM)):
                _verify(owner, accounts[name], tag)
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: noon,
                             dashboard_enabled=True, notebook_enabled=True)
            with TestClient(app) as client:

                def ok(user: str, method: str, target: str, value: dict | None = None) -> dict:
                    response = call(client, user, method, target, value)
                    assert response.status_code in (200, 201), response.json()
                    return response.json()

                titans = ok("sam", "POST", CREWS, {"name": "Titans", "tags": [SAM]})["crew_id"]
                code = ok("sam", "POST", f"{CREWS}/{titans}/invites", {})["code"]
                ok("joe", "POST", f"/v1/account/crew-invites/{code}/accept", {"tags": [MAIN]})
                ring = ok("sam", "POST", NOTEBOOK, {"action": "add", "name": "Ring", "tag": SAM,
                                                    "day": 6})["base"]
                ok("sam", "POST", NOTEBOOK, {"action": "pick", "tag": SAM, "day": 2,
                                             "base": ring})
                ok("sam", "POST", NOTEBOOK, {"action": "add", "base": ring, "crew": titans})
                box = ok("joe", "POST", NOTEBOOK, {"action": "add", "name": "Box", "tag": ALT,
                                                   "day": 6})["base"]
                ok("joe", "POST", NOTEBOOK, {"action": "pick", "tag": ALT, "day": 4,
                                             "base": box})
                spire = ok("joe", "POST", NOTEBOOK, {"action": "add", "name": "Spire"})["base"]
                before = ok("joe", "GET", f"{TOMORROW}?tag=%232PP")
                # Joe once kept Ring and retired it; it is still in Titans.
                ok("joe", "POST", NOTEBOOK, {"action": "add", "base": ring, "crew": None})
                ok("joe", "POST", NOTEBOOK, {"action": "retire", "base": ring, "crew": None})
                retired = ok("joe", "GET", TOMORROW)
                # So Run it from tomorrow picks it there.
                ok("joe", "POST", NOTEBOOK, {"action": "pick", "base": ring, "tag": MAIN,
                                             "day": 7, "crew": None})
                after = ok("joe", "GET", TOMORROW)
                stranger = call(client, "joe", "GET", f"{TOMORROW}?tag=%239PP")
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    # Each counted day's Thrower defenses on players who started on 5,000.
    assert [(row[0], row[1:]) for row in meta] == [
        (day(2), (3, 500, 8, 2, 140)), (day(3), (3, 500, 8, 2, 140)),
        (day(4), (3, 500, 16, 8, 400)), (day(5), (3, 500, 16, 8, 400))]
    assert (before["player"]["tag"], before["day"], before["weekday"]) == (MAIN, 7, "Sunday")
    assert before["trophies"] == {"now": 5000, "attacks_left": 8, "defenses_left": 8,
                                  "about": 5000}
    # Near 5,000 players are 7 apart, so the floor of 40 counts; 1.25 on a Sunday is 9.
    assert (before["gap"], before["width"]) == (7, 40)
    assert before["meta"] == [{"name": "Thrower", "share": 1.0}]

    def card(base: dict) -> tuple:
        return (base["id"], base["ranked"], base["holds"], base["low"], base["high"],
                base["lost"], base["near"], base["recent"], base["width"], base["widened"],
                base["where"], base["mine"], base["running"])

    # Near you, Throwers tripled 20 of 48: Ring's 8 of 32 becomes a 29% rate
    # with 10 pretend defenses at that 42%, holding 5.7 of 8. Box's 12 of 16
    # is under 24 even twice as wide, so it is an early guess at 3.0.
    assert [card(base) for base in before["bases"]] == [
        (ring, True, 5.7, 4.8, 6.6, 150, 32, 32, 40, False, ["Titans"], False, False),
        (box, False, 3.0, 1.8, 4.3, 229, 16, 16, 80, True, ["Your notebook"], True, False),
    ]
    assert before["bases"][0]["why"] == [
        {"name": "Thrower", "share": 1.0, "defenses": 32, "tripled": 8, "holds": 0.71}]
    assert [base["id"] for base in before["untried"]] == [spire]
    ring_retired = retired["bases"][0]
    assert (ring_retired["id"], ring_retired["mine"], ring_retired["where"]) == (
        ring, True, ["Titans"])
    # Joe ran it from tomorrow: Ring is back in his notebook, and running.
    ring_after = after["bases"][0]
    assert (ring_after["id"], ring_after["running"], ring_after["mine"]) == (ring, True, True)
    assert ring_after["where"] == ["Your notebook", "Titans"]
    assert stranger.status_code == 404
