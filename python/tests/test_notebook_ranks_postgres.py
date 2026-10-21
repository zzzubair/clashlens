"""The Notebook's rankings end to end: saved game data in, a base's best and
worst armies and the Season's Top 3 bases out.

Two players' defenses are saved as the collector would save them, the real
worker turns them into Legend days and counts each finished day's defenses
by army, and a Clasher says through the private API which base each player
ran on which day. Each base's armies and the Top 3 come from those counts,
on the counted Legend days the base ran. See docs/testing.md.
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

MAIN, ALT = "#2PP", "#8PP"
HYDRA = "u8x8-6x65"
NOTEBOOK = "/v1/account/notebook"


def test_best_and_worst_armies_and_the_top_3_come_from_the_day_counts(
    database_url: str, archive_server, monkeypatch
) -> None:
    """MAIN ran Ring on Days 2 and 3 and Box on Day 4; ALT ran Spiral on Days
    2 and 3 and Ring on Day 4. Today is Day 5, so Days 2 to 4 are counted.
    Battles with no army carry the fixture's Other army. A defense losing 40
    is a triple, 16 to 32 two stars, 5 to 15 one star, under 5 none."""
    today = day(5)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, MAIN, ALT)
        story.players[MAIN].battles = (
            defenses(day(2), 40, 30, 20, army=THROWER)
            + defenses(day(2) + timedelta(hours=3), 10, 5, 0, army=HYDRA)
            + defenses(day(2) + timedelta(hours=6), 16, 40)
            + defenses(day(3), 40, army=THROWER)
            + defenses(day(3) + timedelta(hours=1), 0, 10, 5, army=HYDRA)
            + defenses(day(3) + timedelta(hours=4), 30, 20, 10, 0)
            + defenses(day(4), 10, 10, 10, army=THROWER)
            + defenses(day(4) + timedelta(hours=3), 40, 40, army=HYDRA)
        )
        story.players[ALT].battles = (
            defenses(day(2), *[5] * 8)
            + defenses(day(3), *[0] * 8)
            + defenses(day(4), 16, 16, 16, army=THROWER)
        )
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
            _verify(owner, me, MAIN)
            _verify(owner, me, ALT)
            app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: noon,
                             dashboard_enabled=True)
            with TestClient(app) as client:

                def save(**change) -> dict:
                    response = call(client, "sloothy", "POST", NOTEBOOK, change)
                    assert response.status_code == 200, response.json()
                    return response.json()

                def notebook(base: str | None = None) -> dict:
                    target = NOTEBOOK if base is None else f"{NOTEBOOK}?base={base}"
                    return call(client, "sloothy", "GET", target).json()

                empty = notebook()
                ring = save(action="add", name="Ring", status="active", tag=MAIN, day=5)["base"]
                box = save(action="add", name="Box", status="to_try")["base"]
                spiral = save(action="add", name="Spiral", status="to_try")["base"]
                hex_ = save(action="add", name="Hex", status="to_try")["base"]
                save(action="pick", tag=MAIN, day=2, base=ring)
                save(action="pick", tag=MAIN, day=4, base=box)
                save(action="pick", tag=ALT, day=2, base=spiral)
                save(action="pick", tag=ALT, day=4, base=ring)
                seen = notebook()
                armies = {code: notebook(code)["base"]["armies"]
                          for code in (ring, box, spiral, hex_)}
                # While the worker relabels armies under new type rules, the
                # base page has no armies rather than old labels.
                with owner.pool.connection() as connection:
                    connection.execute("DELETE FROM army_type_rule_versions")
                relabelling = notebook(ring)
        finally:
            owner.close()
            api.close()

    assert unfinished == []
    assert empty["top"] == []
    records = {base["id"]: base["record"] for base in seen["bases"]}
    # Ring: MAIN's 16 defenses on Days 2 and 3 and ALT's 3 on Day 4.
    assert (records[ring]["defenses"], records[ring]["trophies_lost"]) == (19, 324)
    assert (records[spiral]["defenses"], records[spiral]["trophies_lost"]) == (16, 40)
    assert records[box]["defenses"] == 5
    # Spiral lost 2.5 a defense and Ring about 17; Box has 5 defenses and
    # Hex never ran, too few for the Top 3.
    assert seen["top"] == [spiral, ring]

    def rows(code: str) -> list[tuple]:
        return [(row["name"], row["defenses"], row["held"], row["trophies_lost"])
                for row in armies[code]]

    # Ring held all 6 Hydra defenses, losing 30, and 5 of 7 Thrower ones,
    # losing 178 across both players; its 6 Other defenses are not ranked.
    assert rows(ring) == [("Hydra", 6, 6, 30), ("Thrower", 7, 5, 178)]
    # Box met Hydra twice only, under the 3 a ranked army needs.
    assert rows(box) == [("Thrower", 3, 3, 30)]
    # Spiral met only the Other army; Hex never ran.
    assert (armies[spiral], armies[hex_]) == ([], [])
    assert relabelling["base"]["armies"] is None
