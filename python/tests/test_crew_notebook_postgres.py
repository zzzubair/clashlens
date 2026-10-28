"""Crew notebooks end to end: saved game data in, one base's record shown
the same in a Crew's notebook and in a Clasher's own.

Rin owns Titans, with Sam and Kai in it. Rin puts her base Ring in Titans
and in Owls, where Zed, outside Titans, takes it into his own notebook.
Rin, Sam and Zed each run Ring; the real worker counts their Legend days,
and every notebook showing Ring shows one record from all three. Members
copy bases out; Uploaders, admins and the owner add them; only the owner
and admins invite and make Uploaders while the Notebook is on. See
docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_api_security import FakeDatabase
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import SEASON, _story, day, defenses

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

RIN, SAM, ZED, KAI = "#2PP", "#8PP", "#9PP", "#QPP"
NOTEBOOK = "/v1/account/notebook"
CREWS = "/v1/account/crews"


def test_uploaders_need_the_notebook_switch() -> None:
    app = create_app(FakeDatabase(), keys={("typescript-website", "current"): TS_CURRENT},
                     clock=lambda: NOW_SECONDS, dashboard_enabled=True)
    with TestClient(app) as client:
        target = f"{CREWS}/00000000-0000-4000-8000-000000000000/members/sam"
        response = call(client, "rin", "PATCH", target, {"role": "uploader"})
        assert (response.status_code, response.json()) == (422, {"error": "invalid_request"})
        response = call(client, "rin", "GET", NOTEBOOK)
        assert (response.status_code, response.json()) == (404, {"error": "notebook_disabled"})


def test_a_crew_notebook_shows_one_record_from_everyone_who_ran_a_base(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Today is Day 5, so Days 2 to 4 are counted. Rin runs Ring from Day 2,
    Sam and Zed from Day 3. A defense losing 40 is a triple, 16 to 32 two
    stars, 5 to 15 one star, under 5 none."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, RIN, SAM, ZED)
        story.players[RIN].battles = (
            defenses(day(2), 40, 10) + defenses(day(3), 30) + defenses(day(4), 0))
        story.players[SAM].battles = defenses(day(3), 40) + defenses(day(4), 16)
        story.players[ZED].battles = defenses(day(3), 5) + defenses(day(4), 40)
        trophies = dict.fromkeys(story.players, 5000)
        for number in (2, 3, 4, 5):
            story.reset(day(number), {tag: (total, SEASON) for tag, total in trophies.items()})
            for tag, player in story.players.items():
                trophies[tag] = 5000 + sum(
                    battle.trophies for battle in player.battles if battle.at < day(number + 1))
        noon = day(5) + timedelta(hours=7)
        story.count_armies(noon)
        unfinished = story.unfinished_jobs()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            accounts = {name: _account(owner, "google", f"subject-{name}", name)
                        for name in ("rin", "sam", "zed", "kai")}
            with owner.pool.connection() as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES (%s, true)", (KAI,))
            for name, tag in (("rin", RIN), ("sam", SAM), ("zed", ZED), ("kai", KAI)):
                _verify(owner, accounts[name], tag)
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: noon,
                             dashboard_enabled=True, notebook_enabled=True)
            with TestClient(app) as client:

                def ok(user: str, method: str, target: str, value: dict | None = None) -> dict:
                    response = call(client, user, method, target, value)
                    assert response.status_code in (200, 201), response.json()
                    return response.json()

                def refused(user: str, method: str, target: str,
                            value: dict | None = None) -> tuple[int, str]:
                    response = call(client, user, method, target, value)
                    return response.status_code, response.json()["error"]

                def crew(user: str, name: str, tag: str, *members: tuple[str, str]) -> str:
                    crew_id = ok(user, "POST", CREWS, {"name": name, "tags": [tag]})["crew_id"]
                    code = ok(user, "POST", f"{CREWS}/{crew_id}/invites", {})["code"]
                    for member, member_tag in members:
                        ok(member, "POST", f"/v1/account/crew-invites/{code}/accept",
                           {"tags": [member_tag]})
                    return crew_id

                titans = crew("rin", "Titans", RIN, ("sam", SAM), ("kai", KAI))
                owls = crew("rin", "Owls", RIN, ("zed", ZED))
                # Rin's Ring runs on her account from Day 2; she puts it in
                # both Crews. Zed takes it from Owls and runs it from Day 3.
                ring = ok("rin", "POST", NOTEBOOK, {"action": "add", "name": "Ring"})["base"]
                ok("rin", "POST", NOTEBOOK, {"action": "pick", "tag": RIN, "day": 2, "base": ring})
                for crew_id in (titans, owls):
                    ok("rin", "POST", NOTEBOOK, {"action": "add", "base": ring, "crew": crew_id})
                ok("zed", "POST", NOTEBOOK, {"action": "add", "base": ring, "tag": ZED, "day": 5})
                ok("zed", "POST", NOTEBOOK, {"action": "pick", "tag": ZED, "day": 3, "base": ring})

                bad = [
                    # Members copy bases out but can't add, invite or make
                    # Uploaders; nobody picks in a Crew's notebook.
                    refused("sam", "POST", NOTEBOOK, {"action": "add", "name": "Fort",
                                                      "crew": titans}),
                    refused("sam", "POST", f"{CREWS}/{titans}/invites", {}),
                    refused("kai", "PATCH", f"{CREWS}/{titans}/members/sam",
                            {"role": "uploader"}),
                    refused("rin", "POST", NOTEBOOK, {"action": "pick", "tag": RIN, "day": 3,
                                                      "base": ring, "crew": titans}),
                    # Outside Titans, its notebook can't be seen at all.
                    refused("zed", "GET", f"{NOTEBOOK}?crew={titans}"),
                    refused("zed", "POST", NOTEBOOK, {"action": "add", "base": ring,
                                                      "crew": titans}),
                    # Ring is in Titans already.
                    refused("rin", "POST", NOTEBOOK, {"action": "add", "base": ring,
                                                      "crew": titans}),
                ]
                ok("rin", "PATCH", f"{CREWS}/{titans}/members/sam", {"role": "uploader"})
                # An Uploader adds and retires their own bases, not others',
                # and still can't make admins.
                fort = ok("sam", "POST", NOTEBOOK, {"action": "add", "name": "Fort",
                                                    "crew": titans})["base"]
                bad += [
                    refused("sam", "POST", NOTEBOOK, {"action": "retire", "base": ring,
                                                      "crew": titans}),
                    refused("sam", "PATCH", f"{CREWS}/{titans}/members/kai", {"role": "admin"}),
                    refused("kai", "POST", NOTEBOOK, {"action": "retire", "base": fort,
                                                      "crew": titans}),
                ]
                retired = ok("sam", "POST", NOTEBOOK, {"action": "retire", "base": fort,
                                                       "crew": titans})
                fort_retired = ok("kai", "GET", f"{NOTEBOOK}?crew={titans}")
                ok("sam", "POST", NOTEBOOK, {"action": "undo", "undo": retired["undo"],
                                             "crew": titans})
                # Sam takes Ring from Titans and runs it from Day 3; Kai takes
                # Fort, then leaves Titans and keeps it.
                ok("sam", "POST", NOTEBOOK, {"action": "add", "base": ring})
                ok("sam", "POST", NOTEBOOK, {"action": "pick", "tag": SAM, "day": 3, "base": ring})
                ok("kai", "POST", NOTEBOOK, {"action": "add", "base": fort})
                ok("kai", "DELETE", f"{CREWS}/{titans}/members/me")

                seen = ok("sam", "GET", f"{NOTEBOOK}?crew={titans}&base={ring}")
                zeds = ok("zed", "GET", NOTEBOOK)
                rins = ok("rin", "GET", f"{NOTEBOOK}?base={ring}")
                kais = ok("kai", "GET", NOTEBOOK)
                kai_out = refused("kai", "GET", f"{NOTEBOOK}?crew={titans}")
                members = ok("rin", "GET", f"{CREWS}/{titans}")["members"]
                # Zed runs Ring, but taking it out of Owls changes nobody's
                # runs: it stays in his own notebook.
                ok("rin", "POST", NOTEBOOK, {"action": "remove", "base": ring, "crew": owls})
                owls_after = ok("zed", "GET", f"{NOTEBOOK}?crew={owls}")
                zeds_after = ok("zed", "GET", NOTEBOOK)
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    assert bad == [
        (403, "crew_forbidden"), (403, "crew_forbidden"), (403, "crew_forbidden"),
        (422, "invalid_request"), (404, "crew_not_found"), (404, "crew_not_found"),
        (409, "already_added"), (403, "crew_forbidden"), (403, "crew_forbidden"),
        (403, "crew_forbidden"),
    ]
    # Rin's 4 defenses (40, 10, 30, 0) on Days 2 to 4, Sam's 2 (40, 16) and
    # Zed's 2 (5, 40) on Days 3 and 4: 7 days, 3 triples, 181 lost.
    ring_record = {"days": 7, "no_data": 0, "defenses": 8, "held": 5,
                   "stars": [1, 2, 2, 3], "trophies_lost": 181}
    assert seen["crew"] == {"id": titans, "name": "Titans", "role": "uploader"}
    assert seen["players"] == []
    bases = {base["id"]: base for base in seen["bases"]}
    assert {code: (base["status"], base["mine"], base["own"], base["ran_by"])
            for code, base in bases.items()} == {
        ring: ("running", False, False, 3), fort: ("season", True, True, 0)}
    assert bases[ring]["record"] == ring_record
    # The same record in Zed's notebook, from outside Titans.
    assert {base["id"]: base["record"] for base in zeds["bases"]} == {ring: ring_record}
    assert {base["id"]: base["status"] for base in fort_retired["bases"]}[fort] == "retired"
    # Ring sits in Rin's own notebook and both Crews; on Sam's Titans page it
    # also sits in his own.
    assert rins["base"]["shelves"] == sorted(["mine", titans, owls])
    assert seen["base"]["shelves"] == sorted(["mine", titans])
    assert [(item["name"], item["role"]) for item in rins["crews"]] == [
        ("Owls", "owner"), ("Titans", "owner")]
    # Kai left Titans but kept Fort.
    assert [base["id"] for base in kais["bases"]] == [fort]
    assert kai_out == (404, "crew_not_found")
    assert [(member["username"], member["role"]) for member in members] == [
        ("rin", "owner"), ("sam", "uploader")]
    assert owls_after["bases"] == []
    assert {base["id"]: base["status"] for base in zeds_after["bases"]} == {ring: "running"}
