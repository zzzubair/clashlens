"""Numerical examples ported from W9's player-start/client/labels/live/history tests.

Rendering, date formatting and browser timer assertions remain in TypeScript.
These assertions use its inputs and answers, including its different bot labels.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from clashlens.api_player_presentation import (
    BATTLE_DOUBT_CODES,
    PRESENTATION_GAP_CODES,
    day_presentation,
    display_end,
    display_starts,
)


def at(value):
    return datetime.fromisoformat(value)


def saved_day(start="2026-10-07T05:00:00Z", **changes):
    result = {
        "ranked_day_start": start,
        "ranked_day_end": (at(start) + timedelta(days=1)).isoformat(),
        "season_day_number": 3, "state": "Complete", "confidence": "exact",
        "coverage": "complete", "completeness": {"state": "complete"},
        "uncertainty_reasons": [], "battles_complete": True,
        "attack_count": 8, "defense_count": 8, "attack_gain": 310, "defense_loss": 284,
        "attack_three_star_count": 0, "defense_three_star_count": 0,
        "offense_events": [], "defense_events": [], "adjustments": [],
        "start_trophies": 5000, "start_trophies_source": None, "net_trophy_change": 26,
    }
    result.update(changes)
    return result


def start_day(date, losses, attacks=8):
    start = datetime(2026, 9, date, 5, tzinfo=UTC)

    def event(identity, change):
        return {"battle_id": identity, "battle_timestamp": start.replace(hour=13).isoformat(),
                "stars": 3 if abs(change) == 40 else 1, "trophy_change": change,
                "perspective_disagreement": False}

    return saved_day(
        start.isoformat(), season_day_number=date - 6, state="Live", confidence="partial",
        completeness={"state": "partial"}, start_trophies=None, net_trophy_change=None,
        attack_count=attacks, attack_gain=attacks * 40, defense_count=len(losses),
        defense_loss=sum(losses), offense_events=[event(f"a{n}", 40) for n in range(attacks)],
        defense_events=[event(f"d{n}", -loss) for n, loss in enumerate(losses)],
    )


# player-start-trophies.test.ts, every row of its calculateStartingTrophies table.
@pytest.mark.parametrize("observed,variant,trophies,expected,older_expected", [
    ("2026-09-21T21:41:20Z", "normal", 5632, 5462, 5427),
    ("2026-09-22T04:59:00Z", "normal", 5632, 5462, 5427),
    ("2026-09-22T05:00:00Z", "normal", 5632, None, None),
    ("2026-09-21T12:00:00Z", "normal", 5632, None, None),
    ("2026-09-21T21:41:20Z", "missing-count", 5632, None, None),
    ("2026-09-21T21:41:20Z", "partial-current", 5632, None, None),
    ("2026-09-21T21:41:20Z", "uncertain-current", 5632, None, None),
    ("2026-09-21T21:41:20Z", "full-battles", 5632, 5632, 5597),
    ("2026-09-21T21:41:20Z", "stored", 5632, 5500, 5465),
    ("2026-09-21T21:41:20Z", "partial-older", 5632, 5462, None),
    ("2026-09-21T21:41:20Z", "gap", 5632, 5462, None),
    ("2026-09-21T21:41:20Z", "season-reset", 5170, 5000, None),
    ("2026-09-21T21:41:20Z", "season-rule", 5632, 5000, None),
    ("2026-09-21T21:41:20Z", "normal", 170, 0, None),
    ("2026-09-21T21:41:20Z", "normal", 100, None, None),
    ("2026-09-21T21:41:20Z", "negative-net", 5632, 5792, 5757),
    ("2026-09-21T21:41:20Z", "zero-net", 5632, 5632, 5597),
    ("2026-09-21T21:41:20Z", "late-reset-profile", 6040, 6000, 5965),
])
def test_website_start_examples(observed, variant, trophies, expected, older_expected):
    losses = ([] if variant.endswith("-current") else [40] * 8 if variant in ("zero-net", "full-battles")
              else [40] * 6 + [20, 20] if variant == "late-reset-profile"
              else [40] * 4 if variant == "negative-net" else [40, 40, 40, 30])
    day = start_day(21, losses, 1 if variant.endswith("-current") else 0 if variant == "negative-net" else 8)
    older = start_day(19 if variant == "gap" else 20, [40] * 4 if variant == "partial-older" else [40] * 7 + [5])
    if not variant.endswith("-current") and variant not in ("full-battles", "late-reset-profile", "stored"):
        day["completeness"] = {"state": "complete"}
    if variant == "uncertain-current":
        day["completeness"]["state"] = "uncertain"
    if variant == "missing-count":
        day["attack_count"] = None
    if variant == "stored":
        day["start_trophies"] = 5500
    if variant in ("season-reset", "season-rule"):
        day["season_day_number"], older["season_day_number"] = 1, 28
    if variant == "season-rule":
        day.update(start_trophies=5000, start_trophies_source="season_rule")
    saved = [day["start_trophies"], older["start_trophies"]]
    display_starts([day, older], {"observed_at": observed, "current_league_season_id": "1788757200", "trophies": trophies})
    assert [day["display_start"]["trophies"], older["display_start"]["trophies"]] == [expected, older_expected]
    assert [day["start_trophies"], older["start_trophies"]] == saved
    if expected is not None and variant not in ("stored", "season-rule"):
        assert day["display_start"]["calculation"]["end_trophies"] == trophies


@pytest.mark.parametrize("date,number,season,pending,observed,trophies,saved,expected", [
    ("2026-10-05", 1, "1791176400", False, "2026-10-05T20:00:00Z", 5000, None, 5000),
    ("2026-10-05", 1, "1788757200", True, "2026-10-05T20:00:00Z", 5000, None, None),
    ("2026-10-05", 1, "1791176400", True, "2026-10-05T20:00:00Z", 5000, None, None),
    ("2026-10-05", 1, "1791176400", False, "2026-10-05T20:00:00Z", 5957, None, None),
    ("2026-10-05", 1, "1791176400", False, "2026-10-06T06:00:00Z", 5000, 5957, None),
    ("2026-10-05", 1, "1791176400", False, "2026-10-06T06:00:00Z", 5000, 5000, 5000),
    ("2026-10-04", 28, "1788757200", True, "2026-10-04T23:00:00Z", 5000, None, 5000),
])
def test_website_season_client_start_examples(date, number, season, pending, observed, trophies, saved, expected):
    day = start_day(21, [40] * 8)
    day.update(ranked_day_start=date + "T05:00:00Z", ranked_day_end=(at(date + "T05:00:00Z") + timedelta(days=1)).isoformat(),
               season_day_number=number, start_trophies=saved)
    for event in [*day["offense_events"], *day["defense_events"]]:
        event["battle_timestamp"] = date + "T13:00:00Z"
    display_starts([day], {"observed_at": observed, "current_league_season_id": season,
                           "season_reset_pending": pending, "trophies": trophies})
    assert day["display_start"]["trophies"] == expected
    assert day["start_trophies"] == saved


@pytest.mark.parametrize("next_start,expected", [(5957, None), (5000, 5000)])
def test_day_one_backward_inference(next_start, expected):
    day = start_day(21, [40] * 8)
    day.update(season_day_number=1)
    following = saved_day(day["ranked_day_end"], start_trophies=next_start, season_day_number=2)
    display_starts([day, following], {"observed_at": "2026-10-07T06:00:00Z", "current_league_season_id": "1791176400", "trophies": next_start})
    assert day["display_start"]["trophies"] == expected


@pytest.mark.parametrize("changes,expected", [
    ({}, "verified"), ({"confidence": "inferred"}, "calculated"), ({"confidence": None}, "calculated"),
    ({"state": "Partial", "confidence": "partial", "uncertainty_reasons": ["missing_end_baseline"]}, "calculated"),
    ({"state": "Partial", "confidence": "partial", "coverage": "partial", "uncertainty_reasons": ["missing_start_battle_log_baseline", "missing_start_baseline"]}, "calculated"),
    ({"state": "Partial", "confidence": "partial", "coverage": "partial", "uncertainty_reasons": ["missing_start_battle_log_baseline"], "battles_complete": False, "attack_count": 7}, "uncertain"),
    ({"attack_count": 9, "uncertainty_reasons": ["attack_count_exceeds_eight"]}, "uncertain"),
    ({"state": "Partial", "uncertainty_reasons": ["perspective_disagreement"]}, "uncertain"),
    ({"net_trophy_change": None, "state": "Partial", "uncertainty_reasons": ["missing_start_baseline"]}, "uncertain"),
    ({"state": "Partial", "confidence": "partial", "uncertainty_reasons": ["end_reading_unverified"]}, "calculated"),
    ({"state": "Live", "coverage": "partial", "battles_complete": False, "net_trophy_change": 12}, "uncertain"),
])
def test_website_day_labels(changes, expected):
    assert day_presentation(saved_day(**changes), is_current=False)["status"] == expected


@pytest.mark.parametrize("code", sorted(BATTLE_DOUBT_CODES))
def test_all_doubt_codes_remain_uncertain_with_sixteen_battles(code):
    assert day_presentation(saved_day(uncertainty_reasons=[code]), is_current=False)["status"] == "uncertain"


@pytest.mark.parametrize("code", sorted(PRESENTATION_GAP_CODES))
def test_all_gap_codes_require_complete_battles(code):
    day = saved_day(uncertainty_reasons=[code], battles_complete=False)
    assert day_presentation(day, is_current=False)["status"] == "uncertain"
    day["battles_complete"] = True
    assert day_presentation(day, is_current=False)["status"] == "calculated"


@pytest.mark.parametrize("extra", [None, "battle_log_row_gap", "missing_start_baseline", "end_baseline_incomplete", "new_unknown_code"])
def test_website_live_routine(extra):
    waiting = ["missing_end_battle_log_baseline", "automatic_defense_basis_unavailable", "missing_end_baseline"]
    day = saved_day(state="Live", coverage="partial", uncertainty_reasons=waiting + ([] if extra is None else [extra]))
    fact = day_presentation(day, is_current=True)
    assert fact["status"] == "in_progress"
    assert fact["live_routine"] == (extra is None)
    assert fact["caution_codes"] == ([] if extra is None else [extra])
    assert day_presentation(day, is_current=False)["status"] != "in_progress"


@pytest.mark.parametrize("code", ["not_enrolled", "player_not_eligible"])
@pytest.mark.parametrize("battles", [False, True])
def test_website_and_bot_enrollment_prefilters_stay_different(code, battles):
    day = saved_day(uncertainty_reasons=[code], offense_events=[{}] if battles else [])
    fact = day_presentation(day, is_current=False)
    assert fact["show_in_day_summary"] == battles
    assert fact["show_in_player_log"] == (battles or code == "not_enrolled")
    assert fact["status"] == ("not_enrolled" if code == "not_enrolled" else "calculated")
    assert fact["day_summary_status"] == "calculated"


@pytest.mark.parametrize("start,net,next_start,amount,kind,age,left,expected,conflict", [
    (5992, 8, 6000, 0, "weekly_reset", 0, False, 6000, False),
    (5000, 50, 5992, 0, "weekly_reset", 1, False, 5992, True),
    (4900, 20, 5000, 80, "weekly_reset", 2, False, 5000, False),
    (4887, 81, 4968, 32, "weekly_reset", 1, False, 4968, True),
    (5000, 50, None, 0, "weekly_reset", 2, False, None, False),
    (5992, 8, None, 0, "weekly_reset", 0, False, 6000, False),
    (5150, 50, 5200, 0, "weekly_reset", 1, False, 5200, False),
    (4728, -44, None, 0, "weekly_reset", 16, True, 4684, False),
    (5440, -120, 5000, -320, "season_reset", 0, False, 5000, False),
])
def test_website_day_end_examples(start, net, next_start, amount, kind, age, left, expected, conflict):
    day = saved_day(start_trophies=start, net_trophy_change=net,
                    adjustments=[{"type": kind, "amount": amount}] if amount else [])
    day["display_start"] = {"trophies": start}
    end = at(day["ranked_day_end"])
    following = saved_day(end.isoformat(), start_trophies=next_start)
    following["display_start"] = {"trophies": next_start}
    weeks = [{"week_start": end.isoformat(), "league": "below_legend_i"}] if left else []
    fact = display_end(day, [day, following], weeks, end + timedelta(days=age))
    assert fact["player_log"] == expected
    assert (fact["conflict"] is not None) == conflict
    assert fact["before_reset"] == start + net
    assert fact["after_reset"] == start + net + amount
    if conflict:
        assert fact["status"] == "uncertain"


@pytest.mark.parametrize("offset,expected", [(-1, None), (0, None), (4, None), (5, 5462), (24 * 60 + 4, 5462), (24 * 60 + 5, None)])
def test_start_grace_window_is_half_open(offset, expected):
    day = start_day(21, [40, 40, 40, 30])
    day["completeness"] = {"state": "complete"}
    # A profile after every event, so only the battle window rejects it.
    event = day["offense_events"][0]
    event["battle_timestamp"] = (at(day["ranked_day_start"]) + timedelta(minutes=offset)).isoformat()
    following = saved_day(day["ranked_day_end"], start_trophies=5632, season_day_number=16)
    display_starts([day, following], {"observed_at": "2026-09-23T12:00:00Z", "current_league_season_id": "1788757200", "trophies": 5632})
    assert day["display_start"]["trophies"] == expected
