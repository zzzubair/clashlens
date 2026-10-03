from __future__ import annotations

import threading
import time
from threading import Event, Semaphore

import pytest
from test_worker_lifecycle import _worker_namespace

import clashlens.worker as worker_module
from clashlens import cli
from clashlens.worker import (
    ProcessResult,
    TimedMaintenance,
    lane_work_types,
    process_until_stopped,
)

RESPONSE = "process_observation"
DAILY = "reconcile_ranked_day"
BUILD = "build_army_analytics"


class HeldQueue:
    """In-memory queue whose builds run until ``release_builds`` is set, and
    daily jobs until ``release_daily`` is set.

    Each lane takes the oldest job of a kind it may claim, as the database
    claim does. It records how many builds and derived jobs run at once.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: list[tuple[int, str]] = []
        self.next_id = 1
        self.release_builds = Event()
        self.release_daily = Event()
        self.release_daily.set()
        self.builds_running = 0
        self.most_builds = 0
        self.derived_running = 0
        self.most_derived = 0
        self.done: dict[str, int] = {RESPONSE: 0, DAILY: 0, BUILD: 0}

    def add(self, work_type: str, count: int) -> None:
        with self.lock:
            for _ in range(count):
                self.jobs.append((self.next_id, work_type))
                self.next_id += 1

    def process_once(
        self, *, owner: str, lease_seconds: int, work_types: object = None
    ) -> ProcessResult | None:
        with self.lock:
            job = next(
                (
                    job
                    for job in self.jobs
                    if work_types is None or job[1] in work_types  # type: ignore[operator]
                ),
                None,
            )
            if job is None:
                return None
            self.jobs.remove(job)
            job_id, work_type = job
            if work_type != RESPONSE:
                self.derived_running += 1
                self.most_derived = max(self.most_derived, self.derived_running)
            if work_type == BUILD:
                self.builds_running += 1
                self.most_builds = max(self.most_builds, self.builds_running)
        if work_type == BUILD:
            assert self.release_builds.wait(10), "test release gate was not opened"
        if work_type == DAILY:
            assert self.release_daily.wait(10), "test release gate was not opened"
        with self.lock:
            self.done[work_type] += 1
            if work_type != RESPONSE:
                self.derived_running -= 1
            if work_type == BUILD:
                self.builds_running -= 1
        return ProcessResult(job_id, "processed")


class HeldSweep:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def run_when_due(self) -> None:
        self.started.set()
        assert self.release.wait(10), "test release gate was not opened"


class QueueOnlyDatabase:
    def __init__(self) -> None:
        self.queue_maintenance_runs = 0

    def maintain_queue(self, *, max_jobs: int) -> int:
        self.queue_maintenance_runs += 1
        return 0


def _held_maintenance() -> tuple[TimedMaintenance, HeldSweep, QueueOnlyDatabase]:
    database = QueueOnlyDatabase()
    maintenance = TimedMaintenance(database, worker_module.StageMetrics())  # type: ignore[arg-type]
    sweep = HeldSweep()
    maintenance.late_battles = sweep  # type: ignore[assignment]
    return maintenance, sweep, database


def _start(
    queue: HeldQueue, maintenance: TimedMaintenance, stop: Event
) -> threading.Thread:
    thread = threading.Thread(
        target=process_until_stopped,
        args=(queue,),
        kwargs={
            "concurrency": 12,
            "owner": "reserved",
            "lease_seconds": 30,
            "stop_requested": stop,
            "idle_seconds": 0.01,
            "claims_ready": lambda: True,
            "maintain": maintenance.run_due,
            "on_result": lambda _result: None,
        },
        daemon=True,
    )
    thread.start()
    return thread


def _wait_for(condition, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_responses_keep_processing_while_builds_and_the_sweep_run() -> None:
    # On 2026-10-03 a 42-minute build held up player responses. With every
    # slot able to claim a build, twelve builds left no slot for responses.
    queue = HeldQueue()
    maintenance, sweep, _database = _held_maintenance()
    stop = Event()
    queue.add(BUILD, 20)
    thread = _start(queue, maintenance, stop)
    try:
        assert sweep.started.wait(5)
        assert _wait_for(lambda: queue.builds_running >= 1)
        time.sleep(0.1)  # every lane has looked for work at least once
        queue.add(RESPONSE, 100)
        assert _wait_for(lambda: queue.done[RESPONSE] == 100), (
            f"{queue.done[RESPONSE]} of 100 responses finished while "
            f"{queue.builds_running} builds ran"
        )
        assert not queue.release_builds.is_set() and not sweep.release.is_set()
        assert queue.most_builds == 1
    finally:
        queue.release_builds.set()
        sweep.release.set()
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


def test_daily_results_keep_moving_while_a_build_runs() -> None:
    queue = HeldQueue()
    maintenance, sweep, _database = _held_maintenance()
    sweep.release.set()
    stop = Event()
    queue.add(BUILD, 5)
    thread = _start(queue, maintenance, stop)
    try:
        assert _wait_for(lambda: queue.builds_running == 1)
        queue.add(DAILY, 200)
        queue.add(RESPONSE, 200)
        assert _wait_for(lambda: queue.done[DAILY] == 200)
        assert _wait_for(lambda: queue.done[RESPONSE] == 200)
        assert queue.builds_running == 1 and queue.most_builds == 1
        assert queue.most_derived <= 4
        queue.release_builds.set()
        assert _wait_for(lambda: queue.done[BUILD] == 5)
    finally:
        queue.release_builds.set()
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


def test_the_build_slot_takes_a_build_before_older_daily_jobs() -> None:
    queue = HeldQueue()
    maintenance, sweep, _database = _held_maintenance()
    sweep.release.set()
    queue.release_daily.clear()
    stop = Event()
    queue.add(DAILY, 10)
    queue.add(BUILD, 1)
    thread = _start(queue, maintenance, stop)
    try:
        assert _wait_for(lambda: queue.builds_running == 1), (
            f"{queue.derived_running} daily jobs held every derived slot"
        )
        assert queue.done[DAILY] == 0
    finally:
        queue.release_builds.set()
        queue.release_daily.set()
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


def test_heavy_maintenance_skips_its_tick_without_a_derived_turn() -> None:
    maintenance, sweep, database = _held_maintenance()
    sweep.release.set()
    turns = Semaphore(0)  # every derived turn is taken

    maintenance.run_due(turns)

    assert not sweep.started.is_set()
    assert maintenance.next_reevaluation_at == float("-inf")  # still due
    assert database.queue_maintenance_runs == 1

    turns.release()
    maintenance.next_queue_maintenance_at = float("-inf")
    maintenance.run_due(turns)

    assert sweep.started.is_set()
    assert turns.acquire(blocking=False), "the turn was not given back"


@pytest.mark.parametrize(
    ("concurrency", "responses", "derived"), [(12, 8, 4), (4, 3, 1), (2, 1, 1)]
)
def test_continuous_lanes_reserve_two_thirds_for_responses(
    concurrency: int, responses: int, derived: int
) -> None:
    orders = [lane_work_types(lane, concurrency) for lane in range(1, concurrency + 1)]
    response_lanes = [
        order for order in orders if order == ((RESPONSE, "replay_observation"),)
    ]
    build_lanes = [
        order
        for order in orders
        if order is not None and any(BUILD in kinds for kinds in order)
    ]

    assert len(response_lanes) == responses
    assert len(orders) - len(response_lanes) == derived
    assert len(build_lanes) == 1
    assert BUILD in build_lanes[0][0] and DAILY not in build_lanes[0][0]
    assert DAILY in build_lanes[0][1]
    assert lane_work_types(1, 1) is None


def test_derived_work_cannot_hold_every_database_connection(monkeypatch) -> None:
    # Twelve slots on four connections: four long derived jobs could once take
    # all four, leaving every response slot without a connection.
    release_derived = Event()
    derived_holding = Event()
    responses_done = Event()
    lock = threading.Lock()
    responses = 0
    pools: list[int] = []
    stop: list[Event] = []

    class ConnectionPoolDatabase:
        def __init__(self, _url: str, *, max_size: int, **_kwargs: object) -> None:
            self.connections = threading.BoundedSemaphore(max_size)
            pools.append(max_size)

        def maintain_queue(self, *, max_jobs: int) -> int:
            return 0

        def close(self) -> None:
            return

    class FakeArchive:
        @staticmethod
        def check_ready() -> bool:
            return True

    class ConnectionHoldingProcessor:
        def __init__(self, database: ConnectionPoolDatabase, *_: object) -> None:
            self.database = database

        def process_once(
            self, *, owner: str, lease_seconds: int, work_types: tuple[str, ...]
        ) -> ProcessResult | None:
            nonlocal responses
            if RESPONSE not in work_types:
                with self.database.connections:
                    derived_holding.set()
                    assert release_derived.wait(10), "test gate was not opened"
                return None
            if not self.database.connections.acquire(timeout=2):
                raise TimeoutError("no free connection")
            try:
                with lock:
                    responses += 1
                    if responses == 100:
                        responses_done.set()
                    return ProcessResult(responses, "processed")
            finally:
                self.database.connections.release()

    monkeypatch.setattr(cli, "Database", ConnectionPoolDatabase)
    monkeypatch.setattr(cli, "_archive", lambda _arguments, **_kwargs: FakeArchive())
    monkeypatch.setattr(cli, "ObservationProcessor", ConnectionHoldingProcessor)
    monkeypatch.setattr(cli, "_install_shutdown_handlers", stop.append)
    arguments = _worker_namespace(
        run_forever=True, concurrency=12, database_pool_size=4
    )
    worker_thread = threading.Thread(
        target=cli._run_worker, args=(arguments,), daemon=True
    )
    worker_thread.start()
    try:
        assert derived_holding.wait(5)
        time.sleep(0.1)  # every derived slot has asked for a connection
        assert responses_done.wait(5), f"only {responses} responses finished"
        assert not release_derived.is_set()
    finally:
        release_derived.set()
        stop[0].set()
        worker_thread.join(10)
    assert not worker_thread.is_alive()
    assert pools == [3, 2, 1]



def test_continuous_workers_refuse_a_pool_without_a_response_connection(
    monkeypatch,
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(cli, "Database", lambda *args, **kwargs: opened.append(args))
    arguments = _worker_namespace(
        run_forever=True, concurrency=12, database_pool_size=1
    )

    with pytest.raises(ValueError, match="at least 2"):
        cli._run_worker(arguments)
    assert opened == []
