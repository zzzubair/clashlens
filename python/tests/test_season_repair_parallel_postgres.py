"""End-to-end: Season repair jobs for different players run side by side.

On 13 October 2026 the Season repair recalculated about 35 players a
minute. Each job recalculated one player's days of the Season in one
transaction and took each changed day's Reset lock as it went, holding it
until the job ended; any other repair job reaching that Reset gave up after
250 ms. In 30 minutes 4,561 of 5,618 tries (81%) gave up, and about 1 of the
6 background slots ran. See docs/testing.md.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta

import psycopg
import test_reset_scenarios_postgres as scenarios
from domain_test_support import domain_database
from test_reconciliation_postgres import _processor
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    _ranked,
    _saturday,
    _story,
    attacks,
    day,
    defenses,
)
from test_weekly_raise_postgres import LATE_RAISE, LOSS_TOO, _instant_raise

from clashlens import reading_rule, reconciliation, reconciliation_db
from clashlens.db import Database
from clashlens.domain_repair import season_repair


def test_season_repair_jobs_for_two_players_finish_side_by_side(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Two players' Sunday and Monday saved as on 12 October 2026, before
    the weekly raise fix, as in test_weekly_raise_postgres. The Season
    repair queues one job each. LATE_RAISE's job stops after recalculating
    its Sunday, which changes the Monday board; LOSS_TOO's job runs to the
    end meanwhile instead of giving up on the Monday Reset. Both players'
    days and the Monday and Tuesday boards come out as the repair run one
    job at a time gives them."""
    season = PREVIOUS_SEASON
    saturday, sunday, monday, tuesday = (day(number, season) for number in (20, 21, 22, 23))
    players = (LATE_RAISE, LOSS_TOO)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *players)
        monkeypatch.setattr(scenarios, "READ_AFTER", timedelta(minutes=13))
        judge, settled_start = reading_rule.judge, reconciliation._settled_start
        monkeypatch.setattr(reading_rule, "judge", _instant_raise(judge))
        monkeypatch.setattr(reconciliation, "_settled_start",
                            lambda data: settled_start(replace(data, season_first_day=True)))
        for tag, sunday_defenses in ((LATE_RAISE, 8), (LOSS_TOO, 7)):
            story.players[tag].battles = (
                _saturday(saturday) + attacks(sunday, 40) + defenses(sunday, *[30] * sunday_defenses)
                + attacks(monday, 40) + defenses(monday, *[30] * 8))
        story.reset(saturday, dict.fromkeys(players, (5300, season)))
        story.reset(sunday, dict.fromkeys(players, (5140, season)))
        story.reads(monday - timedelta(minutes=10), {LATE_RAISE: 4940, LOSS_TOO: 4970}, season)
        story.reset(monday, {LATE_RAISE: (4940, season), LOSS_TOO: (4970, season)})
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(players, 5000), season)
        story.wait(timedelta(hours=1))
        story.reads(tuesday - timedelta(minutes=10), dict.fromkeys(players, 4800), season)
        story.reset(tuesday, dict.fromkeys(players, (4800, season)))
        assert story.saved_day(LATE_RAISE, sunday)["state"] == "Inconsistent"

        monkeypatch.setattr(reading_rule, "judge", judge)
        monkeypatch.setattr(reconciliation, "_settled_start", settled_start)
        with psycopg.connect(connection_info) as connection:
            first, second = (
                connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
                ).fetchone()[0]
                for tag in players
            )
        recalculate = reconciliation_db.recalculate_ranked_day
        stopped, go_on = threading.Event(), threading.Event()

        def stopping_after_sunday(database, connection, **day_input):
            changed = recalculate(database, connection, **day_input)
            if day_input["player_id"] == first and day_input["day_start"] == sunday:
                stopped.set()
                go_on.wait(60)
            return changed

        monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", stopping_after_sunday)
        database = Database(connection_info)
        (first_database, first_lane), (second_database, second_lane) = (
            _processor(connection_info, archive_server) for _ in range(2))
        try:
            def repair(action: str) -> dict:
                return season_repair(database, str(int(season.timestamp())), action, max_jobs=100)

            repair("preview")
            assert repair("queue")["queued"] == 2
            with database.pool.connection() as connection:
                # The second job waits until the first has stopped mid-way.
                connection.execute(
                    "UPDATE python_processing_jobs SET due_at = now() + interval '1 day'"
                    " WHERE deduplication_key LIKE 'reconcile:season-repair:%%'"
                    "   AND (input_json ->> 'player_id')::bigint = %s",
                    (second,),
                )
            results = {}
            lane = threading.Thread(target=lambda: results.update(
                first=first_lane.process_once(owner="lane-1", lease_seconds=120)))
            lane.start()
            assert stopped.wait(60)
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET due_at = now()"
                    " WHERE deduplication_key LIKE 'reconcile:season-repair:%%'"
                    "   AND (input_json ->> 'player_id')::bigint = %s",
                    (second,),
                )
            results["second"] = second_lane.process_once(owner="lane-2", lease_seconds=120)
            go_on.set()
            lane.join(60)
            phases = []
            for _ in range(10):
                phases.append(repair("queue")["phase"])
                if phases[-1] == "done":
                    break
                story.wait(timedelta(hours=1))
            receipt = repair("receipt")
        finally:
            go_on.set()
            for opened in (database, first_database, second_database):
                opened.close()
        sundays = {tag: story.saved_day(tag, sunday) for tag in players}
        mondays = {tag: story.saved_day(tag, monday) for tag in players}
        monday_board = story.served_board(monday)
        tuesday_board = story.served_board(tuesday)
        unfinished = story.unfinished_jobs()

    # Before the fix the second job gave up on the Monday Reset the first
    # held: ("retrying", "database_lock_busy").
    assert (results["second"].outcome, results["second"].category) == ("processed", None)
    assert (results["first"].outcome, results["first"].category) == ("processed", None)
    assert phases[-1] == "done"
    assert unfinished == []
    sunday_day = {
        "state": "Complete", "start": 5140, "end": 4940, "next_start": 5000,
        "automatic_loss": None, "raise": 60, "attacks": 1, "defenses": 8, "residual": 0,
    }
    assert sundays == {
        LATE_RAISE: sunday_day, LOSS_TOO: {**sunday_day, "automatic_loss": 30, "defenses": 7}}
    monday_day = {
        "state": "Complete", "start": 5000, "end": 4800, "next_start": 4800,
        "automatic_loss": None, "raise": 0, "attacks": 1, "defenses": 8, "residual": 0,
    }
    assert mondays == {LATE_RAISE: monday_day, LOSS_TOO: monday_day}
    assert _ranked(monday_board) == {LATE_RAISE: 4940, LOSS_TOO: 4970}
    assert {entry["tag"]: entry["confidence"] for entry in monday_board["entries"]} == {
        LATE_RAISE: "confirmed", LOSS_TOO: "confirmed"}
    assert _ranked(tuesday_board) == {LATE_RAISE: 4800, LOSS_TOO: 4800}
    assert receipt["boards_disagreeing"] == []
