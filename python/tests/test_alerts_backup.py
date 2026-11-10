"""Alerts backup behavior."""

import json
import os
import subprocess
from datetime import UTC, datetime

import pytest
from alerts_support import trigger


def test_one_timed_out_backup_check_does_not_alert(runtime, capsys):
    rt = runtime
    rt.backup_error = subprocess.TimeoutExpired(["backup-status"], 25)
    assert rt.run() == 1
    assert "Backup check timed out" in capsys.readouterr().err
    rt.now += 60
    rt.backup_error = None
    assert rt.run() == 0
    assert not rt.posts


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["backup-status"], 25),
        OSError("private failure detail"),
        subprocess.SubprocessError("private failure detail"),
    ],
)
def test_backup_check_unavailable_for_fifteen_minutes_alerts_once_then_recovers(
    runtime, error, capsys
):
    rt = runtime
    started = datetime.fromtimestamp(rt.now, UTC).isoformat()
    rt.backup_error = error
    for _ in range(15):
        assert rt.run() == 1
        rt.now += 60
    assert not rt.posts
    assert rt.run() == 1
    assert len(rt.posts) == 1
    assert "backup check failed" in rt.posts[0]["content"]
    assert f"First observed {started}." in rt.posts[0]["content"]
    rt.now += 60
    assert rt.run() == 1
    assert len(rt.posts) == 1
    rt.now += 60
    rt.backup_error = None
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]
    assert f"Incident first observed {started}." in rt.posts[1]["content"]
    assert rt.run() == 0
    assert len(rt.posts) == 2
    output = capsys.readouterr()
    assert "Backup check" in output.err
    assert "private" not in output.out + output.err + json.dumps(rt.posts)


@pytest.mark.parametrize("timeout_first", [False, True])
def test_completed_backup_failure_alerts_immediately_and_logs_without_secrets(
    runtime, timeout_first, capsys
):
    rt = runtime
    started = datetime.fromtimestamp(rt.now, UTC).isoformat()
    if timeout_first:
        rt.backup_error = subprocess.TimeoutExpired(["backup-status"], 25)
        assert rt.run() == 1
        assert not rt.posts
        rt.now += 60
    rt.backup_error = None
    trigger(rt, "backup")
    assert rt.run() == 1
    assert len(rt.posts) == 1
    assert f"First observed {started}." in rt.posts[0]["content"]
    assert rt.run() == 1
    assert len(rt.posts) == 1
    rt.now += 60
    trigger(rt, "backup", False)
    rt.backup_error = subprocess.TimeoutExpired(["backup-status"], 25)
    assert rt.run() == 1
    assert len(rt.posts) == 1
    rt.now += 60
    rt.backup_error = None
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert f"Incident first observed {started}." in rt.posts[1]["content"]
    output = capsys.readouterr()
    assert "Backup check failed; run ./ops backup-status" in output.err
    assert "private" not in output.out + output.err + json.dumps(rt.posts)


def test_successful_backup_check_restarts_grace_clock(runtime):
    rt = runtime
    error = subprocess.TimeoutExpired(["backup-status"], 25)
    rt.backup_error = error
    assert rt.run() == 1
    rt.now += 840
    rt.backup_error = None
    assert rt.run() == 0
    rt.now += 60
    started = datetime.fromtimestamp(rt.now, UTC).isoformat()
    rt.backup_error = error
    assert rt.run() == 1
    rt.now += 899
    assert rt.run() == 1
    assert not rt.posts
    rt.now += 1
    assert rt.run() == 1
    assert len(rt.posts) == 1
    assert f"First observed {started}." in rt.posts[0]["content"]


def test_backup_grace_and_first_observed_time_exclude_intentional_stop(runtime):
    rt = runtime
    rt.backup_error = subprocess.TimeoutExpired(["backup-status"], 25)
    assert rt.run() == 1
    rt.now += 840
    intent = rt.state_dir / "alert-intent"
    intent.write_text("stopped\n")
    assert rt.run() == 0
    rt.now += 86400
    intent.write_text("running\n")
    os.utime(intent, (rt.now, rt.now))
    resumed = datetime.fromtimestamp(rt.now, UTC).isoformat()
    assert rt.run() == 1
    rt.now += 899
    assert rt.run() == 1
    assert not rt.posts
    rt.now += 1
    assert rt.run() == 1
    assert len(rt.posts) == 1
    assert f"First observed {resumed}." in rt.posts[0]["content"]
    rt.now += 60
    rt.backup_error = None
    assert rt.run() == 0
    assert f"Incident first observed {resumed}." in rt.posts[1]["content"]


def test_delayed_backup_delivery_retry_keeps_first_observed_time(runtime):
    rt = runtime
    started = datetime.fromtimestamp(rt.now, UTC).isoformat()
    rt.backup_error = subprocess.TimeoutExpired(["backup-status"], 25)
    assert rt.run() == 1
    rt.now += 900
    rt.post_status = 500
    assert rt.run() == 1
    assert not rt.posts
    first = rt.attempts[0]
    assert f"First observed {started}." in first["content"]
    rt.now += 60
    rt.backup_error = None
    rt.post_status = 204
    assert rt.run() == 0
    assert rt.posts == [first]
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert f"Incident first observed {started}." in rt.posts[1]["content"]
