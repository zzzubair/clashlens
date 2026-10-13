"""End-to-end: which Legend II answers stop tracking a Legend I player.

On 12 October 2026 a single profile answer showing Legend II with Season ID
0, which the parser marks conflict, stopped tracking 86 players hours after
the Reset; 15 of them had played Legend battles that day. The next Reset
sweep reads only tracked players, so each one left the next Daily board, and
their Sunday and Monday were saved as if they had dropped at the Monday
Reset. See docs/testing.md.
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
    _saturday,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import first_battle_log, reconciliation_db

ONCE, PLAYING, ACCEPTED, REPEATED, STOPPED = "#2PP", "#8PP", "#9PP", "#PPP", "#YPP"


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


def _days(story: Story, sunday, monday) -> dict[str, tuple]:
    """Each player's saved Sunday, and whether their saved Monday is a
    Legend I day."""
    with psycopg.connect(story.connection_info) as connection:
        monday_legend_i = {
            text(tag): "player_not_eligible" not in reasons
            for tag, reasons in connection.execute(
                """
                SELECT DISTINCT ON (player.id) player.normalized_tag, day.failure_reasons
                FROM ranked_day_versions AS day
                JOIN players AS player ON player.id = day.player_id
                WHERE day.ranked_day_start = %s
                ORDER BY player.id, day.version DESC
                """, (monday,),
            ).fetchall()
        }
    return {tag: (story.saved_day(tag, sunday), monday_legend_i.get(tag))
            for tag in story.players}


def test_one_conflict_legend_ii_answer_keeps_a_legend_i_player_tracked(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Monday 28 September 2026, the previous Season's Day 22. Each Sunday
    ends at 4,940; the Monday Reset raises it to 5,000 for those who stay in
    Legend I. At 17:00 ONCE gets one conflict Legend II answer. PLAYING gets
    two, with a Legend battle between them whose log the worker reads last.
    ACCEPTED, really demoted, gets an accepted Legend II answer. REPEATED,
    really demoted, gets a conflict one and again at Tuesday's Reset
    reading. STOPPED was stopped by its one conflict answer under the old
    rule, its days saved as a drop, until a Refresh reads Legend I at 20:00.
    Tuesday's sweep reads the players still tracked, as the collector does,
    and Monday's Daily board is published from it."""
    season = PREVIOUS_SEASON
    saturday, sunday = day(20, season), day(21, season)
    monday, tuesday = day(22, season), day(23, season)
    stays = (ONCE, PLAYING, STOPPED)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       ONCE, PLAYING, ACCEPTED, REPEATED, STOPPED)
        # From 5,300 Saturday ends at 5,140, and Sunday at 4,940: 1 attack
        # of 40 and 8 defenses of 30. Those staying play Monday from 5,000
        # to 4,840; PLAYING attacks again at 18:00, for 4,880.
        for player in story.players.values():
            player.battles = _saturday(saturday) + attacks(sunday, 40) + defenses(
                sunday, *[30] * 8)
        for tag in stays:
            story.players[tag].battles += attacks(
                monday + timedelta(hours=4), 40, 40) + defenses(monday, *[30] * 8)
        story.players[PLAYING].battles.append(
            Battle(monday + timedelta(hours=13), 40, True))
        story.reset(saturday, dict.fromkeys(story.players, (5300, season)))
        story.reset(sunday, dict.fromkeys(story.players, (5140, season)))
        story.reads(monday - timedelta(minutes=10), dict.fromkeys(story.players, 4940), season)
        story.reset(monday, {tag: (5000 if tag in stays else 4940, season)
                             for tag in story.players})
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(stays, 5000), season)
        story.run_worker()

        evening = monday + timedelta(hours=12)
        for tag in (ONCE, PLAYING, REPEATED, STOPPED):
            _legend_ii(story, tag, evening, 5000, accepted=False)
        _legend_ii(story, ACCEPTED, evening, 0, accepted=True)
        story.run_worker()
        # The old rule for STOPPED: tracking stopped, Monday's weekly drop.
        stopped_rule = reconciliation_db._dropped_from_legend_i
        with monkeypatch.context() as patch:
            patch.setattr(reconciliation_db, "_dropped_from_legend_i",
                          lambda connection, player_id, since, until: player_id == stopped
                          or stopped_rule(connection, player_id, since, until))
            with psycopg.connect(connection_info) as connection:
                stopped = connection.execute(
                    "UPDATE players SET active = false, next_due_at = NULL,"
                    " eligibility_state = 'ineligible' WHERE normalized_tag = %s"
                    " RETURNING id", (STOPPED,),
                ).fetchone()[0]
                first_battle_log.queue_weekly_drop(connection, stopped, evening)
            story.wait(timedelta(hours=1))
        after_one_answer = _tracked(story)
        stopped_days = _days(story, sunday, monday)[STOPPED]
        # PLAYING's 18:00 attack, then Legend II again at 19:00, processed
        # before that attack's battle log.
        _legend_ii(story, PLAYING, monday + timedelta(hours=14), 0, accepted=False)
        story.run_worker()
        story.read(PLAYING, monday + timedelta(hours=13, minutes=5))
        # STOPPED's Refresh: Legend I.
        story.read(STOPPED, monday + timedelta(hours=15), 4840, season)
        story.run_worker()

        # The last checks before Tuesday's Reset, then its sweep of every
        # tracked player: REPEATED still shows Legend II there.
        totals = {ONCE: 4840, PLAYING: 4880, STOPPED: 4840, REPEATED: 4940}
        story.reads(tuesday - timedelta(minutes=10), {
            tag: totals[tag] for tag in stays}, season)
        sweep = sorted(tag for tag, (active, _) in _tracked(story).items() if active)
        story.players[REPEATED].dropped = True
        story.reset(tuesday, {
            tag: (totals[tag], None if tag == REPEATED else season) for tag in sweep})
        story.run_worker()
        story.wait(timedelta(hours=1))
        after_tuesday = _tracked(story)
        days = _days(story, sunday, monday)
        unfinished = story.unfinished_jobs()
        board = story.served_board(tuesday)

    assert unfinished == []
    # The old rule stopped tracking ONCE, PLAYING and REPEATED at 17:00.
    assert after_one_answer == {
        ONCE: (True, "eligible"), PLAYING: (True, "eligible"),
        REPEATED: (True, "eligible"), ACCEPTED: (False, "ineligible"),
        STOPPED: (False, "ineligible"),
    }
    assert sweep == sorted([ONCE, PLAYING, REPEATED, STOPPED])
    # A second Legend II answer with no Legend battle since the first stops
    # tracking; one after a newer Legend battle never does, whichever is
    # processed first. A Legend I answer tracks a stopped player again.
    assert after_tuesday == {
        ONCE: (True, "eligible"), PLAYING: (True, "eligible"),
        REPEATED: (False, "ineligible"), ACCEPTED: (False, "ineligible"),
        STOPPED: (True, "eligible"),
    }
    # Those staying keep Sunday's raise to 5,000 and a Legend I Monday;
    # those demoted end Sunday at 4,940 and have no Legend I Monday (ACCEPTED,
    # untracked before Tuesday's Reset, has none saved).
    raised = {"state": "Complete", "start": 5140, "end": 4940, "next_start": 5000,
              "automatic_loss": None, "raise": 60, "attacks": 1, "defenses": 8,
              "residual": 0}
    dropped = {**raised, "next_start": 4940, "raise": 0}
    assert stopped_days != (raised, True)
    assert days == {
        ONCE: (raised, True), PLAYING: (raised, True), STOPPED: (raised, True),
        ACCEPTED: (dropped, None), REPEATED: (dropped, False),
    }
    assert _ranked(board) == {ONCE: 4840, PLAYING: 4880, STOPPED: 4840}
