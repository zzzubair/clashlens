"""A battle log repeats most of its rows from the last one. Saving it must
look up only the reports it has not saved before, and save exactly what a
full lookup would: the same battles, reports, day results and board.

From 05:00 to 05:45 on 12 and 13 November 2026, saving a battle log's
battles took 41% of each log's time on production, and looking up every
row's report by its fingerprint was its largest cost.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, text
from reset_scenarios_postgres_support import (
    SEASON,
    _even,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

STEADY, CLIMBER = "#9PP", "#2PP"


def _fingerprint_lookups(connection_info: str) -> int:
    """Index reads of saved reports by fingerprint, once every worker
    connection has closed and so has reported its counts."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        for _ in range(100):
            if not connection.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            ).fetchone()[0]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("worker connections are still open")
        return int(connection.execute(
            "SELECT idx_scan FROM pg_stat_user_indexes"
            " WHERE indexrelname = 'battle_source_rows_report_hash'"
        ).fetchone()[0])


def test_a_repeated_battle_log_looks_up_only_its_new_reports(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Two players are read through their Legend day as the collector reads
    them: a log every few hours, each repeating every earlier row, and once
    the same log twice. The Reset sweep reads them again. Every log saves
    the rows it shows, both days save Complete, and the board publishes.
    Each report is looked up by its fingerprint only when it is new: at
    most twice, once to save it and once to list it."""
    wednesday, thursday = day(10), day(11)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, STEADY, CLIMBER)
        story.players[STEADY].battles = _even(wednesday)
        story.players[CLIMBER].battles = (
            attacks(wednesday, *[40] * 8) + defenses(wednesday, *[30] * 8)
        )
        before = {STEADY: (5200, SEASON), CLIMBER: (5100, SEASON)}
        story.reset(wednesday, before)
        # The 02:10 and 02:15 logs show the same rows: no battle between.
        for hours, minutes in ((2, 10), (2, 15), (6, 0), (12, 0)):
            for tag in story.players:
                story.read(tag, wednesday + timedelta(hours=hours, minutes=minutes))
            story.run_worker()
        story.reset(thursday, {STEADY: (5200, SEASON), CLIMBER: (5180, SEASON)})
        unfinished = story.unfinished_jobs()
        days = {tag: story.saved_day(tag, wednesday) for tag in story.players}
        board = _ranked(story.served_board())
        lookups = _fingerprint_lookups(connection_info)
        with psycopg.connect(connection_info) as connection:
            logs = connection.execute(
                """
                SELECT player.normalized_tag, log.observed_at,
                       array_agg(source.source_json ORDER BY source.source_row_index)
                FROM battle_log_observations AS log
                JOIN players AS player ON player.id = log.player_id
                JOIN battle_log_observation_source_rows AS source
                  ON source.battle_log_observation_id = log.id
                GROUP BY log.id, player.normalized_tag
                ORDER BY log.observed_at, player.normalized_tag
                """
            ).fetchall()
            saved = connection.execute(
                """
                SELECT (SELECT count(*) FROM battle_source_rows),
                       (SELECT count(*) FROM legend_battles),
                       (SELECT count(*) FROM battle_evidence),
                       (SELECT count(*) FROM battle_perspectives),
                       (SELECT count(*) FROM battle_army_decodes)
                """
            ).fetchone()

    assert unfinished == []
    assert days == {
        STEADY: {
            "state": "Complete", "start": 5200, "end": 5200, "next_start": 5200,
            "automatic_loss": None, "raise": 0, "attacks": 6, "defenses": 8,
            "residual": 0,
        },
        CLIMBER: {
            "state": "Complete", "start": 5100, "end": 5180, "next_start": 5180,
            "automatic_loss": None, "raise": 0, "attacks": 8, "defenses": 8,
            "residual": 0,
        },
    }
    assert board == {STEADY: 5200, CLIMBER: 5180}
    # Every saved log lists exactly the rows the game showed, in its order.
    # Each player's log at the first Reset showed no battle yet.
    assert len(logs) == 2 * 5
    for tag, observed_at, rows in logs:
        shown = json.loads(story.players[text(tag)].log(observed_at))["items"]
        assert rows == shown, (tag, observed_at)
    # 14 and 16 battles, each reported once, by the player who fought it,
    # and each army saved once.
    assert saved == (30, 30, 30, 30, 30)
    # The 10 logs list 98 rows. Each of the 30 reports is looked up at most
    # twice, when first saved and listed; a repeated row is not looked up.
    assert sum(len(rows) for _, _, rows in logs) == 98
    assert lookups <= 2 * 30
