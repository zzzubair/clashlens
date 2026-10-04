"""Host-side, standard-library-only private alerts for ./ops alert-check."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import http.client
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
        (
            "For five minutes, over 5% of Live Leaderboard players were last updated"
            " over ten minutes ago, or one player over 20 minutes ago"
        ),
        "./ops queue-status",
    ),
    "failures": (
        (
            "A new permanent failure of a processing job or raw-response upload in the last 24 hours"
            " (recovery means no new permanent failure for 24 hours, not that anything was repaired)"
        ),
        "./ops failed-items",
    ),
    "processing": (
        (
            "Daily result calculations have waited at least 15 minutes, or ordinary work"
            " excluding publication builds has waited at least 30 minutes"
        ),
        "./ops logs worker",
    ),
    "uploads": (
        "A raw response has waited over an hour to be uploaded to the archive",
        "./ops logs collector",
    ),
    "publication": (
        "A Reset's frozen leaderboard or army results are over an hour past their publication time",
        "./ops logs worker",
    ),
    "monitoring": (
        (
            "A disk, restart-history, Live Leaderboard or Reset publication check"
            " has been unreadable for at least 10 minutes"
        ),
        "journalctl --user -u clashlens-alert.service --since '30 minutes ago' --no-pager",
    ),
    # Checked from outside the server by --uptime, not by alert-check.
    "site": (
        "The Clash Lens website or its health check stopped answering, checked from outside the server",
        "ssh fedora, then ./ops status",
    ),
}


# Plain names for the worker's ordinary job types in processing alerts.
WORK_NAMES = {
    "process_observation": "saved API responses",
    "replay_observation": "replayed API responses",
    "reconcile_ranked_day": "daily result calculations",
    "redecode_army": "army re-decoding",
}

# Checks that would otherwise stay unknown, and so hide their own problem,
# without any warning. Other unreadable checks already have their own.
UNREADABLE = {
    "Spool or data filesystem usage unavailable; disk alert cannot clear",
    "Restart history unavailable; run ./ops logs",
    "Live Leaderboard freshness unavailable; run ./ops logs api",
    "Reset publication status unavailable; run ./ops logs api",
}

# A recovery is sent only after this long without the problem, so a problem
# that comes back sooner continues the same incident.
RECOVERY_HOLD = 900
# The Live Leaderboard alerts only when staleness is widespread or one player
# is badly behind, and stays so for LEADERBOARD_HOLD seconds of checks.
LEADERBOARD_STALE_SHARE = 0.05
LEADERBOARD_OLDEST = 1200
LEADERBOARD_HOLD = 300


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
    """Run inside the API container; prints two counts and the oldest age."""
    from clashlens import api_leaderboard
    from clashlens.api_db import ApiDatabase

    now = now or datetime.now(UTC)
    url = Path(os.environ["CLASHLENS_DATABASE_URL_FILE"]).read_text().strip()
    database = ApiDatabase(url, max_size=1)
    try:
        # The same query and Last updated rule the Live Leaderboard uses.
        board = api_leaderboard.get_live_leaderboard(database, limit=1, now=now)
    finally:
        database.close()
    sources = board["source_observations"]
    oldest = sources["oldest_observed_at"]
    age = 0 if oldest is None else (now - datetime.fromisoformat(oldest)).total_seconds()
    print(sources["stale_count"], board["total_entries"], max(0, int(age)))


def publication_probe() -> None:
    """Run inside the API container; prints how many Resets are unpublished.

    A Reset counts when no generation of it has published both its frozen
    leaderboard and its army results an hour after its target time, or when
    a Reset since the first one has no generation at all 70 minutes after it.
    """
    from clashlens.api_db import ApiDatabase

    url = Path(os.environ["CLASHLENS_DATABASE_URL_FILE"]).read_text().strip()
    database = ApiDatabase(url, max_size=1)
    try:
        print(
            database.scalar(
                """
                WITH resets AS (
                    SELECT boundary_at, min(target_at) AS target_at,
                           bool_or(snapshot_state IN ('published', 'superseded'))
                           AND bool_or(army_state IN ('published', 'superseded')) AS done
                    FROM boundary_publication_generations GROUP BY boundary_at
                ), expected AS (
                    SELECT generate_series(
                        (SELECT min(boundary_at) FROM resets),
                        clock_timestamp() - interval '70 minutes', interval '24 hours'
                    ) AS boundary_at
                )
                SELECT (SELECT count(*) FROM resets WHERE NOT done
                          AND target_at < clock_timestamp() - interval '1 hour')
                     + (SELECT count(*) FROM expected
                        WHERE boundary_at NOT IN (SELECT boundary_at FROM resets))
                """
            )
        )
    finally:
        database.close()


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


def hold_recoveries(state: dict, findings: dict[str, bool | None], now: float) -> None:
    """Keep an alerted incident open until RECOVERY_HOLD seconds of clear checks
    after its alert was delivered."""
    for name, active in findings.items():
        incident = state.setdefault("incidents", {}).setdefault(name, {"active": False})
        if incident.get("clear_since", now) < state.get("resumed_at", 0):
            del incident["clear_since"]  # Stopped time is not clear time.
        if active is False and incident.get("pending", incident)["active"]:
            start = max(
                incident.setdefault("clear_since", now), incident.get("alerted_at", 0)
            )
            if incident.get("pending") or now - start < RECOVERY_HOLD:
                findings[name] = None
        else:
            incident.pop("clear_since", None)


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
                # A recovery reports when the problem first cleared.
                at = now if active else incident.pop("clear_since", now)
                incident["pending"] = {"active": active, "at": at}
                detail = state.get("details", {}).get(name)
                if active and detail:
                    incident["pending"]["detail"] = detail
                save_state(path, state)
            pending = incident["pending"]
            since = datetime.fromtimestamp(incident["since"], UTC).isoformat()
            at = datetime.fromtimestamp(pending["at"], UTC).isoformat()
            description, step = CONDITIONS[name]
            if pending.get("detail"):
                description += f". {pending['detail']}"
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
            if pending["active"]:
                incident["alerted_at"] = time.time()
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
    metrics_read = False
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
        metrics_read = "clashlens_collector_pending_processing" in metrics
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append(
            "Collector metrics unavailable; fetch-gap clock continues and spool usage is unknown"
        )
    last = max(state.setdefault("last_success", now), state.get("resumed_at", 0))
    findings["tracker"] = elapsed_without_reset(last, now) >= 600
    overdue = metrics.get("clashlens_collector_oldest_due_age_seconds")
    reset_total = metrics.get("clashlens_collector_reset_total")
    reset_terminal = metrics.get("clashlens_collector_reset_terminal")
    if overdue is not None and reset_total is not None and reset_terminal == reset_total:
        findings["collection"] = overdue >= 600
    # The Reset pause and sweep leave most players stale for a while.
    clock = datetime.fromtimestamp(now, UTC).strftime("%H:%M")
    resetting = (
        "04:55" <= clock < "05:00" or reset_total is None or reset_terminal != reset_total
    )
    if metrics_read:
        prefix = "clashlens_collector_"
        recent = []
        for kind, count in (("processing", "processing"), ("upload", "uploads")):
            age = metrics.get(f"{prefix}newest_failed_{kind}_age_seconds")
            if age is None and metrics.get(f"{prefix}failed_{count}") != 0:
                recent.append(None)
            else:
                recent.append(age is not None and age < 86400)
        findings["failures"] = (
            True if True in recent else None if None in recent else False
        )
        for name, kind, limit in (
            ("processing", "processing", 1800),
            ("uploads", "upload", 3600),
        ):
            age = metrics.get(f"{prefix}oldest_pending_{kind}_age_seconds")
            findings[name] = None if age is None else age >= limit
        # Daily result calculations are ordinary work with a shorter limit.
        if metrics.get(f"{prefix}oldest_job_reconcile_ranked_day_age_seconds", 0) >= 900:
            findings["processing"] = True
        # Name the oldest ordinary job type so the alert says which work is behind.
        age, work = max(
            (
                (age, name.removeprefix(f"{prefix}oldest_job_").removesuffix("_age_seconds"))
                for name, age in metrics.items()
                if name.startswith(f"{prefix}oldest_job_")
                and not name.startswith(f"{prefix}oldest_job_build_")
            ),
            default=(0, None),
        )
        state["details"] = {}
        if work:
            state["details"]["processing"] = (
                f"Oldest waiting: {WORK_NAMES.get(work, work)}, {int(age // 60)} minutes"
            )

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
    for name, flag, count, unavailable in (
        ("leaderboard", "--leaderboard", 3, "Live Leaderboard freshness"),
        ("publication", "--publication", 1, "Reset publication status"),
    ):
        try:
            result = command(probe[:-1] + [flag], 25)
            values = [int(value) for value in result.stdout.split()]
            if result.returncode or len(values) != count:
                raise ValueError
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append(f"{unavailable} unavailable; run ./ops logs api")
            values = None
        if name == "publication":
            if values is not None:
                findings[name] = values[0] > 0
        elif values is None or resetting:
            state.pop("leaderboard_stale_since", None)
        elif values[2] > LEADERBOARD_OLDEST or values[0] > LEADERBOARD_STALE_SHARE * values[1]:
            since = max(
                state.setdefault("leaderboard_stale_since", now),
                state.get("resumed_at", 0),
            )
            findings[name] = True if now - since >= LEADERBOARD_HOLD else None
        else:
            state.pop("leaderboard_stale_since", None)
            findings[name] = False
    # Each unreadable check keeps its own first-failure time; one readable
    # run restarts its ten minutes.
    failing = state.get("monitoring_failing_since", {})
    failing = {error: failing.get(error, now) for error in errors if error in UNREADABLE}
    state["monitoring_failing_since"] = failing
    if failing:
        since = max(min(failing.values()), state.get("resumed_at", 0))
        findings["monitoring"] = True if now - since >= 600 else None
    else:
        findings["monitoring"] = False
    findings.pop("site")
    return findings, errors


def observe_site(
    config: dict, state: dict, now: float, _root: Path | None
) -> tuple[dict, list[str]]:
    """Alert once the site has failed every check for two minutes."""
    try:
        for url in config["urls"]:
            request(url)
    except (OSError, ValueError, CheckError, http.client.HTTPException):
        since = state.setdefault("site_failing_since", now)
        return {"site": True if now - since >= 120 else None}, [
            "Website or health check did not answer"
        ]
    state.pop("site_failing_since", None)
    return {"site": False}, []


def run(config: dict, state_dir: Path, root: Path | None, check=observe) -> int:
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
        findings, errors = check(config, state, now, root)
        hold_recoveries(state, findings, now)
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
        if sys.argv[1:] == ["--publication"]:
            publication_probe()
            return 0
        if sys.argv[1:2] == ["--uptime"] and len(sys.argv) > 4:
            state_dir, webhook, *urls = sys.argv[2:]
            return run(
                {"webhook_file": webhook, "urls": urls},
                Path(state_dir),
                None,
                observe_site,
            )
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
