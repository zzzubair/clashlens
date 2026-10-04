"""The 15-minute daily-calculation and unreadable-monitoring alerts, against a
local HTTP server, never Discord."""

import json
import os
import subprocess
from datetime import UTC, datetime

import pytest
import test_alerts
from test_alerts import FAKE_WEBHOOK

from clashlens import alerts

runtime = test_alerts.runtime

PREFIX = "clashlens_collector_"
DAILY = f"{PREFIX}oldest_job_reconcile_ranked_day_age_seconds"
ORDINARY = f"{PREFIX}oldest_pending_processing_age_seconds"
OTHER = f"{PREFIX}oldest_job_process_observation_age_seconds"


def real_holds(rt, monkeypatch):
    for name, value in rt.holds.items():
        monkeypatch.setattr(alerts, name, value)


def processing(rt):
    return [p["content"] for p in rt.posts if "Daily result calculations" in p["content"]]


def monitoring(rt):
    return [p["content"] for p in rt.posts if "unreadable for at least" in p["content"]]


def at(rt, seconds, start):
    rt.now = start + seconds
    return rt.run()


@pytest.mark.parametrize(
    ("daily", "other", "alerted"),
    [(899, 899, False), (900, 900, True), (None, 1799, False), (0, 1800, True)],
)
def test_daily_work_alerts_at_15_minutes_and_other_work_at_30(
    runtime, daily, other, alerted
):
    rt = runtime
    if daily is not None:
        rt.metrics[DAILY] = daily
        rt.metrics[ORDINARY] = daily
    if other:
        rt.metrics[OTHER] = other
        rt.metrics[ORDINARY] = other
    assert rt.run() == 0
    assert len(rt.posts) == len(processing(rt)) == int(alerted)


def test_daily_and_other_work_share_one_incident_and_both_must_clear(
    runtime, monkeypatch
):
    rt = runtime
    real_holds(rt, monkeypatch)
    start = rt.now
    # One overdue daily job is enough; an older build is not named.
    rt.metrics |= {
        DAILY: 900,
        ORDINARY: 900,
        f"{PREFIX}oldest_job_build_snapshot_age_seconds": 9000,
    }
    assert at(rt, 0, start) == 0
    assert len(processing(rt)) == 1
    assert "Oldest waiting: daily result calculations, 15 minutes." in rt.posts[0]["content"]
    # Reaching 30 minutes, or other work becoming oldest, is the same incident.
    rt.metrics |= {DAILY: 1800, ORDINARY: 1900, OTHER: 1900}
    assert at(rt, 60, start) == 0
    # Daily work cleared, other work still overdue: no recovery.
    rt.metrics[DAILY] = 899
    for seconds in range(120, 1200, 60):
        assert at(rt, seconds, start) == 0
    # Other work cleared, daily work overdue again: no recovery.
    rt.metrics |= {DAILY: 900, ORDINARY: 1799, OTHER: 1799}
    for seconds in range(1200, 2280, 60):
        assert at(rt, seconds, start) == 0
    assert len(rt.posts) == 1
    rt.metrics |= {DAILY: 899, ORDINARY: 899, OTHER: 899}
    assert at(rt, 2280, start) == 0
    assert at(rt, 2280 + 899, start) == 0
    assert len(rt.posts) == 1
    assert at(rt, 2280 + 900, start) == 0
    assert len(rt.posts) == 2
    cleared = datetime.fromtimestamp(start + 2280, UTC).isoformat()
    assert f"recovered at {cleared}" in rt.posts[1]["content"]


def test_unreadable_work_ages_do_not_recover_daily_work_alert(runtime, monkeypatch):
    rt = runtime
    real_holds(rt, monkeypatch)
    start = rt.now
    rt.metrics |= {DAILY: 900, ORDINARY: 900}
    assert at(rt, 0, start) == 0
    # Clear for 800 seconds, then collector metrics fail: the clear time restarts.
    del rt.metrics[DAILY]
    rt.metrics[ORDINARY] = 0
    assert at(rt, 60, start) == 0
    rt.metrics_status = 503
    assert at(rt, 860, start) == 1
    rt.metrics_status = 200
    assert at(rt, 920, start) == 0
    assert at(rt, 920 + 899, start) == 0
    assert len(processing(rt)) == 1
    assert at(rt, 920 + 900, start) == 0
    assert "recovered" in processing(rt)[1]


def test_missing_work_ages_are_unknown_unless_daily_work_is_overdue(runtime):
    rt = runtime
    del rt.metrics[ORDINARY]
    rt.metrics[DAILY] = 899
    assert rt.run() == 0
    assert not rt.posts
    rt.metrics[DAILY] = 900
    assert rt.run() == 0
    assert len(processing(rt)) == 1
    # Neither a missing ordinary age nor a response without the processing
    # count can clear it, even with no recovery hold.
    rt.metrics[DAILY] = 899
    assert rt.run() == 0
    pending = rt.metrics.pop(f"{PREFIX}pending_processing")
    rt.metrics |= {DAILY: 0, ORDINARY: 0}
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.metrics[f"{PREFIX}pending_processing"] = pending
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]


def test_daily_work_alerts_during_reset_and_immediately_after_resume(runtime):
    rt = runtime
    rt.now = datetime(2026, 9, 28, 5, 2, tzinfo=UTC).timestamp()
    rt.metrics |= {
        f"{PREFIX}reset_total": 200,
        f"{PREFIX}reset_terminal": 100,
        DAILY: 900,
        ORDINARY: 900,
    }
    assert rt.run() == 0
    assert len(rt.posts) == len(processing(rt)) == 1

    rt.posts.clear()
    rt.state_dir = rt.state_dir.parent / "resumed"
    intent = rt.state_dir / "alert-intent"
    rt.state_dir.mkdir()
    intent.write_text("stopped\n")
    assert rt.run() == 0
    assert not rt.posts
    rt.now += 3600
    intent.write_text("running\n")
    os.utime(intent, (rt.now, rt.now))
    assert rt.run() == 0
    assert len(rt.posts) == len(processing(rt)) == 1


def unreadable_disk(rt, _monkeypatch):
    rt.volume_failed = True


def unreadable_restarts(rt, _monkeypatch):
    rt.journal_failed = True


def unreadable_leaderboard(rt, _monkeypatch):
    rt.leaderboard = ""


def unreadable_publication(rt, _monkeypatch):
    rt.publication = ""


def break_command(output=None, code=0, error=None, match="--publication"):
    def setup(_rt, monkeypatch):
        inner = alerts.command

        def command(args, timeout=15):
            if not any(match in arg for arg in args):
                return inner(args, timeout)
            if error:
                raise error
            return subprocess.CompletedProcess(args, code, output, "private detail")

        monkeypatch.setattr(alerts, "command", command)

    return setup


def missing_spool_count(rt, _monkeypatch):
    del rt.metrics["clashlens_spool_objects"]


def unreadable_filesystem(_rt, monkeypatch):
    def disk_usage(_path):
        raise OSError("private detail")

    monkeypatch.setattr(alerts.shutil, "disk_usage", disk_usage)


@pytest.mark.parametrize(
    "fail",
    [
        unreadable_disk,
        unreadable_restarts,
        unreadable_leaderboard,
        unreadable_publication,
        break_command("0", code=1),
        break_command(error=subprocess.TimeoutExpired(["probe"], 25)),
        break_command("x"),
        break_command("0 13000", match="--leaderboard"),
        break_command("0 13000 0", code=1, match="--leaderboard"),
        break_command(error=subprocess.TimeoutExpired(["journal"], 15), match="MESSAGE_ID"),
        break_command("not json", match="MESSAGE_ID"),
        missing_spool_count,
        unreadable_filesystem,
    ],
)
def test_unreadable_check_alerts_once_after_ten_minutes_then_recovers(
    runtime, monkeypatch, capsys, fail
):
    rt = runtime
    start = rt.now
    healthy = alerts.command, alerts.shutil.disk_usage
    fail(rt, monkeypatch)
    assert at(rt, 0, start) == 1
    assert at(rt, 599, start) == 1
    assert not rt.posts
    assert "unavailable" in capsys.readouterr().err
    assert at(rt, 600, start) == 1
    assert len(rt.posts) == len(monitoring(rt)) == 1
    assert "journalctl --user -u clashlens-alert.service" in rt.posts[0]["content"]
    assert at(rt, 660, start) == 1
    assert len(rt.posts) == 1
    monkeypatch.setattr(alerts, "command", healthy[0])
    monkeypatch.setattr(alerts.shutil, "disk_usage", healthy[1])
    rt.volume_failed = rt.journal_failed = False
    rt.leaderboard, rt.publication = "0 13000 0", "0"
    rt.metrics["clashlens_spool_objects"] = 0
    assert at(rt, 720, start) == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]
    output = capsys.readouterr()
    assert "private detail" not in output.err + json.dumps(rt.posts)


def test_each_unreadable_check_has_its_own_ten_minutes(runtime):
    rt = runtime
    start = rt.now
    rt.volume_failed = True
    assert at(rt, 0, start) == 1
    rt.volume_failed, rt.journal_failed = False, True
    assert at(rt, 599, start) == 1
    assert at(rt, 600, start) == 1
    assert at(rt, 1198, start) == 1
    assert not rt.posts
    assert at(rt, 1199, start) == 1
    assert len(monitoring(rt)) == 1


def test_a_check_failing_again_starts_a_new_ten_minutes(runtime):
    rt = runtime
    start = rt.now
    rt.volume_failed, rt.leaderboard = True, ""
    assert at(rt, 0, start) == 1
    rt.volume_failed = False
    assert at(rt, 300, start) == 1
    rt.volume_failed = True
    assert at(rt, 400, start) == 1
    rt.leaderboard = "0 13000 0"
    assert at(rt, 500, start) == 1
    assert at(rt, 999, start) == 1
    assert not rt.posts
    assert at(rt, 1000, start) == 1
    assert len(monitoring(rt)) == 1


def test_all_four_unreadable_checks_send_one_alert(runtime):
    rt = runtime
    start = rt.now
    rt.volume_failed = rt.journal_failed = True
    rt.leaderboard = rt.publication = ""
    assert at(rt, 0, start) == 1
    assert at(rt, 600, start) == 1
    assert len(rt.posts) == 1


def test_a_new_unreadable_check_stops_the_recovery(runtime, monkeypatch):
    rt = runtime
    real_holds(rt, monkeypatch)
    start = rt.now
    rt.volume_failed = True
    assert at(rt, 0, start) == 1
    assert at(rt, 600, start) == 1
    rt.volume_failed = False
    assert at(rt, 660, start) == 0
    rt.publication = ""
    assert at(rt, 660 + 899, start) == 1
    assert at(rt, 660 + 900, start) == 1
    assert len(rt.posts) == 1
    rt.publication = "0"
    assert at(rt, 1620, start) == 0
    assert at(rt, 1620 + 899, start) == 0
    assert len(rt.posts) == 1
    assert at(rt, 1620 + 900, start) == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]


def test_readable_but_unhealthy_checks_are_not_unreadable(runtime):
    rt = runtime
    start = rt.now
    rt.publication, rt.disk_used, rt.leaderboard = "", 81, "1 13000 1201"
    assert at(rt, 0, start) == 1
    assert at(rt, 600, start) == 1
    assert len(monitoring(rt)) == 1
    # Readable again, with its own problem: monitoring recovers, the problem alerts.
    rt.publication = "1"
    assert at(rt, 660, start) == 0
    assert "recovered" in monitoring(rt)[1]
    assert any("publication time" in p["content"] for p in rt.posts)
    assert at(rt, 1320, start) == 0
    assert len(monitoring(rt)) == 2


@pytest.mark.parametrize(("leaderboard", "alerted"), [("1 13000 1201", 0), ("", 1)])
def test_reset_pause_hides_stale_but_not_unreadable_leaderboard(
    runtime, monkeypatch, leaderboard, alerted
):
    rt = runtime
    real_holds(rt, monkeypatch)
    start = datetime(2026, 9, 28, 4, 55, tzinfo=UTC).timestamp()
    rt.metrics |= {f"{PREFIX}reset_total": 200, f"{PREFIX}reset_terminal": 100}
    rt.leaderboard = leaderboard
    for seconds in range(0, 660, 60):
        at(rt, seconds, start)
    assert len(monitoring(rt)) == alerted
    # After the sweep, a stale board waits out its own five minutes.
    rt.metrics[f"{PREFIX}reset_terminal"] = 200
    rt.leaderboard = "1 13000 1201"
    for seconds in range(660, 960, 60):
        assert at(rt, seconds, start) == 0
    assert len(rt.posts) == alerted


@pytest.mark.parametrize(
    "fail",
    [
        lambda rt: setattr(rt, "backup_error", subprocess.TimeoutExpired(["b"], 25)),
        lambda rt: setattr(rt, "backup_error", OSError("private detail")),
        lambda rt: setattr(rt, "backup_failed", True),
        lambda rt: setattr(rt, "reads_failed", True),
        # A bad collector line after the spool counts: only the collector is unreadable.
        lambda rt: rt.metrics.update({f"{PREFIX}zz_bad": -1}),
    ],
)
def test_checks_with_their_own_alert_do_not_count_as_unreadable(runtime, fail):
    rt = runtime
    start = rt.now
    fail(rt)
    for seconds in (0, 600, 900, 960):
        at(rt, seconds, start)
    assert not monitoring(rt)


def test_unreadable_time_survives_reloads_and_excludes_stopped_time(
    runtime, monkeypatch
):
    rt = runtime
    real_holds(rt, monkeypatch)
    start = rt.now
    intent = rt.state_dir / "alert-intent"
    rt.volume_failed = True
    assert at(rt, 0, start) == 1
    intent.write_text("stopped\n")
    assert at(rt, 300, start) == 0
    intent.write_text("running\n")
    os.utime(intent, (start + 3600, start + 3600))
    assert at(rt, 3600, start) == 1
    assert at(rt, 3600 + 599, start) == 1
    assert not rt.posts
    assert at(rt, 3600 + 600, start) == 1
    assert len(monitoring(rt)) == 1
    # An open alert survives a stop and needs 15 readable minutes after it.
    rt.volume_failed = False
    assert at(rt, 4260, start) == 0
    intent.write_text("stopped\n")
    assert at(rt, 5000, start) == 0
    intent.write_text("running\n")
    os.utime(intent, (start + 7200, start + 7200))
    assert at(rt, 7200, start) == 0
    assert at(rt, 7200 + 899, start) == 0
    assert len(rt.posts) == 1
    assert at(rt, 7200 + 900, start) == 0
    assert "recovered" in rt.posts[1]["content"]


def test_retried_monitoring_alert_waits_15_minutes_after_delivery(runtime, monkeypatch):
    rt = runtime
    real_holds(rt, monkeypatch)
    start = rt.now
    rt.volume_failed = True
    assert at(rt, 0, start) == 1
    rt.post_status = 500
    assert at(rt, 600, start) == 1
    rt.volume_failed = False
    assert at(rt, 660, start) == 1
    rt.post_status = 204
    assert at(rt, 720, start) == 0
    assert len(rt.posts) == 1
    assert rt.posts[0]["allowed_mentions"] == {"parse": []}
    assert at(rt, 720 + 899, start) == 0
    assert len(rt.posts) == 1
    assert at(rt, 720 + 900, start) == 0
    cleared = datetime.fromtimestamp(start + 660, UTC).isoformat()
    assert f"recovered at {cleared}" in rt.posts[1]["content"]


def test_old_state_works_and_saved_state_stays_bounded(runtime, capsys):
    rt = runtime
    rt.state_dir.mkdir()
    state_file = rt.state_dir / "alerts.json"
    state_file.write_text(json.dumps({"last_success": rt.now, "incidents": {}}))
    failures = (
        ("volume_failed", True, False),
        ("journal_failed", True, False),
        ("leaderboard", "", "0 13000 0"),
        ("publication", "", "0"),
    )
    for step in range(60):
        for index, (name, broken, fine) in enumerate(failures):
            setattr(rt, name, broken if (step // 12 + index) % 3 else fine)
        rt.now += 60
        rt.run()
    assert monitoring(rt)
    state = state_file.read_text()
    assert len(json.loads(state)["monitoring_failing_since"]) <= 4
    assert len(state) < 4096
    output = capsys.readouterr()
    for private in (FAKE_WEBHOOK, "fake-secret", "private"):
        assert private not in state + json.dumps(rt.posts) + output.err


def test_website_check_sends_only_the_website_alert(runtime):
    rt = runtime
    state_dir = rt.state_dir.parent / "uptime"
    config = {"webhook_file": rt.config["webhook_file"], "urls": [rt.origin + "/healthz"]}
    rt.site_status = 503
    for _ in range(3):
        alerts.run(config, state_dir, None, alerts.observe_site)
        rt.now += 60
    assert len(rt.posts) == 1
    assert "outside the server" in rt.posts[0]["content"]
    state = json.loads((state_dir / "alerts.json").read_text())
    assert set(state["incidents"]) == {"site"}
