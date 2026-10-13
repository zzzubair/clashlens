"""End-to-end: the Reset's own work goes first until its Daily board publishes.

From each swept Reset until its Daily board publishes, and never past
05:45 UTC, worker threads that process no saved responses take the Reset's
day results ahead of the new day's live ones on every claim, and no
background recheck starts (owner decision, 13 Oct 2026). On 13 Oct the
Reset's day results got about half of those threads' jobs and the board
published at 05:46. See ``background_pacing.reset_first_claim``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import psycopg
from domain_test_support import domain_database
from test_reconciliation_postgres import _processor
from test_reset_scenarios_postgres import SEASON, Story, _story, attacks, day, defenses

from clashlens import (
    background_pacing,
    boundary,
    boundary_publication,
    db,
    first_battle_log,
    queue_refresh,
    reconciliation_db,
    reset_baselines,
    snapshots,
)
from clashlens.db import POPULATION_BUILD_WORK_TYPES, RESPONSE_WORK_TYPES
from clashlens.worker import DERIVED_WITHOUT_BUILDS

TAGS = ("#2PP", "#8PP", "#9PP", "#PPP")
RESET = day(4)


def _at_the_reset(story: Story, monkeypatch, clock: list[datetime]) -> None:
    """Days 2 and 3 saved as on any day; then Day 4's Reset, read at 05:04,
    with each player's first Day 4 attack, at 05:06, read at 05:08, while the worker's
    clock says ``clock[0]``. The Reset queues each player's Day 3 result at
    Reset priority, and their live Day 4 result waits."""
    for tag in TAGS:
        story.players[tag].battles = (
            attacks(day(2), 40, 40) + defenses(day(2), 30, 30)
            + attacks(day(3), 40) + defenses(day(3), 30)
            + attacks(RESET - timedelta(minutes=54), 40)
        )
    story.reset(day(2), {tag: (5300, SEASON) for tag in TAGS})
    story.reset(day(3), {tag: (5320, SEASON) for tag in TAGS})
    monkeypatch.setattr(background_pacing, "_now", lambda: clock[0])
    monkeypatch.setattr(background_pacing, "_open_window", [])

    def ended_day_priority(start: datetime) -> int:
        ended = start + timedelta(days=1)
        return db.PYTHON_RESET_PRIORITY if ended <= clock[0] < ended + timedelta(days=1) \
            else db.PYTHON_LIVE_PRIORITY

    for module in (boundary, snapshots, reset_baselines, first_battle_log, reconciliation_db):
        monkeypatch.setattr(module, "ended_day_priority", ended_day_priority)
    story.reset(RESET, {tag: (5330, SEASON) for tag in TAGS}, run=False)
    for tag in TAGS:
        story.read(tag, RESET + timedelta(minutes=8))
    # The live Day 4 result each 05:08 battle log queues in production; the
    # database's clock is past Day 4, so it queues none here itself.
    with psycopg.connect(story.connection_info) as connection:
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (observation_id, work_type,
                deduplication_key, input_json, state, due_at, parser_version,
                processing_version, domain_rule_version, analytics_rule_version, priority)
            SELECT NULL, 'reconcile_ranked_day', 'reconcile:live:' || player.id, jsonb_build_object(
                       'player_id', player.id, 'ranked_day_start', %s::text,
                       'trigger', 'live_battle_projection'),
                   'pending', clock_timestamp(), job.parser_version, job.processing_version,
                   job.domain_rule_version, job.analytics_rule_version, %s
            FROM players AS player, (SELECT * FROM python_processing_jobs_worker
                                     WHERE work_type = 'reconcile_ranked_day' LIMIT 1) AS job
            WHERE player.normalized_tag = ANY(%s)
            """,
            (RESET.strftime("%Y-%m-%dT%H:%M:%SZ"), db.PYTHON_LIVE_PRIORITY, list(TAGS)),
        )


def _lane(processor, kinds: tuple[str, ...], claims: int | None = None) -> list[tuple]:
    """One worker thread claiming ``kinds`` until idle, or ``claims`` jobs:
    each finished job's priority and type."""
    done: list[tuple] = []
    while claims is None or len(done) < claims:
        result = processor.process_once(owner="story.lane-9", work_types=kinds)
        if result is None:
            break
        with processor.database.pool.connection() as connection:
            done.append(connection.execute(
                "SELECT priority, work_type FROM python_processing_jobs WHERE id = %s",
                (result.job_id,)).fetchone())
    return done


def _days(done: list[tuple]) -> list[int]:
    return [priority for priority, kind in done if kind == "reconcile_ranked_day"]


def _published(connection_info: str) -> bool:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT EXISTS (SELECT FROM boundary_publication_generations"
            " WHERE boundary_at = %s AND snapshot_state = 'published')", (RESET,)).fetchone()[0]


def _windows(capsys) -> list[dict]:
    return [line for line in map(json.loads, (
        text for text in capsys.readouterr().out.splitlines() if text.startswith("{")))
        if line.get("event") == "reset_first_window"]


def test_reset_day_results_go_first_and_rechecks_wait_for_the_board(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """At 05:10 the day-result thread takes all 4 of the Reset's day results
    before any of the new day's, and no recheck until the board publishes;
    once it has, the thread runs the rechecks."""
    clock = [RESET + timedelta(minutes=10)]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *TAGS)
        _at_the_reset(story, monkeypatch, clock)
        database, processor = _processor(connection_info, archive_server)
        try:
            queue_refresh.queue_recheck(database, "story", day(3), spread=timedelta(0))
            capsys.readouterr()
            _lane(processor, RESPONSE_WORK_TYPES)
            before_board = _lane(processor, DERIVED_WITHOUT_BUILDS)
            for _ in range(20):
                boundary_publication.reevaluate_boundary_publications(database)
                if _published(connection_info) or not _lane(processor, POPULATION_BUILD_WORK_TYPES):
                    break
            published = _published(connection_info)
            after_board = _lane(processor, DERIVED_WITHOUT_BUILDS)
        finally:
            database.close()
        windows = _windows(capsys)

    days = _days(before_board)
    assert days.count(db.PYTHON_RESET_PRIORITY) == len(TAGS)
    assert days.count(db.PYTHON_LIVE_PRIORITY) >= len(TAGS)
    assert days == sorted(days, reverse=True), "a live day result went before the Reset's"
    assert queue_refresh.DAY_RECHECK_PRIORITY not in days
    assert published
    assert _days(after_board).count(queue_refresh.DAY_RECHECK_PRIORITY) >= len(TAGS)
    assert [(line["status"], line.get("reason")) for line in windows] == [
        ("started", None), ("ended", "published")]
    assert windows[0]["boundary_at"] == RESET.isoformat()


def test_a_stuck_board_holds_live_results_and_rechecks_only_until_0545(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    """The board never builds. Until 05:45 the day-result thread takes the
    Reset's day results first; from 05:45 it takes turns with the new day's
    live results again, and rechecks run, although the board has not
    published."""
    clock = [RESET + timedelta(minutes=10)]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *TAGS)
        _at_the_reset(story, monkeypatch, clock)
        database, processor = _processor(connection_info, archive_server)
        try:
            queue_refresh.queue_recheck(database, "story", day(3), spread=timedelta(0))
            capsys.readouterr()
            _lane(processor, RESPONSE_WORK_TYPES)
            before_cap = _lane(processor, DERIVED_WITHOUT_BUILDS, claims=2)
            clock[0] = RESET + background_pacing.RESET_FIRST_CAP
            after_cap = _lane(processor, DERIVED_WITHOUT_BUILDS)
            published = _published(connection_info)
        finally:
            database.close()
        windows = _windows(capsys)

    assert _days(before_cap) == [db.PYTHON_RESET_PRIORITY] * 2
    days = _days(after_cap)
    assert sorted(days[:2]) == [db.PYTHON_LIVE_PRIORITY, db.PYTHON_RESET_PRIORITY]
    assert days.count(queue_refresh.DAY_RECHECK_PRIORITY) >= len(TAGS)
    assert not published
    assert [(line["status"], line.get("reason")) for line in windows] == [
        ("started", None), ("ended", "cap")]
