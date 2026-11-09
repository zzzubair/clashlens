"""What the weekly league record (``league_week``) means for Legend days
and for tracking.

A Legend day exists only in a week the record says was Legend I. The day's
calculation asks ``day_league`` whether its week was, how a Monday's week
began, and which league the week after it was in. A public copy of a day
(``api_player_daily_logs``) names its week's record row, so the database
refuses a public day in any other week; when a decision takes a week out of
Legend I, ``save_season`` deletes the week's public days before saving it,
and queues every day a changed decision affects. Whether a player is
tracked, so polled and swept at each Reset, follows the current week's
decision (``track``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from . import domain, league_week
from .db import PYTHON_BACKFILL_PRIORITY
from .domain import RankedDay
from .league_week import (
    WEEK,
    Decision,
    Entry,
    League,
    LegendI,
    Strength,
    Unknown,
    league_of,
)

_STARTS_AT_5000 = frozenset({Entry.SEASON_START, Entry.PROMOTED, Entry.FIRST_SEEN})


@dataclass(frozen=True, slots=True)
class DayLeague:
    """The record's answer for one Legend day. ``league`` is its week's,
    unknown when the week has no record; ``entry`` how the week began, set
    for its Monday only; ``following`` the league of the week starting at
    the day's end, set when the day ends at a Monday Reset."""

    league: League
    entry: Entry | None
    following: League | None

    @property
    def legend_day(self) -> bool:
        return self.league is League.LEGEND_I

    @property
    def starts_at_5000(self) -> bool:
        """A Season's first Monday, or a Monday the player was promoted at:
        the game starts the day at 5,000."""
        return self.entry in _STARTS_AT_5000

    @property
    def promoted(self) -> bool:
        return self.entry in {Entry.PROMOTED, Entry.FIRST_SEEN}

    @property
    def after_legend_day(self) -> bool:
        """Whether the day before was a Legend day of the player's: not for
        a Monday they were promoted at, or one after a week not known."""
        return self.entry not in {Entry.PROMOTED, Entry.FIRST_SEEN, Entry.UNKNOWN}

    @property
    def chains(self) -> bool:
        """Whether the day before's end can start the day: it was a Legend
        day of the same run in Legend I."""
        return self.entry in {None, Entry.STAYED}

    @property
    def dropped(self) -> bool:
        """The day ends at a Monday Reset where the player left Legend I:
        no weekly raise to 5,000 follows it."""
        return self.following is League.BELOW


def day_league(connection: Any, player_id: int, day: RankedDay, *, lock: bool = True) -> DayLeague:
    """The record's answer for ``day``. With ``lock``, as the day's
    calculation asks, its week's row stays as read until the day's
    transaction ends: a decision taking the week out of Legend I waits for
    it, then deletes the day it saved (``save_season``). The re-check asks
    without, in its read-only snapshot (``reconcile_pass``)."""
    week = league_week.week_start(day.start)
    ends_week = day.end.weekday() == 0
    weeks = [start for start in ([week, week + WEEK] if ends_week else [week])
             if start >= league_week.RECORDED_FROM]
    rows = {
        row[0]: (League(row[1]), Entry(row[2]) if row[2] else None)
        for row in connection.execute(
            f"""
            SELECT week_start, league, entry FROM player_league_weeks
            WHERE player_id = %s AND week_start = ANY(%s)
            {"FOR KEY SHARE" if lock else ""}
            """,
            (player_id, weeks),
        ).fetchall()
    } if weeks else {}
    unknown = (League.UNKNOWN, None)
    before_record = (League.LEGEND_I, None)
    league, entry = rows.get(week, unknown) if week >= league_week.RECORDED_FROM else before_record
    if week < league_week.RECORDED_FROM and day.start == day.season_start:
        entry = Entry.SEASON_START
    return DayLeague(
        league,
        entry if day.start == week else None,
        rows.get(week + WEEK, unknown if week + WEEK >= league_week.RECORDED_FROM else before_record)[0]
        if ends_week else None,
    )


def save_season(
    database: Any, connection: Any, player_id: int, season_start: datetime,
    decisions: Mapping[datetime, Decision], now: datetime, *, queue_days: bool,
) -> set[datetime]:
    """``league_week.save_season``, keeping days in step: lock each week
    leaving Legend I, so a day being saved in it finishes first and none
    starts, delete its public days (the database refuses the new decision
    while they exist), then save, then, with ``queue_days``, queue the days
    each changed league or entry affects. No other week is locked against
    day calculations, so a Reset's days never wait for a decision that
    keeps their week's league. Only the decider writes the record, one batch
    at a time, so the weeks read stay as read."""
    end = season_start + domain.SEASON_DURATION
    before = {
        row[0]: (League(row[1]), row[2])
        for row in connection.execute(
            """
            SELECT week_start, league, entry FROM player_league_weeks
            WHERE player_id = %s AND week_start >= %s AND week_start < %s
            """,
            (player_id, season_start, end),
        ).fetchall()
    }
    after = {
        week: (league_of(decision), decision.entry.value if isinstance(decision, LegendI) else None)
        for week in before.keys() | decisions.keys()
        for decision in (decisions.get(week, Unknown()),)
    }
    leaving = sorted(
        week for week, (league, _) in after.items()
        if league is not League.LEGEND_I and before.get(week, (None,))[0] is not league
    )
    connection.execute(
        """
        SELECT 1 FROM player_league_weeks
        WHERE player_id = %s AND week_start = ANY(%s) FOR UPDATE
        """,
        (player_id, leaving),
    )
    withdraw(database, connection, player_id, leaving)
    changed = league_week.save_season(connection, player_id, season_start, decisions)
    _leave_boards(database, connection, player_id, leaving)
    moved = {week for week in changed if before.get(week) != after[week]}
    if queue_days and moved:
        numbers = dict(connection.execute(
            """
            SELECT week_start, decision_number FROM player_league_weeks
            WHERE player_id = %s AND week_start = ANY(%s)
            """,
            (player_id, sorted(moved)),
        ).fetchall())
        for week in sorted(moved):
            _queue_week(connection, player_id, week, before.get(week, (None, None)),
                        after[week], int(numbers[week]), now)
    if season_start <= now < end:
        track(connection, player_id, now)
    return changed


class Tracking(StrEnum):
    """Whether a player is tracked (polled, and swept at each Reset) and
    why, saved as ``players.active``, ``next_due_at`` (set while tracked)
    and ``eligibility_state``.

    Tracked: LEGEND_I, the current week's record says Legend I;
    UNSETTLED_AFTER_LEGEND_I, after a Legend I week this week is not known,
    or below only by answers short of an accepted one (Season 0 answers may
    be the game's mistake); NEWEST_ELIGIBLE, the newest recognised profile
    shows Legend I, so a player just promoted is polled before the record
    says so. Stopped: BELOW, the newest recognised profile is below Legend
    I; UNKNOWN, no recognised answer yet, so eligibility is asked again.

    Events: each weekly decision saved for the current Season
    (``save_season``) calls ``track``, which derives the state from those
    rows (``tracking``) and writes it. A player's row starts tracked only
    when first saved from a profile showing Legend I (``ingestion``), else
    UNKNOWN; after that only ``track`` changes it.
    Stopping also cancels their ordinary discovery work. A later promotion
    or accepted answer saves a decision again, so a stopped player is
    tracked again. It is derived afresh each time, so a failed or retried
    call leaves the last written state, and a restart changes nothing until
    the next decision.
    """

    LEGEND_I = "legend_i"
    UNSETTLED_AFTER_LEGEND_I = "unsettled_after_legend_i"
    NEWEST_ELIGIBLE = "newest_eligible"
    BELOW = "below"
    UNKNOWN = "unknown"

    @property
    def tracked(self) -> bool:
        return self not in {Tracking.BELOW, Tracking.UNKNOWN}

    @property
    def eligibility_state(self) -> str:
        """The saved ``players.eligibility_state``."""
        return ("eligible" if self.tracked
                else "ineligible" if self is Tracking.BELOW else "unknown")


def tracking(
    this_week: str, strength: int, last_week: str | None, newest: str | None,
) -> Tracking:
    """The tracking state from this week's record row (its league and
    evidence strength, unknown with none), last week's league (None with no
    row) and the newest recognised profile's ``eligibility_state`` (None
    with none)."""
    unsettled = this_week == League.UNKNOWN.value or (
        this_week == League.BELOW.value and strength < Strength.ACCEPTED)
    if this_week == League.LEGEND_I.value:
        return Tracking.LEGEND_I
    if unsettled and last_week == League.LEGEND_I.value:
        return Tracking.UNSETTLED_AFTER_LEGEND_I
    if newest == "eligible":
        return Tracking.NEWEST_ELIGIBLE
    return Tracking.BELOW if newest is not None else Tracking.UNKNOWN


def track(connection: Any, player_id: int, now: datetime) -> None:
    """Track the player while the record says they are in Legend I this
    week, or, after a Legend I week, this week is not known or below only by
    lower-league answers naming Season 0, or their newest saved answer shows
    Legend I, so a player just promoted is polled at once. Lower-league
    answers naming Season 0 may be the game's mistake, as one shown at a
    Monday's settlement check and saved again after the league display
    window: polling goes on until an accepted answer, a battle or the league
    history settles the week.
    Otherwise stop tracking them and cancel their ordinary discovery work,
    as for any player found below Legend I; one who never gave a recognised
    league answer stays of unknown eligibility, so is asked again."""
    week = league_week.week_start(now)
    weeks = {row[0]: (row[1], row[2]) for row in connection.execute(
        """
        SELECT week_start, league, strength FROM player_league_weeks
        WHERE player_id = %s AND week_start = ANY(%s)
        """,
        (player_id, [week - WEEK, week]),
    ).fetchall()}
    record = weeks.get(week, (League.UNKNOWN.value, Strength.NONE))
    last_week = weeks.get(week - WEEK, (None,))[0]
    state = tracking(*record, last_week, None)
    if not state.tracked:
        # Every saved profile of the player, so asked only when the record
        # does not settle it: on 8 November 2026 this read was 19% of the
        # database's disk reads, about 400 pages a call, nearly all for
        # Legend I players.
        newest = connection.execute(
            """
            SELECT version.eligibility_state
            FROM players AS player
            JOIN player_profile_versions AS version
              ON version.normalized_tag = player.normalized_tag
            LEFT JOIN LATERAL (
                SELECT max(seen.observed_at) AS read_at FROM player_profile_effects AS seen
                WHERE seen.profile_version_id = version.id
            ) AS seen ON true
            WHERE player.id = %s AND version.eligibility_state IN ('eligible', 'ineligible')
            ORDER BY GREATEST(version.observed_at, seen.read_at) DESC, version.id DESC
            LIMIT 1
            """,
            (player_id,),
        ).fetchone()
        state = tracking(*record, last_week, None if newest is None else newest[0])
    connection.execute(
        """
        UPDATE players
        SET active = %(tracked)s,
            next_due_at = CASE WHEN %(tracked)s
                THEN COALESCE(next_due_at, clock_timestamp()) END,
            eligibility_state = %(state)s,
            updated_at = clock_timestamp()
        WHERE id = %(player)s
          AND (active, eligibility_state, next_due_at IS NOT NULL)
              IS DISTINCT FROM (%(tracked)s, %(state)s, %(tracked)s)
        """,
        {"tracked": state.tracked, "player": player_id, "state": state.eligibility_state},
    )
    if not state.tracked:
        connection.execute("SELECT clashlens_cancel_inactive_discovery_work(%s)", (player_id,))


def _queue_week(
    connection: Any, player_id: int, week: datetime, before: tuple, after: tuple,
    decision_number: int, now: datetime,
) -> None:
    """Queue the started days a changed decision about ``week`` affects:
    every day of the week when its league changed, and the Sunday before
    too when that changes how the Sunday ends: within a Season, when the
    week moved into or out of below Legend I, as only a player staying in
    Legend I gets the weekly raise; at a Season's start, when it moved into
    or out of Legend I, as only a player in Legend I the next Season ends
    the last day by the Season rule. Only its Monday when only how it began
    changed. A week out of Legend I before and after has no day saved, and a
    Sunday not a Legend day has none to change: each lower-league player
    with evidence in a new week would otherwise queue a job at Reset
    priority on Monday for nothing."""
    from . import day_recalculation

    if before[0] is not after[0]:
        first, last = week, week + WEEK - timedelta(days=1)
        if League.LEGEND_I not in (before[0], after[0]):
            last = week - timedelta(days=1)
        ends_sunday = League.LEGEND_I if domain.is_season_boundary(week) else League.BELOW
        if (before[0] is ends_sunday) is not (after[0] is ends_sunday) and day_league(
            connection, player_id, domain.ranked_day_for(week - timedelta(days=1))
        ).legend_day:
            first -= timedelta(days=1)
    else:
        first = last = week
    last = min(last, domain.ranked_day_for(now).start)
    if first > last or last < league_week.RECORDED_FROM:
        return
    first = max(first, league_week.RECORDED_FROM)
    day_recalculation.request(
        connection, player_id, first, None,
        key=f"reconcile:league-week:{player_id}:{week:%Y-%m-%dT%H:%M:%SZ}:{decision_number}",
        trigger="league_week", last_day=last, every_day=True,
        priority=None if last >= domain.ranked_day_for(now).start - timedelta(days=1)
        else PYTHON_BACKFILL_PRIORITY,
    )


def withdraw(database: Any, connection: Any, player_id: int, weeks: list[datetime]) -> int:
    """Delete the public days of ``weeks``, which are not Legend I weeks:
    a below Legend I or unknown week has no Legend days. The saved results
    stay as history. Returns how many public copies went."""
    weeks = [week for week in weeks if week >= league_week.RECORDED_FROM]
    if not weeks:
        return 0
    deleted = connection.execute(
        """
        DELETE FROM api_player_daily_logs AS day
        USING unnest(%s::timestamptz[]) AS week (start)
        WHERE day.player_id = %s AND day.ranked_day_start >= week.start
          AND day.ranked_day_start < week.start + interval '7 days'
        RETURNING day.official_season_id, day.ranked_day_start
        """,
        (weeks, player_id),
    ).fetchall()
    _refresh_summaries(connection, player_id, {row[0] for row in deleted})
    return len(deleted)


def _leave_boards(database: Any, connection: Any, player_id: int, weeks: list[datetime]) -> None:
    """Once ``weeks`` are saved out of Legend I, take the player off each
    board ranking a day of them, saved or not (``boundary.remove_league_members``)."""
    from .boundary import remove_league_members

    remove_league_members(database, connection, player_id, [
        week + timedelta(days=offset)
        for week in weeks if week >= league_week.RECORDED_FROM for offset in range(7)])


def _refresh_summaries(connection: Any, player_id: int, seasons: set[Any]) -> None:
    """Store again each saved Season summary those days were in."""
    from .season_lifecycle import acquire_player_season_lock
    from .season_summaries import materialize_player_season

    for season_id in sorted(str(season) for season in seasons if season not in {None, "unknown"}):
        acquire_player_season_lock(connection, player_id, season_id)
        if connection.execute(
            "SELECT 1 FROM player_season_summaries WHERE player_id = %s AND official_season_id = %s",
            (player_id, season_id),
        ).fetchone():
            materialize_player_season(connection, player_id=player_id, season_id=season_id)


def settle(
    database: Any, connection: Any, season_start: datetime, limit: int,
) -> dict[str, int]:
    """For the Season repair, once every week of the Season is decided: the
    public days of at most ``limit`` players saved before their weeks were.
    Each in a week not Legend I is deleted (``withdraw``); each other names
    its week (migration 0118). How many players, deleted and named."""
    start = max(season_start, league_week.RECORDED_FROM)
    end = season_start + domain.SEASON_DURATION
    players = [int(row[0]) for row in connection.execute(
        """
        SELECT DISTINCT player_id FROM api_player_daily_logs
        WHERE ranked_day_start >= %s AND ranked_day_start < %s AND week_start IS NULL
        ORDER BY player_id LIMIT %s
        """,
        (start, end, limit),
    ).fetchall()]
    deleted = named = 0
    for player_id in players:
        legend_i = {row[0] for row in connection.execute(
            """
            SELECT week_start FROM player_league_weeks
            WHERE player_id = %s AND week_start >= %s AND week_start < %s
              AND league = 'legend_i'
            FOR KEY SHARE
            """,
            (player_id, start, end),
        ).fetchall()}
        weeks = {row[0] for row in connection.execute(
            """
            SELECT DISTINCT clashlens_week_start(ranked_day_start) FROM api_player_daily_logs
            WHERE player_id = %s AND ranked_day_start >= %s AND ranked_day_start < %s
              AND week_start IS NULL
            """,
            (player_id, start, end),
        ).fetchall()}
        deleted += withdraw(database, connection, player_id, sorted(weeks - legend_i))
        named += connection.execute(
            """
            UPDATE api_player_daily_logs
            SET week_start = clashlens_week_start(ranked_day_start), league = 'legend_i'
            WHERE player_id = %s AND ranked_day_start >= %s AND ranked_day_start < %s
              AND week_start IS NULL
            """,
            (player_id, start, end),
        ).rowcount
    return {"players_settled": len(players), "public_days_deleted": deleted,
            "public_days_named": named}
