"""Host-side, standard-library-only private alerts for ./ops alert-check."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

# systemd's structured automatic-restart event, independent of log language.
RESTART_MESSAGE = "5eb03494b6584870a536b337290809b3"
CONDITIONS = {
    "tracker": (
        "No successful official API fetch for ten minutes outside the Reset pause",
        "./ops logs collector",
    ),
    "disk": (
        "Spool capacity or a data filesystem is above 80% full",
        "./ops queue-status",
    ),
    "restarts": (
        "A Clash Lens service restarted more than three times in one hour",
        "./ops logs",
    ),
    "backup": (
        "The backup check failed or found a stale backup",
        "./ops backup-status",
    ),
    "reads": ("A private player-data read failed", "./ops logs api"),
    "collection": (
        "A player check is more than ten minutes overdue, so collection is slow or stalled",
        "./ops logs collector",
    ),
    "leaderboard": (
        "More than 1% of Live Leaderboard players were last updated over ten minutes ago",
        "./ops queue-status",
    ),
}


class CheckError(Exception):
    """A fixed, secret-free diagnostic safe for the journal."""


def command(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    with subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # backup-status is a shell command; also stop its host-side children.
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(
    url: str, *, payload: dict | None = None, headers: dict | None = None
) -> bytes:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers or {})
    req.add_header(
        "User-Agent", "ClashLens-Alerts/1.0 (+https://github.com/zzzubair/clashlens)"
    )
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    # Do not send a secret URL or signed request through ambient proxies/redirects.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise CheckError("HTTP check failed")
        return response.read(1024 * 1024)


def private_read_probe(origin: str = "http://127.0.0.1:8000") -> None:
    """Run inside the API container; no keys or response data leave it."""
    from clashlens.hmac_proof import (
        AUDIENCE,
        PROOF_VERSION,
        SigningInput,
        load_secret_file,
        sign,
    )

    if json.loads(request(origin + "/readyz")).get("ready") is not True:
        raise CheckError("Private API is not ready")
    target = "/v1/players/search?q=clashlens-alert-read-check&limit=1"
    encode = lambda value: (
        base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()
    )
    now = int(time.time())
    value = SigningInput(
        PROOF_VERSION,
        encode(os.environ.get("CLASHLENS_HMAC_CALLER", "typescript-website")),
        encode(os.environ.get("CLASHLENS_HMAC_KEY_ID", "current")),
        AUDIENCE,
        "GET",
        encode(target),
        hashlib.sha256(b"").hexdigest(),
        str(now),
        str(now + 30),
        str(uuid4()),
        "",
        "",
    )
    key = load_secret_file(os.environ["CLASHLENS_HMAC_SECRET_FILE"])
    fields = {
        "proof-version": value.proof_version,
        "caller": value.caller_b64url,
        "key-id": value.key_id_b64url,
        "issued-at": value.issued_at,
        "expires-at": value.expires_at,
        "request-id": value.request_id,
        "provider": "",
        "provider-subject": "",
        "signature": sign(key, value),
    }
    result = json.loads(
        request(
            origin + target, headers={"X-ClashLens-" + k: v for k, v in fields.items()}
        )
    )
    if not isinstance(result.get("results"), list) or not isinstance(
        result.get("users"), list
    ):
        raise CheckError("Private API player read returned an invalid response")


def leaderboard_freshness_probe(now: datetime | None = None) -> None:
    """Run inside the API container; prints only two counts."""
    from clashlens import api_leaderboard
    from clashlens.api_db import ApiDatabase

    url = Path(os.environ["CLASHLENS_DATABASE_URL_FILE"]).read_text().strip()
    database = ApiDatabase(url, max_size=1)
    try:
        # The same query and Last updated rule the Live Leaderboard uses.
        board = api_leaderboard.get_live_leaderboard(
            database, limit=1, now=now or datetime.now(UTC), freshness_seconds=600
        )
    finally:
        database.close()
    if board is None:
        raise CheckError("Live Leaderboard is empty")
    print(board["source_observations"]["stale_count"], board["total_entries"])


def elapsed_without_reset(start: float, end: float) -> float:
    # More than a day is already far over the ten-minute threshold.
    start = max(start, end - 86400)
    elapsed = max(0, end - start)
    boundary = datetime.fromtimestamp(start, UTC).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    for offset in (0, 1):
        stop = (boundary + timedelta(days=offset)).timestamp()
        elapsed -= max(0, min(end, stop) - max(start, stop - 300))
    return elapsed


def read_webhook(path: Path) -> str:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
            ):
                raise ValueError
            url = handle.read(4096).strip()
        if not re.fullmatch(
            r"https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9._-]+(?:\?wait=true)?",
            url,
        ):
            raise ValueError
        return url
    except (OSError, ValueError):
        raise CheckError(
            "Webhook file is missing or invalid; use a service-owned mode-600 Discord webhook file"
        ) from None


def save_state(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".alerts-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def deliver(
    state: dict, findings: dict[str, bool | None], now: float, path: Path, webhook: str
) -> bool:
    failed = False
    incidents = state.setdefault("incidents", {})
    for name, active in findings.items():
        incident = incidents.setdefault(name, {"active": False})
        for _ in range(2):  # Retry saved transition, then report a subsequent recovery.
            if not incident.get("pending"):
                if active is None or active == incident["active"]:
                    break
                if active:
                    incident["since"] = (
                        max(
                            state.get("backup_failing_since", now),
                            state.get("resumed_at", 0),
                        )
                        if name == "backup"
                        else now
                    )
                incident["pending"] = {"active": active, "at": now}
                save_state(path, state)
            pending = incident["pending"]
            since = datetime.fromtimestamp(incident["since"], UTC).isoformat()
            at = datetime.fromtimestamp(pending["at"], UTC).isoformat()
            description, step = CONDITIONS[name]
            content = (
                f"Clash Lens alert: {description}. First observed {since}. Next step: {step}."
                if pending["active"]
                else f"Clash Lens recovered at {at}: {description}. Incident first observed {since}. Next step: {step}."
            )
            try:
                request(
                    webhook,
                    payload={"content": content, "allowed_mentions": {"parse": []}},
                )
            except (OSError, ValueError, CheckError):
                print(
                    "alert-check: Discord delivery failed; pending change will retry next run. Check journalctl --user -u clashlens-alert.service",
                    file=sys.stderr,
                )
                failed = True
                break
            incident["active"] = pending["active"]
            del incident["pending"]
            save_state(path, state)
    return not failed


def observe(
    config: dict, state: dict, now: float, root: Path
) -> tuple[dict, list[str]]:
    findings = dict.fromkeys(CONDITIONS)
    errors = []
    podman = os.environ.get("PODMAN_BIN", "podman")
    metrics = {}
    try:
        body = request(
            f"http://127.0.0.1:{int(config['health_port'])}/metrics"
        ).decode()
        for line in body.splitlines():
            parts = line.split()
            if len(parts) == 2 and "{" not in parts[0] and not parts[0].startswith("#"):
                value = float(parts[1])
                if not math.isfinite(value) or value < 0:
                    raise ValueError
                metrics[parts[0]] = value
        age = metrics.get("clashlens_collector_last_success_age_seconds")
        if age is not None:
            state["last_success"] = now - age
        elif "clashlens_collector_active_players" not in metrics:
            raise ValueError
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append(
            "Collector metrics unavailable; fetch-gap clock continues and spool usage is unknown"
        )
    last = max(state.setdefault("last_success", now), state.get("resumed_at", 0))
    findings["tracker"] = elapsed_without_reset(last, now) >= 600
    # The Reset sweep holds regular checks, so the half hour after Reset
    # neither raises nor clears the overdue-check alert.
    clock = datetime.fromtimestamp(now, UTC)
    overdue = metrics.get("clashlens_collector_oldest_due_age_seconds")
    if overdue is not None and not (clock.hour == 5 and clock.minute < 30):
        findings["collection"] = overdue >= 600

    names = (
        ("clashlens_spool_bytes", "max_bytes"),
        ("clashlens_spool_objects", "max_objects"),
    )
    disk_known = all(name in metrics for name, _ in names)
    disk_full = any(
        metrics.get(name, 0) > 0.8 * int(config[cap]) for name, cap in names
    )
    paths = [config["spool_root"]]
    try:
        volume = command(
            [
                podman,
                "volume",
                "inspect",
                "--format",
                "{{.Mountpoint}}",
                "clashlens-postgres-data",
            ]
        )
        if volume.returncode or not volume.stdout.strip():
            raise ValueError
        paths.append(volume.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        disk_known = False
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
            disk_full |= usage.used / usage.total > 0.8
        except OSError:
            disk_known = False
    findings["disk"] = True if disk_full else (False if disk_known else None)
    if not disk_known:
        errors.append(
            "Spool or data filesystem usage unavailable; disk alert cannot clear"
        )

    try:
        # No unit-name pattern here: journalctl matches it against every unit
        # in the whole journal, which took over 15 seconds on production.
        result = command(
            [
                os.environ.get("JOURNALCTL_BIN", "journalctl"),
                "--user",
                "--since",
                "1 hour ago",
                "--no-pager",
                "--output=json",
                "--output-fields=USER_UNIT",
                f"MESSAGE_ID={RESTART_MESSAGE}",
            ]
        )
        if result.returncode:
            raise ValueError
        counts: dict[str, int] = {}
        for line in result.stdout.splitlines():
            unit = json.loads(line).get("USER_UNIT", "")
            # Preview units share the prefix but are not production services.
            if re.fullmatch(r"clashlens-(?!preview-)[a-z0-9-]+\.service", unit):
                counts[unit] = counts.get(unit, 0) + 1
        findings["restarts"] = any(count > 3 for count in counts.values())
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append("Restart history unavailable; run ./ops logs")

    try:
        backup_failed = (
            command([str(root / "ops"), "backup-status"], 25).returncode != 0
        )
    except (OSError, subprocess.SubprocessError) as error:
        errors.append(
            "Backup check timed out after 25 seconds; run ./ops backup-status"
            if isinstance(error, subprocess.TimeoutExpired)
            else "Backup check could not run; run ./ops backup-status"
        )
        since = state.setdefault("backup_failing_since", now)
        failing = now - max(since, state.get("resumed_at", 0)) >= 900
        findings["backup"] = True if failing else None
    else:
        findings["backup"] = backup_failed
        if backup_failed:
            errors.append("Backup check failed; run ./ops backup-status")
        else:
            state.pop("backup_failing_since", None)

    probe = [
        podman,
        "exec",
        "clashlens-python-api",
        "python",
        "-m",
        "clashlens.alerts",
        "--probe",
    ]
    try:
        findings["reads"] = command(probe, 25).returncode != 0
    except (OSError, subprocess.SubprocessError):
        findings["reads"] = True
    try:
        result = command(
            [
                podman,
                "exec",
                "clashlens-python-api",
                "python",
                "-m",
                "clashlens.alerts",
                "--leaderboard",
            ],
            25,
        )
        stale, total = (int(value) for value in result.stdout.split())
        if result.returncode or total < 1:
            raise ValueError
        # A changed profile shows its previous time until the worker applies
        # it, so a few players are always briefly behind.
        findings["leaderboard"] = stale * 100 > total
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append("Live Leaderboard freshness unavailable; run ./ops logs api")
    return findings, errors


def run(config: dict, state_dir: Path, root: Path) -> int:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(
        os.open(
            state_dir / "alerts.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        ),
        "w",
    ) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CheckError("Another alert check is running") from None
        if (state_dir / "alert-intent").exists() and (
            state_dir / "alert-intent"
        ).read_text().strip() == "stopped":
            return 0
        webhook = read_webhook(Path(config["webhook_file"]))
        path = state_dir / "alerts.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        now = time.time()
        intent = state_dir / "alert-intent"
        if intent.exists():
            state["resumed_at"] = intent.stat().st_mtime
        findings, errors = observe(config, state, now, root)
        save_state(path, state)
        delivered = deliver(state, findings, now, path, webhook)
        for error in errors:
            print(f"alert-check: {error}", file=sys.stderr)
        return 0 if delivered and not errors else 1


def main() -> int:
    try:
        if sys.argv[1:] == ["--probe"]:
            private_read_probe()
            return 0
        if sys.argv[1:] == ["--leaderboard"]:
            leaderboard_freshness_probe()
            return 0
        state_dir, root, webhook, health, spool, max_bytes, max_objects = sys.argv[1:]
        return run(
            {
                "webhook_file": webhook,
                "health_port": health,
                "spool_root": spool,
                "max_bytes": max_bytes,
                "max_objects": max_objects,
            },
            Path(state_dir),
            Path(root),
        )
    except CheckError as error:
        print(f"alert-check: {error}", file=sys.stderr)
    except Exception:  # noqa: BLE001 - never disclose secrets in exception text.
        # Exception strings from HTTP, subprocesses or state may contain secrets.
        print(
            "alert-check: check failed; inspect configuration, state permissions and service journal",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
