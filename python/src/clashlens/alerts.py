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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
    "failed_work": (
        (
            "Failed processing jobs or raw-response uploads are waiting for a person;"
            " they stay failed until retried or replayed"
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
        "A raw response has waited over 15 minutes to be uploaded to the archive",
        "./ops logs collector",
    ),
    "publication": (
        (
            "The latest Reset's frozen leaderboard was not readable by 05:30 UTC, or an"
            " earlier Reset's board or army results are over an hour past their publication time"
        ),
        "./ops logs worker",
    ),
    "reset": (
        "Early warning: the Reset is behind its 05:30 UTC board target",
        "./ops queue-status, then ./ops logs worker",
    ),
    "completeness": (
        (
            "Over 10 players who battled in Legend I today or yesterday are still"
            " untracked an hour after their first battle was saved"
        ),
        "./ops logs worker",
    ),
    "health": (
        "Early warning: a container may soon be restarted by its health check",
        "./ops status, then ./ops logs",
    ),
    "warning": (
        "Early warning: work is falling behind",
        "./ops status, then ./ops logs",
    ),
    "monitoring": (
        (
            "A disk, restart-history, Live Leaderboard, Reset publication, Reset progress or"
            " untracked battler check has been unreadable for at least 10 minutes"
        ),
        "journalctl --user -u clashlens-alert.service --since '30 minutes ago' --no-pager",
    ),
    "deploy": (
        "A deploy failed and left Clash Lens stopped",
        "./ops status and ./ops logs, then ./ops up once fixed",
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
    "Reset publication status unavailable; run ./ops logs website",
    "Reset progress unavailable; run ./ops logs worker",
    "Untracked Legend I battler count unavailable; run ./ops logs worker",
}

# A recovery is sent only after this long without the problem, so a problem
# that comes back sooner continues the same incident.
RECOVERY_HOLD = 900
# The Live Leaderboard alerts only when staleness is widespread or one player
# is badly behind, and stays so for LEADERBOARD_HOLD seconds of checks.
LEADERBOARD_STALE_SHARE = 0.05
LEADERBOARD_OLDEST = 1200
LEADERBOARD_HOLD = 300
# Untracked recent Legend I battlers tolerated before the completeness alert.
UNTRACKED_BATTLER_LIMIT = 10
# The early warning. Podman kills a container after six failed health checks
# in a row, about three minutes, so two failures leave time to act. Overdue
# work normally reaches 35 minutes after a Reset (6 Oct 2026), hence the
# higher limit 05:00-07:00 UTC. A normal Reset hour saves 420-2,700
# responses a minute; 7 Oct's stall saved 17-44.
WARNING_HEALTH_STREAK = 2
WARNING_OVERDUE = 600
WARNING_RESET_OVERDUE = 2700
WARNING_RESET_SAVED_PER_MINUTE = 100
# Raw proof waits on 8 Oct 2026 reached 76.5 minutes after the Reset; a raw
# response not yet in the archive is lost with the server's disk.
WARNING_UPLOAD_WAIT = 300
UPLOAD_WAIT = 900
# Work of any kind waiting this long, however often it was claimed and retried,
# while none of its kind finished meanwhile is stalled, however busy the worker
# threads look.
NO_PROGRESS = 120
# PostgreSQL replays its change log after a crash; the health check waits.
WARNING_DATABASE_STARTING = 300
# Minutes after the Reset: collection done, projected inputs, inputs frozen,
# board readable. Freezing by 05:25 leaves five minutes to publish by 05:30.
RESET_COLLECTED, RESET_PROJECTED, RESET_FROZEN, RESET_READABLE = 10, 15, 25, 30


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
    if json.loads(request(origin + "/readyz")).get("ready") is not True:
        raise CheckError("Private API is not ready")
    result = signed_read(
        origin, "/v1/players/search?q=clashlens-alert-read-check&limit=1"
    )
    if not isinstance(result.get("results"), list) or not isinstance(
        result.get("users"), list
    ):
        raise CheckError("Private API player read returned an invalid response")


def signed_read(origin: str, target: str) -> dict:
    """A private API read signed as the website signs it, inside the API container."""
    from clashlens.hmac_proof import (
        AUDIENCE,
        PROOF_VERSION,
        SigningInput,
        load_secret_file,
        sign,
    )

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
    return json.loads(
        request(
            origin + target, headers={"X-ClashLens-" + k: v for k, v in fields.items()}
        )
    )


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


def publication_probe(daily: list[str], origin: str = "http://127.0.0.1:8000") -> None:
    """Run inside the API container; prints how many Resets are unpublished,
    then the Reset of the Daily leaderboard the website showed, or 0.

    A Reset counts when no generation of it has published both its frozen
    leaderboard and its army results an hour after its target time, or when
    a Reset since the first one has no generation at all 70 minutes after it.
    ``daily`` is the Season and day of the board the website's public page
    showed, or empty when it showed none; that board's Reset is read with the
    website's own signed request.
    """
    from clashlens.api_db import ApiDatabase

    served = 0
    if daily:
        season, day = daily
        selector = {"official_season_id": season, "season_day_number": day, "limit": 1}
        board = signed_read(origin, "/v1/leaderboards/frozen?" + urllib.parse.urlencode(selector))
        served = int(datetime.fromisoformat(board["boundary_at"]).timestamp())

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
            ),
            served,
        )
    finally:
        database.close()


def reset_probe(served: str, read_at: str) -> None:
    """Run inside the worker container; updates the latest Reset's record.

    Prints that Reset, its captured members, those whose Reset collection
    ended, and when its board's inputs froze and it was first readable, or 0.
    ``served`` is the Reset of the board the website's public page showed at
    ``read_at``, both 0 when it showed none or could not be read.
    """
    import psycopg

    from clashlens import reset_acceptance

    url = Path(os.environ["CLASHLENS_DATABASE_URL_FILE"]).read_text().strip()
    readable = datetime.fromtimestamp(int(served), UTC) if int(served) else None
    with psycopg.connect(url) as connection:
        record = reset_acceptance.refresh(
            connection,
            readable_boundary=readable,
            readable_at=datetime.fromtimestamp(int(read_at), UTC) if readable else None,
        )
    if record is None:
        print(0, 0, 0, 0, 0)
        return
    epoch = lambda value: 0 if value is None else int(value.timestamp())
    print(
        epoch(record["boundary_at"]),
        record["captured_count"],
        record["collected_count"] + record["not_collected_count"],
        epoch(record["inputs_frozen_at"]),
        epoch(record["readable_at"]),
    )


def website_daily_board(origin: str) -> list[str]:
    """The Season and day of the Daily leaderboard the website's public page
    shows at its public address ``origin``, or an empty list when it shows
    none yet.

    The page sends a visitor on to the board's own address only once the
    website has read the newest frozen board from the API and accepted it, and
    that address must then show the board's own heading, so a board the
    website cannot reach, rejects or fails to show does not count.
    """
    if not origin:
        raise CheckError("No public website address; set CLASHLENS_PUBLIC_ORIGIN")
    try:
        request(origin + "/leaderboards/tracked?view=daily&page=1")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []
        if error.code not in (301, 302, 303, 307, 308):
            raise
        target = urllib.parse.urljoin(origin + "/", error.headers.get("Location", ""))
    else:
        raise CheckError("Daily leaderboard page showed no board")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
    daily = [query.get("season", [""])[0], query.get("day", [""])[0]]
    if (
        not target.startswith(origin + "/leaderboards/tracked?")
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", daily[0])
        or not re.fullmatch(r"[1-9][0-9]?", daily[1])
    ):
        raise CheckError("Daily leaderboard page sent an invalid board address")
    if f">Day {daily[1]} standings</h1>" not in request(target).decode(errors="replace"):
        raise CheckError("Daily leaderboard page did not show the board")
    return daily


def completeness_probe() -> None:
    """Run inside the worker container; prints how many recent battlers are untracked.

    Counts players in Legend I battles of the current or previous Legend day
    who are not tracked although their first such battle was saved over an
    hour ago, long enough for discovery to have checked them. Players with a
    saved profile showing a lower tier observed after the time of their
    latest such battle, such as Monday demotions, are left out.
    """
    import psycopg

    url = Path(os.environ["CLASHLENS_DATABASE_URL_FILE"]).read_text().strip()
    with psycopg.connect(url) as connection:
        print(
            connection.execute(
                """
                WITH since AS (
                    SELECT date_bin(interval '1 day', clock_timestamp(),
                                    timestamptz '2000-01-01 05:00:00+00')
                           - interval '1 day' AS day_start
                ), battlers AS (
                    SELECT side.player_id, battle.created_at, evidence.battle_timestamp
                    FROM legend_battles AS battle
                    JOIN battle_evidence AS evidence ON evidence.battle_id = battle.id
                    CROSS JOIN LATERAL (VALUES (battle.attacker_player_id),
                                               (battle.defender_player_id))
                        AS side (player_id)
                    WHERE battle.ranked_day_start >= (SELECT day_start FROM since)
                )
                SELECT count(*) FROM (
                    SELECT player_id, max(battle_timestamp) AS last_battle_at
                    FROM battlers GROUP BY player_id
                    HAVING min(created_at) < clock_timestamp() - interval '1 hour'
                ) AS seen
                JOIN players ON players.id = seen.player_id
                WHERE NOT players.active
                  AND NOT EXISTS (
                      SELECT 1
                      FROM player_profile_versions AS demoted
                      JOIN player_profile_effects AS demoted_seen
                        ON demoted_seen.profile_version_id = demoted.id
                      WHERE demoted.player_id = players.id
                        AND demoted.eligibility_state = 'ineligible'
                        AND demoted_seen.observed_at > seen.last_battle_at
                  )
                """
            ).fetchone()[0]
        )


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
    config: dict, state: dict, now: float, root: Path, send: Callable[[dict], None]
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
    clock = datetime.fromtimestamp(now, UTC).strftime("%H:%M")
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
            ("uploads", "upload", UPLOAD_WAIT),
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
        # Failed work stays failed, however old, until someone retries it.
        jobs, uploads = (metrics.get(f"{prefix}failed_{kind}") for kind in ("processing", "uploads"))
        findings["failed_work"] = None if None in (jobs, uploads) else jobs + uploads > 0
        if findings["failed_work"]:
            oldest = max(
                metrics.get(f"{prefix}oldest_failed_{kind}_age_seconds", 0)
                for kind in ("processing", "upload")
            )
            state["details"]["failed_work"] = (
                f"Outstanding: {int(jobs)} processing jobs and {int(uploads)} raw-response"
                f" uploads; the oldest failed {int(oldest // 3600)} hours ago"
            )

    findings["warning"] = early_warning(metrics if metrics_read else {}, state, clock)

    def check_health() -> None:
        # Sent before and between the slow checks below: Podman kills about
        # three minutes in, and each slow check may take 25 seconds.
        findings["health"] = health_warning(podman, state)
        send({"health": findings["health"]})

    check_health()

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
    check_health()

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
    check_health()

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
    check_health()

    def run_probe(service: str, arguments: list[str], count: int, unavailable: str):
        try:
            container = f"clashlens-python-{service}"
            result = command([podman, "exec", container, *probe[3:-1], *arguments], 25)
            values = [int(value) for value in result.stdout.split()]
            if result.returncode or len(values) != count:
                raise ValueError
        except (OSError, ValueError, subprocess.SubprocessError):
            errors.append(f"{unavailable} unavailable; run ./ops logs {service}")
            values = None
        check_health()
        return values

    board = run_probe("api", ["--leaderboard"], 3, "Live Leaderboard freshness")
    # 04:55-05:15 UTC ordinary checks pause while the Reset sweep refreshes
    # every player, normally by 05:10, so only one player over 20 minutes
    # counts. After 05:15 the usual limits hold even if the sweep has not
    # finished: a late sweep leaves live pages stale, which is the problem.
    reset_window = "04:55" <= clock < "05:15"
    if board is None:
        state.pop("leaderboard_stale_since", None)
    elif board[2] > LEADERBOARD_OLDEST or (
        not reset_window and board[0] > LEADERBOARD_STALE_SHARE * board[1]
    ):
        since = max(
            state.setdefault("leaderboard_stale_since", now),
            state.get("resumed_at", 0),
        )
        findings["leaderboard"] = True if now - since >= LEADERBOARD_HOLD else None
    else:
        state.pop("leaderboard_stale_since", None)
        findings["leaderboard"] = False
    # The board counts as readable only once the website's public Daily
    # leaderboard page shows it at the address visitors use.
    try:
        daily = website_daily_board(config["public_origin"])
        read_at = int(time.time())
    except (OSError, ValueError, CheckError, http.client.HTTPException):
        errors.append("Reset publication status unavailable; run ./ops logs website")
        daily = read_at = None
    publication = run_probe(
        "api", ["--publication", *(daily or [])], 2, "Reset publication status"
    )
    served = None if publication is None or daily is None else publication[1]
    # The worker's database role writes the Reset's record and reads battles;
    # the API's does neither.
    record = run_probe(
        "worker",
        ["--reset", str(served or 0), str(read_at if served else 0)],
        5,
        "Reset progress",
    )
    findings["reset"], late = reset_stages(record, served, metrics, state, now)
    if publication is not None:
        findings["publication"] = publication[0] > 0 or late
    elif late:
        findings["publication"] = True
    completeness = run_probe(
        "worker", ["--completeness"], 1, "Untracked Legend I battler count"
    )
    if completeness is not None:
        findings["completeness"] = completeness[0] > UNTRACKED_BATTLER_LIMIT
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


def health_warning(podman: str, state: dict) -> bool | None:
    """Warn before a health-check kill: 7 Oct 2026 had none.

    A kill is its own condition, so an open backlog warning never hides one."""
    reasons, unknown = [], False
    for container in ("clashlens-collector", "clashlens-python-worker"):
        try:
            result = command(
                [podman, "inspect", "--format", "{{.State.Health.FailingStreak}}", container]
            )
            streak = None if result.returncode else int(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            streak = None
        if streak is None:
            unknown = True
        elif streak >= WARNING_HEALTH_STREAK:
            reasons.append(f"{container} failed its last {streak} health checks")
    # A database still starting is replaying its change log after a crash; its
    # startup check stops it only once the replay stops advancing.
    try:
        result = command(
            [
                podman,
                "inspect",
                "--format",
                "{{.State.Health.Status}} {{.State.StartedAt.Unix}}",
                "clashlens-postgres",
            ]
        )
        if result.returncode:
            raise ValueError
        status, started = result.stdout.split()
        starting = time.time() - int(started) if status == "starting" else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        unknown = True
    else:
        if starting >= WARNING_DATABASE_STARTING:
            reasons.append(
                f"clashlens-postgres has been starting for {int(starting // 60)} minutes,"
                " probably replaying its change log; ./ops logs postgres shows its progress"
            )
    if reasons:
        state.setdefault("details", {})["health"] = "Now: " + "; ".join(reasons)
    return True if reasons else (None if unknown else False)


def reset_stages(
    record: list[int] | None,
    served: int | None,
    metrics: dict,
    state: dict,
    now: float,
) -> tuple[bool | None, bool]:
    """Warn while the latest Reset is behind its 05:30 board target.

    Returns the warning and whether the board missed 05:30. ``record`` is the
    Reset probe's output: the Reset, its captured members, those whose Reset
    collection ended, and when the board's inputs froze and it was first
    readable (0 if not yet). ``served`` is the Reset of the board the website
    shows now, None if it could not be read.
    """
    start = datetime.fromtimestamp(now, UTC).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    if start.timestamp() > now:
        start -= timedelta(days=1)
    boundary, minutes = int(start.timestamp()), (now - start.timestamp()) / 60
    # Reset work left in the last ten minutes, to project when it finishes.
    remaining = metrics.get("clashlens_collector_reset_work_remaining")
    samples = [sample for sample in state.get("reset_work", []) if now - sample[0] < 600]
    if remaining is not None:
        samples.append([now, remaining])
    state["reset_work"] = samples
    if served == boundary or (record is not None and record[0] == boundary and record[4]):
        return False, False
    # A board that could not be read by 05:30 missed it too.
    late = minutes >= RESET_READABLE
    if late:
        state.setdefault("details", {})["publication"] = (
            f"Now: the {start:%Y-%m-%d} 05:00 UTC board was not readable at 05:30"
            + ("; the website check could not read it" if served is None else "")
        )
    if minutes < RESET_COLLECTED:
        return False, late
    if record is None:
        return None, late
    swept, captured, ended, frozen = record[:4]
    reasons, unknown = [], False
    if swept != boundary:
        reasons.append("Reset collection has not started")
    elif ended < captured:
        reasons.append(f"Reset collection has ended for {ended:,} of {captured:,} players")
    if swept == boundary and frozen:
        pass  # Inputs frozen; publication is checked at 05:30.
    elif minutes >= RESET_FROZEN:
        reasons.append("the board's inputs were not frozen by 05:25")
    elif minutes >= RESET_PROJECTED:
        if remaining is None or len(samples) < 2:
            unknown = True
        elif remaining > 0:
            (first_at, first), (last_at, last) = samples[0], samples[-1]
            pace = (first - last) / (last_at - first_at) if last_at > first_at else 0
            finish = now + remaining / pace if pace > 0 else math.inf
            if finish > boundary + RESET_FROZEN * 60:
                when = (
                    f"about {datetime.fromtimestamp(finish, UTC):%H:%M}"
                    if finish < math.inf
                    else "never at the pace of the last few minutes"
                )
                reasons.append(
                    f"{int(remaining):,} Reset jobs are left and would finish {when},"
                    " after the 05:25 input target"
                )
    if reasons:
        state.setdefault("details", {})["reset"] = "Now: " + "; ".join(reasons)
    return (True if reasons else (None if unknown else False)), late


def early_warning(metrics: dict, state: dict, clock: str) -> bool | None:
    """Warn before a stalled Reset or a growing backlog."""
    reasons, unknown = [], False
    prefix = "clashlens_collector_"
    age = metrics.get(f"{prefix}oldest_pending_processing_age_seconds")
    limit = WARNING_RESET_OVERDUE if "05:00" <= clock < "07:00" else WARNING_OVERDUE
    if age is None:
        unknown = True
    elif age >= limit:
        reasons.append(f"the oldest overdue job has waited {int(age // 60)} minutes")
    upload = metrics.get(f"{prefix}oldest_pending_upload_age_seconds")
    if upload is None:
        unknown = True
    elif upload >= WARNING_UPLOAD_WAIT:
        reasons.append(
            f"a raw response has waited {int(upload // 60)} minutes to be uploaded"
        )
    # Busy or idle threads do not count: only finished work of the kind waiting.
    for name, waiting in sorted(metrics.items()):
        work = re.fullmatch(f"{prefix}waiting_job_(.+)_age_seconds", name)
        if work is None or waiting < NO_PROGRESS:
            continue
        if f"{prefix}completed_jobs_2m" not in metrics:
            unknown = True
        elif not metrics.get(f"{prefix}completed_job_{work[1]}_2m"):
            reasons.append(
                f"{WORK_NAMES.get(work[1], work[1])} have waited {int(waiting // 60)} minutes"
                " and none finished in the last 2 minutes"
            )
    # Responses saved in the collector's sampled minute, once it is all in 05:00-06:00.
    saved = metrics.get("clashlens_collector_responses_saved_last_minute")
    sampled = metrics.get("clashlens_collector_metrics_sample_timestamp_seconds")
    if "05:00" <= clock < "06:00":
        hours = {datetime.fromtimestamp(t, UTC).hour for t in (sampled - 60, sampled)} if sampled is not None else None
        if saved is None or hours != {5}:
            unknown = True
        elif saved < WARNING_RESET_SAVED_PER_MINUTE:
            reasons.append(f"only {int(saved)} responses a minute were saved in the Reset hour")
    if reasons:
        state.setdefault("details", {})["warning"] = "Now: " + "; ".join(reasons)
    return True if reasons else (None if unknown else False)


def observe_site(
    config: dict, state: dict, now: float, _root: Path | None, _send: Callable
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
        intent = state_dir / "alert-intent"
        # ./ops writes stopped while it deliberately stops or starts the
        # stack, running once it is up, and failed when an up left it stopped.
        intended = intent.read_text().strip() if intent.exists() else ""
        if intended == "stopped":
            return 0
        webhook = read_webhook(Path(config["webhook_file"]))
        path = state_dir / "alerts.json"
        state = json.loads(path.read_text()) if path.exists() else {}
        now = time.time()
        if intent.exists():
            state["resumed_at"] = intent.stat().st_mtime

        def send(findings: dict) -> None:
            taken = time.time()
            hold_recoveries(state, findings, taken)
            save_state(path, state)
            deliver(state, findings, taken, path, webhook)

        findings, errors = check(config, state, now, root, send)
        if "site" not in findings:  # Not the outside check.
            findings["deploy"] = intended == "failed"
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
        if sys.argv[1:2] == ["--publication"] and len(sys.argv) in (2, 4):
            publication_probe(sys.argv[2:])
            return 0
        if sys.argv[1:2] == ["--reset"] and len(sys.argv) == 4:
            reset_probe(*sys.argv[2:])
            return 0
        if sys.argv[1:] == ["--completeness"]:
            completeness_probe()
            return 0
        if sys.argv[1:2] == ["--uptime"] and len(sys.argv) > 4:
            state_dir, webhook, *urls = sys.argv[2:]
            return run(
                {"webhook_file": webhook, "urls": urls},
                Path(state_dir),
                None,
                observe_site,
            )
        state_dir, root, webhook, health, spool, max_bytes, max_objects, origin = sys.argv[1:]
        return run(
            {
                "webhook_file": webhook,
                "health_port": health,
                "public_origin": origin,
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
