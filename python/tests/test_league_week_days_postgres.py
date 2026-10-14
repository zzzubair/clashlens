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

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from domain_test_support import domain_database, text
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
    in Legend I throughout."""
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
