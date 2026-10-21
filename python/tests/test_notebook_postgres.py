"""Season notebook end to end: saved game data in, a base's record out.

Two players' defenses are saved as the collector would save them, the real
worker turns them into Legend days and counts each finished day's defenses,
and a Clasher then says through the private API which base each player ran
on which day. The notebook reads every number from those counts: held,
star counts and trophies lost, for each run, each base and each player.
Statuses follow the days each base is up, and Undo puts back each change.
See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
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

MAIN, ALT = "#2PP", "#8PP"
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


def _record(days: int, defenses: int, stars: list[int], lost: int) -> dict:
    return {"days": days, "defenses": defenses, "held": defenses - stars[3],
            "stars": stars, "trophies_lost": lost}


def test_a_bases_record_comes_from_the_workers_day_counts(
    database_url: str, archive_server, monkeypatch
) -> None:
    """MAIN ran Ring on Days 2 and 3 and Box on Day 4; ALT ran Spiral from
    Day 3. Today is Day 5, so Days 2 to 4 are counted. A defense losing 40
    is a triple, 16 to 32 two stars, 5 to 15 one star, under 5 none."""
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
            _account(owner, "google", "subject-other", "other")
            _verify(owner, me, MAIN)
            _verify(owner, me, ALT)
            clock = [noon]
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: clock[0],
                             dashboard_enabled=True)
            with TestClient(app) as client:

                def stored(code: str) -> tuple:
                    with owner.pool.connection() as connection:
                        return connection.execute(
                            "SELECT name, note, base_link, status, created_at, status_at,"
                            " updated_at FROM notebook_bases WHERE code = %s", (code,),
                        ).fetchone()

                def save(**change) -> dict:
                    response = call(client, "sloothy", "POST", NOTEBOOK, change)
                    assert response.status_code == 200, response.json()
                    return response.json()

                def refused(user: str = "sloothy", **change) -> tuple[int, str]:
                    response = call(client, user, "POST", NOTEBOOK, change)
                    return response.status_code, response.json()["error"]

                def notebook(user: str = "sloothy", base: str | None = None) -> dict:
                    target = NOTEBOOK if base is None else f"{NOTEBOOK}?base={base}"
                    return call(client, user, "GET", target).json()

                # The first base on MAIN is up today; then the Clasher fixes
                # Day 2, from when it ran, and Day 4, when Box was up.
                ring = save(action="add", name="  Ring ", status="active", tag=MAIN,
                            day=5)["base"]
                box = save(action="add", name="Box", status="to_try")["base"]
                untried = notebook()
                save(action="pick", tag=MAIN, day=2, base=ring)
                fixed = save(action="pick", tag=MAIN, day=4, base=box)
                # ALT runs Ring from tomorrow; Spiral turns out to have been up
                # since Day 3.
                save(action="pick", tag=ALT, day=6, base=ring)
                spiral = save(action="add", name="Spiral", status="to_try")["base"]
                save(action="pick", tag=ALT, day=3, base=spiral)
                seen = notebook()
                detail = notebook(base=ring)["base"]

                # Days only today or tomorrow can start a base, or any day
                # up to tomorrow when fixing one; only verified players.
                bad = [
                    refused(action="add", name="Hex", status="active", tag=MAIN, day=4),
                    refused(action="pick", tag=MAIN, day=7, base=ring),
                    refused(action="pick", tag="#9PP", day=3, base=ring),
                    refused(action="add", name="Ring", link="javascript:alert(1)"),
                    refused(action="add", name="Hex", status="active", day=5),
                    refused("other", action="edit", base=ring, name="Mine"),
                ]
                theirs = call(client, "other", "GET", f"{NOTEBOOK}?base={ring}")

                # Hex, picked on MAIN from tomorrow, is swapped back to Ring
                # before it ever ran.
                hexed = save(action="add", name="Hex", status="active", tag=MAIN, day=6)
                hex_ = hexed["base"]
                swapped = save(action="status", base=ring, status="active", tag=MAIN, day=6)
                before_retire = notebook()
                # Retiring Ring stops it on both players from tomorrow: MAIN
                # has no base then, and ALT carries Spiral on. Ring is still
                # up today, so it shows Active until the Reset. Undo puts
                # every pick and status back.
                retired = save(action="status", base=ring, status="retired")
                after_retire = notebook()
                clock[0] = day(6) + timedelta(hours=7)
                next_day = notebook()
                clock[0] = noon
                undone = save(action="undo", undo=retired["undo"])
                after_undo = notebook()
                # Zig, made Active on ALT from today while ALT has Ring from
                # tomorrow, runs on Day 5 only, so it is Retired from Day 6.
                # Fixing Day 5 back to Spiral leaves it never run: To try.
                zig = save(action="add", name="Zig", status="to_try")["base"]
                save(action="status", base=zig, status="active", tag=ALT, day=5)
                clock[0] = day(6) + timedelta(hours=7)
                zig_ran = notebook()
                clock[0] = noon
                unrun = save(action="pick", tag=ALT, day=5, base=spiral)
                # Hex, set Retired though it never ran, is mis-tapped on MAIN
                # from tomorrow; Undo makes it Retired again.
                save(action="status", base=hex_, status="retired")
                mistap = save(action="pick", tag=MAIN, day=6, base=hex_)
                save(action="undo", undo=mistap["undo"])
                hex_back = notebook()

                # Undo puts back a base's old name and link, but not a note
                # saved since.
                save(action="edit", base=box, link=LINK)
                edited = save(action="edit", base=box, name="Box v2", link="")
                save(action="edit", base=box, note="Spring traps by the TH")
                save(action="undo", undo=edited["undo"])
                renamed_back = notebook(base=box)
                kept = stored(box)
                # Deleting Box leaves Day 4 on MAIN with no base; Undo puts
                # Box back as it was, on its days.
                deleted = save(action="delete", base=box)
                after_delete = notebook()
                save(action="undo", undo=deleted["undo"])
                restored = notebook(base=box)
                put_back = stored(box)
                # A name and a note with emoji joined by U+200D, 🧑‍🚀 and ❤️‍🔥,
                # save and read back unchanged.
                save(action="edit", base=spiral, name=ROCKET, note=LURE)
                joined = notebook(base=spiral)
                # So do 🫟 and 🫩, newer than Python 3.12's character table,
                # through deleting the base and Undo.
                save(action="edit", base=hex_, name=SPLASH, note=TIRED)
                save(action="undo", undo=save(action="delete", base=hex_)["undo"])
                newer = notebook(base=hex_)

                # Monday, Day 8, an hour after the Reset, before the new
                # week's league record is written: the week follows the one
                # before, so both players still run Ring, a save rewrites no
                # status and a base can be set.
                clock[0] = day(8) + timedelta(hours=1)
                monday = notebook()
                codes = (ring, box, spiral, hex_, zig)
                monday_before = [stored(code)[3] for code in codes]
                save(action="edit", base=ring, note="Monday")
                monday_after = [stored(code)[3] for code in codes]
                save(action="pick", tag=MAIN, day=8, base=ring)
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    # A base saved To try has not run yet.
    assert [(base["id"], base["status"], base["last_ran_day"]) for base in untried["bases"]
            if base["id"] == box] == [(box, "to_try", None)]
    assert seen["season"] == {"id": str(int(SEASON.timestamp())),
                              "start": SEASON.isoformat(),
                              "end": (SEASON + timedelta(days=28)).isoformat(),
                              "today": 5, "tomorrow": 6}
    assert seen["as_of_day"] == 4
    bases = {base["id"]: base for base in seen["bases"]}
    assert bases[ring]["name"] == "Ring"
    assert {code: base["status"] for code, base in bases.items()} == {
        # Ring is MAIN's base today and ALT's from tomorrow; Spiral is ALT's
        # today; Box ran on Day 4 and was swapped out.
        ring: "active", box: "retired", spiral: "active",
    }
    # MAIN's Days 2 and 3 on Ring: 7 defenses, 3 triples; Box's Day 4: 3
    # triples; ALT's Days 3 and 4 on Spiral: 4 defenses, none tripled.
    assert bases[ring]["record"] == _record(2, 7, [1, 1, 2, 3], 176)
    assert bases[box]["record"] == _record(1, 3, [0, 0, 0, 3], 120)
    assert bases[spiral]["record"] == _record(2, 4, [1, 1, 2, 0], 60)
    assert (bases[ring]["last_ran_day"], bases[box]["last_ran_day"]) == (5, 4)
    players = {player["tag"]: player for player in seen["players"]}
    assert [(run["from"], run["to"], run["base"]) for run in players[MAIN]["runs"]] == [
        (2, 3, ring), (4, 4, box), (5, 5, ring)]
    assert players[MAIN]["runs"][1]["record"] == _record(1, 3, [0, 0, 0, 3], 120)
    assert [(run["from"], run["to"], run["base"]) for run in players[ALT]["runs"]] == [
        (3, 5, spiral)]
    assert (players[MAIN]["current"], players[ALT]["current"]) == (ring, spiral)
    assert players[ALT]["next"] == {"day": 6, "base": ring}
    # Fixing Day 4 to Box made Box Retired at once: it ran, and MAIN runs
    # Ring again from today.
    assert fixed["changed"] == [{"base": box, "status": "retired"}]
    assert detail["players"] == [{"tag": MAIN, "record": _record(2, 7, [1, 1, 2, 3], 176)}]

    assert bad == [
        (422, "invalid_day"), (422, "invalid_day"), (404, "player_not_verified"),
        (422, "invalid_base_link"), (422, "player_required"), (404, "base_not_found"),
    ]
    assert (theirs.status_code, theirs.json()) == (404, {"error": "base_not_found"})

    assert swapped["changed"] == [{"base": hex_, "status": "to_try"}]
    assert retired["changed"] == []
    statuses = {base["id"]: base["status"] for base in after_retire["bases"]}
    assert statuses == {ring: "active", box: "retired", spiral: "active", hex_: "to_try"}
    players = {player["tag"]: player for player in after_retire["players"]}
    assert (players[MAIN]["current"], players[ALT]["current"]) == (ring, spiral)
    assert (players[MAIN]["next"], players[ALT]["next"]) == ({"day": 6, "base": None}, None)
    statuses = {base["id"]: base["status"] for base in next_day["bases"]}
    assert statuses == {ring: "retired", box: "retired", spiral: "active", hex_: "to_try"}
    players = {player["tag"]: player for player in next_day["players"]}
    assert (players[MAIN]["current"], players[ALT]["current"]) == (None, spiral)
    assert undone["undo"] is None
    assert after_undo["bases"] == before_retire["bases"]
    assert after_undo["players"] == before_retire["players"]
    assert {base["id"]: base["status"] for base in zig_ran["bases"]}[zig] == "retired"
    assert {item["base"]: item["status"] for item in unrun["changed"]} == {
        zig: "to_try", spiral: "active"}
    assert {base["id"]: base["status"] for base in hex_back["bases"]}[hex_] == "retired"

    bases = {base["id"]: base for base in renamed_back["bases"]}
    box_page = renamed_back["base"]
    assert (bases[box]["name"], box_page["note"], box_page["link"]) == (
        "Box", "Spring traps by the TH", LINK)

    players = {player["tag"]: player for player in after_delete["players"]}
    assert [(run["from"], run["to"], run["base"]) for run in players[MAIN]["runs"]] == [
        (2, 3, ring), (5, 5, ring)]
    assert {base["id"] for base in after_delete["bases"]} == {ring, spiral, hex_, zig}
    assert put_back == kept
    for key in ("bases", "players", "base"):
        assert restored[key] == renamed_back[key]
    names = {base["id"]: base["name"] for base in joined["bases"]}
    assert (names[spiral], joined["base"]["note"]) == (ROCKET, LURE)
    names = {base["id"]: base["name"] for base in newer["bases"]}
    assert (names[hex_], newer["base"]["note"]) == (SPLASH, TIRED)

    players = {player["tag"]: player for player in monday["players"]}
    assert (players[MAIN]["current"], players[ALT]["current"]) == (ring, ring)
    assert players[MAIN]["legend_days"][-2:] == [8, 9]
    assert {base["id"]: base["status"] for base in monday["bases"]}[ring] == "active"
    assert monday_after == monday_before


def test_on_day_28_picks_stop_at_day_28(database_url: str) -> None:
    """Day 28's tomorrow is the next Season's Day 1, in the next notebook."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            _account(owner, "google", "subject-sloothy", "sloothy")
            now = day(28) + timedelta(hours=12)
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: now,
                             dashboard_enabled=True)
            with TestClient(app) as client:
                seen = call(client, "sloothy", "GET", NOTEBOOK).json()
        finally:
            owner.close()
            api.close()
    assert (seen["season"]["today"], seen["season"]["tomorrow"]) == (28, 28)
    assert seen["players"] == [] and seen["bases"] == [] and seen["as_of_day"] is None


def test_bases_are_set_and_counted_on_legend_days_only(
    database_url: str, archive_server, monkeypatch
) -> None:
    """MAIN plays Legend I through Sunday, Day 7, then drops to Legend II at
    the Monday Reset of 12 October 2026 and plays multiplayer battles on
    Monday, Day 8. Today is Tuesday, Day 9. The weekly league record puts
    Days 1 to 7 in Legend I and the week from Day 8 below it: a base can be
    set on Day 6 but not on Day 8, today or tomorrow. Ring, picked on
    Saturday, Day 6, runs only to Sunday, Day 7: today it is in no run, MAIN
    has no base and Ring shows Retired, last run on Day 7. It counts only
    Saturday's and Sunday's defenses, not Monday's. MAIN is back in Legend I
    from the Monday Reset of 19 October, Day 15: on Day 16 it has no base
    until one is picked, and fixing Day 7 to Box changes Day 7 only. Day
    28's week has no record yet, so it follows Day 15's: Box, picked then,
    runs until the Season ends and cannot stop. Hex, picked on Day 21 for
    Day 22 while that week has no record either, is no longer MAIN's base
    for tomorrow once MAIN is read in Legend II that Monday."""
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
                    write(action="add", name="Hex", status="active", tag=MAIN, day=9),
                    write(action="add", name="Hex", status="active", tag=MAIN, day=10),
                ]
                ring = write(action="add", name="Ring", status="to_try")[1]["base"]
                refused += [
                    write(action="pick", tag=MAIN, day=8, base=ring),
                    write(action="status", base=ring, status="active", tag=MAIN, day=9),
                ]
                clock[0] = saturday + timedelta(hours=7)
                picked = write(action="pick", tag=MAIN, day=6, base=ring)
                clock[0] = noon
                seen = call(client, "sloothy", "GET", NOTEBOOK).json()
                clock[0] = day(16) + timedelta(hours=7)
                back = call(client, "sloothy", "GET", NOTEBOOK).json()
                box = write(action="add", name="Box", status="to_try")[1]["base"]
                fixed = write(action="pick", tag=MAIN, day=7, base=box)
                fixed_seen = call(client, "sloothy", "GET", NOTEBOOK).json()
                # Clearing a day needs no Legend day.
                cleared = write(action="pick", tag=MAIN, day=9, base=None)
                clock[0] = day(28) + timedelta(hours=7)
                last_pick = write(action="pick", tag=MAIN, day=28, base=box)
                last_day = write(action="status", base=box, status="retired")
                write(action="undo", undo=last_pick[1]["undo"])
                clock[0] = day(21) + timedelta(hours=7)
                hex_ = write(action="add", name="Hex", status="to_try")[1]["base"]
                hex_picked = write(action="pick", tag=MAIN, day=22, base=hex_)
                hex_next = call(client, "sloothy", "GET", NOTEBOOK).json()
                player.legend_ii = True
                for hours in (1, 9):
                    story.reads(day(22) + timedelta(hours=hours, minutes=10), {MAIN: 5000})
                story.run_worker()
                hex_dropped = call(client, "sloothy", "GET", NOTEBOOK).json()
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    assert refused == [(422, {"error": "not_legend_day"})] * 4
    assert (picked[0], fixed[0], cleared[0], last_pick[0]) == (200, 200, 200, 200)
    assert last_day == (422, {"error": "runs_until_season_end"})
    main = seen["players"][0]
    assert main["legend_days"] == [1, 2, 3, 4, 5, 6, 7]
    assert ([(run["from"], run["to"]) for run in main["runs"]], main["current"]) == (
        [(6, 7)], None)
    assert [(base["status"], base["last_ran_day"]) for base in seen["bases"]] == [
        ("retired", 7)]
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

    assert hex_picked[0] == 200
    assert hex_next["players"][0]["next"] == {"day": 22, "base": hex_}
    main = hex_dropped["players"][0]
    assert (main["legend_days"][-1], main["next"]) == (21, None)
    assert {base["id"]: base["status"] for base in hex_dropped["bases"]}[hex_] == "to_try"
