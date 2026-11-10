"""W9 season-shape, battle-statistics and player-season-history examples."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_player_presentation_facts import saved_day

from clashlens.api_player_presentation import display_starts
from clashlens.api_player_season_facts import (
    battle_statistics,
    historical_facts,
    player_facts,
    shape_of,
)

SEASON = datetime(2026, 10, 5, 5, tzinfo=UTC)
DAY = timedelta(days=1)
NOW = SEASON + 23 * DAY + timedelta(hours=7)


def played(first, last, start=5000, change=9):
    result = []
    for number in range(first, last + 1):
        day = saved_day((SEASON + (number - 1) * DAY).isoformat(), season_day_number=number,
                       start_trophies=start + (number - first) * change, net_trophy_change=change)
        day["display_start"] = {"trophies": day["start_trophies"]}
        result.append(day)
    return result


def weeks(pattern, entries=()):
    return [{"week_start": (SEASON + index * 7 * DAY).isoformat(),
             "league": {"L": "legend_i", "B": "below_legend_i"}[letter],
             "entry": entries[index] if index < len(entries) else None}
            for index, letter in enumerate(pattern)]


def shape(days, pattern, entries=(), today=24):
    return shape_of(SEASON, today, weeks(pattern, entries), {day["season_day_number"]: day for day in days})


def test_monday_exit_wednesday_signup_and_return():
    days = played(1, 7, 5000, -45) + played(15, 16, 5000, 0) + played(17, 17, 5000, 31) + played(18, 24, 5031, 12)
    for day in days[7:9]:
        day.update(attack_count=0, defense_count=0, battles_complete=False)
    days[9].update(attack_count=2, defense_count=2)
    fact = shape(days, "LBLL", ["season_start", None, "promoted", "stayed"])
    assert fact["partial"] is True and fact["outside_now"] is False
    assert fact["kinds"][:24] == ["legend"] * 7 + ["outside"] * 7 + ["waiting"] * 2 + ["legend"] * 8
    assert fact["legend_days"] == {"expected": 14, "saved": 14}
    assert fact["runs"] == [
        {"first": 1, "last": 7, "signed_up": 1, "promoted": False, "dropped": True},
        {"first": 15, "last": 24, "signed_up": 17, "promoted": True, "dropped": False},
    ]
    stats = battle_statistics(days, days, SEASON, NOW, fact)["season"]
    assert stats["days_saved"] == stats["days_expected"] == 14
    assert stats["incomplete"] is False
    days[9]["battles_complete"] = False
    assert battle_statistics(days, days, SEASON, NOW, fact)["season"]["incomplete"] is True


@pytest.mark.parametrize("pattern,first,expected,partial,outside", [
    ("LBBB", 1, 7, True, True), ("BLLL", 8, 16, True, False),
    ("LLLL", 1, 23, False, False), ("", 1, 7, False, False),
])
def test_website_membership_examples(pattern, first, expected, partial, outside):
    last = 7 if pattern in ("LBBB", "") else 24
    fact = shape(played(first, last), pattern)
    assert fact["legend_days"] == {"expected": expected, "saved": expected}
    assert fact["partial"] is partial
    assert fact["outside_now"] is outside
    assert len(fact["kinds"]) == 28
    if not pattern:
        assert fact["kinds"][7:24] == ["unrecorded"] * 17
    if outside:
        assert fact["last_legend_day_start"] == (SEASON + 6 * DAY).isoformat()


def test_unknown_day_is_legend_missing_day_is_not_saved():
    days = played(8, 24)
    days[0].update(start_trophies=None, attack_count=None, defense_count=None, net_trophy_change=None,
                   display_start={"trophies": None})
    fact = shape(days, "BLLL")
    assert fact["kinds"][7] == "legend"
    assert fact["legend_days"] == {"expected": 16, "saved": 16}
    fact = shape([day for day in days if day["season_day_number"] != 10], "BLLL")
    assert fact["legend_days"] == {"expected": 16, "saved": 15}


def test_historical_shape_needs_no_retained_daily_detail():
    start = datetime(2026, 9, 7, 5, tzinfo=UTC)
    entries = [
        {"season_day_number": number, "ranked_day_start": (start + (number - 1) * DAY).isoformat(),
         "state": "Complete", "confidence": "exact", "coverage": "complete", "flags": [],
         "attack_count": 8, "defense_count": 8, "start_trophies": 5000 - (number - 1) * 6,
         "end_trophies": 5000 - number * 6, "net_change": -6}
        for number in range(1, 15)
    ]
    summary = {"season_start": start.isoformat(), "daily_entries": entries,
               "days_observed": 14, "attack_count": 112, "defense_count": 112,
               "attack_gain": None, "defense_loss": None,
               "season_weeks": [{"week_start": (start + offset * DAY).isoformat(),
                                 "league": "legend_i" if offset < 14 else "below_legend_i", "entry": None}
                                for offset in (0, 7, 14, 21)]}
    historical_facts(summary)
    assert summary["season_shape"]["today"] == 29
    assert summary["season_shape"]["legend_days"] == {"expected": 14, "saved": 14}
    assert summary["season_shape"]["last_legend_day_start"] == entries[-1]["ranked_day_start"]
    assert entries[-1]["end_trophies"] == 4916
    assert all(entry["presentation"]["status"] == "verified" for entry in entries)
    assert all(entry["presentation"]["battle_net"] is None for entry in entries)
    summary["season_start"] = None
    historical_facts(summary)
    assert summary["season_shape"] is None


TODAY = datetime(2026, 10, 4, 5, tzinfo=UTC)
BATTLE_NOW = TODAY + timedelta(hours=15)
BATTLE_SEASON = datetime(2026, 9, 7, 5, tzinfo=UTC)


def event(identity, stars=3, trophies=40, at=None, **changes):
    return {"battle_id": identity, "stars": stars, "trophy_change": trophies,
            "battle_timestamp": (at or TODAY).isoformat(), "perspective_disagreement": False, **changes}


def battle_day(offset, offense=(), defense=(), **changes):
    return saved_day((TODAY - offset * DAY).isoformat(), season_day_number=28 - offset,
                     offense_events=list(offense), defense_events=list(defense),
                     attack_count=len(offense), defense_count=len(defense), **changes)


def statistics(days, period="7", now=BATTLE_NOW, start=BATTLE_SEASON, shape=None):
    return battle_statistics(days, days, start, now, shape)[period]


def test_finished_battles_only_and_zero_star_defense_counts():
    days = [battle_day(0, [event("a1")], [event("d1", 0, 0), event("d2", 1, -5)]),
            battle_day(1, [event("a2", 2, 20, TODAY - DAY), event("a3", 1, 10, TODAY - DAY)],
                       [event("d3", 2, -20, TODAY - DAY), event("d4", 0, 0, TODAY - DAY)])]
    fact = statistics(days)
    assert fact["attack"] == {"count": 2, "stars": [0, 1, 1, 0], "trophies": 30,
                              "played_days": 1, "per_day": 30, "per_battle": 15, "disputed": False}
    assert fact["defense"]["stars"] == [1, 0, 1, 0]
    assert fact["defense"]["trophies"] == 20
    assert (fact["days_saved"], fact["days_expected"]) == (1, 7)


def test_grace_battle_stays_on_published_day():
    fact = statistics([battle_day(0), battle_day(1, [event("a1", at=TODAY - DAY), event("late", at=TODAY + timedelta(minutes=1))],
                                               [event("d1", trophies=-40, at=TODAY + timedelta(minutes=1))])])
    assert (fact["attack"]["count"], fact["attack"]["per_day"]) == (2, 80)
    assert (fact["defense"]["count"], fact["defense"]["per_day"]) == (1, 40)


@pytest.mark.parametrize("period,count", [("7", 7), ("14", 14), ("season", 27)])
def test_every_battle_window(period, count):
    days = [battle_day(offset, [event(f"a{offset}", at=TODAY - offset * DAY)]) for offset in range(30)]
    fact = statistics(days, period)
    assert fact["attack"]["count"] == fact["days_expected"] == fact["days_saved"] == count
    assert datetime.fromisoformat(fact["start"]) == TODAY - count * DAY


def test_duplicate_reports_and_gaps_do_not_add_battles_or_old_days():
    repeated = event("same", at=TODAY - DAY)
    fact = statistics([battle_day(1, [repeated, repeated]), battle_day(20, [event("old", at=TODAY - 20 * DAY)])])
    assert (fact["attack"]["count"], fact["days_saved"], fact["days_expected"]) == (1, 1, 7)


def test_battle_free_and_played_day_denominators():
    fact = statistics([battle_day(1)], "season")
    for side in (fact["attack"], fact["defense"]):
        assert side == {"count": 0, "stars": [0, 0, 0, 0], "trophies": 0, "played_days": 0,
                        "per_day": None, "per_battle": None, "disputed": False}
    days = [battle_day(offset, [event(f"a{offset}", trophies=56, at=TODAY - offset * DAY)],
                       [] if offset == 6 else [event(f"d{offset}", 1, -20, TODAY - offset * DAY)])
            for offset in (1, 2, 3, 5, 6)] + [battle_day(4), battle_day(7)]
    fact = statistics(days)
    assert fact["days_saved"] == 7
    assert (fact["attack"]["count"], fact["attack"]["trophies"], fact["attack"]["per_day"]) == (5, 280, 56)
    assert (fact["defense"]["count"], fact["defense"]["trophies"], fact["defense"]["per_day"]) == (4, 80, 20)


def test_new_season_excludes_previous_season_and_has_no_finished_day():
    days = [battle_day(0, [event("last-season")])]
    reset = TODAY + DAY
    assert statistics(days, "season", reset - timedelta(milliseconds=1))["attack"]["count"] == 0
    fact = statistics(days, "season", reset, reset)
    assert fact["days_expected"] == fact["attack"]["count"] == 0
    fact = statistics(days, "7", reset + DAY, reset)
    assert fact["days_expected"] == 1 and fact["attack"]["count"] == 0


def test_future_and_old_events_excluded_disputes_remain_visible():
    day = battle_day(1, [event("before", at=TODAY - 8 * DAY), event("future", at=BATTLE_NOW + timedelta(milliseconds=1)),
                         event("disputed", 2, 30, TODAY - DAY, perspective_disagreement=True)], battles_complete=False)
    fact = statistics([day])
    assert fact["attack"]["count"] == 1 and fact["attack"]["disputed"] and fact["incomplete"]


def test_unenrolled_and_uncertain_days_keep_observed_battle_statistics():
    days = [battle_day(1, [event("a1", trophies=320, at=TODAY - DAY)]), battle_day(2),
            battle_day(3, uncertainty_reasons=["not_enrolled"]),
            battle_day(4, [event("a4", at=TODAY - 4 * DAY)], uncertainty_reasons=["player_not_eligible"]),
            battle_day(5, uncertainty_reasons=["missing_start_baseline", "player_not_eligible"]),
            battle_day(6, uncertainty_reasons=["player_not_eligible"])]
    fact = statistics(days)
    assert fact["days_saved"] == 6
    assert (fact["attack"]["count"], fact["attack"]["trophies"], fact["attack"]["per_day"]) == (2, 360, 180)
    fact = statistics([battle_day(1, [event("a1", at=TODAY - DAY)]),
                       battle_day(2, [event("a2", 1, 10, TODAY - 2 * DAY)], net_trophy_change=None), battle_day(3)])
    assert (fact["attack"]["count"], fact["attack"]["per_battle"], fact["attack"]["per_day"]) == (2, 25, 25)


@pytest.mark.parametrize("latest_rank,prior_rank,expected", [(1042, 2000, 1), (None, 1042, 2), (None, None, 1)])
def test_latest_ranked_reset_fallback(latest_rank, prior_rank, expected):
    days = [battle_day(0), battle_day(1, reset_rank=latest_rank), battle_day(2, reset_rank=prior_rank)]
    assert statistics(days)["reset_day_start"] == days[expected]["ranked_day_start"]
    # A dropped player retains their last Legend I day, even without a rank.
    dropped = {"outside_now": True, "last_legend_day_start": days[2]["ranked_day_start"]}
    assert statistics(days, shape=dropped)["reset_day_start"] == days[2]["ranked_day_start"]


def test_history_filter_and_bot_membership_keep_different_missing_days():
    days = played(1, 10)
    days[3]["uncertainty_reasons"] = ["player_not_eligible"]
    days[4]["uncertainty_reasons"] = ["player_not_eligible", "not_enrolled"]
    days[5]["uncertainty_reasons"] = ["player_not_eligible"]
    days[5]["offense_events"] = [event("battle", at=SEASON + 5 * DAY)]
    days = [day for day in days if day["season_day_number"] != 9]
    page = {"trophies": 5500, "current_league_season_id": str(int(SEASON.timestamp())), "observed_at": NOW.isoformat(),
            "screen_ready": {"days": days, "current_day_start": days[-1]["ranked_day_start"],
                             "recent_day_starts": [day["ranked_day_start"] for day in reversed(days)],
                             "season_day_starts": [day["ranked_day_start"] for day in days],
                             "season_weeks": weeks("BLLL"),
                             "season": {"start": SEASON.isoformat(), "end": (SEASON + 28 * DAY).isoformat(), "current_day_number": 10}}}
    player_facts(page, SEASON + 9 * DAY, battles=False)
    ready = page["screen_ready"]
    assert ready["stats_day_starts"] == [(SEASON + offset * DAY).isoformat() for offset in (7, 8, 9)]
    assert (SEASON + 8 * DAY).isoformat() not in ready["season_day_starts"]
    assert ready["history_day_starts"] == [day["ranked_day_start"] for day in reversed(days) if day["season_day_number"] != 4]
    assert "battle_statistics" not in ready
    # Day 1 of a new Season never resurrects the previous Season's days.
    player_facts(page, SEASON + 28 * DAY, battles=True)
    assert ready["history_day_starts"] == []


def test_start_groups_are_calculated_independently():
    from test_player_presentation_facts import start_day

    current = start_day(21, [40, 40, 40, 30])
    current["completeness"] = {"state": "complete"}
    older = start_day(20, [40] * 7 + [5])
    profile = {"trophies": 5632, "current_league_season_id": "1788757200", "observed_at": "2026-09-21T21:41:20Z"}
    # The Season group has the next day; recent does not. A combined pass
    # would invent the recent group's missing start, changing the old answer.
    first, second = dict(older), dict(older)
    display_starts([first, dict(current)], profile)
    display_starts([second], profile)
    assert first["display_start"]["trophies"] == 5427
    assert second["display_start"]["trophies"] is None


@pytest.mark.parametrize("context,new_day,now,expected", [
    (False, True, "2026-10-05T05:10:00Z", [1]),
    (True, False, "2026-10-05T05:10:00Z", []),
    (True, False, "2026-10-05T04:10:00Z", [28, 27, 26, 25, 24]),
])
def test_exact_history_before_and_after_season_reset(context, new_day, now, expected):
    start = datetime(2026, 9, 7, 5, tzinfo=UTC)
    dates = list(range(24, 29)) + ([29] if new_day else [])
    days = [saved_day((start + (number - 1) * DAY).isoformat(), season_day_number=number) for number in dates]
    page = {"trophies": 5500, "current_league_season_id": str(int(start.timestamp())), "observed_at": now,
            "screen_ready": {"days": days, "current_day_start": None, "season_day_starts": [],
                             "recent_day_starts": [day["ranked_day_start"] for day in days], "season_weeks": [],
                             "season": {"start": start.isoformat(), "end": SEASON.isoformat(), "current_day_number": 28} if context else None}}
    player_facts(page, datetime.fromisoformat(now), battles=True)
    ready = page["screen_ready"]
    assert [ready["history_day_numbers"][value] for value in ready["history_day_starts"]] == expected
    assert [day["season_day_number"] for day in ready["days"]] == dates


@pytest.mark.parametrize("player_index", [0, 1])
def test_player_battle_facts_keep_the_shared_crew_denominators(player_index):
    import json
    from pathlib import Path

    fixture = json.loads((Path(__file__).parents[2] / "testdata/crew-boards-parity.json").read_text())
    player = fixture["players"][player_index]
    days = []
    for raw in player["days"]:
        days.append(saved_day(
            raw["start"],
            offense_events=[event for event in raw["battles"] if event["lens"] == "offense"],
            defense_events=[event for event in raw["battles"] if event["lens"] == "defense"],
        ))
    facts = battle_statistics(days, days, datetime.fromisoformat(fixture["season_start"]),
                              datetime.fromisoformat(fixture["now"]), None)
    for period, key in (("season", "season"), ("7", "week")):
        for side in ("attack", "defense"):
            want = player["expected"][key][side]
            actual = facts[period][side]
            assert actual["count"] == want["battles"]
            assert actual["trophies"] == want["total"]
            assert actual["played_days"] == want["days"]
            assert actual["per_day"] == want["total"] / want["days"]


def test_new_season_with_nothing_saved_still_has_its_28_day_shape():
    # Minutes after a Season Reset, before any day, week or Season context is
    # saved, the page still draws the new Season's empty 28 days.
    now = SEASON + timedelta(minutes=10)
    page = {"trophies": 5000, "current_league_season_id": None, "observed_at": now.isoformat(),
            "screen_ready": {"days": [], "current_day_start": None, "season_day_starts": [],
                             "recent_day_starts": [], "season_weeks": [], "season": None}}
    player_facts(page, now, battles=False)
    shape = page["screen_ready"]["season_shape"]
    assert shape is not None
    assert shape["today"] == 1
    assert shape["season_start"] == SEASON.isoformat()
