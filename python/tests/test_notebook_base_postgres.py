"""A base's page end to end: saved game data in, the real worker counting
Legend days by army, and the base's numbers in each window, by army, by
starting trophies, and Who ran it out. See docs/testing.md.

Joe runs Ring on Main from Day 2 and on Alt from Day 4; Sam runs it from
Day 3. Joe owns Titans, with both his accounts, and Sam is in it. Today is
Day 6, so Days 2 to 5 are counted and Last 3 days is Days 3 to 5. Totals
come from everyone; Who ran it shows Joe's own notebook only Joe's
accounts, and Titans' notebook everyone in Titans.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import SEASON, THROWER, _story, day, defenses

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

MAIN, ALT, SAM = "#2PP", "#8PP", "#9PP"
HYDRA = "u8x8-6x65"
NOTEBOOK = "/v1/account/notebook"
CREWS = "/v1/account/crews"


def test_a_base_page_shows_its_numbers_by_window_army_and_trophies_and_who_ran_it(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A defense losing 40 is a triple, 16 to 32 two stars, 5 to 15 one star,
    under 5 none. Battles with no army carry the fixture's Other army. Main
    starts on 5,000, Alt on 5,600 and Sam on 5,900."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, MAIN, ALT, SAM)
        story.players[MAIN].battles = (
            defenses(day(2), 40, army=THROWER) + defenses(day(2) + timedelta(hours=2), 16,
                                                          army=HYDRA)
            + defenses(day(3), 40, army=HYDRA) + defenses(day(3) + timedelta(hours=2), 0)
            + defenses(day(4), 10, army=THROWER) + defenses(day(5), 40, 40)
        )
        story.players[ALT].battles = (
            defenses(day(4), 16, army=THROWER) + defenses(day(5), 0, army=HYDRA))
        story.players[SAM].battles = (
            defenses(day(3), 40, army=THROWER) + defenses(day(4), 5)
            + defenses(day(5), 16, army=HYDRA))
        opening = {MAIN: 5000, ALT: 5600, SAM: 5900}
        trophies = dict(opening)
        for number in range(2, 7):
            story.reset(day(number), {tag: (total, SEASON) for tag, total in trophies.items()})
            for tag, player in story.players.items():
                trophies[tag] = opening[tag] + sum(
                    battle.trophies for battle in player.battles if battle.at < day(number + 1))
        noon = day(6) + timedelta(hours=7)
        for _ in range(2):
            story.count_armies(noon)
        unfinished = story.unfinished_jobs()

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

                titans = ok("joe", "POST", CREWS, {"name": "Titans", "tags": [MAIN, ALT]})[
                    "crew_id"]
                code = ok("joe", "POST", f"{CREWS}/{titans}/invites", {})["code"]
                ok("sam", "POST", f"/v1/account/crew-invites/{code}/accept", {"tags": [SAM]})
                ring = ok("joe", "POST", NOTEBOOK, {"action": "add", "name": "Ring", "tag": MAIN,
                                                    "day": 6})["base"]
                ok("joe", "POST", NOTEBOOK, {"action": "pick", "tag": MAIN, "day": 2,
                                             "base": ring})
                ok("joe", "POST", NOTEBOOK, {"action": "pick", "tag": ALT, "day": 4,
                                             "base": ring})
                ok("joe", "POST", NOTEBOOK, {"action": "add", "base": ring, "crew": titans})
                ok("sam", "POST", NOTEBOOK, {"action": "add", "base": ring, "tag": SAM,
                                             "day": 6, "crew": None})
                ok("sam", "POST", NOTEBOOK, {"action": "pick", "tag": SAM, "day": 3,
                                             "base": ring})
                mine = ok("joe", "GET", f"{NOTEBOOK}?base={ring}")
                crew = ok("joe", "GET", f"{NOTEBOOK}?base={ring}&crew={titans}")
                member = ok("sam", "GET", f"{NOTEBOOK}?base={ring}&crew={titans}")
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    windows = mine["base"]["windows"]

    def record(days: int, defenses: int, held: int, stars: list[int], lost: int) -> dict:
        return {"days": days, "no_data": 0, "defenses": defenses, "held": held,
                "stars": stars, "trophies_lost": lost}

    # Everyone's 9 counted days on Ring; This Season is the record the
    # notebook page shows for it.
    everything = record(9, 12, 7, [2, 2, 3, 5], 263)
    assert windows["season"]["record"] == everything
    assert windows["all"]["record"] == everything
    assert [base["record"] for base in mine["bases"]] == [everything]
    # Last 3 days leaves out Main's Day 2: a triple and a two-star, 56 lost.
    assert windows["last3"]["record"] == record(8, 10, 6, [2, 2, 2, 4], 207)
    assert [windows[key]["ran_by"] for key in ("last3", "season", "all")] == [3, 3, 3]

    def armies(window: str) -> list[tuple]:
        return [(row["name"], row["defenses"], row["three_star"], row["trophies_lost"])
                for row in windows[window]["armies"]]

    # Most defenses first, Other last.
    assert armies("all") == [("Hydra", 4, 1, 72), ("Thrower", 4, 2, 106), ("Other", 4, 2, 85)]
    assert armies("last3") == [("Hydra", 3, 1, 56), ("Thrower", 3, 1, 66), ("Other", 4, 2, 85)]
    # Under 5,400: Main; 5,400 to 5,799: Alt; 5,800 or more: Sam.
    assert [(band["defenses"], band["held"], band["trophies_lost"])
            for band in windows["all"]["bands"]] == [(7, 3, 186), (2, 2, 16), (3, 2, 61)]

    def who(page: dict) -> list[tuple]:
        return [(row["tag"], row["day"], row["start"], row["defenses"], row["held"],
                 row["trophies_lost"], row["owner"])
                for row in page["base"]["who"]["rows"] if not row["live"]]

    joes = [
        (MAIN, 5, 4894, 2, 0, 80, None), (ALT, 5, 5584, 1, 1, 0, None),
        (MAIN, 4, 4904, 1, 1, 10, None), (ALT, 4, 5600, 1, 1, 16, None),
        (MAIN, 3, 4944, 2, 1, 40, None), (MAIN, 2, 5000, 2, 1, 56, None),
    ]
    rows = mine["base"]["who"]["rows"]
    # Newest first, today's live rows on top; only Joe's accounts in his own.
    assert [row["date"] for row in rows] == sorted((row["date"] for row in rows), reverse=True)
    assert {(row["tag"], row["day"]) for row in rows if row["live"]} == {(MAIN, 6), (ALT, 6)}
    assert sorted(who(mine), key=lambda row: (-row[1], row[0])) == joes
    assert all(row["windows"] == ["all", "season", "last3"] for row in rows if row["day"] > 2)
    # Titans' notebook adds Sam, with whose Clash Lens account each row is.
    sams = [(SAM, 5, 5855, 1, 1, 16, "Sam"), (SAM, 4, 5860, 1, 1, 5, "Sam"),
            (SAM, 3, 5900, 1, 0, 40, "Sam")]
    assert sorted(who(crew), key=lambda row: (-row[1], row[0])) == sorted(
        [(*row[:6], "Joe") for row in joes] + sams, key=lambda row: (-row[1], row[0]))
    assert crew["base"]["windows"] == windows
    assert crew["base"]["who"]["total"] == 12
    # Copies: Joe's and Sam's own notebooks, for Titans' owner only.
    assert (mine["base"]["copies"], crew["base"]["copies"], member["base"]["copies"]) == (
        None, 2, None)
