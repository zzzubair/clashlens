"""End-to-end: players promoted into Legend I at a mid-Season Monday Reset.

On 12 October 2026, the Season's first Monday after Day 1, the game promoted
Legend II's top players into Legend I at 5,000. None was in the Reset sweep,
so 1,724 promoted players' Mondays had no Reset reading to start from, and
most were first read in Legend I only after their first battle. They were
saved Partial and shown unfinished on the Tuesday board, though 1,565 of the
1,567 with a Tuesday Reset reading had battles adding up to it from exactly
5,000. With no Legend I day before, the game averaged each one's automatic
defense loss over that day's own defenses, as on a Season's Day 1: 115 of 119
read after the loss lost exactly that. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import domain_database
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
    multiplayer,
)

PROMOTED, FIRST_SEEN, STEADY, MISSED = "#2PP", "#8PP", "#9PP", "#PPP"
RETURNED, UNPROVEN = "#2PP", "#8PP"


def test_players_promoted_into_legend_i_at_a_monday_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    """PROMOTED, read in Legend II on Sunday with a battle log, and
    FIRST_SEEN, never read before, join Legend I at the Monday Reset at 5,000
    and are first read in Legend I after their first battles. Each Monday
    starts at 5,000 by the Season rule and is Complete on the Tuesday board,
    PROMOTED's from its first battle log saved since. PROMOTED's 3 attacks
    and 2 defenses lose 30 at the Reset, Day 1's charge for the 1 slot its
    attacks outnumber its defenses, not 180 for its 6 empty slots; the
    Tuesday Reset reading came before it. MISSED, in Legend I since Sunday
    with a Sunday battle, has no Monday Reset reading: its Monday starts
    from Sunday's end, not 5,000."""
    season = day(1)
    sunday, monday, tuesday = day(7), day(8), day(9)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       PROMOTED, FIRST_SEEN, STEADY, MISSED)
        story.players[PROMOTED].battles = multiplayer(sunday + timedelta(hours=1), 2) + attacks(
            monday, 40, 40, 40) + defenses(monday, 30, 30)
        story.players[FIRST_SEEN].battles = attacks(monday, 40, 40)
        story.players[STEADY].battles = attacks(sunday, 40) + attacks(monday, 40)
        story.players[MISSED].battles = attacks(sunday, 40) + attacks(monday, 40)
        story.players[PROMOTED].dropped = True
        story.reads(sunday + timedelta(hours=2), {PROMOTED: 1400})
        story.reset(sunday, {STEADY: (5200, season), MISSED: (5200, season)})
        story.reset(monday, {STEADY: (5240, season), MISSED: (None, season)})
        # Read in Legend I after their first battles: neither shows 5,000.
        story.players[PROMOTED].dropped = False
        story.reads(monday + timedelta(hours=2, minutes=10), {PROMOTED: 5020, FIRST_SEEN: 5080})
        story.reads(tuesday - timedelta(minutes=10), {
            PROMOTED: 5060, FIRST_SEEN: 5080, STEADY: 5280, MISSED: 5280})
        story.reset(tuesday, {PROMOTED: (5060, season), FIRST_SEEN: (5080, season),
                              STEADY: (5280, season), MISSED: (5280, season)})
        # The 05:20 settlement check shows PROMOTED's automatic loss.
        story.reads(tuesday + timedelta(minutes=20), {PROMOTED: 5030})
        story.wait(timedelta(hours=1))
        mondays = {tag: story.saved_day(tag, monday) for tag in story.players}
        members = story.board_members(tuesday)
        board = story.served_board(tuesday)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert mondays[PROMOTED] == {
        "state": "Complete", "start": 5000, "end": 5030, "next_start": 5030,
        "automatic_loss": 30, "raise": 0, "attacks": 3, "defenses": 2, "residual": 0}
    assert mondays[FIRST_SEEN] == {
        "state": "Complete", "start": 5000, "end": 5080, "next_start": 5080,
        "automatic_loss": None, "raise": 0, "attacks": 2, "defenses": 0, "residual": 0}
    assert mondays[STEADY]["state"] == "Complete"
    assert (mondays[MISSED]["state"], mondays[MISSED]["start"]) == ("Complete", 5240)
    assert members == dict.fromkeys(story.players, "complete")
    assert _ranked(board) == {PROMOTED: 5060, FIRST_SEEN: 5080, STEADY: 5280, MISSED: 5280}


def test_player_promoted_back_into_legend_i_after_a_demotion_this_season(
    database_url: str, archive_server, monkeypatch
) -> None:
    """RETURNED and UNPROVEN play in Legend I in the Season's first week and
    are read in Legend II after its second Monday Reset: RETURNED 51 times,
    as hourly Refresh checks would, which proves it left Legend I, UNPROVEN
    once, which proves nothing. Both are read in Legend I again after their
    first battles on the third Monday, with no Reset reading there.
    RETURNED, promoted back, starts that Monday
    at 5,000 by the Season rule and is Complete on the Tuesday board, losing
    Day 1's charge of 30 at the Reset as a newly promoted player does.
    UNPROVEN's Monday gets no start of 5,000."""
    season = PREVIOUS_SEASON
    first_week, demoted, monday, tuesday = (day(number, season) for number in (2, 8, 15, 16))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, RETURNED, UNPROVEN)
        for player in story.players.values():
            player.battles = attacks(first_week, 40) + attacks(
                monday, 40, 40, 40) + defenses(monday, 30, 30)
        story.reads(first_week + timedelta(hours=3), {RETURNED: 5040, UNPROVEN: 5040}, season)
        for player in story.players.values():
            player.dropped = True
        for hour in range(51):
            story.read(RETURNED, demoted + timedelta(hours=2 + hour), 4900 + hour, season)
        story.reads(demoted + timedelta(days=1, hours=2), {UNPROVEN: 4900}, season)
        for player in story.players.values():
            player.dropped = False
        story.reads(monday + timedelta(hours=2, minutes=10),
                    {RETURNED: 5020, UNPROVEN: 5020}, season)
        story.reads(tuesday - timedelta(minutes=10), {RETURNED: 5060, UNPROVEN: 5060}, season)
        story.reset(tuesday, {RETURNED: (5060, season), UNPROVEN: (5060, season)})
        story.reads(tuesday + timedelta(minutes=20), {RETURNED: 5030, UNPROVEN: 5030}, season)
        story.wait(timedelta(hours=1))
        mondays = {tag: story.saved_day(tag, monday) for tag in story.players}
        members = story.board_members(tuesday)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert mondays[RETURNED] == {
        "state": "Complete", "start": 5000, "end": 5030, "next_start": 5030,
        "automatic_loss": 30, "raise": 0, "attacks": 3, "defenses": 2, "residual": 0}
    assert mondays[UNPROVEN]["start"] != 5000
    assert members[RETURNED] == "complete"
