"""A Daily board entry is proven only by its day's Reset readings, never by
a profile reading plus the battles stamped after it."""

from __future__ import annotations

from datetime import UTC, datetime

from domain_test_support import domain_database
from test_boundary_manifest_postgres import (
    DAY_2_RESET,
    _build_board,
    _october,
    _seed_board,
    _seed_days,
)

from clashlens import boundary
from clashlens.db import Database
from clashlens.domain import RANKED_DAY_DURATION, ranked_day_for


def test_board_proves_a_reading_only_by_the_days_reset_readings(
    database_url: str,
) -> None:
    """#2QCYU8C2G read 4,703 at 04:37:05 on 7 October 2026 without its attack
    stamped 04:34:08 for 29, and the board showed 4,902 as proven: a reading
    proves no battle stamped before it. Its Complete day ends at 4,931 from
    its Reset readings at both ends. Without that, a reading plus the
    battles after it is proven only when the day's proven start plus all
    its battles comes to it too. Without a start, an end Reset reading
    agreeing with it, with or without a known automatic loss, proves
    nothing: both readings can miss the same delayed credit. A Reset that
    resets trophies, a Season's end or a Monday's raise to 5,000, proves
    nothing either."""
    readings = [
        ("#2QCYU8C2G", 4703, datetime(2026, 10, 7, 4, 37, 5, tzinfo=UTC)),
        ("#GURYYP99", 4923, _october(7, 4, 54)),  # no end reading
        ("#PL0Q0UVLC", 5100, _october(7, 4, 40)),  # no start, end reading agrees
        ("#P0VPRVPJJ", 5090, _october(7, 4, 40)),  # no start, less the known loss
        ("#P2CC9URVR", 5080, _october(7, 4, 40)),  # less an unknown loss
        ("#Y8V9YYP9C", 5151, _october(7, 4, 40)),  # Season reset, disagrees
        ("#YPG0UY9LU", 5047, _october(7, 4, 40)),  # Season reset, agrees
        ("#QQC2GRQU", 4940, _october(7, 4, 40)),  # weekly raise, agrees
        ("#LJQCVPVPL", 4950, _october(7, 4, 40)),  # weekly raise, disagrees
    ]
    days = {
        1: (True, [
            ("defense", 209, _october(7, 3), True),
            ("offense", 29, datetime(2026, 10, 7, 4, 34, 8, tzinfo=UTC), True),
            ("offense", 199, _october(7, 4, 50), True),
        ]),
        2: (True, [
            ("offense", 40, datetime(2026, 10, 7, 4, 52, 3, tzinfo=UTC), True),
            ("offense", 69, _october(7, 4, 58), True),
        ]),
        **{player: (True, [("offense", 40, _october(7, 4, 50), True)])
           for player in range(3, 10)},
    }
    no_start = {"failure_reasons": ["missing_start_baseline"], "start": None}
    complete = {"state": "Complete", "failure_reasons": []}
    results = {
        1: {**complete, "final": 4931, "start": 4912, "end": 4931},
        2: {"start": 4923},
        3: {**no_start, "end": 5140},
        4: {**no_start, "end": 5100, "automatic_loss": 30},
        5: {**no_start, "end": 5090, "automatic_state": "unknown"},
        6: {
            **complete, "final": 5185, "start": 5145, "boundary_kind": "season",
            "end": 5000,
        },
        7: {**complete, "final": 5087, "boundary_kind": "season", "end": 5000, "proven_start": 1},
        8: {**complete, "final": 4980, "boundary_kind": "weekly", "end": 5000, "proven_start": 1},
        9: {
            **complete, "final": 4980, "start": 4940, "boundary_kind": "weekly",
            "end": 5000,
        },
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        generation_id = _seed_board(connection_info, readings)
        _seed_days(connection_info, generation_id, days, results)
        database = Database(connection_info)
        try:
            assert _build_board(connection_info, database, generation_id) == [
                ("#Y8V9YYP9C", 5191, "uncertain"),
                ("#PL0Q0UVLC", 5140, "uncertain"),
                ("#P0VPRVPJJ", 5130, "uncertain"),
                ("#P2CC9URVR", 5120, "uncertain"),
                ("#YPG0UY9LU", 5087, "confirmed"),
                ("#GURYYP99", 4992, "uncertain"),
                ("#LJQCVPVPL", 4990, "uncertain"),
                ("#QQC2GRQU", 4980, "confirmed"),
                ("#2QCYU8C2G", 4931, "confirmed"),
            ]
            season = ranked_day_for(DAY_2_RESET - RANKED_DAY_DURATION).official_season_id
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == []
            # A board built before this rule showed the reading plus the
            # battles after it as proven.
            with database.pool.connection() as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "UPDATE leaderboard_snapshot_entries SET trophies = 4902"
                    " WHERE player_id = 1"
                )
            assert boundary.queue_board_rebuilds(database, season, queue=False)[
                "boards"
            ] == [
                {
                    "boundary_at": DAY_2_RESET.isoformat(),
                    "generation": 1,
                    "profile_not_found": 0,
                    "late_battles": 1,
                    "missing_proof": 0,
                    "correction": "not_queued",
                }
            ]
        finally:
            database.close()
