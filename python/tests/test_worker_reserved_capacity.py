from __future__ import annotations

import threading
import time
from threading import Event, Semaphore

import pytest

import clashlens.worker as worker_module
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
    """In-memory queue whose builds run until ``release_builds`` is set.

    Each lane takes the oldest job of a kind it may claim, as the database
    claim does. It records how many builds and derived jobs run at once.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: list[tuple[int, str]] = []
        self.next_id = 1
        self.release_builds = Event()
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


def test_heavy_maintenance_waits_for_a_derived_turn(monkeypatch) -> None:
    monkeypatch.setattr(worker_module, "HEAVY_MAINTENANCE_WAIT_SECONDS", 0.01)
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
    kinds = [lane_work_types(lane, concurrency) for lane in range(1, concurrency + 1)]
    response_lanes = [
        kind for kind in kinds if kind == (RESPONSE, "replay_observation")
    ]
    build_lanes = [kind for kind in kinds if kind is not None and BUILD in kind]

    assert len(response_lanes) == responses
    assert len(kinds) - len(response_lanes) == derived
    assert len(build_lanes) == 1
    assert lane_work_types(1, 1) is None
