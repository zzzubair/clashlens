"""End-to-end: which Legend II answers stop tracking a Legend I player.

On 12 October 2026 a single profile answer showing Legend II with Season ID
0, which the parser marks conflict, stopped tracking 86 players hours after
the Reset; 15 of them had played Legend battles that day. The next Reset
sweep reads only tracked players, so each one left the next Daily board.
See docs/testing.md.
"""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    Battle,
    Story,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

ONCE, PLAYING, ACCEPTED, REPEATED = "#2PP", "#8PP", "#9PP", "#PPP"


def _legend_ii(story: Story, tag: str, at, trophies: int, *, accepted: bool) -> None:
    """One routine profile read showing Legend II: with Season ID 0, which
    the parser marks conflict, or naming the Season, which it accepts."""
    player = story.players[tag]
    if accepted:
        body = json.loads(player.profile(trophies, PREVIOUS_SEASON))
        body["leagueTier"] = {"id": 105000035, "name": "Legend II"}
        body = json.dumps(body).encode()
    else:
        player.dropped = True
        body = player.profile(trophies, None)
        player.dropped = False
    store_observation(
        story.connection_info, story.archive_server,
        occurrence_key=f"{tag}-legend-ii-{at.isoformat()}", endpoint="profile",
        body=body, observed_at=at, normalized_tag=tag,
    )


def _tracked(story: Story) -> dict[str, tuple[bool, str]]:
    """Each player's tracking and eligibility as saved now."""
    with psycopg.connect(story.connection_info) as connection:
        return {
            text(tag): (active, text(state))
            for tag, active, state in connection.execute(
                "SELECT normalized_tag, active, eligibility_state FROM players"
                " WHERE normalized_tag = ANY(%s)", (list(story.players),),
            ).fetchall()
        }


def test_one_conflict_legend_ii_answer_keeps_a_legend_i_player_tracked(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Monday 28 September 2026, the previous Season's Day 22, after the
    Reset. ONCE gets one conflict Legend II answer at 17:00. PLAYING gets
    two, with a Legend battle between them. ACCEPTED gets an accepted Legend
    II answer. REPEATED gets a conflict Legend II answer at 17:00 and again
    at Tuesday's Reset reading, with no Legend battle since. Tuesday's sweep
    reads the players still tracked, as the collector does, and Monday's
    Daily board is published from it."""
    season = PREVIOUS_SEASON
    monday, tuesday = day(22, season), day(23, season)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       ONCE, PLAYING, ACCEPTED, REPEATED)
        # Each Monday: 2 attacks of 40 by 07:00 and 8 defenses of 30, 5,300
        # to 5,140. PLAYING attacks again at 18:00, for 5,180.
        for player in story.players.values():
            player.battles = attacks(monday + timedelta(hours=4), 40, 40) + defenses(
                monday, *[30] * 8)
        story.players[PLAYING].battles.append(
            Battle(monday + timedelta(hours=13), 40, True))
        story.reset(monday, {tag: (5300, season) for tag in story.players})
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(story.players, 5300), season)
        story.run_worker()

        evening = monday + timedelta(hours=12)
        for tag in (ONCE, PLAYING, REPEATED):
            _legend_ii(story, tag, evening, 5000, accepted=False)
        _legend_ii(story, ACCEPTED, evening, 0, accepted=True)
        story.run_worker()
        after_one_answer = _tracked(story)
        # PLAYING's 18:00 attack, then Legend II again at 19:00.
        story.read(PLAYING, monday + timedelta(hours=13, minutes=5))
        story.run_worker()
        _legend_ii(story, PLAYING, monday + timedelta(hours=14), 0, accepted=False)
        story.run_worker()

        # The last checks before Tuesday's Reset, then its sweep of every
        # tracked player: REPEATED still shows Legend II there.
        totals = {ONCE: 5140, PLAYING: 5180, ACCEPTED: 5140, REPEATED: 5140}
        story.reads(tuesday - timedelta(minutes=10), {
            tag: totals[tag] for tag in (ONCE, PLAYING)}, season)
        sweep = sorted(tag for tag, (active, _) in _tracked(story).items() if active)
        story.players[REPEATED].dropped = True
        story.reset(tuesday, {
            tag: (totals[tag], None if tag == REPEATED else season) for tag in sweep})
        story.run_worker()
        story.wait(timedelta(hours=1))
        after_tuesday = _tracked(story)
        unfinished = story.unfinished_jobs()
        board = story.served_board(tuesday)

    assert unfinished == []
    # The old rule stopped tracking all three at 17:00.
    assert after_one_answer == {
        ONCE: (True, "eligible"), PLAYING: (True, "eligible"),
        REPEATED: (True, "eligible"), ACCEPTED: (False, "ineligible"),
    }
    assert sweep == sorted([ONCE, PLAYING, REPEATED])
    # A second Legend II answer with no Legend battle since the first stops
    # tracking; one after a newer Legend battle never does.
    assert after_tuesday == {
        ONCE: (True, "eligible"), PLAYING: (True, "eligible"),
        REPEATED: (False, "ineligible"), ACCEPTED: (False, "ineligible"),
    }
    assert set(_ranked(board)) >= {ONCE, PLAYING}
    assert _ranked(board)[ONCE] == 5140 and _ranked(board)[PLAYING] == 5180
