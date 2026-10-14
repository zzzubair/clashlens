"""A Reset's board publishes on time, and the day's work keeps up after it.

On 12 Oct 2026 the Daily board published at 12:27 UTC, 7 hours late: two
players' day calculations failed for good at 06:54 and every other player
waited with them, day calculations that met a busy Reset kept their leases
and came back at the head of the queue, and each new battle queued one more
calculation for a day that already had one waiting. These run the worker
against PostgreSQL through to the saved days and the board.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database, store_observation, text
from psycopg.types.json import Jsonb
from test_reconciliation_postgres import (
    DAY_END,
    DAY_START,
    _processor,
    _profile,
    _seed_reset_collection_identity,
)
from test_reset_acceptance_postgres import _as_worker
from test_reset_scenarios_postgres import _tag

from clashlens import (
    alerts,
    api_leaderboard,
    board_deadline,
    boundary,
    boundary_manifest,
    boundary_publication,
    past_reset_pacing,
    queue_refresh,
    reconciliation_db,
    snapshots,
)
from clashlens.api_db import ApiDatabase
from clashlens.collector_metrics import health_metrics
from clashlens.db import WORKER_STATEMENT_TIMEOUT_SECONDS, Database

PLAYERS = ("#2PP", "#8PP")
LATE = "#8PP"
BUILDS = ("build_snapshot", "build_analytics", "build_army_analytics")


def _attack(at: datetime) -> bytes:
    return json.dumps({"items": [{
        "battleType": "legend", "attack": True, "battleTime": 150,
        "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"), "stars": 1,
        "destructionPercentage": 60, "opponentPlayerTag": "#QPP",
        "opponentName": "Opponent", "opponentTownHallLevel": 18,
        "armyShareCode": "u1x0-2x1",
    }]}).encode()


def _swept(connection_info: str, archive_server, processor, tags=PLAYERS) -> None:
    """Save and process both Resets' readings for each player: each day
    starts and ends at 6,000 with no battles."""
    with psycopg.connect(connection_info) as connection:
        for tag in tags:
            connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES (%s, true)", (tag,)
            )
        for at in (DAY_START, DAY_END):
            connection.execute(
                """
                WITH sweep AS (
                    INSERT INTO collector_reset_sweeps (
                        boundary_at, member_ids, membership_captured_at)
                    SELECT %s, array_agg(id ORDER BY id), clock_timestamp() FROM players
                    RETURNING id, member_ids)
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, sweep_id,
                    due_at, coalescing_key, status)
                SELECT 'reset_baseline', 'reset', 'player', player.id,
                       player.normalized_tag, sweep.id, %s,
                       'reset-baseline-' || player.normalized_tag || %s, 'pending'
                FROM sweep JOIN players AS player ON player.id = ANY(sweep.member_ids)
                """,
                (at, at, f"{at:%d}"),
            )
    jobs = []
    for at in (DAY_START, DAY_END):
        for tag in tags:
            key, ids = f"{tag}-{at:%d}", []
            for endpoint, body in (("profile", _profile(6000, tag)),
                                   ("battle_log", b'{"items": []}')):
                observation, job = store_observation(
                    connection_info, archive_server, occurrence_key=f"{key}-{endpoint}",
                    endpoint=endpoint, body=body, observed_at=at + timedelta(minutes=2),
                    normalized_tag=tag,
                )
                ids.append(observation)
                jobs.append(job)
            _seed_reset_collection_identity(
                connection_info, key=key, boundary=at, profile_observation_id=ids[0],
                battle_observation_id=ids[1], normalized_tag=tag,
            )
    for job in jobs:
        result = processor.process_job(job, owner=f"source-{job}")
        assert result is not None and result.outcome == "processed"


def _waiting(database: Database, work_types, tag: str | None = None) -> list[int]:
    with database.pool.connection() as connection:
        return [int(row[0]) for row in connection.execute(
            """
            SELECT job.id FROM python_processing_jobs AS job
            WHERE job.status IN ('pending', 'waiting_retry') AND job.work_type = ANY(%s)
              AND (%s::text IS NULL OR job.input_json ->> 'player_id' = (
                  SELECT id::text FROM players WHERE normalized_tag = %s))
            ORDER BY job.id
            """,
            (list(work_types), tag, tag),
        ).fetchall()]


def _due_now(database: Database, job_id: int) -> None:
    with database.pool.connection() as connection:
        connection.execute(
            "UPDATE python_processing_jobs SET due_at = now() WHERE id = %s", (job_id,)
        )


def _calculate(database: Database, processor, tag: str) -> list[str]:
    """Run ``tag``'s waiting day calculations, retries included, as the
    worker would; return their outcomes."""
    outcomes = []
    while jobs := _waiting(database, ["reconcile_ranked_day"], tag):
        for job_id in jobs:
            _due_now(database, job_id)
            result = processor.process_job(job_id, owner=f"day-{job_id}")
            assert result is not None
            outcomes.append(result.outcome)
    return outcomes


def _build(database: Database, processor) -> None:
    """Run the waiting builds one at a time, with the publication check
    before each, as the worker runs it every 10 seconds."""
    while True:
        boundary_publication.reevaluate_boundary_publications(database)
        if not (jobs := _waiting(database, BUILDS)):
            return
        result = processor.process_job(jobs[0], owner=f"build-{jobs[0]}")
        assert result is not None and result.outcome == "processed", result


def _board(database: Database) -> tuple[int, list[str]]:
    """The newest published board for DAY_END: its generation, and the
    players it ranks with their day's result."""
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT generation, snapshot_id, snapshot_manifest_id
            FROM boundary_publication_generations
            WHERE boundary_at = %s AND snapshot_state = 'published'
              AND army_state = 'published'
            ORDER BY generation DESC LIMIT 1
            """,
            (DAY_END,),
        ).fetchone()
        assert row is not None, "no board published"
        players = connection.execute(
            """
            SELECT player.normalized_tag FROM leaderboard_snapshot_entries AS entry
            JOIN players AS player ON player.id = entry.player_id
            JOIN boundary_publication_manifest_entries(%s) AS frozen
              ON frozen.player_id = entry.player_id
            WHERE entry.snapshot_id = %s AND frozen.ranked_day_version_id IS NOT NULL
            ORDER BY player.normalized_tag
            """,
            (row[2], row[1]),
        ).fetchall()
    return int(row[0]), [text(tag) for (tag,) in players]


def _members(database: Database) -> tuple[int, dict[str, str]]:
    """The newest generation for DAY_END and each member's result state."""
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT generation.generation, player.normalized_tag, member.snapshot_status
            FROM boundary_publication_generation_members AS member
            JOIN boundary_publication_generations AS generation
              ON generation.id = member.generation_id
            JOIN players AS player ON player.id = member.player_id
            WHERE generation.id = (
                SELECT id FROM boundary_publication_generations
                WHERE boundary_at = %s ORDER BY generation DESC LIMIT 1)
            """,
            (DAY_END,),
        ).fetchall()
    return int(rows[0][0]), {text(tag): text(state) for _, tag, state in rows}


def test_board_publishes_at_0530_without_an_unfinished_player_and_adds_them_once_done(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            _calculate(database, processor, "#2PP")
            # Before 05:30 the board waits for the player still unfinished.
            monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database) == (1, {"#2PP": "complete", LATE: "pending"})
            # From 05:30 until 06:00, more unfinished than it may publish
            # without means something bigger is broken: it keeps waiting.
            monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(minutes=30))
            monkeypatch.setattr(board_deadline, "HARD_DEADLINE", timedelta(days=36500))
            monkeypatch.setattr(board_deadline, "MAX_UNFINISHED", 0)
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database)[1][LATE] == "pending"
            assert _waiting(database, BUILDS) == []

            monkeypatch.setattr(board_deadline, "MAX_UNFINISHED", 50)
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database) == (1, {"#2PP": "complete", LATE: "unavailable"})
            _build(database, processor)
            assert _board(database) == (1, ["#2PP"])

            # Its day saves after the board published: the board is rebuilt
            # with it at once, not at the next hourly rebuild.
            assert set(_calculate(database, processor, LATE)) == {"processed"}
            assert _members(database) == (2, {"#2PP": "complete", LATE: "complete"})
            _build(database, processor)
            assert _board(database) == (2, ["#2PP", LATE])

            # Any other change waits for the hourly rebuild, then starts.
            _observation, job = store_observation(
                connection_info, archive_server, occurrence_key="late-attack",
                endpoint="battle_log", body=_attack(DAY_END - timedelta(minutes=10)),
                observed_at=DAY_END + timedelta(minutes=40), normalized_tag="#2PP",
            )
            assert processor.process_job(job, owner="late-attack").outcome == "processed"
            assert set(_calculate(database, processor, "#2PP")) == {"processed"}
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database)[0] == 2
            with database.pool.connection() as connection:
                now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
            monkeypatch.setattr(
                past_reset_pacing, "_now", lambda _connection: now + timedelta(minutes=61)
            )
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database)[0] == 3
            _build(database, processor)
            assert _board(database) == (3, ["#2PP", LATE])
        finally:
            database.close()


def test_at_0600_the_board_publishes_without_every_unfinished_player(
    database_url: str, archive_server, monkeypatch, tmp_path, capsys
) -> None:
    # Before, more than 50 unfinished kept it waiting for good, and once it
    # published without them no alert said so.
    late = [f"#{_tag(number)}" for number in range(1, 52)]
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        (tmp_path / "url").write_text(_as_worker(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(tmp_path / "url"))

        def left_out() -> list[int]:
            """What the alert check reads: the newest Reset whose board left
            out over 50 unfinished players, and how many."""
            capsys.readouterr()
            alerts.reset_probe("0", "0")
            return [int(value) for value in capsys.readouterr().out.split()[7:]]

        try:
            _swept(connection_info, archive_server, processor, ("#2PP", *late))
            _calculate(database, processor, "#2PP")
            monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(minutes=30))
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database) == (
                1, {"#2PP": "complete", **dict.fromkeys(late, "unavailable")})
            _build(database, processor)
            assert _board(database) == (1, ["#2PP"])
            assert left_out() == [int(DAY_END.timestamp()), 51]

            # Their days save later, and the board is rebuilt with them.
            for tag in late:
                assert set(_calculate(database, processor, tag)) == {"processed"}
            _build(database, processor)
            assert sorted(_board(database)[1]) == sorted(["#2PP", *late])
            # The alert moves on to the Reset before, whose board left them
            # out too and was never rebuilt: their days before never saved.
            assert left_out() == [int(DAY_START.timestamp()), 51]
        finally:
            database.close()


def test_the_board_publishes_before_its_army_inputs_freeze(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, the army's inputs froze in the board's own transaction, for
    # 2 min 58 s on 14 Oct 2026, and its build job stayed unseen until then.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            _build(database, processor)
            assert _board(database) == (1, list(PLAYERS))
            with database.pool.connection() as connection:
                published, army_frozen = connection.execute(
                    """
                    SELECT snapshot.published_at, manifest.frozen_at
                    FROM boundary_publication_generations AS generation
                    JOIN leaderboard_snapshots AS snapshot
                      ON snapshot.id = generation.snapshot_id
                    JOIN boundary_publication_manifests AS manifest
                      ON manifest.generation_id = generation.id
                     AND manifest.artifact_kind = 'army'
                    WHERE generation.boundary_at = %s AND generation.generation = 1
                    """,
                    (DAY_END,),
                ).fetchone()
            assert published < army_frozen
        finally:
            database.close()


def test_the_board_is_served_once_built_before_its_statistics(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, it waited for the army share code statistics no Daily board
    # shows: 1 min 22 s on 14 Oct 2026.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            for build in _waiting(database, ["build_snapshot"]):
                assert processor.process_job(build, owner="build").outcome == "processed"
            assert _waiting(database, ["build_analytics"])
            api = ApiDatabase(as_api_role(connection_info))
            try:
                served = api_leaderboard.get_frozen_leaderboard(api, limit=100)
            finally:
                api.close()
            assert served is not None and served["reset_at"] == DAY_END.isoformat()
            assert sorted(entry["tag"] for entry in served["entries"]) == list(PLAYERS)
            _build(database, processor)
            assert _board(database) == (1, list(PLAYERS))
        finally:
            database.close()


@pytest.mark.parametrize("failing", ["build_snapshot", "build_analytics"])
def test_a_board_build_that_fails_once_runs_again_and_publishes(
    database_url: str, archive_server, monkeypatch, failing: str
) -> None:
    # Before, one error failed its job for good, and the next one met the
    # failed job's key: the board never published.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    module, name = {"build_snapshot": (snapshots, "complete_snapshot"),
                    "build_analytics": (boundary_publication, "complete_analytics")}[failing]
    run, faults = getattr(module, name), [ValueError("a build fault")]

    def faulty(database, claim):
        if faults and claim.input_json.get("boundary_at") == f"{DAY_END:%Y-%m-%dT%H:%M:%SZ}":
            raise faults.pop()
        return run(database, claim)

    monkeypatch.setattr(module, name, faulty)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            outcomes = []
            for _ in range(10):
                boundary_publication.reevaluate_boundary_publications(database)
                for job_id in _waiting(database, BUILDS):
                    _due_now(database, job_id)
                    outcomes.append(processor.process_job(job_id, owner=f"build-{job_id}").outcome)
            assert (faults, outcomes.count("failed")) == ([], 1)
            assert _board(database) == (1, list(PLAYERS))
        finally:
            database.close()


def test_army_results_publish_while_the_boards_statistics_keep_failing(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, the army's inputs froze only once the statistics published, so
    # statistics failing every time kept the army results back for good.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))

    def failing(_database, _claim):
        raise ValueError("a statistics fault")

    monkeypatch.setattr(boundary_publication, "complete_analytics", failing)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            for _ in range(4):
                boundary_publication.reevaluate_boundary_publications(database)
                for job_id in _waiting(database, BUILDS):
                    _due_now(database, job_id)
                    processor.process_job(job_id, owner=f"build-{job_id}")
            with database.pool.connection() as connection:
                states = connection.execute(
                    "SELECT snapshot_state, army_state FROM boundary_publication_generations"
                    " WHERE boundary_at = %s",
                    (DAY_END,),
                ).fetchall()
            assert [tuple(map(text, row)) for row in states] == [("building", "published")]
        finally:
            database.close()


def test_a_battle_log_job_leaves_the_army_freeze_to_the_background_check(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, a battle log saving an army for the ended day froze the army's
    # inputs in its own job: 3 to 9 minutes of a response slot.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            boundary_publication.reevaluate_boundary_publications(database)
            for build in _waiting(database, ["build_snapshot"]):
                assert processor.process_job(build, owner="build").outcome == "processed"

            def army_frozen() -> bool:
                with database.pool.connection() as connection:
                    return connection.execute(
                        "SELECT army_manifest_id IS NOT NULL FROM boundary_publication_generations"
                        " WHERE boundary_at = %s",
                        (DAY_END,),
                    ).fetchone()[0]

            _observation, job = store_observation(
                connection_info, archive_server, occurrence_key="late-attack",
                endpoint="battle_log", body=_attack(DAY_END - timedelta(minutes=10)),
                observed_at=DAY_END + timedelta(minutes=40), normalized_tag="#2PP",
            )
            assert processor.process_job(job, owner="battle-log").outcome == "processed"
            frozen_by_the_job = army_frozen()
            boundary_publication.reevaluate_boundary_publications(database)
            assert [frozen_by_the_job, army_frozen()] == [False, True]
        finally:
            database.close()


def test_a_failed_build_runs_again_only_once_a_repair_lets_it(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, it ran again while the repair held its Reset.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    monkeypatch.setattr(board_deadline, "HARD_DEADLINE", timedelta(days=36500))
    run, faults = snapshots.complete_snapshot, [ValueError("a build fault")]
    reset = f"{DAY_END:%Y-%m-%dT%H:%M:%SZ}"

    def faulty(database, claim):
        if faults and claim.input_json["boundary_at"] == reset:
            raise faults.pop()
        return run(database, claim)

    monkeypatch.setattr(snapshots, "complete_snapshot", faulty)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            boundary_publication.reevaluate_boundary_publications(database)
            for build in _waiting(database, ["build_snapshot"]):
                processor.process_job(build, owner="build")
            assert faults == []
            _repair_lists(connection_info, "active")
            boundary_publication.reevaluate_boundary_publications(database)
            with database.pool.connection() as connection:
                held = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                    " AND work_type = 'build_snapshot' AND input_json ->> 'boundary_at' = %s",
                    (reset,),
                ).fetchall()
                connection.execute("UPDATE domain_repair_items SET state = 'done'")
            boundary_publication.reevaluate_boundary_publications(database)
            for job_id in _waiting(database, BUILDS):
                _due_now(database, job_id)
            _build(database, processor)
            assert (held, _board(database)) == ([], (1, list(PLAYERS)))
        finally:
            database.close()


def test_a_past_resets_statistics_wait_out_the_quiet_window(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, only its board and army builds waited; its statistics ran.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            boundary_publication.reevaluate_boundary_publications(database)
            for build in _waiting(database, ["build_snapshot"]):
                assert processor.process_job(build, owner="build").outcome == "processed"
            with database.pool.connection() as connection:
                connection.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
                    " membership_captured_at) VALUES (%s, '{}', clock_timestamp())",
                    (DAY_END + timedelta(days=1),),
                )
            claimed = []
            for hour, minute in ((4, 31), (7, 0)):
                at = (DAY_END + timedelta(days=1)).replace(hour=hour, minute=minute)
                monkeypatch.setattr(past_reset_pacing, "_now", lambda _connection, at=at: at)
                claim = database.claim_job(owner="statistics", work_types=["build_analytics"])
                claimed.append(claim and claim.work_type)
            assert claimed == [None, "build_analytics"]
        finally:
            database.close()


def test_an_army_freeze_past_the_query_limit_waits_before_trying_again(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, every background pass tried it again, 15 minutes each time,
    # and only a log line said so.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    army_rows, tries = boundary_manifest._army_rows, []

    def slow_once(connection, members, generation):
        if generation[0] != DAY_END:
            return army_rows(connection, members, generation)
        tries.append(generation[1])
        if len(tries) == 1:
            connection.execute("SET LOCAL statement_timeout = '50ms'")
            connection.execute("SELECT pg_sleep(1)")
        return army_rows(connection, members, generation)

    monkeypatch.setattr(boundary_manifest, "_army_rows", slow_once)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            _build(database, processor)
            boundary_publication.reevaluate_boundary_publications(database)
            with database.pool.connection() as connection:
                # The alert check's failed-work count, which raises its alerts.
                alerted = health_metrics(connection)["failed_processing"]
                failed = connection.execute(
                    "UPDATE python_processing_jobs SET updated_at = updated_at"
                    " - interval '1 minute' WHERE failure_category = 'freeze_timed_out'"
                    " RETURNING work_type, input_json ->> 'generation'"
                ).fetchall()
            waited = list(tries)
            # A minute later it tries again, and the army results publish.
            _build(database, processor)
            assert ([tuple(map(text, row)) for row in failed], alerted, waited, tries) == (
                [("build_army_analytics", "1")], 1, [1], [1, 1])
            assert _board(database) == (1, list(PLAYERS))
        finally:
            database.close()


def _repair_lists(connection_info: str, state: str) -> None:
    """A Season repair in ``state`` that lists DAY_END's Reset, not yet done."""
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            WITH campaign AS (
                INSERT INTO domain_repair_campaigns (
                    official_season_id, season_start, season_end, write_deadline,
                    state, target_versions, inventory_cutoff, plan_digest)
                VALUES ('repair', %(start)s, %(start)s + interval '28 days',
                        %(start)s + interval '35 days', %(state)s, '{}', %(start)s,
                        repeat('0', 64))
                RETURNING id)
            INSERT INTO domain_repair_items (
                campaign_id, kind, target_key, reasons, boundary_at)
            SELECT id, 'publication', 'reset', ARRAY['payout'], %(reset)s
            FROM campaign
            """,
            {"start": DAY_START - timedelta(days=3), "reset": DAY_END, "state": state},
        )


def test_a_paused_repair_holds_no_first_board_past_0600(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, it held the board until someone resumed or ended the repair.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _repair_lists(connection_info, "paused")
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            # Before 06:00 the repair holds it.
            monkeypatch.setattr(board_deadline, "HARD_DEADLINE", timedelta(days=36500))
            _build(database, processor)
            assert _waiting(database, BUILDS) == []
            monkeypatch.setattr(board_deadline, "HARD_DEADLINE", timedelta(hours=1))
            _build(database, processor)
            assert _board(database) == (1, list(PLAYERS))
        finally:
            database.close()


def test_a_repair_holds_a_correction_started_before_it_past_0600(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, the replaced generation made the Reset look never published, so
    # past 06:00 the repair let the correction build.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    monkeypatch.setattr(past_reset_pacing, "NEWEST_RESET_CORRECTION_INTERVAL", timedelta.min)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                _calculate(database, processor, tag)
            _build(database, processor)
            assert _board(database) == (1, list(PLAYERS))
            with database.pool.connection() as connection:
                boundary_publication._queue_boundary_army_correction(
                    database, connection, boundary_at=DAY_END,
                    generation_id=connection.execute(
                        "SELECT id FROM boundary_publication_generations WHERE boundary_at = %s",
                        (DAY_END,),
                    ).fetchone()[0],
                )
            _repair_lists(connection_info, "active")
            _build(database, processor)
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT generation, army_manifest_id FROM boundary_publication_generations"
                    " WHERE boundary_at = %s ORDER BY generation DESC LIMIT 1",
                    (DAY_END,),
                ).fetchone() == (2, None)
                connection.execute("UPDATE domain_repair_items SET state = 'done'")
            _build(database, processor)
            assert _board(database) == (2, list(PLAYERS))
        finally:
            database.close()


@pytest.mark.parametrize("failing", [(LATE,), PLAYERS])
def test_a_day_that_fails_for_good_leaves_the_board_to_publish_without_it(
    database_url: str, archive_server, monkeypatch, failing: tuple[str, ...]
) -> None:
    # Before, a calculation fault was raised out of the worker on each try,
    # and the board waited for the day until a fix shipped.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    calculate, fault = reconciliation_db.reconcile_ranked_day, [False]

    def faulty(data):
        assert not fault[0], "a calculation fault"
        return calculate(data)

    monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day", faulty)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            for tag in PLAYERS:
                fault[0] = tag in failing
                assert ("failed" in _calculate(database, processor, tag)) == fault[0]
            boundary_publication.reevaluate_boundary_publications(database)
            assert _members(database) == (1, {
                tag: "failed" if tag in failing else "complete" for tag in PLAYERS})
            _build(database, processor)
            assert _board(database) == (1, [tag for tag in PLAYERS if tag not in failing])

            # Fixed and calculated again, the days join the board at once.
            fault[0] = False
            for tag in failing:
                reconciliation_db.enqueue_reconciliation(
                    database, player_tag=tag, day_start=DAY_START, now=DAY_END,
                    request_key="after-the-fix",
                )
                assert set(_calculate(database, processor, tag)) == {"processed"}
            _build(database, processor)
            assert _board(database)[1] == list(PLAYERS)
        finally:
            database.close()


def test_a_day_meeting_a_busy_reset_hands_its_lease_back_and_backs_off(
    database_url: str, archive_server
) -> None:
    # Before, it kept its lease and its try, and came back when the lease
    # ran out at the head of the queue, as the oldest waiting work.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            with database.pool.connection() as connection:
                job_id = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                    " AND work_type = 'reconcile_ranked_day' AND input_json @> %s",
                    (Jsonb({"ranked_day_start": f"{DAY_START:%Y-%m-%dT%H:%M:%SZ}"}),),
                ).fetchone()[0]

            def job() -> tuple[str, str | None, float, int]:
                with database.pool.connection() as connection:
                    state, owner, wait, attempts = connection.execute(
                        "SELECT status, lease_owner,"
                        " extract(epoch FROM due_at - clock_timestamp()), attempt_count"
                        " FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    ).fetchone()
                return text(state), owner, float(wait), attempts

            with psycopg.connect(connection_info) as publisher:
                boundary.lock_boundary_publication(publisher, DAY_END)
                for low in (30, 60):
                    result = processor.process_job(job_id, owner="busy")
                    assert (result.outcome, result.category) == (
                        "retrying", "database_lock_busy")
                    state, owner, wait, attempts = job()
                    # 30 seconds, then 60 at most for Reset work, spread over
                    # 10 so they do not all come back at once.
                    assert (state, owner, attempts) == ("waiting_dependency", None, 1)
                    assert low - 2 < wait < low + 10
                    assert database.claim_job(owner="early", job_id=job_id) is None
                    _due_now(database, job_id)
            assert processor.process_job(job_id, owner="free").outcome == "processed"
            assert job()[::3] == ("complete", 1)
            assert _members(database)[1]["#2PP"] == "complete"
        finally:
            database.close()


def _release_after_a_wait(publisher) -> None:
    """End ``publisher``'s transaction once another session has waited 1 s
    for one of its locks, or after 10 s."""
    for _ in range(500):
        if publisher.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ).fetchone()[0]:
            time.sleep(1)
            break
        time.sleep(0.02)
    publisher.commit()


def test_the_last_day_waits_out_a_brief_hold_on_the_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, it gave up after 250 ms and came back 30 to 60 s later: on
    # 14 Oct 2026 the last day saved 2 min 35 s late.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            _calculate(database, processor, "#2PP")
            with psycopg.connect(connection_info) as publisher:
                boundary.lock_boundary_publication(publisher, DAY_END)
                holder = threading.Thread(target=_release_after_a_wait, args=(publisher,))
                holder.start()
                outcomes = _calculate(database, processor, LATE)
                holder.join()
            assert set(outcomes) == {"processed"}
            assert _members(database) == (1, {"#2PP": "complete", LATE: "complete"})
        finally:
            database.close()


def test_the_0530_check_waits_out_a_brief_hold_on_the_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Before, it waited 50 ms and skipped the Reset: on 14 Oct 2026 it left
    # no one out.
    monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(days=36500))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            _calculate(database, processor, "#2PP")
            monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(minutes=30))
            with psycopg.connect(connection_info) as publisher:
                boundary.lock_boundary_publication(publisher, DAY_END)
                holder = threading.Thread(target=_release_after_a_wait, args=(publisher,))
                holder.start()
                boundary_publication.reevaluate_boundary_publications(database)
                holder.join()
            assert _members(database) == (1, {"#2PP": "complete", LATE: "unavailable"})
            _build(database, processor)
            assert _board(database) == (1, ["#2PP"])
        finally:
            database.close()


def test_the_day_recheck_pass_waits_for_room_and_never_runs_into_the_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    # On 11 Oct 2026 the 23:00 pass queued about 17,500 rechecks into a lane
    # finishing 5,800 to 8,250 an hour, and the work due rose into the Reset.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _swept(connection_info, archive_server, processor)
            _calculate(database, processor, "#2PP")
            _calculate(database, processor, LATE)
            recheck = queue_refresh.DailyRecheck(database)
            night = DAY_END.replace(hour=23)

            def queued(at: datetime) -> int:
                recheck.next_check_at = float("-inf")
                recheck.run_when_due(at)
                with database.pool.connection() as connection:
                    return connection.execute(
                        "SELECT count(*) FROM python_processing_jobs"
                        " WHERE deduplication_key LIKE 'reconcile:recheck:%'"
                    ).fetchone()[0]

            # After midnight it waits for the next evening, so it never runs
            # into the Reset.
            assert queued(night + timedelta(hours=1, minutes=5)) == 0
            # A recheck already due fills the lane, then late live work.
            monkeypatch.setattr(queue_refresh, "RECHECK_ROOM", 1)
            monkeypatch.setattr(queue_refresh, "LIVE_LAG_MIN_JOBS", 1)
            day_text = f"{DAY_START:%Y-%m-%dT%H:%M:%SZ}"
            with database.pool.connection() as connection:
                for priority, key in ((queue_refresh.DAY_RECHECK_PRIORITY, "due-recheck"),
                                      (100, "late-live")):
                    connection.execute(
                        """
                        INSERT INTO python_processing_jobs (
                            work_type, deduplication_key, input_json, priority, due_at)
                        SELECT 'reconcile_ranked_day', %s,
                               jsonb_build_object('player_id', id, 'ranked_day_start', %s::text),
                               %s, now() - interval '10 minutes'
                        FROM players WHERE normalized_tag = %s
                        """,
                        (key, day_text, priority, LATE),
                    )
            assert queued(night) == 0
            for key in ("due-recheck", "late-live"):
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE python_processing_jobs SET status = 'complete',"
                        " completed_at = now() WHERE deduplication_key = %s",
                        (key,),
                    )
                assert (queued(night) > 0) == (key == "late-live")
        finally:
            database.close()


def test_worker_sessions_compile_no_query_code(database_url: str) -> None:
    # Compiling a plan's code took 24% of database time on 12 Oct 2026.
    with domain_database(database_url) as connection_info:
        database = Database(connection_info, statement_timeout_seconds=WORKER_STATEMENT_TIMEOUT_SECONDS)
        try:
            with database.pool.connection() as connection:
                assert connection.execute("SHOW jit").fetchone()[0] == "off"
        finally:
            database.close()
