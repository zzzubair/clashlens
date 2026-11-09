"""Shared setup for player facts postgres tests."""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role
from fastapi.testclient import TestClient
from private_api_support import NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase


def served_player(story, tag, now, *, goal_rank=None, battles=True):
    api = ApiDatabase(as_api_role(story.connection_info))
    try:
        app = create_app(api, keys={("typescript-website", "current"): TS_CURRENT},
                         clock=lambda: NOW_SECONDS, now=lambda: now)
        target = f"/v1/players/%23{tag[1:]}" + (f"?goal_rank={goal_rank}" if goal_rank is not None else "")
        if not battles:
            target += "?battles=false"
        with TestClient(app) as client:
            response = client.get(target, headers=signed_headers(target))
        assert response.status_code == 200, response.text
        result = response.json()
        import json

        ready = result["screen_ready"]
        for field in ("history_day_numbers", "trophy_trends", "rival_season"):
            if field in ready:
                before = dict(ready)
                before.pop(field)
                size = lambda value: len(json.dumps(value, separators=(",", ":")).encode())
                print(f"{field} bytes ({'full' if battles else 'light'}, {len(ready['days'])} saved days): +{size(ready) - size(before)}")
        rival = ready["rival_season"]
        without_rank = {key: value for key, value in rival.items() if key != "reset_rank"}
        print(f"rival reset_rank bytes: +{size(rival) - size(without_rank)}")
        if battles and ready["days"]:
            shown = ready["days"][0]["presentation"]
            before = {key: value for key, value in shown.items() if key != "heatmap_kind"}
            print(f"heatmap_kind bytes per day: +{size(shown) - size(before)}")
        return result
    finally:
        api.close()


def day_at(page, start):
    from datetime import datetime

    return next(day for day in page["screen_ready"]["days"] if datetime.fromisoformat(day["ranked_day_start"]) == start)


def assert_wednesday_facts(story, tuesday, wednesday, tags):
    """Extend the existing Wednesday story, with all its saved-value assertions."""
    no_start, full_gap, ninth, gap, shield = tags
    pages = {tag: served_player(story, tag, wednesday + timedelta(hours=2)) for tag in tags}
    days = {tag: day_at(page, tuesday) for tag, page in pages.items()}
    missing = days[no_start]
    assert missing["start_trophies"] is None
    assert missing["display_start"] == {
        "trophies": 5340, "source": "calculated",
        "calculation": {"end_trophies": 5420, "net_change": 80},
    }
    assert missing["net_trophy_change"] == 80
    assert missing["presentation"]["status"] == "calculated"
    # The worker proved all sixteen battles despite the older log gap:
    # the old website classifier verifies these exact, reason-free inputs.
    assert days[full_gap]["confidence"] == "exact"
    assert days[full_gap]["uncertainty_reasons"] == []
    assert days[full_gap]["presentation"]["status"] == "verified"
    assert days[ninth]["presentation"]["status"] == "uncertain"
    assert days[gap]["presentation"]["status"] == "uncertain"
    assert days[shield]["display_end"]["player_log"] == 5340
    stats = pages[no_start]["screen_ready"]["battle_statistics"]["7"]
    assert stats["attack"]["count"] == stats["defense"]["count"] == 8
    assert stats["attack"]["trophies"] == 320
    assert stats["defense"]["trophies"] == 240
    assert stats["attack"]["played_days"] == 1


def assert_weekly_facts(story, sunday, monday, raised, dropped, in_flight):
    page = served_player(story, raised, monday + timedelta(hours=2))
    ended = day_at(page, sunday)
    assert ended["start_trophies"] == ended["display_start"]["trophies"] == 5140
    assert ended["display_end"]["before_reset"] == 4940
    assert ended["display_end"]["after_reset"] == ended["display_end"]["player_log"] == 5000
    assert story.saved_day(raised, sunday)["end"] == 4940
    left = served_player(story, dropped, monday + timedelta(days=2))
    shape = left["screen_ready"]["season_shape"]
    assert shape["outside_now"] is True
    assert shape["last_legend_day_start"] == sunday.isoformat()
    assert day_at(left, sunday)["display_end"]["player_log"] == 4940
    assert left["screen_ready"]["battle_statistics"]["season"]["reset_day_start"] == sunday.isoformat()
    # The report at 05:04:30 is still Sunday's only attack in the served facts.
    late = served_player(story, in_flight, monday + timedelta(hours=2))
    assert late["screen_ready"]["battle_statistics"]["7"]["attack"]["count"] == 3
    assert day_at(late, sunday)["presentation"]["battle_net_complete"] is None


def assert_season_end_facts(story, tag, last, reset):
    page = served_player(story, tag, reset + timedelta(hours=8))
    ended = day_at(page, last)
    assert ended["display_end"]["before_reset"] == 5320
    # This publication has no explicit Season-reset adjustment. Preserve
    # dayEnd's disagreement instead of inventing one from the calendar.
    assert not any(item["type"] == "season_reset" for item in ended["adjustments"])
    assert ended["display_end"]["after_reset"] == 5320
    assert ended["display_end"]["player_log"] == 5000
    assert ended["display_end"]["conflict"] == {
        "battle_end": 5320, "reset_kind": None, "reset_amount": 0, "next_start": 5000,
    }
    assert story.saved_day(tag, last)["end"] == 5320
    api = ApiDatabase(as_api_role(story.connection_info))
    try:
        from clashlens.api_players import get_player_season_summary

        summary = get_player_season_summary(api, tag, str(int((reset - timedelta(days=28)).timestamp())))
        import json

        with_facts = len(json.dumps(summary, separators=(",", ":")).encode())
        without_facts = len(json.dumps({key: value for key, value in summary.items() if key != "summary_statistics"}, separators=(",", ":")).encode())
        print(f"historical summary bytes: {without_facts} -> {with_facts} (+{with_facts - without_facts})")
        assert summary["end_trophies"] == 5320
        assert summary["official_history"]["eod_trophies"] == 5320
        assert summary["season_shape"]["today"] == 29
        assert len(summary["season_shape"]["kinds"]) == 28
        assert summary["summary_statistics"] == {
            "attack": {"per_day": 100, "per_battle": 40},
            "defense": {"per_day": 240, "per_battle": 30},
        }
        assert summary["daily_entries"]
        assert all("presentation" in entry for entry in summary["daily_entries"])
    finally:
        api.close()
