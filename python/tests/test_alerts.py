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
        },
        posts=[],
        attempts=[],
        post_status=204,
        reject_default_client=False,
        metrics_status=200,
        restarts=[],
        journal_failed=False,
        backup_failed=False,
        reads_failed=False,
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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
    monkeypatch.setattr(
        alerts.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(used=rt.disk_used, total=100),
    )

    def command(args, timeout=15):
        if "volume" in args:
            code, output = int(rt.volume_failed), str(tmp_path)
        elif "backup-status" in args:
            code, output = int(rt.backup_failed), "private backup output"
        elif "--probe" in args:
            code, output = int(rt.reads_failed), "private account output"
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


@pytest.mark.parametrize(
    "condition",
    [
        "tracker",
        "spool_bytes",
        "spool_objects",
        "filesystem",
        "restarts",
        "backup",
        "reads",
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
    trigger(rt, "backup")
    rt.post_status = status
    assert rt.run() == 1
    assert not rt.posts
    first = rt.attempts[0]
    rt.now += 60
    trigger(rt, "backup", False)
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
    trigger(rt, "backup")
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
    trigger(rt, "backup")
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
    trigger(runtime, "backup")
    assert runtime.run() == 0
    assert len(runtime.posts) == 1
