"""End-to-end: a day result saved again with the same values leaves the
next day alone.

A day's saved result named the day before's result it was built from, so a
new result of the day before, even one holding the same numbers, made every
later day of the Season count as built from an outdated day: the late-battle
check and every later recalculation saved each of them again. A day's result
now covers the values it used from the day before, not which saved result
held them. See docs/testing.md.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import psycopg
from domain_test_support import domain_database
from reset_scenarios_postgres_support import (
    SEASON,
    _ranked,
    _results,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens.db import Database
from clashlens.late_battle_sweep import sweep_late_battles

RENAMED, OLD_FORM, LATE = "#2QQ", "#8QQ", "#9QQ"


def test_a_day_saved_again_with_the_same_values_leaves_the_next_day_alone(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Day 2 starts at 5,000, gains 80 over 2 attacks and loses 80 over 8
    defenses, with no automatic loss: 5,000, proven by the Day 3 Reset
    reading at 05:04. RENAMED and OLD_FORM play Day 3 the same way; LATE does
    not play it.

    At 15:00 after the Day 4 Reset each player's battle log is read again.
    RENAMED's and OLD_FORM's Day 2 opponents have changed their names since,
    so their Day 2 results list reports the log no longer shows. LATE's log
    first shows an attack for 30 at 04:58 on Day 2, so its Day 2 ends 30
    above its Reset reading.

    The late-battle check at 15:30 saves RENAMED's and OLD_FORM's Day 2 again
    with the new names and the same numbers, and works out their Day 3 again:
    from the same values, so no Day 3 is saved again, then, after the 16:00
    readings or at the next Reset's check, though each Day 3 names the Day 2
    result before. OLD_FORM's Day 3 result is then stored as results saved
    before this rule were: naming that Day 2 result and its input hash, not
    the values it used. LATE's Day 2 changed, so its Day 3 is saved again
    once, when its log is read."""
    day2, day3, day4 = day(2), day(3), day(4)
    tags = (RENAMED, OLD_FORM, LATE)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *tags)
        for tag in tags:
            story.players[tag].battles = [*attacks(day2, 40, 40), *defenses(day2, *[10] * 8)]
            if tag != LATE:
                story.players[tag].battles += [*attacks(day3, 40, 40), *defenses(day3, *[10] * 8)]
        for boundary in (day2, day3, day4):
            story.reset(boundary, dict.fromkeys(tags, (5000, SEASON)))
        before = {tag: (_results(connection_info, day2, tag),
                        _results(connection_info, day3, tag)) for tag in tags}
        first_board = (_ranked(story.served_board(day4)), _versions(connection_info, day4))

        for tag in (RENAMED, OLD_FORM):
            player = story.players[tag]
            player.battles = [replace(battle, opponent_name="Renamed") if battle.at < day3
                              else battle for battle in player.battles]
        story.players[LATE].battles.append(replace(
            story.players[LATE].battles[0], at=day3 - timedelta(minutes=2), trophies=30))
        for tag in tags:
            story.read(tag, day4 + timedelta(hours=10))
        story.run_worker()
        swept = [_sweep(connection_info, day4 + timedelta(hours=10, minutes=30))]
        _store_as_before_this_rule(connection_info, OLD_FORM, day3)
        story.reads(day4 + timedelta(hours=11), dict.fromkeys(tags, 5000))
        story.run_worker()
        story.wait(timedelta(hours=1))
        board = (_ranked(story.served_board(day4)), _versions(connection_info, day4))
        story.reset(day(5), dict.fromkeys(tags, (5000, SEASON)))
        swept.append(_sweep(connection_info, day(5) + timedelta(minutes=30)))
        saved = {tag: (_results(connection_info, day2, tag) - before[tag][0],
                       _results(connection_info, day3, tag) - before[tag][1]) for tag in tags}
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert first_board == (dict.fromkeys(tags, 5000), (1, 1))
    # New results of (Day 2, Day 3), and how many players each Reset's
    # late-battle check corrected.
    assert (saved, swept) == (
        {RENAMED: (1, 0), OLD_FORM: (1, 0), LATE: (1, 1)}, [(2, 0), (0, 0)])
    # LATE's Day 3 now starts 30 higher, which its Day 4 Reset reading
    # contradicts: one new board and army record, still ranking it at 5,000.
    assert board == (dict.fromkeys(tags, 5000), (2, 2))


def _sweep(connection_info: str, at: datetime) -> tuple[int, int] | None:
    """The worker's late-battle check at ``at``."""
    database = Database(connection_info)
    try:
        return sweep_late_battles(database, now=at)
    finally:
        database.close()


def _store_as_before_this_rule(connection_info: str, tag: str, start: datetime) -> None:
    """Rewrite the player's newest result of the day from ``start`` as one
    saved before this rule: what it read from the day before names that
    day's result and its input hash, without the values it used."""
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            UPDATE ranked_day_versions AS day
            SET input_evidence = jsonb_set(
                day.input_evidence, '{previous_day}',
                (day.input_evidence -> 'previous_day')
                - 'automatic_loss' - 'final_trophies' - 'expected_next_start'
                - 'battles_known' - 'end_baseline_id' - 'unsettled_automatic_loss'
                - 'zero_result_defense_slots' - 'reset_reading_correction'
                || jsonb_build_object('input_hash', previous.input_hash))
            FROM ranked_day_versions AS previous, players AS player
            WHERE player.normalized_tag = %s AND day.player_id = player.id
              AND day.ranked_day_start = %s
              AND previous.id = (day.input_evidence -> 'previous_day' ->> 'version_id')::bigint
              AND day.version = (SELECT max(version) FROM ranked_day_versions
                                 WHERE player_id = player.id AND ranked_day_start = %s)
            """,
            (tag, start, start),
        )


def _versions(connection_info: str, boundary: datetime) -> tuple[int, int]:
    """How many boards and army records the Reset has published."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT count(DISTINCT snapshot_id), count(DISTINCT army_publication_id)
            FROM boundary_publication_generations
            WHERE boundary_at = %s
            """,
            (boundary,),
        ).fetchone()
