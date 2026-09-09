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
import math
import os
import re
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "python" / "src")]

from scripts import deployment_receipt

from clashlens.profile import normalize_player_tag

try:
    from clashlens.operating import RELATION_NAMES as _OPERATING_RELATIONS
except ImportError:  # pragma: no cover - production always has the module
    _OPERATING_RELATIONS = ()

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
                    "interval": timedelta(minutes=75), "require_0500": False,
                    "admission": False,
                    "drain_slots": 15, "bootstrap_slots": 60},
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
    "endpoint_retry",
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


def _exclusive_json(destination: Path, payload: dict,
                    max_bytes: int | None = None) -> str:
    """Write one artifact exclusively (O_EXCL + fsync + dir fsync); return SHA."""
    destination = destination.absolute()
    if destination.is_symlink():
        raise Step9Error("artifact_occupied", "artifact path must not be a symlink")
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    data = text.encode()
    if max_bytes is not None and len(data) > max_bytes:
        raise Step9Error("artifact_capacity_exceeded",
                         "artifact exceeds its byte cap")
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


def _record_failure(run_dir: Path | None, code: str, message: str = "") -> None:
    """Durable fixed-code failure record; raw messages are never retained."""
    if run_dir is None:
        return
    try:
        failures = run_dir / "failures"
        failures.mkdir(parents=True, exist_ok=True)
        stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
        (_exclusive_json(failures / f"{code}-{stamp}.json",
                         {"code": code, "at": stamp}))
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
SELECT (SELECT count(*) FROM collector_jobs AS j
        WHERE j.work_type = ANY(%s::text[])
          AND j.player_id IS NOT NULL
          AND NOT (j.player_id = ANY(%s::bigint[])))
     + (SELECT count(*) FROM collector_jobs AS r
        JOIN collector_attempts AS a ON a.id = r.parent_attempt_id
        JOIN collector_jobs AS p ON p.id = a.job_id
        WHERE r.work_type = 'endpoint_retry'
          AND p.player_id IS NOT NULL
          AND NOT (p.player_id = ANY(%s::bigint[])))
"""

SQL_RESET_MEMBERS = """
SELECT m.player_id FROM collector_reset_sweep_members AS m
WHERE m.sweep_id = %s ORDER BY m.player_id
"""

SQL_GENERATION_MEMBERS = """
SELECT m.player_id, g.expected_population_count,
       g.expected_population_hash, g.generation
FROM boundary_publication_generation_members AS m
JOIN boundary_publication_generations AS g ON g.id = m.generation_id
WHERE g.sweep_id = %s AND g.boundary_at = %s
ORDER BY m.player_id
"""

SQL_PAIRED_BASELINES = """
SELECT m.player_id, count(DISTINCT j.id) AS roots
FROM collector_reset_sweep_members AS m
LEFT JOIN collector_jobs AS j
  ON j.player_id = m.player_id
 AND j.work_type IN ('reset_baseline', 'legacy_reset_profile')
 AND j.sweep_id = m.sweep_id
WHERE m.sweep_id = %s
GROUP BY m.player_id
"""

SQL_WAL_GENERATED = """
SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s::pg_lsn),
       pg_database_size(current_database())
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
    budget_receipt = _budget_receipt_block(receipt, mode_name)
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
    if db is None:
        raise Step9Error("database_required",
                         "start requires a database connection")
    podman_bin = getattr(arguments, "podman_bin", "podman")
    collector_image: str | None = None
    collector_image_error = "image_pin_unattempted"
    postgres_image: str | None = None
    postgres_image_error = "image_pin_unattempted"
    try:
        running, pinned = Podman(None, podman_bin).inspect_running(
            arguments.collector_container)
        if running:
            collector_image, collector_image_error = pinned, None
        else:
            collector_image_error = "collector_not_running_at_start"
    except Exception as error:  # noqa: BLE001 - unpinnable image is unknown
        collector_image_error = f"image_inspect_unavailable: {type(error).__name__}"
    try:
        pg_running, pg_pinned = Podman(None, podman_bin).inspect_running(
            arguments.postgres_container)
        if pg_running:
            postgres_image, postgres_image_error = pg_pinned, None
        else:
            postgres_image_error = "postgres_not_running_at_start"
    except Exception as error:  # noqa: BLE001 - unpinnable image is unknown
        postgres_image_error = f"image_inspect_unavailable: {type(error).__name__}"
    initial = {"status": "unknown", "failure_code": "database_unavailable"}
    try:
        initial = _capture_initial(db, tags, mode_name)
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
            "collector_image": collector_image,
            "collector_image_error": collector_image_error,
            "postgres": arguments.postgres_container,
            "postgres_image": postgres_image,
            "postgres_image_error": postgres_image_error,
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
        "max_invocation_gap_seconds": _require_gap_seconds(arguments),
        "podman_bin": getattr(arguments, "podman_bin", "podman"),
        "database_url_source": ("file" if getattr(
            arguments, "database_url_file", None) else (
                "argv" if getattr(arguments, "database_url", None)
                else "none")),
        "bootstrap_run_id": getattr(arguments, "bootstrap_run_id", None),
        "budget_receipt": budget_receipt,
        "cost_basis": {
            "note": "tariff estimate only, never actual billed cost",
            "tariff": _tariff_block(
                _read_tariff_file(getattr(arguments, "archive_tariff_file",
                                          None) or ""),
                core_start),
        },
        "archive_interfaces": list(getattr(arguments, "archive_interfaces",
                                            None) or []),
        "archive_route_host": getattr(arguments, "archive_route_host", None),
        "transfer_prior_bytes": (
            getattr(arguments, "prior_transfer_bytes", None)
            if getattr(arguments, "prior_transfer_bytes", None) is not None
            else TRANSFER_PRIOR_BYTES),
        "transfer_prior_provenance": (
            getattr(arguments, "prior_transfer_provenance", None)
            or TRANSFER_PRIOR_PROVENANCE),
        "s3_prior": _s3_prior_block(arguments),
        "resource_baseline": collect_resource_facts(
            spool_path=arguments.spool_path,
            postgres_path=arguments.postgres_path, db=db, metrics=None),        "filesystem": filesystem_facts(arguments.spool_path,
                                        arguments.postgres_path),
        "admission": _admission_header(db, mode_name, run_id, core_start,
                                        core_end),
        "admission_receipt": _admission_receipt_block(
            receipt, mode_name, run_id, core_start, core_end,
            getattr(arguments, "lead_in_seconds", 0),
            getattr(arguments, "tail_seconds", 0)),
        "script_sha256": _sha256(Path(__file__).read_bytes()),
    }
    try:
        header["resource_baseline"]["cgroup"] = _cgroup_numbers(
            getattr(arguments, "podman_bin", "podman"),
            arguments.collector_container)
    except Exception:  # noqa: BLE001 - baseline miss stays unknown
        header["resource_baseline"]["cgroup"] = {"oom_kills": None,
            "swap_current_bytes": None, "mem_current_bytes": None,
            "mem_peak_bytes": None, "error": "baseline_unavailable"}
    header["pgdata_baseline"] = _pgdata_probe(
        podman_bin, arguments.postgres_container, postgres_image)
    if not header["archive_interfaces"]:
        raise Step9Error("archive_interface_missing",
                         "at least one archive egress interface is required")
    header["wire_baseline"] = collect_wire_facts(
        interfaces=header["archive_interfaces"],
        route_host=header["archive_route_host"])
    digest = _exclusive_json(run_dir / "run.json", header)
    try:
        os.chmod(run_dir / "run.json", 0o600)
    except OSError as error:
        raise Step9Error("run_unwritable", "run.json cannot be secured") from error
    if db is not None:
        try:
            _exclusive_json(run_dir / "operating-baseline.json",
                            {"schema": mode["schema"], "run_id": run_id,
                             "captured_at": _utc_now().isoformat(),
                             "snapshot": db.operating_snapshot(
                                 list(_OPERATING_RELATIONS))})
        except Step9Error:
            raise
        except Exception as error:  # noqa: BLE001 - capture miss is evidence
            _exclusive_json(run_dir / "operating-baseline.json",
                            {"schema": mode["schema"], "run_id": run_id,
                             "status": "unknown",
                             "failure_code": type(error).__name__})
    _check_capacity(run_dir)
    header["header_sha256"] = digest
    return header


def _admission_receipt_block(receipt: dict, mode_name: str, run_id: str,
                             core_start: datetime, core_end: datetime,
                             lead_in: int, tail: int) -> dict | None:
    """Live-day pins the deployed admission evidence config; fail closed."""
    if mode_name != "live-day":
        return None
    fields = receipt.get("configuration", {}).get("fields", {})
    evidence_id = fields.get("admission_evidence_run_id", "")
    start = fields.get("admission_evidence_start", "")
    end = fields.get("admission_evidence_end", "")
    if evidence_id == "disabled":
        raise Step9Error("admission_disabled",
                         "live-day requires enabled admission evidence")
    if evidence_id != run_id:
        raise Step9Error("admission_run_mismatch",
                         "receipt admission run ID differs from observer run")
    capture_start = core_start - timedelta(seconds=lead_in or 0)
    capture_end = core_end + timedelta(seconds=tail or 0)
    try:
        want_start = _parse_utc(start)
        want_end = _parse_utc(end)
    except Step9Error as error:
        raise Step9Error("admission_interval_malformed",
                         "receipt admission interval is invalid") from error
    if want_start != capture_start or want_end != capture_end:
        raise Step9Error("admission_interval_mismatch",
                         "receipt admission interval differs from run capture")
    try:
        quotas = (int(fields.get("admission_evidence_max_events", "-1")),
                  int(fields.get("admission_evidence_max_selected_entries",
                                 "-1")))
    except (TypeError, ValueError) as error:
        raise Step9Error("admission_quota_malformed",
                         "receipt admission quotas are invalid") from error
    return {"run_id": evidence_id, "capture_start": start,
            "capture_end": end, "max_events": quotas[0],
            "max_selected_entries": quotas[1]}


def _budget_receipt_block(receipt: dict, mode_name: str) -> dict | None:
    """Preflight pins the deployed budget envelope; live-day ignores it."""
    if mode_name != "preflight":
        return None
    fields = receipt.get("configuration", {}).get("fields", {})
    if fields.get("endpoint_budget_enabled") != "true":
        raise Step9Error("budget_not_enabled",
                         "preflight requires endpoint_budget_enabled=true")
    try:
        caps = {key: int(fields[key]) for key in (
            "endpoint_budget_profile", "endpoint_budget_global_rankings",
            "endpoint_budget_battle_log")}
    except (KeyError, TypeError, ValueError) as error:
        raise Step9Error("budget_malformed",
                         "preflight budget caps are invalid") from error
    envelope = {"endpoint_budget_profile": PREFLIGHT_ENVELOPE["profile"],
                "endpoint_budget_global_rankings":
                    PREFLIGHT_ENVELOPE["global_rankings_intents"],
                "endpoint_budget_battle_log": PREFLIGHT_ENVELOPE["battle_log"]}
    for key, ceiling in envelope.items():
        if caps[key] > ceiling:
            raise Step9Error("budget_exceeds_envelope",
                             f"preflight {key} exceeds the fixed envelope")
    if not fields.get("endpoint_budget_run_id") \
            or not fields.get("endpoint_budget_deadline_at"):
        raise Step9Error("budget_unbound",
                         "preflight budget run ID and deadline are required")
    return {"caps": caps,
            "run_id": fields["endpoint_budget_run_id"],
            "deadline_at": fields["endpoint_budget_deadline_at"]}


def _require_gap_seconds(arguments: argparse.Namespace) -> int:
    """Scheduler-cadence evidence bound; fail closed when not explicit."""
    value = getattr(arguments, "max_invocation_gap_seconds", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Step9Error("invocation_gap_unpinned",
                         "--max-invocation-gap-seconds is required")
    return value


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


def _capture_initial(db: object, tags: list[str], mode_name: str = "live-day") -> dict:
    rows = db.snapshot_population(tags)
    eligible: list[int] = []
    matched: list[int] = []
    counts = {"eligible": 0, "ineligible_or_inactive": 0,
              "not_legend_one": 0, "profile_disagree": 0, "unmatched": 0}
    seen = set()
    for row in rows:
        seen.add(row[1])
        matched.append(row[0])
        bucket = _classify_eligible(row)
        counts[bucket] = counts.get(bucket, 0) + 1
        if bucket == "eligible":
            eligible.append(row[0])
    counts["unmatched"] = len(tags) - len(seen)
    outside = db.outside_active(matched)
    if outside:
        raise Step9Error(
            "foreign_population",
            f"{outside} active players outside the supplied file", gate=True)
    if db.outside_roots(matched):
        raise Step9Error(
            "foreign_lineage", "outside player-scoped collection lineage exists",
            gate=True)
    if not eligible:
        if mode_name == "preflight":
            return {
                "status": "pending_bootstrap",
                "eligible_count": 0,
                "eligible_digest": _eligible_digest([]),
                "counts": counts,
                "database_identity": db.identity(),
            }
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
    shift = (captured_utc - expected_utc).total_seconds()
    jump = abs(wall_delta - mono_delta)
    if boot_changed:
        outcome, failure = "boot_change", "boot_id_changed"
    elif jump > 2.0:
        outcome, failure = "clock_jump", "clock_jump"
    elif mono_delta < 0:
        outcome, failure = "non_monotonic", "non_monotonic_time"
    elif wall_delta < 0:
        outcome, failure = "out_of_order", "sample_out_of_order"
    elif shift > late_allowance:
        outcome, failure = "late", "sample_late"
    else:
        outcome, failure = "on_time", None
    return {"outcome": outcome, "failure_code": failure,
            "wall_delta_seconds": wall_delta, "mono_delta_seconds": mono_delta,
            "clock_jump_seconds": jump, "shift_seconds": shift}


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
            cursor.execute(SQL_OUTSIDE_ROOTS,
                           (list(PLAYER_SCOPED_WORK), ids, ids))
            return int(cursor.fetchone()[0])
        return self._one_txn(work)

    def reset_members(self, sweep_id: int) -> list[int]:
        def work(cursor):
            cursor.execute(SQL_RESET_MEMBERS, (sweep_id,))
            return [row[0] for row in cursor.fetchall()]
        return self._one_txn(work)

    def generation_members(self, sweep_id: int, boundary: str) -> list[tuple]:
        def work(cursor):
            cursor.execute(SQL_GENERATION_MEMBERS, (sweep_id, boundary))
            return list(cursor.fetchall())
        return self._one_txn(work)

    def paired_baselines(self, sweep_id: int) -> dict:
        def work(cursor):
            cursor.execute(SQL_PAIRED_BASELINES, (sweep_id,))
            return {row[0]: row[1] for row in cursor.fetchall()}
        return self._one_txn(work)

    def wal_generated(self, since_lsn: str) -> tuple:
        def work(cursor):
            cursor.execute(SQL_WAL_GENERATED, (since_lsn,))
            return tuple(cursor.fetchone())
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
        except Exception as error:
            if self._missing_table(error):
                return False
            raise

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

    def budgets_present(self) -> bool:
        def work(cursor):
            cursor.execute(SQL_0023_TABLES)
            return bool(cursor.fetchone()[0])
        try:
            return self._one_txn(work)
        except Exception as error:
            if self._missing_table(error):
                return False
            raise

    def bootstrap_budgets(self, run_id: str) -> dict:
        def work(cursor):
            cursor.execute(SQL_BOOTSTRAP_RUN, (run_id,))
            row = cursor.fetchone()
            if row is None:
                return {"run": None, "budgets": []}
            keys = ("run_id", "manifest_sha256", "manifest_count",
                    "normalized_set_sha256", "status", "batch_size",
                    "players_registered", "discovery_jobs_created",
                    "created_at", "completed_at")
            run_row = dict(zip(keys, row))
            cursor.execute(SQL_ENDPOINT_BUDGETS, (run_id,))
            budgets = [dict(zip(("endpoint", "cap", "consumed",
                                      "deadline_at", "updated_at"), r))
                         for r in cursor.fetchall()]
            return {"run": run_row, "budgets": budgets}
        return self._one_txn(work)

    def archive_usage(self) -> tuple:
        def work(cursor):
            cursor.execute(SQL_ARCHIVE_USAGE)
            return tuple(cursor.fetchone())
        return self._one_txn(work)

    def operating_snapshot(self, relations: list[str]) -> dict:
        """Sectioned worker-safe operating capture; sections fail closed."""
        snapshot: dict = {}

        def run_section(name, sql, params=None):
            def work(cursor):
                cursor.execute(sql, params or ())
                return list(cursor.fetchall())
            try:
                snapshot[name] = {"status": "complete",
                                  "rows": [list(r) for r in self._one_txn(work)]}
            except Exception as error:  # noqa: BLE001 - denial is evidence
                snapshot[name] = {"status": "unknown",
                                  "failure_code": type(error).__name__}
        run_section("identity", SQL_OP_IDENTITY)
        run_section("collector_queues", SQL_OP_QUEUES)
        run_section("python_queues", SQL_OP_PYTHON_QUEUES)
        run_section("relations", SQL_OP_RELATIONS, (list(relations),))
        run_section("processed", SQL_OP_PROCESSED)
        run_section("failures", SQL_OP_FAILURES)
        for section in snapshot.values():
            if isinstance(section, dict) and "rows" in section:
                section["rows"] = _jsonable(section["rows"])
        snapshot["status"] = ("complete"
                               if all(s["status"] == "complete"
                                      for s in snapshot.values()
                                      if isinstance(s, dict))
                               else "unknown")
        return snapshot

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
        if not name.startswith(("clashlens_collector_", "clashlens_spool_")):
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
                 pgdata: dict | None = None,
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
    sample["pgdata"] = pgdata
    sample["mount_changed"] = _mount_changed(run.get("filesystem") or {}, fs)
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
            "role": ("drain" if run.get("mode") == "preflight"
                       and index >= mode.get("bootstrap_slots", 0)
                       else "core"),
            "admission": "integrated_at_finalize" if mode["admission"]
                         else "not_applicable",
        }
        payload = _canonical(window)
        if len(payload) > WINDOW_MAX_BYTES:
            raise Step9Error("artifact_capacity_exceeded",
                             "window object exceeds 128 KiB")
        sample["window"] = window
    return sample


def _sample_fixed_ids(run: dict, db: object) -> list[int]:
    """Re-derive eligible fixed IDs at sample/finalize time from the pinned cohort."""
    cohort = run.get("cohort", {})
    tags, _raw, _canonical = _read_cohort(cohort["path"])
    if _raw != cohort.get("raw_sha256"):
        raise Step9Error("cohort_changed", "cohort file changed after start",
                         gate=True)
    rows = db.snapshot_population(tags)
    return [row[0] for row in rows if _classify_eligible(row) == "eligible"]


def _check_liveness_reset(previous: dict | None, current: dict | None) -> str | None:
    """High-water IDs must never decrease; a drop is a counter reset."""
    if not previous or not current:
        return None
    for key in ("collector_max_job_id", "attempt_max_id",
                "observation_max_id", "python_max_job_id"):
        old, new = previous.get(key), current.get(key)
        if isinstance(old, int) and isinstance(new, int) and new < old:
            return key
    return None


def _prior_samples(samples_dir: Path) -> list[dict]:
    """Best-effort read of already-written samples for stop evidence."""
    prior = []
    if not samples_dir.is_dir():
        return prior
    for path in sorted(samples_dir.glob("minute-*.json")):
        try:
            prior.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return prior


def _mount_changed(baseline: dict, current: dict) -> bool | None:
    """Mount-identity continuity; None when either side is unknown."""
    try:
        for label in ("spool", "postgres"):
            old, new = baseline[label], current[label]
            for field in ("mount_point", "source", "mnt_id"):
                if old.get(field) is None or new.get(field) is None:
                    return None
                if old[field] != new[field]:
                    return True
        return False
    except (KeyError, TypeError, AttributeError):
        return None


def _systemd_watchdog_check(run: dict) -> bool | None:
    """Report sampler-unit liveness; None when the unit is not configured."""
    import subprocess

    unit = (run.get("watchdog_unit") or "").strip()
    if not unit:
        return None
    try:
        completed = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode == 0


def _cgroup_numbers(podman_bin: str, container: str) -> dict:
    """Container cgroup OOM/swap/memory counters; unknown stays None."""
    import subprocess

    result: dict = {"oom_kills": None, "swap_current_bytes": None,
                    "mem_current_bytes": None, "mem_peak_bytes": None,
                    "error": None}
    try:
        completed = subprocess.run(
            [podman_bin, "container", "inspect", "--format",
             "{{.State.CgroupPath}}", container],
            check=False, capture_output=True, text=True, timeout=30)
        if completed.returncode != 0:
            result["error"] = "cgroup_path_unavailable"
            return result
        base = Path("/sys/fs/cgroup") / completed.stdout.strip().lstrip("/")
        result.update(_read_cgroup_files(base))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        result["error"] = "cgroup_unavailable:" + type(error).__name__
    return result


def _read_cgroup_files(base: Path) -> dict:
    """Read cgroup memory counters below a cgroup directory."""
    out: dict = {}
    events = (base / "memory.events").read_text()
    for line in events.splitlines():
        if line.startswith("oom_kill"):
            out["oom_kills"] = int(line.split()[1])
    out["swap_current_bytes"] = int(
        (base / "memory.swap.current").read_text().strip())
    out["mem_current_bytes"] = int(
        (base / "memory.current").read_text().strip())
    try:
        out["mem_peak_bytes"] = int(
            (base / "memory.peak").read_text().strip())
    except (OSError, ValueError):
        pass
    return out


def _btrfs_device_stats(target: str, podman_bin: str | None = None) -> dict:
    """Real `btrfs device stats` error counters; unavailable fails closed."""
    import subprocess

    result: dict = {"errors": {}, "error": None}
    try:
        completed = subprocess.run(
            ["btrfs", "device", "stats", str(target)],
            check=False, capture_output=True, text=True, timeout=30)
        if completed.returncode != 0:
            result["error"] = "device_stats_failed"
            return result
        totals: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            match = re.fullmatch(r"\[(.*)\]\.(\w+)\s+(\d+).*", line.strip())
            if match:
                totals[match.group(2)] = totals.get(match.group(2), 0) \
                    + int(match.group(3))
        result["errors"] = totals
    except FileNotFoundError:
        result["error"] = "tool_missing"
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        result["error"] = "device_stats_unavailable:" + type(error).__name__
    return result


def _podman_container_probe(run: dict) -> dict | None:
    """Best-effort container state/stats; None stays unknown, never zero."""
    import subprocess

    containers = run.get("containers", {}) or {}
    name = containers.get("collector")
    podman_bin = run.get("podman_bin", "podman") or "podman"
    if not name:
        return None
    try:
        inspect = subprocess.run(
            [podman_bin, "container", "inspect", "--format",
             "{{.State.Running}}\n{{.Image}}\n{{.State.StartedAt}}", name],
            check=False, capture_output=True, text=True, timeout=30)
        if inspect.returncode != 0:
            return None
        lines = inspect.stdout.strip().splitlines()
        stats = subprocess.run(
            [podman_bin, "stats", "--no-stream", "--format",
             "{{.CPUPerc}}\n{{.MemUsage}}", name],
            check=False, capture_output=True, text=True, timeout=30)
        return {"running": (lines[0].strip().lower() == "true") if lines else None,
                "image": lines[1].strip() if len(lines) > 1 else None,
                "started_at": lines[2].strip() if len(lines) > 2 else None,
                "stats": stats.stdout.strip() if stats.returncode == 0 else None,
                "cgroup": _cgroup_numbers(podman_bin, name)}
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def cmd_sample(arguments: argparse.Namespace, hooks=None) -> int:
    """Minute loop; hooks injects (db, metrics_fetch, watchdog_check, clock)."""
    run_dir = _resolve_run_dir(arguments.run_dir)
    run = _load_run(run_dir)
    hooks = hooks or {}
    db = hooks.get("db")
    fetch_metrics = hooks.get("fetch_metrics", fetch_runtime_metrics)
    watchdog_check = hooks.get("watchdog_check", _systemd_watchdog_check)
    clock = hooks.get("clock", time.monotonic_ns)
    mode = MODES.get(run.get("mode", "live-day"), MODES["live-day"])
    max_slots = hooks.get("max_slots", mode["slots"])
    core_start = _parse_utc(run["core_start"])
    samples_dir = run_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    fixed_ids: list[int] | None = hooks.get("fixed_ids", [])
    derivation_error: str | None = None
    if db is not None and not fixed_ids:
        try:
            fixed_ids = _sample_fixed_ids(run, db)
        except Step9Error:
            raise
        except Exception as error:  # noqa: BLE001 - derivation miss is evidence
            derivation_error = f"database_unavailable: {error}"
            fixed_ids = []
    start_mono = clock()
    previous_metrics: dict | None = None
    previous_mono: int | None = None
    previous_wall: datetime | None = None
    previous_s3: dict | None = None
    previous_liveness: dict | None = None
    outcome_strikes = 0
    unavailable_strikes = 0
    container_probe = hooks.get("container_probe")
    resource_baseline = run.get("resource_baseline") or {}
    mem_over = 0
    for index in range(max_slots):
        expected_utc = slot_expected_utc(core_start, index)
        captured_utc = hooks.get("now_utc", _utc_now)()
        now_mono = clock()
        mono_elapsed = (now_mono - start_mono) / 1e9
        mono_delta = 0.0 if previous_mono is None else (now_mono - previous_mono) / 1e9
        wall_delta = (0.0 if previous_wall is None
                      else (captured_utc - previous_wall).total_seconds())
        try:
            db_facts, db_error = (None, derivation_error)
            if db is not None and derivation_error is None:
                try:
                    db_facts = db.minute_snapshot(fixed_ids or [])
                except Exception as error:  # noqa: BLE001 - adapter failure is evidence
                    db_error = f"database_unavailable: {error}"
            elif db is None:
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
            name = f"minute-{index:04d}.json"
            try:
                sample["container"] = container_probe(run) \
                    if container_probe else _podman_container_probe(run)
            except Exception:  # noqa: BLE001 - probe miss is unknown
                sample["container"] = None
            if db is not None and (index + 1) % 60 == 0:
                try:
                    sample["operating"] = db.operating_snapshot(
                        list(_OPERATING_RELATIONS))
                except Exception:  # noqa: BLE001 - capture miss is evidence
                    sample["operating"] = {"status": "unknown",
                                             "failure_code": "capture_failed"}
            facts_hook = hooks.get("resource_facts")
            if facts_hook is not None:
                resources = facts_hook(run=run, db=db, metrics=metrics)
            else:
                resources = collect_resource_facts(
                    spool_path=run["spool_path"],
                    postgres_path=run["postgres_path"], db=db,
                    metrics=metrics,
                    btrfs_probe=hooks.get("btrfs_probe"))
            cgroup = (sample.get("container") or {}).get("cgroup")
            if cgroup:
                resources = dict(resources)
                resources["cgroup"] = cgroup
            wire_hook = hooks.get("wire_facts")
            if wire_hook is not None:
                wire = wire_hook(run=run)
            else:
                wire = collect_wire_facts(
                    interfaces=run.get("archive_interfaces") or [],
                    route_host=run.get("archive_route_host"))
            wire_failures, wire_unknown, wire_total = evaluate_wire(
                run.get("wire_baseline") or {}, wire,
                run.get("transfer_prior_bytes", TRANSFER_PRIOR_BYTES))
            sample["wire"] = {"failures": wire_failures,
                                "unknown": wire_unknown,
                                "conservative_host_wire_bytes": wire_total}
            if wire_failures:
                sample["failure_code"] = wire_failures[0]
                sample["outcome"] = "transfer_gate"
                _exclusive_json(samples_dir / name, sample,
                                max_bytes=SAMPLE_MAX_BYTES)
                _record_failure(run_dir, sample["failure_code"],
                                    sample["outcome"])
                return 1
            s3_py, s3_error = _worker_snapshots(
                run, hooks.get("worker_probe"))
            s3 = _s3_snapshot(metrics, s3_py)
            s3["error"] = s3_error
            sample["s3"] = s3
            if s3_error is not None:
                unavailable_strikes += 1
                if unavailable_strikes >= 2:
                    _record_failure(run_dir, "two_consecutive_unavailable",
                                    "two consecutive unavailable samples")
                    return 1
            else:
                if _s3_decreased(previous_s3, s3):
                    sample["failure_code"] = "s3_counter_reset"
                    sample["outcome"] = "s3_counter_reset"
                    _exclusive_json(samples_dir / name, sample,
                                    max_bytes=SAMPLE_MAX_BYTES)
                    _record_failure(run_dir, sample["failure_code"],
                                        sample["outcome"])
                    return 1
                previous_s3 = s3
                prior = _s3_prior(run)
                cumulative = prior["attempts"] + s3["total"]
                sample["s3_attempts_cumulative"] = cumulative
                if cumulative > S3_ATTEMPTS_MAX:
                    sample["failure_code"] = "s3_attempts_breach"
                    sample["outcome"] = "s3_attempts_breach"
                    _exclusive_json(samples_dir / name, sample,
                                    max_bytes=SAMPLE_MAX_BYTES)
                    _record_failure(run_dir, sample["failure_code"],
                                        sample["outcome"])
                    return 1
            pgdata_probe = hooks.get("pgdata_probe")
            if pgdata_probe is not None:
                try:
                    pgdata = pgdata_probe(run=run)
                except Exception:  # noqa: BLE001 - probe miss is unknown
                    pgdata = {"status": "unknown",
                              "failure_code": "pgdata_probe_failed"}
            else:
                pgdata = _pgdata_probe(
                    run.get("podman_bin", "podman") or "podman",
                    (run.get("containers", {}) or {}).get("postgres", ""),
                    (run.get("containers", {}) or {}).get("postgres_image"))
            sample["pgdata"] = pgdata
            pgdata_bad = (sample.get("pgdata") or {}).get("failure_code")
            res_failures, res_unknown, mem_over = evaluate_resource_gates(
                resource_baseline, resources, mem_over)
            sample["resources"] = {"failures": res_failures,
                                     "unknown": res_unknown}
            if res_failures:
                sample["failure_code"] = res_failures[0]
                sample["outcome"] = "resource_gate"
            if metrics is not None:
                previous_metrics = metrics
            if (samples_dir / name).exists():
                raise Step9Error("duplicate_sample", f"slot {index} already written",
                                 gate=True)
            sample["liveness_reset"] = _check_liveness_reset(
                previous_liveness, sample.get("liveness"))
            if isinstance(sample.get("liveness"), dict):
                previous_liveness = sample["liveness"]
            stop_proven = (run_dir / "watchdog-outcome.json").exists() or any(
                (s.get("container") or {}).get("running") is False
                for s in _prior_samples(samples_dir)) or (
                    (sample.get("container") or {}).get("running") is False)
            sample["stop_proven"] = bool(stop_proven)
            if (sample.get("container") or {}).get("running") is False \
                    and index < mode.get("bootstrap_slots", mode["slots"]):
                sample["failure_code"] = "collector_stopped_early"
                sample["outcome"] = "collector_stopped_early"
                _exclusive_json(samples_dir / name, sample,
                                max_bytes=SAMPLE_MAX_BYTES)
                _record_failure(run_dir, sample["failure_code"], "collector")
                return 1
            metrics_absent = metrics_error is not None
            if metrics_absent and run.get("mode") == "preflight" \
                    and index >= mode.get("bootstrap_slots", 0) \
                    and stop_proven:
                sample["metrics_absent_authorized"] = True
                metrics_absent = False
            _exclusive_json(samples_dir / name, sample,
                            max_bytes=SAMPLE_MAX_BYTES)
            _check_capacity(run_dir)
            outcome = sample["outcome"]
            if outcome in ("clock_jump", "boot_change", "non_monotonic",
                           "out_of_order", "late", "admission_mismatch",
                           "process_restart", "resource_gate", "transfer_gate",
                           "s3_counter_reset", "s3_attempts_breach"):
                _record_failure(run_dir, sample["failure_code"], outcome)
                outcome_strikes += 1
                if outcome_strikes >= 2 or outcome in ("process_restart",
                                                      "resource_gate"):
                    return 1
            elif outcome == "on_time" and db_error is None \
                    and (metrics_error is None
                         or sample.get("metrics_absent_authorized")):
                outcome_strikes = 0
            if db_error is not None or metrics_absent or pgdata_bad:
                unavailable_strikes += 1
                if unavailable_strikes >= 2:
                    _record_failure(run_dir, "two_consecutive_unavailable",
                                    "two consecutive unavailable samples")
                    return 1
            else:
                unavailable_strikes = 0
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


def _finalize_wal(samples: list[dict], db: object | None) -> dict:
    """Generated LSN WAL plus retained PGDATA/pg_wal bytes from probes."""
    if db is None:
        return {"status": "unknown", "failure_code": "database_unavailable"}
    try:
        first_lsn = (samples[0].get("liveness") or {}).get("wal_lsn")
        if not first_lsn:
            return {"status": "unknown", "failure_code": "wal_lsn_missing"}
        generated, size = db.wal_generated(first_lsn)
        retained = next((s.get("pgdata") for s in reversed(samples)
                         if isinstance(s.get("pgdata"), dict)
                         and s["pgdata"].get("status") == "captured"), None)
        result: dict = {"status": "complete",
                        "generated_bytes": int(generated),
                        "database_bytes": int(size)}
        if retained is None:
            result["status"] = "unknown"
            result["failure_code"] = "pgdata_retained_unknown"
        else:
            result["retained_pgdata_bytes"] = retained.get("pgdata_bytes")
            result["retained_pg_wal_bytes"] = retained.get("pg_wal_bytes")
            result["retained_pgdata"] = retained.get("pgdata")
            result["retained_captured_at"] = retained.get("captured_at")
        return result
    except Step9Error as error:
        return {"status": "unknown", "failure_code": error.code}
    except Exception as error:  # noqa: BLE001 - DB failure is evidence
        return {"status": "unknown",
                "failure_code": f"database_unavailable: {error}"}


def _finalize_operating(run: dict, db: object | None,
                        run_dir: Path) -> dict:
    """Final operating capture plus baseline/final failure regression."""
    if db is None:
        return {"status": "unknown", "failure_code": "database_unavailable"}
    try:
        final = db.operating_snapshot(list(_OPERATING_RELATIONS))
    except Exception as error:  # noqa: BLE001 - capture miss is evidence
        return {"status": "unknown",
                "failure_code": f"database_unavailable: {error}"}
    try:
        baseline_path = run_dir / "operating-baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        base_snap = baseline.get("snapshot", baseline)
    except (OSError, json.JSONDecodeError):
        base_snap = {}
    result: dict = {"status": final.get("status", "unknown"),
                     "snapshot": final, "regressed": []}
    if final.get("status") != "complete":
        result["failure_code"] = "operating_incomplete"
        return result
    base_fail = {r[0]: r[1] for r in
                 base_snap.get("failures", {}).get("rows", [])
                 if isinstance(base_snap.get("failures"), dict)}
    final_fail = {r[0]: r[1] for r in final["failures"]["rows"]}
    for category, count in final_fail.items():
        if int(count) > int(base_fail.get(category, 0)):
            result["regressed"].append(category)
    if result["regressed"]:
        result["status"] = "failed"
        result["failure_code"] = "operating_regressed"
    return result


def _finalize_transfer(samples: list[dict], run: dict) -> dict:
    """Retained transfer totals: wire bound plus S3 attempt accounting."""
    wire_last = next((s.get("wire") for s in reversed(samples)
                      if isinstance(s.get("wire"), dict)), None)
    s3_seen = [s.get("s3") for s in samples
               if isinstance(s.get("s3"), dict)
               and s["s3"].get("error") is None]
    result: dict = {
        "prior_bytes": run.get("transfer_prior_bytes", TRANSFER_PRIOR_BYTES),
        "prior_provenance": run.get("transfer_prior_provenance",
                                      TRANSFER_PRIOR_PROVENANCE),
        "prior_attempts": TRANSFER_PRIOR_ATTEMPTS,
        "cap_bytes": TRANSFER_CUMULATIVE_MAX,
        "attempts_cap": S3_ATTEMPTS_MAX,
        "wire_bytes": (wire_last or {}).get("conservative_host_wire_bytes"),
        "status": "unknown", "failure": None,
    }
    if wire_last is None or wire_last.get("conservative_host_wire_bytes") is None:
        result["failure"] = "transfer_unknown"
        return result
    if wire_last.get("failures"):
        result["status"] = "failed"
        result["failure"] = wire_last["failures"][0]
        return result
    if not s3_seen:
        result["failure"] = "transfer_unknown"
        return result
    first, last = s3_seen[0], s3_seen[-1]
    if _s3_decreased(first, last):
        result["status"] = "failed"
        result["failure"] = "s3_counter_reset"
        return result
    result["s3_attempts"] = _s3_prior(run).get("attempts",
                                                 TRANSFER_PRIOR_ATTEMPTS) \
        + last["total"]
    if result["s3_attempts"] > S3_ATTEMPTS_MAX:
        result["status"] = "failed"
        result["failure"] = "s3_attempts_breach"
        return result
    result["status"] = "complete"
    return result


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


def _finalize_budget(run: dict, db: object, endpoints: dict) -> dict:
    """Provisional 0023 durable-budget cross-check; unknown until merged."""
    bootstrap_id = run.get("bootstrap_run_id")
    if not bootstrap_id:
        return {"status": "unknown_pending_0023", "failure": None}
    try:
        if not db.budgets_present():
            return {"status": "unknown_pending_0023", "failure": None}
        data = db.bootstrap_budgets(bootstrap_id)
    except Step9Error as error:
        return {"status": "unknown", "failure": None,
                "failure_code": error.code}
    except Exception as error:  # noqa: BLE001 - denial is evidence
        if getattr(error, "sqlstate", "") == "42501":
            return {"status": "unknown_pending_grant", "failure": None}
        return {"status": "unknown", "failure": None,
                "failure_code": f"database_unavailable: {error}"}
    brow = data["run"]
    if brow is None:
        return {"status": "unknown", "failure": None,
                "failure_code": "budget_run_missing"}
    cohort = run.get("cohort", {})
    result: dict = {"status": "complete", "failure": None,
                     "bootstrap_status": brow["status"],
                     "budgets": {b["endpoint"]: {"cap": b["cap"],
                                                     "consumed": b["consumed"]}
                                 for b in data["budgets"]}}
    pinned = run.get("budget_receipt") or {}
    if pinned:
        try:
            receipt_deadline = _parse_utc(pinned["deadline_at"])
        except Step9Error:
            result["status"] = "failed"
            result["failure"] = "budget_deadline_malformed"
            return result
        db_caps = {b["endpoint"]: b["cap"] for b in data["budgets"]}
        db_deadlines = {str(b["deadline_at"]) for b in data["budgets"]}
        envelope_map = {"profile": pinned["caps"].get(
            "endpoint_budget_profile"),
            "global_player_rankings": pinned["caps"].get(
                "endpoint_budget_global_rankings"),
            "battle_log": pinned["caps"].get("endpoint_budget_battle_log")}
        if pinned.get("run_id") != brow.get("run_id") \
                or db_caps != envelope_map \
                or not db_deadlines \
                or any(_parse_utc(str(d)) != receipt_deadline
                       for d in db_deadlines):
            result["status"] = "failed"
            result["failure"] = "budget_binding_mismatch"
            return result
    if brow["manifest_sha256"] != cohort.get("raw_sha256") \
            or brow["manifest_count"] != cohort.get("input_count") \
            or brow["normalized_set_sha256"] != cohort.get("canonical_sha256"):
        result["status"] = "failed"
        result["failure"] = "budget_manifest_mismatch"
        return result
    consumed = {b["endpoint"]: b["consumed"] for b in data["budgets"]}
    if endpoints.get("profile", 0) > consumed.get("profile", -1):
        result["status"] = "failed"
        result["failure"] = "budget_evidence_mismatch"
    return result


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
        result["budget"] = _finalize_budget(run, db, result["endpoints"])
        if result["budget"].get("failure"):
            result["failures"] = sorted(set(result["failures"]) |
                                          {result["budget"]["failure"]})
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


def _finalize_eligibility(run: dict, db: object | None) -> dict:
    """Final cohort/eligibility reconciliation plus domain transitions."""
    if db is None:
        return {"status": "unknown", "failure_code": "database_unavailable"}
    try:
        cohort = run.get("cohort", {})
        tags, _raw, _canonical = _read_cohort(cohort["path"])
        if _raw != cohort.get("raw_sha256"):
            return {"status": "unknown", "failure_code": "cohort_changed"}
        rows = db.snapshot_population(tags)
        eligible = [r[0] for r in rows if _classify_eligible(r) == "eligible"]
        matched = [r[0] for r in rows]
        outside = db.outside_active(matched)
        lineage = db.outside_roots(matched)
        effects = db.transitions(matched, run["core_start"], run["core_end"])
        result: dict = {
            "status": "complete", "eligible_count": len(eligible),
            "eligible_digest": _eligible_digest(eligible),
            "matched_count": len(matched),
            "outside_active": outside, "outside_lineage": lineage,
            "effect_rows": len(effects),
        }
        if not eligible:
            result["status"] = "failed"
            result["failure_code"] = "zero_eligible"
        elif outside or lineage:
            result["status"] = "failed"
            result["failure_code"] = "foreign_population_recheck"
        return result
    except Step9Error as error:
        return {"status": "unknown", "failure_code": error.code}
    except Exception as error:  # noqa: BLE001 - DB failure is evidence
        return {"status": "unknown",
                "failure_code": f"database_unavailable: {error}"}


def _finalize_reset_deep(run: dict, db: object,
                         rows: list[tuple]) -> dict:
    """Sweep/generation membership equality, paired roots, drain gate."""
    deep: dict = {"boundaries": len(rows)}
    failures: list[str] = []
    try:
        for row in rows:
            boundary_at, sweep_id = row[0], row[1]
            sweep_members = db.reset_members(sweep_id)
            gen_rows = db.generation_members(sweep_id, str(boundary_at))
            gen_members = [r[0] for r in gen_rows]
            if sorted(sweep_members) != sorted(gen_members):
                failures.append("reset_membership_mismatch")
            paired = db.paired_baselines(sweep_id)
            unpaired = [p for p in sweep_members if paired.get(p, 0) < 1]
            if unpaired:
                failures.append("reset_baseline_unpaired")
            if gen_rows:
                expected_count = gen_rows[0][1]
                if expected_count != len(gen_members):
                    failures.append("reset_generation_count")
                deep.setdefault("expected_hashes", []).append(gen_rows[0][2])
            if int(row[7]) > 0:
                failures.append("reset_drain_incomplete")
        deep["failures"] = sorted(set(failures))
        deep["status"] = "complete" if not failures else "failed"
        return deep
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
        reset_rows: list = []
        db = hooks.get("db")
        if db is not None:
            try:
                reset_rows = db.reset_identity(run["core_start"], run["core_end"])
                resets = {"status": "captured", "boundaries": len(reset_rows),
                          "safe_handoffs": sum(1 for r in reset_rows if r[4]),
                          "nonterminal_reset_jobs": sum(int(r[7]) for r in reset_rows)}
                if mode["admission"] and (len(reset_rows) != 1
                                            or not reset_rows[0][4]):
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
            "reset": resets,
            "transfer": _finalize_transfer(samples, run),
            "operating": _finalize_operating(run, db, run_dir),
            "reset_deep": (_finalize_reset_deep(run, db, reset_rows)
                            if db is not None and mode["admission"]
                            else {"status": "not_applicable"}),
            "eligibility": _finalize_eligibility(run, db),
            "admission": _finalize_admission(run, db),
            "preflight": _finalize_preflight(run, db),
            "filesystem": filesystem_facts(run["spool_path"], run["postgres_path"]),
            "wal": _finalize_wal(samples, db),
            "failure_codes": sorted({s.get("failure_code") for s in samples
                                     if s.get("failure_code")}),
        }
        _exclusive_json(run_dir / "final.json", final)
        _write_manifest(run_dir)
        _check_capacity(run_dir)
        return _finalize_exit(final, mode)
    except Step9Error as error:
        _record_failure(run_dir, error.code, str(error))
        return 1 if error.gate else 2


def _finalize_exit(final: dict, mode: dict) -> int:
    """Sealed evidence always; nonzero when required blocks fail/are unknown."""
    required = [final.get("eligibility", {}), final.get("wal", {}),
                final.get("operating", {})]
    transfer = final.get("transfer", {})
    if mode["admission"]:
        required += [final.get("admission", {}), final.get("reset_deep", {})]
    else:
        required += [final.get("preflight", {})]
    if any(block.get("status") == "failed" for block in required):
        return 1
    if any(block.get("status") != "complete" for block in required):
        return 2
    if transfer.get("status") != "complete":
        return 2 if transfer.get("failure") in ("transfer_unknown", None) else 1
    return 0


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
        for sample in samples:
            if sample.get("outcome") != "on_time":
                raise Step9Error("sample_outcome_failed",
                                 f"slot {sample.get('slot')} outcome "
                                 f"{sample.get('outcome')}", gate=True)
            for field in ("database_error", "metrics_error", "counter_reset",
                          "liveness_reset"):
                if field == "metrics_error" \
                        and sample.get("metrics_absent_authorized") is True:
                    continue
                if sample.get(field) is not None:
                    raise Step9Error("sample_evidence_failed",
                                     f"slot {sample.get('slot')} {field} set",
                                     gate=True)
            pgdata = sample.get("pgdata") or {}
            if pgdata.get("failure_code") is not None:
                raise Step9Error("sample_evidence_failed",
                                 f"slot {sample.get('slot')} pgdata "
                                 f"{pgdata.get('failure_code')}", gate=True)
            s3err = (sample.get("s3") or {}).get("error")
            if s3err is not None:
                raise Step9Error("sample_evidence_failed",
                                 f"slot {sample.get('slot')} s3 {s3err}",
                                 gate=True)
            if sample.get("watchdog_active") is not True:
                raise Step9Error("watchdog_liveness_unproven",
                                 f"slot {sample.get('slot')} watchdog not active",
                                 gate=True)
            if sample.get("mount_changed") is True:
                raise Step9Error("mount_identity_changed",
                                 f"slot {sample.get('slot')} mount changed",
                                 gate=True)
            if sample.get("mount_changed") is None:
                raise Step9Error("mount_identity_unknown",
                                 f"slot {sample.get('slot')} mount unproven",
                                 gate=True)
            resources = sample.get("resources")
            if isinstance(resources, dict) and (
                    resources.get("failures") or resources.get("unknown")):
                raise Step9Error("resource_evidence_failed",
                                 f"slot {sample.get('slot')} resource "
                                 "failures or unknowns", gate=True)
            operating = sample.get("operating")
            if operating is not None and operating.get("status") != "complete":
                raise Step9Error("operating_failed",
                                 f"slot {sample.get('slot')} operating "
                                 "capture incomplete", gate=True)
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
        if final.get("non_on_time_slots"):
            raise Step9Error("sample_outcome_failed",
                             "final.json records non-on-time slots", gate=True)
        eligibility = final.get("eligibility", {})
        if eligibility.get("status") != "complete":
            raise Step9Error("eligibility_unproven",
                             "final.json lacks complete eligibility reconciliation",
                             gate=True)
        if eligibility.get("outside_active") or eligibility.get("outside_lineage"):
            raise Step9Error("foreign_population_recheck",
                             "final eligibility recheck found outside population",
                             gate=True)
        deep = final.get("reset_deep", {})
        if mode["admission"] and deep.get("status") != "complete":
            raise Step9Error("reset_deep_unproven",
                             "final.json lacks complete reset reconciliation",
                             gate=True)
        transfer = final.get("transfer", {})
        if transfer.get("status") != "complete":
            raise Step9Error("transfer_unproven",
                             "final.json lacks complete transfer accounting",
                             gate=transfer.get("failure") not in (
                                 "transfer_unknown", None))
        operating = final.get("operating", {})
        if operating.get("status") != "complete":
            raise Step9Error("operating_unproven",
                             "final.json lacks complete operating evidence",
                             gate=True)
        if operating.get("regressed"):
            raise Step9Error("operating_regressed",
                             "operating failures grew during the run",
                             gate=True)
        if final.get("failure_codes"):
            raise Step9Error("sample_evidence_failed",
                             "final.json records failure codes", gate=True)
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

    def inspect_restart_policy(self, container: str) -> str | None:
        try:
            output = self._run([self._bin, "container", "inspect", "--format",
                                "{{.HostConfig.RestartPolicy.Name}}", container])
        except Exception:  # noqa: BLE001 - stop adapter reports only
            return None
        return output.strip() or None

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
    pinned_image = (run.get("containers", {}) or {}).get("collector_image")
    if pinned_image is None:
        _record_failure(run_dir, "unpinned_image",
                        "no start-time collector image pin")
        return 2
    if image != pinned_image:
        _record_failure(run_dir, "container_image_changed",
                        "collector image differs from start pin")
        return 1
    verified, prior = _verified_restart_disabled(
        podman, collector,
        tries=1 if hooks.get("no_sleep") else 6)
    if not verified:
        _record_failure(run_dir, "restart_not_disabled",
                        "restart policy is not verified no")
        return 1
    _exclusive_json(run_dir / "watchdog.json", {
        "schema": run.get("schema", SCHEMA), "run_id": run["run_id"],
        "collector": collector, "image": image,
        "prior_restart_policy": prior,
        "verified_restart": "no", "started_at": _utc_now().isoformat(),
        "deadline": deadline.isoformat(),
        "max_sample_age_seconds": arguments.max_sample_age_seconds,
        "systemd_unit": arguments.systemd_unit,
    })
    now_utc = hooks.get("now_utc", _utc_now)
    max_iterations = hooks.get("max_iterations", 2**31)
    iteration = 0
    sampler_check = hooks.get("sampler_check", _systemd_watchdog_check)
    while iteration < max_iterations:
        iteration += 1
        outcome = _watchdog_once(run_dir, run, podman, collector, deadline,
                                 arguments.max_sample_age_seconds, now_utc,
                                 sampler_check)
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


def _verified_restart_disabled(podman: Podman, container: str,
                               tries: int = 6) -> tuple[bool, str | None]:
    """Disable restart and re-inspect until exactly 'no'."""
    prior = podman.inspect_restart_policy(container)
    error = podman.disable_restart(container)
    if error is not None:
        return False, prior
    for _attempt in range(tries):
        if podman.inspect_restart_policy(container) == "no":
            return True, prior
        time.sleep(2)
    return False, prior


def _watchdog_once(run_dir: Path, run: dict, podman: Podman, collector: str,
                   deadline: datetime, max_age: int, now_utc,
                   sampler_check=None) -> str | None:
    now = now_utc()
    if now >= deadline:
        return "deadline_reached"
    running, _ = podman.inspect_running(collector)
    if running is None:
        return "inspect_unavailable"
    if not running:
        return "collector_stopped"
    try:
        core_start = _parse_utc(run["core_start"])
    except Step9Error:
        return "run_malformed"
    grace_ends = core_start + timedelta(seconds=max_age + 60)
    samples = sorted((run_dir / "samples").glob("minute-*.json")) if (
        run_dir / "samples").is_dir() else []
    if not samples:
        if now < grace_ends:
            return None  # sampler still starting; deadline still enforced
        if sampler_check is not None and sampler_check(run) is False:
            return "sampler_unit_inactive"
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
    start.add_argument("--database-url-file", default=None)
    start.add_argument("--archive-tariff-file", default=None)
    start.add_argument("--archive-egress-interface", dest="archive_interfaces",
                       action="append", default=[])
    start.add_argument("--archive-route-host", default=None)
    start.add_argument("--prior-s3-attempts", type=int, default=None)
    start.add_argument("--prior-s3-provenance", default=None)
    start.add_argument("--bootstrap-run-id", default=None)
    start.add_argument("--mode", choices=sorted(MODES), default="live-day")
    start.add_argument("--max-invocation-gap-seconds", type=int, default=None,
                       help="required: scheduler invocation cadence evidence bound")

    sample = sub.add_parser("sample", help="run the minute sampling loop")
    _add_common(sample)
    sample.add_argument("--database-url", default=None)
    sample.add_argument("--database-url-file", default=None)

    finalize = sub.add_parser("finalize", help="reconcile once and seal a manifest")
    _add_common(finalize)
    finalize.add_argument("--database-url", default=None)
    finalize.add_argument("--database-url-file", default=None)

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


def _read_database_url_file(path_str: str) -> str:
    """Read a protected database URL file; never log or retain the value."""
    from clashlens.bootstrap import read_secret_file

    if not os.path.isabs(path_str):
        raise Step9Error("db_url_file", "database URL file must be absolute")
    try:
        st = os.lstat(path_str)
    except OSError as error:
        raise Step9Error("db_url_file", "database URL file unreadable") from error
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        raise Step9Error("db_url_file", "database URL file must be a file")
    if st.st_mode & 0o077:
        raise Step9Error("db_url_file", "database URL file must be private")
    try:
        value = read_secret_file(path_str)
    except Exception as error:
        raise Step9Error("db_url_file", "database URL file invalid") from error
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Step9Error("db_url_file", "database URL file invalid")
    return value


def _resolve_database_url(arguments: argparse.Namespace) -> str | None:
    url = getattr(arguments, "database_url", None)
    path = getattr(arguments, "database_url_file", None)
    if url and path:
        raise Step9Error("db_url_conflict",
                         "use only one of --database-url/--database-url-file")
    if path:
        return _read_database_url_file(path)
    return url


def _cli_hooks(arguments: argparse.Namespace) -> dict:
    """Build production hooks: real DB when a URL source is given."""
    url = _resolve_database_url(arguments)
    if not url:
        return {}
    import psycopg

    return {"db": Database(lambda: psycopg.connect(url))}


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    run_dir = _resolve_run_dir(arguments.run_dir) if arguments.run_dir else None
    try:
        if arguments.command == "start":
            db = None
            url = _resolve_database_url(arguments)
            if url:
                import psycopg

                db = Database(lambda: psycopg.connect(url))
            header = cmd_start(arguments, db)
            print(json.dumps({"run_id": header["run_id"],
                              "header_sha256": header["header_sha256"]}))
            return 0
        if arguments.command == "sample":
            return cmd_sample(arguments, _cli_hooks(arguments))
        if arguments.command == "finalize":
            return cmd_finalize(arguments, _cli_hooks(arguments))
        if arguments.command == "validate":
            return cmd_validate(arguments)
        if arguments.command == "watchdog":
            return cmd_watchdog(arguments)
    except Step9Error as error:
        _record_failure(run_dir, error.code, str(error))
        print(f"step9 {arguments.command}: {error.code}: {error}", file=sys.stderr)
        return 1 if error.gate else 2
    raise AssertionError("unreachable")



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


SQL_0023_TABLES = """
SELECT to_regclass('population_bootstrap_runs') IS NOT NULL
   AND to_regclass('collector_endpoint_budgets') IS NOT NULL
"""

SQL_BOOTSTRAP_RUN = """
SELECT run_id, manifest_sha256, manifest_count, normalized_set_sha256,
       status, batch_size, players_registered, discovery_jobs_created,
       created_at, completed_at
FROM population_bootstrap_runs WHERE run_id = %s
"""

SQL_ENDPOINT_BUDGETS = """
SELECT endpoint, cap, consumed, deadline_at, updated_at
FROM collector_endpoint_budgets WHERE run_id = %s ORDER BY endpoint
"""

for _statement in (SQL_0023_TABLES, SQL_BOOTSTRAP_RUN, SQL_ENDPOINT_BUDGETS):
    assert_read_only(_statement)

ALL_RO_STATEMENTS += (SQL_0023_TABLES, SQL_BOOTSTRAP_RUN, SQL_ENDPOINT_BUDGETS)


SQL_OP_IDENTITY = """
SELECT (SELECT system_identifier::text FROM pg_control_system()),
       (SELECT oid::bigint FROM pg_database
        WHERE datname = current_database()),
       statement_timestamp()
"""

SQL_OP_QUEUES = """
SELECT status, count(*), min(due_at) FROM collector_jobs GROUP BY status;
"""

SQL_OP_PYTHON_QUEUES = """
SELECT status, count(*), min(due_at) FROM python_processing_jobs
GROUP BY status
"""

SQL_OP_RELATIONS = """
SELECT known.name,
       pg_table_size(class.oid) - CASE WHEN class.reltoastrelid = 0 THEN 0
           ELSE pg_total_relation_size(class.reltoastrelid) END AS table_bytes,
       pg_indexes_size(class.oid) AS index_bytes,
       CASE WHEN class.reltoastrelid = 0 THEN 0
           ELSE pg_total_relation_size(class.reltoastrelid) END AS toast_bytes,
       pg_total_relation_size(class.oid) AS total_bytes
FROM (SELECT unnest(%s::text[]) AS name) AS known
JOIN pg_class AS class
  ON class.oid = to_regclass(current_schema() || '.' || known.name)
"""

SQL_OP_PROCESSED = """
SELECT 'observations', endpoint, count(*) FROM collector_observations
GROUP BY endpoint
UNION ALL
SELECT 'ranked_day', state, count(*) FROM ranked_day_versions GROUP BY state
UNION ALL
SELECT 'army_decode', status, count(*) FROM battle_army_decodes
GROUP BY status
UNION ALL
SELECT 'snapshots', state, count(*) FROM leaderboard_snapshots
GROUP BY state
UNION ALL
SELECT 'outcomes', outcome, count(*) FROM observation_processing_outcomes
GROUP BY outcome
"""

SQL_OP_FAILURES = """
SELECT 'transport', count(*) FROM collector_transport_failures
UNION ALL
SELECT 'source_parses', count(*) FROM source_response_parses
WHERE outcome <> 'valid'
UNION ALL
SELECT 'processed_versions', count(*) FROM processed_observation_versions
WHERE outcome = 'failed'
"""

for _statement in (SQL_OP_IDENTITY, SQL_OP_QUEUES, SQL_OP_PYTHON_QUEUES,
                   SQL_OP_RELATIONS, SQL_OP_PROCESSED, SQL_OP_FAILURES):
    assert_read_only(_statement)

ALL_RO_STATEMENTS += (SQL_OP_IDENTITY, SQL_OP_QUEUES, SQL_OP_PYTHON_QUEUES,
                      SQL_OP_RELATIONS, SQL_OP_PROCESSED, SQL_OP_FAILURES)


def _jsonable(value):
    """Convert driver-native scalars to retained JSON scalars."""
    import datetime as _datetime
    import decimal as _decimal

    if isinstance(value, (_datetime.datetime, _datetime.date)):
        return value.isoformat()
    if isinstance(value, _decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _pgdata_probe(podman_bin: str, container: str,
                  expected_image: str | None = None) -> dict:
    """Bounded read-only PGDATA/pg_wal measurement inside the PG container.

    Runs as the container's existing default user (no --user override).
    Fail closed on unsafe names/paths, output/time caps, or identity change.
    """
    import subprocess

    result: dict = {"status": "unknown", "failure_code": None,
                    "captured_at": None, "container": container,
                    "image": None, "pgdata": None, "source": None,
                    "pgdata_bytes": None, "pg_wal_bytes": None}
    if not _CONTAINER.fullmatch(container or ""):
        result["failure_code"] = "pgdata_unsafe_container"
        return result
    if not podman_bin or "/" in podman_bin or "\\" in podman_bin \
            or " " in podman_bin:
        result["failure_code"] = "pgdata_unsafe_bin"
        return result
    try:
        ident = subprocess.run(
            [podman_bin, "container", "inspect", "--format",
             "{{.Image}}\n{{.ImageName}}", container],
            check=False, capture_output=True, text=True, timeout=30)
        if ident.returncode != 0:
            result["failure_code"] = "pgdata_inspect_unavailable"
            return result
        lines = ident.stdout.strip().splitlines()
        result["image"] = lines[0].strip() if lines else None
        if expected_image is not None and result["image"] != expected_image:
            result["failure_code"] = "pgdata_image_changed"
            return result
        env = subprocess.run(
            [podman_bin, "exec", container, "printenv", "PGDATA"],
            check=False, capture_output=True, text=True, timeout=30)
        if env.returncode != 0 or len(env.stdout.encode()) > 4096:
            result["failure_code"] = "pgdata_unresolvable"
            return result
        pgdata = env.stdout.strip()
        if not pgdata.startswith("/") or ".." in pgdata.split("/") \
                or any(ord(c) < 32 for c in pgdata):
            result["failure_code"] = "pgdata_unsafe_path"
            return result
        result["pgdata"] = pgdata
        measured = subprocess.run(
            [podman_bin, "exec", container, "du", "-sb",
             pgdata, pgdata + "/pg_wal"],
            check=False, capture_output=True, text=True, timeout=60)
        if measured.returncode != 0 \
                or len(measured.stdout.encode()) > 4096:
            result["failure_code"] = "pgdata_measure_failed"
            return result
        sizes = {}
        for line in measured.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit():
                sizes[parts[1]] = int(parts[0])
        if pgdata not in sizes:
            result["failure_code"] = "pgdata_measure_malformed"
            return result
        result["pgdata_bytes"] = sizes[pgdata]
        result["pg_wal_bytes"] = sizes.get(pgdata + "/pg_wal")
        result["source"] = "podman-exec:" + container
        result["captured_at"] = _utc_now().isoformat()
        result["status"] = "captured"
        return result
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        result["failure_code"] = "pgdata_probe_unavailable"
        return result


TRANSFER_PRIOR_ATTEMPTS = 21
S3_PRIOR_PROVENANCE = "retained-qualification-21-requests"
S3_PRIOR_PROVENANCE_MAX = 512
TRANSFER_PRIOR_BYTES = 21 * 1024**2
TRANSFER_PRIOR_PROVENANCE = (
    "documented-upper-bound:21-bounded-qualification-requests-x-1MiB-"\
    "response-ceiling")
TRANSFER_CUMULATIVE_MAX = 64 * 1024**3
S3_ATTEMPTS_MAX = 100_000


def _read_proc_net_dev() -> dict:
    """Kernel per-interface RX/TX byte counters; raises on miss."""
    counters: dict[str, dict] = {}
    for line in Path("/proc/net/dev").read_text().splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        fields = rest.split()
        if not name or len(fields) < 16:
            continue
        counters[name] = {"rx_bytes": int(fields[0]),
                          "tx_bytes": int(fields[8])}
    if not counters:
        raise OSError("empty proc net dev")
    return counters


def _interface_identity(interface: str) -> dict:
    """MAC/operstate pin for rename/replace detection; unknowns stay None."""
    result: dict = {"mac": None, "operstate": None, "error": None}
    base = Path("/sys/class/net") / interface
    try:
        if not base.is_dir() or base.is_symlink():
            result["error"] = "interface_not_present"
            return result
        result["mac"] = (base / "address").read_text().strip() or None
        result["operstate"] = (base / "operstate").read_text().strip() or None
    except OSError as error:
        result["error"] = "identity_unavailable:" + type(error).__name__
    return result


def _route_device(host: str, ip_bin: str = "ip") -> tuple[str | None, str | None]:
    """Resolve the egress device toward host; (dev, error)."""
    import subprocess

    try:
        completed = subprocess.run(
            [ip_bin, "-o", "route", "get", host],
            check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return None, "route_probe_unavailable:" + type(error).__name__
    if completed.returncode != 0 \
            or len(completed.stdout.encode()) > 4096:
        return None, "route_lookup_failed"
    match = re.search(r"\bdev\s+(\S+)", completed.stdout)
    if not match:
        return None, "route_device_unparseable"
    return match.group(1), None


def collect_wire_facts(*, interfaces: list[str], route_host: str | None,
                       ip_bin: str = "ip",
                       net_dev=None) -> dict:
    """Baseline/current host-wire facts; never raises."""
    try:
        counters = net_dev() if net_dev else _read_proc_net_dev()
    except OSError as error:
        return {"status": "unknown",
                "failure_code": "wire_counters_unavailable:" +
                                  type(error).__name__,
                "interfaces": {}}
    facts: dict[str, dict] = {}
    for interface in interfaces:
        if interface not in counters:
            facts[interface] = {"present": False,
                                "failure_code": "wire_interface_missing"}
            continue
        entry = {"present": True,
                 "rx_bytes": counters[interface]["rx_bytes"],
                 "tx_bytes": counters[interface]["tx_bytes"]}
        entry.update(_interface_identity(interface))
        if route_host:
            dev, error = _route_device(route_host, ip_bin)
            entry["route_dev"] = dev
            entry["route_error"] = error
        facts[interface] = entry
    return {"status": "captured", "failure_code": None,
            "boot_id": _boot_id(), "interfaces": facts}


def evaluate_wire(baseline: dict, current: dict,
                  prior_bytes: int) -> tuple[list[str], list[str], int | None]:
    """Conservative host-wire bound; all path traffic counts."""
    failures: list[str] = []
    unknown: list[str] = []
    if current.get("status") != "captured":
        return ["wire_unavailable"], [], None
    if (baseline.get("boot_id") or current.get("boot_id")) and \
            baseline.get("boot_id") != current.get("boot_id"):
        return ["wire_boot_changed"], [], None
    total = prior_bytes
    base_ifaces = baseline.get("interfaces") or {}
    for name in base_ifaces:
        if name not in (current.get("interfaces") or {}):
            failures.append("wire_interface_missing")
    for name, facts in (current.get("interfaces") or {}).items():
        if not facts.get("present"):
            failures.append("wire_interface_missing")
            continue
        old = base_ifaces.get(name)
        if old is None or not old.get("present"):
            unknown.append("wire_baseline_unknown")
            continue
        if facts["rx_bytes"] < old["rx_bytes"] \
                or facts["tx_bytes"] < old["tx_bytes"]:
            failures.append("wire_counter_reset")
            continue
        if facts.get("mac") != old.get("mac"):
            failures.append("wire_identity_changed")
        if facts.get("route_dev") is not None \
                and old.get("route_dev") is not None \
                and facts["route_dev"] != old["route_dev"]:
            failures.append("wire_route_changed")
        if facts.get("route_error"):
            failures.append("wire_route_unavailable")
        total += (facts["rx_bytes"] - old["rx_bytes"]) + \
                 (facts["tx_bytes"] - old["tx_bytes"])
    if not failures and total > TRANSFER_CUMULATIVE_MAX:
        failures.append("transfer_breach")
    return sorted(set(failures)), sorted(set(unknown)), total


def _worker_snapshots(run: dict, worker_probe=None) -> tuple[dict, str | None]:
    """Sum Python worker remote attempts across replicas; (totals, error)."""
    probe = worker_probe or _podman_worker_files
    try:
        files = probe(run)
    except Exception as error:  # noqa: BLE001 - probe miss is unknown
        return {}, "s3_worker_unavailable:" + type(error).__name__
    totals: dict[str, int] = {}
    for payload in files:
        try:
            attempts = payload["archive"]["remote_attempts"]
            for operation, count in attempts.items():
                totals[operation] = totals.get(operation, 0) + int(count)
        except (KeyError, TypeError, ValueError) as error:
            return {}, "s3_worker_malformed:" + type(error).__name__
    return totals, None


def _podman_worker_files(run: dict) -> list[dict]:
    """Read worker operating snapshots via podman exec cat."""
    import subprocess

    containers = run.get("containers", {}) or {}
    base = containers.get("python_worker")
    replicas = int(containers.get("worker_replicas", 0) or 0)
    podman_bin = run.get("podman_bin", "podman") or "podman"
    if not base or replicas < 1:
        raise RuntimeError("worker replicas unconfigured")
    snapshots = []
    for replica in range(1, replicas + 1):
        completed = subprocess.run(
            [podman_bin, "exec", f"{base}-{replica}", "cat",
             "/tmp/clashlens-worker-operating.json"],
            check=False, capture_output=True, text=True, timeout=30)
        if completed.returncode != 0 \
                or len(completed.stdout.encode()) > 65536:
            raise RuntimeError(f"worker {replica} snapshot unavailable")
        snapshots.append(json.loads(completed.stdout))
    return snapshots


def _s3_decreased(previous: dict | None, current: dict) -> bool:
    if not previous:
        return False
    for section in ("go", "python"):
        for key, value in (current.get(section) or {}).items():
            old = (previous.get(section) or {}).get(key)
            if isinstance(old, int) and isinstance(value, int) and value < old:
                return True
    return False


def _s3_snapshot(metrics: dict | None, py_totals: dict) -> dict:
    """Go + Python attempt totals from query-free counters."""
    go_total = 0
    go: dict[str, int] = {}
    for key, value in ((metrics or {}).get("counters") or {}).items():
        if key.startswith("clashlens_collector_archive_requests_total{"):
            go[key] = int(value)
            go_total += int(value)
    py_total = sum(py_totals.values())
    return {"go": go, "go_total": go_total, "python": dict(py_totals),
            "python_total": py_total, "total": go_total + py_total}


TARIFF_MAX_BYTES = 65536
TARIFF_STALE_DAYS = 30
TARIFF_EXPECTED = {
    "payload_cap_gib": 16,
    "aggregate_transfer_cap_gib": 64,
    "retention_projection_days": 186,
    "uncertainty_multiplier": 1.5,
    "absolute_preparation_ceiling_eur": 5,
}


def _read_tariff_file(path_str: str) -> dict:
    """Read the protected verified tariff JSON; fail closed, never estimate."""
    if not path_str or not os.path.isabs(path_str):
        raise Step9Error("tariff_unavailable",
                         "tariff file path must be absolute")
    try:
        descriptor = os.open(path_str, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise Step9Error("tariff_unavailable",
                         "tariff file is unreadable") from error
    try:
        size = os.fstat(descriptor).st_size
        if size > TARIFF_MAX_BYTES:
            raise Step9Error("tariff_malformed", "tariff file is too large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(TARIFF_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > TARIFF_MAX_BYTES:
        raise Step9Error("tariff_malformed", "tariff file is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Step9Error("tariff_malformed",
                         "tariff file is not valid JSON") from error
    if not isinstance(payload, dict):
        raise Step9Error("tariff_malformed", "tariff file is not an object")
    return payload


def _tariff_decimal(value, label: str) -> Decimal:
    """Strict finite decimal; rejects NaN/Infinity/missing values."""
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise Step9Error("tariff_malformed",
                         f"tariff {label} is invalid") from error
    if not math.isfinite(number):
        raise Step9Error("tariff_malformed",
                         f"tariff {label} is not finite")
    return Decimal(str(value))


def _tariff_block(payload: dict, core_start: datetime) -> dict:
    """Validate exact bounds/provenance; the envelope is reported, not billed."""
    for key, expected in TARIFF_EXPECTED.items():
        if payload.get(key) != expected:
            raise Step9Error("tariff_mismatch",
                             f"tariff {key} differs from the approved envelope")
    rate_hour = _tariff_decimal(payload.get("tariff_eur_per_decimal_gb_hour"),
                                "tariff_eur_per_decimal_gb_hour")
    rate_egress = _tariff_decimal(payload.get("egress_eur_per_decimal_gb"),
                                  "egress_eur_per_decimal_gb")
    if rate_hour <= 0 or rate_egress <= 0:
        raise Step9Error("tariff_malformed", "tariff rates must be positive")
    operational_stop = _tariff_decimal(payload.get("operational_stop_eur"),
                                       "operational_stop_eur")
    absolute_cap = _tariff_decimal(
        payload.get("absolute_preparation_ceiling_eur"),
        "absolute_preparation_ceiling_eur")
    if operational_stop != Decimal("4.5"):
        raise Step9Error("tariff_mismatch",
                         "tariff operational stop is not EUR 4.50")
    if absolute_cap != Decimal(5):
        raise Step9Error("tariff_mismatch",
                         "tariff absolute ceiling is not EUR 5")
    _verify_tariff_math(payload, rate_hour, rate_egress)
    try:
        verified = datetime.strptime(
            payload["verified_utc_date"], "%Y-%m-%d").replace(tzinfo=UTC)
    except (KeyError, ValueError, TypeError) as error:
        raise Step9Error("tariff_malformed",
                         "tariff verified date is invalid") from error
    age_days = (core_start - verified).total_seconds() / 86400
    if age_days < 0 or age_days > TARIFF_STALE_DAYS:
        raise Step9Error("tariff_stale",
                         "tariff verification is stale or in the future")
    for key in ("with_uncertainty_eur", "operational_stop_eur"):
        _tariff_decimal(payload.get(key), key)
    if not payload.get("source"):
        raise Step9Error("tariff_malformed", "tariff source is missing")
    return {
        "digest": _sha256(json.dumps(payload, sort_keys=True).encode()),
        "source": payload["source"],
        "verified_utc_date": payload["verified_utc_date"],
        "retention_projection_days": payload["retention_projection_days"],
        "tariff_eur_per_decimal_gb_hour": payload[
            "tariff_eur_per_decimal_gb_hour"],
        "egress_eur_per_decimal_gb": payload["egress_eur_per_decimal_gb"],
        "payload_cap_gib": payload["payload_cap_gib"],
        "aggregate_transfer_cap_gib": payload["aggregate_transfer_cap_gib"],
        "uncertainty_multiplier": payload["uncertainty_multiplier"],
        "with_uncertainty_eur": payload["with_uncertainty_eur"],
        "operational_stop_eur": payload["operational_stop_eur"],
        "absolute_preparation_ceiling_eur": payload[
            "absolute_preparation_ceiling_eur"],
        "note": "tariff estimate only, never actual billed cost",
    }


def _verify_tariff_math(payload: dict, rate_hour: Decimal,
                        rate_egress: Decimal) -> None:
    """Recompute the approved envelope from rates and approved quantities."""
    import math as _math

    try:
        storage_gb = Decimal(str(payload["rounded_storage_decimal_gb"]))
        transfer_gb = Decimal(str(
            payload["conservative_all_transfer_egress_decimal_gb_rounded"]))
        days = Decimal(str(payload["retention_projection_days"]))
        uncertainty = Decimal(str(payload["uncertainty_multiplier"]))
        gib = Decimal(2**30) / Decimal(10**9)
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise Step9Error("tariff_malformed",
                         "tariff projection inputs are invalid") from error
    for value in (storage_gb, transfer_gb, days, uncertainty):
        if not value.is_finite() or value <= 0:
            raise Step9Error("tariff_malformed",
                             "tariff projection inputs are invalid")
    if storage_gb != _math.ceil(Decimal(16) * gib) \
            or transfer_gb != _math.ceil(Decimal(64) * gib):
        raise Step9Error("tariff_mismatch",
                         "tariff rounded quantities disagree with caps")
    expected = {
        "storage_projection_eur": storage_gb * days * 24 * rate_hour,
        "egress_projection_eur": transfer_gb * rate_egress,
    }
    expected["combined_projection_eur"] = (
        expected["storage_projection_eur"] + expected["egress_projection_eur"])
    expected["with_uncertainty_eur"] = (
        expected["combined_projection_eur"] * uncertainty)
    for key, value in expected.items():
        try:
            stated = Decimal(str(payload[key]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise Step9Error("tariff_malformed",
                             f"tariff {key} is invalid") from error
        if stated != value:
            raise Step9Error("tariff_mismatch",
                             f"tariff {key} disagrees with rates and caps")


def _s3_prior_block(arguments: argparse.Namespace) -> dict:
    """Mandatory rehearsal prior: bounded int, bounded provenance."""
    raw_attempts = getattr(arguments, "prior_s3_attempts", None)
    raw_provenance = getattr(arguments, "prior_s3_provenance", None)
    attempts = TRANSFER_PRIOR_ATTEMPTS if raw_attempts is None else raw_attempts
    provenance = S3_PRIOR_PROVENANCE if raw_provenance is None else raw_provenance
    if isinstance(attempts, bool) or not isinstance(attempts, int) \
            or attempts < 0 or attempts > S3_ATTEMPTS_MAX:
        raise Step9Error("s3_prior_invalid",
                         "prior S3 attempts must be an integer 0..100000")
    if not isinstance(provenance, str) or not provenance \
            or len(provenance) > S3_PRIOR_PROVENANCE_MAX:
        raise Step9Error("s3_prior_invalid",
                         "prior S3 provenance must be nonempty and bounded")
    record = {"attempts": attempts, "provenance": provenance}
    record["record_sha256"] = _sha256(_canonical(record))
    return record


def _s3_prior(run: dict) -> dict:
    prior = run.get("s3_prior") or {}
    if isinstance(prior.get("attempts"), int) \
            and isinstance(prior.get("provenance"), str):
        return prior
    return {"attempts": TRANSFER_PRIOR_ATTEMPTS,
            "provenance": S3_PRIOR_PROVENANCE,
            "record_sha256": _sha256(_canonical({
                "attempts": TRANSFER_PRIOR_ATTEMPTS,
                "provenance": S3_PRIOR_PROVENANCE}))}


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
    reopening = next((e for e in events if e["gate_allowed"]
                      and e.get("gate_handoff_at") is not None), None)
    handoff = (reopening["gate_handoff_at"] if reopening
               else header.get("handoff_at"))
    suppression_start = core_start + mode["interval"] - timedelta(minutes=5)
    suppression_end = handoff or (core_start + mode["interval"])
    if events and max_gap_seconds is not None:
        head_gap = (events[0]["database_at"]
                    - header["capture_start"]).total_seconds()
        if head_gap > max_gap_seconds:
            unknown.append("admission_head_gap")
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
        if suppression_start <= database_at < suppression_end and (
                event["gate_allowed"] or event["selected_count"]
                or event["inserted_count"]):
            failures.append("admission_suppression_breach")
        if event["gate_allowed"]:
            if handoff is not None and reopening is event \
                    and database_at < handoff:
                failures.append("admission_early_reopen")
            grace = event.get("gate_handoff_at") or handoff
            for due_at in _event_selected_due(event):
                effective = max(due_at, grace) if grace else due_at
                if database_at > effective + DEADLINE_ALLOWANCE:
                    failures.append("admission_selected_late_recomputed")
                    slot["late"] += 1
                    break
    tail_need = max(core_start + mode["interval"],
                    handoff or core_start) + DEADLINE_ALLOWANCE
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
                                "max_cycle_at": str(intents[2])}}


SQL_ARCHIVE_USAGE = """
SELECT COALESCE(sum(byte_size), 0), count(*)
FROM archive_catalogue
"""

for _statement in (SQL_ARCHIVE_USAGE,):
    assert_read_only(_statement)

ALL_RO_STATEMENTS += (SQL_ARCHIVE_USAGE,)

RES_FS_USE_PCT_MAX = 80.0
RES_FS_FREE_MIN = 200 * 1024**3
RES_PHYS_GROWTH_MAX = 64 * 1024**3
RES_BTRFS_METADATA_MAX = 80.0
RES_BTRFS_UNALLOC_MIN = 100 * 1024**3
RES_MEM_AVAIL_MIN = 4 * 1024**3
RES_ARCHIVE_LOGICAL_MAX = 16 * 1024**3
RES_ARCHIVE_PHYSICAL_MAX = 64 * 1024**3
RES_ARCHIVE_OBJECTS_MAX = 100_000


def _parse_btrfs_usage(stdout: str) -> dict:
    meta_size = meta_used = 0
    unallocated = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        match = re.fullmatch(
            r"(Metadata\w*),[^:]*: Size: (\d+), Used: (\d+).*", line)
        if match:
            meta_size += int(match.group(2))
            meta_used += int(match.group(3))
            continue
        match = re.fullmatch(r"Unallocated:\s*(\d+).*", line)
        if match:
            unallocated = int(match.group(1))
    metadata_pct = (100.0 * meta_used / meta_size) if meta_size else None
    return {"metadata_pct": metadata_pct, "unallocated_bytes": unallocated}


def _btrfs_probe_numbers(target: str) -> dict:
    result: dict = {"metadata_pct": None, "unallocated_bytes": None,
                    "error": None, "stderr": None}
    try:
        from scripts import spool_filesystem_check as spool_check

        probe = spool_check._btrfs_probe(Path(target))
    except Exception as error:  # noqa: BLE001 - probe miss is unknown
        result["error"] = "probe_unavailable:" + type(error).__name__
        return result
    if probe.get("error"):
        result["error"] = probe["error"]
        return result
    if probe.get("stderr"):
        result["stderr"] = "diagnostic_stderr"
    parsed = _parse_btrfs_usage(probe.get("stdout", ""))
    result.update(parsed)
    if parsed["metadata_pct"] is None and parsed["unallocated_bytes"] is None:
        result["error"] = result["error"] or "allocation_evidence_missing"
    return result


def evaluate_resource_gates(baseline: dict, current: dict,
                            mem_over: int) -> tuple[list[str], list[str], int]:
    """Compare real probe fields against Phase 4 thresholds.

    Returns (failures, unknowns, mem_over): null stays unknown/failure,
    never zero. mem_over counts consecutive memory-over samples.
    """
    failures: list[str] = []
    unknown: list[str] = []
    base_fs = (baseline.get("filesystems") or {})
    for key, facts in (current.get("filesystems") or {}).items():
        use_pct = facts.get("use_pct")
        free_b = facts.get("free_bytes")
        if use_pct is None or free_b is None:
            unknown.append("filesystem_unknown")
        else:
            if use_pct >= RES_FS_USE_PCT_MAX:
                failures.append("filesystem_use_breach")
            if free_b < RES_FS_FREE_MIN:
                failures.append("filesystem_free_breach")
        old = base_fs.get(key, {})
        if facts.get("used_bytes") is not None \
                and old.get("used_bytes") is not None:
            if facts["used_bytes"] - old["used_bytes"] > RES_PHYS_GROWTH_MAX:
                failures.append("physical_growth_breach")
        else:
            unknown.append("physical_growth_unknown")
        btrfs = facts.get("btrfs") or {}
        is_btrfs = facts.get("filesystem_type") == "btrfs"
        if btrfs.get("error"):
            (failures if is_btrfs else unknown).append(
                "btrfs_evidence_" + str(btrfs["error"]))
        if btrfs.get("stderr"):
            failures.append("btrfs_diagnostic_stderr")
        old_btrfs = old.get("btrfs") or {}
        if old_btrfs.get("error") is None and btrfs.get("error"):
            failures.append("btrfs_new_error")
        if is_btrfs:
            meta_pct = btrfs.get("metadata_pct")
            if meta_pct is None:
                failures.append("btrfs_metadata_unproven")
            elif meta_pct >= RES_BTRFS_METADATA_MAX:
                failures.append("btrfs_metadata_breach")
            unalloc = btrfs.get("unallocated_bytes")
            if unalloc is None:
                failures.append("btrfs_unallocated_unproven")
            elif unalloc < RES_BTRFS_UNALLOC_MIN:
                failures.append("btrfs_unallocated_breach")
            device = btrfs.get("device") or {}
            if device.get("error"):
                failures.append("btrfs_device_stats_unavailable")
            else:
                old_dev = (old_btrfs.get("device") or {}).get("errors", {})
                for kind, count in (device.get("errors") or {}).items():
                    if int(count) > int(old_dev.get(kind, 0)):
                        failures.append("btrfs_device_error")
                        break
    mem = current.get("memory") or {}
    old_mem = baseline.get("memory") or {}
    cgroup = current.get("cgroup") or {}
    old_cgroup = baseline.get("cgroup") or {}
    oom_now = cgroup.get("oom_kills", mem.get("oom_kills"))
    oom_old = old_cgroup.get("oom_kills", old_mem.get("oom_kills"))
    if oom_now is not None and oom_old is not None:
        if oom_now > oom_old:
            failures.append("oom_kill_observed")
    else:
        unknown.append("oom_unknown")
    cgroup_swap = cgroup.get("swap_current_bytes")
    old_cgroup_swap = old_cgroup.get("swap_current_bytes")
    if cgroup_swap is not None and old_cgroup_swap is not None:
        if cgroup_swap > old_cgroup_swap:
            failures.append("swap_growth")
    elif mem.get("swap_used_bytes") is not None \
            and old_mem.get("swap_used_bytes") is not None:
        if mem["swap_used_bytes"] > old_mem["swap_used_bytes"]:
            failures.append("swap_growth")
    else:
        unknown.append("swap_unknown")
    if mem.get("available_bytes") is None:
        unknown.append("memory_unknown")
    elif mem["available_bytes"] < RES_MEM_AVAIL_MIN:
        mem_over += 1
        if mem_over >= 2:
            failures.append("memory_low")
    else:
        mem_over = 0
    archive = current.get("archive") or {}
    if archive.get("logical_bytes") is None or archive.get("objects") is None:
        unknown.append("archive_unknown")
    else:
        if archive["logical_bytes"] > RES_ARCHIVE_LOGICAL_MAX:
            failures.append("archive_logical_breach")
        if archive["objects"] > RES_ARCHIVE_OBJECTS_MAX:
            failures.append("archive_objects_breach")
    if archive.get("physical_bytes") is None:
        unknown.append("archive_physical_unknown")
    elif archive["physical_bytes"] > RES_ARCHIVE_PHYSICAL_MAX:
        failures.append("archive_physical_breach")
    return sorted(set(failures)), sorted(set(unknown)), mem_over


def collect_resource_facts(*, spool_path: str, postgres_path: str,
                           db: object | None, metrics: dict | None,
                           btrfs_probe=None, device_probe=None) -> dict:
    probe = btrfs_probe or _btrfs_probe_numbers
    dev_probe = device_probe or _btrfs_device_stats
    filesystems: dict[str, dict] = {}
    for label, target in (("spool", spool_path), ("postgres", postgres_path)):
        try:
            facts = filesystem_facts(target, target)[label]
        except Exception:  # noqa: BLE001 - probe miss is unknown
            facts = {"mount_point": None, "source": None,
                     "filesystem_type": "unknown", "mnt_id": None,
                     "free_bytes": None, "error": "filesystem_unavailable"}
        try:
            stat = os.statvfs(target)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            use_pct = 100.0 * used / total if total else None
        except OSError:
            total, free, used, use_pct = None, facts.get("free_bytes"), None, None
        key = str(facts.get("source") or facts.get("mount_point") or label)
        entry = filesystems.setdefault(key, {
            "key": key, "mount_point": facts.get("mount_point"),
            "source": facts.get("source"), "mnt_id": facts.get("mnt_id"),
            "filesystem_type": facts.get("filesystem_type"),
            "total_bytes": total, "free_bytes": free, "used_bytes": used,
            "use_pct": use_pct, "labels": [],
            "btrfs": probe(target), "error": facts.get("error")})
        entry["labels"].append(label)
        if facts.get("filesystem_type") == "btrfs":
            entry["btrfs"]["device"] = dev_probe(target)
        else:
            entry["btrfs"]["device"] = {"errors": {}, "error": None,
                                            "skipped": "not_btrfs"}
    pressure = host_pressure()
    memory = pressure.get("memory") or {}
    try:
        text = Path("/proc/vmstat").read_text()
        oom_kills = int(text.split("oom_kill ")[1].split()[0])
    except (OSError, IndexError, ValueError):
        oom_kills = None
    try:
        psi = Path("/proc/pressure/memory").read_text()
        some = next(line for line in psi.splitlines()
                    if line.startswith("some"))
        psi_avg10 = float(some.split("avg10=")[1].split()[0])
    except (OSError, IndexError, ValueError, StopIteration):
        psi_avg10 = None
    total_b = memory.get("total_bytes")
    avail_b = memory.get("available_bytes")
    swap_total = memory.get("swap_total_bytes")
    swap_free = memory.get("swap_free_bytes")
    archive: dict = {"logical_bytes": None, "objects": None,
                     "physical_bytes": None,
                     "error": None}
    if db is not None:
        try:
            logical, objects = db.archive_usage()
            archive["logical_bytes"] = int(logical)
            archive["objects"] = int(objects)
        except Exception as error:  # noqa: BLE001 - probe miss is unknown
            archive["error"] = "catalogue_unavailable:" + type(error).__name__
    else:
        archive["error"] = "database_unavailable"
    spool_bytes = None
    if metrics is not None:
        for name, value in (metrics.get("counters") or {}).items():
            if "spool_final_bytes" in name:
                spool_bytes = value
                break
    if isinstance(spool_bytes, (int, float)):
        archive["physical_bytes"] = int(spool_bytes)
    return {"filesystems": filesystems,
            "memory": {"used_bytes": (total_b - avail_b
                                          if total_b is not None
                                          and avail_b is not None else None),
                         "available_bytes": avail_b,
                         "swap_used_bytes": (swap_total - swap_free
                                              if swap_total is not None
                                              and swap_free is not None
                                              else None),
                         "oom_kills": oom_kills, "psi_avg10": psi_avg10,
                         "error": pressure.get("error")},
            "archive": archive}


if __name__ == "__main__":
    sys.exit(main())

