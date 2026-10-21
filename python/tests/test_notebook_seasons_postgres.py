"""Notebook Seasons end to end: one Season's notebook carried into the next.

A player's defenses at the end of the Season of 7 September 2026 are saved
as the collector would save them, and the real worker turns them into
Legend days and counts them. The Clasher's notebook for that Season then
carries its Active and To try bases into the next Season's, keeping their
base IDs; the past notebook stays readable with its records, takes changes
until the end of the Season after it, and lends any base to this Season as
a copy. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_crews_api import call
from test_discord_bot_postgres import _account, _verify
from test_notebook_postgres import LINK, MAIN, NOTEBOOK, _record
from test_private_api import NOW_SECONDS, TS_CURRENT
from test_reset_scenarios_postgres import PREVIOUS_SEASON, SEASON, _story, day, defenses

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

SEPTEMBER = str(int(PREVIOUS_SEASON.timestamp()))
NOVEMBER = SEASON + timedelta(days=28)


def test_active_and_to_try_bases_carry_into_the_next_season(
    database_url: str, archive_server, monkeypatch
) -> None:
    """In the Season of 7 Sep - 5 Oct, MAIN ran Spiral on Day 26 and Ring
    from Day 27 to the end; Box was saved To try and Hex set Retired without
    running. Day 26's 2 defenses lost 40 (a triple) and 30; Day 27's lost 16
    and 10. On Day 2 of the Season of 5 Oct, Ring and Box are in its
    notebook with their base IDs, note and link, Ring on MAIN from Day 1;
    Spiral and Hex stay behind. The September notebook still shows each
    record, takes a fixed day until 2 Nov, and copies Spiral in as a new
    base. From 2 Nov it is read-only, yet still opens and copies."""
    season = PREVIOUS_SEASON
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, MAIN)
        story.players[MAIN].battles = (
            defenses(day(26, season), 40, 30) + defenses(day(27, season), 16, 10))
        story.reset(day(26, season), {MAIN: (5000, season)})
        story.reset(day(27, season), {MAIN: (4930, season)})
        story.reset(day(28, season), {MAIN: (4904, season)})
        story.count_armies(day(28, season) + timedelta(hours=7))
        unfinished = story.unfinished_jobs()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            me = _account(owner, "google", "subject-sloothy", "sloothy")
            _verify(owner, me, MAIN)
            clock = [day(26, season) + timedelta(hours=7)]
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: clock[0],
                             dashboard_enabled=True)
            with TestClient(app) as client:

                def save(**change) -> dict:
                    response = call(client, "sloothy", "POST", NOTEBOOK, change)
                    assert response.status_code == 200, response.json()
                    return response.json()

                def refused(**change) -> tuple[int, str]:
                    response = call(client, "sloothy", "POST", NOTEBOOK, change)
                    return response.status_code, response.json()["error"]

                def notebook(**query: str) -> dict:
                    target = NOTEBOOK + "".join(
                        f"{'&' if index else '?'}{key}={value}"
                        for index, (key, value) in enumerate(query.items()))
                    return call(client, "sloothy", "GET", target).json()

                # Day 26 of the September Season.
                spiral = save(action="add", name="Spiral", status="active", tag=MAIN,
                              day=26)["base"]
                ring = save(action="add", name="Ring", status="active", tag=MAIN,
                            day=27)["base"]
                box = save(action="add", name="Box", status="to_try", link=LINK)["base"]
                save(action="edit", base=box, note="CC: lure first")
                hex_ = save(action="add", name="Hex", status="to_try")["base"]
                save(action="status", base=hex_, status="retired")

                # Day 2 of the October Season: the first look carries the
                # bases in; a second does not bring a deleted one back.
                clock[0] = day(2) + timedelta(hours=7)
                october = notebook()
                ring_page = notebook(base=ring)
                box_page = notebook(base=box)
                september = notebook(season=SEPTEMBER)
                ring_then = notebook(season=SEPTEMBER, base=ring)
                missing = [
                    call(client, "sloothy", "GET", f"{NOTEBOOK}?season={value}").json()
                    for value in ("123", str(int(NOVEMBER.timestamp())), "x")
                ]
                # Fixing Day 25 of September, then undoing it.
                fixed = save(action="pick", season=SEPTEMBER, tag=MAIN, day=25, base=spiral)
                september_fixed = notebook(season=SEPTEMBER)
                save(action="undo", undo=fixed["undo"])
                september_undone = notebook(season=SEPTEMBER)
                ended = [
                    refused(action="add", season=SEPTEMBER, name="Zig", status="to_try"),
                    refused(action="status", season=SEPTEMBER, base=box, status="retired"),
                ]
                # Copying Spiral in, undoing it, and copying it again.
                copied = save(action="copy", season=SEPTEMBER, base=spiral)
                copy_page = notebook(base=copied["base"])
                save(action="undo", undo=copied["undo"])
                uncopied = notebook()
                copy = save(action="copy", season=SEPTEMBER, base=spiral)["base"]
                spiral_then = notebook(season=SEPTEMBER, base=spiral)
                # A rename this Season leaves September's Ring as it was.
                save(action="edit", base=ring, name="Ring v2")
                save(action="delete", base=box)
                again = notebook()
                september_again = notebook(season=SEPTEMBER)

                # 3 November, in the Season after next: read-only.
                clock[0] = day(2, NOVEMBER) + timedelta(hours=7)
                read_only = [
                    refused(action="pick", season=SEPTEMBER, tag=MAIN, day=25, base=spiral),
                    refused(action="edit", season=SEPTEMBER, base=ring, note="late"),
                    refused(action="delete", season=SEPTEMBER, base=hex_),
                ]
                september_late = notebook(season=SEPTEMBER)
                late_copy = save(action="copy", season=SEPTEMBER, base=hex_)["base"]
                november = notebook()
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    # The bases still Active or To try at the end of September carry in with
    # their base IDs; Ring, MAIN's base on Day 28, runs on from Day 1.
    bases = {base["id"]: base for base in october["bases"]}
    assert {code: base["status"] for code, base in bases.items()} == {
        ring: "active", box: "to_try"}
    assert (bases[ring]["added_day"], bases[box]["added_day"]) == (1, 1)
    assert bases[box]["last_ran_day"] is None
    main = october["players"][0]
    assert ([(run["from"], run["to"], run["base"]) for run in main["runs"]],
            main["current"]) == ([(1, 2, ring)], ring)
    assert (box_page["base"]["note"], box_page["base"]["link"]) == ("CC: lure first", LINK)
    assert ring_page["base"]["this_season"] is None
    assert october["season"]["current"] and october["season"]["editable"]
    assert october["season"]["editable_until"] == (SEASON + timedelta(days=56)).isoformat()
    assert [item["id"] for item in october["seasons"]] == [
        str(int(SEASON.timestamp())), SEPTEMBER]
    assert october["seasons"][1] == {"id": SEPTEMBER, "start": PREVIOUS_SEASON.isoformat(),
                                     "end": SEASON.isoformat()}

    # September is read as on its Day 28, with each record from the
    # worker's counts: Day 28 itself is not counted yet.
    assert september["season"] | {"editable_until": None} == {
        "id": SEPTEMBER, "start": PREVIOUS_SEASON.isoformat(), "end": SEASON.isoformat(),
        "today": 28, "tomorrow": 28, "current": False, "editable": True,
        "editable_until": None}
    assert september["season"]["editable_until"] == NOVEMBER.isoformat()
    then = {base["id"]: base for base in september["bases"]}
    assert {code: base["status"] for code, base in then.items()} == {
        spiral: "retired", ring: "active", box: "to_try", hex_: "retired"}
    assert then[spiral]["record"] == _record(1, 2, [0, 0, 1, 1], 70)
    assert then[ring]["record"] == _record(1, 2, [0, 1, 1, 0], 26)
    assert [(run["from"], run["to"], run["base"]) for run in september["players"][0]["runs"]
            ] == [(26, 26, spiral), (27, 28, ring)]
    assert ring_then["base"]["this_season"] == ring
    assert missing == [{"error": "season_not_found"}] * 3

    # A past Season still takes a fixed day, and Undo finds it there.
    assert [(run["from"], run["to"]) for run in september_fixed["players"][0]["runs"]
            ] == [(25, 26), (27, 28)]
    assert september_undone["players"] == september["players"]
    assert ended == [(422, "season_ended")] * 2

    # A copy is a new base with a new ID, To try, in this Season only.
    assert copied["base"] not in {spiral, ring, box, hex_}
    copied_base = {base["id"]: base for base in copy_page["bases"]}[copied["base"]]
    assert (copied_base["name"], copied_base["status"]) == ("Spiral", "to_try")
    assert {base["id"] for base in uncopied["bases"]} == {ring, box}
    assert spiral_then["base"]["this_season"] == copy
    # Carried once: Box, deleted, stays gone; each Season keeps its own name.
    assert {base["id"]: base["name"] for base in again["bases"]} == {
        ring: "Ring v2", copy: "Spiral"}
    assert {base["id"]: base["name"] for base in september_again["bases"]} == {
        spiral: "Spiral", ring: "Ring", box: "Box", hex_: "Hex"}

    # From 2 November September is read-only but still opens and copies.
    assert read_only == [(422, "read_only")] * 3
    assert (september_late["season"]["editable"], september_late["bases"]) == (
        False, september_again["bases"])
    assert {base["id"]: base["status"] for base in november["bases"]}[late_copy] == "to_try"
    # October's To try copy of Spiral carried into November with its ID.
    assert {base["id"]: base["status"] for base in november["bases"]}[copy] == "to_try"
    assert [item["id"] for item in november["seasons"]] == [
        str(int(NOVEMBER.timestamp())), str(int(SEASON.timestamp())), SEPTEMBER]
