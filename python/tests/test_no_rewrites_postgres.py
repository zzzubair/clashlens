"""End-to-end: a Reset's results come out as before without saving unchanged rows again.

Production wrote about 100 GB of change log a day for about 6.4 GB of new
data, and the backups keep every byte of it for 10 days. Three of the
writers saved again what had not changed: each input added to a board
correction saved the correction's whole list again, 3.1 MB for 40,422
inputs on 9 October 2026; each battle log re-saved every battle it repeated;
each recount of a day's armies deleted and saved the whole day again. See
migration 0122 and docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import test_reset_scenarios_postgres as scenarios
from domain_test_support import domain_database, text
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    _ranked,
    _saturday,
    _story,
    attacks,
    day,
    defenses,
)

PLAYERS = ("#2PP", "#8PP", "#9PP", "#PPP", "#LPP")
COUNTS = ("army_package_day_counts", "army_package_defense_day_counts")


def _saved(connection_info: str, sunday) -> dict[str, object]:
    """What this story checks was saved: each battle's saved time; each
    correction's inputs, all of them and those in its own list; Sunday's army
    counts, each row with its row version, which changes whenever the row is
    saved again; and the facts each Sunday count was made from, against the
    facts saved now."""
    with psycopg.connect(connection_info) as connection:
        return {
            "battles": dict(connection.execute(
                "SELECT id, updated_at FROM legend_battles").fetchall()),
            "corrections": [
                (inputs, listed) for inputs, listed in connection.execute(
                    """
                    SELECT clashlens_correction_inputs(correction), pending_inputs
                    FROM boundary_publication_corrections AS correction
                    ORDER BY id
                    """
                ).fetchall()
            ],
            "counts": {
                table: sorted(
                    (text(version), *row[1:]) for version, *row in connection.execute(
                        f"SELECT xmin, * FROM {table} WHERE ranked_day_start = %s", (sunday,)
                    ).fetchall()
                )
                for table in COUNTS
            },
            "counted": connection.execute(
                """
                SELECT (SELECT fact_input_hash FROM army_type_counted_days
                        WHERE ranked_day_start = %(day)s),
                       (SELECT fact_input_hash FROM army_type_defense_counted_days
                        WHERE ranked_day_start = %(day)s),
                       (SELECT fact_input_hash FROM army_analytics_completed_days
                        WHERE ranked_day_start = %(day)s)
                """,
                {"day": sunday},
            ).fetchone(),
        }


def test_monday_corrections_battles_and_army_counts_save_only_what_changed(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Five Sundays end at 4,940 and are raised to 5,000 at the Monday Reset,
    read at 05:13, before the raise, as the last Reset readings were on 12
    October 2026. The 05:20 check shows the raise, so every Sunday changes
    after the Monday board's first build and joins it through corrections at
    the hourly rebuild. Each Sunday saves Complete and the board ranks each
    at 4,940, confirmed, as before. Battle logs keep reporting Sunday's
    battles, and Sunday's armies are counted again: neither saves an
    unchanged row again, and the counts read the same. The days must have
    ended by the real clock, so this is the previous Season's Monday,
    28 September 2026."""
    season = PREVIOUS_SEASON
    saturday, sunday, monday = (day(number, season) for number in (20, 21, 22))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *PLAYERS)
        monkeypatch.setattr(scenarios, "READ_AFTER", timedelta(minutes=13))
        for player in story.players.values():
            player.battles = (_saturday(saturday) + attacks(sunday, 40)
                              + defenses(sunday, *[30] * 8))
        story.reset(saturday, dict.fromkeys(PLAYERS, (5300, season)))
        story.reset(sunday, dict.fromkeys(PLAYERS, (5140, season)))
        story.reads(monday - timedelta(minutes=10), dict.fromkeys(PLAYERS, 4940), season)
        story.reset(monday, dict.fromkeys(PLAYERS, (4940, season)))
        # The worker's timer counts armies outside 04:30 to 07:00 UTC.
        noon = monday + timedelta(hours=7)
        story.count_armies(noon)
        before = _saved(connection_info, sunday)
        armies_before = {tag: story.served_armies(tag, monday) for tag in PLAYERS}
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(PLAYERS, 5000), season)
        story.wait(timedelta(hours=1))
        story.count_armies(noon)
        after = _saved(connection_info, sunday)
        armies_after = {tag: story.served_armies(tag, monday) for tag in PLAYERS}
        sundays = {tag: story.saved_day(tag, sunday) for tag in PLAYERS}
        members = story.board_members(monday)
        board = story.served_board(monday)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    complete = {
        "state": "Complete", "start": 5140, "end": 4940, "next_start": 5000,
        "automatic_loss": None, "raise": 60, "attacks": 1, "defenses": 8, "residual": 0,
    }
    assert sundays == {tag: complete for tag in PLAYERS}
    assert members == dict.fromkeys(PLAYERS, "complete")
    assert _ranked(board) == dict.fromkeys(PLAYERS, 4940)
    assert {entry["confidence"] for entry in board["entries"]} == {"confirmed"}

    # Each Sunday that changed after a build is an input of a correction, in
    # the order the changes arrived. A correction's own list keeps only the
    # input it was created with; each one added later is a row of its own,
    # so adding one saves no list again.
    corrections = [inputs for inputs, _ in after["corrections"] if inputs]
    assert len({entry["player_id"] for inputs in corrections for entry in inputs}) == len(PLAYERS)
    for inputs in corrections:
        versions = [entry["ranked_day_version_id"] for entry in inputs]
        assert versions == sorted(versions)
    assert all(len(listed) <= 1 for _, listed in after["corrections"])
    assert max(len(inputs) for inputs in corrections) == len(PLAYERS)

    # Every battle was reported again by the 05:20 and 05:21 checks: none
    # is saved again.
    assert before["battles"] and after["battles"] == before["battles"]

    # Sunday's armies were counted again from its new saved days, with the
    # same battles: the same counts, and no row saved again.
    assert len(set(before["counted"])) == len(set(after["counted"])) == 1
    assert after["counted"] != before["counted"]
    assert all(before["counts"][table] for table in COUNTS)
    assert after["counts"] == before["counts"]
    assert armies_after == armies_before
