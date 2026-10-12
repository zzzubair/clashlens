"""The Daily board's number is the trophies at the Reset before the automatic
defense loss, so a reading that matched it proves it while the loss is still
calculated."""

from __future__ import annotations

import json

import psycopg
from domain_test_support import domain_database
from test_boundary_manifest_postgres import (
    _build_board,
    _october,
    _seed_board,
    _seed_days,
)

from clashlens.db import Database


def test_a_clean_reading_before_the_loss_proves_the_board_entry(database_url: str) -> None:
    # Both days end at their Reset before a calculated 70 loss, read before it
    # landed. #2PP's end reading showed every battle landed and none in
    # flight; #8QV's had one in flight, read the one way that fits.
    readings = [
        ("#2PP", 5940, _october(7, 4, 40)),
        ("#8QV", 5930, _october(7, 4, 40)),
    ]
    days = {player: (True, [("offense", 40, _october(7, 3), True)]) for player in (1, 2)}
    complete = {"state": "Complete", "failure_reasons": [], "automatic_loss": 70,
                "confidence": "inferred"}
    results = {
        1: {**complete, "final": 5870, "start": 5900, "end": 5940},
        2: {**complete, "final": 5860, "start": 5890, "end": 5930},
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days, results)
        with psycopg.connect(connection_info) as connection:
            for player, clean in ((1, True), (2, False)):
                connection.execute(
                    "UPDATE ranked_day_versions SET input_evidence = input_evidence || %s"
                    " WHERE player_id = %s",
                    (json.dumps({"end_reading": {"outcome": "verified", "clean": clean}}),
                     player),
                )
        database = Database(connection_info)
        try:
            board = _build_board(connection_info, database, generation_id)
        finally:
            database.close()

    assert board == [("#2PP", 5940, "confirmed"), ("#8QV", 5930, "uncertain")]
