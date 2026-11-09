"""A malformed battle date must not prevent a saved log from reaching its board."""

from __future__ import annotations

import json

import psycopg
import pytest
from domain_test_support import domain_database
from reset_scenarios_postgres_support import SEASON, _even, _story, day


@pytest.mark.parametrize(
    "unusable",
    [
        {"battleTimestamp": "00010101T000000.000Z"},
        # Before year 1 once moved to UTC.
        {"battleTimestamp": "0001-01-01T00:00:00+01:00"},
        # Past Python's integer digit limit.
        {"battleTimestamp": None, "battleTime": "9" * 4301},
    ],
)
def test_unusable_battle_date_keeps_valid_battles_and_publishes_board(
    database_url: str, archive_server, monkeypatch, unusable: dict,
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
                row = {**payload["items"][0], **unusable}
                payload["items"].append(
                    {key: value for key, value in row.items() if value is not None}
                )
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
