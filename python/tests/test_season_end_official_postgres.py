"""End-to-end: the Season's last board shows the game's official final.

On the 2 November 2026 Season end, 17 players' Day 28 entries kept a profile
reading 1 to 168 trophies above their saved official final, because their
days were Inconsistent (14) or Partial (3) and only a Complete day's total
could stand in for the reading. Their saved days already held the official
final. The board now shows it whatever the day's state, and the board
correction (``republish-current-season --boards``) republishes a board
published under the old rule. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import domain_database, text
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    SEASON,
    _ranked,
    _saturday,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import boundary, boundary_manifest, reconcile_pass
from clashlens.db import Database

# COMPLETE plays both days and agrees with the game. INCONSISTENT, as
# #QLQ0G0YG, plays neither day and sits at 5,262, but the game's final is
# 5,261. PARTIAL, as #YL9QC90L, is first read on Day 28, after its 6
# attacks and 5 defenses: no reading starts the day.
COMPLETE, INCONSISTENT, PARTIAL = "#2PP", "#8PP", "#9PP"


def _old_rule(reading, ranked_day, season_end, *rest):
    """The rule before this fix, for these players: only a Complete day used
    the official final, and only as proof of its own end. A Complete day
    with an official final always ends at it, as one that does not is saved
    Inconsistent, so the stand-in leaves Complete days to the new rule."""
    if ranked_day is not None and text(ranked_day[0]) != "Complete":
        ranked_day = (*ranked_day[:-1], None)
    return _new_rule(reading, ranked_day, season_end, *rest)


_new_rule = boundary_manifest._reset_total


def _days(connection_info: str, start) -> dict[str, tuple]:
    """Each player's newest saved day from ``start``: state, failure reasons,
    end and next start."""
    with psycopg.connect(connection_info) as connection:
        return {
            text(tag): (text(state), sorted(reasons), end, next_start)
            for tag, state, reasons, end, next_start in connection.execute(
                """
                SELECT DISTINCT ON (day.player_id) player.normalized_tag, day.state,
                       day.failure_reasons, day.final_trophies_before_reset,
                       day.next_start_trophies
                FROM ranked_day_versions AS day
                JOIN players AS player ON player.id = day.player_id
                WHERE day.ranked_day_start = %s
                ORDER BY day.player_id, day.version DESC
                """,
                (start,),
            ).fetchall()
        }


def _entries(connection_info: str, snapshot_id: int) -> dict[str, tuple[int, str]]:
    with psycopg.connect(connection_info) as connection:
        return {
            text(tag): (trophies, text(confidence))
            for tag, trophies, confidence in connection.execute(
                """
                SELECT player.normalized_tag, entry.trophies, entry.confidence
                FROM leaderboard_snapshot_entries AS entry
                JOIN players AS player ON player.id = entry.player_id
                WHERE entry.snapshot_id = %s
                """,
                (snapshot_id,),
            ).fetchall()
        }


def test_day_28_board_shows_the_saved_official_final(
    database_url: str, archive_server, monkeypatch
) -> None:
    day_27, day_28, day_1 = day(27, PREVIOUS_SEASON), day(28, PREVIOUS_SEASON), day(1)
    old, new = PREVIOUS_SEASON, SEASON
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       COMPLETE, INCONSISTENT, PARTIAL)
        # COMPLETE: 5,600, then 5,440 after Day 27 and 5,320 after Day 28's 3
        # attacks of 40 and 8 defenses of 30. PARTIAL: 6 attacks of 40 and 5
        # defenses of 30 on Day 28, then 3 * 30 automatic loss.
        story.players[COMPLETE].battles = _saturday(day_27) + attacks(
            day_28, 40, 40, 40) + defenses(day_28, *[30] * 8)
        story.players[PARTIAL].battles = attacks(day_28, *[40] * 6) + defenses(
            day_28, *[30] * 5)
        story.reset(day_27, {COMPLETE: (5600, old), INCONSISTENT: (5262, old)})
        story.reset(day_28, {COMPLETE: (5440, old), INCONSISTENT: (5262, old)})
        story.read(PARTIAL, day_28 + timedelta(hours=8), 5044, old)
        story.read(PARTIAL, day_28 + timedelta(hours=8, minutes=1))
        story.reads(day_1 - timedelta(minutes=10),
                    {COMPLETE: 5320, INCONSISTENT: 5262, PARTIAL: 5044}, old)
        # The game's finals arrive 34 minutes after the Reset, before the
        # board's first build, as on 2 November 2026.
        opening = {tag: (5000, new) for tag in story.players}
        story.reset(day_1, opening, run=False)
        finals = {COMPLETE: 5320, INCONSISTENT: 5261, PARTIAL: 5044 - 90}
        story.history(day_1 + timedelta(minutes=34), day_1, finals)
        # The board is built and rebuilt under the old rule.
        monkeypatch.setattr(boundary_manifest, "_reset_total", _old_rule)
        story.run_worker()
        story.reads(day_1 + timedelta(minutes=20),
                    {tag: trophies for tag, (trophies, _) in opening.items()})
        story.wait(timedelta(hours=1))
        before_days = _days(connection_info, day_28)
        before_board = story.served_board(day_1)
        old_snapshot = int(before_board["snapshot_id"])
        old_entries = _entries(connection_info, old_snapshot)

        # The fix is deployed and the board correction runs for that Season.
        monkeypatch.setattr(boundary_manifest, "_reset_total", _new_rule)
        season_id = str(int(old.timestamp()))
        database = Database(connection_info)
        try:
            queued = boundary.queue_board_rebuilds(database, season_id, queue=True)
            story.wait(timedelta(hours=1))
            again = boundary.queue_board_rebuilds(database, season_id, queue=False)
        finally:
            database.close()
        after_days = _days(connection_info, day_28)
        after_board = story.served_board(day_1)
        with psycopg.connect(connection_info) as connection:
            players = [int(row[0]) for row in connection.execute("SELECT id FROM players")]
            board_checks = [finding for finding in reconcile_pass.judge(
                connection, day_28, players)[0] if finding.check == "board"]
        kept_entries = _entries(connection_info, old_snapshot)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    # The saved days are production's: each holds the official final as its
    # next start, and two are not Complete.
    assert before_days[COMPLETE] == ("Complete", [], 5320, 5320)
    assert before_days[INCONSISTENT][0] == "Inconsistent"
    assert before_days[INCONSISTENT][3] == 5261
    assert before_days[PARTIAL][0] == "Partial"
    assert "missing_start_baseline" in before_days[PARTIAL][1]
    assert before_days[PARTIAL][3] == 4954
    # Under the old rule the board kept their readings, uncertain.
    assert _ranked(before_board) == {COMPLETE: 5320, INCONSISTENT: 5262, PARTIAL: 5044}
    assert old_entries == {COMPLETE: (5320, "confirmed"), INCONSISTENT: (5262, "uncertain"),
                           PARTIAL: (5044, "uncertain")}
    # The correction rebuilds only the Season's last board.
    assert [board["boundary_at"] for board in queued["boards"]] == [day_1.isoformat()]
    assert queued["boards"][0]["late_battles"] == 2
    assert again["boards"] == []
    # It shows every official final, proven; the days stay as they were.
    assert _ranked(after_board) == finals
    assert {entry["tag"]: entry["confidence"] for entry in after_board["entries"]} == {
        tag: "confirmed" for tag in finals}
    assert int(after_board["snapshot_id"]) != old_snapshot
    assert after_days == before_days
    # The board it replaced keeps its entries, and the nightly re-check agrees.
    assert kept_entries == old_entries
    assert board_checks == []
