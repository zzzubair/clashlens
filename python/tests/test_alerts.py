"""Observable alert delivery against a local HTTP server, never Discord."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alerts_support import (
    FAKE_WEBHOOK,
    ROOT,
    board_page,
    reset_at,
    trigger,
)

from clashlens import alerts


def old_board_page(reset: int) -> str:
    """The same heading before the redesign, still read while either side ships."""
    shown = datetime.fromtimestamp(reset, UTC)
    return (
        '<p class="rankings-kicker">Day <!-- -->7</p><h1 id="leaderboard-title">'
        f'Leaderboard at <time class="local-time" dateTime="{shown:%Y-%m-%dT%H:%M:%SZ}"'
        f' title="{shown:%-d %b %Y, %H:%M} UTC">{shown:%-d %b %Y, %H:%M} UTC</time></h1>'
    )




@pytest.mark.parametrize(
    "condition",
    [
        "tracker",
        "spool_bytes",
        "spool_objects",
        "filesystem",
        "restarts",
        "reads",
        "collection",
        "leaderboard",
        "failures",
        "processing",
        "upload",
        "publication",
        "completeness",
        "battle_day",
    ],
)
def test_alert_and_recovery_once_across_separate_runs(runtime, condition, capsys):
    rt = runtime
    assert rt.run() == 0
    trigger(rt, condition)
    assert rt.run() == 0
    assert len(rt.posts) == 1
    assert "alert:" in rt.posts[0]["content"]
    assert "2026-09-27" in rt.posts[0]["content"]
    assert "./ops" in rt.posts[0]["content"]
    rt.now += 60
    assert rt.run() == 0
    assert len(rt.posts) == 1
    trigger(rt, condition, False)
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]
    assert rt.run() == 0
    assert len(rt.posts) == 2
    state = rt.state_dir / "alerts.json"
    assert state.stat().st_mode & 0o777 == 0o600
    assert len(state.read_bytes()) < 4096
    output = capsys.readouterr()
    for private in (FAKE_WEBHOOK, "private account", "private backup", "fake-secret"):
        assert private not in output.out + output.err + state.read_text() + json.dumps(
            rt.posts
        )


@pytest.mark.parametrize("status", [302, 429, 500])
def test_retry_keeps_original_incident_even_if_it_recovers(runtime, status, capsys):
    rt = runtime
    trigger(rt, "reads")
    rt.post_status = status
    assert rt.run() == 1
    assert not rt.posts
    first = rt.attempts[0]
    rt.now += 60
    trigger(rt, "reads", False)
    rt.post_status = 204
    assert rt.run() == 0
    assert rt.posts == [first]
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]
    assert rt.run() == 0
    assert len(rt.posts) == 2
    err = capsys.readouterr().err
    assert "delivery failed" in err
    assert FAKE_WEBHOOK not in err

def test_failed_recovery_retries_without_resending_alert(runtime):
    rt = runtime
    trigger(rt, "reads")
    assert rt.run() == 0
    trigger(rt, "reads", False)
    rt.post_status = 500
    assert rt.run() == 1
    rt.now += 60
    rt.post_status = 200
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[-1]["content"]


@pytest.mark.parametrize("mode", [None, 0o644])
def test_missing_or_public_webhook_fails_loudly(runtime, monkeypatch, capsys, mode):
    rt = runtime
    path = Path(rt.config["webhook_file"])
    if mode is None:
        path.unlink()
    else:
        path.chmod(mode)
    monkeypatch.setattr(
        alerts.sys,
        "argv",
        [
            "alerts",
            str(rt.state_dir),
            str(ROOT),
            str(path),
            str(rt.config["health_port"]),
            rt.config["spool_root"],
            "100",
            "100",
            rt.config["public_origin"],
        ],
    )
    assert alerts.main() == 1
    assert "Webhook file" in capsys.readouterr().err
    assert not rt.attempts

def test_thresholds_are_strict_and_restarts_are_per_service(runtime):
    rt = runtime
    rt.disk_used = 80
    rt.metrics["clashlens_spool_bytes"] = 80
    rt.metrics["clashlens_spool_objects"] = 80
    rt.metrics["clashlens_collector_last_success_age_seconds"] = 599
    rt.restarts = ["clashlens-api.service"] * 3 + ["clashlens-worker.service"] * 3
    rt.restarts += ["pipewire.service", "clashlens-preview-api.service"] * 4
    assert rt.run() == 0
    assert not rt.posts
    rt.metrics["clashlens_collector_last_success_age_seconds"] = 600
    assert rt.run() == 0
    assert len(rt.posts) == 1

def test_reset_pause_is_excluded_but_stuck_reset_work_still_alerts(runtime):
    rt = runtime
    last = datetime(2026, 9, 27, 4, 54, tzinfo=UTC).timestamp()
    for hour, minute in ((4, 59), (5, 0), (5, 8)):
        rt.now = datetime(2026, 9, 27, hour, minute, tzinfo=UTC).timestamp()
        rt.metrics["clashlens_collector_last_success_age_seconds"] = rt.now - last
        assert rt.run() == 0
        assert not rt.posts
    rt.now += 60
    rt.metrics["clashlens_collector_last_success_age_seconds"] = rt.now - last
    assert rt.run() == 0
    assert len(rt.posts) == 1


def test_slow_collection_alerts_when_reset_finishes_before_half_past(runtime):
    rt = runtime
    rt.now = datetime(2026, 9, 27, 5, 1, tzinfo=UTC).timestamp()
    rt.metrics["clashlens_collector_oldest_due_age_seconds"] = 1800
    rt.metrics["clashlens_collector_reset_total"] = 200
    rt.metrics["clashlens_collector_reset_terminal"] = 199
    assert rt.run() == 0
    assert not rt.posts
    rt.now = datetime(2026, 9, 27, 5, 20, tzinfo=UTC).timestamp()
    rt.metrics["clashlens_collector_reset_terminal"] = 200
    assert rt.run() == 0
    assert len(rt.posts) == 1
    assert "overdue" in rt.posts[0]["content"]
    rt.now += 86400 - 15 * 60
    rt.metrics["clashlens_collector_reset_terminal"] = 0
    rt.metrics["clashlens_collector_oldest_due_age_seconds"] = 1
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.metrics["clashlens_collector_reset_terminal"] = 200
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[-1]["content"]


def test_unfinished_reset_holds_overdue_alert_after_half_past(runtime):
    rt = runtime
    rt.now = datetime(2026, 9, 27, 6, tzinfo=UTC).timestamp()
    trigger(rt, "collection")
    rt.metrics["clashlens_collector_reset_total"] = 200
    rt.metrics["clashlens_collector_reset_terminal"] = 199
    assert rt.run() == 0
    assert not rt.posts
    rt.metrics["clashlens_collector_reset_total"] = 0
    rt.metrics["clashlens_collector_reset_terminal"] = 0
    assert rt.run() == 0
    assert len(rt.posts) == 1


@pytest.mark.parametrize(
    "metric",
    ["clashlens_collector_reset_total", "clashlens_collector_reset_terminal"],
)
def test_missing_reset_progress_does_not_clear_overdue_alert(runtime, metric):
    rt = runtime
    trigger(rt, "collection")
    assert rt.run() == 0
    trigger(rt, "collection", False)
    del rt.metrics[metric]
    assert rt.run() == 0
    assert len(rt.posts) == 1


def test_unreadable_leaderboard_freshness_fails_the_check_without_clearing(runtime):
    rt = runtime
    trigger(rt, "leaderboard")
    assert rt.run() == 0
    rt.leaderboard = ""
    assert rt.run() == 1
    assert len(rt.posts) == 1


@pytest.mark.parametrize(
    "page",
    [
        '<h1 id="leaderboard-title">Leaderboard at 8 Oct 2026, 05:00 UTC</h1>',
        '<h1 id="leaderboard-title">Daily leaderboard</h1>',
        board_page(reset_at(datetime(2026, 10, 9, 12, tzinfo=UTC).timestamp())).replace(
            "05:00:00Z", "05:01:00Z"
        ),
    ],
)
def test_daily_page_without_the_board_fails_the_check(runtime, page, capsys):
    rt = runtime
    rt.board_page = page
    assert rt.run() == 1
    assert "Reset publication status unavailable" in capsys.readouterr().err


def test_the_daily_page_from_before_the_redesign_still_shows_the_board(
    runtime, capsys
):
    rt = runtime
    rt.board_page = old_board_page(reset_at(rt.now))
    assert rt.run() == 0
    assert "Reset publication status unavailable" not in capsys.readouterr().err


def test_empty_leaderboard_is_healthy_and_recovers_an_open_alert(runtime):
    rt = runtime
    rt.leaderboard = "0 0 0"
    assert rt.run() == 0
    assert not rt.posts
    trigger(rt, "leaderboard")
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.leaderboard = "0 0 0"
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[-1]["content"]


def test_stale_leaderboard_alerts_only_when_widespread_or_long_for_five_minutes(
    runtime, monkeypatch
):
    rt = runtime
    for name, value in rt.holds.items():
        monkeypatch.setattr(alerts, name, value)

    def minutes(count, board):
        rt.leaderboard = board
        for _ in range(count):
            assert rt.run() == 0
            rt.now += 60

    # Exactly 5% stale, or one player exactly 20 minutes old, is tolerated.
    minutes(10, "650 13000 1200")
    # Over the line for four minutes, then back: no alert and no recovery.
    minutes(4, "651 13000 900")
    minutes(1, "0 13000 300")
    minutes(4, "1 13000 1201")
    minutes(1, "0 13000 300")
    assert not rt.posts
    # Either rule alone alerts on its sixth check in a row.
    for board in ("651 13000 900", "1 13000 1201"):
        minutes(5, board)
        assert len(rt.posts) % 2 == 0
        minutes(1, board)
        assert "Live Leaderboard" in rt.posts[-1]["content"]
        minutes(16, "0 13000 300")
        assert "recovered" in rt.posts[-1]["content"]
    assert len(rt.posts) == 4
    # 04:55-05:15 the Reset sweep refreshes every player, so widespread
    # staleness under 20 minutes is expected. From 05:15 it counts again,
    # even while Reset collection is unfinished (8 Oct 2026).
    rt.now = datetime(2026, 9, 28, 4, 55, tzinfo=UTC).timestamp()
    rt.metrics["clashlens_collector_reset_total"] = 200
    rt.metrics["clashlens_collector_reset_terminal"] = 199
    minutes(25, "9000 13000 1200")
    assert len(rt.posts) == 4
    minutes(1, "9000 13000 1200")
    assert len(rt.posts) == 5


def test_recovery_waits_for_fifteen_clear_minutes_and_folds_repeats(
    runtime, monkeypatch
):
    rt = runtime
    for name, value in rt.holds.items():
        monkeypatch.setattr(alerts, name, value)
    trigger(rt, "reads")
    assert rt.run() == 0
    assert len(rt.posts) == 1
    # Clear for 14 minutes, then failing again: still the same incident.
    trigger(rt, "reads", False)
    for _ in range(15):
        rt.now += 60
        assert rt.run() == 0
    trigger(rt, "reads")
    rt.now += 60
    assert rt.run() == 0
    assert len(rt.posts) == 1
    trigger(rt, "reads", False)
    rt.now += 60
    cleared = rt.now
    assert rt.run() == 0
    rt.now += 899
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.now += 1
    assert rt.run() == 0
    assert len(rt.posts) == 2
    first_cleared = datetime.fromtimestamp(cleared, UTC).isoformat()
    assert f"recovered at {first_cleared}" in rt.posts[1]["content"]
    rt.now += 60
    assert rt.run() == 0
    assert len(rt.posts) == 2


def test_stopped_time_counts_toward_neither_hold(runtime, monkeypatch):
    rt = runtime
    for name, value in rt.holds.items():
        monkeypatch.setattr(alerts, name, value)
    trigger(rt, "reads")
    assert rt.run() == 0
    trigger(rt, "reads", False)
    rt.leaderboard = "0 13000 1201"
    rt.now += 60
    assert rt.run() == 0
    intent = rt.state_dir / "alert-intent"
    intent.write_text("stopped\n")
    rt.now += 3600
    intent.write_text("running\n")
    os.utime(intent, (rt.now, rt.now))
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.now += 299
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.now += 1
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "Live Leaderboard" in rt.posts[1]["content"]
    rt.now += 600
    assert rt.run() == 0
    assert len(rt.posts) == 3
    assert "recovered" in rt.posts[2]["content"]


def test_retried_alert_still_waits_fifteen_clear_minutes_to_recover(
    runtime, monkeypatch
):
    rt = runtime
    for name, value in rt.holds.items():
        monkeypatch.setattr(alerts, name, value)
    # Rejected at 12:00, clear from 12:01, still rejected until 12:16.
    rt.now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC).timestamp()
    trigger(rt, "reads")
    rt.post_status = 500
    assert rt.run() == 1
    trigger(rt, "reads", False)
    cleared = rt.now + 60
    for _ in range(15):
        rt.now += 60
        assert rt.run() == 1
    # The 12:16 check takes 90 seconds before Discord accepts the alert.
    rt.now += 60
    rt.post_status = 204
    monkeypatch.setattr(alerts.time, "time", lambda: rt.now + 90 * len(rt.posts))
    assert rt.run() == 0
    monkeypatch.setattr(alerts.time, "time", lambda: rt.now)
    assert len(rt.posts) == 1
    assert "recovered" not in rt.posts[0]["content"]
    rt.now += 90 + 899
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.now += 1
    assert rt.run() == 0
    assert len(rt.posts) == 2
    first_cleared = datetime.fromtimestamp(cleared, UTC).isoformat()
    assert f"recovered at {first_cleared}" in rt.posts[1]["content"]


def test_unavailable_leaderboard_check_restarts_the_five_minutes(
    runtime, monkeypatch
):
    rt = runtime
    for name, value in rt.holds.items():
        monkeypatch.setattr(alerts, name, value)
    rt.now = datetime(2026, 9, 28, 4, 50, tzinfo=UTC).timestamp()
    rt.leaderboard = "1 13000 1201"
    assert rt.run() == 0
    # Unavailable from 04:54, through the Reset pause, until 05:04.
    rt.now += 240
    rt.leaderboard = ""
    for _ in range(10):
        assert rt.run() == 1
        rt.now += 60
    rt.leaderboard = "1 13000 1201"
    for _ in range(5):
        assert rt.run() == 0
        rt.now += 60
    assert not rt.posts
    assert rt.run() == 0
    assert len(rt.posts) == 1
    # An open alert stays open through an unavailable check.
    rt.leaderboard = ""
    rt.now += 60
    assert rt.run() == 1
    assert len(rt.posts) == 1


@pytest.mark.parametrize(
    "missing",
    ["clashlens_collector_reset_total", "clashlens_collector_reset_terminal", None],
)
def test_the_leaderboard_check_does_not_wait_on_collector_reset_progress(
    runtime, missing
):
    # Until 8 Oct 2026 unknown or unfinished Reset collection paused this
    # check, so a Reset that never finished hid stale live pages.
    rt = runtime
    if missing is None:
        rt.metrics_status = 503
    else:
        del rt.metrics[missing]
    rt.leaderboard = "9000 13000 1801"
    rt.run()
    assert len(rt.posts) == 1
    assert "Live Leaderboard" in rt.posts[0]["content"]
    rt.leaderboard = "0 13000 0"
    rt.run()
    assert "recovered" in rt.posts[-1]["content"]


def test_saved_work_alerts_clear_only_when_their_own_problem_clears(runtime):
    rt = runtime
    for condition in ("failures", "processing", "upload", "publication"):
        trigger(rt, condition)
    assert rt.run() == 0
    assert len(rt.posts) == 4
    rt.metrics_status = 503
    rt.publication = ""
    assert rt.run() == 1
    assert len(rt.posts) == 4
    # Finished work and a fresh Live Leaderboard leave a missing Reset
    # publication open.
    rt.metrics_status = 200
    for condition in ("failures", "processing", "upload"):
        trigger(rt, condition, False)
    rt.publication = "1"
    assert rt.run() == 0
    assert len(rt.posts) == 7
    assert all("recovered" in post["content"] for post in rt.posts[4:])
    assert not any("publication time" in post["content"] for post in rt.posts[4:])
    state = json.loads((rt.state_dir / "alerts.json").read_text())
    assert "site" not in state["incidents"]


def test_processing_alert_ignores_builds_and_names_the_oldest_work(runtime):
    rt = runtime
    prefix = "clashlens_collector_"
    rt.metrics |= {
        f"{prefix}oldest_pending_processing_age_seconds": 1799,
        f"{prefix}oldest_job_reconcile_ranked_day_age_seconds": 899,
        f"{prefix}oldest_job_process_observation_age_seconds": 1799,
        f"{prefix}oldest_job_build_snapshot_age_seconds": 7200,
    }
    assert rt.run() == 0
    assert not rt.posts
    rt.metrics[f"{prefix}oldest_pending_processing_age_seconds"] = 1860
    rt.metrics[f"{prefix}oldest_job_process_observation_age_seconds"] = 1860
    assert rt.run() == 0
    assert len(rt.posts) == 1
    assert "at least 30 minutes" in rt.posts[0]["content"]
    assert "Oldest waiting: saved API responses, 31 minutes." in rt.posts[0]["content"]


def test_missing_failure_age_is_unknown_unless_nothing_has_failed(runtime):
    rt = runtime

    def new_failures():
        return [p["content"] for p in rt.posts if "A new permanent failure" in p["content"]]

    trigger(rt, "failures")
    assert rt.run() == 0
    assert len(new_failures()) == 1
    # An older collector reports failed counts but no newest-failure ages.
    del rt.metrics["clashlens_collector_newest_failed_upload_age_seconds"]
    rt.metrics["clashlens_collector_failed_processing"] = 1
    assert rt.run() == 0
    assert len(new_failures()) == 1
    rt.metrics["clashlens_collector_failed_processing"] = 0
    del rt.metrics["clashlens_collector_failed_uploads"]
    assert rt.run() == 0
    assert len(new_failures()) == 1
    rt.metrics["clashlens_collector_failed_uploads"] = 0
    assert rt.run() == 0
    assert len(new_failures()) == 2
    assert "recovered" in new_failures()[-1]


def test_outside_check_alerts_after_two_minutes_down_and_again_on_recovery(runtime):
    rt = runtime
    state_dir = rt.state_dir.parent / "uptime"
    config = {
        "webhook_file": rt.config["webhook_file"],
        "urls": [rt.origin + "/readyz", rt.origin + "/healthz"],
    }

    def check(status=None, minutes=1):
        if status is not None:
            rt.site_status = status
        rt.now += 60 * minutes
        return alerts.run(config, state_dir, None, alerts.observe_site)

    assert check() == 0
    # A one-minute blip is not an outage.
    assert check(503) == 1
    assert check(200) == 0
    assert check(503) == 1
    assert check() == 1
    assert not rt.posts
    assert check() == 1
    assert len(rt.posts) == 1
    assert "outside the server" in rt.posts[0]["content"]
    assert check() == 1
    assert check(200) == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[1]["content"]
    state = json.loads((state_dir / "alerts.json").read_text())
    assert set(state["incidents"]) == {"site"}


def test_publication_probe_counts_resets_missing_their_publication(
    database_url, tmp_path, monkeypatch, capsys
):
    from datetime import timedelta

    import psycopg
    from domain_test_support import as_api_role, domain_database

    latest = datetime.now(UTC) - timedelta(minutes=70)
    if latest.hour < 5:
        latest -= timedelta(days=1)
    latest = latest.replace(hour=5, minute=0, second=0, microsecond=0)
    with domain_database(database_url) as connection_info:
        url_file = tmp_path / "database-url"
        url_file.write_text(as_api_role(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))

        def unpublished(*generations):
            with psycopg.connect(connection_info) as connection:
                connection.execute("DELETE FROM boundary_publication_generations")
                for days_ago, generation, snapshot, army in generations:
                    boundary = latest - timedelta(days=days_ago)
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_generations (
                            boundary_at, generation, ordering_rule_version,
                            freshness_rule_version, expected_population_count,
                            expected_population_hash, snapshot_state, army_state,
                            target_at
                        ) VALUES (%s, %s, 'order', 'freshness', 0, %s, %s, %s, %s)
                        """,
                        (boundary, generation, "0" * 64, snapshot, army,
                         boundary + timedelta(minutes=5)),
                    )
            capsys.readouterr()
            alerts.publication_probe()
            return int(capsys.readouterr().out)

        assert unpublished() == 0
        published = [(2, 1, "published", "published"), (1, 1, "superseded", "superseded"),
                     (1, 2, "pending", "pending")]
        assert unpublished(*published, (0, 1, "published", "published")) == 0
        # The frozen leaderboard published but the army results never did.
        assert unpublished(*published, (0, 1, "published", "pending")) == 1
        # A Reset with no publication record at all also counts.
        assert unpublished(*published[:1], (0, 1, "published", "published")) == 1
        assert unpublished(*published) == 1


@pytest.mark.parametrize("missing_metrics", [False, True])
def test_never_started_or_unreachable_tracker_alerts_after_ten_minutes(
    runtime, missing_metrics
):
    rt = runtime
    del rt.metrics["clashlens_collector_last_success_age_seconds"]
    if missing_metrics:
        rt.metrics_status = 503
    assert rt.run() == int(missing_metrics)
    rt.now += 600
    assert rt.run() == int(missing_metrics)
    # An unreachable collector also leaves spool usage unreadable.
    assert len(rt.posts) == 1 + int(missing_metrics)
    assert "No successful official API fetch" in rt.posts[0]["content"]
    if missing_metrics:
        assert "unreadable for at least 10 minutes" in rt.posts[1]["content"]


def test_unknown_checks_do_not_send_false_recovery(runtime):
    rt = runtime
    trigger(rt, "spool_bytes")
    trigger(rt, "restarts")
    assert rt.run() == 0
    rt.metrics_status = 503
    rt.journal_failed = True
    assert rt.run() == 1
    assert len(rt.posts) == 2


def test_spool_alert_survives_unavailable_volume_inspection(runtime):
    rt = runtime
    rt.volume_failed = True
    trigger(rt, "spool_bytes")
    assert rt.run() == 1
    assert len(rt.posts) == 1


def test_intentional_stop_suppresses_alerts_and_resume_excludes_stopped_time(runtime):
    rt = runtime
    assert rt.run() == 0
    intent = rt.state_dir / "alert-intent"
    intent.write_text("stopped\n")
    rt.backup_failed = True
    assert rt.run() == 0
    assert not rt.posts
    rt.backup_failed = False
    rt.now += 86400
    intent.write_text("running\n")
    os.utime(intent, (rt.now, rt.now))
    rt.metrics["clashlens_collector_last_success_age_seconds"] = 86400
    assert rt.run() == 0
    assert not rt.posts
    rt.now += 600
    rt.metrics["clashlens_collector_last_success_age_seconds"] += 600
    assert rt.run() == 0
    assert len(rt.posts) == 1


def test_private_probe_reads_data_with_valid_signature_after_healthy_readiness(runtime):
    rt = runtime
    alerts.private_read_probe(rt.origin)
    assert len(rt.read_requests) == 1
    assert rt.read_requests[0][0].startswith("/v1/players/search?")
    assert rt.read_requests[0][1].provider == ""
    rt.probe_status = 503
    with pytest.raises(OSError):
        alerts.private_read_probe(rt.origin)
    assert len(rt.read_requests) == 2


def test_command_boundary_missing_webhook_fails_without_printing_config(tmp_path):
    config = tmp_path / "app.env"
    config.write_text(
        f"CLASHLENS_API_KEY_HOST_DIR={tmp_path}\n"
        "UNRELATED_SECRET=must-not-appear-in-output\n"
    )
    result = subprocess.run(
        [str(ROOT / "ops"), "alert-check"],
        env=dict(
            os.environ, OPS_ENV_FILE=str(config), XDG_STATE_HOME=str(tmp_path / "state")
        ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "Webhook file" in result.stderr
    assert "must-not-appear" not in result.stdout + result.stderr


def test_network_failure_keeps_pending_and_other_conditions_still_deliver(
    runtime, monkeypatch, capsys
):
    rt = runtime
    trigger(rt, "tracker")
    trigger(rt, "reads")
    original = alerts.request

    def fail_first(url, **kwargs):
        if kwargs.get("payload") and "fetch" in kwargs["payload"]["content"]:
            raise OSError(FAKE_WEBHOOK)
        return original(url, **kwargs)

    monkeypatch.setattr(alerts, "request", fail_first)
    assert rt.run() == 1
    assert len(rt.posts) == 1
    assert FAKE_WEBHOOK not in capsys.readouterr().err
    monkeypatch.setattr(alerts, "request", original)
    assert rt.run() == 0
    assert len(rt.posts) == 2


def test_concurrent_check_does_not_duplicate_pending_delivery(runtime):
    rt = runtime
    trigger(rt, "reads")
    rt.state_dir.mkdir()
    with (rt.state_dir / "alerts.lock").open("w") as lock:
        alerts.fcntl.flock(lock, alerts.fcntl.LOCK_EX)
        with pytest.raises(alerts.CheckError):
            rt.run()
        assert not rt.posts
    assert rt.run() == 0
    assert len(rt.posts) == 1


def test_private_probe_detects_real_api_route_database_failure(runtime, monkeypatch):
    from contextlib import contextmanager
    from urllib.parse import urlsplit

    import psycopg
    from fastapi.testclient import TestClient

    from clashlens.api import create_app

    class Pool:
        failed = False
        timeout = 5.0
        max_size = 8

        @contextmanager
        def connection(self):
            yield self

        def execute(self, *_args):
            if self.failed:
                raise psycopg.OperationalError("synthetic data read failure")
            return self

        def fetchall(self):
            return []

    pool = Pool()
    database = SimpleNamespace(pool=pool, is_ready=lambda **_: True)
    app = create_app(
        database,
        keys={("typescript-website", "current"): b"a" * 32},
        clock=lambda: runtime.now,
    )
    client = TestClient(app, raise_server_exceptions=False)

    def request(url, *, headers=None, **_kwargs):
        parsed = urlsplit(url)
        target = parsed.path + ("?" + parsed.query if parsed.query else "")
        response = client.get(target, headers=headers)
        if response.status_code != 200:
            raise alerts.CheckError("Private data read failed")
        return response.content

    monkeypatch.setattr(alerts, "request", request)
    alerts.private_read_probe()
    pool.failed = True
    assert client.get("/readyz").status_code == 200
    with pytest.raises(alerts.CheckError):
        alerts.private_read_probe()
    client.close()


def test_timed_out_checks_do_not_leave_host_children_running(tmp_path):
    import sys
    import time

    orphan = tmp_path / "orphan-finished"
    child = (
        f"import pathlib,time; time.sleep(0.5); pathlib.Path({str(orphan)!r}).touch()"
    )
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
    with pytest.raises(subprocess.TimeoutExpired):
        alerts.command([sys.executable, "-c", parent], timeout=0.2)
    time.sleep(0.6)
    assert not orphan.exists()


def test_delivery_when_discord_rejects_default_python_client(runtime):
    runtime.reject_default_client = True
    trigger(runtime, "reads")
    assert runtime.run() == 0
    assert len(runtime.posts) == 1
