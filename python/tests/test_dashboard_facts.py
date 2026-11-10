"""Numerical examples from W9 dashboard-cards and season-heat-grid tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_player_presentation_facts import saved_day

from clashlens.api_dashboard_facts import dashboard_season, dashboard_windows, goal_plan

START = datetime(2026, 9, 7, 5, tzinfo=UTC)
CONTEXT = {"start": START.isoformat(), "end": (START + timedelta(days=28)).isoformat(), "current_day_number": 9}


def card_day(number, net, **changes):
    return {"day": number, "attacks": 8, "defenses": 8, "offense": 300, "defense": net - 300,
            "net": net, "triples": 6, "held": 4, "end_trophies": 5000 + net, "rank": None, "hits": [], **changes}


def data(days, today=9):
    return {"days": days, "today": today}


def test_current_and_previous_three_day_form():
    days = [card_day(number, net) for number, net in ((3, 0), (4, 8), (5, 64), (6, 2), (8, 86))]
    form = dashboard_windows(data(days))["3"]
    assert (form["first"], form["last"], form["span"]) == (6, 8, 3)
    assert form["form"]["now"]["net"] == 44
    assert form["form"]["before"]["net"] == 24
    assert dashboard_windows(data([card_day(1, 10), card_day(2, 20)], 3))["3"]["form"]["before"] is None
    assert dashboard_windows(data([], 1)) == {"3": None, "7": None, "season": None}


def test_short_day_counts_every_attack_slot_as_a_miss():
    result = dashboard_windows(data([card_day(8, 0, hits=[[3, 40], [3, 40], [1, 12]])]))["3"]["consistency"]
    assert (result["attacks"], result["slots"], result["triples"], result["stars"]) == (3, 8, 2, [2, 0, 1, 0])
    assert result["missed"] == 92 - 320
    assert result["per_miss"] == 12


def goal(**changes):
    return {"rank": 200, "finished": 8, "window": 7, "you": 5324, "you_then": 5044,
            "line": 5400, "line_then": 5120, "history": [5044, changes.get("you", 5324)], **changes}


@pytest.mark.parametrize("changes,expected", [
    ({}, "behind"), ({"you_then": 5000}, "on"),
    ({"you": 5600, "you_then": 5300, "line_then": 5400}, "ahead"),
])
def test_goal_verdicts(changes, expected):
    result = goal_plan(goal(**changes))
    assert result["status"] == expected
    if expected == "ahead":
        assert result["gap"] == 200


def test_goal_shields_board_gap_and_daily_pace():
    result = goal_plan(goal())
    assert [item["days"] for item in result["shields"]] == [0, 1, 2, 3]
    assert result["pace"] == result["drift"] == 40
    assert result["gap"] == -76
    assert result["need"] == 43.8
    assert result["shields"][0] == {"days": 0, "need": 43.8, "on_pace": False}
    assert [item["days"] for item in goal_plan(goal(finished=26))["shields"] if item["days"]] == [1]
    assert goal_plan(goal(line=None)) is None
    result = goal_plan(goal(history=[5044, 5444]))
    assert result["gap"] == 44 and result["pace"] == 40
    assert goal_plan(goal(history=[5044, None])) is None
    assert goal_plan(goal(finished=0, window=0)) is None


def main_days(*, plan_changes=None, **day_six):
    result = []
    plans = [(1, 6, 4, 5000, 900, 8, 8), (2, 7, 5, 5044, 700, 8, 8),
             (3, 0, 0, 5114, 760, 0, 0), (4, 4, 3, 5114, 800, 8, 8),
             (5, 8, 4, 5122, 500, 8, 8), (6, 5, 2, 5186, 520, 8, 8), (8, 7, 6, 5238, 318, 8, 8)]
    for number, triples, held, start, rank, attacks, defenses in plans:
        plan = plan_changes if number == 6 and plan_changes else {}
        triples, held = plan.get("triples", triples), plan.get("held", held)
        attacks, defenses = plan.get("attacks", attacks), plan.get("defenses", defenses)
        gap = plan.get("gap", False)
        gains = [40 if index < triples else 30 for index in range(attacks)]
        losses = [24 if index < held else 40 for index in range(defenses)]
        begins = START + timedelta(days=number - 1)
        events = [{"stars": 3 if gain == 40 else 2, "trophy_change": gain,
                   "battle_timestamp": (begins + timedelta(hours=index + 1)).isoformat(), "perspective_disagreement": False}
                  for index, gain in enumerate(gains)]
        day = saved_day(begins.isoformat(), season_day_number=number, start_trophies=start,
                        reset_rank=rank, attack_count=attacks, defense_count=defenses,
                        attack_three_star_count=min(triples, attacks), defense_three_star_count=defenses - held,
                        attack_gain=sum(gains), defense_loss=sum(losses), net_trophy_change=None if gap else sum(gains) - sum(losses), offense_events=events,
                        state="Partial" if gap else "Complete", confidence="partial" if gap else "exact",
                        coverage="partial" if gap else "complete", battles_complete=not gap and attacks == 8 and defenses == 8,
                        uncertainty_reasons=["battle_log_gap"] if gap else [])
        if number == 6:
            day.update(day_six)
        day["display_start"] = {"trophies": start}
        result.append(day)
    return result


def test_full_loader_numerical_example():
    days = main_days()
    season = dashboard_season(CONTEXT, days[-2:], days, None)
    assert [day["net"] for day in season["days"]] == [44, 70, 0, 8, 64, 2, 86]
    assert [day["end_trophies"] for day in season["days"]] == [5044, 5114, 5114, 5122, 5186, 5188, 5324]
    assert [day["kind"] for day in season["days"]] == ["played", "played", "shield", "played", "played", "played", "played"]
    window = dashboard_windows(season)["7"]
    assert window["day_numbers"] == [2, 3, 4, 5, 6, 8]
    form = window["form"]
    assert form["counted"] == 6 and form["total"] == 230
    assert form["now"]["net"] == 46
    assert form["now"]["per_attack"] == 1510 / 40
    assert form["now"]["tripled"] == 31 / 40
    assert form["now"]["per_defense"] == -32
    assert form["now"]["held"] == 0.5
    consistency = window["consistency"]
    assert (consistency["attacks"], consistency["slots"], consistency["triples"]) == (40, 48, 31)
    assert consistency["per_attack"] == 37.75 and consistency["per_miss"] == 30
    assert consistency["missed"] == -410
    assert consistency["per_day"] == [{"day": number, "triples": triples} for number, triples in ((2, 7), (3, 0), (4, 4), (5, 8), (6, 5), (8, 7))]


def test_unknown_sides_remain_independent():
    days = main_days(net_trophy_change=None, battles_complete=False, attack_gain=None)
    season = dashboard_season(CONTEXT, [], days, None)
    missing = next(day for day in season["days"] if day["day"] == 6)
    assert missing["net"] is None and missing["hits"] is None
    window = dashboard_windows(season)["7"]
    assert window["form"]["counted"] == 5
    assert window["form"]["now"]["net"] == 57
    assert window["consistency"]["attacks"] == 32
    assert (window["consistency"]["triples"], window["consistency"]["slots"]) == (26, 40)
    days = main_days(net_trophy_change=None, battles_complete=False, defense_loss=None, coverage="partial")
    season = dashboard_season(CONTEXT, [], days, None)
    missing = next(day for day in season["days"] if day["day"] == 6)
    assert (missing["net"], missing["triples"], missing["offense"]) == (None, 5, 290)
    assert len(missing["hits"]) == 8
    assert dashboard_windows(season)["7"]["consistency"]["triples"] == 31


@pytest.mark.parametrize("net,expected", [(-304, "played"), (0, "shield"), (None, "missing")])
def test_heat_grid_uses_the_existing_display_shield_rule(net, expected):
    day = saved_day(START.isoformat(), attack_count=0, defense_count=0, attack_gain=0, defense_loss=0,
                    net_trophy_change=net, battles_complete=net is not None)
    day["display_start"] = {"trophies": 5000}
    result = dashboard_season({**CONTEXT, "current_day_number": 2}, [], [day], None)["days"][0]
    assert result["kind"] == expected
    assert result["net"] == net


def test_dashboard_has_broader_end_fallback_than_player_log():
    days = main_days()
    days[0].update(net_trophy_change=None, battles_complete=True)
    days[1]["display_start"]["trophies"] = None
    days[0]["adjustments"] = [{"type": "weekly_reset", "amount": 80}]
    result = dashboard_season(CONTEXT, [], days, None)
    assert result["days"][0]["end_trophies"] == 5124
    assert days[0]["net_trophy_change"] is None
    days[0]["offense_events"] *= 2
    assert dashboard_season(CONTEXT, [], days, None)["days"][0]["hits"] is None
    assert dashboard_season(None, [], days, None) is None


@pytest.mark.parametrize("plan,attacks,triples,slots", [
    ({"triples": 6, "held": 0, "attacks": 6, "gap": True}, 32, 26, 40),
    ({"defenses": 6, "gap": True}, 40, 31, 48),
    ({"attacks": 9, "triples": 9}, 32, 26, 40),
])
def test_exact_website_loader_gap_and_ninth_battle_inputs(plan, attacks, triples, slots):
    days = main_days(plan_changes=plan)
    season = dashboard_season(CONTEXT, days[-2:], days, None)
    sixth = next(day for day in season["days"] if day["day"] == 6)
    window = dashboard_windows(season)["7"]
    assert (window["consistency"]["attacks"], window["consistency"]["triples"], window["consistency"]["slots"]) == (attacks, triples, slots)
    if plan.get("gap"):
        assert sixth["net"] is None and sixth["end_trophies"] is None
    if plan.get("attacks") == 6:
        assert sixth["hits"] is None
        assert window["form"]["counted"] == 5
        assert window["form"]["total"] == 228
        assert window["form"]["now"]["net"] == 57
    if plan.get("defenses") == 6:
        assert sixth["triples"] == 5 and sixth["offense"] == 290
        assert len(sixth["hits"]) == 8
    if plan.get("attacks") == 9:
        assert sixth["hits"] is None


def test_exact_closing_reset_example():
    days = main_days()
    days[-1]["adjustments"] = [{"type": "weekly_reset", "amount": 80}]
    assert dashboard_season(CONTEXT, [], days, None)["days"][-1]["end_trophies"] == 5404
