"""The early warning before a health-check kill or a stalled Reset, against a
local HTTP server, never Discord. On 7 Oct 2026 Podman killed the collector 13
times and the worker 4 times with no warning at all."""

from datetime import UTC, datetime

import pytest
import test_alerts

from clashlens import alerts

runtime = test_alerts.runtime

PREFIX = "clashlens_collector_"
OVERDUE = f"{PREFIX}oldest_pending_processing_age_seconds"


@pytest.fixture
def rt(runtime, monkeypatch):
    for name, value in runtime.warning_limits.items():
        monkeypatch.setattr(alerts, name, value)
    return runtime


def warnings(rt) -> list[str]:
    return [p["content"] for p in rt.posts if "Early warning" in p["content"]]


def at(rt, hour, minute=0, second=0) -> None:
    rt.now = datetime(2026, 10, 8, hour, minute, second, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(("streak", "warned"), [(1, False), (2, True)])
def test_two_failed_health_checks_in_a_row_warn(rt, streak, warned) -> None:
    rt.health_streaks["clashlens-python-worker"] = streak
    assert rt.run() == 0
    assert len(warnings(rt)) == int(warned)
    if warned:
        assert (
            "clashlens-python-worker failed its last 2 health checks" in warnings(rt)[0]
        )
        rt.health_streaks["clashlens-python-worker"] = 0
        assert rt.run() == 0
        assert "recovered" in rt.posts[-1]["content"]


@pytest.mark.parametrize(
    ("hour", "age", "warned"),
    [
        (12, 599, False),
        (12, 600, True),
        # A normal Reset leaves work overdue up to 35 minutes (6 Oct 2026).
        (5, 2699, False),
        (6, 2700, True),
        (7, 600, True),
    ],
)
def test_overdue_work_warns_at_10_minutes_or_45_after_a_reset(
    rt, hour, age, warned
) -> None:
    at(rt, hour, 30)
    rt.metrics[OVERDUE] = age
    assert rt.run() == 0
    assert len(warnings(rt)) == int(warned)
    if warned:
        assert f"oldest overdue job has waited {age // 60} minutes" in warnings(rt)[0]


def saved_sample(rt, hour, minute, saved) -> None:
    at(rt, hour, minute, 10)
    rt.metrics[f"{PREFIX}metrics_sample_timestamp_seconds"] = rt.now - 5
    rt.metrics[f"{PREFIX}responses_saved_last_minute"] = saved


@pytest.mark.parametrize(("saved", "warned"), [(99, True), (100, False)])
def test_reset_hour_saving_under_100_responses_a_minute_warns(
    rt, saved, warned
) -> None:
    saved_sample(rt, 5, 21, saved)
    assert rt.run() == 0
    assert len(warnings(rt)) == int(warned)
    if warned:
        assert (
            "only 99 responses a minute were saved in the Reset hour" in warnings(rt)[0]
        )


def test_slow_saving_outside_the_reset_hour_or_across_its_start_does_not_warn(
    rt,
) -> None:
    # The minute before the Reset is a deliberate pause.
    saved_sample(rt, 4, 59, 0)
    assert rt.run() == 0
    saved_sample(rt, 5, 0, 0)
    assert rt.run() == 0
    saved_sample(rt, 6, 30, 0)
    assert rt.run() == 0
    assert warnings(rt) == []


def test_an_unreadable_health_streak_keeps_an_open_warning_open(rt) -> None:
    rt.health_streaks["clashlens-collector"] = 3
    assert rt.run() == 0
    assert len(warnings(rt)) == 1
    # A container mid-restart cannot be inspected; that is not a recovery.
    rt.health_streaks["clashlens-collector"] = "no such container"
    rt.now += 60
    assert rt.run() == 0
    assert len(rt.posts) == 1


def test_a_health_warning_is_sent_before_the_slow_checks_run(rt, monkeypatch) -> None:
    # Podman kills at 6 failures, about three minutes; the backup and database
    # checks after it can take 25 seconds each.
    rt.health_streaks["clashlens-collector"] = 4
    run_command = alerts.command
    warned_before = []

    def command(args, timeout=15):
        if "{{.State.Health.FailingStreak}}" not in args:
            warned_before.append(bool(warnings(rt)))
        return run_command(args, timeout)

    monkeypatch.setattr(alerts, "command", command)
    assert rt.run() == 0
    assert warned_before and all(warned_before)
    assert len(warnings(rt)) == 1


def test_an_open_backlog_warning_does_not_hide_a_health_warning(rt) -> None:
    at(rt, 7, 10)
    rt.metrics[OVERDUE] = 600
    assert rt.run() == 0
    assert len(warnings(rt)) == 1
    rt.health_streaks["clashlens-collector"] = 2
    rt.now += 60
    assert rt.run() == 0
    assert len(warnings(rt)) == 2
    assert "clashlens-collector failed its last 2 health checks" in warnings(rt)[1]


def test_a_health_failure_starting_during_the_slow_checks_warns_in_the_same_run(
    rt, monkeypatch
) -> None:
    # The minute timer skips while a run is busy, so a run's slow checks can
    # outlast the three minutes before Podman kills a failing container.
    run_command = alerts.command

    def command(args, timeout=15):
        if "backup-status" in args:
            rt.health_streaks["clashlens-python-worker"] = 2
        return run_command(args, timeout)

    monkeypatch.setattr(alerts, "command", command)
    assert rt.run() == 0
    assert len(warnings(rt)) == 1
    assert "clashlens-python-worker failed its last 2 health checks" in warnings(rt)[0]
