#!/usr/bin/env python3
"""Bounded read-only live-day observer/validator/watchdog for issue #92 Step 9.

Subcommands: start | sample | finalize | validate | watchdog.
Exit 0 = complete valid action, 1 = objective live gate failure,
2 = absent, malformed, mixed, or unavailable evidence.

The observer never enqueues work, changes player state, creates/drops
schemas, resets counters, or calls the official API. All database access is
one REPEATABLE READ READ ONLY transaction per capture. Missing or malformed
evidence is unknown/failure, never zero.

Semantic regular-window admission reconciliation is pending the validated
admission handoff (see ADMISSION_STATUS); this tool records liveness,
resources, continuity, and structural reset evidence now and refuses to claim
complete window accounting before that integration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "python" / "src")]

from scripts import deployment_receipt

from clashlens.profile import normalize_player_tag

SCHEMA_LIVE = "step9-live-day-v1"
SCHEMA_PREFLIGHT = "step9-preflight-v1"
SCHEMA = SCHEMA_LIVE  # default mode; run.json pins the actual schema
ADMISSION_SCHEMA_VERSION = "0022-final"
ADMISSION_RUN_STATES = ("active", "capacity_exceeded", "capture_out_of_range")
ADMISSION_FAILURE_CODES = ("admission_evidence_capacity_exceeded",
                           "admission_evidence_capture_out_of_range")
MODES = {
    "live-day": {"schema": SCHEMA_LIVE, "slots": 1440, "windows": 288,
                  "interval": timedelta(hours=24), "require_0500": True,
                  "admission": True},
    "preflight": {"schema": SCHEMA_PREFLIGHT, "slots": 75, "windows": 15,
                    "interval": timedelta(hours=1), "require_0500": False,
                    "admission": False,
                    "drain_slots": 15},
}
# Fixed preflight official-traffic envelope (Phase 4 ceiling, probes measured).
PREFLIGHT_ENVELOPE = {"profile": 13500, "global_rankings_intents": 1,
                      "battle_log": 0}
PREFLIGHT_ALLOWED_WORK = ("discovery_profile", "endpoint_retry",
                          "global_player_rankings")
CORE_WINDOWS = 288
WINDOW_MINUTES = 5
SLOT_SECONDS = 60
SLOTS_PER_WINDOW = WINDOW_MINUTES * 60 // SLOT_SECONDS
CORE_SLOTS = CORE_WINDOWS * SLOTS_PER_WINDOW  # 1440
COHORT_MAX_BYTES = 1024 * 1024
COHORT_MAX_LINES = 20000
SAMPLE_MAX_BYTES = 64 * 1024
WINDOW_MAX_BYTES = 128 * 1024
RUN_DIR_MAX_BYTES = 256 * 1024 * 1024
WATCHDOG_POLL_SECONDS = 30
WRITE_STATEMENTS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|TRUNCATE|VACUUM|ALTER|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ACTIVE_STATUSES = ("pending", "leased", "waiting_retry", "waiting_dependency")
# Player-scoped collection roots that must never belong to an outside player.
PLAYER_SCOPED_WORK = (
    "regular_poll",
    "reset_baseline",
    "discovery_profile",
    "initial_collection",
    "live_refresh",
)
LEGEND_I_TIER_ID = 105000036
LEGEND_I_TIER_NAME = "Legend I"


class Step9Error(Exception):
    def __init__(self, code: str, message: str, *, gate: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.gate = gate  # True -> exit 1 live gate failure, else exit 2


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _parse_utc(value: str) -> datetime:
    text = value.strip()
    try:
        if text.endswith("Z"):
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        else:
            parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise Step9Error("bad_interval", f"invalid UTC timestamp: {value}") from error
    if parsed.tzinfo is None:
        raise Step9Error("bad_interval", f"UTC timestamp missing offset: {value}")
    return parsed.astimezone(UTC)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _digest(payload: object) -> str:
    return _sha256(_canonical(payload))


def _boot_id() -> str | None:
    try:
        return (Path("/proc/sys/kernel/random/boot_id").read_text()).strip() or None
    except OSError:
        return None


def _read_cohort(path_str: str) -> tuple[list[str], str, str]:
    """Validate the protected cohort file; return sorted tags, raw/canonical SHA."""
    if not os.path.isabs(path_str):
        raise Step9Error("cohort_unreadable", "cohort path must be absolute")
    path = Path(path_str)
    if path.is_symlink():
        raise Step9Error("cohort_symlink", "cohort path must not be a symlink")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise Step9Error("cohort_unreadable", "cohort file is unreadable") from error
    try:
        size = os.fstat(descriptor).st_size
        if size > COHORT_MAX_BYTES:
            raise Step9Error("cohort_oversized", "cohort file exceeds 1 MiB")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(COHORT_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > COHORT_MAX_BYTES:
        raise Step9Error("cohort_oversized", "cohort file exceeds 1 MiB")
    raw_sha = _sha256(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Step9Error("cohort_malformed", "cohort file is not UTF-8") from error
    lines = text.splitlines()
    if len(lines) > COHORT_MAX_LINES:
        raise Step9Error("cohort_oversized", "cohort file exceeds 20,000 lines")
    tags: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        if len(line.encode()) > 64:
            raise Step9Error("cohort_malformed", "cohort line is too long")
        try:
            normalized = normalize_player_tag(line)
        except ValueError as error:
            raise Step9Error("cohort_malformed", f"malformed cohort tag: {error}") from error
        if normalized in seen:
            raise Step9Error("cohort_duplicate", "duplicate tag after normalization")
        seen.add(normalized)
        tags.append(normalized)
    tags.sort()
    return tags, raw_sha, _digest(tags)


def _exclusive_json(destination: Path, payload: dict) -> str:
    """Write one artifact exclusively (O_EXCL + fsync + dir fsync); return SHA."""
    destination = destination.absolute()
    if destination.is_symlink():
        raise Step9Error("artifact_occupied", "artifact path must not be a symlink")
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    data = text.encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as error:
        raise Step9Error("artifact_occupied", "artifact path is already occupied") from error
    except OSError as error:
        raise Step9Error("artifact_unwritable", "artifact could not be written") from error
    finally:
        temporary.unlink(missing_ok=True)
    digest = _sha256(data)
    if len(data) > SAMPLE_MAX_BYTES and destination.parent.name == "samples":
        raise Step9Error("artifact_capacity_exceeded", "minute sample exceeds 64 KiB")
    return digest


def _dir_usage(run_dir: Path) -> int:
    total = 0
    for child in run_dir.rglob("*"):
        if child.is_file() and not child.is_symlink():
            total += child.stat().st_size
    return total


def _check_capacity(run_dir: Path) -> None:
    if _dir_usage(run_dir) >= RUN_DIR_MAX_BYTES:
        raise Step9Error(
            "artifact_capacity_exceeded", "run directory exceeds 256 MiB", gate=True
        )


def _load_run(run_dir: Path) -> dict:
    path = run_dir / "run.json"
    if path.is_symlink() or not path.is_file():
        raise Step9Error("run_missing", "run.json is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Step9Error("run_malformed", "run.json is unreadable") from error
    if not isinstance(payload, dict) or payload.get("schema") not in (SCHEMA_LIVE, SCHEMA_PREFLIGHT):
        raise Step9Error("run_malformed", "run.json schema mismatch")
    return payload


def _record_failure(run_dir: Path | None, code: str, message: str) -> None:
    if run_dir is None:
        return
    try:
        failures = run_dir / "failures"
        failures.mkdir(parents=True, exist_ok=True)
        stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        (_exclusive_json(failures / f"{code}-{stamp}.json",
                         {"code": code, "message": message, "at": stamp}))
    except Step9Error:
        pass
    except OSError:
        pass

# --- Read-only database contract -------------------------------------------
# Illustrative shapes are checked against the real migrated schema by
# scripts/test_step9_check.py. Semantic admission reconciliation stays pending
# ADMISSION_STATUS and must not be claimed complete before that integration.

SQL_READ_ONLY_PREAMBLE = (
    "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
    "SET LOCAL statement_timeout = '10s'; SET LOCAL lock_timeout = '2s';"
)

SQL_DATABASE_IDENTITY = (
    "SELECT system_identifier::text, current_database(), "
    "statement_timestamp() AT TIME ZONE 'UTC'"
    " FROM pg_control_system()"
)

SQL_POPULATION_MAP = """
SELECT p.id, p.normalized_tag, p.active, p.eligibility_state,
       p.next_due_at, p.current_observed_at,
       v.id, v.observed_at, v.league_tier_id, v.league_tier_name,
       v.eligibility_state, v.source_contract_state
FROM players AS p
LEFT JOIN player_profile_versions AS v ON v.id = p.current_profile_version_id
WHERE p.normalized_tag = ANY(%s::text[])
ORDER BY p.normalized_tag
"""

SQL_OUTSIDE_ACTIVE = """
SELECT count(*) FROM players WHERE active AND NOT (id = ANY(%s::bigint[]))
"""

SQL_OUTSIDE_ROOTS = """
SELECT count(*) FROM collector_jobs AS j
WHERE j.work_type = ANY(%s::text[])
  AND j.player_id IS NOT NULL
  AND NOT (j.player_id = ANY(%s::bigint[]))
  AND j.status IN ('pending','leased','waiting_retry','waiting_dependency')
"""

SQL_FIXED_IDS = """
SELECT id, active, eligibility_state, current_profile_version_id,
       current_observed_at, next_due_at
FROM players WHERE id = ANY(%s::bigint[]) ORDER BY id
"""

SQL_ACTIVE_QUEUES = """
SELECT status, count(*), min(due_at) FROM collector_jobs
WHERE status IN ('pending','leased','waiting_retry','waiting_dependency')
GROUP BY status
"""

SQL_ACTIVE_PYTHON_QUEUES = """
SELECT status, count(*), min(due_at) FROM python_processing_jobs
WHERE status IN ('pending','leased','waiting_retry','waiting_dependency')
GROUP BY status
"""

SQL_LIVENESS_COUNTERS = """
SELECT (SELECT max(id) FROM collector_jobs),
       (SELECT max(id) FROM collector_attempts),
       (SELECT max(id) FROM collector_observations),
       (SELECT max(id) FROM python_processing_jobs),
       pg_current_wal_lsn()::text, statement_timestamp()
"""

SQL_RESET_IDENTITY = """
SELECT a.boundary_at, a.reset_sweep_id, a.regular_drain_complete,
       a.reset_drain_complete, a.safe_handoff,
       (SELECT count(*) FROM collector_reset_sweep_members AS m
         WHERE m.sweep_id = a.reset_sweep_id),
       (SELECT count(*) FROM boundary_publication_generation_members AS gm
         JOIN boundary_publication_generations AS g ON g.id = gm.generation_id
         WHERE g.sweep_id = a.reset_sweep_id AND g.boundary_at = a.boundary_at),
       (SELECT count(*) FROM collector_jobs AS j
         WHERE j.sweep_id = a.reset_sweep_id
           AND j.status IN ('pending','leased','waiting_retry','waiting_dependency'))
FROM collector_boundary_admission AS a
WHERE a.boundary_at >= %s AND a.boundary_at <= %s
ORDER BY a.boundary_at
"""

SQL_ELIGIBILITY_TRANSITIONS = """
SELECT v.player_id, e.id, e.created_at, v.observed_at,
       v.eligibility_state, v.source_contract_state,
       v.league_tier_id, v.league_tier_name
FROM player_profile_effects AS e
JOIN player_profile_versions AS v ON v.id = e.profile_version_id
WHERE v.player_id = ANY(%s::bigint[])
  AND e.created_at >= %s AND e.created_at < %s
ORDER BY v.player_id, e.created_at, e.id
"""

ALL_RO_STATEMENTS = (
    SQL_POPULATION_MAP, SQL_OUTSIDE_ACTIVE, SQL_OUTSIDE_ROOTS, SQL_FIXED_IDS,
    SQL_ACTIVE_QUEUES, SQL_ACTIVE_PYTHON_QUEUES, SQL_LIVENESS_COUNTERS,
    SQL_RESET_IDENTITY, SQL_ELIGIBILITY_TRANSITIONS,
)


def assert_read_only(sql: str) -> None:
    """Source-shape guard: retained observer SQL must never write."""
    if WRITE_STATEMENTS.search(sql):
        raise Step9Error("sql_not_read_only", "observer SQL must be read-only")


for _statement in ALL_RO_STATEMENTS:
    assert_read_only(_statement)


def _classify_eligible(row: tuple) -> str:
    (_pid, _tag, active, state, _due, _obs,
     _vid, _vobs, tier_id, tier_name, velig, contract) = row
    if not active or state != "eligible":
        return "ineligible_or_inactive"
    if tier_id != LEGEND_I_TIER_ID or tier_name != LEGEND_I_TIER_NAME:
        return "not_legend_one"
    if velig != "eligible" or contract != "accepted":
        return "profile_disagree"
    return "eligible"


def _eligible_digest(ids: list[int]) -> str:
    return _digest(sorted(ids))

# --- start ---------------------------------------------------------------

def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--podman-bin", default="podman")


def _resolve_run_dir(value: str) -> Path:
    if not os.path.isabs(value):
        raise Step9Error("bad_run_dir", "run directory must be absolute")
    path = Path(value)
    if path.is_symlink():
        raise Step9Error("bad_run_dir", "run directory must not be a symlink")
    return path


def cmd_start(arguments: argparse.Namespace, db: object | None = None) -> dict:
    run_dir = _resolve_run_dir(arguments.run_dir)
    mode_name = getattr(arguments, "mode", "live-day")
    if mode_name not in MODES:
        raise Step9Error("bad_mode", "mode must be live-day or preflight")
    mode = MODES[mode_name]
    tags, raw_sha, canonical_sha = _read_cohort(arguments.cohort_file)
    try:
        receipt = json.loads(Path(arguments.deployed_receipt).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Step9Error("receipt_unavailable", "deployed receipt is unreadable") from error
    try:
        deployment_receipt.validate_receipt(receipt, require_digest=True)
    except Exception as error:
        raise Step9Error("receipt_invalid", f"deployed receipt invalid: {error}") from error
    if receipt.get("receipt_scope") != "deployed-stack":
        raise Step9Error("receipt_scope", "Step 9 start requires a deployed-stack receipt")
    discovery = receipt.get("configuration", {}).get("fields", {}).get(
        "player_discovery_enabled")
    if discovery != "false":
        raise Step9Error("discovery_not_disabled",
                         "Step 9 start requires player_discovery_enabled=false")
    core_start = _parse_utc(arguments.core_start)
    core_end = _parse_utc(arguments.core_end)
    if core_end - core_start != mode["interval"]:
        raise Step9Error("bad_interval",
                         f"{mode_name} interval must be exactly {mode['interval']}")
    if mode["require_0500"]:
        for edge in (core_start, core_end):
            if edge.hour != 5 or edge.minute != 0 or edge.second != 0:
                raise Step9Error("bad_interval", "core edges must be 05:00:00Z")
    elif core_start.second != 0 or core_end.second != 0:
        raise Step9Error("bad_interval", "preflight edges must align to a minute")
    for name in ("collector_container", "postgres_container",
                 "python_api_container", "python_worker_container"):
        value = getattr(arguments, name)
        if not _CONTAINER.fullmatch(value):
            raise Step9Error("bad_container", f"invalid container name: {name}")
    run_id = arguments.run_id or (
        "step9-" + core_start.strftime("%Y%m%d") + "-" + canonical_sha[:12]
    )
    if not _RUN_ID.fullmatch(run_id):
        raise Step9Error("bad_run_id", "invalid run ID")
    try:
        run_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError as error:
        raise Step9Error("artifact_occupied", "run directory already exists") from error
    except OSError as error:
        raise Step9Error("run_unwritable", "run directory cannot be created") from error
    os.chmod(run_dir, 0o700)
    initial = {"status": "unknown", "failure_code": "database_unavailable"}
    if db is not None:
        try:
            initial = _capture_initial(db, tags)
        except Step9Error:
            raise
        except Exception as error:
            raise Step9Error("database_unavailable",
                             f"initial snapshot failed: {error}") from error
    header = {
        "schema": mode["schema"], "mode": mode_name, "run_id": run_id,
        "core_start": core_start.isoformat(), "core_end": core_end.isoformat(),
        "lead_in_seconds": arguments.lead_in_seconds,
        "tail_seconds": arguments.tail_seconds,
        "created_at": _utc_now().isoformat(),
        "monotonic_ns": time.monotonic_ns(), "boot_id": _boot_id(),
        "source_revision": receipt["source"]["revision"],
        "receipt_digest": receipt["receipt_digest"],
        "database_identity": initial.get("database_identity"),
        "containers": {
            "collector": arguments.collector_container,
            "postgres": arguments.postgres_container,
            "python_api": arguments.python_api_container,
            "python_worker": arguments.python_worker_container,
            "worker_replicas": arguments.worker_replicas,
        },
        "runtime_metrics_url": arguments.runtime_metrics_url,
        "spool_path": arguments.spool_path,
        "postgres_path": arguments.postgres_path,
        "cohort": {
            "path": arguments.cohort_file,
            "input_count": len(tags), "raw_sha256": raw_sha,
            "canonical_sha256": canonical_sha,
        },
        "initial": initial,
        "deadline": arguments.deadline,
        "max_sample_age_seconds": arguments.max_sample_age_seconds,
        "watchdog_unit": arguments.watchdog_unit,
        "max_invocation_gap_seconds": getattr(
            arguments, "max_invocation_gap_seconds", 5),
        "admission": _admission_header(db, mode_name, run_id, core_start,
                                        core_end),
        "script_sha256": _sha256(Path(__file__).read_bytes()),
    }
    digest = _exclusive_json(run_dir / "run.json", header)
    try:
        os.chmod(run_dir / "run.json", 0o600)
    except OSError as error:
        raise Step9Error("run_unwritable", "run.json cannot be secured") from error
    _check_capacity(run_dir)
    header["header_sha256"] = digest
    return header


def _admission_header(db: object | None, mode_name: str, run_id: str,
                      core_start: datetime, core_end: datetime) -> dict:
    """Pin admission expectations; fail closed when 0022 is absent."""
    if not MODES[mode_name]["admission"]:
        return {"schema": None, "status": "not_applicable"}
    if db is None:
        return {"schema": ADMISSION_SCHEMA_VERSION, "status": "unprobed"}
    try:
        present = db.admission_present()
    except Exception as error:
        raise Step9Error("database_unavailable",
                         f"admission probe failed: {error}") from error
    if not present:
        raise Step9Error("admission_schema_absent",
                         "0022 admission tables are absent")
    try:
        row = db.admission_run(run_id)
    except Exception as error:
        raise Step9Error("database_unavailable",
                         f"admission header unreadable: {error}") from error
    if row is None:
        raise Step9Error("admission_run_missing",
                         "no 0022 run header for this run ID")
    return {"schema": ADMISSION_SCHEMA_VERSION, "status": "integrated",
            "state": row["state"], "failure_code": row["failure_code"],
            "capture_start": str(row["capture_start"]),
            "capture_end": str(row["capture_end"]),
            "max_events": row["max_events"],
            "max_selected_entries": row["max_selected_entries"]}


def _capture_initial(db: object, tags: list[str]) -> dict:
    rows = db.snapshot_population(tags)
    eligible: list[int] = []
    counts = {"eligible": 0, "ineligible_or_inactive": 0,
              "not_legend_one": 0, "profile_disagree": 0, "unmatched": 0}
    seen = set()
    for row in rows:
        seen.add(row[1])
        bucket = _classify_eligible(row)
        counts[bucket] = counts.get(bucket, 0) + 1
        if bucket == "eligible":
            eligible.append(row[0])
    counts["unmatched"] = len(tags) - len(seen)
    outside = db.outside_active(sorted(eligible))
    if outside:
        raise Step9Error(
            "foreign_population",
            f"{outside} active players outside the supplied file", gate=True)
    if db.outside_roots(sorted(eligible)):
        raise Step9Error(
            "foreign_lineage", "outside player-scoped collection lineage exists",
            gate=True)
    if not eligible:
        raise Step9Error("zero_eligible", "zero eligible supplied players", gate=True)
    return {
        "status": "captured", "eligible_count": len(eligible),
        "eligible_digest": _eligible_digest(eligible),
        "counts": counts,
        "database_identity": db.identity(),
    }

# --- time slots ----------------------------------------------------------

def classify_slot(expected_utc: datetime, captured_utc: datetime,
                 wall_delta: float, mono_delta: float,
                 boot_changed: bool, late_allowance: float = 5.0) -> dict:
    """Pure slot classification; all inputs injectable for tests."""
    jump = abs(wall_delta - mono_delta)
    if boot_changed:
        outcome, failure = "boot_change", "boot_id_changed"
    elif jump > 2.0:
        outcome, failure = "clock_jump", "clock_jump"
    elif mono_delta < 0:
        outcome, failure = "non_monotonic", "non_monotonic_time"
    elif wall_delta < 0:
        outcome, failure = "out_of_order", "sample_out_of_order"
    elif mono_delta > SLOT_SECONDS + late_allowance:
        outcome, failure = "late", "sample_late"
    else:
        outcome, failure = "on_time", None
    return {"outcome": outcome, "failure_code": failure,
            "wall_delta_seconds": wall_delta, "mono_delta_seconds": mono_delta,
            "clock_jump_seconds": jump}


def slot_expected_utc(core_start: datetime, index: int) -> datetime:
    return core_start + timedelta(seconds=SLOT_SECONDS * index)


# --- database + environment adapters -------------------------------------

class Database:
    """Thin read-only psycopg wrapper; connection factory is injectable."""

    def __init__(self, connect) -> None:
        self._connect = connect

    def _one_txn(self, work):
        import psycopg

        with self._connect() as connection:
            # Apply before the first statement so the implicit transaction
            # actually runs repeatable-read read-only.
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute("SET LOCAL lock_timeout = '2s'")
                return work(cursor)

    def identity(self) -> dict:
        def work(cursor):
            cursor.execute(SQL_DATABASE_IDENTITY)
            system_id, name, at = cursor.fetchone()
            return {"system_identifier": system_id, "database_name": name,
                    "captured_at": str(at)}
        return self._one_txn(work)

    def snapshot_population(self, tags: list[str]) -> list[tuple]:
        def work(cursor):
            cursor.execute(SQL_POPULATION_MAP, (tags,))
            return list(cursor.fetchall())
        return self._one_txn(work)

    def outside_active(self, ids: list[int]) -> int:
        def work(cursor):
            cursor.execute(SQL_OUTSIDE_ACTIVE, (ids,))
            return int(cursor.fetchone()[0])
        return self._one_txn(work)

    def outside_roots(self, ids: list[int]) -> int:
        def work(cursor):
            cursor.execute(SQL_OUTSIDE_ROOTS, (list(PLAYER_SCOPED_WORK), ids))
            return int(cursor.fetchone()[0])
        return self._one_txn(work)

    def reset_identity(self, start: str, end: str) -> list[tuple]:
        def work(cursor):
            cursor.execute(SQL_RESET_IDENTITY, (start, end))
            return list(cursor.fetchall())
        return self._one_txn(work)

    def transitions(self, ids: list[int], start: str, end: str) -> list[tuple]:
        """Domain effect transitions for reporting only; never scheduler
        visibility evidence."""
        def work(cursor):
            cursor.execute(SQL_ELIGIBILITY_TRANSITIONS, (ids, start, end))
            return list(cursor.fetchall())
        return self._one_txn(work)

    def admission_present(self) -> bool:
        def work(cursor):
            cursor.execute(SQL_ADMISSION_TABLES)
            return bool(cursor.fetchone()[0])
        try:
            return self._one_txn(work)
        except Exception:  # noqa: BLE001 - probe absence is evidence
            return False

    @staticmethod
    def _missing_table(error: Exception) -> bool:
        return getattr(error, "sqlstate", "") == "42P01"

    def admission_run(self, run_id: str) -> dict | None:
        def work(cursor):
            cursor.execute(SQL_ADMISSION_RUN, (run_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            keys = ("run_id", "capture_start", "capture_end", "max_events",
                    "max_selected_entries", "events_written",
                    "selected_entries_written", "state", "stopped_at",
                    "failure_code")
            return dict(zip(keys, row))
        try:
            return self._one_txn(work)
        except Exception as error:
            if self._missing_table(error):
                return None
            raise

    def admission_events(self, run_id: str, start: str, end: str) -> list[dict]:
        keys = ("id", "invocation_id", "cycle_at", "scheduler_at",
                "database_at", "gate_allowed", "gate_handoff_at",
                "batch_limit", "capture_start", "capture_end", "visible_due_count", "visible_due_min_at",
                "unselected_visible_due_count",
                "unselected_visible_due_min_at",
                "unselected_visible_past_deadline_count",
                "unselected_visible_past_deadline_min_at",
                "selected_past_deadline_count", "selected_player_ids",
                "selected_due_ats", "selected_profile_version_ids",
                "selected_eligibility_states", "inserted_job_ids",
                "advanced_count", "selected_count", "inserted_count")

        def work(cursor):
            cursor.execute(SQL_ADMISSION_EVENTS, (run_id, start, end))
            return [dict(zip(keys, row)) for row in cursor.fetchall()]
        return self._one_txn(work)

    def admission_profile_counts(self, run_id: str, start: str,
                                 end: str) -> dict:
        def work(cursor):
            cursor.execute(SQL_ADMISSION_PROFILE_CHECK, (run_id, start, end))
            return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
        return self._one_txn(work)

    def admission_latest(self, run_id: str) -> dict | None:
        keys = ("id", "database_at", "gate_allowed", "selected_count",
                "inserted_count", "advanced_count")

        def work(cursor):
            cursor.execute(SQL_ADMISSION_LATEST, (run_id,))
            row = cursor.fetchone()
            return dict(zip(keys, row)) if row else None
        try:
            return self._one_txn(work)
        except Exception as error:
            if self._missing_table(error):
                return None
            raise

    def semantic_roots(self, start: str, end: str) -> list[tuple]:
        def work(cursor):
            cursor.execute(SQL_SEMANTIC_ROOTS, (start, end))
            return list(cursor.fetchall())
        return self._one_txn(work)

    def active_queues(self) -> dict:
        def work(cursor):
            cursor.execute(SQL_ACTIVE_QUEUES)
            collector = [tuple(r) for r in cursor.fetchall()]
            cursor.execute(SQL_ACTIVE_PYTHON_QUEUES)
            python = [tuple(r) for r in cursor.fetchall()]
            return {"collector": collector, "python": python}
        return self._one_txn(work)

    def preflight_probes(self, start: str, end: str) -> dict:
        def work(cursor):
            cursor.execute(SQL_PREFLIGHT_WORKCOUNTS, (start, end))
            workcounts = [tuple(r) for r in cursor.fetchall()]
            cursor.execute(SQL_PREFLIGHT_OBSERVATIONS, (start, end))
            observations = [tuple(r) for r in cursor.fetchall()]
            cursor.execute(SQL_PREFLIGHT_RANKING_INTENTS, (start, end))
            intents = tuple(cursor.fetchone())
            cursor.execute(SQL_PREFLIGHT_PENDING_REMOTE)
            pending = int(cursor.fetchone()[0])
            return {"workcounts": workcounts, "observations": observations,
                    "intents": intents, "pending_remote": pending}
        return self._one_txn(work)

    def minute_snapshot(self, ids: list[int]) -> dict:
        def work(cursor):
            cursor.execute(SQL_FIXED_IDS, (ids,))
            fixed = list(cursor.fetchall())
            cursor.execute(SQL_ACTIVE_QUEUES)
            queues = [tuple(r) for r in cursor.fetchall()]
            cursor.execute(SQL_ACTIVE_PYTHON_QUEUES)
            py_queues = [tuple(r) for r in cursor.fetchall()]
            cursor.execute(SQL_LIVENESS_COUNTERS)
            counters = tuple(cursor.fetchone())
            return {"fixed": fixed, "queues": queues,
                    "python_queues": py_queues, "counters": counters}
        return self._one_txn(work)


def fetch_runtime_metrics(url: str, timeout: float = 10.0) -> dict:
    """Fetch query-free collector counters; missing/unparseable is unknown."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read(65536).decode("utf-8")
    except Exception as error:
        raise Step9Error("metrics_unavailable",
                         f"runtime metrics unavailable: {error}") from error
    return parse_runtime_metrics(text)


def host_pressure() -> dict:
    """Bounded /proc memory facts; unavailable fields stay null."""
    facts: dict = {"memory": None, "error": None}
    try:
        fields: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2] == "kB":
                fields[parts[0].rstrip(":")] = int(parts[1]) * 1024
        facts["memory"] = {
            "total_bytes": fields.get("MemTotal"),
            "available_bytes": fields.get("MemAvailable"),
            "swap_total_bytes": fields.get("SwapTotal"),
            "swap_free_bytes": fields.get("SwapFree"),
        }
    except (OSError, ValueError) as error:
        facts["error"] = f"host_pressure_unavailable: {error}"
    return facts


def filesystem_facts(spool: str, postgres: str) -> dict:
    """Mount identity + statvfs; never raises, missing is unknown."""
    from clashlens.filesystem import mount_facts as shared_mount_facts

    result: dict = {}
    for label, target in (("spool", spool), ("postgres", postgres)):
        try:
            facts = shared_mount_facts(Path(target))
            stat = os.statvfs(target)
            result[label] = {
                "mount_point": facts.get("mount_point"),
                "source": facts.get("source"),
                "filesystem_type": str(facts.get("filesystem_type", "unknown")),
                "mnt_id": facts.get("mnt_id"),
                "free_bytes": stat.f_bavail * stat.f_frsize,
                "error": facts.get("error"),
            }
        except OSError as error:
            result[label] = {"mount_point": None, "source": None,
                             "filesystem_type": "unknown", "mnt_id": None,
                             "free_bytes": None,
                             "error": f"filesystem_unavailable: {error}"}
    return result

# --- sample --------------------------------------------------------------

_RUNTIME_METRIC_LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+"
    r"(-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)$"
)
# Gauges that may legitimately decrease; everything else numeric is a
# monotonic counter for continuity purposes.
_RUNTIME_GAUGES = {
    "clashlens_collector_database_pool_acquired_connections",
    "clashlens_collector_database_pool_idle_connections",
}


def _parse_metric_labels(text: str) -> dict:
    if not text:
        return {}
    labels: dict[str, str] = {}
    pattern = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\\]|\\\\.)*)"(?:,|$)')
    position = 0
    while position < len(text):
        match = pattern.match(text, position)
        if match is None or match.group(1) in labels:
            raise Step9Error("metrics_malformed", "bad metric labels")
        labels[match.group(1)] = match.group(2).replace('\\"', '"').replace(
            "\\\\", "\\")
        position = match.end()
    return labels


def parse_runtime_metrics(text: str) -> dict:
    """Parse B1 query-free /runtime-metrics Prometheus text exposition."""
    if len(text.encode()) > 65536:
        raise Step9Error("metrics_malformed", "metrics body too large")
    process_id: str | None = None
    started: float | None = None
    counters: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _RUNTIME_METRIC_LINE.fullmatch(line)
        if match is None:
            raise Step9Error("metrics_malformed", "bad metric line")
        name, raw_labels, raw_value = match.groups()
        if not name.startswith("clashlens_collector_"):
            continue
        try:
            value = float(raw_value)
        except ValueError as error:
            raise Step9Error("metrics_malformed", "bad metric value") from error
        labels = _parse_metric_labels(raw_labels or "")
        if name == "clashlens_collector_process_identity_info":
            if set(labels) != {"process_id"} or value != 1:
                raise Step9Error("metrics_malformed", "bad process identity")
            process_id = labels["process_id"]
        elif name == "clashlens_collector_process_start_time_seconds":
            if labels or value < 0:
                raise Step9Error("metrics_malformed", "bad process start")
            started = value
        else:
            key = name + "{" + ",".join(
                f"{k}={v}" for k, v in sorted(labels.items())) + "}"
            counters[key] = value
    if process_id is None or started is None:
        raise Step9Error("metrics_malformed", "missing process identity")
    return {"process_id": process_id, "started_at": started,
            "counters": counters,
            "digest": _digest({"p": process_id, "s": started,
                                 "c": counters})}


def _check_counters_decreased(previous: dict | None, current: dict) -> str | None:
    if not previous:
        return None
    for key, value in current.items():
        if key.split("{", 1)[0] in _RUNTIME_GAUGES:
            continue
        old = previous.get(key)
        if isinstance(old, (int, float)) and value < old:
            return key
    return None


def build_sample(*, run: dict, index: int, expected_utc: datetime,
                 captured_utc: datetime, mono_elapsed: float,
                 wall_delta: float, mono_delta: float, boot_id: str | None,
                 db_facts: dict | None, db_error: str | None,
                 metrics: dict | None, metrics_error: str | None,
                 previous_metrics: dict | None,
                 pressure: dict, fs: dict, watchdog_active: bool | None,
                 admission_latest: dict | None = None,
                 ) -> dict:
    classification = classify_slot(
        expected_utc, captured_utc, wall_delta, mono_delta,
        boot_changed=(boot_id != run.get("boot_id") and run.get("boot_id") is not None
                      and boot_id is not None))
    mode = MODES.get(run.get("mode", "live-day"), MODES["live-day"])
    sample: dict = {
        "schema": mode["schema"], "mode": run.get("mode", "live-day"),
        "run_id": run["run_id"], "slot": index,
        "expected_utc": expected_utc.isoformat(),
        "captured_utc": captured_utc.isoformat(),
        "monotonic_elapsed_seconds": mono_elapsed, "boot_id": boot_id,
        **classification,
        "database_utc": None, "metrics": metrics,
        "metrics_error": metrics_error, "metrics_digest": None,
        "counter_reset": None, "queue": None, "eligibility": None,
        "host_pressure": pressure, "filesystem": fs,
        "watchdog_active": watchdog_active,
    }
    if metrics is not None:
        sample["metrics_digest"] = metrics["digest"]
        sample["counter_reset"] = _check_counters_decreased(
            (previous_metrics or {}).get("counters"), metrics["counters"])
        previous_pid = (previous_metrics or {}).get("process_id")
        if previous_pid is not None and previous_pid != metrics["process_id"]:
            sample["failure_code"] = "process_identity_changed"
            sample["outcome"] = "process_restart"
    sample["admission_latest"] = admission_latest
    if admission_latest is not None and admission_latest.get("mismatch"):
        sample["failure_code"] = "admission_count_mismatch"
        sample["outcome"] = "admission_mismatch"
    if db_error is not None or db_facts is None:
        sample["database_error"] = db_error or "database_unavailable"
        return sample
    counters = db_facts["counters"]
    sample["database_utc"] = str(counters[5])
    sample["queue"] = {
        "collector": [{"status": s, "count": int(c), "oldest_due_at": str(m)}
                      for s, c, m in db_facts["queues"]],
        "python": [{"status": s, "count": int(c), "oldest_due_at": str(m)}
                   for s, c, m in db_facts["python_queues"]],
    }
    # The minute probe carries only current player IDs/flags; tier and
    # contract agreement is proven at finalize from the joined population
    # map, never inferred here.
    sample["eligibility"] = {
        "active_fixed_count": sum(1 for r in db_facts["fixed"] if r[1]),
        "fixed_rows": len(db_facts["fixed"]),
        "agreement": "unknown_until_finalize",
    }
    sample["liveness"] = {"collector_max_job_id": counters[0],
                          "attempt_max_id": counters[1],
                          "observation_max_id": counters[2],
                          "python_max_job_id": counters[3],
                          "wal_lsn": counters[4]}
    if index % SLOTS_PER_WINDOW == SLOTS_PER_WINDOW - 1:
        window_index = index // SLOTS_PER_WINDOW
        window = {
            "window_index": window_index,
            "cycle_start": slot_expected_utc(
                _parse_utc(run["core_start"]),
                window_index * SLOTS_PER_WINDOW).isoformat(),
            "cycle_end": slot_expected_utc(
                _parse_utc(run["core_start"]),
                (window_index + 1) * SLOTS_PER_WINDOW).isoformat(),
            "role": "core",
            "admission": "integrated_at_finalize" if mode["admission"]
                         else "not_applicable",
        }
        payload = _canonical(window)
        if len(payload) > WINDOW_MAX_BYTES:
            raise Step9Error("artifact_capacity_exceeded",
                             "window object exceeds 128 KiB")
        sample["window"] = window
    return sample


def cmd_sample(arguments: argparse.Namespace, hooks=None) -> int:
    """Minute loop; hooks injects (db, metrics_fetch, watchdog_check, clock)."""
    run_dir = _resolve_run_dir(arguments.run_dir)
    run = _load_run(run_dir)
    hooks = hooks or {}
    db = hooks.get("db")
    fetch_metrics = hooks.get("fetch_metrics", fetch_runtime_metrics)
    watchdog_check = hooks.get("watchdog_check", lambda run: None)
    clock = hooks.get("clock", time.monotonic_ns)
    mode = MODES.get(run.get("mode", "live-day"), MODES["live-day"])
    max_slots = hooks.get("max_slots", mode["slots"])
    core_start = _parse_utc(run["core_start"])
    samples_dir = run_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    start_mono = clock()
    previous_metrics: dict | None = None
    previous_mono: int | None = None
    previous_wall: datetime | None = None
    failures = 0
    for index in range(max_slots):
        expected_utc = slot_expected_utc(core_start, index)
        captured_utc = hooks.get("now_utc", _utc_now)()
        now_mono = clock()
        mono_elapsed = (now_mono - start_mono) / 1e9
        mono_delta = 0.0 if previous_mono is None else (now_mono - previous_mono) / 1e9
        wall_delta = (0.0 if previous_wall is None
                      else (captured_utc - previous_wall).total_seconds())
        try:
            db_facts, db_error = (None, None)
            if db is not None:
                try:
                    db_facts = db.minute_snapshot(hooks.get("fixed_ids", []))
                except Exception as error:  # noqa: BLE001 - adapter failure is evidence
                    db_error = f"database_unavailable: {error}"
            else:
                db_error = "database_unavailable"
            metrics, metrics_error = None, None
            try:
                metrics = fetch_metrics(run["runtime_metrics_url"])
            except Step9Error as error:
                metrics_error = error.code
            admission_latest = None
            if db is not None and mode["admission"]:
                try:
                    latest = db.admission_latest(run["run_id"])
                except Exception:  # noqa: BLE001 - latest-evidence miss is data
                    latest = None
                if latest is not None:
                    mismatch = not (latest["selected_count"]
                                      == latest["inserted_count"]
                                      == latest["advanced_count"])
                    admission_latest = {"event_id": latest["id"],
                                        "database_at": str(latest["database_at"]),
                                        "mismatch": mismatch}
            sample = build_sample(
                run=run, index=index, expected_utc=expected_utc,
                captured_utc=captured_utc, mono_elapsed=mono_elapsed,
                wall_delta=wall_delta, mono_delta=mono_delta,
                boot_id=_boot_id(), db_facts=db_facts, db_error=db_error,
                metrics=metrics, metrics_error=metrics_error,
                previous_metrics=previous_metrics, pressure=host_pressure(),
                fs=filesystem_facts(run["spool_path"], run["postgres_path"]),
                watchdog_active=watchdog_check(run),
                admission_latest=admission_latest)
            if metrics is not None:
                previous_metrics = metrics
            name = f"minute-{index:04d}.json"
            if (samples_dir / name).exists():
                raise Step9Error("duplicate_sample", f"slot {index} already written",
                                 gate=True)
            _exclusive_json(samples_dir / name, sample)
            _check_capacity(run_dir)
            outcome = sample["outcome"]
            if outcome in ("clock_jump", "boot_change", "non_monotonic",
                           "admission_mismatch", "process_restart"):
                failures += 1
                if failures >= 2 or outcome == "process_restart":
                    _record_failure(run_dir, sample["failure_code"], outcome)
                    return 1
            elif outcome == "on_time":
                failures = 0
            if db_error is not None or metrics_error is not None:
                failures += 1
                if failures >= 2:
                    _record_failure(run_dir, "two_consecutive_unavailable",
                                    "two consecutive unavailable samples")
                    return 1
            else:
                failures = 0
        except Step9Error as error:
            _record_failure(run_dir, error.code, str(error))
            return 1 if error.gate else 2
        previous_mono = now_mono
        previous_wall = captured_utc
        if hooks.get("single_pass"):
            break
        target = start_mono + (index + 1) * SLOT_SECONDS * 1e9
        remaining = (target - clock()) / 1e9
        if remaining > 0 and not hooks.get("no_sleep"):
            time.sleep(min(remaining, SLOT_SECONDS))
    return 0

# --- finalize / validate -------------------------------------------------

def _read_samples(run_dir: Path) -> list[dict]:
    run = _load_run(run_dir)
    mode = MODES.get(run.get("mode", "live-day"), MODES["live-day"])
    slots = mode["slots"]
    samples_dir = run_dir / "samples"
    if not samples_dir.is_dir():
        raise Step9Error("samples_missing", "no samples directory")
    samples = []
    for index in range(slots):
        path = samples_dir / f"minute-{index:04d}.json"
        if not path.is_file() or path.is_symlink():
            raise Step9Error("sample_missing", f"missing minute sample {index}",
                             gate=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise Step9Error("sample_malformed",
                             f"unreadable minute sample {index}") from error
        if payload.get("slot") != index or payload.get("schema") != mode["schema"]:
            raise Step9Error("sample_mismatch", f"sample {index} identity mismatch",
                             gate=True)
        samples.append(payload)
    extras = sorted(p.name for p in samples_dir.glob("minute-*.json")
                    if p.name > f"minute-{slots - 1:04d}.json")
    if extras:
        raise Step9Error("sample_overflow",
                         f"more than {slots} core samples", gate=True)
    return samples


def _plus_minutes(iso: str, minutes: int) -> str:
    return (_parse_utc(iso) + timedelta(minutes=minutes)).isoformat()


def _finalize_admission(run: dict, db: object | None) -> dict:
    """Full admission reconciliation for live-day; preflight is N/A."""
    if not MODES.get(run.get("mode", "live-day"), MODES["live-day"])["admission"]:
        return {"status": "not_applicable"}
    if db is None:
        return {"status": "unknown", "failure_code": "database_unavailable"}
    try:
        if not db.admission_present():
            return {"status": "unknown",
                    "failure_code": "admission_schema_absent"}
        header = db.admission_run(run["run_id"])
        if header is None:
            return {"status": "unknown",
                    "failure_code": "admission_run_missing"}
        events = db.admission_events(run["run_id"], run["core_start"],
                                     _plus_minutes(run["core_end"], 10))
        profile_counts = db.admission_profile_counts(
            run["run_id"], run["core_start"], _plus_minutes(run["core_end"], 10))
        tail_end = _parse_utc(run["core_end"]) + DEADLINE_ALLOWANCE * 2
        roots = db.semantic_roots(run["core_start"], tail_end.isoformat())
        result = evaluate_admission(
            run=run, header=header, events=events,
            profile_counts=profile_counts, roots=roots,
            max_gap_seconds=run.get("max_invocation_gap_seconds", 5))
        result["status"] = ("complete" if not result["failures"]
                              and not result["unknown"] else "failed")
        result["run_state"] = header["state"]
        return result
    except Step9Error as error:
        return {"status": "unknown", "failure_code": error.code}
    except Exception as error:  # noqa: BLE001 - DB failure is evidence
        return {"status": "unknown",
                "failure_code": f"database_unavailable: {error}"}


def _finalize_preflight(run: dict, db: object | None) -> dict:
    """Envelope reconciliation for preflight; live-day is N/A."""
    if MODES.get(run.get("mode", "live-day"), MODES["live-day"])["admission"]:
        return {"status": "not_applicable"}
    if db is None:
        return {"status": "unknown", "failure_code": "database_unavailable"}
    try:
        probes = db.preflight_probes(run["core_start"], run["core_end"])
        result = evaluate_preflight_envelope(
            workcounts=probes["workcounts"],
            observations=probes["observations"], intents=probes["intents"])
        result["pending_remote_verification"] = probes["pending_remote"]
        if probes["pending_remote"]:
            result["failures"] = sorted(set(result["failures"]) |
                                          {"preflight_pending_remote"})
        residue = db.active_queues()
        result["queue_residue"] = {
            "collector": [(s, int(c)) for s, c, _m in residue["collector"]],
            "python": [(s, int(c)) for s, c, _m in residue["python"]]}
        if any(int(c) for _s, c, _m in residue["collector"] + residue["python"]):
            result["failures"] = sorted(set(result["failures"]) |
                                          {"preflight_queue_residue"})
        result["status"] = "complete" if not result["failures"] else "failed"
        return result
    except Step9Error as error:
        return {"status": "unknown", "failure_code": error.code}
    except Exception as error:  # noqa: BLE001 - DB failure is evidence
        return {"status": "unknown",
                "failure_code": f"database_unavailable: {error}"}


def cmd_finalize(arguments: argparse.Namespace, hooks=None) -> int:
    run_dir = _resolve_run_dir(arguments.run_dir)
    run = _load_run(run_dir)
    mode_name = run.get("mode", "live-day")
    mode = MODES.get(mode_name, MODES["live-day"])
    hooks = hooks or {}
    try:
        samples = _read_samples(run_dir)
        windows = [s for s in samples if "window" in s]
        if len(windows) != mode["windows"]:
            raise Step9Error("window_count",
                             f"expected {mode['windows']} windows, found {len(windows)}",
                             gate=True)
        missing = [s["slot"] for s in samples if s.get("outcome") != "on_time"]
        resets: dict = {"status": "unknown", "failure_code": "database_unavailable"}
        transitions: dict = {"status": "unknown",
                             "failure_code": "database_unavailable"}
        db = hooks.get("db")
        if db is not None:
            try:
                rows = db.reset_identity(run["core_start"], run["core_end"])
                resets = {"status": "captured", "boundaries": len(rows),
                          "safe_handoffs": sum(1 for r in rows if r[4]),
                          "nonterminal_reset_jobs": sum(int(r[7]) for r in rows)}
                if len(rows) != 1 or not rows[0][4]:
                    resets["failure_code"] = "reset_handoff_unproven"
            except Exception as error:  # noqa: BLE001 - DB failure is evidence
                resets = {"status": "unknown",
                          "failure_code": f"database_unavailable: {error}"}
        final = {
            "schema": mode["schema"], "mode": mode_name,
            "run_id": run["run_id"],
            "finalized_at": _utc_now().isoformat(),
            "core_slots": len(samples),
            "core_windows": len(windows),
            "non_on_time_slots": missing,
            "reset": resets, "transitions": transitions,
            "admission": _finalize_admission(run, db),
            "preflight": _finalize_preflight(run, db),
            "operating": hooks.get("operating", {"status": "unknown",
                                  "failure_code": "operating_unavailable"}),
            "filesystem": filesystem_facts(run["spool_path"], run["postgres_path"]),
            "failure_codes": sorted({s.get("failure_code") for s in samples
                                     if s.get("failure_code")}),
        }
        _exclusive_json(run_dir / "final.json", final)
        _write_manifest(run_dir)
        _check_capacity(run_dir)
        return 0
    except Step9Error as error:
        _record_failure(run_dir, error.code, str(error))
        return 1 if error.gate else 2


def _write_manifest(run_dir: Path) -> dict:
    entries = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == "manifest.json" or ".tmp" in path.name:
            continue
        rel = str(path.relative_to(run_dir))
        data = path.read_bytes()
        entries.append({"path": rel, "bytes": len(data),
                        "sha256": _sha256(data),
                        "role": ("sample" if path.parent.name == "samples"
                                 else "evidence")})
    manifest = {"schema": _load_run(run_dir).get("schema", SCHEMA),
                "run_id": _load_run(run_dir)["run_id"],
                "created_at": _utc_now().isoformat(), "artifacts": entries,
                "admission": _load_run(run_dir).get("admission")}
    manifest["manifest_digest"] = _digest(
        {"artifacts": entries, "run_id": manifest["run_id"]})
    _exclusive_json(run_dir / "manifest.json", manifest)
    return manifest


def cmd_validate(arguments: argparse.Namespace) -> int:
    run_dir = _resolve_run_dir(arguments.run_dir)
    try:
        run = _load_run(run_dir)
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise Step9Error("manifest_missing", "manifest.json is missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        listed = {e["path"]: e for e in manifest.get("artifacts", [])}
        actual = set()
        for path in sorted(run_dir.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name == "manifest.json":
                continue
            if ".tmp" in path.name:
                raise Step9Error("partial_tempfile", f"partial temp file: {path.name}")
            rel = str(path.relative_to(run_dir))
            actual.add(rel)
            entry = listed.get(rel)
            if entry is None:
                raise Step9Error("unlisted_artifact", f"unlisted artifact: {rel}")
            data = path.read_bytes()
            if len(data) != entry["bytes"] or _sha256(data) != entry["sha256"]:
                raise Step9Error("digest_mismatch", f"digest mismatch: {rel}")
        missing = set(listed) - actual
        if missing:
            raise Step9Error("artifact_missing", f"missing artifacts: {sorted(missing)}")
        if manifest.get("manifest_digest") != _digest(
                {"artifacts": manifest["artifacts"], "run_id": manifest["run_id"]}):
            raise Step9Error("digest_mismatch", "manifest digest mismatch")
        samples = _read_samples(run_dir)
        mode = MODES.get(run.get("mode", "live-day"), MODES["live-day"])
        windows = [s for s in samples if "window" in s]
        if len(windows) != mode["windows"]:
            raise Step9Error("window_count",
                             f"expected exactly {mode['windows']} core windows",
                             gate=True)
        if any(s.get("run_id") != run["run_id"] for s in samples):
            raise Step9Error("stitched_run", "samples carry mixed run IDs", gate=True)
        slots = [s["slot"] for s in samples]
        if slots != list(range(mode["slots"])):
            raise Step9Error("sample_order",
                             f"samples are not exactly 0..{mode['slots'] - 1}",
                             gate=True)
        admission = run.get("admission", {})
        if mode["admission"] and admission.get("status") != "integrated":
            raise Step9Error("admission_unproven",
                             "live-day run lacks integrated admission evidence")
        final_path = run_dir / "final.json"
        if not final_path.is_file():
            raise Step9Error("final_missing", "final.json is missing")
        cohort = run.get("cohort", {})
        if cohort.get("path"):
            try:
                _tags, raw_sha, _canonical = _read_cohort(cohort["path"])
            except Step9Error as error:
                raise Step9Error("cohort_changed",
                                 f"cohort unreadable at validate: {error.code}")
            if raw_sha != cohort.get("raw_sha256"):
                raise Step9Error("cohort_changed",
                                 "cohort file changed after start")
        final = json.loads(final_path.read_text(encoding="utf-8"))
        admission_result = final.get("admission", {})
        if mode["admission"]:
            if admission_result.get("status") != "complete":
                raise Step9Error("admission_unproven",
                                 "final.json lacks complete admission accounting",
                                 gate=True)
            if admission_result.get("failures") or admission_result.get("unknown"):
                raise Step9Error("admission_gate",
                                 f"admission failures: {admission_result.get('failures')} "
                                 f"unknown: {admission_result.get('unknown')}",
                                 gate=True)
        preflight = final.get("preflight", {})
        if not mode["admission"]:
            if preflight.get("status") != "complete":
                raise Step9Error("preflight_unproven",
                                 "final.json lacks complete preflight accounting",
                                 gate=True)
            if preflight.get("failures"):
                raise Step9Error("preflight_gate",
                                 f"preflight failures: {preflight.get('failures')}",
                                 gate=True)
        reset = final.get("reset", {})
        if (mode["admission"]
                and reset.get("failure_code") == "reset_handoff_unproven"):
            raise Step9Error("reset_handoff_unproven", "reset handoff is unproven",
                             gate=True)
        print(json.dumps({"run_id": run["run_id"], "slots": len(samples),
                          "windows": len(windows), "verdict": "valid"}, indent=1))
        return 0
    except Step9Error as error:
        print(f"step9 validate: {error.code}: {error}", file=sys.stderr)
        return 1 if error.gate else 2
    except (OSError, json.JSONDecodeError) as error:
        print(f"step9 validate: evidence_unavailable: {error}", file=sys.stderr)
        return 2

# --- watchdog ------------------------------------------------------------

def _podman_run(command: list[str]) -> str:
    import subprocess

    try:
        completed = subprocess.run(command, check=False, capture_output=True,
                                     text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"podman unavailable: {error}") from error
    if completed.returncode != 0:
        raise RuntimeError(f"podman failed: {completed.stderr.strip()[:200]}")
    return completed.stdout


class Podman:
    """Exact-container stop adapter; all process/time access injectable."""

    def __init__(self, run=None, podman_bin: str = "podman") -> None:
        self._run = run or _podman_run
        self._bin = podman_bin

    def inspect_running(self, container: str) -> tuple[bool | None, str | None]:
        try:
            output = self._run([self._bin, "container", "inspect", "--format",
                                "{{.State.Running}}\n{{.Image}}", container])
        except Exception as error:  # noqa: BLE001 - stop adapter reports only
            return None, f"inspect_unavailable: {error}"
        lines = output.strip().splitlines()
        if len(lines) != 2:
            return None, "inspect_malformed"
        return lines[0].strip().lower() == "true", lines[1].strip()

    def disable_restart(self, container: str) -> str | None:
        try:
            self._run([self._bin, "update", "--restart=no", container])
        except Exception as error:  # noqa: BLE001 - stop adapter reports only
            return f"restart_disable_failed: {error}"
        return None

    def stop(self, container: str) -> str | None:
        try:
            self._run([self._bin, "stop", "--ignore", "--time", "30", container])
        except Exception as error:  # noqa: BLE001 - stop adapter reports only
            return f"stop_failed: {error}"
        running, _ = self.inspect_running(container)
        if running:
            return "container_still_running"
        if running is None:
            return "stop_unverified"
        return None


def cmd_watchdog(arguments: argparse.Namespace, hooks=None) -> int:
    hooks = hooks or {}
    run_dir = _resolve_run_dir(arguments.run_dir)
    run = _load_run(run_dir)
    collector = arguments.collector_container
    if run["containers"]["collector"] != collector:
        raise Step9Error("container_mismatch", "watchdog container != pinned collector")
    if not _CONTAINER.fullmatch(collector):
        raise Step9Error("bad_container", "invalid collector container name")
    deadline = _parse_utc(arguments.deadline)
    podman = Podman(hooks.get("podman_run"), arguments.podman_bin)
    running, image = podman.inspect_running(collector)
    if running is None:
        _record_failure(run_dir, "inspect_unavailable", str(image))
        return 2
    if not running:
        _record_failure(run_dir, "collector_not_running",
                        "collector is not running at watchdog start")
        return 1
    error = podman.disable_restart(collector)
    if error is not None:
        _record_failure(run_dir, "restart_disable_failed", error)
        return 1
    _exclusive_json(run_dir / "watchdog.json", {
        "schema": run.get("schema", SCHEMA), "run_id": run["run_id"],
        "collector": collector, "image": image,
        "prior_restart_policy": "recorded_by_operator",
        "verified_restart": "no", "started_at": _utc_now().isoformat(),
        "deadline": deadline.isoformat(),
        "max_sample_age_seconds": arguments.max_sample_age_seconds,
        "systemd_unit": arguments.systemd_unit,
    })
    now_utc = hooks.get("now_utc", _utc_now)
    max_iterations = hooks.get("max_iterations", 2**31)
    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        outcome = _watchdog_once(run_dir, run, podman, collector, deadline,
                                 arguments.max_sample_age_seconds, now_utc)
        if outcome is not None:
            _record_failure(run_dir, outcome, f"watchdog stop: {outcome}")
            stop_error = podman.stop(collector)
            result = {"schema": run.get("schema", SCHEMA),
                      "run_id": run["run_id"],
                      "stopped_at": now_utc().isoformat(), "trigger": outcome,
                      "stop_error": stop_error}
            try:
                _exclusive_json(run_dir / "watchdog-outcome.json", result)
            except Step9Error:
                pass
            return 1 if stop_error is None else 2
        if hooks.get("single_pass"):
            return 0
        time.sleep(WATCHDOG_POLL_SECONDS)
    return 0


def _watchdog_once(run_dir: Path, run: dict, podman: Podman, collector: str,
                   deadline: datetime, max_age: int, now_utc) -> str | None:
    now = now_utc()
    if now >= deadline:
        return "deadline_reached"
    running, _ = podman.inspect_running(collector)
    if running is None:
        return "inspect_unavailable"
    if not running:
        return "collector_stopped"
    samples = sorted((run_dir / "samples").glob("minute-*.json")) if (
        run_dir / "samples").is_dir() else []
    if not samples:
        return "no_samples"
    try:
        latest = json.loads(samples[-1].read_text(encoding="utf-8"))
        captured = _parse_utc(latest["captured_utc"])
    except (OSError, json.JSONDecodeError, KeyError, Step9Error):
        return "sample_unreadable"
    if (now - captured).total_seconds() > max_age:
        return "sample_stale"
    return None


# --- CLI -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="validate inputs and create the run header")
    _add_common(start)
    start.add_argument("--cohort-file", required=True)
    start.add_argument("--deployed-receipt", required=True)
    start.add_argument("--core-start", required=True)
    start.add_argument("--core-end", required=True)
    start.add_argument("--collector-container", required=True)
    start.add_argument("--postgres-container", required=True)
    start.add_argument("--python-api-container", required=True)
    start.add_argument("--python-worker-container", required=True)
    start.add_argument("--worker-replicas", type=int, default=1)
    start.add_argument("--runtime-metrics-url", required=True)
    start.add_argument("--spool-path", required=True)
    start.add_argument("--postgres-path", required=True)
    start.add_argument("--lead-in-seconds", type=int, default=0)
    start.add_argument("--tail-seconds", type=int, default=0)
    start.add_argument("--deadline", required=True)
    start.add_argument("--max-sample-age-seconds", type=int, default=125)
    start.add_argument("--watchdog-unit", required=True)
    start.add_argument("--run-id", default=None)
    start.add_argument("--database-url", default=None)
    start.add_argument("--mode", choices=sorted(MODES), default="live-day")
    start.add_argument("--max-invocation-gap-seconds", type=int, default=5)

    sample = sub.add_parser("sample", help="run the minute sampling loop")
    _add_common(sample)

    finalize = sub.add_parser("finalize", help="reconcile once and seal a manifest")
    _add_common(finalize)

    validate = sub.add_parser("validate",
                              help="validate a sealed run without traffic or DB")
    _add_common(validate)

    watchdog = sub.add_parser("watchdog", help="monitor freshness; stop the collector")
    _add_common(watchdog)
    watchdog.add_argument("--collector-container", required=True)
    watchdog.add_argument("--deadline", required=True)
    watchdog.add_argument("--max-sample-age-seconds", type=int, default=125)
    watchdog.add_argument("--systemd-unit", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    run_dir = _resolve_run_dir(arguments.run_dir) if arguments.run_dir else None
    try:
        if arguments.command == "start":
            db = None
            if arguments.database_url:
                import psycopg

                url = arguments.database_url
                db = Database(lambda: psycopg.connect(url))
            header = cmd_start(arguments, db)
            print(json.dumps({"run_id": header["run_id"],
                              "header_sha256": header["header_sha256"]}))
            return 0
        if arguments.command == "sample":
            return cmd_sample(arguments)
        if arguments.command == "finalize":
            return cmd_finalize(arguments)
        if arguments.command == "validate":
            return cmd_validate(arguments)
        if arguments.command == "watchdog":
            return cmd_watchdog(arguments)
    except Step9Error as error:
        _record_failure(run_dir, error.code, str(error))
        print(f"step9 {arguments.command}: {error.code}: {error}", file=sys.stderr)
        return 1 if error.gate else 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    sys.exit(main())

# --- Admission evidence (migration 0022-final read side) -------------------
# Table/column names follow the validated final handoff exactly. The observer
# creates nothing; missing tables fail closed (unknown, never zero).

SQL_ADMISSION_TABLES = """
SELECT to_regclass('collector_regular_admission_evidence_runs') IS NOT NULL
   AND to_regclass('collector_regular_admission_evidence') IS NOT NULL
"""

SQL_ADMISSION_RUN = """
SELECT run_id, capture_start, capture_end, max_events, max_selected_entries,
       events_written, selected_entries_written, state, stopped_at, failure_code
FROM collector_regular_admission_evidence_runs WHERE run_id = %s
"""

SQL_ADMISSION_EVENTS = """
SELECT id, invocation_id, cycle_at, scheduler_at, database_at,
       gate_allowed, gate_handoff_at, batch_limit,
       capture_start, capture_end,
       visible_due_count, visible_due_min_at,
       unselected_visible_due_count, unselected_visible_due_min_at,
       unselected_visible_past_deadline_count,
       unselected_visible_past_deadline_min_at,
       selected_past_deadline_count,
       selected_player_ids, selected_due_ats,
       selected_profile_version_ids, selected_eligibility_states,
       inserted_job_ids, advanced_count,
       cardinality(selected_player_ids), cardinality(inserted_job_ids)
FROM collector_regular_admission_evidence
WHERE run_id = %s AND cycle_at >= %s AND cycle_at < %s
ORDER BY database_at, id
"""

SQL_ADMISSION_PROFILE_CHECK = """
WITH events AS MATERIALIZED (
    SELECT id, selected_player_ids, selected_due_ats,
           selected_profile_version_ids, selected_eligibility_states
    FROM collector_regular_admission_evidence
    WHERE run_id = %s AND cycle_at >= %s AND cycle_at < %s
), selected AS (
    SELECT e.id AS event_id,
           s.player_id, d.due_at,
           p.profile_version_id, q.eligibility_state
    FROM events AS e
    CROSS JOIN LATERAL unnest(e.selected_player_ids)
         WITH ORDINALITY AS s(player_id, n)
    JOIN LATERAL unnest(e.selected_due_ats)
         WITH ORDINALITY AS d(due_at, n) ON d.n = s.n
    LEFT JOIN LATERAL unnest(e.selected_profile_version_ids)
         WITH ORDINALITY AS p(profile_version_id, n) ON p.n = s.n
    LEFT JOIN LATERAL unnest(e.selected_eligibility_states)
         WITH ORDINALITY AS q(eligibility_state, n) ON q.n = s.n
    WHERE s.player_id IS NOT NULL
)
SELECT s.event_id,
       count(*) FILTER (
           WHERE s.profile_version_id IS NULL
              OR v.player_id IS DISTINCT FROM s.player_id
              OR v.league_tier_id IS DISTINCT FROM 105000036
              OR v.league_tier_name IS DISTINCT FROM 'Legend I'
              OR v.eligibility_state IS DISTINCT FROM 'eligible'
              OR v.source_contract_state IS DISTINCT FROM 'accepted')
           AS invalid_selected_profile_count,
       count(*) AS selected_count
FROM selected AS s
LEFT JOIN player_profile_versions AS v ON v.id = s.profile_version_id
GROUP BY s.event_id
"""

SQL_ADMISSION_LATEST = """
SELECT id, database_at, gate_allowed,
       cardinality(selected_player_ids), cardinality(inserted_job_ids),
       advanced_count
FROM collector_regular_admission_evidence
WHERE run_id = %s
ORDER BY database_at DESC, id DESC LIMIT 1
"""

SQL_SEMANTIC_ROOTS = """
SELECT player_id, coalescing_key, id, status
FROM collector_jobs
WHERE work_type = 'regular_poll'
  AND created_at >= %s AND created_at < %s
"""

for _statement in (SQL_ADMISSION_TABLES, SQL_ADMISSION_RUN, SQL_ADMISSION_EVENTS,
                   SQL_ADMISSION_PROFILE_CHECK, SQL_ADMISSION_LATEST,
                   SQL_SEMANTIC_ROOTS):
    assert_read_only(_statement)

ALL_RO_STATEMENTS += (SQL_ADMISSION_TABLES, SQL_ADMISSION_RUN,
                      SQL_ADMISSION_EVENTS, SQL_ADMISSION_PROFILE_CHECK,
                      SQL_ADMISSION_LATEST, SQL_SEMANTIC_ROOTS)

DEADLINE_ALLOWANCE = timedelta(minutes=5)


def _event_selected_due(event: dict) -> list:
    return event.get("selected_due_ats") or []


def evaluate_admission(*, run: dict, header: dict, events: list[dict],
                       profile_counts: dict, roots: list[tuple],
                       max_gap_seconds: int) -> dict:
    """Pure admission validator implementing the final-handoff semantics."""
    failures: list[str] = []
    unknown: list[str] = []
    windows: dict[int, dict] = {}
    core_start = _parse_utc(run["core_start"])
    mode = MODES["preflight"] if run.get("mode") == "preflight" else MODES["live-day"]

    if header.get("state") not in ADMISSION_RUN_STATES:
        failures.append("admission_run_state_invalid")
    if header.get("state") == "capacity_exceeded":
        failures.append("admission_evidence_capacity_exceeded")
    if header.get("state") == "capture_out_of_range":
        failures.append("admission_evidence_capture_out_of_range")
    if (header.get("state") != "active"
            and header.get("failure_code") not in ADMISSION_FAILURE_CODES):
        failures.append("admission_failure_code_unknown")
    previous_at = None
    for event in events:
        cycle_at = event["cycle_at"]
        database_at = event["database_at"]
        if (event.get("capture_start") != header["capture_start"]
                or event.get("capture_end") != header["capture_end"]):
            failures.append("admission_capture_mismatch")
        if not (header["capture_start"] <= database_at < header["capture_end"]):
            failures.append("admission_event_out_of_range")
        if (previous_at is not None and max_gap_seconds is not None
                and (database_at - previous_at).total_seconds()
                > max_gap_seconds):
            unknown.append("admission_visibility_unknown")
        previous_at = database_at
        selected = event["selected_count"]
        inserted = event["inserted_count"]
        if not (selected == inserted == event["advanced_count"]):
            failures.append("admission_count_mismatch")
        invalid = profile_counts.get(event["id"], (0, selected))[0]
        if invalid:
            failures.append("invalid_selected_profile")
        window_index = int((cycle_at - core_start).total_seconds() // 300)
        slot = windows.setdefault(window_index, {"late": 0, "unknown": 0})
        if event["selected_past_deadline_count"]:
            failures.append("admission_past_deadline_selected")
            slot["late"] += event["selected_past_deadline_count"]
        if event["unselected_visible_past_deadline_count"]:
            failures.append("admission_past_deadline_unselected")
            slot["late"] += event["unselected_visible_past_deadline_count"]
        if event["gate_allowed"]:
            for due_at in _event_selected_due(event):
                if database_at > due_at + DEADLINE_ALLOWANCE:
                    failures.append("admission_selected_late_recomputed")
                    slot["late"] += 1
                    break
    handoff_at = header.get("handoff_at")
    tail_need = max(core_start + mode["interval"],
                    handoff_at or core_start) + DEADLINE_ALLOWANCE
    if previous_at is None or previous_at < tail_need:
        unknown.append("admission_tail_insufficient")
    root_check = reconcile_semantic_roots(run, events, roots)
    failures.extend(root_check["failures"])
    return {"failures": sorted(set(failures)),
            "unknown": sorted(set(unknown)),
            "events": len(events),
            "windows": {str(k): v for k, v in sorted(windows.items())},
            "roots": root_check["summary"]}


def reconcile_semantic_roots(run: dict, events: list[dict],
                             roots: list[tuple]) -> dict:
    """Exact-root reconciliation from semantic (player_id, coalescing key)."""
    failures: list[str] = []
    selected: dict[tuple, int] = {}
    inserted_ids: set = set()
    for event in events:
        for player in event.get("selected_player_ids") or []:
            if player is None:
                failures.append("admission_null_selected_player")
                continue
            cycle = event["cycle_at"]
            key = f"regular:{player}:{int(cycle.timestamp())}"
            selected[(player, key)] = event["id"]
        for job in event.get("inserted_job_ids") or []:
            inserted_ids.add(job)
    by_identity: dict[tuple, list] = {}
    for player_id, key, job_id, _status in roots:
        parts = (key or "").split(":")
        if len(parts) != 3 or parts[0] != "regular":
            failures.append("admission_malformed_coalescing_key")
            continue
        try:
            if parts[1] != str(int(player_id)) or int(parts[2]) < 0:
                failures.append("admission_malformed_coalescing_key")
                continue
        except (TypeError, ValueError):
            failures.append("admission_malformed_coalescing_key")
            continue
        by_identity.setdefault((player_id, key), []).append(job_id)
    for identity in selected:
        matches = by_identity.get(identity, [])
        if len(matches) != 1:
            failures.append("admission_root_count")
        elif matches[0] not in inserted_ids:
            failures.append("admission_root_identity")
    for identity in by_identity:
        if identity not in selected:
            failures.append("admission_unexplained_regular_root")
    return {"failures": sorted(set(failures)),
            "summary": {"selected_identities": len(selected),
                        "retained_identities": len(by_identity)}}


SQL_PREFLIGHT_WORKCOUNTS = """
SELECT work_type, status, count(*)
FROM collector_jobs
WHERE created_at >= %s AND created_at < %s
GROUP BY work_type, status
"""

SQL_PREFLIGHT_OBSERVATIONS = """
SELECT endpoint, count(*)
FROM collector_observations
WHERE created_at >= %s AND created_at < %s
GROUP BY endpoint
"""

SQL_PREFLIGHT_RANKING_INTENTS = """
SELECT count(*), min(cycle_at), max(cycle_at)
FROM global_rankings_intents
WHERE cycle_at >= %s AND cycle_at < %s
"""

SQL_PREFLIGHT_PENDING_REMOTE = """
SELECT count(*)
FROM collector_endpoint_results
WHERE pending_remote_verification IS NOT NULL
"""

for _statement in (SQL_PREFLIGHT_WORKCOUNTS, SQL_PREFLIGHT_OBSERVATIONS,
                   SQL_PREFLIGHT_RANKING_INTENTS, SQL_PREFLIGHT_PENDING_REMOTE):
    assert_read_only(_statement)

ALL_RO_STATEMENTS += (SQL_PREFLIGHT_WORKCOUNTS, SQL_PREFLIGHT_OBSERVATIONS,
                      SQL_PREFLIGHT_RANKING_INTENTS, SQL_PREFLIGHT_PENDING_REMOTE)


def evaluate_preflight_envelope(*, workcounts: list[tuple],
                                observations: list[tuple],
                                intents: tuple) -> dict:
    """Check measured bootstrap traffic against the fixed envelope."""
    failures: list[str] = []
    work: dict = {}
    for work_type, status, count in workcounts:
        work.setdefault(work_type, {})[status] = int(count)
        if work_type not in PREFLIGHT_ALLOWED_WORK:
            failures.append("unexpected_scheduler_traffic")
    endpoints = {endpoint: int(count) for endpoint, count in observations}
    if endpoints.get("profile", 0) > PREFLIGHT_ENVELOPE["profile"]:
        failures.append("preflight_profile_budget_exceeded")
    if endpoints.get("battle_log", 0) > PREFLIGHT_ENVELOPE["battle_log"]:
        failures.append("preflight_unexpected_battle_traffic")
    if int(intents[0]) > PREFLIGHT_ENVELOPE["global_rankings_intents"]:
        failures.append("preflight_rankings_budget_exceeded")
    return {"failures": sorted(set(failures)), "work": work,
            "endpoints": endpoints,
            "ranking_intents": {"count": int(intents[0]),
                                "min_cycle_at": str(intents[1]),
                                "max_cycle_at": str(intents[2])},
            "budget_ledger": "unknown_pending_0023"}
