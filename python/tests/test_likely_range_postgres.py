"""End to end: the dashboard's likely place at the next Reset, from saved
game data through the real worker to the Legend day card's API response.

A finished Legend day's Daily board publishes, the worker's maintenance saves
that day's examples, and the dashboard's board read on the next day imagines
the rest of today from them. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import as_api_role, domain_database
from test_reconciliation_postgres import _processor
from test_reset_scenarios_postgres import SEASON, Battle, _story, attacks, day, defenses

from clashlens import api_dashboard, likely_range, worker
from clashlens.api_db import ApiDatabase

DONE, CHASER = "#2PP", "#8PP"
EXAMPLE, TODAY = day(3), day(4)
# 20:00 UTC, the 61st 15-minute step of the day.
NOW = TODAY + timedelta(hours=15)


def _days_battles(start, *, late: bool) -> dict[str, list[Battle]]:
    """DONE plays all 16 battles by 13:00. CHASER defends 8 times by 12:30
    and attacks 6 times by 11:00, then, when ``late``, twice more at 22:00
    and 23:00."""
    return {
        DONE: attacks(start, *[40] * 8) + defenses(start, *[30] * 8),
        CHASER: attacks(start, *[40] * 6)
        + ([Battle(start + timedelta(hours=17), 40, True),
            Battle(start + timedelta(hours=18), 40, True)] if late else [])
        + defenses(start, *[30] * 8),
    }


def test_card_shows_a_likely_range_learned_from_the_day_before(
    database_url: str, archive_server, monkeypatch
) -> None:
    """At 20:00 DONE has finished on 5,460 and CHASER, 50 behind on 5,410,
    still has 2 attacks. The outer range lets either finish first. The day
    before, CHASER was in the same spot at 20:00 and won 80 more, so the
    likely range puts CHASER first and DONE second."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, DONE, CHASER)
        # Today's card is read at 20:00. The worker publishes a day's battles
        # after its Reset when it runs weeks later, as here, so today ends
        # at 20:00 and the next Reset publishes what was known then.
        for start, late in ((EXAMPLE, True), (TODAY, False)):
            for tag, battles in _days_battles(start, late=late).items():
                story.players[tag].battles += battles
        story.reset(day(2), {DONE: (5300, SEASON), CHASER: (5330, SEASON)})
        story.reset(EXAMPLE, {DONE: (5300, SEASON), CHASER: (5330, SEASON)})
        # The collector's checks at 19:00 and 04:50 read the day's battles.
        story.reads(EXAMPLE + timedelta(hours=14), {DONE: 5380, CHASER: 5330})
        story.reads(EXAMPLE + timedelta(hours=23, minutes=50),
                    {DONE: 5380, CHASER: 5410})
        story.run_worker()
        # The Reset publishes the example day's Daily board.
        story.reset(TODAY, {DONE: (5380, SEASON), CHASER: (5410, SEASON)})
        story.reads(TODAY + timedelta(hours=14, minutes=55),
                    {DONE: 5460, CHASER: 5410})
        story.reset(day(5), {DONE: (5460, SEASON), CHASER: (5410, SEASON)})
        with psycopg.connect(connection_info) as connection:
            # Production saves a battle a median 1.3-1.7 minutes after it
            # ends (12 October 2026); this worker ran weeks later.
            connection.execute(
                """
                UPDATE legend_battles AS battle SET created_at = seen.at
                FROM (
                    SELECT battle_id, min(battle_timestamp) + interval '2 minutes' AS at
                    FROM battle_evidence GROUP BY battle_id
                ) AS seen
                WHERE seen.battle_id = battle.id
                """
            )
            saved_before = connection.execute(
                "SELECT count(*) FROM dashboard_remaining_changes"
            ).fetchone()[0]
        # The real worker's maintenance, on a timer, saves the day.
        monkeypatch.setattr(likely_range, "_now", lambda: NOW)
        monkeypatch.setattr(likely_range, "_next_refresh_at", float("-inf"))
        database, _ = _processor(connection_info, archive_server)
        maintenance = worker.TimedMaintenance(database, worker.StageMetrics())
        try:
            maintenance.run_due()
        finally:
            maintenance.close()
            database.close()
        with psycopg.connect(connection_info) as connection:
            saved = connection.execute(
                """
                SELECT ranked_day_start, count(*), min(slot), max(slot)
                FROM dashboard_remaining_changes GROUP BY ranked_day_start
                """
            ).fetchall()
            evening = connection.execute(
                "SELECT changes FROM dashboard_remaining_changes WHERE slot = 60"
            ).fetchone()
        unfinished = story.unfinished_jobs()
        api = ApiDatabase(as_api_role(connection_info))
        try:
            board = api_dashboard.DashboardBoard()
            cards = {
                tag: api_dashboard.get_player_today(api, board, tag, now=NOW)
                for tag in (DONE, CHASER)
            }
        finally:
            api.close()

    assert unfinished == []
    assert saved_before == 0
    assert saved == [(EXAMPLE, 96, 0, 95)]
    # At 20:00 the day before, DONE had nothing left and CHASER won 80 more.
    assert evening == ([0, 80],)
    assert {tag: card["rank_range"] for tag, card in cards.items()} == {
        DONE: {"best": 1, "worst": 2}, CHASER: {"best": 1, "worst": 2},
    }
    assert {tag: card["likely_rank_range"] for tag, card in cards.items()} == {
        DONE: {"best": 2, "worst": 2}, CHASER: {"best": 1, "worst": 1},
    }
