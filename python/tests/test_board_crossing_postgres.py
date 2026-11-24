"""End-to-end: two pieces of board work that cross, against PostgreSQL.

Each test runs a pair two ways, each in its own database: one after the
other, and crossed, with the first stopped just after it takes the Reset's
publication lock (``boundary-publication:<Reset>``) and the second started
until it waits on a lock or finishes. Crossed, neither may deadlock or fail,
and the board must end as the first-then-second run's ends
(``board_transcript``). The pause keys on the lock's name alone, so the
tests hold as the board code moves behind one owner.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

import psycopg
import pytest
from board_later_reading_postgres_support import _hold
from domain_test_support import domain_database, store_observation
from league_week_days_postgres_support import _decide_below
from reset_scenarios_postgres_support import (
    SEASON,
    THROWER,
    Battle,
    _ranked,
    _story,
    attacks,
    board_transcript,
    day,
    defenses,
)

from clashlens import boundary, reconciliation_db
from clashlens.boundary_publication import reevaluate_boundary_publications
from clashlens.db import Database
from clashlens.job_contracts import POPULATION_BUILD_WORK_TYPES
from clashlens.late_battle_sweep import sweep_late_battles

A, B, NEW = "#2PP", "#8PP", "#2QQ"
# Half an hour after the day 4 Reset: its late-battle check is due.
CHECK_AT = day(4) + timedelta(minutes=31)

Side = Callable[[], object]


def _with_database(connection_info: str, work: Callable[[Database], object]) -> Side:
    def run() -> object:
        database = Database(connection_info)
        try:
            return work(database)
        finally:
            database.close()
    return run


def _reset_locks(observer, reset: datetime) -> list[tuple[int, bool, list[int]]]:
    """Each session holding or waiting for ``reset``'s publication lock:
    (its process ID, whether it holds it, the sessions it waits for)."""
    return [(pid, granted, list(blockers)) for pid, granted, blockers in observer.execute(
        """
        SELECT lock.pid, lock.granted, pg_blocking_pids(lock.pid)
        FROM pg_locks AS lock
        WHERE lock.locktype = 'advisory' AND lock.objsubid = 1
          AND lock.database = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND lock.classid::bigint = (hashtextextended(%(key)s, 0) >> 32) & 4294967295
          AND lock.objid::bigint = hashtextextended(%(key)s, 0) & 4294967295
        ORDER BY lock.granted DESC, lock.pid
        """,
        {"key": f"boundary-publication:{reset.isoformat()}"},
    ).fetchall()]


def _cross(monkeypatch, connection_info: str, reset: datetime, first: Side,
           second: Side) -> dict:
    """Run ``first`` until it takes ``reset``'s publication lock, then
    ``second`` until it waits for ``first`` on that lock or ends, then let
    both finish."""
    key = f"boundary-publication:{reset.isoformat()}"
    took, go = threading.Event(), threading.Event()
    execute = psycopg.Connection.execute
    first_thread: list[threading.Thread] = []
    first_pid: list[int] = []
    out: dict = {"errors": [], "waited": False}

    def pausing(connection, query, params=None, **kwargs):
        cursor = execute(connection, query, params, **kwargs)
        if (threading.current_thread() in first_thread and not took.is_set()
                and "pg_advisory_xact_lock" in str(query) and key in (params or ())):
            first_pid.append(connection.info.backend_pid)
            took.set()
            go.wait(60)
        return cursor

    def run(name: str, work: Side) -> None:
        try:
            out[name] = work()
        except Exception as error:  # noqa: BLE001 - reported, then asserted empty
            out["errors"].append(f"{name}: {error!r}")

    threads = [threading.Thread(target=run, args=("first", first)),
               threading.Thread(target=run, args=("second", second))]
    first_thread.append(threads[0])
    observer = psycopg.connect(connection_info, autocommit=True)
    monkeypatch.setattr(psycopg.Connection, "execute", pausing)
    try:
        threads[0].start()
        out["took"] = took.wait(60)
        threads[1].start()
        deadline = time.monotonic() + 10
        while out["took"] and threads[1].is_alive() and time.monotonic() < deadline:
            if any(not granted and first_pid[0] in blockers
                   for _pid, granted, blockers in _reset_locks(observer, reset)):
                out["waited"] = True
                break
            time.sleep(0.01)
    finally:
        go.set()
        forced = False
        try:
            for thread in threads:
                thread.join(120)
            if any(thread.is_alive() for thread in threads):
                forced = True
                # End their database work so the threads end before the test does.
                observer.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND pid <> pg_backend_pid()")
                for thread in threads:
                    thread.join(30)
        finally:
            observer.close()
            monkeypatch.setattr(psycopg.Connection, "execute", execute)
    assert not forced, "a side had to be stopped"
    assert not any(thread.is_alive() for thread in threads), "a side never finished"
    return out


def _both_ways(database_url: str, archive_server, monkeypatch, reset: datetime,
               setup: Callable[..., tuple[Side, Side]]) -> tuple[dict, dict]:
    """The pair ``setup`` returns, run one after the other and crossed:
    each run's sides' results, then, once the worker has finished and an
    hour has passed for any rebuild, ``reset``'s transcript and served
    board, the attacks on each player's saved days 2 and 3, unfinished jobs
    and deadlocks."""
    runs = {}
    for crossed in (False, True):
        # Each run patches the clocks afresh: a story's clock builds on the
        # one patched before it.
        with monkeypatch.context() as patch, domain_database(database_url) as info:
            story = _story(info, archive_server, patch, A, B, NEW)
            first, second = setup(story)
            if crossed:
                out = _cross(patch, info, reset, first, second)
            else:
                out = {"errors": [], "first": first(), "second": second()}
            story.run_worker()
            story.wait(timedelta(hours=1))
            out["transcript"] = board_transcript(info, reset)
            out["board"] = _ranked(story.served_board(reset))
            out["days"] = {tag: [saved and saved["attacks"] for saved in (
                story.saved_day(tag, day(2)), story.saved_day(tag, day(3)))]
                for tag in (A, B, NEW)}
            out["unfinished"] = story.unfinished_jobs()
            with psycopg.connect(info) as connection:
                out["deadlocks"] = connection.execute(
                    "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
                ).fetchone()[0]
            runs[crossed] = out
    return runs[False], runs[True]


def _assert_crossed_like_sequential(sequential: dict, crossed: dict) -> None:
    assert crossed["errors"] == sequential["errors"] == []
    assert crossed["took"] and crossed["waited"], "the two never crossed"
    assert crossed["deadlocks"] == sequential["deadlocks"] == 0
    assert crossed["unfinished"] == sequential["unfinished"] == []
    assert crossed["first"] == sequential["first"]
    assert crossed["second"] == sequential["second"]
    assert crossed["transcript"] == sequential["transcript"]
    assert crossed["board"] == sequential["board"]
    assert crossed["days"] == sequential["days"]


def _processed(results: list) -> bool:
    return bool(results) and all(result == ("processed", None) for result in results)


def _late_battle(story, *, late_for: tuple[str, ...], logged: tuple[str, ...]) -> None:
    """Day 2 for A, B and NEW, an attack of 40 each, and day 3 for the
    others. Each of ``late_for`` attacks on day 2 only, again 10 seconds
    before the day 3 Reset, which no Reset battle log shows; the ones in
    ``logged`` have a battle log saved 20 minutes after the day 4 Reset
    whose day calculations were missed, so only the late-battle check finds
    it. Day 4's board waits to build."""
    start, end, next_end = day(2), day(3), day(4)
    first = {A: 5100, B: 5101, NEW: 5102}
    for tag in first:
        story.players[tag].battles = attacks(start, 40) + (
            [Battle(end - timedelta(seconds=10), 40, True)] if tag in late_for
            else attacks(end, 40))
    story.reset(start, {tag: (first[tag], SEASON) for tag in first})
    story.reset(end, {tag: (first[tag] + 40 + 40 * (tag in late_for), SEASON)
                      for tag in first}, no_log=late_for)
    story.reset(next_end, {tag: (first[tag] + 80, SEASON) for tag in first},
                run=False, no_log=late_for)
    story.run_round(skip=POPULATION_BUILD_WORK_TYPES)
    story.run_jobs(*(story.read(tag, next_end + timedelta(minutes=20)) for tag in logged))
    with psycopg.connect(story.connection_info) as connection:
        connection.execute(
            "UPDATE python_processing_jobs SET status = 'complete', completed_at = now()"
            " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'")


def test_the_late_battle_check_holds_an_older_reset_while_it_calculates_a_newer_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Suspected bug (a), modelled on test_season_repair_parallel_postgres:
    the late-battle check recalculates A's days 2 and 3 in one transaction,
    so as it starts day 3 it still holds the day 3 Reset it took for day 2.
    B's late battle log, saved meanwhile, shows only day 2 battles, so it
    needs only that Reset, and should not wait for A's newer day."""
    with domain_database(database_url) as info:
        story = _story(info, archive_server, monkeypatch, A, B, NEW)
        _late_battle(story, late_for=(A, B), logged=(A,))
        story.run_worker()
        recalculate = reconciliation_db.recalculate_ranked_day
        stopped, go = threading.Event(), threading.Event()

        def stopping(database, connection, **day_input):
            if day_input["day_start"] == day(3) and not stopped.is_set():
                stopped.set()
                go.wait(60)
            return recalculate(database, connection, **day_input)

        monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", stopping)
        checked: dict = {}
        check = threading.Thread(target=lambda: checked.update(
            result=_with_database(info, lambda database: sweep_late_battles(
                database, now=CHECK_AT))()))
        check.start()
        try:
            assert stopped.wait(60)
            with psycopg.connect(info, autocommit=True) as observer:
                held = {reset: [granted for _pid, granted, _ in _reset_locks(observer, reset)]
                        for reset in (day(3), day(4))}
            log = story.run_jobs(story.read(B, day(4) + timedelta(minutes=40)))
        finally:
            go.set()
            check.join(60)
        monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", recalculate)
        story.run_worker()
        unfinished = story.unfinished_jobs()
        days = {tag: story.saved_day(tag, day(2))["attacks"] for tag in (A, B)}
    assert checked["result"] == (1, 0)
    assert unfinished == []
    assert days == {A: 2, B: 2}
    if log == [("retrying", "database_lock_busy")]:
        # Paused before A's day 3, the check held day 3's Reset, not day 4's.
        assert held == {day(3): [True], day(4): []}
        pytest.xfail("B's battle log waited on the day 3 Reset the late-battle check "
                     "held while it calculated A's day 3, and was sent to retry")
    assert log == [("processed", None)]


def test_late_battle_check_crossing_the_board_build(
    database_url: str, archive_server, monkeypatch
) -> None:
    def setup(story):
        _late_battle(story, late_for=(A,), logged=(A,))
        build = story.jobs("build_snapshot")
        assert len(build) == 1
        return (_with_database(story.connection_info, lambda database: sweep_late_battles(
            database, now=CHECK_AT)), lambda: story.run_jobs(*build))

    sequential, crossed = _both_ways(database_url, archive_server, monkeypatch, day(4), setup)
    _assert_crossed_like_sequential(sequential, crossed)
    assert sequential["first"] == (1, 0) and _processed(sequential["second"])
    # The check saved A's late attack on its day 2.
    assert sequential["days"][A] == [2, 0]
    assert {k: sequential[k] for k in ("transcript", "board")} == {
        "transcript": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 2),
                (1, 'terminal', 'partial', 'partial', 1),
            ],
            "rebuilds": [],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 1, 'Partial', [5180, -1436, 'eligible']),
                ('snapshot@1:m1', '#8PP', 1, 'Complete', [5141, -1436, 'eligible']),
                ('snapshot@1:m1', '#2QQ', 1, 'Complete', [5142, -1436, 'eligible']),
                ('army@1:m2', '#2PP', 1, 'Partial', []),
                ('army@1:m2', '#8PP', 1, 'Complete', ['u1x0-2x1']),
                ('army@1:m2', '#2QQ', 1, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [('analytics', 1, 'snapshot@1:m1'), ('army', 1, 'army@1:m2')],
            "events": [(1, None)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-08T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-08T05:00:00Z:gen:1:manifest:m2', 'complete'),
            ],
        },
        "board": {"#2QQ": 5182, "#8PP": 5181, "#2PP": 5180},
    }


def test_reset_reading_crossing_the_statistics_publish(
    database_url: str, archive_server, monkeypatch
) -> None:
    """B's Reset battle log came after the board froze; B's Reset profile
    reading arrives as the board's statistics publish. (Crossing both B's
    responses with the statistics, the statistics would land between them,
    another order.)"""
    def setup(story):
        start, end = day(2), day(3)
        for tag in (A, B):
            story.players[tag].battles = attacks(start, 40)
        story.reset(start, {A: (5100, SEASON), B: (5101, SEASON)})
        story.reads(end.replace(hour=4, minute=50), {A: 5140, B: 5141})
        story.reset(end, {A: (5140, SEASON), B: (5141, SEASON)}, run=False)
        _hold(story.connection_info, (B,), end)
        story.run_round(skip=("build_analytics",))
        with psycopg.connect(story.connection_info) as connection:
            responses = [row[0] for row in connection.execute(
                "UPDATE python_processing_jobs SET due_at = now()"
                " WHERE status = 'pending' AND due_at > now() RETURNING id").fetchall()]
        statistics = story.jobs("build_analytics")
        assert len(responses) == 2 and len(statistics) == 1
        profile, log = sorted(responses)
        assert _processed(story.run_jobs(log))
        return (lambda: story.run_jobs(profile)), (lambda: story.run_jobs(*statistics))

    sequential, crossed = _both_ways(database_url, archive_server, monkeypatch, day(3), setup)
    _assert_crossed_like_sequential(sequential, crossed)
    assert _processed(sequential["first"]) and _processed(sequential["second"])
    assert {k: sequential[k] for k in ("transcript", "board")} == {
        "transcript": {
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
                ('army', 1, 'army@1:m2'),
                ('analytics', 1, 'snapshot@1:m1'),
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
        "board": {"#8PP": 5141, "#2PP": 5140},
    }


def test_army_decode_crossing_the_background_check(
    database_url: str, archive_server, monkeypatch
) -> None:
    """B's Reset responses came after the board froze, so a rebuild waits
    its hour; when it is due, the worker's background publication check
    starts it as a battle log showing another army for A's defense is
    saved."""
    def setup(story):
        start, end = day(2), day(3)
        story.players[A].battles = attacks(start, 40) + defenses(start, 30, army=THROWER)
        story.players[B].battles = attacks(start, 40)
        story.reset(start, {A: (5100, SEASON), B: (5101, SEASON)})
        story.reads(end.replace(hour=4, minute=50), {A: 5110, B: 5141})
        story.reset(end, {A: (5110, SEASON), B: (5141, SEASON)}, run=False)
        _hold(story.connection_info, (B,), end)
        story.run_round()
        story.run_worker()
        story.players[A].battles = [
            replace(battle, army="u1x58") if battle.army == THROWER else battle
            for battle in story.players[A].battles]
        log = story.read(A, end.replace(hour=6))
        story.elapsed += timedelta(hours=1)
        return (_with_database(story.connection_info, reevaluate_boundary_publications),
                lambda: story.run_jobs(log))

    sequential, crossed = _both_ways(database_url, archive_server, monkeypatch, day(3), setup)
    _assert_crossed_like_sequential(sequential, crossed)
    assert sequential["first"] == 1 and _processed(sequential["second"])
    assert {k: sequential[k] for k in ("transcript", "board")} == {
        "transcript": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['snapshot'], 1, 'snapshot@2:m3', 'army@1:m2', 2, 1),
                (3, 'published', 'published', 'finalized', ['army'], 2, 'snapshot@2:m3', 'army@3:m4', 2, 3),
                (4, 'published', 'published', 'finalized', ['army'], 3, 'snapshot@2:m3', 'army@4:m5', 2, 4),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (1, 'terminal', 'partial', 'partial', 1),
                (2, 'terminal', 'complete', 'complete', 2),
                (3, 'terminal', 'complete', 'complete', 2),
                (4, 'terminal', 'complete', 'complete', 2),
            ],
            "rebuilds": [
                ('finalized', ['snapshot'], [], 1, 2, 1),
                ('finalized', ['army'], ['decode'], 2, 3, 0),
                ('finalized', ['army'], [], 3, 4, 1),
            ],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Complete', [5110, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Partial', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Complete', ['u1x0-2x1', 'u9x132-4x7']),
                ('army@1:m2', '#8PP', 2, 'Partial', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 2, 'Complete', [5110, -10, 'eligible']),
                ('snapshot@2:m3', '#8PP', 3, 'Complete', [5141, -10, 'eligible']),
                ('army@3:m4', '#2PP', 2, 'Complete', ['u1x0-2x1', 'u1x58']),
                ('army@3:m4', '#8PP', 3, 'Complete', ['u1x0-2x1']),
                ('army@4:m5', '#2PP', 3, 'Complete', ['u1x0-2x1', 'u1x58']),
                ('army@4:m5', '#8PP', 3, 'Complete', ['u1x0-2x1']),
            ],
            "identities": [
                ('analytics', 1, 'snapshot@1:m1'),
                ('army', 1, 'army@1:m2'),
                ('analytics', 2, 'snapshot@2:m3'),
                ('army', 3, 'army@3:m4'),
                ('army', 4, 'army@4:m5'),
            ],
            "events": [(1, None), (2, 1), (3, 2), (4, 3)],
            "jobs": [
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m1', 'complete'),
                ('build_analytics:snapshot:#:v1:input:h1', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:1:manifest:m2', 'complete'),
                ('build_snapshot:boundary:2026-10-07T05:00:00Z:gen:2:manifest:m3', 'complete'),
                ('build_analytics:snapshot:#:v2:input:h2', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:3:manifest:m4', 'complete'),
                ('build_army_analytics:boundary:2026-10-07T05:00:00Z:gen:4:manifest:m5', 'complete'),
            ],
        },
        "board": {"#8PP": 5141, "#2PP": 5110},
    }


def _saved_again_after_the_freeze(story) -> list[int]:
    """A's day 2 ends at 5,180 by a 04:58 attack its Reset battle log
    missed; a battle log read at 05:40 shows it. Returns the queued day
    calculations it gives."""
    start, end = day(2), day(3)
    story.players[A].battles = attacks(start, 40) + [
        Battle(end - timedelta(minutes=2), 40, True)]
    story.players[B].battles = attacks(start, 40)
    story.reset(start, {A: (5100, SEASON), B: (5101, SEASON)})
    story.reads(end - timedelta(minutes=10), {A: 5140, B: 5141})
    story.reset(end, {A: (5180, SEASON), B: (5141, SEASON)}, no_log=(A,))
    story.run_jobs(story.read(A, end + timedelta(minutes=40)))
    return story.jobs("reconcile_ranked_day")


def test_league_exit_crossing_a_day_save(
    database_url: str, archive_server, monkeypatch
) -> None:
    """B is decided below Legend I for the week as A's day is saved again."""
    def setup(story):
        days = _saved_again_after_the_freeze(story)
        story.decides = False
        return (lambda: _decide_below(story.connection_info, (B,), story.clock)), (
            lambda: story.run_jobs(*days))

    sequential, crossed = _both_ways(database_url, archive_server, monkeypatch, day(3), setup)
    _assert_crossed_like_sequential(sequential, crossed)
    assert _processed(sequential["second"])
    assert {k: sequential[k] for k in ("transcript", "board")} == {
        "transcript": {
            "versions": [
                (1, 'published', 'published', 'finalized', [], None, 'snapshot@1:m1', 'army@1:m2', 1, 1),
                (2, 'published', 'published', 'finalized', ['army', 'snapshot'], 1, 'snapshot@2:m3', 'army@2:m4', 2, 2),
            ],
            "members": [
                (1, 'terminal', 'complete', 'complete', 1),
                (1, 'terminal', 'partial', 'partial', 1),
                (2, 'terminal', 'partial', 'partial', 1),
            ],
            "rebuilds": [('finalized', ['army', 'snapshot'], [], 1, 2, 1)],
            "frozen": [
                ('snapshot@1:m1', '#2PP', 2, 'Partial', [5140, -10, 'eligible']),
                ('snapshot@1:m1', '#8PP', 2, 'Complete', [5141, -10, 'eligible']),
                ('army@1:m2', '#2PP', 2, 'Partial', ['u1x0-2x1']),
                ('army@1:m2', '#8PP', 2, 'Complete', ['u1x0-2x1']),
                ('snapshot@2:m3', '#2PP', 3, 'Partial', [5140, -10, 'eligible']),
                ('army@2:m4', '#2PP', 3, 'Partial', ['u1x0-2x1', 'u1x0-2x1']),
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


def test_operator_rebuild_crossing_a_day_save(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The operator queues the Season's board rebuilds, B's profile having
    answered "not found" at 04:55, as A's day is saved again."""
    def setup(story):
        days = _saved_again_after_the_freeze(story)
        store_observation(
            story.connection_info, story.archive_server, occurrence_key=f"{B}-404",
            endpoint="profile", body=f'{{"reason":"notFound","tag":"{B}"}}'.encode(),
            observed_at=day(3) - timedelta(minutes=5), normalized_tag=B, http_status=404)
        story.run_round(skip=("reconcile_ranked_day",))
        days = story.jobs("reconcile_ranked_day")
        return (_with_database(story.connection_info, lambda database: boundary.queue_board_rebuilds(
            database, str(int(SEASON.timestamp())), queue=True)), lambda: story.run_jobs(*days))

    sequential, crossed = _both_ways(database_url, archive_server, monkeypatch, day(3), setup)
    _assert_crossed_like_sequential(sequential, crossed)
    assert [board["correction"] for board in sequential["first"]["boards"]] == ["queued"]
    assert _processed(sequential["second"])
    assert {k: sequential[k] for k in ("transcript", "board")} == {
        "transcript": {
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
                ('snapshot@2:m3', '#8PP', 2, 'Complete', [5141, -10, 'profile_not_found']),
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
        "board": {"#2PP": 5140},
    }

