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
from domain_test_support import domain_database, repair_season, store_observation, text
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    SEASON,
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

ONCE, PLAYING, ACCEPTED, REPEATED, STOPPED, RESET, SKIPPED, FLAPS, RETURNS, EARLY = (
    "#2PP", "#8PP", "#9PP", "#PPP", "#YPP", "#LPP", "#QPP", "#GPP", "#RPP", "#CPP")


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
    RESET's Monday Reset reading is one conflict Legend II answer. SKIPPED
    gets two, at 17:00 and 19:00, around a Legend I answer at 18:00 that the
    worker skips once a Legend I answer at 21:00 covers it. FLAPS gets a
    pair, a Legend battle, a Legend I answer and another pair, so tracking
    stops twice, then a Legend battle on Tuesday tracks them again. RETURNS
    gets a pair that stops tracking, then a Legend I answer that tracks them
    again while the pair still makes Monday no Legend I day, until ONCE's
    Wednesday log shows ONCE attacking them. EARLY gets a conflict answer on
    Sunday at 17:00 and another as its Monday Reset reading, then defends at
    05:30. Tuesday's sweep reads the players still tracked, as the collector
    does, and Monday's Daily board is published from it. REPEATED is
    promoted back on 12 October, attacks at 05:41, before its first Legend I
    answer at 06:10, and again after it, and the Season is repaired."""
    season = PREVIOUS_SEASON
    saturday, sunday = day(20, season), day(21, season)
    monday, tuesday, wednesday = day(22, season), day(23, season), day(24, season)
    stays = (ONCE, PLAYING, STOPPED, RESET, SKIPPED, FLAPS, RETURNS, EARLY)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       ONCE, PLAYING, ACCEPTED, REPEATED, STOPPED, RESET, SKIPPED, FLAPS,
                       RETURNS, EARLY)
        # From 5,300 Saturday ends at 5,140, and Sunday at 4,940: 1 attack
        # of 40 and 8 defenses of 30. Those staying play Monday from 5,000
        # to 4,840; PLAYING attacks again at 18:00 and FLAPS at 17:45, for
        # 4,880, FLAPS once more at 05:30 on Tuesday, and ONCE attacks
        # RETURNS at 06:00 on Wednesday.
        for player in story.players.values():
            player.battles = _saturday(saturday) + attacks(sunday, 40) + defenses(
                sunday, *[30] * 8)
        for tag in stays:
            story.players[tag].battles += attacks(
                monday + timedelta(hours=4), 40, 40) + defenses(monday, *[30] * 8)
        story.players[PLAYING].battles.append(
            Battle(monday + timedelta(hours=13), 40, True))
        story.players[FLAPS].battles += [Battle(monday + timedelta(hours=12, minutes=45), 40, True),
                                         Battle(tuesday + timedelta(minutes=30), 40, True)]
        story.players[ONCE].battles.append(Battle(wednesday + timedelta(hours=1), 40, True))
        story.players[RETURNS].battles.append(Battle(wednesday + timedelta(hours=1), -40, False))
        story.reset(saturday, dict.fromkeys(story.players, (5300, season)))
        story.reset(sunday, dict.fromkeys(story.players, (5140, season)))
        _legend_ii(story, EARLY, sunday + timedelta(hours=12), 4940, accepted=False)
        story.reads(monday - timedelta(minutes=10), {
            tag: 4940 for tag in story.players if tag != EARLY}, season)
        for tag in (RESET, EARLY):
            story.players[tag].dropped = True
        story.reset(monday, {tag: (5000 if tag in stays else 4940, season)
                             for tag in story.players})
        for tag in (RESET, EARLY):
            story.players[tag].dropped = False
        story.reads(monday + timedelta(minutes=20), {
            tag: 5000 for tag in stays if tag != EARLY}, season)
        story.run_worker()
        # EARLY's own log shows its 05:30 defense.
        story.read(EARLY, monday + timedelta(minutes=45))
        story.run_worker()

        evening = monday + timedelta(hours=12)
        for tag in (ONCE, PLAYING, REPEATED, STOPPED, SKIPPED, FLAPS, RETURNS):
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
        # SKIPPED's 18:00 answer waits until the worker has saved those after it.
        story.read(SKIPPED, monday + timedelta(hours=13), 4840, season)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET due_at = now() + interval '1 day'"
                " WHERE observation_id = (SELECT id FROM collector_observations"
                " WHERE occurrence_key = %s)",
                (f"{SKIPPED}-profile-{(monday + timedelta(hours=13)).isoformat()}",),
            )
        _legend_ii(story, SKIPPED, monday + timedelta(hours=14), 5000, accepted=False)
        story.read(SKIPPED, monday + timedelta(hours=16), 4840, season)
        # FLAPS's and RETURNS's second answers stop tracking, as nothing came between.
        for tag in (FLAPS, RETURNS):
            _legend_ii(story, tag, monday + timedelta(hours=12, minutes=30), 5000, accepted=False)
        story.run_worker()
        story.read(PLAYING, monday + timedelta(hours=13, minutes=5))
        # STOPPED's Refresh: Legend I.
        story.read(STOPPED, monday + timedelta(hours=15), 4840, season)
        # FLAPS's 17:45 battle tracks them again; then Legend I, then a pair.
        story.read(FLAPS, monday + timedelta(hours=12, minutes=50))
        # RETURNS's Legend I answer.
        story.read(RETURNS, monday + timedelta(hours=13, minutes=30), 4840, season)
        story.run_worker()
        story.read(FLAPS, monday + timedelta(hours=13, minutes=30), 4880, season)
        for hours in (14.5, 15.5):
            _legend_ii(story, FLAPS, monday + timedelta(hours=hours), 5000, accepted=False)
        story.run_worker()

        # The last checks before Tuesday's Reset, then its sweep of every
        # tracked player: REPEATED still shows Legend II there.
        totals = {ONCE: 4840, PLAYING: 4880, STOPPED: 4840, RESET: 4840, SKIPPED: 4840,
                  RETURNS: 4840, EARLY: 4840, REPEATED: 4940}
        story.reads(tuesday - timedelta(minutes=10), {
            tag: totals[tag] for tag in stays if tag != FLAPS}, season)
        sweep = sorted(tag for tag, (active, _) in _tracked(story).items() if active)
        story.players[REPEATED].dropped = True
        story.reset(tuesday, {
            tag: (totals[tag], None if tag == REPEATED else season) for tag in sweep})
        story.run_worker()
        # FLAPS's own battle log from Tuesday's 05:30 battle, then ONCE's
        # Wednesday log naming RETURNS.
        story.read(FLAPS, tuesday + timedelta(hours=1))
        at = wednesday + timedelta(hours=2)
        log = json.loads(story.players[ONCE].log(at))
        log["items"][0]["opponentPlayerTag"] = RETURNS
        store_observation(
            connection_info, archive_server, occurrence_key=f"{ONCE}-battle_log-{at.isoformat()}",
            endpoint="battle_log", body=json.dumps(log).encode(), observed_at=at,
            normalized_tag=ONCE,
        )
        story.wait(timedelta(hours=1))
        after_tuesday = _tracked(story)
        days = _days(story, sunday, monday)
        board = story.served_board(tuesday)
        sunday_board = story.served_board(monday)
        # REPEATED, promoted back two weeks later, attacks before its first
        # Legend I answer and after it.
        promoted = day(8)
        story.players[REPEATED].dropped = False
        story.players[REPEATED].battles += [Battle(promoted + timedelta(minutes=41), 40, True),
                                            Battle(promoted + timedelta(hours=2), 40, True)]
        story.read(REPEATED, promoted + timedelta(hours=1))
        story.run_worker()
        story.read(REPEATED, promoted + timedelta(hours=1, minutes=10), 5040, SEASON)
        story.run_worker()
        promoted_tracking = _tracked(story)[REPEATED]
        story.read(REPEATED, promoted + timedelta(hours=2, minutes=30))
        story.run_worker()
        repair_season(connection_info, str(int(season.timestamp())))
        story.run_worker()
        repaired = _days(story, sunday, monday)[REPEATED]
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    # The old rule stopped tracking ONCE, PLAYING and REPEATED at 17:00.
    assert after_one_answer == {
        ONCE: (True, "eligible"), PLAYING: (True, "eligible"),
        REPEATED: (True, "eligible"), ACCEPTED: (False, "ineligible"),
        STOPPED: (False, "ineligible"), RESET: (True, "eligible"),
        SKIPPED: (True, "eligible"), FLAPS: (True, "eligible"),
        RETURNS: (True, "eligible"), EARLY: (True, "eligible"),
    }
    assert sweep == sorted([ONCE, PLAYING, REPEATED, STOPPED, RESET, SKIPPED, RETURNS, EARLY])
    # A second Legend II answer with no Legend battle since the first stops
    # tracking; one after a newer Legend battle never does, whichever is
    # processed first, nor one whose answer before it went unsaved. A Legend I
    # answer or a newer Legend battle tracks a stopped player again.
    assert after_tuesday == {
        ONCE: (True, "eligible"), PLAYING: (True, "eligible"),
        REPEATED: (False, "ineligible"), ACCEPTED: (False, "ineligible"),
        STOPPED: (True, "eligible"), RESET: (True, "eligible"),
        SKIPPED: (True, "eligible"), FLAPS: (True, "eligible"),
        RETURNS: (True, "eligible"), EARLY: (True, "eligible"),
    }
    # Those staying keep Sunday's raise to 5,000 and a Legend I Monday;
    # those demoted end Sunday at 4,940 and have no Legend I Monday (ACCEPTED,
    # untracked before Tuesday's Reset, has none saved). FLAPS's second
    # return recalculates Sunday again, as ONCE's log of its Wednesday
    # attack does for RETURNS, tracked by then.
    raised = {"state": "Complete", "start": 5140, "end": 4940, "next_start": 5000,
              "automatic_loss": None, "raise": 60, "attacks": 1, "defenses": 8,
              "residual": 0}
    dropped = {**raised, "next_start": 4940, "raise": 0}
    assert stopped_days != (raised, True)
    reset_sunday, reset_monday = days.pop(RESET)
    early_sunday, early_monday = days.pop(EARLY)
    assert days == {
        ONCE: (raised, True), PLAYING: (raised, True), STOPPED: (raised, True),
        SKIPPED: (raised, True), FLAPS: (raised, True), RETURNS: (raised, True),
        ACCEPTED: (dropped, None), REPEATED: (dropped, False),
    }
    # RESET's Reset reading gives no total, so Sunday's end is calculated;
    # the raise and a Legend I Monday stay.
    assert (reset_sunday["end"], reset_sunday["raise"], reset_monday) == (4940, 60, True)
    # EARLY's Monday defense, after the Reset reading, shows it never left:
    # it keeps tracking, the raise and a Legend I Monday.
    assert (early_sunday["end"], early_sunday["raise"], early_monday) == (4940, 60, True)
    # FLAPS, untracked at Tuesday's Reset, is not on Monday's Daily board.
    assert _ranked(board) == {
        ONCE: 4840, PLAYING: 4880, STOPPED: 4840, RESET: 4840, SKIPPED: 4840,
        RETURNS: 4840, EARLY: 4840}
    assert _ranked(sunday_board)[RESET] == 4940
    # REPEATED's Legend I answer tracks it again, but its Legend battles from
    # the Monday Reset it was promoted at, before that answer or after it,
    # show nothing about the week it left: the repaired Season keeps its drop.
    assert promoted_tracking == (True, "eligible")
    assert repaired == (dropped, False)


def _legend_days(story: Story, tag: str, *starts) -> dict:
    """The player's saved attacks and defenses on each Legend day from
    ``starts``, and whether it is a Legend I day."""
    with psycopg.connect(story.connection_info) as connection:
        return {
            start: (attack_count, defense_count, "player_not_eligible" not in reasons)
            for start, attack_count, defense_count, reasons in connection.execute(
                """
                SELECT DISTINCT ON (day.ranked_day_start) day.ranked_day_start,
                       day.attack_count, day.defense_count, day.failure_reasons
                FROM ranked_day_versions AS day
                JOIN players AS player ON player.id = day.player_id
                WHERE player.normalized_tag = %s AND day.ranked_day_start = ANY(%s)
                ORDER BY day.ranked_day_start, day.version DESC
                """, (tag, list(starts)),
            ).fetchall()
        }


def test_wrongly_stopped_players_tracked_again_get_each_legend_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    """BUSY and LATE, still in Legend I, were stopped at 17:00 on Monday 28
    September by one conflict Legend II answer each under the old rule, so
    the Reset sweeps, which read only the players tracked, left them out. On
    Wednesday at 06:00 a Refresh still reads conflict Legend II for BUSY, but
    its battle log shows a Legend defense at 06:00 on Tuesday, so it never
    left. LATE keeps playing on Tuesday and Wednesday, and its Refresh on
    Thursday at 06:00 reads Legend I, as the re-check of the 86 would.
    Tracking each again saves every Legend day from Monday as a Legend I day
    with its battles, though none after Monday's Reset had a Reset reading."""
    season = PREVIOUS_SEASON
    saturday, sunday = day(20, season), day(21, season)
    monday, tuesday, wednesday, thursday = (day(number, season) for number in (22, 23, 24, 25))
    busy, late, keep = "#PPP", "#2PP", "#9PP"
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, busy, late, keep)
        for player in story.players.values():
            player.battles = _saturday(saturday) + attacks(sunday, 40) + defenses(
                sunday, *[30] * 8) + attacks(monday + timedelta(hours=4), 40, 40) + defenses(
                monday, *[30] * 8)
        story.players[busy].battles.append(Battle(tuesday + timedelta(hours=1), -30, False))
        story.players[late].battles += attacks(tuesday + timedelta(hours=4), 40) + defenses(
            tuesday, 30) + defenses(wednesday, 30, 30)
        story.reset(saturday, dict.fromkeys(story.players, (5300, season)))
        story.reset(sunday, dict.fromkeys(story.players, (5140, season)))
        story.reads(monday - timedelta(minutes=10), dict.fromkeys(story.players, 4940), season)
        story.reset(monday, dict.fromkeys(story.players, (5000, season)))
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(story.players, 5000), season)
        evening = monday + timedelta(hours=12)
        for tag in (busy, late):
            _legend_ii(story, tag, evening, 5000, accepted=False)
        story.run_worker()
        # The old rule: tracking stopped, Monday's weekly drop.
        stopped_rule = reconciliation_db._dropped_from_legend_i
        stopped: list[int] = []
        with monkeypatch.context() as patch:
            patch.setattr(reconciliation_db, "_dropped_from_legend_i",
                          lambda connection, player_id, since, until: player_id in stopped
                          or stopped_rule(connection, player_id, since, until))
            with psycopg.connect(connection_info) as connection:
                for tag in (busy, late):
                    stopped.append(connection.execute(
                        "UPDATE players SET active = false, next_due_at = NULL,"
                        " eligibility_state = 'ineligible' WHERE normalized_tag = %s"
                        " RETURNING id", (tag,),
                    ).fetchone()[0])
                    first_battle_log.queue_weekly_drop(connection, stopped[-1], evening)
            story.wait(timedelta(hours=1))
        for boundary in (tuesday, wednesday):
            story.reads(boundary - timedelta(minutes=10), {keep: 4840}, season)
            story.reset(boundary, {keep: (4840, season)})
        refresh = wednesday + timedelta(hours=1)
        _legend_ii(story, busy, refresh, 4810, accepted=False)
        story.run_worker()
        story.read(busy, refresh + timedelta(minutes=1))
        story.wait(timedelta(hours=1))
        # Thursday's sweep reads BUSY again; LATE's Refresh reads Legend I.
        story.reads(thursday - timedelta(minutes=10), {keep: 4840, busy: 4810}, season)
        story.reset(thursday, {keep: (4840, season), busy: (4810, season)})
        story.read(late, thursday + timedelta(hours=1), 4790, season)
        story.run_worker()
        story.read(late, thursday + timedelta(hours=1, minutes=1))
        story.wait(timedelta(hours=1))
        tracked = _tracked(story)
        days = _days(story, sunday, monday)
        busy_monday = story.saved_day(busy, monday)
        busy_tuesday = story.saved_day(busy, tuesday)
        legend_days = {tag: _legend_days(story, tag, monday, tuesday, wednesday)
                       for tag in (busy, late)}
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert tracked == dict.fromkeys(story.players, (True, "eligible"))
    raised = {"state": "Complete", "start": 5140, "end": 4940, "next_start": 5000,
              "automatic_loss": None, "raise": 60, "attacks": 1, "defenses": 8,
              "residual": 0}
    assert days == dict.fromkeys(story.players, (raised, True))
    assert (busy_monday["start"], busy_monday["attacks"], busy_monday["defenses"]) == (
        5000, 2, 8)
    assert busy_tuesday is not None and busy_tuesday["defenses"] == 1
    assert legend_days == {
        busy: {monday: (2, 8, True), tuesday: (0, 1, True), wednesday: (0, 0, True)},
        late: {monday: (2, 8, True), tuesday: (1, 1, True), wednesday: (0, 2, True)},
    }


def test_wrongly_stopped_player_demoted_while_untracked_is_legend_i_to_the_next_monday_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    """GONE and STAYS, still in Legend I, were stopped at 17:00 on Monday 14
    September by one conflict Legend II answer each under the old rule, and
    played Legend battles on Tuesday and Wednesday while untracked. GONE is
    demoted at the Monday 21 September Reset; STAYS defends on Tuesday 22
    September. A Refresh on Wednesday 23 September reads conflict Legend II
    for both, and their battle logs track them again. Thursday's Reset
    reading shows GONE in Legend II again, which stops tracking it, and
    STAYS in Legend I. Leagues change only at a Monday Reset, so each was a
    Legend I player to the 21 September Reset; after it only STAYS's
    Tuesday defense shows it stayed."""
    season = PREVIOUS_SEASON
    days = {number: day(number, season) for number in range(6, 19)}
    saturday, sunday, monday = days[6], days[7], days[8]
    gone, stays, keep = "#PPP", "#2PP", "#9PP"
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, gone, stays, keep)
        for player in story.players.values():
            player.battles = _saturday(saturday) + attacks(sunday, 40) + defenses(
                sunday, *[30] * 8) + attacks(monday + timedelta(hours=4), 40, 40) + defenses(
                monday, *[30] * 8)
        for tag in (gone, stays):
            story.players[tag].battles += attacks(days[9] + timedelta(hours=4), 40) + defenses(
                days[9], 30) + defenses(days[10], 30, 30)
        story.players[stays].battles += defenses(days[16], 30)
        story.reset(saturday, dict.fromkeys(story.players, (5300, season)))
        story.reset(sunday, dict.fromkeys(story.players, (5140, season)))
        story.reads(monday - timedelta(minutes=10), dict.fromkeys(story.players, 4940), season)
        story.reset(monday, dict.fromkeys(story.players, (5000, season)))
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(story.players, 5000), season)
        evening = monday + timedelta(hours=12)
        for tag in (gone, stays):
            _legend_ii(story, tag, evening, 5000, accepted=False)
        story.run_worker()
        # The old rule: tracking stopped, Monday's weekly drop.
        stopped_rule = reconciliation_db._dropped_from_legend_i
        stopped: list[int] = []
        with monkeypatch.context() as patch:
            patch.setattr(reconciliation_db, "_dropped_from_legend_i",
                          lambda connection, player_id, since, until: player_id in stopped
                          or stopped_rule(connection, player_id, since, until))
            with psycopg.connect(connection_info) as connection:
                for tag in (gone, stays):
                    stopped.append(connection.execute(
                        "UPDATE players SET active = false, next_due_at = NULL,"
                        " eligibility_state = 'ineligible' WHERE normalized_tag = %s"
                        " RETURNING id", (tag,),
                    ).fetchone()[0])
                    first_battle_log.queue_weekly_drop(connection, stopped[-1], evening)
            story.wait(timedelta(hours=1))
        for number in range(9, 18):
            story.reads(days[number] - timedelta(minutes=10), {keep: 4840}, season)
            story.reset(days[number], {keep: (4840, season)})
        refresh = days[17] + timedelta(hours=1)
        for tag in (gone, stays):
            _legend_ii(story, tag, refresh, 4790, accepted=False)
        story.run_worker()
        for tag in (gone, stays):
            story.read(tag, refresh + timedelta(minutes=1))
        story.wait(timedelta(hours=1))
        returned = _tracked(story)
        story.reads(days[18] - timedelta(minutes=10), {keep: 4840}, season)
        story.players[gone].dropped = True
        story.reset(days[18], {keep: (4840, season), gone: (4790, None), stays: (4760, season)})
        story.wait(timedelta(hours=1))
        tracked = _tracked(story)
        legend_days = {tag: _legend_days(story, tag, *(days[number] for number in range(8, 18)))
                       for tag in (gone, stays)}
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert returned == dict.fromkeys(story.players, (True, "eligible"))
    assert tracked == {gone: (False, "ineligible"), stays: (True, "eligible"),
                       keep: (True, "eligible")}
    # Monday 14 to Sunday 20 September are Legend I days with their battles.
    week = {days[8]: (2, 8, True), days[9]: (1, 1, True), days[10]: (0, 2, True),
            **{days[number]: (0, 0, True) for number in range(11, 15)}}
    assert legend_days == {
        gone: {**week, **{days[number]: (0, 0, False) for number in range(15, 18)}},
        stays: {**week, days[15]: (0, 0, True), days[16]: (0, 1, True),
                days[17]: (0, 0, True)},
    }
