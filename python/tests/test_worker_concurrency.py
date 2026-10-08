from __future__ import annotations

import sys
import threading
import time
from threading import Event
from types import SimpleNamespace

import pytest
from psycopg_pool import PoolTimeout

from clashlens import cli, reconciliation_db
from clashlens.db import DOMAIN_RULE_VERSION, PROCESSING_VERSION
from clashlens.worker import (
    MAX_CONCURRENCY,
    ObservationProcessor,
    ProcessResult,
    lane_owner,
    process_concurrently,
    process_until_stopped,
    run_processes,
    worker_process_commands,
)


class RecordingProcessor:
    """Fake observation processor that records every claim call."""

    def __init__(self, available_jobs: int | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self.available_jobs = available_jobs

    def process_once(
        self, *, owner: str, lease_seconds: int, work_types: object = None
    ) -> ProcessResult | None:
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


def test_pool_timeout_in_one_lane_retries_while_other_lanes_keep_working() -> None:
    stop = Event()
    lane_one_recovered = Event()
    lock = threading.Lock()
    lane_one_timeouts = 0
    worked_after_timeout: set[str] = set()

    class PoolBusyProcessor:
        def process_once(self, *, owner: str, **_kwargs: object) -> ProcessResult:
            nonlocal lane_one_timeouts
            lane = owner.rsplit(".", 1)[1]
            with lock:
                if lane == "lane-1" and lane_one_timeouts < 2:
                    lane_one_timeouts += 1
                    raise PoolTimeout("couldn't get a connection after 30.00 sec")
                if lane_one_timeouts:
                    worked_after_timeout.add(lane)
            if lane == "lane-1":
                lane_one_recovered.set()
            time.sleep(0.005)
            return ProcessResult(1, "processed")

    thread = _run_until_stopped(PoolBusyProcessor(), stop_requested=stop)
    try:
        assert lane_one_recovered.wait(5), "the lane that waited never claimed again"
        deadline = time.monotonic() + 5
        while len(worked_after_timeout) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(10)
    assert not thread.is_alive()
    assert worked_after_timeout == {"lane-1", "lane-2", "lane-3"}


@pytest.mark.parametrize("refund_waits", [False, True])
def test_pool_timeout_during_a_job_retries_that_job(refund_waits, monkeypatch) -> None:
    claims = [
        SimpleNamespace(
            job_id=job_id,
            work_type="reconcile_ranked_day",
            processing_version=PROCESSING_VERSION,
            domain_rule_version=DOMAIN_RULE_VERSION,
            attempt_count=1,
            max_attempts=3,
        )
        for job_id in (17, 18)
    ]
    refunded: list[int] = []

    class Database:
        def claim_job(self, **_kwargs: object) -> object:
            return claims.pop(0) if claims else None

        def renew_claim(self, _claim: object, **_kwargs: object) -> None:
            pass

        def refund_claim_attempt(self, claim: SimpleNamespace) -> None:
            if refund_waits:
                raise PoolTimeout("couldn't get a connection after 30.00 sec")
            refunded.append(claim.job_id)

    def complete_reconciliation(_database: object, claim: SimpleNamespace) -> None:
        if claim.job_id == 17:
            raise PoolTimeout("couldn't get a connection after 30.00 sec")

    monkeypatch.setattr(
        reconciliation_db, "complete_reconciliation", complete_reconciliation
    )
    results = process_concurrently(
        ObservationProcessor(Database(), archive=object()),
        concurrency=1,
        owner="pool-busy",
        max_jobs=2,
    )

    assert results == [
        ProcessResult(17, "retrying", "database_pool_timeout"),
        ProcessResult(18, "processed"),
    ]
    # Without a connection for the refund, the lease runs out and queue
    # maintenance retries the job instead.
    assert refunded == ([] if refund_waits else [17])


def _run_until_stopped(processor: object, **overrides: object) -> threading.Thread:
    arguments: dict[str, object] = {
        "concurrency": 3,
        "owner": "steady-lane",
        "lease_seconds": 30,
        "idle_seconds": 0.01,
        "claims_ready": lambda: True,
        "maintain": lambda _turns: None,
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

    def maintain(_turns: object) -> None:
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
        maintain=lambda _turns: maintained.append(True),
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


def test_lanes_stop_claiming_when_the_spool_fails_during_slow_maintenance() -> None:
    stop = Event()
    spool_ready = Event()
    spool_ready.set()
    maintenance_started = Event()
    release_maintenance = Event()
    lock = threading.Lock()
    false_checks: dict[int, int] = {}
    all_lanes_saw_failure = Event()
    release_lanes = Event()
    all_lanes_rechecked = Event()

    def maintain(_turns: object) -> None:
        maintenance_started.set()
        assert release_maintenance.wait(10), "test release gate was not opened"

    def claims_ready() -> bool:
        if spool_ready.is_set():
            return True
        # Each lane's first failed check waits, so the count below is taken
        # after any claim already under way and before any lane moves on.
        with lock:
            lane = threading.get_ident()
            false_checks[lane] = false_checks.get(lane, 0) + 1
            first_check = false_checks[lane] == 1
            if len(false_checks) == 3:
                all_lanes_saw_failure.set()
                if min(false_checks.values()) >= 2:
                    all_lanes_rechecked.set()
        if first_check:
            assert release_lanes.wait(10), "test release gate was not opened"
        return False

    processor = RecordingProcessor()
    thread = _run_until_stopped(
        processor,
        stop_requested=stop,
        claims_ready=claims_ready,
        maintain=maintain,
    )
    try:
        assert maintenance_started.wait(5)
        spool_ready.clear()
        assert all_lanes_saw_failure.wait(5)
        claims_when_spool_failed = len(processor.calls)
        release_lanes.set()
        assert all_lanes_rechecked.wait(5)
        assert len(processor.calls) == claims_when_spool_failed
        spool_ready.set()
        deadline = time.monotonic() + 5
        while (
            len(processor.calls) == claims_when_spool_failed
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert len(processor.calls) > claims_when_spool_failed
        assert not release_maintenance.is_set()
    finally:
        release_lanes.set()
        release_maintenance.set()
        stop.set()
        thread.join(10)
    assert not thread.is_alive()


def test_no_job_or_maintenance_starts_after_stop_during_a_slow_ready_check() -> None:
    stop = Event()
    release_checks = Event()
    lock = threading.Lock()
    waiting_checks = 0
    all_checks_waiting = Event()
    maintained: list[bool] = []

    def claims_ready() -> bool:
        nonlocal waiting_checks
        with lock:
            waiting_checks += 1
            if waiting_checks == 4:
                all_checks_waiting.set()
        assert release_checks.wait(10), "test release gate was not opened"
        return True

    processor = RecordingProcessor()
    thread = _run_until_stopped(
        processor,
        stop_requested=stop,
        claims_ready=claims_ready,
        maintain=lambda _turns: maintained.append(True),
    )
    try:
        assert all_checks_waiting.wait(5), "three lanes and the timer must check"
        stop.set()
    finally:
        release_checks.set()
        stop.set()
        thread.join(10)
    assert not thread.is_alive()
    assert processor.calls == []
    assert maintained == []


WORKER_ARGV = ["worker", "--database-url", "postgresql://prototype@postgres/db",
               "--owner", "production-python-1", "--run-forever", "--processes", "2",
               "--concurrency", "16", "--response-lanes", "12"]


def test_each_worker_process_runs_the_same_worker_under_its_own_owner() -> None:
    arguments = cli.build_parser().parse_args(WORKER_ARGV)
    arguments.argv = WORKER_ARGV
    commands = worker_process_commands(arguments)
    assert [command[:3] for command in commands] == [[sys.executable, "-m", "clashlens.cli"]] * 2
    parsed = [cli.build_parser().parse_args(command[3:]) for command in commands]
    assert [(each.processes, each.process_index, each.owner) for each in parsed] == [
        (2, 1, "production-python-1.process-1"),
        (2, 2, "production-python-1.process-2"),
    ]
    assert {(each.concurrency, each.response_lanes, each.run_forever) for each in parsed} == {
        (16, 12, True)
    }


def test_worker_processes_over_the_connection_budget_never_start(capsys) -> None:
    # Two processes of 32 connections each would leave the collector short.
    assert cli.main([*WORKER_ARGV, "--database-pool-size", "32"]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert cli.main([*WORKER_ARGV[:-2], "--response-lanes", "16"]) == 1


def test_when_one_worker_process_exits_the_others_stop_and_the_worker_fails() -> None:
    commands = [[sys.executable, "-c", "import time; time.sleep(60)"],
                [sys.executable, "-c", "raise SystemExit(3)"]]
    started = time.monotonic()
    assert run_processes(commands, Event()) == 1
    assert time.monotonic() - started < 10


def test_a_requested_stop_reaches_every_worker_process() -> None:
    graceful = ("import signal, sys, time;"
                " signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(60)")
    stop = Event()
    threading.Timer(1.0, stop.set).start()
    started = time.monotonic()
    assert run_processes([[sys.executable, "-c", graceful]] * 2, stop) == 0
    assert time.monotonic() - started < 10
