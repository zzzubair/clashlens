from __future__ import annotations

import threading
import time
from threading import Event

import pytest

from clashlens.worker import (
    MAX_CONCURRENCY,
    ProcessResult,
    lane_owner,
    process_concurrently,
    process_until_stopped,
)


class RecordingProcessor:
    """Fake observation processor that records every claim call."""

    def __init__(self, available_jobs: int | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self.available_jobs = available_jobs

    def process_once(self, *, owner: str, lease_seconds: int) -> ProcessResult | None:
        with self._lock:
            if self.available_jobs is not None:
                if self.available_jobs <= 0:
                    return None
                self.available_jobs -= 1
            call_index = len(self.calls) + 1
            self.calls.append((owner, lease_seconds))
        return ProcessResult(call_index, "processed")


def test_lane_owner_is_stable_unique_and_derived_from_configured_owner() -> None:
    first = [lane_owner("production-python-1", lane) for lane in (1, 2, 3)]
    second = [lane_owner("production-python-1", lane) for lane in (1, 2, 3)]

    assert first == second
    assert len(set(first)) == 3
    assert first == [
        "production-python-1.lane-1",
        "production-python-1.lane-2",
        "production-python-1.lane-3",
    ]


def test_lane_owner_rejects_missing_owner_and_invalid_lane() -> None:
    with pytest.raises(ValueError, match="lease owner is required"):
        lane_owner("", 1)
    with pytest.raises(ValueError, match="lane index"):
        lane_owner("owner", 0)


def test_at_most_concurrency_jobs_run_at_once() -> None:
    in_flight = 0
    peak = 0
    state_lock = threading.Lock()
    saturated = Event()
    proceed = Event()

    class GatedProcessor:
        def process_once(self, **_kwargs: object) -> ProcessResult:
            nonlocal in_flight, peak
            with state_lock:
                in_flight += 1
                peak = max(peak, in_flight)
                if in_flight == 3:
                    saturated.set()
            assert proceed.wait(10), "test gate was not released"
            with state_lock:
                in_flight -= 1
            return ProcessResult(1, "processed")

    captured: list[list[ProcessResult]] = []
    thread = threading.Thread(
        target=lambda: captured.append(
            process_concurrently(
                GatedProcessor(),
                concurrency=3,
                owner="bounded-lanes",
                max_jobs=9,
            )
        ),
        daemon=True,
    )
    thread.start()

    assert saturated.wait(10)
    time.sleep(0.05)
    assert peak == 3
    proceed.set()
    thread.join(10)
    assert not thread.is_alive(), "concurrent worker did not terminate in time"
    assert len(captured[0]) == 9


def test_max_jobs_is_a_total_bound_across_all_lanes() -> None:
    processor = RecordingProcessor(available_jobs=100)

    results = process_concurrently(
        processor,
        concurrency=8,
        owner="bounded-total",
        max_jobs=3,
    )

    assert len(results) == 3
    assert len(processor.calls) == 3


def test_zero_max_jobs_returns_without_any_claim() -> None:
    processor = RecordingProcessor(available_jobs=10)

    results = process_concurrently(
        processor,
        concurrency=4,
        owner="no-budget",
        max_jobs=0,
    )

    assert results == []
    assert processor.calls == []


def test_empty_queue_drains_lanes_without_runaway_claims() -> None:
    processor = RecordingProcessor(available_jobs=1)

    results = process_concurrently(
        processor,
        concurrency=4,
        owner="draining-queue",
        max_jobs=10,
    )

    assert len(results) == 1
    assert results[0].outcome == "processed"
    assert len(processor.calls) == 1
    assert processor.calls[0][0].startswith("draining-queue.lane-")
    assert processor.calls[0][1] == 30


def test_concurrency_rejects_out_of_bounds_values() -> None:
    processor = RecordingProcessor()
    with pytest.raises(ValueError, match="concurrency"):
        process_concurrently(processor, concurrency=0, owner="o", max_jobs=1)
    with pytest.raises(ValueError, match="concurrency"):
        process_concurrently(
            processor, concurrency=MAX_CONCURRENCY + 1, owner="o", max_jobs=1
        )
    with pytest.raises(ValueError, match="lease owner is required"):
        process_concurrently(processor, concurrency=1, owner="", max_jobs=1)


def test_each_lane_uses_a_stable_unique_lease_owner() -> None:
    barrier = threading.Barrier(3)
    calls: list[tuple[str, int]] = []
    calls_lock = threading.Lock()

    class BarrierProcessor:
        def process_once(self, *, owner: str, lease_seconds: int) -> ProcessResult:
            with calls_lock:
                calls.append((owner, lease_seconds))
                call_index = len(calls)
            barrier.wait(timeout=10)
            return ProcessResult(call_index, "processed")

    first_run = process_concurrently(
        BarrierProcessor(),
        concurrency=3,
        owner="lane-owner",
        max_jobs=6,
    )
    first_owners = {owner for owner, _lease in calls}
    calls.clear()
    process_concurrently(
        BarrierProcessor(),
        concurrency=3,
        owner="lane-owner",
        max_jobs=6,
    )
    second_owners = {owner for owner, _lease in calls}

    assert len(first_run) == 6
    assert first_owners == {
        "lane-owner.lane-1",
        "lane-owner.lane-2",
        "lane-owner.lane-3",
    }
    assert second_owners == first_owners
    assert all(lease_seconds == 30 for _owner, lease_seconds in calls)


def test_stop_before_run_prevents_any_claim() -> None:
    processor = RecordingProcessor(available_jobs=10)
    stop_requested = Event()
    stop_requested.set()

    results = process_concurrently(
        processor,
        concurrency=4,
        owner="stopped-worker",
        max_jobs=10,
        stop_requested=stop_requested,
    )

    assert results == []
    assert processor.calls == []


def test_stop_waits_boundedly_for_in_flight_jobs_and_claims_nothing_new() -> None:
    both_in_flight = Event()
    release = Event()
    calls = 0
    calls_lock = threading.Lock()
    stop_requested = Event()
    captured: list[list[ProcessResult]] = []

    class BlockingProcessor:
        def process_once(self, **_kwargs: object) -> ProcessResult:
            nonlocal calls
            with calls_lock:
                calls += 1
                call_index = calls
            if call_index == 2:
                both_in_flight.set()
            assert release.wait(10), "test release gate was not opened"
            return ProcessResult(call_index, "processed")

    thread = threading.Thread(
        target=lambda: captured.append(
            process_concurrently(
                BlockingProcessor(),
                concurrency=2,
                owner="draining-worker",
                max_jobs=10,
                stop_requested=stop_requested,
            )
        ),
        daemon=True,
    )
    thread.start()
    assert both_in_flight.wait(10)
    stop_requested.set()
    time.sleep(0.1)
    assert calls == 2, "no new claims may start after stop is requested"
    release.set()
    thread.join(10)
    assert not thread.is_alive(), "worker did not finish in-flight jobs in time"
    assert len(captured[0]) == 2
    assert all(result.outcome == "processed" for result in captured[0])


def test_lane_exception_is_isolated_and_other_lanes_finish() -> None:
    barrier = threading.Barrier(3)
    started: list[str] = []
    started_lock = threading.Lock()

    class FlakyProcessor:
        def process_once(self, *, owner: str, lease_seconds: int) -> ProcessResult:
            del lease_seconds
            barrier.wait(timeout=10)
            if owner.endswith("lane-1"):
                raise RuntimeError("lane-1 exploded with a secret detail")
            with started_lock:
                started.append(owner)
                started_count = len(started)
            time.sleep(0.02)
            return ProcessResult(started_count, "processed")

    with pytest.raises(RuntimeError, match="worker lane failed"):
        process_concurrently(
            FlakyProcessor(),
            concurrency=3,
            owner="isolated",
            max_jobs=5,
        )

    assert len(started) == 2, "other in-flight lanes must complete their jobs"
    assert all(owner.endswith((".lane-2", ".lane-3")) for owner in started)


def test_lane_exception_never_exposes_job_details_or_credentials() -> None:
    secret = "archive-secret-material-7f3a"

    class LeakingProcessor:
        def process_once(self, **_kwargs: object) -> ProcessResult:
            raise RuntimeError(f"internal failure referencing {secret}")

    with pytest.raises(RuntimeError) as excinfo:
        process_concurrently(
            LeakingProcessor(),
            concurrency=2,
            owner="secret-guard",
            max_jobs=4,
        )

    assert secret not in str(excinfo.value)
    assert "archive-secret-material" not in str(excinfo.value)


def _run_until_stopped(processor: object, **overrides: object) -> threading.Thread:
    arguments: dict[str, object] = {
        "concurrency": 3,
        "owner": "steady-lane",
        "lease_seconds": 30,
        "idle_seconds": 0.01,
        "claims_ready": lambda: True,
        "maintain": lambda: None,
        "on_result": lambda _result: None,
    }
    arguments.update(overrides)
    thread = threading.Thread(
        target=process_until_stopped, args=(processor,), kwargs=arguments, daemon=True
    )
    thread.start()
    return thread


def test_back_to_back_long_jobs_never_leave_other_lanes_idle() -> None:
    release_long_jobs = Event()
    both_long_jobs_running = Event()
    fast_jobs_done = Event()
    stop = Event()
    lock = threading.Lock()
    queue = ["long"]
    running_long = 0
    reported: list[int] = []

    class LongJobProcessor:
        def process_once(self, **_kwargs: object) -> ProcessResult | None:
            nonlocal running_long
            with lock:
                if not queue:
                    return None
                job = queue.pop(0)
                if job == "long":
                    running_long += 1
                    if running_long == 2:
                        both_long_jobs_running.set()
            if job == "long":
                assert release_long_jobs.wait(10), "test release gate was not opened"
                return ProcessResult(0, "processed")
            return ProcessResult(int(job), "processed")

    def on_result(result: ProcessResult) -> None:
        reported.append(result.job_id)
        if set(range(1, 11)) <= set(reported):
            fast_jobs_done.set()

    thread = _run_until_stopped(
        LongJobProcessor(), stop_requested=stop, on_result=on_result
    )
    try:
        time.sleep(0.1)  # one lane holds the long job; the others find no work
        with lock:
            queue.append("long")
        assert both_long_jobs_running.wait(5)
        with lock:
            queue.extend(str(job_id) for job_id in range(1, 11))
        assert fast_jobs_done.wait(5), "the free lane stopped claiming"
    finally:
        release_long_jobs.set()
        stop.set()
        thread.join(10)
    assert not thread.is_alive()
    assert sorted(reported) == [0, 0, *range(1, 11)]


def test_maintenance_failure_is_retried_without_stopping_lanes(capsys) -> None:
    stop = Event()
    maintained_again = Event()
    maintenance_calls = 0

    def maintain() -> None:
        nonlocal maintenance_calls
        maintenance_calls += 1
        if maintenance_calls == 1:
            raise RuntimeError("postgresql://secret@db/clashlens unavailable")
        maintained_again.set()

    processor = RecordingProcessor(available_jobs=5)
    thread = _run_until_stopped(processor, stop_requested=stop, maintain=maintain)
    try:
        assert maintained_again.wait(5)
        deadline = time.monotonic() + 5
        while len(processor.calls) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(processor.calls) == 5
    output = capsys.readouterr().out
    assert '"error": "RuntimeError"' in output
    assert "secret" not in output


def test_lanes_do_not_claim_while_the_spool_is_not_ready() -> None:
    stop = Event()
    ready_checks = Event()
    maintained: list[bool] = []

    def claims_ready() -> bool:
        ready_checks.set()
        return False

    processor = RecordingProcessor(available_jobs=5)
    thread = _run_until_stopped(
        processor,
        stop_requested=stop,
        claims_ready=claims_ready,
        maintain=lambda: maintained.append(True),
    )
    try:
        assert ready_checks.wait(5)
        time.sleep(0.1)
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()
    assert processor.calls == []
    assert maintained == []
