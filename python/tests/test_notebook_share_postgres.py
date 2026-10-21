"""Shared bases end to end: saved game data in, a shared page's record out.

Two players' defenses are saved as the collector would save them, and the
real worker counts each finished Legend day. A Clasher picks the base each
player ran, shares it, and anyone with the unlisted link reads the same
record the Clasher's notebook shows, with the base link and player names
as the Clasher chooses. Stop sharing kills the link for good. See
docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_api_security import FakeDatabase
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_notebook_postgres import ALT, LINK, MAIN, NOTEBOOK
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import SEASON, _story, day, defenses

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

SHARE = "/v1/account/notebook/share"
SHARED = "/v1/notebook-shares/"


def test_both_share_routes_are_missing_while_the_dashboard_is_off() -> None:
    app = create_app(FakeDatabase(), keys={("typescript-website", "current"): TS_CURRENT},
                     clock=lambda: NOW_SECONDS)
    with TestClient(app) as client:
        for method, target, value in (
            ("POST", SHARE, {"action": "share", "base": "B-7K3QX9"}),
            ("GET", SHARED + "A" * 22, None),
        ):
            response = call(client, "sloothy", method, target, value)
            assert (response.status_code, response.json()) == (
                404, {"error": "notebook_disabled"})


def test_a_shared_link_shows_the_bases_record_until_sharing_stops(
    database_url: str, archive_server, monkeypatch
) -> None:
    """MAIN ran Ring from Day 2 and ALT from Day 3; today is Day 4, so Days
    2 and 3 are counted. A defense losing 40 is a triple, 16 to 32 two
    stars, 5 to 15 one star, under 5 none."""
    today = day(4)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, MAIN, ALT)
        story.players[MAIN].battles = (
            defenses(day(2), 40, 16, 0) + defenses(day(3), 30, 10)
        )
        story.players[ALT].battles = defenses(day(3), 5)
        trophies = {MAIN: 5000, ALT: 5000}
        for number in (2, 3, 4):
            story.reset(day(number), {tag: (total, SEASON) for tag, total in trophies.items()})
            for tag, player in story.players.items():
                trophies[tag] = 5000 + sum(
                    battle.trophies for battle in player.battles if battle.at < day(number + 1))
        noon = today + timedelta(hours=7)
        story.count_armies(noon)
        unfinished = story.unfinished_jobs()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            me = _account(owner, "google", "subject-sloothy", "sloothy")
            _account(owner, "google", "subject-other", "other")
            _verify(owner, me, MAIN)
            _verify(owner, me, ALT)
            clock = [noon]
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: clock[0],
                             dashboard_enabled=True)
            with TestClient(app) as client:

                def save(**change) -> dict:
                    response = call(client, "sloothy", "POST", NOTEBOOK, change)
                    assert response.status_code == 200, response.json()
                    return response.json()

                def share(user: str = "sloothy", **change) -> tuple[int, dict]:
                    response = call(client, user, "POST", SHARE, change)
                    return response.status_code, response.json()

                def shared(link: str) -> tuple[int, dict]:
                    # Anyone with the link: no one signed in.
                    response = call(client, None, "GET", SHARED + link)
                    return response.status_code, response.json()

                ring = save(action="add", name="Ring", link=LINK, status="active",
                            tag=MAIN, day=4)["base"]
                save(action="pick", tag=MAIN, day=2, base=ring)
                save(action="pick", tag=ALT, day=3, base=ring)
                box = save(action="add", name="Box", status="to_try")["base"]
                notebook = call(client, "sloothy", "GET", NOTEBOOK).json()

                _, first = share(action="share", base=ring)
                link = first["share"]["link"]
                _, again = share(action="share", base=ring)
                page = call(client, "sloothy", "GET", f"{NOTEBOOK}?base={ring}").json()
                opened = shared(link)
                # Someone else's account can't share, change or stop it.
                theirs = [share("other", action=action, base=ring)
                          for action in ("share", "settings", "stop")]
                _, changed = share(action="settings", base=ring, include_link=False,
                                   show_names=True)
                named = shared(link)
                unshared = share(action="settings", base=box, show_names=True)

                # The next Season, Ring still shows its record as of the
                # Season's end.
                clock[0] = day(31)
                next_season = shared(link)
                clock[0] = noon

                _, stopped = share(action="stop", base=ring)
                after_stop = shared(link)
                _, restarted = share(action="share", base=ring)
                new_link = restarted["share"]["link"]
                old_after_restart = shared(link)
                save(action="delete", base=ring)
                after_delete = shared(new_link)
                malformed = shared("not-a-link")
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    ring_record = next(base for base in notebook["bases"] if base["id"] == ring)["record"]
    # MAIN's Days 2 and 3: 5 defenses, 1 triple; ALT's Day 3: one 1-star.
    assert ring_record == {"days": 3, "defenses": 6, "held": 5, "stars": [1, 2, 2, 1],
                           "trophies_lost": 101}

    assert len(link) == 22
    assert again["share"] == first["share"] == {
        "link": link, "include_link": True, "show_names": False}
    assert page["base"]["share"] == first["share"]
    assert {base["id"]: base["shared"] for base in page["bases"]} == {
        ring: True, box: False}

    assert opened == (200, {
        "kind": "shared-base",
        "name": "Ring",
        "link": LINK,
        "record": ring_record,
        "season": {"start": SEASON.isoformat(),
                   "end": (SEASON + timedelta(days=28)).isoformat()},
        "as_of_day": 3,
        "players": None,
    })
    # Never the base ID or the note.
    assert ring not in str(opened[1])

    assert [status for status, _ in theirs] == [404, 404, 404]
    assert changed["share"] == {"link": link, "include_link": False, "show_names": True}
    names = {player["tag"]: player["name"] or player["tag"]
             for player in notebook["players"]}
    assert named[0] == 200 and named[1]["link"] is None
    assert named[1]["players"] == [
        {"name": names[MAIN], "record": {"days": 2, "defenses": 5, "held": 4,
                                         "stars": [1, 1, 2, 1], "trophies_lost": 96}},
        {"name": names[ALT], "record": {"days": 1, "defenses": 1, "held": 1,
                                        "stars": [0, 1, 0, 0], "trophies_lost": 5}},
    ]
    assert unshared == (404, {"error": "share_not_found"})
    assert next_season[0] == 200
    assert next_season[1]["record"] == ring_record

    assert stopped["share"] is None
    assert after_stop == (404, {"error": "share_not_found"})
    assert new_link != link
    assert old_after_restart == (404, {"error": "share_not_found"})
    assert after_delete == (404, {"error": "share_not_found"})
    assert malformed == (404, {"error": "share_not_found"})
