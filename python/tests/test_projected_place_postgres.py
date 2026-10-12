"""End to end: the dashboard's projected place at the next Reset, from saved
game data through the real worker to the Legend day card's API response.

A finished Legend day's Daily board publishes, the worker's maintenance saves
that day's examples, and the dashboard's board read on the next day imagines
the rest of today from them. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import as_api_role, domain_database
from test_dashboard_bases_postgres import Facing
from test_reconciliation_postgres import _processor
from test_reset_scenarios_postgres import SEASON, Battle, _story, attacks, day, defenses

from clashlens import api_dashboard, projected_place, worker
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


def _cards(connection_info: str, *tags: str) -> dict[str, dict]:
    """Each player's Legend day card read at 20:00 today, from a new board."""
    api = ApiDatabase(as_api_role(connection_info))
    try:
        board = api_dashboard.DashboardBoard()
        return {
            tag: api_dashboard.get_player_today(api, board, tag, now=NOW)
            for tag in tags
        }
    finally:
        api.close()


def _save_examples(connection_info: str, archive_server, monkeypatch) -> None:
    """The real worker's maintenance, on a timer at 20:00 today, saves the
    finished days' examples."""
    with psycopg.connect(connection_info) as connection:
        # Production saves a battle a median 1.3-1.7 minutes after it ends
        # (12 October 2026); this worker ran weeks later.
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
    monkeypatch.setattr(projected_place, "_now", lambda: NOW)
    monkeypatch.setattr(projected_place, "_next_refresh_at", float("-inf"))
    database, _ = _processor(connection_info, archive_server)
    maintenance = worker.TimedMaintenance(database, worker.StageMetrics())
    try:
        maintenance.run_due()
    finally:
        maintenance.close()
        database.close()


def test_card_shows_a_projected_place_learned_from_the_day_before(
    database_url: str, archive_server, monkeypatch
) -> None:
    """At 20:00 DONE has finished on 5,460 and CHASER, 50 behind on 5,410,
    still has 2 attacks. The outer range lets either finish first. The day
    before, CHASER was in the same spot at 20:00 and won 80 more, so the
    projected place puts CHASER first and DONE second. Before any day's
    examples are saved the card has no projected place; the worker's first
    run saves every finished day with a Daily board at once."""
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
        hidden = _cards(connection_info, DONE, CHASER)
        with psycopg.connect(connection_info) as connection:
            saved_before = connection.execute(
                "SELECT count(*) FROM dashboard_remaining_changes"
            ).fetchone()[0]
        _save_examples(connection_info, archive_server, monkeypatch)
        with psycopg.connect(connection_info) as connection:
            boards = [row[0] for row in connection.execute(
                """
                SELECT DISTINCT boundary_at - interval '1 day'
                FROM leaderboard_snapshots
                WHERE snapshot_kind = 'frozen' AND state = 'published'
                  AND boundary_at <= %s
                ORDER BY 1
                """,
                (TODAY,),
            ).fetchall()]
            saved = connection.execute(
                """
                SELECT ranked_day_start, count(*), min(slot), max(slot)
                FROM dashboard_remaining_changes
                GROUP BY ranked_day_start ORDER BY ranked_day_start
                """
            ).fetchall()
            evening = connection.execute(
                """
                SELECT changes, moved FROM dashboard_remaining_changes
                WHERE ranked_day_start = %s AND slot = 60
                """,
                (EXAMPLE,),
            ).fetchone()
        unfinished = story.unfinished_jobs()
        cards = _cards(connection_info, DONE, CHASER)

    assert unfinished == []
    assert saved_before == 0
    assert {tag: card["projected_rank"] for tag, card in hidden.items()} == {
        DONE: None, CHASER: None,
    }
    # The days before today with a published Daily board, saved in one run.
    assert len(boards) > 1 and boards[-1] == EXAMPLE
    assert saved == [(start, 96, 0, 95) for start in boards]
    # At 20:00 the day before, DONE had nothing left and CHASER won 80 more.
    assert evening == ([0, 80], [False, True])
    assert {tag: card["rank_range"] for tag, card in cards.items()} == {
        DONE: {"best": 1, "worst": 2}, CHASER: {"best": 1, "worst": 2},
    }
    assert {tag: card["projected_rank"] for tag, card in cards.items()} == {
        DONE: 2, CHASER: 1,
    }


# Three players finish today on the same trophies; RAIDER attacks HIT.
DEFENDED, ATTACKED, HIT, RAIDER = "#2PY", "#8PY", "#9PY", "#QPY"
HIT_AT = TODAY + timedelta(hours=7, minutes=59)


def test_equal_trophies_keep_the_daily_boards_order_by_when_battles_ended(
    database_url: str, archive_server, monkeypatch
) -> None:
    """DEFENDED, ATTACKED and HIT have all finished on 5,360. Their last
    trophy changes: ATTACKED's attack at 13:00; DEFENDED's defense reported
    at 12:58 by an attacker with no report, so it ended 155 seconds later,
    at 13:00:35; HIT's defense reported at 12:59 by RAIDER, whose own report
    says it ended at 12:59:30. The Daily board puts the latest end first, so
    the projected places are DEFENDED, ATTACKED, HIT: not the order of the
    players' own report times, nor of every defense ending 155 seconds after
    its report."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch)
        early = TODAY - timedelta(minutes=45)
        story.players = {
            DEFENDED: Facing(DEFENDED, attacks(early, *[40] * 8)
                             + defenses(TODAY, *[30] * 7)
                             + [Battle(TODAY + timedelta(hours=7, minutes=58), -30, False)]),
            ATTACKED: Facing(ATTACKED, attacks(TODAY, *[40] * 8)
                             + defenses(TODAY, *[30] * 8)),
            HIT: Facing(HIT, attacks(early, *[40] * 8) + defenses(TODAY, *[30] * 7)
                        + [Battle(HIT_AT, -30, False)], faced={HIT_AT: RAIDER}),
            RAIDER: Facing(RAIDER, attacks(early, *[40] * 7)
                           + [Battle(HIT_AT + timedelta(seconds=30), 30, True)]
                           + defenses(TODAY, *[30] * 8),
                           faced={HIT_AT + timedelta(seconds=30): HIT}),
        }
        # Each played the same 16 battles the day before, finished by 13:00.
        for player in story.players.values():
            player.battles += attacks(EXAMPLE, *[40] * 8) + defenses(EXAMPLE, *[30] * 8)
        tags = list(story.players)
        story.reset(day(2), {tag: (5200, SEASON) for tag in tags})
        story.reset(EXAMPLE, {tag: (5200, SEASON) for tag in tags})
        # The Reset publishes the example day's Daily board.
        story.reset(TODAY, {tag: (5280, SEASON) for tag in tags})
        finals = {DEFENDED: 5360, ATTACKED: 5360, HIT: 5360, RAIDER: 5350}
        story.reads(TODAY + timedelta(hours=14, minutes=55), finals)
        story.reset(day(5), {tag: (trophies, SEASON) for tag, trophies in finals.items()})
        _save_examples(connection_info, archive_server, monkeypatch)
        unfinished = story.unfinished_jobs()
        cards = _cards(connection_info, *tags)

    assert unfinished == []
    assert {tag: card["rank_range"] for tag, card in cards.items()} == {
        DEFENDED: {"best": 1, "worst": 3}, ATTACKED: {"best": 1, "worst": 3},
        HIT: {"best": 1, "worst": 3}, RAIDER: {"best": 4, "worst": 4},
    }
    assert {tag: card["projected_rank"] for tag, card in cards.items()} == {
        DEFENDED: 1, ATTACKED: 2, HIT: 3, RAIDER: 4,
    }
