"""The Reset board's member results run side by side, and Reset work goes first.

On 2026-10-06 every Reset reading, battle log and day result took the Reset's
publication lock in turn, so the 12,795-member board was still waiting at
06:00. These tests hold one member result open and check the others do not
wait behind it, that the last results still see every other one before the
board is built, and that Reset work is claimed before live work, while live
work that has waited 20 minutes still gets every other claim.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database
from test_boundary_publication_lock_order_postgres import _wait_for_waiters
from test_boundary_publication_postgres import (
    BOUNDARY,
    _player_and_version,
    _sweep_with_members,
)
from test_claim_jobs_postgres import (
    _insert_job,
    _insert_observation,
    _production_database,
)

from clashlens import boundary, reset_baselines
from clashlens.collector_db import CollectorDatabase
from clashlens.db import (
    POPULATION_BUILD_WORK_TYPES,
    PYTHON_LIVE_PRIORITY,
    PYTHON_RESET_PRIORITY,
    RESPONSE_WORK_TYPES,
    Database,
    ended_day_priority,
)
from clashlens.worker import DERIVED_WITHOUT_BUILDS, ObservationProcessor

# One more member than the tail, plus room to keep results shared.
MEMBERS = boundary.OPEN_GENERATION_TAIL + 5


def _board(connection, count: int) -> list[tuple[int, int]]:
    players = [
        _player_and_version(connection, f"#B{index}", 1, "a" * 64)
        for index in range(count)
    ]
    _sweep_with_members(connection, [player_id for player_id, _ in players])
    return players


def _record(database: Database, connection, player: tuple[int, int]) -> None:
    assert boundary._record_boundary_generation(
        database,
        connection,
        boundary_at=BOUNDARY,
        player_id=player[0],
        ranked_day_version_id=player[1],
        ranked_day_input_hash="a" * 64,
        reset_lock_wait="1s",
    )


def _states(connection) -> tuple[int, int]:
    """(members still pending, snapshot builds queued)."""
    return connection.execute(
        """
        SELECT
            (SELECT count(*) FROM boundary_publication_generation_members
             WHERE status = 'pending'),
            (SELECT count(*) FROM python_processing_jobs
             WHERE work_type = 'build_snapshot')
        """
    ).fetchone()


def test_member_results_do_not_wait_for_each_other(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                players = _board(connection, MEMBERS)
                # The first result creates the board under the full lock.
                _record(database, connection, players[0])
            with (
                psycopg.connect(connection_info) as held,
                psycopg.connect(connection_info) as other,
            ):
                _record(database, held, players[1])
                # Before, this waited a second for ``held`` and gave up.
                _record(database, other, players[2])
                other.commit()
                held.commit()
            with database.pool.connection() as connection:
                assert _states(connection) == (MEMBERS - 3, 0)
        finally:
            database.close()


def test_last_results_see_every_other_one_and_build_once(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        errors: list[BaseException] = []
        try:
            with database.pool.connection() as connection:
                players = _board(connection, MEMBERS)
                _record(database, connection, players[0])

            def record_rest() -> None:
                try:
                    with psycopg.connect(connection_info) as connection:
                        for player in players[2:]:
                            boundary._record_boundary_generation(
                                database,
                                connection,
                                boundary_at=BOUNDARY,
                                player_id=player[0],
                                ranked_day_version_id=player[1],
                                ranked_day_input_hash="a" * 64,
                            )
                            connection.commit()
                except BaseException as error:  # noqa: BLE001 - asserted below
                    errors.append(error)

            with (
                psycopg.connect(connection_info) as held,
                psycopg.connect(connection_info, autocommit=True) as observer,
            ):
                # Shared while more than the tail wait; it stays uncommitted
                # while the rest finish.
                _record(database, held, players[1])
                worker = threading.Thread(target=record_rest)
                worker.start()
                # The tail's full lock waits for the shared result.
                _wait_for_waiters(observer, 1, errors)
                assert _states(observer)[1] == 0
                time.sleep(0.2)
                assert worker.is_alive()
                held.commit()
                worker.join(60)
            assert errors == []
            with database.pool.connection() as connection:
                # The last result saw the held one, so the board is built once.
                assert _states(connection) == (0, 1)
        finally:
            database.close()


def _member(connection, player_id: int) -> tuple[str, str]:
    status, snapshot_status = connection.execute(
        """
        SELECT status, snapshot_status FROM boundary_publication_generation_members
        WHERE player_id = %s
        """,
        (player_id,),
    ).fetchone()
    return str(status), str(snapshot_status)


def test_reset_replay_keeps_the_result_it_waited_for(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        errors: list[BaseException] = []
        try:
            with database.pool.connection() as connection:
                players = _board(connection, MEMBERS)
                _record(database, connection, players[0])
                sweep_id = int(
                    connection.execute(
                        "SELECT id FROM collector_reset_sweeps WHERE boundary_at = %s",
                        (BOUNDARY,),
                    ).fetchone()[0]
                )

            def replay() -> None:
                try:
                    with psycopg.connect(connection_info) as connection:
                        reset_baselines._record_boundary_baseline(
                            database,
                            connection,
                            boundary_at=BOUNDARY,
                            reset_sweep_id=sweep_id,
                            player_id=players[1][0],
                            state="complete",
                        )
                        connection.commit()
                except BaseException as error:  # noqa: BLE001 - asserted below
                    errors.append(error)

            with (
                psycopg.connect(connection_info) as held,
                psycopg.connect(connection_info, autocommit=True) as observer,
            ):
                # A day result writes the member and is still uncommitted when
                # a replay of the member's Reset reading arrives.
                _record(database, held, players[1])
                written = _member(held, players[1][0])
                assert written[0] != "pending"
                worker = threading.Thread(target=replay)
                worker.start()
                deadline = time.monotonic() + 30
                while not errors and not observer.execute(
                    "SELECT count(*) FROM pg_locks"
                    " WHERE NOT granted AND locktype IN ('transactionid', 'tuple')"
                ).fetchone()[0]:
                    assert time.monotonic() < deadline, "the replay never waited"
                    time.sleep(0.02)
                held.commit()
                worker.join(60)
            assert errors == []
            with database.pool.connection() as connection:
                assert _member(connection, players[1][0]) == written
        finally:
            database.close()


def test_ended_day_work_has_reset_priority() -> None:
    now = datetime.now(UTC)
    today = now.replace(minute=0, second=0, microsecond=0)
    if today.hour < 5:
        today -= timedelta(days=1)
    today = today.replace(hour=5)
    assert ended_day_priority(today - timedelta(days=1)) == PYTHON_RESET_PRIORITY
    assert ended_day_priority(today) == PYTHON_LIVE_PRIORITY
    assert ended_day_priority(today - timedelta(days=2)) == PYTHON_LIVE_PRIORITY


def test_collector_queues_reset_readings_at_reset_priority(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            priorities = {}
            for kind in ("reset_baseline", "reset_settlement", None):
                handoff = SimpleNamespace(
                    occurrence_key=f"priority-{kind}",
                    response_completed_at=datetime.now(UTC),
                )
                job_id = CollectorDatabase._upsert_processing_job(
                    connection,
                    handoff,
                    _insert_observation(connection, occurrence_key=f"priority-{kind}"),
                    "supercell-source-parser-v2",
                    kind,
                )
                priorities[kind] = connection.execute(
                    "SELECT priority FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()[0]
        assert priorities == {
            "reset_baseline": PYTHON_RESET_PRIORITY,
            "reset_settlement": PYTHON_LIVE_PRIORITY,
            None: PYTHON_LIVE_PRIORITY,
        }


def test_reset_work_goes_before_recent_live_work(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            jobs = {
                key: _insert_job(
                    connection,
                    work_type="process_observation",
                    deduplication_key=f"order:{key}",
                    input_json={},
                    observation_id=_insert_observation(
                        connection, occurrence_key=f"order-{key}"
                    ),
                    priority=priority,
                )
                for key, priority in (
                    ("live", PYTHON_LIVE_PRIORITY),
                    ("delayed", PYTHON_RESET_PRIORITY),
                    ("reset", PYTHON_RESET_PRIORITY),
                    ("old", PYTHON_LIVE_PRIORITY),
                )
            }
            # Live work that has waited 15 minutes still yields; after 25 it
            # goes first, so a Reset cannot hold live data back for long,
            # even a Reset reading the collector delayed by 30 minutes: on
            # 2026-10-07 an outage delayed about 19,000 of them, and live
            # pages fell up to 59 minutes behind while they all went first.
            # This holds for live jobs among those a claim looks at; a retried
            # live job, due again from its retry time, can still wait longer.
            for key, waited in (
                ("live", "15 minutes"),
                ("delayed", "30 minutes"),
                ("old", "25 minutes"),
            ):
                connection.execute(
                    "UPDATE python_processing_jobs"
                    " SET created_at = clock_timestamp() - %s::interval,"
                    " due_at = clock_timestamp() - %s::interval WHERE id = %s",
                    (waited, waited, jobs[key]),
                )
            connection.commit()
        database = Database(connection_info)
        try:
            claimed = [
                database.claim_job(owner=f"lane-{index}") for index in range(4)
            ]
        finally:
            database.close()
        assert [claim.job_id for claim in claimed if claim] == [
            jobs["old"], jobs["delayed"], jobs["reset"], jobs["live"]
        ]


def test_planned_claim_yields_to_waiting_reset_work(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            jobs = {
                key: _insert_job(
                    connection,
                    work_type="process_observation",
                    deduplication_key=f"planned:{key}",
                    input_json={},
                    observation_id=_insert_observation(
                        connection, occurrence_key=f"planned-{key}"
                    ),
                    priority=priority,
                )
                for key, priority in (
                    ("reset", PYTHON_RESET_PRIORITY),
                    ("planned", PYTHON_LIVE_PRIORITY),
                    ("asked", PYTHON_LIVE_PRIORITY),
                )
            }
            connection.commit()
        database = Database(connection_info)
        try:
            # Asking for one job by number still takes it.
            asked = database.claim_job(owner="operator", job_id=jobs["asked"])
            claimed = [
                database.claim_job(owner="lane", job_id=jobs["planned"], planned=True)
                for _ in range(2)
            ]
        finally:
            database.close()
        assert asked is not None and asked.job_id == jobs["asked"]
        assert [claim.job_id if claim else None for claim in claimed] == [
            jobs["reset"], jobs["planned"]
        ]


def test_reset_priority_jobs_are_claimable_alongside_unlimited_work(
    database_url: str,
) -> None:
    # A Reset day result is claimed by a lane limited to derived work.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            job_id = _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:reset-priority",
                input_json={"player_id": 1, "ranked_day_start": "2026-08-03T05:00:00Z"},
                priority=PYTHON_RESET_PRIORITY,
            )
            connection.commit()
        database = Database(connection_info)
        try:
            claim = database.claim_job(
                owner="derived", work_types=("reconcile_ranked_day",)
            )
        finally:
            database.close()
        assert claim is not None and claim.job_id == job_id


@pytest.mark.parametrize("live_waited", ["25 minutes", "1 minute"])
def test_reset_backlog_and_live_work_take_turns_in_every_lane(
    database_url: str, live_waited: str
) -> None:
    # Around 05:21 the new day's recalculations queued at 05:00 have waited
    # over 20 minutes, so they win on waiting time while the ended day's
    # results the board needs are still queued. On 8 Oct 2026 live responses
    # that had waited under 20 minutes lost every claim to the Reset backlog
    # instead, for 21 minutes. Each lane alternates, so both keep moving: the
    # Reset backlog finishes, and live work never waits more than one turn per
    # lane behind it, even on the lane that first checks for a build and finds
    # none.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            classes: dict[int, str] = {}
            for work_type, count in (
                ("process_observation", 6), ("reconcile_ranked_day", 6)
            ):
                for index in range(count):
                    for kind, priority in (
                        ("reset", PYTHON_RESET_PRIORITY),
                        ("live", PYTHON_LIVE_PRIORITY),
                    ):
                        key = f"turns:{work_type}:{kind}:{index}"
                        job = (
                            {"input_json": {}, "observation_id": _insert_observation(
                                connection, occurrence_key=key
                            )}
                            if work_type == "process_observation"
                            else {"input_json": {
                                "player_id": index + 1,
                                "ranked_day_start": "2026-08-03T05:00:00Z",
                            }}
                        )
                        job_id = _insert_job(
                            connection,
                            work_type=work_type,
                            deduplication_key=key,
                            priority=priority,
                            **job,
                        )
                        classes[job_id] = kind
            connection.execute(
                "UPDATE python_processing_jobs"
                " SET created_at = clock_timestamp() - %s::interval,"
                " due_at = clock_timestamp() - %s::interval"
                " WHERE priority = %s",
                (live_waited, live_waited, PYTHON_LIVE_PRIORITY),
            )
            connection.commit()
        database = Database(connection_info)
        try:
            processor = ObservationProcessor(database, archive=object())
            claimed = {
                lane: [
                    processor._claim_next(
                        owner=lane, lease_seconds=60, work_types=work_types
                    )
                    for _ in range(count)
                ]
                for lane, work_types, count in (
                    ("responses", RESPONSE_WORK_TYPES, 12),
                    ("results", DERIVED_WITHOUT_BUILDS, 6),
                )
            }
            claimed["builds"] = [
                processor._claim_next(
                    owner="builds", lease_seconds=60, work_types=work_types
                )
                for _ in range(6)
                for work_types in (POPULATION_BUILD_WORK_TYPES, DERIVED_WITHOUT_BUILDS)
            ]
        finally:
            database.close()
        assert {
            lane: [classes[claim.job_id] for claim in claims if claim]
            for lane, claims in claimed.items()
        } == {
            "responses": ["reset", "live"] * 6,
            "results": ["reset", "live"] * 3,
            "builds": ["reset", "live"] * 3,
        }
        assert claimed["builds"][::2] == [None] * 6
