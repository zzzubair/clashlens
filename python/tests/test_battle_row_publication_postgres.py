"""A malformed battle date must not prevent a saved log from reaching its board."""

from __future__ import annotations

import json

import psycopg
from domain_test_support import domain_database
from test_reset_scenarios_postgres import SEASON, _even, _story, day


def test_unusable_battle_date_keeps_valid_battles_and_publishes_board(
    database_url: str, archive_server, monkeypatch,
) -> None:
    tag = "#2PP"
    start, reset = day(2), day(3)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, tag)
        player = story.players[tag]
        player.battles = _even(start)
        original_log = player.log

        def with_unusable_date(at):
            payload = json.loads(original_log(at))
            if payload["items"]:
                payload["items"].append({
                    **payload["items"][0], "battleTimestamp": "00010101T000000.000Z",
                })
            return json.dumps(payload).encode()

        monkeypatch.setattr(player, "log", with_unusable_date)
        story.reset(start, {tag: (5000, SEASON)})
        story.reset(reset, {tag: (5000, SEASON)})
        saved = story.saved_day(tag, start)
        assert story.unfinished_jobs() == []
        assert saved is not None
        assert (saved["state"], saved["attacks"], saved["defenses"]) == ("Malformed", 6, 8)
        assert story.board_members(reset) == {tag: "malformed"}
        board = story.served_board(reset)
        assert board is not None
        assert [(entry["tag"], entry["trophies"]) for entry in board["entries"]] == [(tag, 5000)]
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT count(*) FROM battle_log_observation_source_rows"
                " WHERE outcome = 'malformed_legend_row'"
                " AND failure_category = 'invalid_battle_timestamp'",
            ).fetchone()[0] == 1
