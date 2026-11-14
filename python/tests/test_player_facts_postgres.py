"""W6 E2–E4: saved game responses, real worker, frozen board, API role reads."""
from __future__ import annotations

from datetime import timedelta

from crews_postgres_support import Site
from domain_test_support import domain_database
from fastapi.testclient import TestClient
from player_facts_postgres_support import (
    day_at,
    served_player,
)
from private_api_support import NOW_SECONDS, TS_CURRENT, signed_headers
from reset_scenarios_postgres_support import (
    SEASON,
    Battle,
    Player,
    Story,
    attacks,
    day,
    defenses,
)

from bot.replies import season_days
from clashlens import api_players
from clashlens.api import create_app


def served_public_days(story, tag, now):
    site = Site(story.connection_info)
    try:
        site.account("owner")
        site.link("owner", tag)
        app = create_app(site.api, keys={("typescript-website", "current"): TS_CURRENT},
                         clock=lambda: NOW_SECONDS, now=lambda: now)
        target = "/v1/users/owner/days"
        with TestClient(app) as client:
            response = client.get(target, headers=signed_headers(target))
        assert response.status_code == 200, response.text
        result = response.json()
        import json

        for player in result["players"]:
            before = {key: value for key, value in player.items() if key != "heatmap_day_kinds"}
            size = lambda value: len(json.dumps(value, separators=(",", ":")).encode())
            print(f"public heatmap_day_kinds bytes ({len(player['days'])} saved days): +{size(player) - size(before)}")
        return result
    finally:
        site.close()


def test_goal_and_windows_from_fourteen_real_worker_boards(database_url, archive_server, monkeypatch):
    """14 days permit current/previous 3/7-day comparisons and eight Goal dates.

    Every row starts as a saved game response. No board or public day is
    inserted by the test. All board ordering and saved totals remain pinned.
    """
    you, leader = "#2PP", "#8PP"
    with domain_database(database_url) as info:
        story = Story(info, archive_server, monkeypatch, {tag: Player(tag) for tag in (you, leader)})
        for tag, loss in ((you, 30), (leader, 20)):
            story.players[tag].battles = [battle for number in range(1, 15)
                                         for battle in attacks(day(number), *[40] * 8) + defenses(day(number), *[loss] * 8)]
        story.reset(day(1), {tag: (5000, SEASON) for tag in (you, leader)})
        opening = served_player(story, you, day(1) + timedelta(minutes=10), goal_rank=1)
        assert opening["screen_ready"]["dashboard_goal"]["plan"] is None
        assert opening["screen_ready"]["dashboard_goal"]["finished"] == 0
        for number in range(1, 15):
            totals = {you: 5000 + 80 * number, leader: 5000 + 160 * number}
            story.reads(day(number + 1) - timedelta(minutes=10), totals)
            story.reset(day(number + 1), {tag: (total, SEASON) for tag, total in totals.items()})
            story.reads(day(number + 1) + timedelta(minutes=20), totals)
            # The Day 1 board is first built before the record decides the
            # players' Monday week Legend I: they join at its hourly rebuild.
            story.wait(timedelta(hours=1))
        story.wait(timedelta(hours=1))
        assert story.unfinished_jobs() == []
        for number in range(1, 15):
            board = story.served_board(day(number + 1))
            assert [(entry["tag"], entry["trophies"]) for entry in board["entries"]] == [
                (leader, 5000 + 160 * number), (you, 5000 + 80 * number)]
        before = [story.saved_day(you, day(number)) for number in range(1, 15)]
        page = served_player(story, you, day(15) + timedelta(hours=2), goal_rank=1)
        ready = page["screen_ready"]
        goal = ready["dashboard_goal"]
        assert goal["finished"] == 14 and goal["window"] == 7
        assert (goal["you"], goal["you_then"], goal["line"], goal["line_then"]) == (6120, 5560, 7240, 6120)
        assert goal["history"] == [5000 + 80 * number for number in range(7, 15)]
        plan = goal["plan"]
        assert (plan["pace"], plan["drift"], plan["gap"], plan["need"], plan["days_left"], plan["status"]) == (80, 160, -1120, 240, 14, "behind")
        assert plan["shields"] == [{"days": skipped, "need": 3360 / (14 - skipped), "on_pace": False} for skipped in range(4)]
        for size in ("3", "7"):
            window = ready["dashboard_windows"][size]
            assert window["form"]["now"]["net"] == window["form"]["before"]["net"] == 80
            assert window["form"]["total"] == 80 * int(size)
            assert window["consistency"]["attacks"] == window["consistency"]["slots"] == window["consistency"]["triples"] == 8 * int(size)
            assert window["consistency"]["missed"] == 0
        assert len(season_days(page)) == 15
        no_goal = served_player(story, you, day(15) + timedelta(hours=2))
        assert "dashboard_goal" not in no_goal["screen_ready"]
        # The player page asks for none of the Dashboard's facts and gets
        # the same day, Season and battle facts without them.
        plain = served_player(story, you, day(15) + timedelta(hours=2), dashboard=False)["screen_ready"]
        assert not {"rival_season", "dashboard_season", "dashboard_windows", "dashboard_goal"} & set(plain)
        for key in ("battle_statistics", "season_shape", "trophy_trends", "history_day_starts"):
            assert plain[key] == ready[key]
        assert [saved["presentation"] for saved in plain["days"]] == [saved["presentation"] for saved in ready["days"]]
        assert all("dashboard" in saved["display_end"] for saved in ready["days"])
        assert [{**saved["display_end"], "dashboard": None} for saved in plain["days"]] == [
            {**saved["display_end"], "dashboard": None} for saved in ready["days"]]
        assert not any("dashboard" in saved["display_end"] for saved in plain["days"])
        assert [story.saved_day(you, day(number)) for number in range(1, 15)] == before


def test_day_one_goal_uses_5000_baseline(database_url, archive_server, monkeypatch):
    tag = "#2PP"
    with domain_database(database_url) as info:
        story = Story(info, archive_server, monkeypatch, {tag: Player(tag)})
        story.players[tag].battles = attacks(day(1), *[40] * 8) + defenses(day(1), *[30] * 8)
        story.reset(day(1), {tag: (5000, SEASON)})
        story.reads(day(2) - timedelta(minutes=10), {tag: 5080})
        story.reset(day(2), {tag: (5080, SEASON)})
        story.reads(day(2) + timedelta(minutes=20), {tag: 5080})
        # The player joins the Day 1 board at its hourly rebuild, as their
        # Monday week is Legend I only from Tuesday's Reset reading.
        story.wait(timedelta(hours=1))
        assert story.served_board(day(2))["entries"][0]["trophies"] == 5080
        full = served_player(story, tag, day(2) + timedelta(hours=1), goal_rank=1)["screen_ready"]
        assert full["trophy_trends"]["7"] == {"span": 1, "counted": 1, "total": 80}
        result = full["dashboard_goal"]
        assert result["you_then"] == result["line_then"] == 5000
        assert result["history"] == [5000, 5080]
        assert result["plan"]["pace"] == result["plan"]["drift"] == 80
        # A page without battles names no army, so it decodes none.
        decoded = []
        monkeypatch.setattr(api_players, "decode_army_share_code", lambda code: decoded.append(code))
        totals = served_player(story, tag, day(2) + timedelta(hours=1), battles=False)["screen_ready"]
        assert "trophy_trends" not in totals
        assert totals["army_units"] == {} and decoded == []
        assert totals["rival_season"] == {"hit_rate": 1, "offense": 320, "defense": -240,
                                          "net_per_day": 80, "finished_days": 1, "reset_rank": 1}
        assert totals["rival_season"] == full["rival_season"]
        assert all(not saved["offense_events"] and not saved["defense_events"] for saved in totals["days"])


def test_season_rival_excludes_shields_from_real_worker_days(database_url, archive_server, monkeypatch):
    tag = "#2PP"
    with domain_database(database_url) as info:
        story = Story(info, archive_server, monkeypatch, {tag: Player(tag)})
        # W9's six played days total 37/48 triples, +1810 and -1536.
        plans = {1: (6, 4), 2: (7, 5), 4: (4, 3), 5: (8, 4), 6: (5, 2), 8: (7, 6)}
        for number, (triples, held) in plans.items():
            story.players[tag].battles += attacks(day(number), *([40] * triples + [30] * (8 - triples)))
            story.players[tag].battles += defenses(day(number), *([24] * held + [40] * (8 - held)))
        story.reset(day(1), {tag: (5000, SEASON)})
        end = 5000
        for number in range(1, 9):
            triples, held = plans.get(number, (0, 0))
            if number in plans:
                end += 40 * triples + 30 * (8 - triples) - 24 * held - 40 * (8 - held)
            story.reads(day(number + 1) - timedelta(minutes=10), {tag: end})
            story.reset(day(number + 1), {tag: (end, SEASON)})
            story.reads(day(number + 1) + timedelta(minutes=20), {tag: end})
            story.run_worker()
        story.wait(timedelta(hours=1))
        assert story.served_board(day(9))["entries"][0]["trophies"] == 5274
        before = [story.saved_day(tag, day(number)) for number in range(1, 9)]
        assert before[2]["attacks"] == before[2]["defenses"] == before[6]["attacks"] == before[6]["defenses"] == 0
        assert before[2]["end"] == before[2]["start"] == 5114
        assert before[6]["end"] == before[6]["start"] == 5188
        ready = served_player(story, tag, day(9) + timedelta(hours=1), battles=False)["screen_ready"]
        # The Watchlist's Whole Season row: 274 net over the six played days.
        assert ready["rival_season"] == {"hit_rate": 37 / 48, "offense": 1810 / 6, "defense": -1536 / 6,
                                         "net_per_day": 274 / 6, "finished_days": 8, "reset_rank": 1}
        assert "dashboard_season" not in ready and "battle_statistics" not in ready
        assert [story.saved_day(tag, day(number)) for number in range(1, 9)] == before


def test_full_battles_profile_start_is_only_a_display_projection(database_url, archive_server, monkeypatch):
    tag = "#2PP"
    with domain_database(database_url) as info:
        story = Story(info, archive_server, monkeypatch, {tag: Player(tag)})
        story.players[tag].battles = attacks(day(2), *[40] * 8) + defenses(day(2), *[30] * 8)
        story.reset(day(2), {tag: (None, SEASON)})
        story.reads(day(3) - timedelta(minutes=10), {tag: 5420})
        story.run_worker()
        before = served_player(story, tag, day(3) - timedelta(minutes=1))
        finished = day_at(before, day(2))
        assert finished["start_trophies"] is None
        assert finished["display_start"] == {
            "trophies": 5340, "source": "calculated",
            "calculation": {"end_trophies": 5420, "net_change": 80},
        }
        story.reset(day(3), {tag: (5420, SEASON)})
        story.reads(day(3) + timedelta(minutes=20), {tag: 5420})
        story.run_worker()
        board = story.served_board(day(3))
        assert board["entries"][0]["trophies"] == 5420
        assert story.saved_day(tag, day(2))["start"] is None
        after_page = served_player(story, tag, day(3) + timedelta(hours=1))
        after = day_at(after_page, day(2))
        # The profile has moved off this day and no following day was
        # published. The website's original guards withdraw the calculation.
        assert after_page["screen_ready"]["current_day_start"] is None
        assert after["display_start"] == {"trophies": None, "source": "unknown", "calculation": None}
        assert after["start_trophies"] is None


def test_leaving_returning_and_signing_up_later_from_saved_game_data(database_url, archive_server, monkeypatch):
    tag = "#2PP"
    with domain_database(database_url) as info:
        story = Story(info, archive_server, monkeypatch, {tag: Player(tag)})
        story.players[tag].battles = [battle for number in range(1, 8)
                                     for battle in attacks(day(number), *[30] * 8) + defenses(day(number), *[40] * 8)]
        # The eighth attack is reported at 05:03 after the closing Reset.
        story.players[tag].battles += attacks(day(17), *[40] * 7) + defenses(day(17), *[30] * 8) + [Battle(day(18) + timedelta(minutes=3), 40, True)]
        story.reset(day(1), {tag: (5000, SEASON)})
        for number in range(1, 8):
            end = 5000 - 80 * number
            story.reads(day(number + 1) - timedelta(minutes=10), {tag: end})
            story.players[tag].dropped = number == 7
            story.reset(day(number + 1), {tag: (end, SEASON)})
            story.reads(day(number + 1) + timedelta(minutes=20), {tag: end})
            story.run_worker()
        story.players[tag].dropped = False
        for number in (15, 16, 17):
            story.reset(day(number), {tag: (5000, None)})
            story.reads(day(number) + timedelta(minutes=20), {tag: 5000}, season=None)
            story.run_worker()
        story.reads(day(18) - timedelta(minutes=10), {tag: 5040})
        story.reset(day(18), {tag: (5080, SEASON)})
        story.reads(day(18) + timedelta(minutes=20), {tag: 5080})
        story.run_worker()
        story.wait(timedelta(hours=1))
        assert story.served_board(day(18))["entries"][0]["trophies"] == 5040
        page = served_player(story, tag, day(18) + timedelta(hours=2))
        ready = page["screen_ready"]
        assert ready["history_day_numbers"][day(17).isoformat()] == 17
        assert ready["season_shape"]["kinds"][:18] == ["legend"] * 7 + ["outside"] * 7 + ["waiting"] * 2 + ["legend"] * 2
        assert ready["season_shape"]["legend_days"] == {"expected": 8, "saved": 8}
        assert [run["signed_up"] for run in ready["season_shape"]["runs"]] == [1, 17]
        assert ready["season_shape"]["runs"][0]["dropped"] is True
        assert ready["season_shape"]["runs"][1]["promoted"] is True
        assert ready["stats_day_starts"] == [day(n).isoformat() for n in [*range(1, 8), *range(15, 19)]]
        assert ready["battle_statistics"]["7"]["attack"]["count"] == 8
        assert ready["battle_statistics"]["7"]["attack"]["played_days"] == 1
        assert len(day_at(page, day(17))["offense_events"]) == 8
        assert story.saved_day(tag, day(17))["end"] == 5080
        linked = served_public_days(story, tag, day(18) + timedelta(hours=2))
        assert linked["calendar"]["day_number"] == 18
        assert linked["calendar"]["as_of"] == (day(18) + timedelta(hours=2)).isoformat()
        player = linked["players"][0]
        assert player["reset_position"] == 1
        # Days 8–14 were outside Legend I; 15–16 were 5,000 before sign-up.
        # The compact endpoint has no current saved day in this story.
        assert player["heatmap_day_numbers"] == [*range(1, 8), 17]
        kinds = dict(zip((kept[0] for kept in player["days"]), player["heatmap_day_kinds"], strict=True))
        assert kinds[17] == "p"
        assert kinds[15] == "s"
        assert day_at(page, day(17))["presentation"]["heatmap_kind"] == "played"
        assert [kept[0] for kept in player["days"] if kept[4]] == [15, 16]
