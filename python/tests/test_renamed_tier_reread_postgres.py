"""The re-read of Reset readings judged before the renamed Legend tiers
were read, end to end: saved game data in, a corrected Daily board out.

Between 12:21 and 12:31 UTC on Monday 16 November 2026 the game renamed
Legend I "Legend League I". Until the fix deployed on 18 November at 10:03
UTC, every profile under the new name was kept uncertain, so the 17 and 18
November Reset pairs failed: the Day 15 board published 98 players proven of
12,044, Day 16 454 of 12,076. Those readings stay saved, judged by the old
reader. `./ops failed-items --reread-reset` re-reads them with the fixed one.
See docs/testing.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from domain_test_support import as_api_role, domain_database
from reset_scenarios_postgres_support import SEASON, Story, _even, _story, day

from clashlens import api_leaderboard, profile
from clashlens.api_db import ApiDatabase
from clashlens.cli import main

STEADY = "#9PP"


def test_reset_readings_kept_uncertain_before_the_fix_are_re_read(
    database_url: str, archive_server, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """STEADY plays an even day each day and stays at 5,100. From Monday
    afternoon every profile names "Legend League I", and the reader before
    the fix keeps each uncertain, so the Tuesday and Wednesday Reset pairs
    fail and both boards leave STEADY unproven. Once the fixed reader
    re-reads those two Reset readings, Monday and Tuesday save Complete and
    both boards rank STEADY proven at 5,100."""
    season = SEASON + timedelta(days=28)
    sunday, monday, tuesday, wednesday = (day(n, season) for n in (14, 15, 16, 17))
    fixed_reader = profile.RENAMED_LEGEND_TIERS
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, STEADY)
        player = story.players[STEADY]
        player.battles = _even(sunday) + _even(monday) + _even(tuesday)
        story.reset(sunday, {STEADY: (5100, season)})
        story.reset(monday, {STEADY: (5100, season)})
        # The game renames the tier at 12:21; the reader then in production
        # keeps every such profile uncertain.
        monkeypatch.setattr(profile, "RENAMED_LEGEND_TIERS", {})
        player.renamed = True
        for start in (monday, tuesday):
            story.reads(start + timedelta(hours=7, minutes=30), {STEADY: 5100}, season)
            story.reads(start + timedelta(hours=23, minutes=50), {STEADY: 5100}, season)
            story.reset(start + timedelta(days=1), {STEADY: (5100, season)})
            story.reads(start + timedelta(days=1, minutes=20), {STEADY: 5100}, season)
        story.wait(timedelta(hours=1))
        before = _shown(story, season, monday, tuesday)

        # The fix deploys, and an operator re-reads the two Resets' readings.
        monkeypatch.setattr(profile, "RENAMED_LEGEND_TIERS", fixed_reader)
        command = [
            "failed-items", "--database-url", info, "--operator", "ops:zubair",
            "--reread-reset", tuesday.isoformat(), "--reread-reset", wednesday.isoformat(),
            "--reason", "re-read Reset profiles kept uncertain before the tier rename fix",
        ]
        capsys.readouterr()
        assert main(command) == 0
        preview = json.loads(capsys.readouterr().out)
        assert main([*command, "--apply"]) == 0
        applied = json.loads(capsys.readouterr().out)
        story.run_worker()
        capsys.readouterr()
        # Run again, it finds nothing left to re-read.
        assert main([*command, "--apply"]) == 0
        again = json.loads(capsys.readouterr().out)
        # Older Resets' boards rebuild 6 hours after their last build.
        story.wait(timedelta(hours=6))
        unfinished = story.unfinished_jobs()
        after = _shown(story, season, monday, tuesday)

    # Each day saved, then its board as the API serves it: (tag, trophies,
    # confidence) for each entry.
    assert before == {
        monday: ("Partial", [(STEADY, 5100, "uncertain")]),
        tuesday: ("Partial", [(STEADY, 5100, "uncertain")]),
    }
    assert [(entry["readings"], entry["would_enqueue"]) for entry in preview["resets"]] == [
        (1, 1), (1, 1)]
    assert (applied["outcome"], applied["replayed_count"]) == ("replay_enqueued", 2)
    assert [entry["readings"] for entry in again["resets"]] == [0, 0]
    assert unfinished == []
    assert after == {
        monday: ("Complete", [(STEADY, 5100, "confirmed")]),
        tuesday: ("Complete", [(STEADY, 5100, "confirmed")]),
    }


def _shown(story: Story, season: datetime, *days: datetime) -> dict[datetime, tuple]:
    """Each Legend day's saved state and its Daily board's entries as the API
    serves them: (tag, trophies, confidence)."""
    api = ApiDatabase(as_api_role(story.connection_info))
    try:
        return {
            start: (story.saved_day(STEADY, start)["state"], [
                (entry["tag"], entry["trophies"], entry["confidence"])
                for entry in api_leaderboard.get_frozen_leaderboard(
                    api, limit=100, offset=0,
                    official_season_id=str(int(season.timestamp())),
                    season_day_number=(start - season).days + 1,
                )["entries"]
            ])
            for start in days
        }
    finally:
        api.close()
