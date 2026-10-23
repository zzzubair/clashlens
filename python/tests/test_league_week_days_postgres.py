"""End-to-end: Legend days exist only in weeks the player was in Legend I.

The Monday Reset of 12 October 2026, mid-Season, moved players between
leagues: about 2,000 dropped from Legend I to Legend II and about 1,700 were
promoted into it at 5,000. The game kept showing the old league for a
while, sometimes naming Season 0 for 12 hours or more. A dropped player's
Monday is no Legend day and must not show as one, unfinished or not; a
promoted player's Monday is one, starting at 5,000, whenever Clash Lens
first saw them; a Legend I player whose requests failed is still in Legend
I. The story saves what the collector would have saved, runs the real
worker against PostgreSQL and reads back the public days and Daily boards.
See docs/testing.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_collector_db_postgres import _handoff
from test_reconcile_pass_postgres import _open, _queued, _run_pass
from test_reset_scenarios_postgres import (
    SEASON,
    Player,
    Story,
    _ranked,
    _saturday,
    attacks,
    day,
    defenses,
    multiplayer,
)

from clashlens import reset_baselines
from clashlens.boundary import queue_board_rebuilds
from clashlens.collector_db import CollectorDatabase
from clashlens.collector_uploads import claim_upload, complete_upload
from clashlens.db import Database
from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION


@dataclass
class Climber(Player):
    """A player whose profile can show Legend II while naming the Season:
    the game's accepted answer for a Legend II player. ``dropped`` still
    shows Legend II naming Season 0."""

    legend_ii: bool = False

    def profile(self, trophies: int, season: datetime | None) -> bytes:
        payload = json.loads(super().profile(trophies, season))
        if self.legend_ii:
            payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
        return json.dumps(payload).encode()


def _public_days(connection_info: str, tag: str) -> dict[datetime, dict[str, object]]:
    """Each Legend day the player's page shows: the newest public row of
    each day, its state, unfinished reasons, attacks and defenses."""
    with psycopg.connect(connection_info) as connection:
        return {
            start: {"state": text(state), "reasons": list(reasons),
                    "attacks": attack_count, "defenses": defense_count}
            for start, state, reasons, attack_count, defense_count in connection.execute(
                """
                SELECT DISTINCT ON (daily.ranked_day_start)
                       daily.ranked_day_start, daily.state, daily.partial_reasons,
                       daily.attack_count, daily.defense_count
                FROM api_player_daily_logs AS daily
                JOIN players AS player ON player.id = daily.player_id
                WHERE player.normalized_tag = %s
                ORDER BY daily.ranked_day_start, daily.version DESC
                """,
                (tag,),
            ).fetchall()
        }


(DROPPED, STALE, STALE_LATE, PROMOTED, IDLE_PROMOTED, IDLE_BELOW, FAILED, FAILED_TWICE,
 FIRST_SEEN, STEADY_1, STEADY_2, STEADY_3) = (
    "#2PP", "#8PP", "#CPP", "#9PP", "#PPP", "#YPP", "#LPP", "#UPP", "#QPP", "#GPP", "#RPP",
    "#JPP")


def test_a_mid_season_monday_moves_players_between_leagues(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The Reset of Monday 12 October 2026, Day 8, and the Tuesday Reset
    after it.

    DROPPED, STALE and STALE_LATE play Legend I through Sunday and drop at
    the Monday Reset without another Legend battle. DROPPED, below 5,000, is
    read in Legend I at the Reset and at 05:20, the game's display lagging,
    then in Legend II naming the Season at 06:00. STALE, above 5,000, reads
    Legend I naming Season 0 at the Reset, at 05:20 and at 09:20, then
    Legend II naming Season 0 at 21:04 and 22:04, as #GQ9QQ0VLJ did.
    STALE_LATE, below 5,000, reads Legend I naming Season 0 until 04:50 on
    Tuesday, so it is still tracked at the Tuesday Reset, whose reading and
    the 05:20 one show Legend II naming Season 0, as 1 of 62 such players did
    on 12 October. Each Sunday ranks on the Monday board without the weekly
    raise; none of them has a Monday Legend day or a Tuesday board entry.

    PROMOTED and IDLE_PROMOTED, Legend II and untracked the week before,
    are promoted at the Monday Reset, read in Legend I at 5,000 at 06:10
    and swept at the Tuesday Reset. PROMOTED battles from 07:00; IDLE
    never does. Neither had a Legend day the week before. IDLE_BELOW stays
    in Legend II, read once that week naming Season 0, and never has a
    Legend day.

    FAILED stays in Legend I, but its Monday Reset profile and battle log
    never answered. FAILED_TWICE stays in Legend I, but its Reset profile
    requests fail at every Reset, as 135 tracked players' did through 12
    October; its regular checks answer. FIRST_SEEN, promoted at the Monday
    Reset like 83222770 and 83226079 were, is first read on Tuesday at
    06:25, its battle log reaching back to its Monday battles: its Monday
    starts at 5,000. With no reading at or before the Tuesday Reset, the
    Tuesday board has no trophies to rank it by. Three STEADY players stay
    in Legend I throughout.

    That night's re-check of the Season's days, with STEADY_1's Monday lost
    from its page, must find only that Monday missing: until 20 October
    2026 it expected a day of every player the Tuesday Reset read, so
    STALE_LATE's Monday was missing too, and its recalculation saved
    nothing."""
    saturday, sunday, monday, tuesday = day(6), day(7), day(8), day(9)
    week_before = day(1)
    failed = [FAILED, FAILED_TWICE]
    steady = {STEADY_1: 5300, STEADY_2: 5400, STEADY_3: 5500}
    legend_i = [DROPPED, STALE, STALE_LATE, *failed, *steady]
    lower = [PROMOTED, IDLE_PROMOTED, IDLE_BELOW]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {
            tag: Climber(tag) for tag in (*legend_i, *lower, FIRST_SEEN)})
        players = story.players
        # From 5,300 (STEADY_2 5,400, STEADY_3 5,500) each Saturday ends
        # 160 down.
        for tag in legend_i:
            players[tag].battles = _saturday(saturday)
        # Sunday from 5,140: DROPPED ends at 4,940, STALE at 5,020 and
        # STALE_LATE at 4,980; the rest end where they began.
        players[DROPPED].battles += attacks(sunday, 40) + defenses(sunday, *[30] * 8)
        players[STALE].battles += attacks(sunday, 40, 40, 40) + defenses(sunday, *[30] * 8)
        players[STALE_LATE].battles += attacks(sunday, 40, 40) + defenses(sunday, *[30] * 8)
        for tag in (*failed, *steady):
            players[tag].battles += attacks(sunday, *[40] * 6) + defenses(sunday, *[30] * 8)
        # Monday: FAILED and FAILED_TWICE 4 attacks and 8 defenses, 5,140
        # to 5,060; each STEADY 8 and 8, 80 up; PROMOTED 3 attacks and 2
        # defenses from 07:00, 5,000 to 5,050, then Day 1's charge of 30 for
        # its 1 empty slot; FIRST_SEEN 2 and 2 from 08:30, 5,000 to 5,040.
        for tag in failed:
            players[tag].battles += attacks(monday + timedelta(hours=2), *[40] * 4) + defenses(
                monday, *[30] * 8)
        for tag in steady:
            players[tag].battles += attacks(monday + timedelta(hours=8), *[40] * 8) + defenses(
                monday, *[30] * 8)
        players[PROMOTED].battles = attacks(monday + timedelta(hours=1), 40, 40, 30) + defenses(
            monday + timedelta(hours=2), 30, 30)
        players[FIRST_SEEN].battles = attacks(monday + timedelta(hours=3), 40, 40) + defenses(
            monday + timedelta(hours=3), 20, 20)
        # The Legend II players' logs hold the week before's multiplayer
        # battles.
        for tag in lower:
            players[tag].battles = multiplayer(week_before + timedelta(days=3), 3) + players[
                tag].battles
            players[tag].legend_ii = True

        # The week of 5 October: two accepted Legend II answers each for the
        # Legend II players; Saturday's and Sunday's Reset sweeps, a check
        # during Sunday and the 04:50 checks for the Legend I ones.
        for at, trophies in ((week_before + timedelta(days=3, hours=9), 1400),
                             (saturday + timedelta(hours=9), 1450)):
            for tag in lower:
                story.read(tag, at, trophies)
        story.reset(saturday, {tag: (steady.get(tag, 5300), SEASON) for tag in legend_i}
                    | {FAILED_TWICE: (None, SEASON)})
        sunday_start = {tag: steady.get(tag, 5300) - 160 for tag in legend_i}
        story.reset(sunday, {tag: (trophies, SEASON) for tag, trophies in sunday_start.items()}
                    | {FAILED_TWICE: (None, SEASON)})
        sunday_end = {**sunday_start, DROPPED: 4940, STALE: 5020, STALE_LATE: 4980}
        story.reads(sunday + timedelta(hours=9, minutes=15), sunday_end)
        story.reads(monday - timedelta(minutes=10), sunday_end)

        # Monday 12 October. No weekly raise: the Legend I players are above
        # 5,000 and the ones below dropped. FAILED's Reset profile and
        # battle log never answered, FAILED_TWICE's profile neither; the
        # stale readings name Season 0.
        stale = {STALE: 5020, STALE_LATE: 4980}
        story.reset(monday, {
            **{tag: (trophies, SEASON) for tag, trophies in sunday_end.items()},
            **{tag: (trophies, None) for tag, trophies in stale.items()},
            FAILED: (None, SEASON), FAILED_TWICE: (None, SEASON),
        }, no_log=(FAILED,))
        settled = [tag for tag in legend_i if tag not in (*stale, FAILED_TWICE)]
        story.reads(monday + timedelta(minutes=20), {tag: sunday_end[tag] for tag in settled})
        story.reads(monday + timedelta(minutes=20), stale, None)
        story.run_worker()
        players[DROPPED].legend_ii = True
        story.reads(monday + timedelta(hours=1), {DROPPED: 4940})
        for tag in (PROMOTED, IDLE_PROMOTED):
            players[tag].legend_ii = False
        story.reads(monday + timedelta(hours=1, minutes=10),
                    {PROMOTED: 5000, IDLE_PROMOTED: 5000})
        story.run_worker()
        story.reads(monday + timedelta(hours=4, minutes=20), stale, None)
        story.reads(monday + timedelta(hours=9, minutes=15), {
            FAILED: 5060, FAILED_TWICE: 5060, PROMOTED: 5050, IDLE_PROMOTED: 5000,
            # 8 defenses and the first attack, at 14:00.
            **{tag: start - 160 - 240 + 40 for tag, start in steady.items()}})
        players[IDLE_BELOW].dropped = True
        story.read(IDLE_BELOW, monday + timedelta(hours=10), 1460, None)
        players[STALE].dropped = True
        for hour in (16, 17):
            story.reads(monday + timedelta(hours=hour, minutes=4), {STALE: 5020}, None)
        story.wait(timedelta(hours=1))

        # Tuesday 13 October: the promoted players are tracked by now, and
        # STALE_LATE still is.
        monday_end = {
            FAILED: 5060, FAILED_TWICE: 5060, PROMOTED: 5050, IDLE_PROMOTED: 5000,
            **{tag: start - 80 for tag, start in steady.items()}}
        story.reads(tuesday - timedelta(minutes=10), monday_end)
        story.reads(tuesday - timedelta(minutes=10), {STALE_LATE: 4980}, None)
        players[STALE_LATE].dropped = True
        story.reset(tuesday, {
            **{tag: (trophies, SEASON) for tag, trophies in monday_end.items()},
            STALE_LATE: (4980, None), FAILED_TWICE: (None, SEASON),
        })
        story.reads(tuesday + timedelta(minutes=20), {
            tag: trophies for tag, trophies in {**monday_end, PROMOTED: 5020}.items()
            if tag != FAILED_TWICE})
        story.reads(tuesday + timedelta(minutes=20), {STALE_LATE: 4980}, None)
        story.run_worker()
        story.wait(timedelta(hours=1))
        story.reads(tuesday + timedelta(hours=1, minutes=25), {FIRST_SEEN: 5040})
        story.run_worker()
        story.wait(timedelta(hours=1))

        public = {tag: _public_days(connection_info, tag) for tag in players}
        sundays = {tag: story.saved_day(tag, sunday) for tag in (DROPPED, STALE, STALE_LATE)}
        mondays = {tag: story.saved_day(tag, monday)
                   for tag in (PROMOTED, IDLE_PROMOTED, FIRST_SEEN)}
        monday_board = story.served_board(monday)
        tuesday_board = story.served_board(tuesday)
        unfinished = story.unfinished_jobs()

        # That night's re-check of the Season's days, Monday its last, with
        # STEADY_1's Monday lost from its page.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "DELETE FROM api_player_daily_logs WHERE ranked_day_start = %s AND player_id ="
                " (SELECT id FROM players WHERE normalized_tag = %s)", (monday, STEADY_1))
        _run_pass(story, monkeypatch, tuesday + timedelta(hours=18))
        missing = {(tag, number) for check, tag, number in _open(connection_info)
                   if check == "missing_day"}
        queued = _queued(connection_info)
        story.run_worker()
        rechecked = {tag: _public_days(connection_info, tag) for tag in (STALE_LATE, STEADY_1)}

    def days(tag: str, since: datetime, until: datetime) -> dict[datetime, dict[str, object]]:
        return {start: row for start, row in public[tag].items() if since <= start < until}

    assert unfinished == []
    complete = {"state": "Complete", "reasons": []}
    # The dropped players' Sundays are Legend days, ending without the
    # weekly raise; their Mondays are not, and no day of theirs is labelled
    # "not eligible", which also means "readings missing".
    assert {tag: days(tag, sunday, tuesday) for tag in (DROPPED, STALE, STALE_LATE)} == {
        DROPPED: {sunday: {**complete, "attacks": 1, "defenses": 8}},
        STALE: {sunday: {**complete, "attacks": 3, "defenses": 8}},
        STALE_LATE: {sunday: {**complete, "attacks": 2, "defenses": 8}},
    }
    assert {tag: (saved["next_start"], saved["raise"]) for tag, saved in sundays.items()} == {
        DROPPED: (4940, 0), STALE: (5020, 0), STALE_LATE: (4980, 0)}
    assert [(tag, start) for tag in players for start, row in public[tag].items()
            if "player_not_eligible" in row["reasons"]] == []
    # The week before, in Legend II, holds no Legend day of the promoted
    # players' or IDLE_BELOW's, nor of FIRST_SEEN's, and IDLE_BELOW has
    # none at all.
    assert {tag: days(tag, week_before, monday) for tag in (*lower, FIRST_SEEN)} == {
        tag: {} for tag in (*lower, FIRST_SEEN)}
    assert public[IDLE_BELOW] == {}
    assert {tag: days(tag, monday, tuesday) for tag in (PROMOTED, IDLE_PROMOTED, FIRST_SEEN)} == {
        PROMOTED: {monday: {**complete, "attacks": 3, "defenses": 2}},
        IDLE_PROMOTED: {monday: {**complete, "attacks": 0, "defenses": 0}},
        FIRST_SEEN: {monday: {**complete, "attacks": 2, "defenses": 2}},
    }
    promoted = {"state": "Complete", "start": 5000, "raise": 0, "residual": 0}
    assert mondays[PROMOTED] == {
        **promoted, "end": 5020, "next_start": 5020, "automatic_loss": 30,
        "attacks": 3, "defenses": 2}
    assert mondays[IDLE_PROMOTED] == {
        **promoted, "end": 5000, "next_start": 5000, "automatic_loss": None,
        "attacks": 0, "defenses": 0}
    # First read on Tuesday, after the Monday Reset that promoted it: the
    # owner's rule starts its Monday at 5,000, and its 06:25 reading, after
    # its last Monday battle and before any Tuesday one, proves the end.
    first_seen = {key: mondays[FIRST_SEEN][key]
                  for key in ("state", "start", "end", "attacks", "defenses")}
    assert first_seen == {
        "state": "Complete", "start": 5000, "end": 5040, "attacks": 2, "defenses": 2}
    # A Legend I player whose Reset requests failed still has Legend days.
    # Neither Sunday has an end reading, so neither Monday has a start, as
    # the Wednesday story's CHAIN_BROKEN; FAILED_TWICE has no Reset reading
    # at either end of either day.
    unread = {"state": "Partial", "reasons": ["missing_start_baseline", "missing_end_baseline"]}
    assert {tag: days(tag, sunday, tuesday) for tag in failed} == {
        FAILED: {
            sunday: {"state": "Partial", "reasons": [
                "missing_end_battle_log_baseline", "missing_end_baseline"],
                "attacks": 6, "defenses": 8},
            monday: {"state": "Partial", "reasons": ["missing_start_baseline"],
                     "attacks": 4, "defenses": 8}},
        FAILED_TWICE: {sunday: {**unread, "attacks": 6, "defenses": 8},
                       monday: {**unread, "attacks": 4, "defenses": 8}},
    }
    assert _ranked(monday_board) == {
        STEADY_3: 5340, STEADY_2: 5240, STEADY_1: 5140, FAILED: 5140, FAILED_TWICE: 5140,
        STALE: 5020, STALE_LATE: 4980, DROPPED: 4940}
    assert _ranked(tuesday_board) == {
        STEADY_3: 5420, STEADY_2: 5320, STEADY_1: 5220, FAILED: 5060, FAILED_TWICE: 5060,
        PROMOTED: 5050, IDLE_PROMOTED: 5000}
    # The re-check expects a Legend day exactly where the record puts the
    # week in Legend I, as the day's calculation does: STALE_LATE, read at
    # the Tuesday Reset but out of Legend I that week, has no Monday to
    # miss and nothing is queued for it; STEADY_1's lost Monday is missing,
    # and its recalculation gives the page that Monday back.
    assert missing == {(STEADY_1, 8)}
    assert queued == [(STEADY_1, "reconcile_pass", 25, "pending")]
    assert monday not in rechecked[STALE_LATE]
    assert rechecked[STEADY_1][monday] == public[STEADY_1][monday]


KEPT = "#VPP"


def _tracked(connection_info: str, tag: str) -> bool:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT active FROM players WHERE normalized_tag = %s", (tag,)).fetchone()[0]


def _saved_again(connection_info: str, tag: str, body: bytes, at: datetime) -> int | None:
    """The collector's save of a profile answer read at ``at``, the same as
    the player's last saved one, through its own save and upload path: the
    new response's id, or None when it was not saved."""
    digest = hashlib.sha256(body).hexdigest()
    with psycopg.connect(connection_info) as connection:
        player_id, last_id, last_at = connection.execute(
            "SELECT player_id, id, response_completed_at FROM collector_observations"
            " WHERE normalized_tag = %s AND endpoint = 'profile' ORDER BY id DESC LIMIT 1",
            (tag,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO collector_response_state (
                scope, identity_key, endpoint, player_id, normalized_tag,
                last_response_hash, last_content_fingerprint, last_occurrence_key,
                last_applied_occurrence_key, last_seen_at, last_observation_id,
                last_success_at
            ) VALUES ('player', %s, 'profile', %s, %s, %s, %s, 'last', 'last', %s, %s, %s)
            """,
            (tag, player_id, tag, digest, digest, last_at, last_id, last_at),
        )
    database = CollectorDatabase(connection_info)
    try:
        saved = database.record_response(replace(
            _handoff(occurrence_key=f"{tag}-again", response_hash=digest,
                     player_id=player_id, tag=tag, completed_at=at),
            byte_size=len(body)))
        # The uploader stores it in the archive, where it already is.
        claim = claim_upload(database, owner="uploader")
        assert claim is not None and claim.response_hash == digest
        complete_upload(database, claim, archive_reference=f"s3://evidence/sha256/"
                        f"{digest[:2]}/{digest}", archive_instance_id="fixture-instance")
    finally:
        database.close()
    return saved.observation_id


def test_a_false_lower_answer_saved_again_after_the_display_window_keeps_tracking(
    database_url: str, archive_server, monkeypatch
) -> None:
    """KEPT plays Legend I through Sunday 11 October 2026. At Monday's 05:20
    check the game shows them in Legend II naming Season 0 by mistake, and at
    06:02 the collector saves that unchanged answer again, the first after
    the league display window. The two answers stopped tracking them, so
    polling them, until a battle check put them back (the 12 October
    failure). They battle from 13:30 and are shown in Legend I again at
    22:00. They stay tracked, and Monday is their Legend day, ranked on
    Tuesday's board."""
    saturday, sunday, monday, tuesday = day(6), day(7), day(8), day(9)
    again_at = monday + timedelta(hours=1, minutes=2)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {KEPT: Climber(KEPT)})
        kept = story.players[KEPT]
        # Sunday from 5,140 and Monday from 13:30: 8 attacks of 40 and 8
        # defenses of 30, Monday ending 80 up.
        kept.battles = _saturday(saturday) + attacks(sunday, *[40] * 6) + defenses(
            sunday, *[30] * 8) + attacks(monday + timedelta(hours=8), *[40] * 8) + defenses(
            monday + timedelta(hours=8), *[30] * 8)
        story.reset(saturday, {KEPT: (5300, SEASON)})
        story.reset(sunday, {KEPT: (5140, SEASON)})
        story.reads(monday - timedelta(minutes=10), {KEPT: 5140})
        story.reset(monday, {KEPT: (5140, SEASON)})
        kept.dropped = True
        story.read(KEPT, monday + timedelta(minutes=20), 5140, None)
        story.run_worker()
        tracked = [_tracked(connection_info, KEPT)]
        saved = _saved_again(connection_info, KEPT, kept.profile(5140, None), again_at)
        story.clock = max(story.clock, again_at)
        story.run_worker()
        tracked.append(_tracked(connection_info, KEPT))
        kept.dropped = False
        story.reads(monday + timedelta(hours=17), {KEPT: 5220})
        story.reads(tuesday - timedelta(minutes=10), {KEPT: 5220})
        story.reset(tuesday, {KEPT: (5220, SEASON)})
        story.reads(tuesday + timedelta(minutes=20), {KEPT: 5220})
        story.wait(timedelta(hours=1))
        tracked.append(_tracked(connection_info, KEPT))
        monday_day = _public_days(connection_info, KEPT).get(monday)
        tuesday_board = story.served_board(tuesday)
        unfinished = story.unfinished_jobs()

    assert saved is not None
    assert tracked == [True, True, True]
    assert unfinished == []
    assert monday_day == {"state": "Complete", "reasons": [], "attacks": 8, "defenses": 8}
    assert _ranked(tuesday_board) == {KEPT: 5220}


def test_a_legend_i_player_found_on_thursday_gets_no_monday_from_5000(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Clash Lens first reads FOUND_LATE on Thursday 15 October at 07:25.
    In Legend I for weeks, it began Monday at 5,600 and fought 8 attacks
    and 8 defenses each day, so its 50-row battle log reaches back only to
    Monday 05:30 and shows no Legend battle of the week before. It may have
    been promoted at the Monday Reset or been in Legend I all along, so its
    Monday must not start at 5,000 by the Season rule, 600 short; first read
    on Tuesday, a promoted player's log reaches back past the Monday's start
    (FIRST_SEEN above)."""
    found_late = "#VPP"
    monday, thursday = day(8), day(11)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {found_late: Player(found_late)})
        story.players[found_late].battles = [
            battle for start in (monday, day(9), day(10))
            for battle in attacks(start, *[40] * 8) + defenses(start, *[30] * 8)
        ] + attacks(thursday, 40) + defenses(thursday, 30)
        story.reads(thursday + timedelta(hours=2, minutes=25), {found_late: 5850})
        story.run_worker()
        story.wait(timedelta(hours=1))
        saved = story.saved_day(found_late, monday)
        public = _public_days(connection_info, found_late)

    assert (saved["state"], saved["start"], saved["attacks"], saved["defenses"]) == (
        "Partial", None, 8, 8)
    assert public[monday]["reasons"] == ["missing_start_baseline", "missing_end_baseline"]


def _left_off(connection_info: str, boundary: datetime) -> dict[str, str]:
    """Why the Reset's newest Daily board ranks each member it leaves off."""
    with psycopg.connect(connection_info) as connection:
        return {
            text(tag): text(quality)
            for tag, quality in connection.execute(
                """
                SELECT player.normalized_tag, entry.input_identity->>'snapshot_quality'
                FROM boundary_publication_manifest_entries((
                    SELECT snapshot_manifest_id FROM boundary_publication_generations
                    WHERE boundary_at = %s ORDER BY generation DESC LIMIT 1)) AS entry
                JOIN players AS player ON player.id = entry.player_id
                WHERE entry.input_identity->>'snapshot_quality' <> 'eligible'
                """,
                (boundary,),
            ).fetchall()
        }


GONE, NEVER_FOUND, UNANSWERED, STAYING = "#2LL", "#8LL", "#9LL", "#GLL"


def test_players_the_game_no_longer_finds_are_left_off_as_not_found(
    database_url: str, archive_server, monkeypatch
) -> None:
    """GONE and STAYING play Legend I through Sunday 11 October 2026. From
    the Monday Reset every profile check of GONE's answers 404 (player not
    found), as for 80 tracked players on 12 October, so the record has no
    league for its week of 12 October. NEVER_FOUND's checks never found
    them, as for 59 tracked players all Season, and UNANSWERED's never
    answered. All four are swept at the Monday and Tuesday Resets. A board
    leaves off the members the game does not find as player not found, not
    as league not confirmed, which counts as a day not proven; only
    UNANSWERED's league is not confirmed."""
    saturday, sunday, monday, tuesday = day(6), day(7), day(8), day(9)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {
            tag: Player(tag) for tag in (GONE, NEVER_FOUND, UNANSWERED, STAYING)})
        # Sunday from 5,140 for both; STAYING's Monday from 13:00 ends 80 up.
        for tag in (GONE, STAYING):
            story.players[tag].battles = _saturday(saturday) + attacks(
                sunday, *[40] * 6) + defenses(sunday, *[30] * 8)
        story.players[STAYING].battles += attacks(
            monday + timedelta(hours=8), *[40] * 8) + defenses(
            monday + timedelta(hours=8), *[30] * 8)

        def not_found(tag: str, at: datetime) -> None:
            # The collector saves a 404 once; the same answer again is not saved.
            story.clock = max(story.clock, at)
            store_observation(
                connection_info, archive_server, occurrence_key=f"{tag}-404",
                endpoint="profile", body=f'{{"reason":"notFound","tag":"{tag}"}}'.encode(),
                observed_at=at, normalized_tag=tag, http_status=404,
            )

        not_found(NEVER_FOUND, saturday + timedelta(hours=9))
        story.reset(saturday, {GONE: (5300, SEASON), STAYING: (5300, SEASON)})
        story.reset(sunday, {GONE: (5140, SEASON), STAYING: (5140, SEASON)})
        story.reads(monday - timedelta(minutes=10), {GONE: 5140, STAYING: 5140})
        unread = {tag: (None, SEASON) for tag in (GONE, NEVER_FOUND, UNANSWERED)}
        story.reset(monday, {STAYING: (5140, SEASON), **unread}, run=False,
                    no_log=tuple(unread))
        not_found(GONE, monday + timedelta(minutes=4))
        story.run_worker()
        story.reads(monday + timedelta(minutes=20), {STAYING: 5140})
        story.reads(monday + timedelta(hours=17), {STAYING: 5220})
        story.reads(tuesday - timedelta(minutes=10), {STAYING: 5220})
        story.reset(tuesday, {STAYING: (5220, SEASON), **unread}, no_log=tuple(unread))
        story.reads(tuesday + timedelta(minutes=20), {STAYING: 5220})
        story.wait(timedelta(hours=1))
        boards = {at: (_ranked(story.served_board(at)), _left_off(connection_info, at))
                  for at in (monday, tuesday)}
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    # GONE's first 404 came after the Monday Reset, so it still ranks there.
    assert boards[monday] == ({STAYING: 5140, GONE: 5140}, {
        NEVER_FOUND: "profile_not_found", UNANSWERED: "league_not_confirmed"})
    assert boards[tuesday] == ({STAYING: 5220}, {
        GONE: "profile_not_found", NEVER_FOUND: "profile_not_found",
        UNANSWERED: "league_not_confirmed"})


BOUNCED, IDLE_UP, RETURNED = "#2YY", "#8YY", "#9YY"


def test_players_back_in_legend_i_at_a_monday_reset_start_at_5000_and_rank(
    database_url: str, archive_server, monkeypatch
) -> None:
    """BOUNCED plays Legend I through Sunday 11 October 2026, drops to
    Legend II at the Monday Reset, read in Legend II once that week, naming
    Season 0, and is promoted back at the Monday Reset of 19 October, as 107 players on the
    20 October board were. Its league history, read at 05:34 that Monday,
    holds its Legend II week, labelled 12 October: the Monday that started
    it. Until 22 October 2026 that label was read as the week before, so
    the record found BOUNCED both in Legend I and below in the week of
    5 October, deleted its Sunday and left the 12 October week unknown, and
    its Monday of 19 October started at nothing. From 06:10 it reads Legend I
    at 5,000 naming Season 0 and never plays: its Monday is Complete from
    5,000 to 5,000 and it ranks at 5,000 on the 20 October board.

    IDLE_UP, read twice in Legend II naming the Season the week before, is
    promoted at the Monday Reset of 12 October and from 06:10 reads Legend I
    at 5,000 naming Season 0, never accepted, and never plays, as the 135
    players left off the 15 October board as not Legend I were. The
    Tuesday board ranks it at 5,000, so the Season repair's boards check
    finds no board to rebuild.

    RETURNED plays Legend I on Saturday 10 October, ranking at 5,140 on the
    12 October board, is read in Legend II naming the Season during the week
    of 12 October and is promoted back at the Monday Reset of 19 October,
    then reads Legend I at 5,000 naming Season 0 and never plays: its
    Saturday battle is before its promotion, so the 20 October board ranks
    it at 5,000 too."""
    saturday, sunday, monday, tuesday = day(6), day(7), day(8), day(9)
    back, back_tuesday = day(15), day(16)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {
            tag: Climber(tag) for tag in (BOUNCED, IDLE_UP, RETURNED)})
        bounced, idle_up = story.players[BOUNCED], story.players[IDLE_UP]
        returned = story.players[RETURNED]
        # Sunday from 5,140 ends where it began.
        bounced.battles = _saturday(saturday) + attacks(sunday, *[40] * 6) + defenses(
            sunday, *[30] * 8)
        idle_up.battles = multiplayer(day(4), 3)
        returned.battles = attacks(saturday, 40)
        idle_up.legend_ii = True
        for at, trophies in ((day(4) + timedelta(hours=9), 1400),
                             (saturday + timedelta(hours=9), 1450)):
            story.read(IDLE_UP, at, trophies)
        story.reset(saturday, {BOUNCED: (5300, SEASON), RETURNED: (5100, SEASON)})
        story.reset(sunday, {BOUNCED: (5140, SEASON), RETURNED: (5140, SEASON)})
        story.reads(sunday + timedelta(hours=9, minutes=15), {BOUNCED: 5140})
        story.reads(monday - timedelta(minutes=10), {BOUNCED: 5140})

        # Monday 12 October: BOUNCED reads Legend I naming Season 0 at the
        # Reset and at 05:20, the game's display lagging, then once Legend II
        # naming Season 0; IDLE_UP Legend I at 5,000 naming Season 0.
        story.reset(monday, {BOUNCED: (5140, None)})
        story.reads(monday + timedelta(minutes=20), {BOUNCED: 5140}, None)
        bounced.dropped = True
        story.read(BOUNCED, monday + timedelta(hours=16, minutes=4), 5140, None)
        idle_up.legend_ii = False
        for at in (timedelta(hours=1, minutes=10), timedelta(hours=9, minutes=15),
                   timedelta(hours=23, minutes=50)):
            story.reads(monday + at, {IDLE_UP: 5000}, None)
        returned.legend_ii = True
        for at in (timedelta(hours=9, minutes=15), timedelta(hours=33, minutes=15)):
            story.read(RETURNED, monday + at, 1500)
        story.reset(tuesday, {IDLE_UP: (5000, None)})
        story.reads(tuesday + timedelta(minutes=20), {IDLE_UP: 5000}, None)
        story.wait(timedelta(hours=1))

        # Monday 19 October: BOUNCED is back in Legend I.
        story.clock = max(story.clock, back + timedelta(minutes=34))
        store_observation(
            connection_info, archive_server, occurrence_key=f"{BOUNCED}-history",
            endpoint="league_history", normalized_tag=BOUNCED,
            observed_at=back + timedelta(minutes=34),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(monday.timestamp())), "leagueTrophies": 1800,
                "leagueTierId": 105000035, "placement": 40, "attackWins": 0,
                "attackLosses": 0, "attackStars": 0, "defenseWins": 0, "defenseLosses": 0,
                "defenseStars": 0, "maxBattles": 8,
            }]}).encode(),
        )
        bounced.dropped = returned.legend_ii = False
        for at in (timedelta(hours=1, minutes=10), timedelta(hours=9, minutes=15),
                   timedelta(hours=23, minutes=50)):
            story.reads(back + at, {BOUNCED: 5000, RETURNED: 5000}, None)
        story.reset(back_tuesday, {BOUNCED: (5000, None), RETURNED: (5000, None)})
        story.reads(back_tuesday + timedelta(minutes=20), {BOUNCED: 5000, RETURNED: 5000},
                    None)
        # The 13 October board, which ranked BOUNCED's Monday while its week
        # was carried in Legend I, is rebuilt without it 6 hours on.
        story.wait(timedelta(hours=6))

        sunday_day = _public_days(connection_info, BOUNCED).get(sunday)
        mondays = {BOUNCED: story.saved_day(BOUNCED, back),
                   IDLE_UP: story.saved_day(IDLE_UP, monday)}
        boards = {at: (story.served_board(at), _left_off(connection_info, at))
                  for at in (monday, tuesday, back_tuesday)}
        database = Database(connection_info)
        try:
            rebuilds = queue_board_rebuilds(
                database, str(int(SEASON.timestamp())), queue=False)["boards"]
        finally:
            database.close()
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert sunday_day == {"state": "Complete", "reasons": [], "attacks": 6, "defenses": 8}
    from_5000 = {"state": "Complete", "start": 5000, "end": 5000, "next_start": 5000,
                 "automatic_loss": None, "raise": 0, "attacks": 0, "defenses": 0,
                 "residual": 0}
    assert mondays == {BOUNCED: from_5000, IDLE_UP: from_5000}
    assert {at: (_ranked(board), left_off) for at, (board, left_off) in boards.items()} == {
        monday: ({BOUNCED: 5140, RETURNED: 5140}, {}),
        tuesday: ({IDLE_UP: 5000}, {}),
        back_tuesday: ({BOUNCED: 5000, RETURNED: 5000}, {}),
    }
    assert rebuilds == []


IDLE_ACROSS, ANCHOR = "#QYY", "#RYY"


def _idle_across(story: Story, resets: range) -> None:
    """IDLE_ACROSS, read twice in Legend II naming the Season the week
    before, is promoted into Legend I at the Monday Reset of 12 October 2026,
    still read in Legend II 10 minutes after it, and never plays: each Reset of ``resets`` reads it in Legend I at 5,000
    naming Season 0, as about 115 players promoted on 12 October were read
    from 13 October on. ANCHOR, swept in Legend I naming the Season at every
    Reset, dates the Season."""
    idle = story.players[IDLE_ACROSS]
    if resets.start == 9:
        idle.battles = multiplayer(day(4), 3)
        idle.legend_ii = True
        story.read(IDLE_ACROSS, day(4) + timedelta(hours=9), 1400)
        story.reset(day(6), {ANCHOR: (5300, SEASON)})
        story.read(IDLE_ACROSS, day(6) + timedelta(hours=9), 1450)
        for number in (7, 8):
            story.reset(day(number), {ANCHOR: (5300, SEASON)})
        story.read(IDLE_ACROSS, day(8) + timedelta(minutes=10), 1450)
        idle.legend_ii = False
        for at in (timedelta(hours=1, minutes=10), timedelta(hours=9, minutes=15)):
            story.reads(day(8) + at, {IDLE_ACROSS: 5000}, None)
    for number in resets:
        story.reset(day(number), {IDLE_ACROSS: (5000, None), ANCHOR: (5300, SEASON)})
        for at in (timedelta(minutes=20), timedelta(hours=9, minutes=15)):
            story.reads(day(number) + at, {IDLE_ACROSS: 5000}, None)


def _idle_days(story: Story, days: range) -> dict[int, tuple[object, ...]]:
    """IDLE_ACROSS's Legend days of ``days`` as its page shows them and as
    saved: state, unfinished reasons, start and end."""
    public = _public_days(story.connection_info, IDLE_ACROSS)
    return {number: (public[day(number)]["state"], public[day(number)]["reasons"],
                     saved["start"], saved["end"])
            for number in days
            if (saved := story.saved_day(IDLE_ACROSS, day(number))) is not None}


def test_a_promoted_player_idle_across_the_next_monday_stays_proven(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Until 23 October 2026 each Reset reading of IDLE_ACROSS (``_idle_across``)
    was set aside: "has not played yet" was counted from the Season's start,
    and the Legend II profiles at other totals came before it. The days
    chained from the Monday's 5,000 through Saturday, but Sunday 18 October,
    ending at the next Monday Reset, had no end and Monday 19 October no
    start, so every board from then on showed the player unfinished.
    Counted from the promotion, as Daily boards count it, each reading
    starts its day at 5,000 and every board shows the player's day
    Complete."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Climber(tag) for tag in (IDLE_ACROSS, ANCHOR)})
        _idle_across(story, range(9, 17))
        story.wait(timedelta(hours=1))

        days = _idle_days(story, range(8, 16))
        boards = {number: story.board_members(day(number)).get(IDLE_ACROSS)
                  for number in range(9, 17)}
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert days == {number: ("Complete", [], 5000, 5000) for number in range(8, 16)}
    assert boards == {number: "complete" for number in range(9, 17)}


def test_days_saved_unfinished_before_the_fix_finish_at_the_next_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    """IDLE_ACROSS's days and boards to 20 October were saved by the rule
    before 23 October 2026, counting from the Season's start: Sunday 18
    October and every day after unfinished, as about 115 players' were on
    production: each Reset reading was set aside. From the Reset of 21
    October the rule counts from the promotion. That day's 5,000 start recalculates each day before it with
    no end, back to Sunday, and each past board they change is rebuilt, so
    no Season repair is needed."""
    unplayed = reset_baselines._unplayed
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Climber(tag) for tag in (IDLE_ACROSS, ANCHOR)})
        monkeypatch.setattr(reset_baselines, "_unplayed", lambda *reading: False)
        _idle_across(story, range(9, 17))
        story.wait(timedelta(hours=1))
        before = (_idle_days(story, range(14, 16)),
                  {number: story.board_members(day(number)).get(IDLE_ACROSS)
                   for number in (15, 16)})
        monkeypatch.setattr(reset_baselines, "_unplayed", unplayed)
        _idle_across(story, range(17, 18))
        # Each past board is rebuilt at most once in 6 hours.
        story.wait(timedelta(hours=7))

        days = _idle_days(story, range(8, 17))
        boards = {number: story.board_members(day(number)).get(IDLE_ACROSS)
                  for number in range(9, 18)}
        unfinished = story.unfinished_jobs()

    assert before == ({
        14: ("Partial", ["missing_end_baseline"], 5000, 5000),
        15: ("Partial", ["missing_start_baseline", "missing_end_baseline"], None, None),
    }, {15: "partial", 16: "partial"})
    assert unfinished == []
    assert days == {number: ("Complete", [], 5000, 5000) for number in range(8, 17)}
    assert boards == {number: "complete" for number in range(9, 18)}
