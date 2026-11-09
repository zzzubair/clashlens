"""Shared setup for alerts tests."""

import base64
import json
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
        served=None,
        reset=None,
        database="healthy 0",
        completeness="0",
        battle_day="0",
        reconcile="1 0 0 0 2 2 2 0 1 3",
        health_streaks={},
        site_status=200,
        disk_used=10,
        volume_failed=False,
        read_requests=[],
        probe_status=200,
        website_status=200,
        board_page=None,
        calls=[],
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
            elif self.path == "/leaderboards/tracked?view=daily&page=1":
                # The public page shows the newest board it accepted.
                page = rt.board_page
                if page is None:
                    page = board_page(rt.served or reset_at(rt.now))
                body, status = page.encode(), rt.website_status
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
    # The nightly re-check's daily line has its own test in test_alerts_slim.
    monkeypatch.setattr(alerts, "RECONCILE_LINE_AT", "24:00")
    # The early warning's overdue-work limits sit under the processing alert's.
    # Other alerts' tests leave them out; the warning's own tests restore them.
    rt.warning_limits = {
        name: getattr(alerts, name)
        for name in ("WARNING_OVERDUE", "WARNING_RESET_OVERDUE", "WARNING_UPLOAD_WAIT")
    }
    for name in rt.warning_limits:
        monkeypatch.setattr(alerts, name, float("inf"))
    monkeypatch.setattr(
        alerts.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(used=rt.disk_used, total=100),
    )

    def command(args, timeout=15):
        rt.calls.append(args)
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
        elif "--reset" in args:
            # A healthy Reset: all 13,000 collected, frozen 05:10, readable 05:15.
            at = reset_at(rt.now)
            code, output = 0, rt.reset or f"{at} 13000 13000 {at + 600} {at + 900}"
        elif args[-1] == "clashlens-postgres":
            code, output = 0, rt.database
        elif "--completeness" in args:
            code, output = 0, rt.completeness
        elif "--battle-day" in args:
            code, output = 0, rt.battle_day
        elif "--reconcile-pass" in args:
            code, output = 0, rt.reconcile
        elif "inspect" in args:
            code, output = 0, str(rt.health_streaks.get(args[-1], 0))
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
        "public_origin": f"http://127.0.0.1:{server.server_port}",
    }
    rt.state_dir = tmp_path / "state"
    rt.run = lambda: alerts.run(rt.config, rt.state_dir, ROOT)
    try:
        yield rt
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def reset_at(now: float) -> int:
    """The latest 05:00 UTC Reset at or before ``now``."""
    return int(now - (now - 5 * 3600) % 86400)


def board_page(reset: int) -> str:
    """The Daily board's heading as the website renders it since the Oct 2026
    redesign, the board shown at the address visitors use rather than a
    redirect to it."""
    shown = datetime.fromtimestamp(reset, UTC)
    return (
        '<h1 id="leaderboard-title">At Reset</h1><p class="rankings-date">Day <!-- -->7'
        '<!-- --> · <time class="local-time board-reset"'
        f' dateTime="{shown:%Y-%m-%dT%H:%M:%SZ}"'
        f' title="{shown:%-d %b %Y, %H:%M} UTC">{shown:%-d %b %Y, %H:%M} UTC</time></p>'
    )


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
        rt.leaderboard = "1 13000 1201" if value else "0 13000 0"
    elif condition == "failures":
        rt.metrics["clashlens_collector_newest_failed_upload_age_seconds"] = (
            86399 if value else 86400
        )
    elif condition in ("processing", "upload"):
        name = f"clashlens_collector_oldest_pending_{condition}_age_seconds"
        limit = 1800 if condition == "processing" else 900
        rt.metrics[name] = limit if value else limit - 1
    elif condition == "publication":
        rt.publication = "1" if value else "0"
    elif condition == "completeness":
        rt.completeness = "11" if value else "10"
    elif condition == "battle_day":
        rt.battle_day = "1" if value else "0"
    if condition in ("completeness", "battle_day"):
        rt.now += alerts.SLOW_PROBE_INTERVAL  # Read at its next quarter-hour run.

