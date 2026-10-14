"""End-to-end: days judged against the game's own battle timeline.

From 5 to 13 October 2026, 1,410 days were saved Inconsistent by a reading
during the day and 1,297 by a reading from the next Reset on, though both
players' logs agreed on 18,140 of their 18,142 battles. The readings were
late or early, not wrong: a profile can lag its battle log by an hour, and
show a defense from its stamp. A player's first Legend day was charged an
automatic loss the game never took. A day after one saved Inconsistent
ignored that day's loss. See docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import test_reset_scenarios_postgres as scenarios
from domain_test_support import domain_database
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

LATE, FIRST, NEXT, EARLY = "#2PP", "#8PP", "#9PP", "#PPP"


def _at(date: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, date, hour, minute, second, tzinfo=UTC)


def _battles(*rows: tuple[int, int, int, int, int]) -> list[Battle]:
    """(date, hour, minute, second, trophies): an attack gains, a defense loses."""
    return [Battle(_at(*at), trophies, trophies > 0) for *at, trophies in rows]


# #9G29VJ8C9's real 13 October day, 8 attacks and 8 defenses, and the first
# ten battles of its 14 October.
LATE_13_OCTOBER = _battles(
    (13, 5, 38, 36, -26), (13, 5, 44, 47, -40), (13, 7, 13, 10, -40), (13, 7, 22, 50, 40),
    (13, 7, 25, 49, 30), (13, 7, 28, 25, 40), (13, 7, 31, 59, 40), (13, 7, 35, 51, 40),
    (13, 7, 38, 55, 25), (13, 7, 41, 27, 27), (13, 7, 43, 59, 29), (13, 13, 57, 30, -40),
    (13, 14, 28, 8, -15), (13, 14, 44, 13, -28), (13, 20, 54, 17, -40), (14, 1, 13, 19, -40),
)
LATE_14_OCTOBER = _battles(
    (14, 5, 9, 36, 40), (14, 5, 12, 8, -30), (14, 5, 12, 22, 28), (14, 5, 15, 1, 27),
    (14, 5, 16, 14, -31), (14, 5, 17, 44, 40), (14, 5, 20, 29, 40), (14, 5, 23, 29, 40),
    (14, 5, 26, 39, 28), (14, 5, 29, 50, 40), (14, 7, 8, 16, -26),
)
# #2CQLYV28L's real 12 October, its first played day of the Season after
# sitting at 5,000: 7 attacks (+208) and 7 defenses (-162). Then 13 October:
# 8 attacks (+228) and 8 defenses (-253).
FIRST_12_OCTOBER = _battles(
    (12, 11, 3, 31, 28), (12, 11, 6, 36, 40), (12, 11, 6, 59, -11), (12, 11, 9, 36, 40),
    (12, 11, 12, 39, -14), (12, 11, 13, 43, 15), (12, 11, 16, 55, 30), (12, 11, 20, 13, 40),
    (12, 11, 22, 56, 15), (12, 11, 44, 42, -15), (12, 11, 56, 25, -30), (12, 13, 42, 40, -40),
    (12, 15, 54, 5, -12), (12, 21, 57, 26, -40),
)
FIRST_13_OCTOBER = _battles(
    (13, 7, 4, 14, 26), (13, 7, 11, 10, 30), (13, 7, 14, 58, 27), (13, 7, 18, 39, 40),
    (13, 7, 22, 10, 30), (13, 7, 25, 45, 31), (13, 7, 29, 17, 29), (13, 7, 33, 27, 15),
    (13, 11, 30, 27, -40), (13, 13, 26, 46, -24), (13, 15, 37, 1, -24), (13, 15, 49, 11, -25),
    (13, 16, 27, 45, -28), (13, 20, 43, 47, -32), (13, 21, 3, 47, -40), (14, 3, 30, 1, -40),
)


def test_days_judged_against_the_battle_timeline(
    database_url: str, archive_server, monkeypatch
) -> None:
    """LATE is #9G29VJ8C9's 13 October: every reading fits, the 14 October
    Reset reading at 05:05:23 shows its 5,013, and the 05:26:19 reading still
    shows 5,013 after seven new-day battles its own log already listed. That
    reading is the profile running late; the battles belong to 14 October,
    and 13 October stays Complete.

    FIRST is #2CQLYV28L: at 5,000 under the Season rule until its first
    battle at 11:03 on 12 October, with 7 attacks and 7 defenses. The game
    charges a first Legend day for (attacks - defenses) missing defenses,
    so nothing: 13 October's readings start from 5,046, and the 14 October
    Reset reading shows 5,021. Charged for (8 - 7), as an ordinary day, it
    ends at 5,023, and 13 October ends at 4,998.

    NEXT's 11 and 12 October are each Inconsistent: a reading at 20:00 shows
    2 trophies the battles never gave. Their battles are all known, so 12
    October's 6 defenses cost 2 x 30. Its Reset reading at 05:05:23 came
    before the loss; the 05:15:38 one shows it. 13 October starts at 5,460,
    not 5,520.

    EARLY's 13 October has a reading at 06:31:00 already showing a defense
    stamped 06:30:00 that lasted 150 s: a profile can show a defense from its
    stamp."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, LATE, FIRST, NEXT, EARLY)
        monkeypatch.setattr(scenarios, "READ_AFTER", timedelta(minutes=5, seconds=23))
        story.players[LATE].battles = LATE_13_OCTOBER + LATE_14_OCTOBER
        story.players[FIRST].battles = FIRST_12_OCTOBER + FIRST_13_OCTOBER
        story.players[NEXT].battles = (
            attacks(day(7), *[40] * 8) + defenses(day(7), *[30] * 8)
            + attacks(day(8), *[40] * 8) + defenses(day(8), *[30] * 6)
            + attacks(day(9), *[40] * 8) + defenses(day(9), *[30] * 8))
        story.players[EARLY].battles = attacks(day(9), *[40] * 8) + defenses(day(9), *[30] * 8)

        story.reset(day(7), {FIRST: (5000, None), NEXT: (5300, SEASON)})
        story.reads(_at(11, 20, 0), {NEXT: 5382})
        story.reset(day(8), {FIRST: (5000, None), NEXT: (5380, SEASON)})
        story.reads(_at(12, 5, 20, 55), {FIRST: 5000}, None)
        story.reads(_at(12, 11, 1, 47), {FIRST: 5000}, None)
        for at, trophies in (
            (_at(12, 11, 28, 29), 5183), (_at(12, 11, 49, 18), 5168), (_at(12, 12, 4, 43), 5138),
            (_at(12, 13, 47, 33), 5098), (_at(12, 15, 58, 19), 5086), (_at(12, 22, 1, 51), 5046),
        ):
            story.reads(at, {FIRST: trophies})
        story.reads(_at(12, 20, 0), {NEXT: 5522})

        story.reset(day(9), {FIRST: (5046, SEASON), NEXT: (5520, SEASON),
                             LATE: (5011, SEASON), EARLY: (5300, SEASON)})
        story.reads(_at(13, 5, 15, 38), {NEXT: 5460})
        story.reads(_at(13, 5, 21, 9), {FIRST: 5046})
        story.reads(_at(13, 6, 31), {EARLY: 5280})
        for tag, at, trophies in (
            (LATE, _at(13, 5, 46, 33), 4985), (LATE, _at(13, 5, 51, 3), 4945),
            (FIRST, _at(13, 7, 18, 6), 5072), (LATE, _at(13, 7, 23, 54), 4905),
            (LATE, _at(13, 7, 34, 21), 5015), (FIRST, _at(13, 7, 34, 37), 5259),
            (FIRST, _at(13, 7, 41, 9), 5274), (LATE, _at(13, 7, 49, 12), 5176),
            (FIRST, _at(13, 11, 34, 50), 5234), (FIRST, _at(13, 13, 31, 57), 5210),
            (LATE, _at(13, 14, 2, 18), 5136), (LATE, _at(13, 14, 32, 59), 5121),
            (LATE, _at(13, 14, 50), 5093), (FIRST, _at(13, 15, 43, 14), 5186),
            (FIRST, _at(13, 15, 54, 58), 5161), (FIRST, _at(13, 16, 31, 51), 5133),
            (FIRST, _at(13, 20, 49, 2), 5101), (FIRST, _at(13, 21, 8, 47), 5061),
            (LATE, _at(13, 20, 59, 14), 5053), (LATE, _at(14, 1, 17, 39), 5013),
            (FIRST, _at(14, 3, 34, 30), 5021),
        ):
            story.reads(at, {tag: trophies})
        story.reads(_at(13, 14, 0), {NEXT: 5540, EARLY: 5380})

        story.reset(day(10), {FIRST: (5021, SEASON), NEXT: (5540, SEASON),
                              LATE: (5013, SEASON), EARLY: (5380, SEASON)})
        story.read(LATE, _at(14, 5, 16, 13))
        story.read(LATE, _at(14, 5, 19, 48))
        story.read(LATE, _at(14, 5, 26, 19), 5013)
        story.reads(_at(14, 5, 37, 14), {LATE: 5235})
        story.reads(_at(14, 7, 14, 28), {LATE: 5209})
        story.wait(timedelta(hours=1))
        mondays = {tag: story.saved_day(tag, day(8)) for tag in (FIRST, NEXT)}
        tuesdays = {tag: story.saved_day(tag, day(9)) for tag in story.players}
        members = story.board_members(day(10))
        board = story.served_board(day(10))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert mondays[FIRST] == {
        "state": "Complete", "start": 5000, "end": 5046, "next_start": 5046,
        "automatic_loss": 0, "raise": 0, "attacks": 7, "defenses": 7, "residual": 0,
    }
    assert (mondays[NEXT]["state"], mondays[NEXT]["automatic_loss"], mondays[NEXT]["end"]) == (
        "Inconsistent", 60, 5460)
    assert {tag: (saved["state"], saved["start"], saved["end"])
            for tag, saved in tuesdays.items()} == {
        LATE: ("Complete", 5011, 5013), FIRST: ("Complete", 5046, 5021),
        NEXT: ("Complete", 5460, 5540), EARLY: ("Complete", 5300, 5380),
    }
    assert members == dict.fromkeys(story.players, "complete")
    assert _ranked(board) == {NEXT: 5540, EARLY: 5380, FIRST: 5021, LATE: 5013}
    labels = {entry["tag"]: entry["confidence"] for entry in board["entries"]}
    assert labels == dict.fromkeys(story.players, "confirmed")
