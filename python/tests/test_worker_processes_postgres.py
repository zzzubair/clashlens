"""Two worker processes share one queue without repeating or losing work.

On 8 Oct 2026 one worker process handled 894 responses a minute: its Python
interpreter runs one thread at a time, so more lanes did not help. These
tests run two processors, each with its own database pools as two worker
processes have, against one queue.
"""

from __future__ import annotations

import threading
import time
from threading import Event

import psycopg
import pytest
from domain_test_support import domain_database, text
from test_domain_processing_postgres import _processor
from test_worker_reserved_capacity_postgres import (
    _queue_builds,
    _queue_profiles,
    _wait_for_statuses,
)

from clashlens import army_ingestion
from clashlens.db import (
    POPULATION_BUILD_WORK_TYPES,
    RESPONSE_WORK_TYPES,
    Database,
    LeaseLost,
)
from clashlens.worker import (
    MaintenancePermit,
    ObservationProcessor,
    process_until_stopped,
)

TAGS = [f"#2{first}{second}" for first in "PYLQ" for second in "GRJCU"]


def _jobs(connection_info: str, job_ids: list[int]) -> dict[int, tuple]:
    with psycopg.connect(connection_info) as connection:
        return {
            row[0]: (text(row[1]), row[2], row[3])
            for row in connection.execute(
                "SELECT id, status, attempt_count, lease_owner"
                " FROM python_processing_jobs WHERE id = ANY(%s)",
                (job_ids,),
            )
        }


def _attempts(connection_info: str, job_ids: list[int]) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return [
            (row[0], text(row[1]), text(row[2]) if row[2] is not None else None)
            for row in connection.execute(
                "SELECT job_id, state, failure_category FROM python_processing_attempts"
                " WHERE job_id = ANY(%s) ORDER BY job_id, attempt_number",
                (job_ids,),
            )
        ]


def test_one_claim_leases_a_batch_each_fenced_on_its_own(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        job_ids = _queue_profiles(connection_info, archive_server, TAGS[:5])
        database = Database(connection_info)
        try:
            claims = database.claim_jobs(
                owner="batch", lease_seconds=60, limit=3, work_types=RESPONSE_WORK_TYPES
            )
            assert len(claims) == 3
            assert len({claim.job_id for claim in claims}) == 3
            assert len({claim.lease_token for claim in claims}) == 3
            assert len({claim.attempt_id for claim in claims}) == 3
            jobs = _jobs(connection_info, job_ids)
            assert sorted(jobs[claim.job_id][:2] for claim in claims) == [("leased", 1)] * 3
            assert sum(state == "pending" for state, _, _ in jobs.values()) == 2

            # Each job keeps its own fence: renewing one with another's token fails.
            database.renew_claim(claims[0], lease_seconds=60)
            forged = claims[0].__class__(
                **{**{field: getattr(claims[0], field)
                      for field in claims[0].__dataclass_fields__},
                   "lease_token": claims[1].lease_token}
            )
            with pytest.raises(LeaseLost):
                database.renew_claim(forged, lease_seconds=60)
        finally:
            database.close()


def test_claims_given_back_unstarted_keep_their_attempts_and_run_at_once(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        job_ids = _queue_profiles(connection_info, archive_server, TAGS[:4])
        database = Database(connection_info)
        try:
            claims = database.claim_jobs(
                owner="stopping", lease_seconds=60, limit=4, work_types=RESPONSE_WORK_TYPES
            )
            assert len(claims) == 4
            # Another worker took one after its lease ran out: it stays theirs.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET lease_expires_at ="
                    " clock_timestamp() - interval '1 second' WHERE id = %s",
                    (claims[0].job_id,),
                )
            taken = database.claim_job(owner="other", lease_seconds=60, job_id=claims[0].job_id)
            assert taken is not None

            assert database.release_claims(claims) == 3
            jobs = _jobs(connection_info, job_ids)
            assert jobs[taken.job_id] == ("leased", 2, "other")
            released = [claim.job_id for claim in claims[1:]]
            assert [jobs[job_id] for job_id in released] == [("pending", 0, None)] * 3
            assert {
                (state, category)
                for job_id, state, category in _attempts(connection_info, released)
            } == {("stale", "claim_released")}
            with pytest.raises(LeaseLost):
                database.renew_claim(claims[1], lease_seconds=60)
            # Due at once, not after a lease time.
            again = database.claim_jobs(
                owner="next", lease_seconds=60, limit=4, work_types=RESPONSE_WORK_TYPES
            )
            assert sorted(claim.job_id for claim in again) == sorted(released)
            assert {claim.attempt_number for claim in again} == {2}
        finally:
            database.close()


def test_only_one_build_runs_across_worker_processes(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _queue_builds(connection_info, 3)
        first, second = Database(connection_info), Database(connection_info)
        try:
            build = first.claim_job(
                owner="process-1", lease_seconds=1, work_types=POPULATION_BUILD_WORK_TYPES
            )
            assert build is not None
            # A claimed build that has not started yet holds the permit.
            assert second.claim_job(owner="process-2", work_types=POPULATION_BUILD_WORK_TYPES) is None
            with first.pool.connection() as connection, connection.transaction():
                # A running build keeps it even after its lease runs out.
                first._lock_live_claim(connection, build, build=True)
                time.sleep(1.1)
                assert second.claim_job(
                    owner="process-2", work_types=POPULATION_BUILD_WORK_TYPES
                ) is None
            # The build's transaction ended without finishing it: its expired
            # lease is free to claim again, by either process.
            retried = second.claim_job(owner="process-2", work_types=POPULATION_BUILD_WORK_TYPES)
            assert retried is not None and retried.job_id == build.job_id
        finally:
            first.close()
            second.close()


def test_one_process_at_a_time_runs_publication_maintenance(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        first, second = MaintenancePermit(connection_info), MaintenancePermit(connection_info)
        try:
            assert first.acquire()
            assert not second.acquire()
            assert first.acquire()  # it keeps the permit while it runs
            first.close()  # a process that stops gives it up
            assert second.acquire()
            assert not first.acquire()
        finally:
            first.close()
            second.close()


def test_two_processes_finish_every_response_once_and_give_back_the_rest(
    database_url: str, archive_server, monkeypatch
) -> None:
    builds_running = 0
    most_builds = 0
    lock = threading.Lock()

    def counted_build(_database: object, _claim: object) -> None:
        nonlocal builds_running, most_builds
        with lock:
            builds_running += 1
            most_builds = max(most_builds, builds_running)
        time.sleep(0.2)
        with lock:
            builds_running -= 1
        raise ValueError("dependency_not_ready: test build waits")

    monkeypatch.setattr(army_ingestion, "complete_army_analytics", counted_build)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _queue_builds(connection_info, 4)
        job_ids = _queue_profiles(connection_info, archive_server, TAGS)
        processes = []
        for index in (1, 2):
            database, processor = _processor(
                connection_info,
                archive_server,
                database_factory=lambda info: Database(info, max_size=4),
            )
            processor.claim_batch = 4
            processes.append((database, processor, Event(), f"process-{index}"))
        threads = [
            threading.Thread(
                target=process_until_stopped,
                args=(processor,),
                kwargs={
                    "concurrency": 4,
                    "response_lanes": 2,
                    "owner": owner,
                    "lease_seconds": 60,
                    "stop_requested": stop,
                    "idle_seconds": 0.05,
                    "claims_ready": lambda: True,
                    "maintain": lambda _turns: None,
                    "on_result": lambda _result: None,
                },
                daemon=True,
            )
            for _database, processor, stop, owner in processes
        ]
        for thread in threads:
            thread.start()
        try:
            statuses = _wait_for_statuses(connection_info, job_ids)
            assert statuses.count("complete") == len(job_ids), statuses
        finally:
            for _database, _processor_, stop, _owner in processes:
                stop.set()
            for thread in threads:
                thread.join(30)
        for database, *_ in processes:
            database.close()
        assert not any(thread.is_alive() for thread in threads)
        attempts = _attempts(connection_info, job_ids)
        completed = [job_id for job_id, state, _ in attempts if state == "complete"]
        assert sorted(completed) == sorted(job_ids)
        with psycopg.connect(connection_info) as connection:
            owners = {
                text(row[0]).split(".")[0]
                for row in connection.execute(
                    "SELECT lease_owner FROM python_processing_attempts WHERE job_id = ANY(%s)",
                    (job_ids,),
                )
            }
            leased = connection.execute(
                "SELECT count(*) FROM python_processing_jobs WHERE status = 'leased'"
            ).fetchone()[0]
        assert owners == {"process-1", "process-2"}
        assert leased == 0
        assert most_builds == 1


def test_queue_health_reports_each_kind_of_work_apart(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _queue_builds(connection_info, 2)
        _queue_profiles(connection_info, archive_server, TAGS[:3])
        database = Database(connection_info)
        try:
            database.claim_job(owner="health", work_types=RESPONSE_WORK_TYPES)
            health = database.queue_health()
        finally:
            database.close()
    assert (health["pending"], health["leased"], health["overdue"]) == (4, 1, 4)
    assert {kind: values["overdue"] for kind, values in health["kinds"].items()} == {
        "responses": 2, "builds": 2
    }
    assert health["kinds"]["responses"]["oldest_due_seconds"] >= 0


def test_each_process_plans_its_own_share_of_the_newest_jobs(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        job_ids = _queue_profiles(connection_info, archive_server, TAGS[:6])
        database = Database(connection_info)
        try:
            shares = []
            for share in (1, 2):
                processor = ObservationProcessor(database, archive=None)
                processor.plan_share = (share, 2)
                first = processor._next_planned_job()
                shares.append({first, *processor._plan})
        finally:
            database.close()
    assert shares[0].isdisjoint(shares[1])
    assert shares[0] | shares[1] == set(job_ids)


def test_a_batched_claim_that_waited_half_its_lease_is_renewed_before_it_starts(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _queue_profiles(connection_info, archive_server, TAGS[:3])
        database = Database(connection_info)
        try:
            processor = ObservationProcessor(database, archive=None, claim_batch=3)
            first = processor._claim_next(
                owner="slow", lease_seconds=2, work_types=RESPONSE_WORK_TYPES
            )
            assert first is not None
            time.sleep(1.1)  # the next waited more than half its 2-second lease
            second = processor._claim_next(
                owner="slow", lease_seconds=2, work_types=RESPONSE_WORK_TYPES
            )
            assert second is not None
            with psycopg.connect(connection_info) as connection:
                remaining = connection.execute(
                    "SELECT extract(epoch FROM lease_expires_at - clock_timestamp())"
                    " FROM python_processing_jobs WHERE id = %s",
                    (second.job_id,),
                ).fetchone()[0]
            assert remaining > 1.5  # without renewal, under 0.9 seconds were left
            assert processor.release_batched_claims() == 1
        finally:
            database.close()
