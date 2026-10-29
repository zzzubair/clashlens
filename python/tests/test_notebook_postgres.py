"""Notebook end to end: saved game data in, a base's record out.

Players' defenses are saved as the collector would save them, the real
worker turns them into Legend days and counts each finished day's defenses,
and Clashers then say through the private API which base each game account
ran on which day. One base sits in two notebooks with one set of results,
from everyone who ran it; each notebook names only its own game accounts.
A day with no saved data counts as no data, never zero. Statuses, Seasons
and Undo follow. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database, legend_i_week
from fastapi.testclient import TestClient
from test_api_security import FakeDatabase
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_league_week_days_postgres import Climber
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import (
    SEASON,
    THROWER,
    Story,
    _saturday,
    _story,
    attacks,
    day,
    defenses,
    multiplayer,
)

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

MAIN, ALT, QUIET = "#2PP", "#8PP", "#QPP"
NOTEBOOK = "/v1/account/notebook"
LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH17%3AHV%3AAAAAKgAAAAJ"
ROCKET = "\U0001f9d1\u200d\U0001f680 Rocket"
LURE = "CC: \u2764\ufe0f\u200d\U0001f525 lure first"
SPLASH = "\U0001fadf Splash"
TIRED = "CC \U0001fae9 lure first"


def test_both_notebook_routes_are_missing_while_the_dashboard_is_off() -> None:
    app = create_app(FakeDatabase(), keys={("typescript-website", "current"): TS_CURRENT},
                     clock=lambda: NOW_SECONDS)
    with TestClient(app) as client:
        for method, value in (("GET", None), ("POST", {"action": "nothing"})):
            response = call(client, "sloothy", method, NOTEBOOK, value)
            assert (response.status_code, response.json()) == (
                404, {"error": "notebook_disabled"})


def _record(days: int, defenses: int, stars: list[int], lost: int, no_data: int = 0) -> dict:
    return {"days": days, "no_data": no_data, "defenses": defenses,
            "held": defenses - stars[3], "stars": stars, "trophies_lost": lost}


def test_one_base_has_one_record_from_everyone_who_ran_it(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Sloothy's MAIN ran Ring on Days 2 and 3 and Box on Day 4. QUIET, also
    Sloothy's, ran Ring from Day 2 but has no saved Legend day. Ring is also
    in Other's notebook, and Other's ALT ran it from Day 3. Today is Day 5,
    so Days 2 to 4 are counted. A defense losing 40 is a triple, 16 to 32
    two stars, 5 to 15 one star, under 5 none."""
    today = day(5)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, MAIN, ALT)
        story.players[MAIN].battles = (
            defenses(day(2), 40, 40, 30, 10, army=THROWER)
            + defenses(day(3), 16, 40, 0)
            + defenses(day(4), 40, 40, 40)
        )
        story.players[ALT].battles = defenses(day(3), 30, 20) + defenses(day(4), 10, 0)
        trophies = {MAIN: 5000, ALT: 5000}
        for number in (2, 3, 4, 5):
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
            other = _account(owner, "google", "subject-other", "other")
            with owner.pool.connection() as connection:
                # QUIET: in Legend I for six weeks, into the next Season,
                # with no battle or Legend day saved.
                quiet = connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES (%s, true)"
                    " RETURNING id", (QUIET,),
                ).fetchone()[0]
                for week in range(6):
                    legend_i_week(connection, [quiet], SEASON + timedelta(weeks=week))
            _verify(owner, me, MAIN)
            _verify(owner, me, QUIET)
            _verify(owner, other, ALT)
            clock = [noon]
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: clock[0],
                             dashboard_enabled=True)
            with TestClient(app) as client:

                def stored(code: str) -> tuple:
                    with owner.pool.connection() as connection:
                        return connection.execute(
                            "SELECT code, made_by, name, note, base_link, created_at,"
                            " updated_at FROM notebook_bases WHERE code = %s", (code,),
                        ).fetchone()

                def save(user: str = "sloothy", **change) -> dict:
                    response = call(client, user, "POST", NOTEBOOK, change)
                    assert response.status_code == 200, response.json()
                    return response.json()

                def refused(user: str = "sloothy", **change) -> tuple[int, str]:
                    response = call(client, user, "POST", NOTEBOOK, change)
                    return response.status_code, response.json()["error"]

                def notebook(user: str = "sloothy", query: str = "") -> dict:
                    return call(client, user, "GET", f"{NOTEBOOK}{query}").json()

                # Ring goes up on MAIN today; then Sloothy fixes Day 2, from
                # when it ran, Day 4, when Box was up, and QUIET's Day 2.
                ring = save(action="add", name="  Ring ", tag=MAIN, day=5)["base"]
                box = save(action="add", name="Box")["base"]
                save(action="pick", tag=MAIN, day=2, base=ring)
                save(action="pick", tag=MAIN, day=4, base=box)
                save(action="pick", tag=QUIET, day=2, base=ring,
                     season=str(int(SEASON.timestamp())))
                # Other adds Ring to their notebook, as adding a shared base
                # will, and fixes ALT's Day 3 to it.
                with owner.pool.connection() as connection:
                    connection.execute(
                        "WITH book AS (INSERT INTO notebooks (account_id) VALUES (%s)"
                        " RETURNING id) INSERT INTO notebook_entries"
                        " (notebook_id, base_id, season_id)"
                        " SELECT book.id, base.id, %s FROM book, notebook_bases AS base"
                        " WHERE base.code = %s",
                        (other, str(int(SEASON.timestamp())), ring),
                    )
                save("other", action="pick", tag=ALT, day=3, base=ring)
                seen = notebook()
                theirs = notebook("other")
                detail = notebook(query=f"?base={ring}")["base"]
                their_detail = notebook("other", f"?base={ring}")["base"]

                bad = [
                    # Days only today or tomorrow can start a new base, any
                    # day up to tomorrow can be fixed; only verified players.
                    refused(action="add", name="Hex", tag=MAIN, day=4),
                    refused(action="pick", tag=MAIN, day=7, base=ring),
                    refused(action="pick", tag="#9PP", day=3, base=ring),
                    refused(action="add", name="Ring", link="javascript:alert(1)"),
                    refused(action="add", name="Hex", day=5),
                    # Only Ring's maker edits it; Box is in no notebook of Other's.
                    refused("other", action="edit", base=ring, name="Mine"),
                    refused("other", action="pick", tag=ALT, day=4, base=box),
                    # Nor does an Undo it never had.
                    refused("other", action="undo", undo={
                        "picks": [], "entry": None, "delete": None, "restore": None,
                        "edit": {"base": ring, "name": "Mine", "link": "", "note": ""}}),
                    # Day numbers from a page on another Season.
                    refused(action="pick", tag=MAIN, day=2, base=box, season="123"),
                    refused(action="undo", undo={"season": "123", "picks": [
                        {"tag": MAIN, "day": 2, "base": box}]}),
                    # A base someone runs today or tomorrow stays listed.
                    refused(action="remove", base=ring),
                ]
                box_for_other = call(client, "other", "GET", f"{NOTEBOOK}?base={box}")

                # Retiring Ring stops it on MAIN and QUIET from tomorrow, not
                # on Other's ALT. It is still up today, so it shows Running
                # until the Reset. Undo puts every pick and the entry back.
                before_retire = notebook()
                retired = save(action="retire", base=ring)
                after_retire = notebook()
                clock[0] = day(6) + timedelta(hours=7)
                next_day = notebook()
                clock[0] = noon
                undone = save(action="undo", undo=retired["undo"])
                after_undo = notebook()

                # Undo puts back a base's old name, link and note.
                save(action="edit", base=box, link=LINK, note="Spring traps by the TH")
                edited = save(action="edit", base=box, name="Box v2", link="", note="")
                save(action="undo", undo=edited["undo"])
                renamed_back = notebook(query=f"?base={box}")
                # Box ran, so removing it keeps the base and its record, and
                # MAIN's Day 4 drops off the page; Undo lists it again.
                kept = stored(box)
                removed = save(action="remove", base=box)
                after_remove = notebook()
                still = stored(box)
                save(action="undo", undo=removed["undo"])
                listed_again = notebook(query=f"?base={box}")
                # Hex never ran, so removing it deletes it; Undo puts it back
                # as it was, with its emoji, newer than Python 3.12's table.
                hex_ = save(action="add", name=SPLASH, note=TIRED)["base"]
                made = stored(hex_)
                hex_removed = save(action="remove", base=hex_)
                hex_gone = stored(hex_)
                save(action="undo", undo=hex_removed["undo"])
                hex_back = stored(hex_)
                # Names and notes with emoji joined by U+200D, 🧑‍🚀 and ❤️‍🔥.
                save(action="edit", base=box, name=ROCKET, note=LURE)
                joined = notebook(query=f"?base={box}")
                # Undoing a rename leaves a note saved after it.
                renamed = save(action="edit", base=box, name="Box v3")
                save(action="edit", base=box, note="Newer note")
                save(action="undo", undo=renamed["undo"])
                rename_undone = notebook(query=f"?base={box}")
                # Adding Zig and undoing it deletes it.
                zig = save(action="add", name="Zig", tag=MAIN, day=6)
                save(action="undo", undo=zig["undo"])
                zig_gone = stored(zig["base"])
                after_zig = notebook()

                # Day 8 of the next Season, 31 days on: Ring still runs on
                # QUIET, so it carried itself onto the new page. Box last
                # counted on Day 4 and Hex was added 31 days ago, so both
                # have retired on their own and stayed on the old page.
                old = str(int(SEASON.timestamp()))
                clock[0] = noon + timedelta(days=31)
                carried = notebook()
                past = notebook(query=f"?season={old}")
                # Bringing Box back puts it on this Season's page.
                save(action="keep", base=box)
                brought = notebook()
                missing = call(client, "sloothy", "GET", f"{NOTEBOOK}?season=123")
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    assert seen["season"] == {"id": str(int(SEASON.timestamp())),
                              "start": SEASON.isoformat(),
                              "end": (SEASON + timedelta(days=28)).isoformat(),
                              "today": 5, "tomorrow": 6, "current": True}
    assert seen["as_of_day"] == 4
    bases = {base["id"]: base for base in seen["bases"]}
    assert bases[ring]["name"] == "Ring"
    assert {code: (base["status"], base["mine"], base["here"]) for code, base in bases.items()} == {
        ring: ("running", True, True), box: ("season", True, True)}
    # Ring: MAIN's Days 2 and 3, ALT's Days 3 and 4, and QUIET's three
    # counted days with no data; Box: MAIN's Day 4.
    ring_record = _record(4, 11, [2, 2, 4, 3], 236, no_data=3)
    assert bases[ring]["record"] == ring_record
    assert bases[ring]["ran_by"] == 3
    assert bases[box]["record"] == _record(1, 3, [0, 0, 0, 3], 120)
    assert (bases[ring]["last_ran_day"], bases[box]["last_ran_day"]) == (5, 4)
    players = {player["tag"]: player for player in seen["players"]}
    assert set(players) == {MAIN, QUIET}
    assert [(run["from"], run["to"], run["base"]) for run in players[MAIN]["runs"]] == [
        (2, 3, ring), (4, 4, box), (5, 5, ring)]
    assert players[MAIN]["runs"][1]["record"] == _record(1, 3, [0, 0, 0, 3], 120)
    assert players[QUIET]["runs"] == [
        {"from": 2, "to": 5, "base": ring, "record": _record(0, 0, [0, 0, 0, 0], 0, 3)}]
    assert (players[MAIN]["current"], players[QUIET]["current"]) == (ring, ring)
    # The same totals in Other's notebook; each names only its own accounts.
    assert {base["id"]: base["record"] for base in theirs["bases"]} == {ring: ring_record}
    assert {base["id"]: base["mine"] for base in theirs["bases"]} == {ring: False}
    assert [player["tag"] for player in theirs["players"]] == [ALT]
    assert detail["players"] == [
        {"tag": MAIN, "record": _record(2, 7, [1, 1, 2, 3], 176)},
        {"tag": QUIET, "record": _record(0, 0, [0, 0, 0, 0], 0, 3)},
    ]
    assert their_detail["players"] == [{"tag": ALT, "record": _record(2, 4, [1, 1, 2, 0], 60)}]

    assert bad == [
        (422, "invalid_day"), (422, "invalid_day"), (404, "player_not_verified"),
        (422, "invalid_base_link"), (422, "player_required"), (403, "not_your_base"),
        (404, "base_not_found"), (403, "not_your_base"), (409, "season_changed"),
        (409, "season_changed"), (422, "base_running"),
    ]
    assert (box_for_other.status_code, box_for_other.json()) == (
        404, {"error": "base_not_found"})

    assert retired["changed"] == []
    assert {base["id"]: base["status"] for base in after_retire["bases"]}[ring] == "running"
    players = {player["tag"]: player for player in after_retire["players"]}
    assert (players[MAIN]["next"], players[QUIET]["next"]) == (
        {"day": 6, "base": None}, {"day": 6, "base": None})
    assert {base["id"]: base["status"] for base in next_day["bases"]}[ring] == "retired"
    players = {player["tag"]: player for player in next_day["players"]}
    assert (players[MAIN]["current"], players[QUIET]["current"]) == (None, None)
    assert undone["undo"] is None
    assert after_undo["bases"] == before_retire["bases"]
    assert after_undo["players"] == before_retire["players"]

    bases = {base["id"]: base for base in renamed_back["bases"]}
    assert (bases[box]["name"], renamed_back["base"]["note"], renamed_back["base"]["link"]) == (
        "Box", "Spring traps by the TH", LINK)
    assert removed["undo"]["restore"] is None and still == kept
    players = {player["tag"]: player for player in after_remove["players"]}
    assert [(run["from"], run["to"], run["base"]) for run in players[MAIN]["runs"]] == [
        (2, 3, ring), (5, 5, ring)]
    assert {base["id"] for base in after_remove["bases"]} == {ring}
    for key in ("bases", "players", "base"):
        assert listed_again[key] == renamed_back[key]
    assert hex_gone is None and hex_back == made
    assert (made[1], made[2], made[3]) == (me, SPLASH, TIRED)
    names = {base["id"]: base["name"] for base in joined["bases"]}
    assert (names[box], joined["base"]["note"]) == (ROCKET, LURE)
    names = {base["id"]: base["name"] for base in rename_undone["bases"]}
    assert (names[box], rename_undone["base"]["note"]) == (ROCKET, "Newer note")
    assert zig_gone is None
    assert {base["id"] for base in after_zig["bases"]} == {ring, box, hex_}

    assert carried["season"]["current"] and carried["season"]["today"] == 8
    assert carried["seasons"] == [str(int((SEASON + timedelta(days=28)).timestamp())), old]
    assert {base["id"]: base["status"] for base in carried["bases"]} == {ring: "running"}
    assert {base["id"]: (base["status"], base["here"]) for base in past["bases"]} == {
        ring: ("running", True), box: ("retired", False), hex_: ("retired", False)}
    assert past["season"]["current"] is False and past["season"]["today"] == 28
    assert {base["id"]: base["status"] for base in brought["bases"]} == {
        ring: "running", box: "season"}
    assert (missing.status_code, missing.json()) == (404, {"error": "season_not_found"})


def test_bases_are_set_and_counted_on_legend_days_only(
    database_url: str, archive_server, monkeypatch
) -> None:
    """MAIN plays Legend I through Sunday, Day 7, then drops to Legend II at
    the Monday Reset of 12 October 2026 and plays multiplayer battles on
    Monday, Day 8. Today is Tuesday, Day 9. The weekly league record puts
    Days 1 to 7 in Legend I and the week from Day 8 below it: a base can be
    set on Day 6 but not on Day 8, today or tomorrow. Ring, picked on
    Saturday, Day 6, runs only to Sunday, Day 7: today it is in no run and
    MAIN has no base. It counts only Saturday's and Sunday's defenses, not
    Monday's. MAIN is back in Legend I from the Monday Reset of 19 October,
    Day 15: on Day 16 it has no base until one is picked, and fixing Day 7
    to Box changes Day 7 only. On Day 28 a base can be picked from the next
    Season's Day 1, as Day 28's week has no record yet and follows Day 15's."""
    saturday, sunday, monday, tuesday = day(6), day(7), day(8), day(9)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {MAIN: Climber(MAIN)})
        player = story.players[MAIN]
        # Saturday: 8 defenses of 30 from 5,300; Sunday: 1 attack of 40 and
        # 8 defenses of 30, ending at 4,940, below 5,000.
        player.battles = (_saturday(saturday) + attacks(sunday + timedelta(hours=10), 40)
                          + defenses(sunday, *[30] * 8))
        story.reset(saturday, {MAIN: (5300, SEASON)})
        story.reset(sunday, {MAIN: (5140, SEASON)})
        story.reads(monday - timedelta(minutes=10), {MAIN: 4940})
        story.reset(monday, {MAIN: (4940, SEASON)})
        story.run_worker()
        # Read in Legend II naming the Season an hour after the Reset; then
        # multiplayer battles, which the game shows below Legend I.
        player.legend_ii = True
        player.battles += multiplayer(monday + timedelta(hours=2), 3)
        story.reads(monday + timedelta(hours=1), {MAIN: 4940})
        story.reads(monday + timedelta(hours=9), {MAIN: 4940})
        story.wait(timedelta(hours=1))
        story.reads(tuesday - timedelta(minutes=10), {MAIN: 4940})
        story.reset(tuesday, {MAIN: (4940, SEASON)})
        noon = tuesday + timedelta(hours=7)
        story.count_armies(noon)
        # Read in Legend I at 5,000 an hour after the Reset of Day 15.
        player.legend_ii = False
        story.reads(day(15) + timedelta(hours=1, minutes=10), {MAIN: 5000})
        story.run_worker()
        unfinished = story.unfinished_jobs()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            me = _account(owner, "google", "subject-sloothy", "sloothy")
            _verify(owner, me, MAIN)
            clock = [noon]
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: clock[0],
                             dashboard_enabled=True)
            with TestClient(app) as client:

                def write(**change) -> tuple[int, dict]:
                    response = call(client, "sloothy", "POST", NOTEBOOK, change)
                    return response.status_code, response.json()

                refused = [
                    write(action="add", name="Hex", tag=MAIN, day=9),
                    write(action="add", name="Hex", tag=MAIN, day=10),
                ]
                ring = write(action="add", name="Ring")[1]["base"]
                refused.append(write(action="pick", tag=MAIN, day=8, base=ring))
                clock[0] = saturday + timedelta(hours=7)
                picked = write(action="pick", tag=MAIN, day=6, base=ring)
                clock[0] = noon
                seen = call(client, "sloothy", "GET", NOTEBOOK).json()
                clock[0] = day(16) + timedelta(hours=7)
                back = call(client, "sloothy", "GET", NOTEBOOK).json()
                box = write(action="add", name="Box")[1]["base"]
                fixed = write(action="pick", tag=MAIN, day=7, base=box)
                fixed_seen = call(client, "sloothy", "GET", NOTEBOOK).json()
                # Clearing a day needs no Legend day.
                cleared = write(action="pick", tag=MAIN, day=9, base=None)
                clock[0] = day(28) + timedelta(hours=7)
                write(action="pick", tag=MAIN, day=28, base=ring)
                next_season = write(action="pick", tag=MAIN, day=29, base=box)
                last_day = call(client, "sloothy", "GET", NOTEBOOK).json()
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    assert refused == [(422, {"error": "not_legend_day"})] * 3
    assert (picked[0], fixed[0], cleared[0], next_season[0]) == (200, 200, 200, 200)
    main = seen["players"][0]
    assert main["legend_days"] == [1, 2, 3, 4, 5, 6, 7]
    assert ([(run["from"], run["to"]) for run in main["runs"]], main["current"]) == (
        [(6, 7)], None)
    assert [(base["status"], base["last_ran_day"]) for base in seen["bases"]] == [
        ("season", 7)]
    # Saturday's 8 defenses of 30 (two stars each) and Sunday's 8: 16
    # defenses, none tripled, 480 trophies lost, on 2 Legend days.
    ring_record = _record(2, 16, [0, 0, 16, 0], 480)
    assert main["runs"][0]["record"] == ring_record
    assert seen["bases"][0]["record"] == ring_record

    main = back["players"][0]
    assert main["legend_days"] == [1, 2, 3, 4, 5, 6, 7, 15, 16, 17]
    assert ([(run["from"], run["to"], run["base"]) for run in main["runs"]],
            main["current"]) == ([(6, 7, ring)], None)
    main = fixed_seen["players"][0]
    assert ([(run["from"], run["to"], run["base"]) for run in main["runs"]],
            main["current"]) == ([(6, 6, ring), (7, 7, box)], None)

    main = last_day["players"][0]
    assert (last_day["season"]["today"], last_day["season"]["tomorrow"]) == (28, 29)
    # Day 29's pick is in the next Season, which has no page yet.
    assert last_day["seasons"] == [str(int(SEASON.timestamp()))]
    assert (main["current"], main["next"]) == (ring, {"day": 29, "base": box})
