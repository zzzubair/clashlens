"""End-to-end: what each Reset's Daily board does, pinned as a transcript.

Each story saves the game data the collector would have saved, runs the real
worker, its job handlers and the operator commands against PostgreSQL, and
pins the board's history (``board_transcript``): every version, its members
by status, rebuild requests, frozen input lists, identities, events and build
jobs. The board code is moving behind one owner; these stories must read the
same before and after each move, so a changed answer here is a changed
behavior to explain. See docs/testing.md.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import psycopg
from board_later_reading_postgres_support import _hold
from domain_test_support import domain_database, store_observation
from league_week_days_postgres_support import _decide_below
from reset_scenarios_postgres_support import (
    PREVIOUS_SEASON,
    SEASON,
    THROWER,
    Battle,
    _ranked,
    _saturday,
    _story,
    attacks,
    board_transcript,
    day,
    defenses,
)
from weekly_raise_postgres_support import LATE_RAISE, LOSS_TOO, _instant_raise

from clashlens import (
    board_deadline,
    boundary,
    boundary_manifest,
    reading_rule,
    reconciliation,
)
from clashlens.db import Database
from clashlens.domain_repair import season_repair

A, B, NEW = "#2PP", "#8PP", "#2QQ"
SEASON_ID = str(int(SEASON.timestamp()))


def _at(hour: int, minute: int = 0) -> datetime:
    """A time on the morning the stories' Legend day ends."""
    return day(3).replace(hour=hour, minute=minute)


def _short(transcript: dict[str, list]) -> dict[str, list]:
    """The versions and rebuild requests of a transcript, for the steps
    between a story's start and its end."""
    return {"versions": transcript["versions"], "rebuilds": transcript["rebuilds"]}


def _operator(command, connection_info: str, *args, **kwargs):
    """Run an operator command as the operator's shell does."""
    database = Database(connection_info)
    try:
        return command(database, *args, **kwargs)
    finally:
        database.close()


def _one_day(story, *tags: str) -> None:
    """Each of ``tags`` attacks once for 40 from 5,100 + its index, read at
    04:50, and the day ends at that total."""
    start, end = day(2), day(3)
    first = {tag: 5100 + index for index, tag in enumerate(tags)}
    for tag in tags:
        story.players[tag].battles = attacks(start, 40)
    story.reset(start, {tag: (first[tag], SEASON) for tag in tags})
    story.reads(_at(4, 50), {tag: first[tag] + 40 for tag in tags})
    story.reset(end, {tag: (first[tag] + 40, SEASON) for tag in tags}, run=False)


def _changed_after_freeze(story) -> None:
    """A and B attack once for 40 from 5,100 and 5,101. A's last attack, at
    04:58, misses A's Reset battle log, which the collector gave up on, so
    the first board ranks A by its 04:50 reading against a day of one attack;
    a battle log read at 05:40 shows it, and A's day is saved again."""
    start, end = day(2), day(3)
    story.players[A].battles = attacks(start, 40) + [Battle(_at(4, 58), 40, True)]
    story.players[B].battles = attacks(start, 40)
    story.reset(start, {A: (5100, SEASON), B: (5101, SEASON)})
    story.reads(_at(4, 50), {A: 5140, B: 5141})
    story.reset(end, {A: (5180, SEASON), B: (5141, SEASON)}, no_log=(A,))


def test_a_day_saved_again_after_the_freeze_rebuilds_at_the_hourly_pace(
    database_url: str, archive_server, monkeypatch
) -> None:
    end = day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B)
        _changed_after_freeze(story)
        seen = {"first": _short(board_transcript(info, end))}
        story.read(A, _at(5, 40))
        story.run_worker()
        seen["saved again"] = _short(board_transcript(info, end))
        story.wait(timedelta(hours=1))
        seen["an hour later"] = board_transcript(info, end)
        seen["board"] = _ranked(story.served_board(end))
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "first": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "saved again": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['army', 'snapshot'], [], 1, None, 1)],
        },
        "an hour later": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (1, 'terminal', 'partial', 'partial', 1),
                (2, 'terminal', 'complete', 'complete', 1),
                (2, 'terminal', 'partial', 'partial', 1),
            ],
            "rebuilds": [('finalized', ['army', 'snapshot'], [], 1, 2, 1)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Partial', [5140, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Partial', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 3, 'Partial', [5140, -10, 'eligible']),
                ('snapshot@2:m3', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@2:m4', '#2PP', 3, 'Partial', ['u1x0-2x1', 'u1x0-2x1']),
                ('army@2:m4', '#8PP', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 2, 'army@2:m4'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m4', 'complete'),
            ],
        },
        "board": {"#8PP": 5141, "#2PP": 5140},
    }


def test_a_late_reset_reading_on_a_frozen_board_rebuilds_at_the_hourly_pace(
    database_url: str, archive_server, monkeypatch
) -> None:
    """PR 632's case: B's Reset responses come after the board froze with
    B's day unfinished from the 04:50 reads."""
    end = day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B)
        _one_day(story, A, B)
        _hold(info, (B,), end)
        story.run_round()
        seen = {"first": _short(board_transcript(info, end)),
                "B before": story.saved_day(B, day(2))["state"]}
        story.run_worker()
        seen["B after"] = story.saved_day(B, day(2))["state"]
        seen["arrived"] = _short(board_transcript(info, end))
        story.wait(timedelta(hours=1))
        seen["an hour later"] = board_transcript(info, end)
        board = story.served_board(end)
        seen["board"] = {entry["tag"]: f"{entry['trophies']} {entry['confidence']}"
                         for entry in board["entries"]}
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "first": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "B before": "Partial",
        "B after": "Complete",
        "arrived": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['snapshot'], [], 1, None, 1)],
        },
        "an hour later": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['snapshot'], 1, 'snapshot@2:m3', 'army@1:m2', 2, 1),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (1, 'terminal', 'partial', 'partial', 1),
                (2, 'terminal', 'complete', 'complete', 2),
            ],
            "rebuilds": [('finalized', ['snapshot'], [], 1, 2, 1)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Partial', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', 2, 'Partial', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('snapshot@2:m3', '#8PP', 3, 'Complete', [5141, -10, 'eligible']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
            ],
        },
        "board": {"#8PP": "5141 confirmed", "#2PP": "5140 confirmed"},
    }


def test_a_new_player_joins_at_the_hourly_rebuild(
    database_url: str, archive_server, monkeypatch
) -> None:
    """NEW, not in the Reset sweep, is first read after the board froze."""
    start, end = day(2), day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, NEW)
        for tag in (A, NEW):
            story.players[tag].battles = attacks(start, 40)
        story.reset(start, {A: (5100, SEASON)})
        story.reads(_at(4, 50), {A: 5140})
        story.reset(end, {A: (5140, SEASON)})
        seen = {"first": _short(board_transcript(info, end))}
        story.reads(_at(4, 50), {NEW: 5142})
        story.run_worker()
        seen["NEW saved"] = _short(board_transcript(info, end))
        story.wait(timedelta(hours=1))
        seen["an hour later"] = board_transcript(info, end)
        seen["board"] = _ranked(story.served_board(end))
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "first": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "NEW saved": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['army', 'snapshot'], [], 1, None, 1)],
        },
        "an hour later": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (2, 'terminal', 'complete', 'complete', 1),
                (2, 'terminal', 'partial', 'partial', 1),
            ],
            "rebuilds": [('finalized', ['army', 'snapshot'], [], 1, 2, 1)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('snapshot@2:m3', '#2QQ', 1, 'Partial', [5142, -10, 'eligible']),
                ('army@2:m4', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m4', '#2QQ', 1, 'Partial', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 2, 'army@2:m4'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m4', 'complete'),
            ],
        },
        "board": {"#2QQ": 5142, "#2PP": 5140},
    }


def test_the_0530_and_0600_board_deadlines(
    database_url: str, archive_server, monkeypatch, fixed_clock
) -> None:
    """Under the production deadlines, on a clock that stands at each
    moment: day 3's board passes 05:30 with B unfinished; day 4's passes
    05:30 with two unfinished, above the limit (1 here, 50 in production),
    and then 06:00."""
    deadline = board_deadline.BOARD_DEADLINE
    start, end, next_end = day(2), day(3), day(4)
    with domain_database(database_url, clock=fixed_clock) as info:
        story = _story(info, archive_server, monkeypatch, A, B, NEW, wall_clock=fixed_clock)
        monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", deadline)
        for tag in (A, B, NEW):
            story.players[tag].battles = attacks(start, 40) + attacks(end, 40)
        story.reset(start, {A: (5100, SEASON), B: (5101, SEASON), NEW: (5102, SEASON)})
        story.reset(end, {A: (5140, SEASON), B: (5141, SEASON), NEW: (5142, SEASON)},
                    run=False)
        _hold(info, (B,), end)

        def at(moment: datetime) -> None:
            story._at(moment)
            story.run_round()

        at(end + timedelta(minutes=29))
        seen = {"05:29": _short(board_transcript(info, end))}
        at(end + timedelta(minutes=30))
        seen["05:30 cut"] = board_transcript(info, end)
        story.run_worker()
        seen["05:30 back"] = _short(board_transcript(info, end))
        story.reset(next_end, {A: (5180, SEASON), B: (5181, SEASON), NEW: (5182, SEASON)},
                    run=False)
        _hold(info, (B, NEW), next_end)
        monkeypatch.setattr(board_deadline, "MAX_UNFINISHED", 1)
        at(next_end + timedelta(minutes=59))
        seen["05:59 holding"] = board_transcript(info, next_end)
        at(next_end + timedelta(hours=1))
        seen["06:00 cut"] = board_transcript(info, next_end)
        story.run_worker()
        seen["06:00 back"] = board_transcript(info, next_end)
        seen["boards"] = [_ranked(story.served_board(at)) for at in (end, next_end)]
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "05:29": {
            "versions": [(1, 'pending', 'pending', 'none', [], None, None, None, None, None)],
            "rebuilds": [],
        },
        "05:30 cut": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 2),
                (1, 'unavailable', 'unavailable', 'unavailable', 1),
                (2, 'terminal', 'complete', 'complete', 2),
                (2, 'terminal', 'partial', 'partial', 1),
            ],
            "rebuilds": [('finalized', ['army', 'snapshot'], ['arrival'], 1, 2, 1)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5100, -1436, 'eligible']),
                ('snapshot@1:m1', '#8PP', None, 'Unavailable', [5101, -1436, 'unavailable']),
                ('snapshot@1:m1', '#2QQ', 2, 'Complete', [5102, -1436, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', None, 'Unavailable', []),
                ('army@1:m2', '#2QQ', 2, 'Complete', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5100, -1436, 'eligible']),
                ('snapshot@2:m3', '#8PP', 2, 'Partial', [5101, -1436, 'eligible']),
                ('snapshot@2:m3', '#2QQ', 2, 'Complete', [5102, -1436, 'eligible']),
                ('army@2:m4', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m4', '#8PP', 2, 'Partial', []),
                ('army@2:m4', '#2QQ', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 2, 'army@2:m4'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m4', 'complete'),
            ],
        },
        "05:30 back": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "rebuilds": [
                ('finalized', ['army', 'snapshot'], ['arrival'], 1, 2, 1),
                ('queued', ['army', 'snapshot'], [], 2, None, 1),
            ],
        },
        "05:59 holding": {
            "versions": [(1, 'pending', 'pending', 'none', [], None, None, None, None, None)],
            "members": [
                (1, 'pending', 'pending', 'pending', 2),
                (1, 'terminal', 'complete', 'complete', 1),
            ],
            "rebuilds": [],
            "frozen": [],
            "identities": [],
            "events": [],
            "jobs": [],
        },
        "06:00 cut": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (1, 'unavailable', 'unavailable', 'unavailable', 2),
            ],
            "rebuilds": [],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5140, -1436, 'eligible']),
                ('snapshot@1:m1', '#8PP', None, 'Unavailable', [5141, -1436, 'unavailable']),
                ('snapshot@1:m1', '#2QQ', None, 'Unavailable', [5142, -1436, 'unavailable']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', None, 'Unavailable', []),
                ('army@1:m2', '#2QQ', None, 'Unavailable', []),
            ],
            "identities": [('analytics', 1, 'snapshot@1:m1'), ('army', 1, 'army@1:m2')],
            "events": [(1, None)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-08T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-08T05:00:00Z:gen:1:manifest:m2', 'complete'),
            ],
        },
        "06:00 back": {
            "versions": [
                (1, 'superseded', 'superseded', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
                (3, 'published', 'published', 'finalized', ['army', 'snapshot'], 2, 'snapshot@3:m5', 'army@3:m6', 3, 3),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (1, 'unavailable', 'unavailable', 'unavailable', 2),
                (2, 'terminal', 'complete', 'complete', 2),
                (2, 'unavailable', 'unavailable', 'unavailable', 1),
                (3, 'terminal', 'complete', 'complete', 3),
            ],
            "rebuilds": [
                ('finalized', ['army', 'snapshot'], [], 1, 2, 0),
                ('finalized', ['army', 'snapshot'], ['arrival'], 2, 3, 1),
            ],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5140, -1436, 'eligible']),
                ('snapshot@1:m1', '#8PP', None, 'Unavailable', [5141, -1436, 'unavailable']),
                ('snapshot@1:m1', '#2QQ', None, 'Unavailable', [5142, -1436, 'unavailable']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', None, 'Unavailable', []),
                ('army@1:m2', '#2QQ', None, 'Unavailable', []),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5140, -1436, 'eligible']),
                ('snapshot@2:m3', '#8PP', 2, 'Complete', [5141, -1436, 'eligible']),
                ('snapshot@2:m3', '#2QQ', None, 'Unavailable', [5142, -1436, 'unavailable']),
                ('army@2:m4', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m4', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m4', '#2QQ', None, 'Unavailable', []),
                ('snapshot@3:m5', '#2PP', 2, 'Complete', [5140, -1436, 'eligible']),
                ('snapshot@3:m5', '#8PP', 2, 'Complete', [5141, -1436, 'eligible']),
                ('snapshot@3:m5', '#2QQ', 2, 'Complete', [5142, -1436, 'eligible']),
                ('army@3:m6', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@3:m6', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@3:m6', '#2QQ', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 2, 'army@2:m4'),
                ('analytics', 3, 'snapshot@3:m5'),
                ('army', 3, 'army@3:m6'),
            ],
            "events": [(1, None), (2, 1), (3, 2)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-08T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-08T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-08T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-08T05:00:00Z:gen:2:manifest:m4', 'complete'),
                ('build_snapshot:boundary:2026-10-08T05:00:00Z:gen:3:manifest:m5', 'complete'),
                ('build_analytics:snapshot:#:v3:input:h3', 'complete'),
                ('build_army_analytics:boundary:2026-10-08T05:00:00Z:gen:3:manifest:m6', 'complete'),
            ],
        },
        "boards": [
            {"#2QQ": 5142, "#8PP": 5141, "#2PP": 5140},
            {"#2QQ": 5182, "#8PP": 5181, "#2PP": 5180},
        ],
    }


def test_a_player_leaving_legend_i_is_rebuilt_off_the_board(
    database_url: str, archive_server, monkeypatch
) -> None:
    end = day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B)
        _one_day(story, A, B)
        story.run_worker()
        seen = {"first": _short(board_transcript(info, end))}
        story.decides = False
        _decide_below(info, (B,), story.clock)
        story.run_worker()
        seen["B below"] = _short(board_transcript(info, end))
        story.wait(timedelta(hours=1))
        seen["an hour later"] = board_transcript(info, end)
        seen["board"] = _ranked(story.served_board(end))
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "first": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "B below": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['army', 'snapshot'], [], 1, None, 0)],
        },
        "an hour later": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 2),
                (2, 'terminal', 'complete', 'complete', 1),
            ],
            "rebuilds": [('finalized', ['army', 'snapshot'], [], 1, 2, 0)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('army@2:m4', '#2PP', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 2, 'army@2:m4'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m4', 'complete'),
            ],
        },
        "board": {"#2PP": 5140},
    }


def test_an_army_decode_change_after_the_army_froze(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A's defense was by THROWER's army; a battle log read at 05:50 shows
    the same defense with another army."""
    start, end = day(2), day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B)
        story.players[A].battles = attacks(start, 40) + defenses(start, 30, army=THROWER)
        story.players[B].battles = attacks(start, 40)
        story.reset(start, {A: (5100, SEASON), B: (5101, SEASON)})
        story.reads(_at(4, 50), {A: 5110, B: 5141})
        story.reset(end, {A: (5110, SEASON), B: (5141, SEASON)})
        seen = {"first": _short(board_transcript(info, end))}
        story.players[A].battles = [
            replace(battle, army="u1x58") if battle.army == THROWER else battle
            for battle in story.players[A].battles]
        story.read(A, _at(5, 50))
        story.run_worker()
        seen["read again"] = _short(board_transcript(info, end))
        story.wait(timedelta(hours=1))
        seen["an hour later"] = board_transcript(info, end)
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "first": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "read again": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['army'], ['decode'], 1, None, 1)],
        },
        "an hour later": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army'], 1, 'snapshot@1:m1', 'army@2:m3', 1, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 2),
                (2, 'terminal', 'complete', 'complete', 2),
            ],
            "rebuilds": [('finalized', ['army'], ['decode'], 1, 2, 1)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5110, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1', 'u9x132-4x7']),
                ('army@1:m2', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m3', '#2PP', 3, 'Complete', ['u1x0-2x1', 'u1x58']),
                ('army@2:m3', '#8PP', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('army', 2, 'army@2:m3'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
            ],
        },
    }


def test_the_army_correction_listing_during_an_army_only_rebuild(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Suspected bug (c): ``queue_army_corrections`` takes a Reset's newest
    version whose army is not replaced, other paths the newest with neither
    side replaced. A's defense by THROWER is published as unread, as Resets
    frozen during the unit catalogue v3 re-read were (the one state written
    by hand here, as test_army_corrections_postgres does). The listing finds
    it, queues one army-only rebuild and, while it builds, names the version
    being rebuilt: the newest, as the other paths would, since today no path
    replaces one side of a version alone. The hand-written state leaves each
    battle's inputs as they were, so the rebuild keeps the unread record and
    the Reset is listed again afterwards."""
    start, end = day(2), day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B)
        story.players[A].battles = attacks(start, 40) + defenses(start, 30, army=THROWER)
        story.players[B].battles = attacks(start, 40)
        story.reset(start, {A: (5100, SEASON), B: (5101, SEASON)})
        story.reads(_at(4, 50), {A: 5110, B: 5141})
        story.reset(end, {A: (5110, SEASON), B: (5141, SEASON)})
        with psycopg.connect(info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                "UPDATE army_analytics_battle_facts SET army_state = 'decode_missing'"
                " WHERE is_current AND lens = 'defense'")

        def listed(queue: bool = False) -> list:
            return _operator(boundary.queue_army_corrections, info, SEASON_ID,
                             queue=queue, max_jobs=10)["resets"]

        seen = {"listed": listed(), "queued": listed(queue=True),
                "after queueing": _short(board_transcript(info, end))}
        story.elapsed += timedelta(hours=1)
        story.run_round(skip=("build_army_analytics",))
        seen["rebuilding"] = _short(board_transcript(info, end))
        seen["listed while rebuilding"] = listed()
        story.run_worker()
        seen["rebuilt"] = board_transcript(info, end)
        seen["listed when rebuilt"] = listed()
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "listed": [
            {
                "boundary_at": "2026-10-07T05:00:00+00:00",
                "generation": 1,
                "sides": 1,
                "correction": "not_queued",
            },
        ],
        "queued": [
            {
                "boundary_at": "2026-10-07T05:00:00+00:00",
                "generation": 1,
                "sides": 1,
                "correction": "queued",
            },
        ],
        "after queueing": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['army'], ['operator'], 1, None, 0)],
        },
        "rebuilding": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'ready', 'active', ['army'], 1, 'snapshot@1:m1', 'army@2:m3', 1, None),
            ],
            "rebuilds": [('active', ['army'], ['operator'], 1, 2, 0)],
        },
        "listed while rebuilding": [
            {
                "boundary_at": "2026-10-07T05:00:00+00:00",
                "generation": 2,
                "sides": 1,
                "correction": "rebuilding",
            },
        ],
        "rebuilt": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army'], 1, 'snapshot@1:m1', 'army@2:m3', 1, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 2),
                (2, 'terminal', 'complete', 'complete', 2),
            ],
            "rebuilds": [('finalized', ['army'], ['operator'], 1, 2, 0)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5110, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1', 'u9x132-4x7']),
                ('army@1:m2', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m3', '#2PP', 2, 'Complete', ['u1x0-2x1', 'u9x132-4x7']),
                ('army@2:m3', '#8PP', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('army', 2, 'army@2:m3'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
            ],
        },
        "listed when rebuilt": [
            {
                "boundary_at": "2026-10-07T05:00:00+00:00",
                "generation": 2,
                "sides": 1,
                "correction": "not_queued",
            },
        ],
    }


def test_an_operator_rebuild_of_the_seasons_last_board(
    database_url: str, archive_server, monkeypatch
) -> None:
    """B's profile answers "not found" at 04:55, after its last reading,
    which no automatic rebuild notices; the operator's audit does."""
    end = day(3)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B)
        _one_day(story, A, B)
        story.run_worker()
        seen = {"first": _short(board_transcript(info, end))}
        store_observation(
            info, archive_server, occurrence_key=f"{B}-404", endpoint="profile",
            body=f'{{"reason":"notFound","tag":"{B}"}}'.encode(),
            observed_at=_at(4, 55), normalized_tag=B, http_status=404)
        story.run_worker()
        story.wait(timedelta(hours=1))
        seen["an hour after the 404"] = _short(board_transcript(info, end))
        seen["report"] = _operator(boundary.queue_board_rebuilds, info, SEASON_ID, queue=True)
        seen["queued"] = _short(board_transcript(info, end))
        story.wait(timedelta(hours=1))
        seen["an hour later"] = board_transcript(info, end)
        seen["board"] = _ranked(story.served_board(end))
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "first": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "an hour after the 404": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [],
        },
        "report": {
            "season_id": "1791176400",
            "queue": True,
            "boards": [
                {
                    "boundary_at": "2026-10-07T05:00:00+00:00",
                    "generation": 1,
                    "profile_not_found": 1,
                    "late_battles": 0,
                    "reordered": False,
                    "members_changed": 0,
                    "official_order": 0,
                    "correction": "queued",
                },
            ],
        },
        "queued": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "rebuilds": [('queued', ['army', 'snapshot'], [], 1, None, 0)],
        },
        "an hour later": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 2),
                (2, 'terminal', 'complete', 'complete', 2),
            ],
            "rebuilds": [('finalized', ['army', 'snapshot'], [], 1, 2, 0)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5140, -10, 'eligible']),
                ('snapshot@2:m3', '#8PP', 2, 'Complete', [5141, -10, 'profile_not_found']),
                ('army@2:m4', '#2PP', 2, 'Complete', ['u1x0-2x1']),
                ('army@2:m4', '#8PP', 2, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 2, 'army@2:m4'),
            ],
            "events": [(1, None), (2, 1)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m4', 'complete'),
            ],
        },
        "board": {"#2PP": 5140},
    }


def test_a_season_repair_rebuilds_the_boards_it_changes(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The weekly raise story of test_reset_scenarios_postgres, saved under
    an older rule (an instant raise, every day a first day), then repaired
    under today's."""
    import reset_scenarios_postgres_support as scenarios

    season = PREVIOUS_SEASON
    saturday, sunday, monday, tuesday = (day(number, season) for number in (20, 21, 22, 23))
    players = (LATE_RAISE, LOSS_TOO)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, *players)
        monkeypatch.setattr(scenarios, "READ_AFTER", timedelta(minutes=13))
        judge, settled_start = reading_rule.judge, reconciliation._settled_start
        monkeypatch.setattr(reading_rule, "judge", _instant_raise(judge))
        monkeypatch.setattr(reconciliation, "_settled_start",
                            lambda data: settled_start(replace(data, season_first_day=True)))
        for tag, sunday_defenses in ((LATE_RAISE, 8), (LOSS_TOO, 7)):
            story.players[tag].battles = (
                _saturday(saturday) + attacks(sunday, 40)
                + defenses(sunday, *[30] * sunday_defenses)
                + attacks(monday, 40) + defenses(monday, *[30] * 8))
        story.reset(saturday, dict.fromkeys(players, (5300, season)))
        story.reset(sunday, dict.fromkeys(players, (5140, season)))
        story.reads(monday - timedelta(minutes=10), {LATE_RAISE: 4940, LOSS_TOO: 4970}, season)
        story.reset(monday, {LATE_RAISE: (4940, season), LOSS_TOO: (4970, season)})
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(players, 5000), season)
        story.wait(timedelta(hours=1))
        story.reads(tuesday - timedelta(minutes=10), dict.fromkeys(players, 4800), season)
        story.reset(tuesday, dict.fromkeys(players, (4800, season)))
        monkeypatch.setattr(reading_rule, "judge", judge)
        monkeypatch.setattr(reconciliation, "_settled_start", settled_start)
        seen = {"before": [_short(board_transcript(info, at)) for at in (monday, tuesday)]}
        season_id = str(int(season.timestamp()))
        phases = []
        for _ in range(10):
            phases.append(_operator(season_repair, info, season_id, "queue",
                                    max_jobs=100)["phase"])
            if phases[-1] == "done":
                break
            story.wait(timedelta(hours=1))
        receipt = _operator(season_repair, info, season_id, "receipt", max_jobs=100)
        seen["phases"] = phases
        seen["receipt"] = {
            "boards_disagreeing": receipt["boards_disagreeing"],
            "board versions": {at: board["version"]
                               for at, board in receipt["boards"]["now"].items()}}
        seen["after"] = [board_transcript(info, at) for at in (monday, tuesday)]
        seen["boards"] = [_ranked(story.served_board(at)) for at in (monday, tuesday)]
        unfinished = story.unfinished_jobs()
    assert unfinished == []
    assert seen == {
        "before": [
            {
                "versions": [
                    (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                ],
                "rebuilds": [],
            },
            {
                "versions": [
                    (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                ],
                "rebuilds": [],
            },
        ],
        "phases": ["days", "done"],
        "receipt": {
            "boards_disagreeing": [],
            "board versions": {
                "2026-09-26T05:00:00+00:00": 1,
                "2026-09-27T05:00:00+00:00": 1,
                "2026-09-28T05:00:00+00:00": 1,
                "2026-09-29T05:00:00+00:00": 1,
            },
        },
        "after": [
            {
                "versions": [
                    (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                ],
                "members": [(1, 'terminal', 'inconsistent', 'partial', 2)],
                "rebuilds": [],
                "frozen": [
                    ('snapshot@1:m1', '#2PP', 1, 'Inconsistent', [4940, -10, 'eligible']),
                    ('snapshot@1:m1', '#8PP', 1, 'Inconsistent', [4970, -10, 'eligible']),
                    ('army@1:m2', '#2PP', 1, 'Partial', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                    ('army@1:m2', '#8PP', 1, 'Partial', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                ],
                "identities": [('analytics', 1, 'snapshot@1:m1'), ('army', 1, 'army@1:m2')],
                "events": [(1, None)],
                "jobs": [
                    ('build_snapshot:boundary:2026-09-28T05:00:00Z:gen:1:manifest:m1', 'complete'),
                    ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                    ('build_army_analytics:boundary:2026-09-28T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ],
            },
            {
                "versions": [
                    (1, 'superseded', 'superseded', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                    (2, 'published', 'published', 'finalized', ['army'], 1, 'snapshot@1:m1', 'army@2:m3', 1, 2),
                    (3, 'published', 'published', 'finalized', ['army'], 2, 'snapshot@1:m1', 'army@3:m4', 1, 3),
                ],
                "members": [
                    (1, 'terminal', 'inconsistent', 'partial', 2),
                    (2, 'terminal', 'complete', 'complete', 1),
                    (2, 'terminal', 'inconsistent', 'partial', 1),
                    (3, 'terminal', 'complete', 'complete', 2),
                ],
                "rebuilds": [
                    ('finalized', ['army'], [], 1, 2, 0),
                    ('finalized', ['army'], [], 2, 3, 1),
                ],
                "frozen": [
                    ('snapshot@1:m1', '#2PP', 3, 'Inconsistent', [4800, -10, 'eligible']),
                    ('snapshot@1:m1', '#8PP', 3, 'Inconsistent', [4800, -10, 'eligible']),
                    ('army@1:m2', '#2PP', 3, 'Partial', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                    ('army@1:m2', '#8PP', 3, 'Partial', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                    ('army@2:m3', '#2PP', 4, 'Complete', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                    ('army@2:m3', '#8PP', 3, 'Partial', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                    ('army@3:m4', '#2PP', 4, 'Complete', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                    ('army@3:m4', '#8PP', 4, 'Complete', ['u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1', 'u1x0-2x1']),
                ],
                "identities": [
                    ('analytics', 1, 'snapshot@1:m1'),
                    ('army', 1, 'army@1:m2'),
                    ('army', 2, 'army@2:m3'),
                    ('army', 3, 'army@3:m4'),
                ],
                "events": [(1, None), (2, 1), (3, 2)],
                "jobs": [
                    ('build_snapshot:boundary:2026-09-29T05:00:00Z:gen:1:manifest:m1', 'complete'),
                    ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                    ('build_army_analytics:boundary:2026-09-29T05:00:00Z:gen:1:manifest:m2', 'complete'),
                    ('build_army_analytics:boundary:2026-09-29T05:00:00Z:gen:2:manifest:m3', 'complete'),
                    ('build_army_analytics:boundary:2026-09-29T05:00:00Z:gen:3:manifest:m4', 'complete'),
                ],
            },
        ],
        "boards": [{"#8PP": 4970, "#2PP": 4940}, {"#2PP": 4800, "#8PP": 4800}],
    }


def test_a_new_player_saved_while_a_rebuild_waits_on_its_freeze(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Bug (b): A's day is saved again after the freeze, so the hourly
    rebuild captures its members, A and B, and freezes their inputs. That
    freeze times out once, as a slow one does in production (forced here by
    the one internal hook), and NEW, first read meanwhile, has its day saved
    before the retry. The rebuild keeps the members it took, and NEW waits
    in the queue for the next rebuild, as a new player does in
    test_a_new_player_joins_at_the_hourly_rebuild."""
    start, end = day(2), day(3)
    rows, tries = boundary_manifest._snapshot_rows, []

    def slow_once(connection, members, generation, parts):
        if generation[0] == end and generation[1] == 2:
            tries.append(generation[1])
            if len(tries) == 1:
                connection.execute("SET LOCAL statement_timeout = '50ms'")
                connection.execute("SELECT pg_sleep(1)")
        return rows(connection, members, generation, parts)

    monkeypatch.setattr(boundary_manifest, "_snapshot_rows", slow_once)
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B, NEW)
        story.players[NEW].battles = attacks(start, 40)
        _changed_after_freeze(story)
        story.read(A, _at(5, 40))
        story.run_worker()
        story.wait(timedelta(hours=1))
        seen = {"timed out": _short(board_transcript(info, end))}
        story.reads(_at(4, 50), {NEW: 5142})
        story.run_worker()
        seen["NEW saved"] = board_transcript(info, end)
        seen["NEW's day"] = story.saved_day(NEW, start)["state"]
        # The freeze waits a minute after a timeout before it tries again.
        with psycopg.connect(info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET updated_at = updated_at - interval '2 minutes'"
                " WHERE failure_category = 'freeze_timed_out'")
        story.wait(timedelta(hours=7))
        final = board_transcript(info, end)
        board = _ranked(story.served_board(end))
        unfinished = story.unfinished_jobs()
    # The freeze timed out once, and that try is kept as a failed job.
    assert tries[:1] == [2]
    assert [job[:3] for job in unfinished] == [
        ("build_snapshot", "failed", "freeze_timed_out")]
    assert seen["timed out"] == {
        "versions": [
            (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            (2, 'ready', 'ready', 'active', ['army', 'snapshot'], 1, None, None, None, None),
        ],
        "rebuilds": [('active', ['army', 'snapshot'], [], 1, 2, 1)],
    }
    assert seen["NEW's day"] == "Partial"
    # NEW waits in the queue behind the rebuild in progress.
    assert _short(seen["NEW saved"]) == {
        "versions": [
            (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            (2, 'ready', 'ready', 'active', ['army', 'snapshot'], 1, None, None, None, None),
        ],
        "rebuilds": [
            ('active', ['army', 'snapshot'], [], 1, 2, 1),
            ('queued', ['army', 'snapshot'], [], 2, None, 1),
        ],
    }
    # The retry froze the members it took, A and B, and the next rebuild
    # took NEW in.
    assert final["versions"] == [
        (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
        (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
        (3, 'published', 'published', 'finalized', ['army', 'snapshot'], 2, 'snapshot@3:m5', 'army@3:m6', 3, 3),
    ]
    assert final["rebuilds"] == [
        ('finalized', ['army', 'snapshot'], [], 1, 2, 1),
        ('finalized', ['army', 'snapshot'], [], 2, 3, 1),
    ]
    assert {row[1] for row in final["frozen"] if row[0] == 'snapshot@2:m3'} == {A, B}
    assert {row[1] for row in final["frozen"] if row[0] == 'snapshot@3:m5'} == {A, B, NEW}
    assert board == {NEW: 5142, B: 5141, A: 5140}
