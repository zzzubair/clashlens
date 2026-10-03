"""Observable alert delivery against a local HTTP server, never Discord."""

import base64
import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from clashlens import alerts
from clashlens.hmac_proof import verify_proof

ROOT = Path(__file__).resolve().parents[2]
FAKE_WEBHOOK = "https://discord.com/api/webhooks/123/fake-secret-never-real"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    rt = SimpleNamespace(
        now=datetime(2026, 9, 27, 12, tzinfo=UTC).timestamp(),
        metrics={
            "clashlens_collector_last_success_age_seconds": 1,
            "clashlens_collector_active_players": 200,
            "clashlens_spool_bytes": 0,
            "clashlens_spool_objects": 0,
            "clashlens_collector_oldest_due_age_seconds": 120,
            "clashlens_collector_reset_total": 0,
            "clashlens_collector_reset_terminal": 0,
            "clashlens_collector_pending_processing": 0,
            "clashlens_collector_failed_processing": 0,
            "clashlens_collector_failed_uploads": 0,
            "clashlens_collector_oldest_pending_processing_age_seconds": 0,
            "clashlens_collector_oldest_pending_upload_age_seconds": 0,
        },
        posts=[],
        attempts=[],
        post_status=204,
        reject_default_client=False,
        metrics_status=200,
        restarts=[],
        journal_failed=False,
        backup_failed=False,
        backup_error=None,
        reads_failed=False,
        leaderboard="0 13000 0",
        publication="0",
        site_status=200,
        disk_used=10,
        volume_failed=False,
        read_requests=[],
        probe_status=200,
    )
    key = b"a" * 32
    secret = tmp_path / "hmac"
    secret.write_bytes(base64.urlsafe_b64encode(key).rstrip(b"="))
    monkeypatch.setenv("CLASHLENS_HMAC_SECRET_FILE", str(secret))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            rt.attempts.append(body)
            status = rt.post_status
            if rt.reject_default_client and self.headers.get(
                "User-Agent", ""
            ).startswith("Python-urllib/"):
                status = 403
            if 200 <= status < 300:
                rt.posts.append(body)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/redirected")
            self.end_headers()

        def do_GET(self):
            if self.path == "/metrics":
                body = "\n".join(f"{k} {v}" for k, v in rt.metrics.items()).encode()
                status = rt.metrics_status
            elif self.path == "/readyz":
                body, status = b'{"ready":true}', 200
            elif self.path == "/healthz":
                body, status = b'{"status":"ok"}', rt.site_status
            else:
                proof = verify_proof(
                    headers=[
                        (k.lower().encode(), v.encode())
                        for k, v in self.headers.items()
                    ],
                    method="GET",
                    raw_target=self.path.encode(),
                    body=b"",
                    keys={("typescript-website", "current"): key},
                    now=int(rt.now),
                )
                rt.read_requests.append((self.path, proof))
                body, status = b'{"users":[],"results":[]}', rt.probe_status
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # A short poll interval lets shutdown() return in milliseconds instead of
    # the 0.5-second default; requests still go through this real server.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    rt.origin = f"http://127.0.0.1:{server.server_port}"
    real_request = alerts.request

    def local_request(url, **kwargs):
        # A secret-shaped dummy file exercises production validation without
        # providing any path to a real webhook in this test process.
        if url == FAKE_WEBHOOK:
            url = rt.origin + "/discord"
        assert url.startswith(rt.origin + "/"), "Test attempted non-local HTTP"
        return real_request(url, **kwargs)

    monkeypatch.setattr(alerts, "request", local_request)
    monkeypatch.setattr(alerts.time, "time", lambda: rt.now)
    # Each condition's own rule is tested without the holds; the holds have
    # their own tests below.
    rt.holds = {
        "RECOVERY_HOLD": alerts.RECOVERY_HOLD,
        "LEADERBOARD_HOLD": alerts.LEADERBOARD_HOLD,
    }
    monkeypatch.setattr(alerts, "RECOVERY_HOLD", 0)
    monkeypatch.setattr(alerts, "LEADERBOARD_HOLD", 0)
    monkeypatch.setattr(
        alerts.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(used=rt.disk_used, total=100),
    )

    def command(args, timeout=15):
        if "volume" in args:
            code, output = int(rt.volume_failed), str(tmp_path)
        elif "backup-status" in args:
            if rt.backup_error:
                raise rt.backup_error
            code, output = int(rt.backup_failed), "private backup output"
        elif "--probe" in args:
            code, output = int(rt.reads_failed), "private account output"
        elif "--leaderboard" in args:
            code, output = 0, rt.leaderboard
        elif "--publication" in args:
            code, output = 0, rt.publication
        else:
            assert f"MESSAGE_ID={alerts.RESTART_MESSAGE}" in args
            code, output = (
                int(rt.journal_failed),
                "\n".join(json.dumps({"USER_UNIT": u}) for u in rt.restarts),
            )
        return subprocess.CompletedProcess(args, code, output, "private failure detail")

    monkeypatch.setattr(alerts, "command", command)
    webhook = tmp_path / "webhook"
    webhook.write_text(FAKE_WEBHOOK)
    webhook.chmod(0o600)
    rt.config = {
        "webhook_file": str(webhook),
        "health_port": server.server_port,
        "spool_root": str(tmp_path),
        "max_bytes": 100,
        "max_objects": 100,
    }
    rt.state_dir = tmp_path / "state"
    rt.run = lambda: alerts.run(rt.config, rt.state_dir, ROOT)
    try:
        yield rt
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def trigger(rt, condition, value=True):
    if condition == "tracker":
        rt.metrics["clashlens_collector_last_success_age_seconds"] = 601 if value else 1
    elif condition == "spool_bytes":
        rt.metrics["clashlens_spool_bytes"] = 81 if value else 0
    elif condition == "spool_objects":
        rt.metrics["clashlens_spool_objects"] = 81 if value else 0
    elif condition == "filesystem":
        rt.disk_used = 81 if value else 10
    elif condition == "restarts":
        rt.restarts = ["clashlens-worker.service"] * (4 if value else 0)
    elif condition == "backup":
        rt.backup_failed = value
    elif condition == "reads":
        rt.reads_failed = value
    elif condition == "collection":
        rt.metrics["clashlens_collector_oldest_due_age_seconds"] = 600 if value else 599
    elif condition == "leaderboard":
        rt.leaderboard = "1 13000 1801" if value else "0 13000 0"
    elif condition == "failures":
        rt.metrics["clashlens_collector_newest_failed_upload_age_seconds"] = (
            86399 if value else 86400
        )
    elif condition in ("processing", "upload"):
        name = f"clashlens_collector_oldest_pending_{condition}_age_seconds"
        rt.metrics[name] = 3600 if value else 3599
    elif condition == "publication":
        rt.publication = "1" if value else "0"


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
    assert rt.posts[0] == first
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

    # Exactly 1% stale, or one player exactly 30 minutes old, is tolerated.
    minutes(10, "130 13000 1800")
    # Over the line for four minutes, then back: no alert and no recovery.
    minutes(4, "131 13000 900")
    minutes(1, "0 13000 300")
    minutes(4, "1 13000 1801")
    minutes(1, "0 13000 300")
    # During Reset work only the oldest player's age counts.
    rt.metrics["clashlens_collector_reset_total"] = 200
    rt.metrics["clashlens_collector_reset_terminal"] = 199
    minutes(10, "9000 13000 1500")
    assert not rt.posts
    minutes(5, "9000 13000 1801")
    assert not rt.posts
    minutes(1, "9000 13000 1801")
    assert len(rt.posts) == 1
    assert "Live Leaderboard" in rt.posts[0]["content"]


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
    rt.leaderboard = "0 13000 1801"
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


def test_missing_failure_age_is_unknown_unless_nothing_has_failed(runtime):
    rt = runtime
    trigger(rt, "failures")
    assert rt.run() == 0
    assert len(rt.posts) == 1
    # An older collector reports failed counts but no newest-failure ages.
    del rt.metrics["clashlens_collector_newest_failed_upload_age_seconds"]
    rt.metrics["clashlens_collector_failed_processing"] = 1
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.metrics["clashlens_collector_failed_processing"] = 0
    del rt.metrics["clashlens_collector_failed_uploads"]
    assert rt.run() == 0
    assert len(rt.posts) == 1
    rt.metrics["clashlens_collector_failed_uploads"] = 0
    assert rt.run() == 0
    assert len(rt.posts) == 2
    assert "recovered" in rt.posts[-1]["content"]


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
    with domain_database(database_url, include_coordinator=True) as connection_info:
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


def test_failed_or_pending_checks_keep_the_confirmed_profile_time(
    runtime, database_url, archive_server, tmp_path, monkeypatch, capsys
):
    import hashlib
    from datetime import timedelta

    import psycopg
    from domain_test_support import as_api_role, domain_database, store_observation
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from test_domain_processing_postgres import PROFILE_FIXTURE, _processor

    from clashlens import api_leaderboard, api_players
    from clashlens.api_db import ApiDatabase
    from clashlens.collector_db import CollectorDatabase, ResponseHandoff
    from clashlens.response_fields import content_fingerprint

    accepted_at = datetime(2026, 8, 6, 6, tzinfo=UTC)
    confirmed_at = accepted_at + timedelta(hours=2)
    body = PROFILE_FIXTURE.read_bytes()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _, job = store_observation(
            connection_info, archive_server, occurrence_key="confirmed-initial",
            endpoint="profile", body=body, observed_at=accepted_at,
            normalized_tag="#2PP",
            parser_version="supercell-profile-parser-v3",
        )
        options = conninfo_to_dict(connection_info)["options"]
        database, processor = _processor(
            make_conninfo(connection_info, options=options + " -c role=clashlens_python_worker"),
            archive_server,
        )
        collector = CollectorDatabase(make_conninfo(
            connection_info, options=options + " -c role=clashlens_collector"
        ))
        api = ApiDatabase(as_api_role(connection_info))
        url_file = tmp_path / "database-url"
        url_file.write_text(as_api_role(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))

        def check(payload, at, occurrence, status=200):
            digest = hashlib.sha256(payload).hexdigest()
            with psycopg.connect(connection_info) as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            return collector.record_response(ResponseHandoff(
                occurrence_key=occurrence, scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=at - timedelta(seconds=1), response_completed_at=at,
                http_status=status, response_hash=digest,
                content_fingerprint=content_fingerprint(
                    "profile", payload, http_status=status, response_hash=digest
                ),
                byte_size=len(payload), spool_key=f"sha256/{digest[:2]}/{digest}",
                collector_version="confirmation-test", key_label="regular-a",
                evidence_headers={"content-type": "application/json"},
            ))

        def assert_time(now, expected, trophies):
            page = api_players.get_player_page(api, "#2PP", now=now, freshness_seconds=900)
            board = api_leaderboard.get_live_leaderboard(api, limit=1, now=now)
            [entry] = board["entries"]
            assert page["observed_at"] == entry["observed_at"] == expected.isoformat()
            assert page["trophies"] == entry["trophies"] == trophies
            assert page["age_seconds"] == entry["age_seconds"] == int((now - expected).total_seconds())
            assert board["source_observations"]["stale_count"] == 0
            capsys.readouterr()
            alerts.leaderboard_freshness_probe(now)
            runtime.leaderboard = capsys.readouterr().out.strip()
            runtime.now = now.timestamp()
            assert runtime.run() == 0
            assert not runtime.posts

        try:
            capsys.readouterr()
            alerts.leaderboard_freshness_probe(accepted_at)
            runtime.leaderboard = capsys.readouterr().out.strip()
            assert runtime.leaderboard == "0 0 0"
            runtime.now = accepted_at.timestamp()
            assert runtime.run() == 0
            assert not runtime.posts
            assert processor.process_job(job, owner="confirmed-initial") is not None
            trophies = json.loads(body)["trophies"]
            check(body, accepted_at, "confirmed-initial")
            assert check(body, confirmed_at, "confirmed-unchanged").changed is False
            check(b'{"reason":"inMaintenance"}', confirmed_at + timedelta(minutes=5),
                  "confirmed-failed", status=503)
            assert_time(confirmed_at + timedelta(minutes=6), confirmed_at, trophies)
            older = json.loads(body)
            older["trophies"] += 50
            older_body = json.dumps(older).encode()
            older_at = confirmed_at - timedelta(hours=1)
            _, older_job = store_observation(
                connection_info, archive_server, occurrence_key="confirmed-older",
                endpoint="profile", body=older_body, observed_at=older_at,
                normalized_tag="#2PP",
                parser_version="supercell-profile-parser-v3",
            )
            check(older_body, older_at, "confirmed-older")
            assert processor.process_job(older_job, owner="confirmed-older") is not None
            assert_time(confirmed_at + timedelta(minutes=6), confirmed_at, trophies)
            changed = json.loads(body)
            changed["trophies"] += 30
            changed_body = json.dumps(changed).encode()
            changed_at = confirmed_at + timedelta(minutes=7)
            _, changed_job = store_observation(
                connection_info, archive_server, occurrence_key="confirmed-changed",
                endpoint="profile", body=changed_body, observed_at=changed_at,
                normalized_tag="#2PP",
                parser_version="supercell-profile-parser-v3",
            )
            check(changed_body, changed_at, "confirmed-changed")
            assert_time(changed_at + timedelta(minutes=1), confirmed_at, trophies)
            assert processor.process_job(changed_job, owner="confirmed-changed") is not None
            assert_time(changed_at + timedelta(minutes=1), changed_at, trophies + 30)
            check(changed_body, changed_at + timedelta(minutes=2), "confirmed-new-unchanged")
            assert_time(changed_at + timedelta(minutes=3), changed_at + timedelta(minutes=2), trophies + 30)
        finally:
            api.close()
            collector.close()
            database.close()


@pytest.mark.parametrize("later_status", [None, 503])
@pytest.mark.parametrize("upgrade", [False, True])
def test_not_found_player_leaves_the_leaderboard_and_alert_until_found_again(
    runtime, database_url, archive_server, tmp_path, monkeypatch, capsys,
    later_status, upgrade,
):
    import hashlib
    from datetime import timedelta

    import psycopg
    from domain_test_support import as_api_role, domain_database, store_observation
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from test_domain_processing_postgres import PROFILE_FIXTURE, _processor

    from clashlens import api_leaderboard
    from clashlens.api_db import ApiDatabase
    from clashlens.collector_db import (
        CollectorDatabase,
        ResponseHandoff,
        TransportFailure,
    )
    from clashlens.response_fields import content_fingerprint

    accepted_at = datetime(2026, 8, 6, 6, tzinfo=UTC)
    body = PROFILE_FIXTURE.read_bytes()
    not_found = b'{"reason":"notFound","message":"Not found"}'
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _, job = store_observation(
            connection_info, archive_server, occurrence_key="nf-initial",
            endpoint="profile", body=body, observed_at=accepted_at,
            normalized_tag="#2PP", parser_version="supercell-profile-parser-v3",
        )
        options = conninfo_to_dict(connection_info)["options"]
        database, processor = _processor(
            make_conninfo(connection_info, options=options + " -c role=clashlens_python_worker"),
            archive_server,
        )
        collector = CollectorDatabase(make_conninfo(
            connection_info, options=options + " -c role=clashlens_collector"
        ))
        api = ApiDatabase(as_api_role(connection_info))
        url_file = tmp_path / "database-url"
        url_file.write_text(as_api_role(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]

        def check(payload, at, occurrence, status=200):
            digest = hashlib.sha256(payload).hexdigest()
            return collector.record_response(ResponseHandoff(
                occurrence_key=occurrence, scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=at - timedelta(seconds=1), response_completed_at=at,
                http_status=status, response_hash=digest,
                content_fingerprint=content_fingerprint(
                    "profile", payload, http_status=status, response_hash=digest
                ),
                byte_size=len(payload), spool_key=f"sha256/{digest[:2]}/{digest}",
                collector_version="not-found-test", key_label="regular-a",
                evidence_headers={"content-type": "application/json"},
            ))

        def alert_at(now):
            board = api_leaderboard.get_live_leaderboard(api, limit=1, now=now)
            capsys.readouterr()
            alerts.leaderboard_freshness_probe(now)
            runtime.leaderboard = capsys.readouterr().out.strip()
            runtime.now = now.timestamp()
            assert runtime.run() == 0
            return board, runtime.leaderboard

        try:
            assert processor.process_job(job, owner="nf-initial") is not None
            check(body, accepted_at, "nf-initial")
            # A server error and a timeout leave the player listed and stale.
            check(b'{"reason":"inMaintenance"}', accepted_at + timedelta(minutes=5),
                  "nf-failed", status=503)
            collector.record_transport_failure(TransportFailure(
                occurrence_key="nf-timeout", scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=accepted_at + timedelta(minutes=10),
                failed_at=accepted_at + timedelta(minutes=10, seconds=30),
                failure_category="timeout", retry_state="next_pass",
                key_label="regular-a",
            ))
            board, counts = alert_at(accepted_at + timedelta(hours=3))
            assert [entry["tag"] for entry in board["entries"]] == ["#2PP"]
            assert counts == "1 1 10800"
            assert len(runtime.posts) == 1
            # Not found, then the same answer again: hidden, and the alert clears.
            check(not_found, accepted_at + timedelta(hours=3), "nf-404", status=404)
            assert check(
                not_found, accepted_at + timedelta(hours=3, minutes=5), "nf-404-again",
                status=404,
            ).changed is False
            board, counts = alert_at(accepted_at + timedelta(hours=3, minutes=6))
            assert board["entries"] == [] and board["total_entries"] == 0
            assert board["tracked_population"] == 1
            assert counts == "0 0 0"
            assert len(runtime.posts) == 2
            assert "recovered" in runtime.posts[-1]["content"]
            if later_status is not None:
                check(b'{"reason":"inMaintenance"}',
                      accepted_at + timedelta(hours=3, minutes=7),
                      "nf-still-failed", status=later_status)
            collector.record_transport_failure(TransportFailure(
                occurrence_key="nf-still-timeout", scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=accepted_at + timedelta(hours=3, minutes=8),
                failed_at=accepted_at + timedelta(hours=3, minutes=8, seconds=30),
                failure_category="timeout", retry_state="next_pass",
                key_label="regular-a",
            ))
            if upgrade:
                with psycopg.connect(connection_info) as connection:
                    connection.execute(
                        "ALTER TABLE collector_response_state DROP COLUMN last_not_found_at"
                    )
                    connection.commit()
                    connection.execute(
                        (ROOT / "deploy/migrations/0043_api_profile_not_found_read.sql").read_text()
                    )
            board, counts = alert_at(accepted_at + timedelta(hours=3, minutes=9))
            assert board["entries"] == [] and board["total_entries"] == 0
            assert board["tracked_population"] == 1
            assert counts == "0 0 0"
            assert len(runtime.posts) == 2
            # The next successful check brings the player straight back, fresh.
            found_at = accepted_at + timedelta(hours=3, minutes=10)
            check(body, found_at, "nf-found")
            board, counts = alert_at(found_at + timedelta(minutes=1))
            [entry] = board["entries"]
            assert entry["tag"] == "#2PP"
            assert entry["observed_at"] == found_at.isoformat()
            assert counts == "0 1 60"
            assert len(runtime.posts) == 2
        finally:
            api.close()
            collector.close()
            database.close()


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
    assert len(rt.posts) == 1


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
    assert len(rt.posts) == 2
    assert rt.posts[0] == first
    assert f"Incident first observed {started}." in rt.posts[1]["content"]
