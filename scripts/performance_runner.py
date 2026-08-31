#!/usr/bin/env python3
"""Run issue #60 Step 8 workloads against an isolated PostgreSQL schema."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import platform
import re
import resource
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
sys.path[:0] = [str(ROOT), str(PYTHON / "src"), str(PYTHON / "tests")]

MODES = (
    "reset-boundary",
    "correction",
    "duplicate-heavy",
    "mixed-backfill",
    "coordinator-12500",
    "army-analytics",
)
DUPLICATE_ENDPOINT_MIX = {
    "profile": 12_500,
    "battle_log": 12_500,
    "global_player_rankings": 24,
}
DUPLICATE_EXECUTION_CAP = sum(DUPLICATE_ENDPOINT_MIX.values())
ARTIFACT_SCHEMA_VERSION = 9
CANDIDATE_RECEIPT_SCHEMA_VERSION = 2
REQUIRED_MIGRATION_VERSIONS = tuple(range(1, 16))
CANONICAL_REPOSITORY_URL = "https://github.com/zzzubair/clashlens"
CONFIGURATION_KEYS = {
    "mode",
    "post_fix",
    "populations",
    "duplicate_observations",
    "live_jobs",
    "backfill_jobs",
    "army_facts",
    "lanes",
    "effective_lanes",
    "army_warmups",
    "army_requests",
    "analytics_lanes",
    "duplicate_cycles",
    "duplicate_endpoint_mix",
    "skip_collector_probe",
}
# Hard failures are an artifact contract, not a log channel.  Keep this
# vocabulary finite so a player tag, job id, exception, or timing detail can
# never become retained evidence by accident.
HARD_FAILURE_CODES = frozenset(
    {
        "fixed_acceptance_failure",
        "reset_generation_count_mismatch",
        "reset_fanout_mismatch",
        "reset_non_processed_result",
        "reset_queue_residue",
        "army_read_sample_unavailable",
        "step5_overlap_incomplete",
        "step5_collection_result_count_mismatch",
        "step5_non_processed_result",
        "step5_forced_miss_exceeded",
        "step5_cgroup_unavailable",
        "step5_memory_pressure_increased",
        "step5_p95_exceeded",
        "step5_account_overlap_incomplete",
        "step5_account_p95_exceeded",
        "step5_collection_cycle_too_slow",
        "mixed_result_count_mismatch",
        "mixed_non_processed_result",
        "mixed_live_latency_exceeded",
        "mixed_collection_latency_exceeded",
        "mixed_queue_residue",
        "memory_pressure_unavailable",
        "memory_pressure_increased",
        "queue_residue",
    }
)
ALLOWED_HARD_FAILURE_CODES = HARD_FAILURE_CODES
MAX_RETAINED_FAILURE_CODES = 64
MAX_COMPLETION_ORDER = 256
MAX_ARTIFACT_SAMPLES = 32
MAX_RETAINED_SEQUENCE = 4096
# Raw EXPLAIN metadata is validated only for safe traversal before discard.
# Its backend-generated SQL expressions must not use public text semantics.
MAX_EXPLAIN_DETAIL_DEPTH = 8
MAX_EXPLAIN_DETAIL_ITEMS = 4096
MAX_EXPLAIN_DETAIL_SEQUENCE = 4096
MAX_EXPLAIN_DETAIL_TEXT = 16_384
MAX_EXPLAIN_DETAIL_KEY = 256
MAX_EXPLAIN_DETAIL_MAPPING = 64


def _failure_codes(values: Any) -> list[str]:
    """Deduplicate and bound the finite acceptance-code vocabulary."""
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in HARD_FAILURE_CODES:
            raise ValueError("unknown hard failure code")
        if value in result:
            continue
        result.append(value)
        if len(result) == MAX_RETAINED_FAILURE_CODES:
            break
    return result
AFFECTED_RELATIONS = (
    "players",
    "collector_jobs",
    "collector_attempts",
    "collector_endpoint_results",
    "collector_observations",
    "archive_catalogue",
    "python_processing_jobs",
    "python_processing_attempts",
    "observation_processing_outcomes",
    "parsed_source_payloads",
    "player_profile_versions",
    "player_profile_effects",
    "season_anchor_evidence",
    "battle_log_observations",
    "battle_source_rows",
    "legend_battles",
    "battle_evidence",
    "battle_perspectives",
    "known_player_discoveries",
    "official_top200_attempts",
    "official_top200_versions",
    "official_top200_entries",
    "battle_log_observation_rows",
    "official_top200_version_entries",
    "official_top200_attempt_entries",
    "ranked_day_versions",
    "leaderboard_snapshots",
    "leaderboard_snapshot_entries",
    "analytics_summaries",
    "army_analytics_battle_facts",
)
STEP5_MODE = "army-analytics"
STEP5_POPULATION = 12_500
STEP5_DAYS = 28
STEP5_FACTS_PER_MEMBER_DAY = 8
STEP5_SELECTED_MEMBERS = 1_000
STEP5_MISSING_TROPHY_RATE = 100
STEP5_WARMUPS = 5
STEP5_REQUESTS = 100
STEP5_ANALYTICS_LANES = 4
STEP5_MIXED_LANE_POOL_MAX_SIZE = 2
STEP5_P95_TARGET_MS = 200.0
STEP5_FORCED_MISS_TARGET_SECONDS = 5.0
STEP5_COLLECTION_LIMIT_SECONDS = 300.0
STEP5_STATISTICS_TIMEOUT_SECONDS = 600
STEP5_STATISTICS_RELATIONS = (
    "api_player_daily_logs",
    "army_analytics_completed_days",
    "leaderboard_snapshots",
    "leaderboard_snapshot_entries",
    "ranked_day_versions",
    "army_analytics_battle_facts",
)
STEP5_TROOP_KEYS = tuple(sorted(f"troop:{index}" for index in range(27)))
BATTLE_FIXTURE = (PYTHON / "testdata" / "legend_i_battle_log_v1.json").read_bytes()
RANKING_FIXTURE = (PYTHON / "testdata" / "global_top_200_v1.json").read_bytes()
_LANES = 32
BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY_START = BOUNDARY - timedelta(days=1)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _source_migrations() -> list[dict[str, str]]:
    migrations: list[dict[str, str]] = []
    versions: list[int] = []
    for path in sorted((ROOT / "deploy/migrations").glob("*.sql")):
        match = re.fullmatch(r"([0-9]{4})_[a-z0-9_]+\.sql", path.name)
        if match is None:
            raise RuntimeError("migration filename is invalid")
        versions.append(int(match.group(1)))
        migrations.append({"name": path.name, "sha256": _sha(path.read_bytes())})
    if tuple(versions) != REQUIRED_MIGRATION_VERSIONS:
        raise RuntimeError("source migrations are incomplete or out of date")
    return migrations


def _clean_source() -> str:
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError("source checkout must be clean")
    revision = _git("rev-parse", "--verify", "HEAD^{commit}")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError("source revision is invalid")
    return revision


def _bounded_writer_source_ready(
    source: str, function_name: str, required_sql: str
) -> bool:
    """Reject a writer that executes SQL from inside its population loop."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    if len(functions) != 1:
        return False
    function = functions[0]
    function_source = ast.get_source_segment(source, function) or ""
    if required_sql not in function_source:
        return False
    for loop in ast.walk(function):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            continue
        for call in ast.walk(loop):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if (
                call.func.attr in {"execute", "executemany"}
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "connection"
            ):
                return False
    return True


def _bounded_army_source_ready(source: str) -> bool:
    """Reject the pre-PR1 season/day/lens-only fact materialization shape."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "get_army_analytics"
    ]
    if len(functions) != 1:
        return False
    function_source = ast.get_source_segment(source, functions[0]) or ""
    return (
        "population_player_id = ANY(%s::bigint[])" in function_source
        and "battle_time_trophies BETWEEN %s AND %s" in function_source
        and "component_column" in function_source
    )


def post_fix_source_ready() -> bool:
    """Require coordinator ownership and bounded snapshot/army writers."""
    migrations = "\n".join(
        path.read_text() for path in sorted((ROOT / "deploy/migrations").glob("*.sql"))
    )
    database = (PYTHON / "src/clashlens/db.py").read_text()
    return (
        "boundary_publication_generation" in migrations
        and _bounded_writer_source_ready(
            database, "_publish_snapshot_kind", "jsonb_to_recordset"
        )
        and _bounded_writer_source_ready(
            database, "_build_army_facts", "jsonb_to_recordset"
        )
        and "build_snapshot:ranked-day-version:" not in database
    )


def validate_reset(populations: list[int], post_fix: bool) -> None:
    if (
        not populations
        or len(populations) > MAX_ARTIFACT_SAMPLES
        or any(value < 1 for value in populations)
    ):
        raise ValueError("--populations requires positive integers")
    if any(value >= 12_500 for value in populations):
        if not post_fix:
            raise ValueError(
                "refusing reset population >= 12,500; pass --post-fix after the bounded writer gate passes"
            )
        if not post_fix_source_ready():
            raise ValueError(
                "--post-fix requires both bounded Step 4 reset writers"
            )


class _ArchiveHandler(BaseHTTPRequestHandler):
    objects: ClassVar[dict[str, bytes]] = {}
    gets = 0
    get_bytes = 0
    heads = 0
    puts = 0
    put_bytes = 0
    conditional_puts = 0
    conflicts = 0
    counter_lock: ClassVar[Lock] = Lock()

    def log_message(self, format: str, *arguments: object) -> None:
        del format, arguments

    def do_GET(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        with type(self).counter_lock:
            body = type(self).objects.get(key)
            type(self).gets += 1
            if body is not None:
                type(self).get_bytes += len(body)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Amz-Meta-Sha256", _sha(body))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        with type(self).counter_lock:
            type(self).heads += 1
        self.send_response(200)
        self.end_headers()

    def do_PUT(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with type(self).counter_lock:
            type(self).puts += 1
            type(self).put_bytes += len(body)
            if self.headers.get("If-None-Match") == "*":
                type(self).conditional_puts += 1
            conflict = key in type(self).objects
            if conflict:
                type(self).conflicts += 1
            else:
                type(self).objects[key] = body
        if conflict:
            self.send_response(412)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()


@contextmanager
def count_sql_calls() -> Iterator[list[int]]:
    import psycopg

    count = [0]
    original_cursor = psycopg.Cursor.execute
    original_many = psycopg.Cursor.executemany

    def cursor_execute(cursor: Any, *args: Any, **kwargs: Any) -> Any:
        count[0] += 1
        return original_cursor(cursor, *args, **kwargs)

    def cursor_executemany(cursor: Any, *args: Any, **kwargs: Any) -> Any:
        count[0] += 1
        return original_many(cursor, *args, **kwargs)

    psycopg.Cursor.execute = cursor_execute
    psycopg.Cursor.executemany = cursor_executemany
    try:
        yield count
    finally:
        psycopg.Cursor.execute = original_cursor
        psycopg.Cursor.executemany = original_many


@contextmanager
def capture_sql_calls() -> Iterator[list[dict[str, Any]]]:
    """Capture the SQL and parameters issued by one production API call."""
    import copy

    import psycopg

    calls: list[dict[str, Any]] = []
    original_cursor = psycopg.Cursor.execute

    def cursor_execute(cursor: Any, query: Any, params: Any = None, *args: Any, **kwargs: Any) -> Any:
        text = str(query).strip()
        if text.upper().startswith(("SELECT", "WITH")):
            calls.append({"sql": text, "params": copy.deepcopy(params)})
        return original_cursor(cursor, query, params, *args, **kwargs)

    psycopg.Cursor.execute = cursor_execute
    try:
        yield calls
    finally:
        psycopg.Cursor.execute = original_cursor


@contextmanager
def archive_server() -> Iterator[tuple[str, str, str, type[_ArchiveHandler]]]:
    handler = type(
        "PerformanceArchiveHandler",
        (_ArchiveHandler,),
        {
            "objects": {},
            "gets": 0,
            "get_bytes": 0,
            "heads": 0,
            "puts": 0,
            "put_bytes": 0,
            "conditional_puts": 0,
            "conflicts": 0,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", "", "", handler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _tag(index: int) -> str:
    alphabet = "0289PYLQGRJCUV"
    encoded = ""
    while index:
        encoded = alphabet[index % len(alphabet)] + encoded
        index //= len(alphabet)
    return "#P" + encoded.rjust(5, "0")


def _processor(connection_info: str, archive: tuple[str, str, str, Any]):
    from clashlens.archive import S3ArchiveReader, SpoolFirstReader
    from clashlens.db import Database
    from clashlens.worker import ObservationProcessor, StageMetrics

    metrics = StageMetrics()
    database = Database(connection_info, max_size=_LANES)
    s3 = S3ArchiveReader(
        endpoint=archive[0],
        bucket="evidence",
        access_key="test",
        secret_key="test",
        secure=False,
        allow_insecure_test_origin=True,
        pool_size=_LANES,
    )
    spool = SpoolFirstReader(
        s3,
        spool_root=tempfile.mkdtemp(prefix="clashlens-perf-spool-"),
        stage_metrics=metrics,
    )
    return database, ObservationProcessor(database, spool, metrics), metrics, spool


def _profile_body(tag: str, variant: int = 0) -> bytes:
    source = json.loads((PYTHON / "testdata/legend_i_profile_v1.json").read_text())
    source["tag"] = tag
    if variant:
        source["expLevel"] = int(source.get("expLevel", 1)) + variant
    return json.dumps(source, separators=(",", ":")).encode()


def _seed_fixture_discoveries(
    connection_info: str, *fixture_bodies: bytes
) -> int:
    """Pre-qualify committed fixture discoveries without manufacturing work."""
    import psycopg

    tags: set[str] = set()
    for body in fixture_bodies:
        payload = json.loads(body)
        for item in payload.get("items", []):
            tag = item.get("opponentPlayerTag") or item.get("tag")
            if isinstance(tag, str) and tag:
                tags.add(tag)
    if not tags:
        return 0
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            INSERT INTO players (normalized_tag, active, eligibility_state)
            SELECT tag, true, 'eligible' FROM unnest(%s::text[]) AS item(tag)
            ON CONFLICT (normalized_tag) DO UPDATE
                SET active = true, eligibility_state = 'eligible'
            """,
            (sorted(tags),),
        )
        connection.commit()
    return len(tags)


def _duplicate_endpoint_mix(count: int) -> dict[str, int]:
    """Return the production mix, balancing smaller test populations."""
    if count >= DUPLICATE_EXECUTION_CAP:
        return dict(DUPLICATE_ENDPOINT_MIX)
    endpoints = tuple(DUPLICATE_ENDPOINT_MIX)
    base, remainder = divmod(count, len(endpoints))
    return {
        endpoint: base + int(index < remainder)
        for index, endpoint in enumerate(endpoints)
    }


def _duplicate_response_mix(
    count: int, endpoint_mix: dict[str, int]
) -> dict[str, int]:
    """Scale a capped execution mix to the requested response count."""
    total = sum(endpoint_mix.values())
    if total == count:
        return dict(endpoint_mix)
    result = {endpoint: count * value // total for endpoint, value in endpoint_mix.items()}
    for endpoint in endpoint_mix:
        if sum(result.values()) == count:
            break
        result[endpoint] += 1
    return result


def _duplicate_fixture_body(
    endpoint: str,
    index: int,
    endpoint_count: int,
    profile_bodies: dict[tuple[str, int], bytes],
    fixture_bodies: dict[str, bytes],
    battle_fixture: bytes | None = None,
) -> tuple[str | None, bytes]:
    if endpoint == "profile":
        window = max(1, endpoint_count // 200)
        tag = _tag(index // window + 1)
        variant = (index // window) % 8
        cache_key = (tag, variant)
        if cache_key not in profile_bodies:
            profile_bodies[cache_key] = _profile_body(tag, variant)
        return tag, profile_bodies[cache_key]
    if endpoint not in fixture_bodies:
        if endpoint == "battle_log" and battle_fixture is not None:
            fixture_bodies[endpoint] = battle_fixture
        else:
            fixture_bodies[endpoint] = (
                PYTHON / "testdata" / (
                    "legend_i_battle_log_v1.json"
                    if endpoint == "battle_log"
                    else "global_top_200_v1.json"
                )
            ).read_bytes()
    return (None if endpoint == "global_player_rankings" else _tag(index + 1)), fixture_bodies[endpoint]


def _battle_fixture_for_day(day_start: datetime) -> bytes:
    """Move the committed battle fixture while preserving its payload size."""
    source_day = DAY_START.strftime("%Y-%m-%d").encode()
    target_day = day_start.astimezone(UTC).strftime("%Y-%m-%d").encode()
    shifted = BATTLE_FIXTURE.replace(source_day, target_day)
    if shifted == BATTLE_FIXTURE or len(shifted) != len(BATTLE_FIXTURE):
        raise RuntimeError("battle fixture day could not be shifted exactly")
    return shifted


def _process_jobs(
    processor: Any,
    jobs: list[int],
    prefix: str,
    *,
    serial: bool = False,
) -> list[dict[str, Any]]:
    """Process a batch, preserving reset-pair evidence ordering when needed."""

    def process(index: int, job: int) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        result = processor.process_job(
            job, owner=f"perf-{prefix}-{index}", lease_seconds=300
        )
        if result is None:
            raise RuntimeError(f"job {job} was not claimable")
        return index, {
            "job_id": job,
            "outcome": result.outcome,
            "category": result.category,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }

    if serial:
        return [process(index, job)[1] for index, job in enumerate(jobs)]
    results: list[tuple[int, dict[str, Any]]] = []
    with ThreadPoolExecutor(
        max_workers=_LANES, thread_name_prefix="clashlens-perf"
    ) as executor:
        futures = [
            executor.submit(process, index, job) for index, job in enumerate(jobs)
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return [result for _, result in sorted(results)]


_RESULT_OUTCOMES = (
    "processed",
    "processed_with_gaps",
    "retrying",
    "failed",
    "lease_lost",
    "published",
    "skipped",
    "other",
)
_RESULT_STATUSES = ("complete", "pending", "leased", "waiting_retry", "waiting_dependency", "failed", "other")
_RESULT_WORK_TYPES = (
    "process_observation",
    "replay_observation",
    "reconcile_ranked_day",
    "build_snapshot",
    "build_analytics",
    "build_army_analytics",
    "redecode_army",
    "reset_baseline",
    "live",
    "backfill",
    "other",
)


def _result_bucket(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else "other"


def _result_summary(results: list[dict[str, Any]], *, expected: int | None = None) -> dict[str, Any]:
    """Return bounded result evidence without retaining occurrence identities."""
    outcomes = dict.fromkeys(_RESULT_OUTCOMES, 0)
    statuses = dict.fromkeys(_RESULT_STATUSES, 0)
    work_types = dict.fromkeys(_RESULT_WORK_TYPES, 0)
    kinds = {"live": 0, "backfill": 0, "other": 0}
    elapsed_ms: list[float] = []
    for result in results:
        outcome = _result_bucket(result.get("outcome"), _RESULT_OUTCOMES)
        outcomes[outcome] += 1
        status = _result_bucket(result.get("status"), _RESULT_STATUSES)
        statuses[status] += 1
        work_type = _result_bucket(result.get("work_type"), _RESULT_WORK_TYPES)
        work_types[work_type] += 1
        kind = result.get("kind")
        kinds[kind if kind in {"live", "backfill"} else "other"] += 1
        elapsed = result.get("elapsed_ms", result.get("queue_latency_seconds"))
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            value = float(elapsed)
            if value >= 0 and math.isfinite(value):
                elapsed_ms.append(
                    value
                    * (
                        1000
                        if "queue_latency_seconds" in result
                        and "elapsed_ms" not in result
                        else 1
                    )
                )
    elapsed_ms.sort()
    p95 = None
    if elapsed_ms:
        p95 = elapsed_ms[(len(elapsed_ms) * 95 + 99) // 100 - 1]
    return {
        "count": len(results),
        "expected_count": expected,
        "count_matches_expected": expected is None or len(results) == expected,
        "outcomes": outcomes,
        "statuses": statuses,
        "work_types": work_types,
        "kinds": kinds,
        "retry_count": outcomes["retrying"],
        "completed_count": statuses["complete"] if any("status" in item for item in results) else outcomes["processed"],
        "failed_count": outcomes["failed"] + outcomes["lease_lost"],
        "elapsed_ms": {
            "count": len(elapsed_ms),
            "sum": sum(elapsed_ms),
            "maximum": max(elapsed_ms, default=None),
            "p95_upper": p95,
        },
    }


def _duplicate_hard_failure_codes(
    workload: Any, database: Any
) -> list[str]:
    """Map duplicate acceptance facts to the bounded artifact failure codes."""
    failures: list[str] = []
    summary = workload.get("processing_summary") if isinstance(workload, dict) else None
    if isinstance(summary, dict) and "total" in summary:
        summary = summary["total"]
    if isinstance(summary, dict):
        outcomes = summary.get("outcomes")
        statuses = summary.get("statuses")
        count = summary.get("count")
        if (
            isinstance(outcomes, dict)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and (
                outcomes.get("processed") != count
                or summary.get("count_matches_expected") is False
                or (
                    isinstance(statuses, dict)
                    and any(
                        statuses.get(status, 0) > 0
                        for status in (
                            "pending",
                            "leased",
                            "waiting_retry",
                            "waiting_dependency",
                            "failed",
                        )
                    )
                )
            )
        ):
            failures.append("fixed_acceptance_failure")
    if isinstance(database, dict) and database.get("queue_residue"):
        failures.append("queue_residue")
    return _failure_codes(failures)


def _drain(processor: Any, limit: int) -> list[dict[str, Any]]:
    results = []
    for index in range(limit):
        started = time.perf_counter()
        result = processor.process_once(owner=f"perf-drain-{index}", lease_seconds=300)
        if result is None:
            break
        results.append(
            {
                "job_id": result.job_id,
                "outcome": result.outcome,
                "category": result.category,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
            }
        )
    return results


def _store_reconciliation_population(
    connection_info: str, archive: Any, count: int
) -> list[int]:
    """Create real paired reset evidence consumed by production reconciliation."""
    from test_reconciliation_postgres import _store_baseline_pair

    jobs: list[int] = []
    for index in range(count):
        tag = _tag(index + 1)
        for label, boundary, trophies, empty in (
            ("start", DAY_START, 6000 + index, True),
            ("end", BOUNDARY, 6040 + index, False),
        ):
            pair = _store_baseline_pair(
                connection_info,
                archive,
                key=f"perf-{index}-{label}",
                boundary=boundary,
                trophies=trophies,
                empty_battle_log=empty,
                normalized_tag=tag,
            )
            jobs.extend(pair[2:])
    return jobs


_ADMISSION_MARKER = "CLASHLENS_STEP8_ADMISSION="


def _boundary_admission_probe(
    connection_info: str, phase: str, population: int
) -> dict[str, Any]:
    """Run the production collector admission seam in this disposable schema."""
    if phase not in {"admit", "handoff"} or not 1 <= population <= 12_500:
        raise ValueError("boundary admission probe input is invalid")
    environment = dict(os.environ)
    environment.update(
        {
            "CLASHLENS_STEP8_ADMISSION_DATABASE_URL": connection_info,
            "CLASHLENS_STEP8_ADMISSION_PHASE": phase,
            "CLASHLENS_STEP8_ADMISSION_BOUNDARY": BOUNDARY.isoformat().replace(
                "+00:00", "Z"
            ),
            "CLASHLENS_STEP8_ADMISSION_POPULATION": str(population),
        }
    )
    completed = subprocess.run(
        [
            "go",
            "test",
            "./internal/collector",
            "-run",
            "^TestStep8BoundaryAdmissionProbe$",
            "-count=1",
            "-timeout=600s",
            "-v",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=660,
    )
    if completed.returncode != 0:
        raise RuntimeError("production boundary admission probe failed")
    markers = [
        line.removeprefix(_ADMISSION_MARKER)
        for line in completed.stdout.splitlines()
        if line.startswith(_ADMISSION_MARKER)
    ]
    if len(markers) != 1 or len(markers[0]) > 4096:
        raise RuntimeError("production boundary admission evidence is unavailable")
    try:
        evidence = json.loads(markers[0])
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "production boundary admission evidence is invalid"
        ) from error
    _validate_boundary_admission_evidence(evidence, phase, population)
    return evidence


def _validate_boundary_admission_evidence(
    evidence: Any, phase: str, population: int
) -> None:
    if not isinstance(evidence, dict):
        raise TypeError("boundary admission evidence must be an object")
    if phase == "admit":
        expected = {
            "phase": "admit",
            "blocked_before_regular_drain": True,
            "state_before_drain": "regular_draining",
            "regular_nonterminal_before": 2,
            "state_after_admission": "reset_draining",
            "regular_drain_complete": True,
            "reset_drain_complete": False,
            "safe_handoff": False,
            "reset_generation": 1,
            "regular_nonterminal_after": 0,
            "reset_nonterminal_after": population,
            "membership_count": population,
            "reset_root_count": population,
            "regular_allowed_during_reset": False,
            "regular_scheduled_during_reset": 0,
        }
    elif phase == "handoff":
        expected = {
            "phase": "handoff",
            "state": "safe_handoff",
            "regular_drain_complete": True,
            "reset_drain_complete": True,
            "safe_handoff": True,
            "reset_generation": 1,
            "handoff_recorded": True,
            "regular_nonterminal_count": 0,
            "reset_nonterminal_count": 0,
            "membership_count": population,
            "completed_reset_root_count": population,
            "regular_allowed_after_handoff": True,
            "regular_scheduled_after_handoff": 1,
        }
    else:
        raise ValueError("boundary admission phase is invalid")
    if evidence != expected:
        raise ValueError("boundary admission evidence contradicts the workload")


def _store_production_reset_population(
    connection_info: str, archive: Any, count: int
) -> tuple[list[int], dict[str, Any]]:
    """Attach committed observations to production-admitted reset roots."""
    import psycopg
    from test_reconciliation_postgres import _store_baseline_pair

    jobs: list[int] = []
    for index in range(count):
        pair = _store_baseline_pair(
            connection_info,
            archive,
            key=f"perf-{index}-start",
            boundary=DAY_START,
            trophies=6000 + index,
            empty_battle_log=True,
            normalized_tag=_tag(index + 1),
        )
        jobs.extend(pair[2:])
    with psycopg.connect(connection_info) as connection:
        active_count = connection.execute(
            """UPDATE players SET active = true, eligibility_state = 'eligible',
                                      next_due_at = NULL
               WHERE normalized_tag LIKE '#P%'
               RETURNING id"""
        ).fetchall()
        if len(active_count) != count:
            raise RuntimeError("reset fixture population is incomplete")
        connection.execute(
            """UPDATE players SET next_due_at = %s
               WHERE id = (SELECT min(id) FROM players WHERE active)""",
            (BOUNDARY,),
        )
        connection.commit()
    admitted = _boundary_admission_probe(connection_info, "admit", count)
    for index in range(count):
        pair = _store_baseline_pair(
            connection_info,
            archive,
            key=f"perf-{index}-end",
            boundary=BOUNDARY,
            trophies=6040 + index,
            empty_battle_log=False,
            normalized_tag=_tag(index + 1),
            production_admission=True,
        )
        jobs.extend(pair[2:])
    return jobs, {
        "admit": admitted,
        "handoff": _boundary_admission_probe(connection_info, "handoff", count),
    }


def _store_reconciliation_corrections(
    connection_info: str, archive: Any, count: int
) -> list[int]:
    from test_reconciliation_postgres import _store_baseline_pair

    jobs: list[int] = []
    for index in range(count):
        pair = _store_baseline_pair(
            connection_info,
            archive,
            key=f"perf-{index}-correction",
            boundary=BOUNDARY,
            trophies=6039 + index,
            empty_battle_log=False,
            observed_at=BOUNDARY + timedelta(seconds=1),
            normalized_tag=_tag(index + 1),
        )
        jobs.extend(pair[2:])
    return jobs


def _relation_snapshot(connection_info: str) -> dict[str, dict[str, Any]]:
    """Capture row, DML, and table/index/TOAST metrics for this schema."""
    import psycopg
    from psycopg import sql

    def timestamp(value: Any) -> str | None:
        return None if value is None else value.isoformat()

    with psycopg.connect(connection_info) as connection:
        rows = connection.execute(
            """
            SELECT c.relname, c.oid, c.reltoastrelid,
                   s.n_live_tup, s.n_dead_tup,
                   s.n_tup_ins, s.n_tup_upd, s.n_tup_del,
                   s.last_vacuum, s.last_autovacuum,
                   s.last_analyze, s.last_autoanalyze,
                   s.n_mod_since_analyze,
                   pg_table_size(c.oid), pg_indexes_size(c.oid),
                   CASE WHEN c.reltoastrelid = 0 THEN 0
                        ELSE pg_total_relation_size(c.reltoastrelid) END,
                   pg_total_relation_size(c.oid)
            FROM pg_class AS c
            LEFT JOIN pg_stat_all_tables AS s ON s.relid = c.oid
            WHERE c.relnamespace = current_schema()::regnamespace
              AND c.relkind IN ('r', 'm')
            ORDER BY c.relname
            """
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = _text(row[0])
            toast_bytes = int(row[15])
            result[name] = {
                "row_count": int(
                    connection.execute(
                        sql.SQL("SELECT count(*) FROM {}")
                        .format(sql.Identifier(name))
                    ).fetchone()[0]
                ),
                "live_tuples": int(row[3] or 0),
                "dead_tuples": int(row[4] or 0),
                "rows_inserted_total": int(row[5] or 0),
                "rows_updated_total": int(row[6] or 0),
                "rows_deleted_total": int(row[7] or 0),
                "last_vacuum": timestamp(row[8]),
                "last_autovacuum": timestamp(row[9]),
                "last_analyze": timestamp(row[10]),
                "last_autoanalyze": timestamp(row[11]),
                "modifications_since_analyze": int(row[12] or 0),
                "table_bytes": int(row[13]) - toast_bytes,
                "index_bytes": int(row[14]),
                "toast_bytes": toast_bytes,
                "total_bytes": int(row[16]),
            }
    return result


def _db_snapshot(
    connection_info: str,
    wal_start: str,
    statement_start: int | None,
    relation_start: dict[str, dict[str, Any]] | None = None,
    wal_retained_start: int | None = None,
) -> dict[str, Any]:
    import psycopg

    relation_stats = _relation_snapshot(connection_info)
    now = datetime.now(tz=UTC)
    for name, current in relation_stats.items():
        previous = (relation_start or {}).get(name)
        dml: dict[str, int | None] = {}
        for label in ("inserted", "updated", "deleted"):
            total = current[f"rows_{label}_total"]
            old_total = None if previous is None else previous[f"rows_{label}_total"]
            dml[label] = None if old_total is None else max(0, total - old_total)
        current["rows_inserted"] = dml["inserted"]
        current["rows_updated"] = dml["updated"]
        current["rows_deleted"] = dml["deleted"]
        current["dml"] = dml
        current["autovacuum_lag_seconds"] = (
            None
            if current["last_autovacuum"] is None
            else max(0.0, (now - datetime.fromisoformat(current["last_autovacuum"])).total_seconds())
        )
        current["autoanalyze_lag_seconds"] = (
            None
            if current["last_autoanalyze"] is None
            else max(0.0, (now - datetime.fromisoformat(current["last_autoanalyze"])).total_seconds())
        )
        current["autovacuum_due"] = current["dead_tuples"] > max(
            50, int(current["live_tuples"] * 0.2)
        )
        current["autoanalyze_due"] = current["modifications_since_analyze"] > max(
            50, int(current["live_tuples"] * 0.1)
        )

    with psycopg.connect(connection_info) as connection:
        wal = connection.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s::pg_lsn)::bigint",
            (wal_start,),
        ).fetchone()[0]
        retained_wal = int(
            connection.execute(
                "SELECT COALESCE(sum(size), 0)::bigint FROM pg_ls_waldir()"
            ).fetchone()[0]
        )
        retained_wal_growth = (
            None
            if wal_retained_start is None
            else max(0, retained_wal - wal_retained_start)
        )
        queues = {}
        queue_residue = []
        for table in ("collector_jobs", "python_processing_jobs"):
            rows = connection.execute(
                f"""SELECT status, work_type, count(*),
                           CASE WHEN status IN ('pending','leased','waiting_retry','waiting_dependency')
                                THEN extract(epoch FROM clock_timestamp() - min(created_at))
                           END
                    FROM {table} GROUP BY status, work_type ORDER BY status, work_type"""
            ).fetchall()
            queues[table] = [
                {
                    "status": _text(row[0]),
                    "work_type": _text(row[1]),
                    "count": int(row[2]),
                    "oldest_active_age_seconds": None
                    if row[3] is None
                    else float(row[3]),
                }
                for row in rows
            ]
            queue_residue.extend(
                {
                    "queue": table,
                    **item,
                }
                for item in queues[table]
                if item["status"]
                in {"pending", "leased", "waiting_retry", "waiting_dependency"}
            )
        endpoint_counts = dict.fromkeys(DUPLICATE_ENDPOINT_MIX, 0)
        endpoint_counts.update(
            {
                _text(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT endpoint, count(*) FROM collector_observations GROUP BY endpoint ORDER BY endpoint"
                ).fetchall()
            }
        )
        pending_remote = 0
        try:
            pending_remote = int(
                connection.execute(
                    "SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'"
                ).fetchone()[0]
            )
        except psycopg.Error:
            connection.rollback()
        statement_calls = None
        try:
            current = int(
                connection.execute(
                    "SELECT COALESCE(sum(calls),0)::bigint FROM public.pg_stat_statements"
                ).fetchone()[0]
            )
            statement_calls = (
                current - statement_start if statement_start is not None else None
            )
        except psycopg.Error:
            connection.rollback()
    queue_age = {
        table: (
            max(
                (
                    item["oldest_active_age_seconds"]
                    for item in rows
                    if item["oldest_active_age_seconds"] is not None
                ),
                default=None,
            )
        )
        for table, rows in queues.items()
    }
    return {
        "wal_bytes": int(wal),
        "wal_retained_bytes": retained_wal,
        "wal_retained_growth_bytes": retained_wal_growth,
        "sql_statement_calls": statement_calls,
        "pending_remote_verification": pending_remote,
        "response_counts_by_endpoint": endpoint_counts,
        "occurrence_counts_by_endpoint": dict(endpoint_counts),
        "relations": {
            name: int(values["total_bytes"])
            for name, values in relation_stats.items()
        },
        "relation_sizes": {
            name: {
                key: int(values[key])
                for key in ("table_bytes", "index_bytes", "toast_bytes", "total_bytes")
            }
            for name, values in relation_stats.items()
        },
        "relation_stats": relation_stats,
        "affected_relations": [
            name for name in AFFECTED_RELATIONS if name in relation_stats
        ],
        "queues": queues,
        "queue_age_seconds": queue_age,
        "queue_residue": queue_residue,
    }


def _start_metrics(connection_info: str) -> tuple[str, int | None, int]:
    import psycopg

    with psycopg.connect(connection_info) as connection:
        wal = _text(
            connection.execute("SELECT pg_current_wal_insert_lsn()::text").fetchone()[0]
        )
        retained_wal = int(
            connection.execute(
                "SELECT COALESCE(sum(size), 0)::bigint FROM pg_ls_waldir()"
            ).fetchone()[0]
        )
        try:
            calls = int(
                connection.execute(
                    "SELECT COALESCE(sum(calls),0)::bigint FROM public.pg_stat_statements"
                ).fetchone()[0]
            )
        except psycopg.Error:
            connection.rollback()
            calls = None
    return wal, calls, retained_wal


def _plan_counts(node: dict[str, Any]) -> tuple[int, int]:
    children = node.get("Plans", [])
    if not children:
        loops = int(node.get("Actual Loops", 1))
        scanned = int(node.get("Actual Rows", 0)) * loops
        scanned += int(node.get("Rows Removed by Filter", 0)) * loops
        return scanned, int(node.get("Actual Rows", 0))
    scanned = sum(_plan_counts(child)[0] for child in children)
    return scanned, int(node.get("Actual Rows", 0))


def _p95(values: list[float]) -> float:
    if not values:
        raise ValueError("p95 requires at least one measurement")
    return sorted(values)[(len(values) * 95 + 99) // 100 - 1]


def _army_selection_specs() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "selection": population,
            "lens": lens,
            "expected_facts": (
                STEP5_SELECTED_MEMBERS * STEP5_DAYS * STEP5_FACTS_PER_MEMBER_DAY
                if population != "trophies-5000-9999"
                else STEP5_DAYS
                * STEP5_POPULATION
                * STEP5_FACTS_PER_MEMBER_DAY
                * (STEP5_MISSING_TROPHY_RATE - 1)
                // STEP5_MISSING_TROPHY_RATE
            ),
        }
        for population in ("top-1000", "trophies-5000-9999", "streak-top-1000")
        for lens in ("offense", "defense")
    )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _artifact_digest(artifact: dict[str, Any]) -> str:
    canonical = {
        key: value for key, value in artifact.items() if key != "artifact_digest"
    }
    return _sha(
        json.dumps(
            _json_value(canonical), sort_keys=True, separators=(",", ":")
        ).encode()
    )


_RETAINED_JOB_DETAIL_KEYS = frozenset(
    {
        "account_id",
        "archive_reference",
        "database_url",
        "error",
        "exception",
        "job_id",
        "job_ids",
        "normalized_tag",
        "player_tag",
        "results",
        "request_id",
        "raw_body",
        "raw_config",
        "stderr",
        "stdout",
        "profile_results",
        "dependent_results",
        "correction_results",
    }
)

_ARTIFACT_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "started_at",
        "finished_at",
        "provenance",
        "execution",
        "prepared_candidate_images",
        "candidate_receipt",
        "official_api_requests",
        "collector_probe",
        "samples",
        "army_read_sample",
        "hard_failures",
        "artifact_digest",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "source_sha",
        "source_dirty",
        "runner_sha256",
        "migrations",
        "applied_migration_versions",
        "configuration_fingerprint",
        "configuration",
        "host",
        "execution",
        "prepared_candidate_images",
        "candidate_receipt",
        "postgres",
    }
)
_STANDARD_SAMPLE_KEYS = frozenset(
    {
        "workload",
        "database",
        "archive_operations",
        "storage_runway",
        "evidence",
        "spool",
        "elapsed_seconds",
        "cpu_seconds",
        "peak_rss_kib",
    }
)
_COORDINATOR_SAMPLE_KEYS = frozenset(
    {
        "workload",
        "database",
        "archive_operations",
        "storage_runway",
        "evidence",
        "queue_residue",
    }
)
_RESET_WORKLOAD_KEYS = frozenset(
    {
        "status",
        "hard_failures",
        "population",
        "official_responses",
        "processing_summary",
        "fact_counts",
        "fanout_evidence",
        "boundary_admission",
        "queue_residue",
        "fixture_discoveries_prequalified",
        "stage_metrics",
        "spool",
        "evidence_counters",
        "army_endpoint",
        "correction_evidence",
    }
)
_DUPLICATE_WORKLOAD_KEYS = frozenset(
    {
        "observations",
        "official_responses",
        "executed_observations",
        "measured_cycles",
        "cycle_elapsed_seconds",
        "median_cycle_seconds",
        "daily_288_cycle_projection_seconds",
        "aggregation_factor",
        "aggregation_method",
        "endpoint_mix",
        "response_counts_by_endpoint",
        "occurrence_counts_by_endpoint",
        "fixture_bytes_by_endpoint",
        "exact_bytes",
        "official_api_traffic",
        "canonical_content",
        "contract",
        "fixture_discoveries_prequalified",
        "processing_summary",
        "stage_metrics",
        "spool",
        "evidence_counters",
        "collector_archive_operations",
    }
)
_MIXED_WORKLOAD_KEYS = frozenset(
    {
        "completion_order",
        "completion_order_complete",
        "completion_counts",
        "live_jobs",
        "backfill_jobs",
        "configured_lanes",
        "effective_lanes",
        "official_responses",
        "official_api_traffic",
        "fixture_discoveries_prequalified",
        "live_first_completion_index",
        "live_queue_latency_seconds",
        "oldest_active_queue_age_seconds",
        "elapsed_seconds",
        "cpu_seconds",
        "peak_rss_kib",
        "memory_pressure_before",
        "memory_pressure_after",
        "memory_pressure_delta",
        "live_latency_contract",
        "five_minute_contract",
        "hard_failures",
        "processing_summary",
        "database",
        "stage_metrics",
        "spool",
        "evidence_counters",
    }
)
_COORDINATOR_WORKLOAD_KEYS = frozenset(
    {
        "population",
        "official_responses",
        "coordinator_job_counts",
        "manifest_publication",
        "contract",
        "coverage",
        "publication_identities",
        "generation",
        "coordinator_links",
        "coordinator_residue",
        "snapshot_headers",
        "snapshot_entries",
        "full_large_reset",
        "statement_ceiling",
        "queue_residue",
        "coordinator_processing",
    }
)

_POSTGRES_SETTING_NAMES = frozenset(
    {
        "server_version",
        "server_version_num",
        "shared_buffers",
        "work_mem",
        "maintenance_work_mem",
        "max_connections",
        "track_io_timing",
    }
)
_ARMY_PLAN_KEYS = frozenset(
    {
        "correlation",
        "sql",
        "parameters",
        "rows_scanned",
        "rows_returned",
        "explain_analyze_buffers",
    }
)
_ARMY_PARAMETER_KEYS = frozenset({"arity", "types"})
_ARMY_RELATION_SUBSETS = {
    "duplicate-heavy": frozenset(
        {
            "collector_observations",
            "parsed_source_payloads",
            "archive_catalogue",
            "python_processing_jobs",
        }
    ),
    "reset-boundary": frozenset(
        {
            "ranked_day_versions",
            "leaderboard_snapshots",
            "leaderboard_snapshot_entries",
            "analytics_summaries",
            "army_analytics_battle_facts",
        }
    ),
    "correction": frozenset(
        {
            "ranked_day_versions",
            "leaderboard_snapshots",
            "leaderboard_snapshot_entries",
            "analytics_summaries",
            "army_analytics_battle_facts",
        }
    ),
    "mixed-backfill": frozenset(
        {"collector_observations", "python_processing_jobs"}
    ),
    "army-analytics": frozenset(
        {"army_analytics_battle_facts", "leaderboard_snapshot_entries"}
    ),
}
_ARMY_QUERY_SHAPES: dict[tuple[str, str], tuple[str, ...]] = {
    ("top-1000", "army_analytics.completed_day_logs"): ("str", "int", "int"),
    ("top-1000", "army_analytics.completed_days"): ("str", "int", "int"),
    ("top-1000", "army_analytics.published_snapshots"): ("array",),
    ("top-1000", "army_analytics.rank_members"): ("int", "int", "int"),
    ("top-1000", "army_analytics.cohort_quality"): ("int", "int", "int"),
    ("top-1000", "army_analytics.troop_state_aggregates"): (
        "str",
        "int",
        "int",
        "str",
        "array",
    ),
    ("top-1000", "army_analytics.troop_component_aggregates"): (
        "str",
        "int",
        "int",
        "str",
        "array",
    ),
    ("top-1000", "army_analytics.selected_source_hash"): (
        "str",
        "int",
        "int",
        "str",
        "array",
    ),
    ("trophies-5000-9999", "army_analytics.completed_day_logs"): (
        "str",
        "int",
        "int",
    ),
    ("trophies-5000-9999", "army_analytics.completed_days"): (
        "str",
        "int",
        "int",
    ),
    ("trophies-5000-9999", "army_analytics.missing_trophies"): (
        "str",
        "int",
        "int",
        "str",
    ),
    ("trophies-5000-9999", "army_analytics.troop_state_aggregates"): (
        "str",
        "int",
        "int",
        "str",
        "int",
        "int",
    ),
    ("trophies-5000-9999", "army_analytics.troop_component_aggregates"): (
        "str",
        "int",
        "int",
        "str",
        "int",
        "int",
    ),
    ("trophies-5000-9999", "army_analytics.selected_source_hash"): (
        "str",
        "int",
        "int",
        "str",
        "int",
        "int",
    ),
    ("streak-top-1000", "army_analytics.completed_day_logs"): (
        "str",
        "int",
        "int",
    ),
    ("streak-top-1000", "army_analytics.completed_days"): (
        "str",
        "int",
        "int",
    ),
    ("streak-top-1000", "army_analytics.published_snapshots"): ("array",),
    ("streak-top-1000", "army_analytics.streak_members"): (
        "array",
        "int",
        "int",
    ),
    ("streak-top-1000", "army_analytics.streak_candidates"): ("array", "int"),
    ("streak-top-1000", "army_analytics.streak_shield_state"): (
        "array",
        "array",
    ),
    ("streak-top-1000", "army_analytics.cohort_quality"): (
        "array",
        "int",
        "int",
    ),
    ("streak-top-1000", "army_analytics.troop_state_aggregates"): (
        "str",
        "int",
        "int",
        "str",
        "array",
    ),
    ("streak-top-1000", "army_analytics.troop_component_aggregates"): (
        "str",
        "int",
        "int",
        "str",
        "array",
    ),
    ("streak-top-1000", "army_analytics.selected_source_hash"): (
        "str",
        "int",
        "int",
        "str",
        "array",
    ),
}
_ARMY_QUERY_ORDER = {
    population: tuple(
        identity
        for (selection, identity), _shape in _ARMY_QUERY_SHAPES.items()
        if selection == population
    )
    for population in ("top-1000", "trophies-5000-9999", "streak-top-1000")
}
_ARMY_OVERLAPPED_QUERY_IDENTITIES = frozenset(
    {
        "army_analytics.troop_component_aggregates",
        "army_analytics.selected_source_hash",
    }
)
_DUPLICATE_LATENCY_STAGES = {
    "python_read": "python_archive_get_verify",
    "local_verify": "python_archive_local_verify",
    "transaction": "python_domain_profile",
    "repair": "python_archive_repair",
}
_DUPLICATE_LATENCY_KEYS = frozenset(
    set(_DUPLICATE_LATENCY_STAGES)
    | {
        "collector_hashing_us",
        "collector_operation_total_us",
        "collector_remote_put_us",
        "collector_get_verify_us",
        "collector_local_verify_us",
    }
)
_ARMY_MEMORY_KEYS = frozenset(
    {
        "host_swap_used_bytes",
        "process_cgroup_available",
        "process_swap_used_bytes",
        "process_oom",
        "process_oom_kill",
        "database_cgroup_available",
        "database_swap_used_bytes",
        "database_oom",
        "database_oom_kill",
    }
)
_ARMY_MEMORY_DELTA_KEYS = frozenset(_ARMY_MEMORY_KEYS - {"host_swap_used_bytes", "process_cgroup_available", "database_cgroup_available"})


def _army_forced_miss_failures(
    seconds: float,
    before: dict[str, int],
    after: dict[str, int],
    delta: dict[str, int],
) -> list[str]:
    """Derive the bounded forced-miss failure codes from its three gates."""
    failures: list[str] = []
    if seconds >= STEP5_FORCED_MISS_TARGET_SECONDS:
        failures.append("step5_forced_miss_exceeded")
    if any(
        pressure["process_cgroup_available"] != 1
        or pressure["database_cgroup_available"] != 1
        for pressure in (before, after)
    ):
        failures.append("step5_cgroup_unavailable")
    if any(delta.values()):
        failures.append("step5_memory_pressure_increased")
    return failures


def _army_forced_miss_passed(
    seconds: float,
    before: dict[str, int],
    after: dict[str, int],
    delta: dict[str, int],
) -> bool:
    return not _army_forced_miss_failures(seconds, before, after, delta)


def _army_completed_read_failures(selections: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for selection in selections:
        failures.extend(
            _army_forced_miss_failures(
                selection["forced_miss_seconds"],
                selection["forced_miss_memory_before"],
                selection["forced_miss_memory_after"],
                selection["forced_miss_memory_delta"],
            )
        )
        if not selection["target_passed"]:
            failures.append("step5_p95_exceeded")
    return failures


def _bounded_int(value: Any, label: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} is invalid")
    if positive and value == 0:
        raise ValueError(f"{label} is invalid")
    return value


def _bounded_number(value: Any, label: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} is invalid")
    return float(value)


def _bounded_text(value: Any, label: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{label} is invalid")
    return value


def _validate_postgres_identity(value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "settings",
        "applied_migration_versions",
    }:
        raise ValueError(f"{label} schema is invalid")
    if not _bounded_text(value["version"], f"{label}.version").startswith("PostgreSQL "):
        raise ValueError(f"{label}.version is invalid")
    settings = value["settings"]
    if not isinstance(settings, dict) or set(settings) != _POSTGRES_SETTING_NAMES:
        raise ValueError(f"{label}.settings are invalid")
    for name, setting in settings.items():
        _bounded_text(name, f"{label}.setting name")
        _bounded_text(setting, f"{label}.{name}")
    if value["applied_migration_versions"] != list(REQUIRED_MIGRATION_VERSIONS):
        raise ValueError(f"{label}.applied_migration_versions is invalid")


def _army_parameter_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("army query parameter is non-finite")
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, datetime):
        return "timestamp"
    if isinstance(value, (list, tuple)):
        return "array"
    raise ValueError("army query parameter has an unsupported type")


def _army_parameter_shape(parameters: Any) -> tuple[str, ...]:
    if parameters is None:
        values: list[Any] = []
    elif isinstance(parameters, (list, tuple)):
        values = list(parameters)
    else:
        values = [parameters]
    if len(values) > 16:
        raise ValueError("army query has too many parameters")
    return tuple(_army_parameter_type(value) for value in values)


def _army_query_identity(sql: Any) -> str:
    if not isinstance(sql, str):
        raise TypeError("army query is not text")
    normalized = re.sub(r"\s+", " ", sql.strip()).upper()
    if "FROM API_PLAYER_DAILY_LOGS" in normalized:
        return "army_analytics.completed_day_logs"
    if "FROM ARMY_ANALYTICS_COMPLETED_DAYS" in normalized:
        return "army_analytics.completed_days"
    if "FROM LEADERBOARD_SNAPSHOTS" in normalized and "DISTINCT ON" in normalized:
        return "army_analytics.published_snapshots"
    if "FROM LEADERBOARD_SNAPSHOT_ENTRIES" in normalized and "NOT (FRESHNESS" in normalized:
        return "army_analytics.cohort_quality"
    if "GROUP BY PLAYER_ID HAVING COUNT(DISTINCT SNAPSHOT_ID)" in normalized:
        return "army_analytics.streak_members"
    if "SELECT DISTINCT PLAYER_ID" in normalized and "FROM LEADERBOARD_SNAPSHOT_ENTRIES" in normalized:
        return "army_analytics.streak_candidates"
    if "FROM UNNEST" in normalized and "RANKED_DAY_VERSIONS" in normalized:
        return "army_analytics.streak_shield_state"
    if "FROM LEADERBOARD_SNAPSHOT_ENTRIES" in normalized and "POSITION BETWEEN" in normalized:
        return "army_analytics.rank_members"
    if "BATTLE_TIME_TROPHIES IS NULL" in normalized:
        return "army_analytics.missing_trophies"
    if "STRING_AGG(" in normalized and "INPUT_HASH" in normalized:
        return "army_analytics.selected_source_hash"
    if "GROUP BY ARMY_STATE" in normalized:
        return "army_analytics.troop_state_aggregates"
    if "CROSS JOIN LATERAL" in normalized:
        return "army_analytics.troop_component_aggregates"
    raise ValueError("army query is outside the fixed production protocol")


def _public_explain_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValueError("army EXPLAIN payload is invalid")
    envelope = payload[0]
    if not set(envelope).issubset(
        {
            "Plan",
            "Planning",
            "Planning Time",
            "Triggers",
            "JIT",
            "Execution Time",
        }
    ) or not {"Plan", "Planning Time", "Execution Time"}.issubset(envelope):
        raise ValueError("army EXPLAIN payload retains non-public fields")
    if "Triggers" in envelope and envelope["Triggers"] not in ([], None):
        raise ValueError("army EXPLAIN triggers are not public facts")

    discarded_items = 0

    def discard_fact(value: Any, label: str, depth: int = 0) -> None:
        """Bound raw EXPLAIN details while keeping them out of the artifact."""
        nonlocal discarded_items
        if depth > MAX_EXPLAIN_DETAIL_DEPTH:
            raise ValueError("army EXPLAIN detail is too deep")
        discarded_items += 1
        if discarded_items > MAX_EXPLAIN_DETAIL_ITEMS:
            raise ValueError("army EXPLAIN detail is unbounded")
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{label} is invalid")
            return
        if isinstance(value, str):
            if len(value) > MAX_EXPLAIN_DETAIL_TEXT:
                raise ValueError(f"{label} is invalid")
            return
        if isinstance(value, list):
            if len(value) > MAX_EXPLAIN_DETAIL_SEQUENCE:
                raise ValueError("army EXPLAIN detail is unbounded")
            for index, child in enumerate(value):
                discard_fact(child, f"{label}[{index}]", depth + 1)
            return
        if isinstance(value, dict):
            if len(value) > MAX_EXPLAIN_DETAIL_MAPPING:
                raise ValueError("army EXPLAIN detail is unbounded")
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > MAX_EXPLAIN_DETAIL_KEY:
                    raise ValueError(f"{label} key is invalid")
                discard_fact(child, f"{label}.{key}", depth + 1)
            return
        raise ValueError("army EXPLAIN detail is invalid")

    def plan_node(value: Any, depth: int = 0) -> dict[str, Any]:
        if not isinstance(value, dict) or depth > 32:
            raise ValueError("army EXPLAIN plan is invalid")
        allowed = {"Node Type", "Actual Rows", "Actual Loops", "Rows Removed by Filter", "Plans"}
        if not {"Node Type", "Actual Rows", "Actual Loops"}.issubset(value):
            raise ValueError("army EXPLAIN plan retains non-public fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > MAX_EXPLAIN_DETAIL_KEY:
                raise ValueError("army EXPLAIN plan key is invalid")
            if key not in allowed:
                discard_fact(child, f"army EXPLAIN {key}")
        node = {
            "Node Type": _bounded_text(value["Node Type"], "army EXPLAIN node", limit=256),
            "Actual Rows": _bounded_number(value["Actual Rows"], "army EXPLAIN rows"),
            "Actual Loops": _bounded_number(value["Actual Loops"], "army EXPLAIN loops"),
        }
        if "Rows Removed by Filter" in value:
            node["Rows Removed by Filter"] = _bounded_number(
                value["Rows Removed by Filter"], "army EXPLAIN filtered rows"
            )
        if "Plans" in value:
            children = value["Plans"]
            if not isinstance(children, list) or len(children) > MAX_RETAINED_SEQUENCE:
                raise ValueError("army EXPLAIN plan is unbounded")
            node["Plans"] = [plan_node(child, depth + 1) for child in children]
        return node

    if "Planning" in envelope:
        discard_fact(envelope["Planning"], "army EXPLAIN planning")
    if "JIT" in envelope:
        discard_fact(envelope["JIT"], "army EXPLAIN JIT")

    return [
        {
            "Plan": plan_node(envelope["Plan"]),
            "Planning Time": _bounded_number(envelope["Planning Time"], "army planning time"),
            "Execution Time": _bounded_number(envelope["Execution Time"], "army execution time"),
        }
    ]


def _validate_relation_subset(database: Any, mode: str, label: str) -> None:
    required = _ARMY_RELATION_SUBSETS[mode]
    if not isinstance(database, dict):
        raise TypeError(f"{label} database is invalid")
    maps = [database.get(name) for name in ("relations", "relation_sizes", "relation_stats")]
    affected = database.get("affected_relations")
    if any(not isinstance(value, dict) or not required.issubset(value) for value in maps) or not isinstance(affected, list) or not required.issubset(affected):
        raise ValueError(f"{label} relation evidence is incomplete")


def _validate_published_latency(workload: Any, evidence: Any, label: str) -> None:
    stages = workload.get("stage_metrics") if isinstance(workload, dict) else None
    latency = evidence.get("latency_ms") if isinstance(evidence, dict) else None
    if not isinstance(stages, dict) or not isinstance(latency, dict):
        raise TypeError(f"{label} latency evidence is invalid")
    if not set(latency).issubset(_DUPLICATE_LATENCY_KEYS):
        raise ValueError(f"{label} latency evidence has an unknown stage")
    for name, stage_name in _DUPLICATE_LATENCY_STAGES.items():
        metric = stages.get(stage_name)
        published = latency.get(name)
        if metric is None:
            if published is not None:
                raise ValueError(f"{label}.{name} has no source stage")
            continue
        if not isinstance(metric, dict) or "average_ms" not in metric:
            raise ValueError(f"{label}.{stage_name} is invalid")
        average = _bounded_number(metric["average_ms"], f"{label}.{stage_name}.average_ms")
        if published is None or not math.isclose(float(published), average or 0.0, rel_tol=1e-12):
            raise ValueError(f"{label}.{name} contradicts {stage_name}")
    for name, value in latency.items():
        _bounded_number(value, f"{label}.latency_ms.{name}", allow_none=True)


def _validate_duplicate_protocol(
    workload: Any,
    config: dict[str, Any],
    label: str,
    *,
    overlap_cycle: bool = False,
    fixed_acceptance_failure: bool = False,
) -> None:
    if not isinstance(workload, dict):
        raise TypeError(f"{label} workload is invalid")
    observations = _bounded_int(config["duplicate_observations"], f"{label}.observations", positive=True)
    cycles = _bounded_int(config["duplicate_cycles"], f"{label}.cycles", positive=True)
    if observations < 2 or observations > DUPLICATE_EXECUTION_CAP or cycles > 4:
        raise ValueError(f"{label} duplicate protocol is invalid")
    expected_total = observations * cycles
    if workload.get("observations") != observations:
        raise ValueError(f"{label}.observations contradict configuration")
    if workload.get("official_responses") != expected_total or workload.get("executed_observations") != expected_total:
        raise ValueError(f"{label} response counts contradict configuration")
    if overlap_cycle:
        _bounded_number(workload.get("cycle_elapsed_seconds"), f"{label}.cycle_elapsed_seconds")
    else:
        elapsed = workload.get("cycle_elapsed_seconds")
        if not isinstance(elapsed, list) or len(elapsed) != cycles:
            raise ValueError(f"{label}.cycle_elapsed_seconds is invalid")
        for value in elapsed:
            _bounded_number(value, f"{label}.cycle_elapsed_seconds")
    for key in ("median_cycle_seconds", "daily_288_cycle_projection_seconds", "aggregation_factor"):
        _bounded_number(workload.get(key), f"{label}.{key}")
    expected_mix = _duplicate_response_mix(observations, _duplicate_endpoint_mix(observations))
    expected_responses = {key: value * cycles for key, value in expected_mix.items()}
    expected_occurrences = {
        key: value * cycles for key, value in _duplicate_endpoint_mix(observations).items()
    }
    if workload.get("endpoint_mix") != expected_responses or workload.get("response_counts_by_endpoint") != expected_responses:
        raise ValueError(f"{label} response endpoint counts are invalid")
    if workload.get("occurrence_counts_by_endpoint") != expected_occurrences:
        raise ValueError(f"{label} occurrence endpoint counts are invalid")
    contract = workload.get("contract")
    if not isinstance(contract, dict) or set(contract) != {
        "expected_occurrences",
        "executed_occurrences",
        "matches_expected",
        "endpoint_mix",
    }:
        raise ValueError(f"{label} contract is invalid")
    if contract != {
        "expected_occurrences": DUPLICATE_EXECUTION_CAP,
        "executed_occurrences": observations,
        "matches_expected": observations == DUPLICATE_EXECUTION_CAP,
        "endpoint_mix": dict(DUPLICATE_ENDPOINT_MIX),
    }:
        raise ValueError(f"{label} contract is invalid")
    fixture_bytes = workload.get("fixture_bytes_by_endpoint")
    if not isinstance(fixture_bytes, dict) or set(fixture_bytes) != set(DUPLICATE_ENDPOINT_MIX):
        raise ValueError(f"{label} fixture bytes are invalid")
    exact_bytes = sum(
        expected_occurrences[key] * _bounded_int(fixture_bytes[key], f"{label}.{key}", positive=True)
        for key in DUPLICATE_ENDPOINT_MIX
    )
    if workload.get("exact_bytes") != exact_bytes:
        raise ValueError(f"{label}.exact_bytes contradicts endpoint occurrences")
    traffic = workload.get("official_api_traffic")
    if traffic != {"requests": 0, "source": "committed fixtures"}:
        raise ValueError(f"{label} official API traffic is invalid")
    canonical = workload.get("canonical_content")
    if not isinstance(canonical, dict) or set(canonical) != {
        "parsed_payloads_by_endpoint",
        "profile_semantic_versions",
        "profile_occurrence_effects",
        "battle_canonical_rows",
        "battle_occurrence_rows",
        "ranking_canonical_rows",
        "ranking_occurrence_links",
    }:
        raise ValueError(f"{label} canonical content is invalid")
    parsed = canonical["parsed_payloads_by_endpoint"]
    if not isinstance(parsed, dict) or any(
        key not in DUPLICATE_ENDPOINT_MIX or _bounded_int(value, f"{label}.parsed.{key}") < 0
        for key, value in parsed.items()
    ):
        raise ValueError(f"{label} parsed payload counts are invalid")
    for key in (
        "profile_semantic_versions",
        "profile_occurrence_effects",
        "battle_canonical_rows",
        "battle_occurrence_rows",
        "ranking_canonical_rows",
        "ranking_occurrence_links",
    ):
        _bounded_int(canonical[key], f"{label}.canonical.{key}")
    if (
        canonical["profile_semantic_versions"] != parsed.get("profile", 0)
        or canonical["profile_occurrence_effects"] > expected_occurrences["profile"]
        or (
            not fixed_acceptance_failure
            and canonical["profile_occurrence_effects"]
            != expected_occurrences["profile"]
        )
    ):
        raise ValueError(f"{label} profile canonical counts are invalid")
    if canonical["battle_canonical_rows"] > canonical["battle_occurrence_rows"] or canonical["ranking_canonical_rows"] > canonical["ranking_occurrence_links"]:
        raise ValueError(f"{label} canonical counts are invalid")
    summary = workload.get("processing_summary")
    if (
        not isinstance(summary, dict)
        or summary.get("expected_count") != expected_total
        or not isinstance(summary.get("count"), int)
        or isinstance(summary.get("count"), bool)
        or not 0 <= summary["count"] <= expected_total
        or (not fixed_acceptance_failure and summary["count"] != expected_total)
    ):
        raise ValueError(f"{label} processing count is invalid")


def _validate_duplicate_sample_semantics(
    sample: dict[str, Any], config: dict[str, Any], artifact_failures: list[str], label: str
) -> None:
    workload = sample.get("workload")
    database = sample.get("database")
    derived = _duplicate_hard_failure_codes(workload, database)
    fixed_acceptance_failure = "fixed_acceptance_failure" in derived
    _validate_duplicate_protocol(
        workload,
        config,
        label,
        fixed_acceptance_failure=fixed_acceptance_failure,
    )
    _validate_relation_subset(database, "duplicate-heavy", label)
    evidence = sample.get("evidence")
    spool = sample.get("spool")
    archive = sample.get("archive_operations")
    if not isinstance(evidence, dict) or not isinstance(spool, dict) or not isinstance(archive, dict):
        raise TypeError(f"{label} evidence is invalid")
    _validate_published_latency(workload, evidence, label)
    if evidence.get("response_count") != workload["official_responses"] or evidence.get("executed_responses") != workload["executed_observations"] or evidence.get("exact_bytes") != workload["exact_bytes"] or evidence.get("execution_method") != workload["aggregation_method"]:
        raise ValueError(f"{label} workload and evidence counters disagree")
    counters = workload.get("evidence_counters")
    if not isinstance(counters, dict) or any(
        counters.get(key) != evidence.get(key)
        for key in ("local_hits", "local_misses", "repairs", "provider_errors")
    ):
        raise ValueError(f"{label} evidence counters disagree")
    if evidence.get("projected_responses") != evidence["response_count"] - evidence["executed_responses"]:
        raise ValueError(f"{label} projected response count is invalid")
    for key in ("response_counts_by_endpoint", "occurrence_counts_by_endpoint"):
        if database.get(key) != workload[key]:
            raise ValueError(f"{label}.{key} disagrees with workload")
    local_misses = _bounded_int(evidence.get("local_misses"), f"{label}.local_misses")
    distinct_hashes = _bounded_int(evidence.get("distinct_hashes"), f"{label}.distinct_hashes")
    final_objects = _bounded_int(spool.get("final_object_count"), f"{label}.final_object_count")
    archive_get = _bounded_int(archive.get("get"), f"{label}.archive.get")
    repairs = _bounded_int(evidence.get("repairs"), f"{label}.repairs")
    exact_counters = (
        local_misses == archive_get == repairs
        and distinct_hashes == final_objects
        and local_misses >= distinct_hashes
    )
    failed_counters = (
        repairs <= local_misses
        and repairs <= archive_get
        and final_objects <= distinct_hashes
        and final_objects <= local_misses
        and final_objects <= archive_get
    )
    if not (failed_counters if fixed_acceptance_failure else exact_counters):
        raise ValueError(f"{label} local/archive/hash counters disagree")
    archived_bytes = _bounded_int(evidence.get("archived_bytes"), f"{label}.archived_bytes")
    archive_get_bytes = _bounded_int(archive.get("get_bytes"), f"{label}.archive.get_bytes")
    final_bytes = _bounded_int(spool.get("final_bytes"), f"{label}.final_bytes")
    exact_bytes = archived_bytes == final_bytes and archive_get_bytes >= archived_bytes
    failed_bytes = final_bytes <= archived_bytes and final_bytes <= archive_get_bytes
    if not (failed_bytes if fixed_acceptance_failure else exact_bytes):
        raise ValueError(f"{label} archived bytes disagree")
    if not _bounded_int(evidence.get("retries"), f"{label}.retries") == workload["processing_summary"]["retry_count"]:
        raise ValueError(f"{label}.retries disagrees with processing")
    if not set(derived).issubset(artifact_failures):
        raise ValueError(f"{label} duplicate hard failures are incomplete")
    if not derived:
        full_spool = workload.get("spool", {})
        residue_keys = (
            "temporary_bytes",
            "temporary_objects",
            "abandoned_temp_bytes",
            "abandoned_temp_objects",
            "reserved_bytes",
            "reserved_objects",
        )
        if (
            not isinstance(full_spool, dict)
            or any(key not in full_spool or full_spool[key] != 0 for key in residue_keys)
            or evidence.get("provider_errors") != 0
            or sample["database"].get("queue_residue")
        ):
            raise ValueError(f"{label} passing residue is non-zero")


def _validate_resources(value: dict[str, Any], label: str) -> None:
    _bounded_number(value.get("elapsed_seconds"), f"{label}.elapsed_seconds")
    _bounded_number(value.get("cpu_seconds"), f"{label}.cpu_seconds")
    _bounded_int(value.get("peak_rss_kib"), f"{label}.peak_rss_kib", positive=True)


def _validate_reset_semantics(
    samples: list[dict[str, Any]], config: dict[str, Any], mode: str, artifact_failures: list[str]
) -> None:
    generations = 2 if mode == "correction" else 1
    for index, sample in enumerate(samples):
        label = f"sample {index} reset"
        workload = sample["workload"]
        population = config["populations"][index]
        if workload["population"] != population:
            raise ValueError(f"{label} population disagrees with configuration")
        _validate_resources(sample, label)
        expected = {
            "ranked_day_versions": generations * population,
            "snapshot_headers": 2 * generations,
            "snapshot_entries": 2 * generations * population,
        }
        actual = {key: workload["fact_counts"][key] for key in expected}
        summary = workload["processing_summary"]["total"]
        derived: list[str] = []
        if workload["fanout_evidence"]["expected"] != expected or actual != expected:
            derived.append("reset_fanout_mismatch")
        if summary["outcomes"]["processed"] != summary["count"]:
            derived.append("reset_non_processed_result")
        if workload["queue_residue"] or sample["database"].get("queue_residue"):
            derived.append("reset_queue_residue")
        if len(workload["fanout_evidence"]["generation_states"]) != generations:
            derived.append("reset_generation_count_mismatch")
        derived = _failure_codes(derived)
        if set(workload["hard_failures"]) != set(derived) or not set(derived).issubset(artifact_failures):
            raise ValueError(f"{label} hard failures are incomplete")
        if workload["official_responses"] != workload["processing_summary"]["official"]["count"]:
            raise ValueError(f"{label} response count is invalid")


def _validate_memory_facts(before: Any, after: Any, delta: Any, label: str) -> None:
    for value, name in ((before, "before"), (after, "after")):
        if not isinstance(value, dict) or set(value) != _ARMY_MEMORY_KEYS:
            raise ValueError(f"{label}.{name} memory facts are invalid")
        for key in _ARMY_MEMORY_KEYS:
            _bounded_int(value[key], f"{label}.{name}.{key}")
        if value["process_cgroup_available"] not in {0, 1} or value["database_cgroup_available"] not in {0, 1}:
            raise ValueError(f"{label}.{name} cgroup facts are invalid")
    if not isinstance(delta, dict) or set(delta) != _ARMY_MEMORY_DELTA_KEYS:
        raise ValueError(f"{label}.delta memory facts are invalid")
    for key in _ARMY_MEMORY_DELTA_KEYS:
        _bounded_int(delta[key], f"{label}.delta.{key}")
        if delta[key] != max(0, after[key] - before[key]):
            raise ValueError(f"{label}.delta contradicts memory facts")


def _validate_mixed_semantics(
    sample: dict[str, Any], config: dict[str, Any], artifact_failures: list[str]
) -> None:
    workload = sample["workload"]
    if (
        workload["live_jobs"] != config["live_jobs"]
        or workload["backfill_jobs"] != config["backfill_jobs"]
        or workload["configured_lanes"] != config["lanes"]
        or workload["effective_lanes"] != config["effective_lanes"]
        or workload["official_responses"] != config["live_jobs"] + config["backfill_jobs"]
    ):
        raise ValueError("mixed workload disagrees with configuration")
    _validate_resources(workload, "mixed workload")
    counts = workload["completion_counts"]
    expected_counts = {
        "live": config["live_jobs"],
        "backfill": config["backfill_jobs"],
    }
    if any(counts[kind] > expected_counts[kind] for kind in expected_counts):
        raise ValueError("mixed completion counts disagree with jobs")
    order = workload["completion_order"]
    if order is not None and counts != {
        "live": order.count("live"),
        "backfill": order.count("backfill"),
    }:
        raise ValueError("mixed completion order disagrees with jobs")
    live = workload["live_queue_latency_seconds"]
    contract = workload["live_latency_contract"]
    if any(contract[key] != live[source] for key, source in {
        "p95_seconds": "p95",
        "maximum_seconds": "maximum",
        "collection_maximum_seconds": "collection_maximum",
    }.items()):
        raise ValueError("mixed latency contract is not sourced from measurements")
    five = workload["five_minute_contract"]
    if five["elapsed_seconds"] != workload["elapsed_seconds"] or five["passed"] is not (workload["elapsed_seconds"] <= five["target_seconds"]):
        raise ValueError("mixed five-minute contract is invalid")
    ages = sample["database"].get("queue_age_seconds", {})
    oldest = max((age for age in ages.values() if age is not None), default=None)
    if workload["oldest_active_queue_age_seconds"] != oldest:
        raise ValueError("mixed oldest queue age is invalid")
    _validate_memory_facts(
        workload["memory_pressure_before"],
        workload["memory_pressure_after"],
        workload["memory_pressure_delta"],
        "mixed workload",
    )
    derived: list[str] = []
    expected_count = config["live_jobs"] + config["backfill_jobs"]
    summary = workload["processing_summary"]
    if summary["expected_count"] != expected_count:
        raise ValueError("mixed expected processing count disagrees with jobs")
    if counts != {
        "live": summary["kinds"]["live"],
        "backfill": summary["kinds"]["backfill"],
    } or sum(counts.values()) != summary["count"]:
        raise ValueError("mixed completion counts disagree with processing")
    if summary["count"] != expected_count:
        derived.append("mixed_result_count_mismatch")
    if summary["outcomes"]["processed"] != summary["count"] or summary["statuses"]["complete"] != summary["count"]:
        derived.append("mixed_non_processed_result")
    if not contract["passed"]:
        derived.append("mixed_live_latency_exceeded")
    if not five["passed"]:
        derived.append("mixed_collection_latency_exceeded")
    if sample["database"].get("queue_residue"):
        derived.append("mixed_queue_residue")
    derived.extend(
        _memory_pressure_failure_codes(
            workload["memory_pressure_before"],
            workload["memory_pressure_after"],
            workload["memory_pressure_delta"],
        )
    )
    if workload["official_api_traffic"] != {"requests": 0, "source": "committed fixtures"}:
        raise ValueError("mixed official API traffic is invalid")
    if workload["hard_failures"] != _failure_codes(derived) or not set(derived).issubset(artifact_failures):
        raise ValueError("mixed hard failures are incomplete")


def _validate_army_plan(value: Any, selection: str, statement_id: int, label: str) -> None:
    if not isinstance(value, dict) or set(value) != _ARMY_PLAN_KEYS:
        raise ValueError(f"{label} plan schema is invalid")
    correlation = value["correlation"]
    if correlation != {
        "selection": selection,
        "lens": correlation.get("lens") if isinstance(correlation, dict) else None,
        "statement_id": statement_id,
    } or correlation["lens"] not in {"offense", "defense"}:
        raise ValueError(f"{label} plan correlation is invalid")
    identity = value["sql"]
    if not isinstance(identity, str) or (selection, identity) not in _ARMY_QUERY_SHAPES:
        raise ValueError(f"{label} plan identity is invalid")
    parameters = value["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != _ARMY_PARAMETER_KEYS:
        raise ValueError(f"{label} parameter shape is invalid")
    shape = tuple(parameters["types"]) if isinstance(parameters["types"], list) else ()
    if parameters["arity"] != len(shape) or shape != _ARMY_QUERY_SHAPES[(selection, identity)]:
        raise ValueError(f"{label} parameter shape is invalid")
    for kind in shape:
        if kind not in {"array", "bool", "float", "int", "null", "str", "timestamp"}:
            raise ValueError(f"{label} parameter shape is invalid")
    scanned = _bounded_int(value["rows_scanned"], f"{label}.rows_scanned")
    returned = _bounded_int(value["rows_returned"], f"{label}.rows_returned")
    if scanned < returned:
        raise ValueError(f"{label} plan counts are invalid")
    public = _public_explain_payload(value["explain_analyze_buffers"])
    if public != value["explain_analyze_buffers"]:
        raise ValueError(f"{label} plan retains non-public facts")
    planned_scanned, planned_returned = _plan_counts(public[0]["Plan"])
    if (scanned, returned) != (planned_scanned, planned_returned):
        raise ValueError(f"{label} plan counts disagree with EXPLAIN")


def _validate_army_selection(value: Any, spec: dict[str, Any], label: str) -> None:
    if not isinstance(value, dict) or value.get("selection") != spec["selection"] or value.get("lens") not in {"offense", "defense"}:
        raise ValueError(f"{label} selection identity is invalid")
    if value.get("warmups") != STEP5_WARMUPS or value.get("requests") != STEP5_REQUESTS:
        raise ValueError(f"{label} warmup/request protocol is invalid")
    _bounded_number(value.get("forced_miss_seconds"), f"{label}.forced_miss_seconds")
    _bounded_number(value.get("forced_miss_target_seconds"), f"{label}.forced_miss_target_seconds")
    _validate_memory_facts(value.get("forced_miss_memory_before"), value.get("forced_miss_memory_after"), value.get("forced_miss_memory_delta"), label)
    forced_passed = _army_forced_miss_passed(
        value["forced_miss_seconds"],
        value["forced_miss_memory_before"],
        value["forced_miss_memory_after"],
        value["forced_miss_memory_delta"],
    )
    if value.get("forced_miss_passed") is not forced_passed:
        raise ValueError(f"{label}.forced_miss_passed is invalid")
    latencies = value.get("latencies_ms")
    if not isinstance(latencies, list) or len(latencies) != STEP5_REQUESTS:
        raise ValueError(f"{label}.latencies_ms is invalid")
    values = [_bounded_number(item, f"{label}.latency") for item in latencies]
    numeric = [float(item) for item in values if item is not None]
    p95 = _p95(numeric)
    if not math.isclose(value["p95_ms"], p95, rel_tol=1e-12) or value["min_ms"] != min(numeric) or value["max_ms"] != max(numeric):
        raise ValueError(f"{label} latency summaries are invalid")
    if value.get("target_ms") != STEP5_P95_TARGET_MS or value.get("target_passed") is not (p95 < STEP5_P95_TARGET_MS):
        raise ValueError(f"{label} latency target is invalid")
    if value.get("selected_fact_count") != spec["expected_facts"] or value.get("expected_fact_count") != spec["expected_facts"]:
        raise ValueError(f"{label} selected fact counts are invalid")
    if value.get("troop_keys") != list(STEP5_TROOP_KEYS):
        raise ValueError(f"{label} troop keys are invalid")
    _bounded_int(value.get("peak_rss_kib"), f"{label}.peak_rss_kib", positive=True)
    plans = value.get("endpoint_sql")
    expected = _ARMY_QUERY_ORDER[spec["selection"]]
    if not isinstance(plans, list) or len(plans) != len(expected):
        raise ValueError(f"{label}.endpoint_sql is invalid")
    for statement_id, plan in enumerate(plans, 1):
        _validate_army_plan(
            plan, spec["selection"], statement_id, f"{label}.endpoint_sql[{statement_id}]"
        )
        if plan["correlation"]["lens"] != spec["lens"]:
            raise ValueError(f"{label}.endpoint_sql identity is invalid")
    identities = [plan["sql"] for plan in plans]
    if (
        identities[:-2] != list(expected[:-2])
        or frozenset(identities[-2:]) != _ARMY_OVERLAPPED_QUERY_IDENTITIES
    ):
        raise ValueError(f"{label}.endpoint_sql identity is invalid")


def _validate_nested_army_read_sample(value: Any, label: str) -> None:
    if not isinstance(value, dict) or value.get("status") != "passed":
        raise ValueError(f"{label} status is invalid")
    selections = value.get("selections")
    expected_selections = ("top-1000", "trophies-5000-9999", "streak-top-1000")
    if not isinstance(selections, list) or len(selections) != len(expected_selections):
        raise ValueError(f"{label}.selections are invalid")
    read_keys = {
        "selection",
        "synthetic_fact_limit",
        "rows_scanned",
        "rows_returned",
        "latency_ms",
        "endpoint",
        "explain_analyze_buffers",
    }
    for read, expected_selection in zip(selections, expected_selections, strict=True):
        if not isinstance(read, dict) or set(read) != read_keys or read["selection"] != expected_selection:
            raise ValueError(f"{label}.selection evidence is invalid")
        _bounded_int(read["synthetic_fact_limit"], f"{label}.synthetic_fact_limit", positive=True)
        scanned = _bounded_int(read["rows_scanned"], f"{label}.rows_scanned")
        returned = _bounded_int(read["rows_returned"], f"{label}.rows_returned")
        if scanned < returned:
            raise ValueError(f"{label}.plan counts are invalid")
        _bounded_number(read["latency_ms"], f"{label}.latency_ms")
        endpoint = read["endpoint"]
        if not isinstance(endpoint, dict) or set(endpoint) != {
            "status",
            "returned_fact_count",
            "latency_ms",
        } or endpoint["status"] not in {"returned", "not-found", "ArmyAnalyticsUnavailable", "CurrentSeasonEmpty"}:
            raise ValueError(f"{label}.endpoint evidence is invalid")
        _bounded_int(endpoint["returned_fact_count"], f"{label}.returned_fact_count")
        _bounded_number(endpoint["latency_ms"], f"{label}.endpoint.latency_ms")
        payload = read["explain_analyze_buffers"]
        if not isinstance(payload, dict):
            raise TypeError(f"{label}.plan evidence is invalid")
        public = _public_explain_payload([payload])[0]
        if public != payload:
            raise ValueError(f"{label}.plan retains raw EXPLAIN facts")
        planned_scanned, planned_returned = _plan_counts(public["Plan"])
        if (scanned, returned) != (planned_scanned, planned_returned):
            raise ValueError(f"{label}.plan counts disagree with EXPLAIN")


def _validate_army_mixed(value: Any, specs: tuple[dict[str, Any], ...], label: str) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("analytics_lanes"), list) or len(value["analytics_lanes"]) != len(specs):
        raise ValueError(f"{label} analytics lanes are invalid")
    spec_by_pair = {(spec["selection"], spec["lens"]): spec for spec in specs}
    seen: set[tuple[str, str]] = set()
    for lane in value["analytics_lanes"]:
        pair = (lane.get("selection"), lane.get("lens"))
        if pair in seen or pair not in spec_by_pair or lane.get("warmups") != STEP5_WARMUPS or lane.get("requests") != STEP5_REQUESTS:
            raise ValueError(f"{label} analytics lane identity is invalid")
        seen.add(pair)
        spec = spec_by_pair[pair]
        if lane.get("selected_fact_count") != spec["expected_facts"] or lane.get("troop_keys") != list(STEP5_TROOP_KEYS) or lane.get("target_ms") != STEP5_P95_TARGET_MS or lane.get("target_passed") is not (lane.get("p95_ms") < STEP5_P95_TARGET_MS):
            raise ValueError(f"{label} analytics lane facts are invalid")
        _bounded_number(lane.get("p95_ms"), f"{label}.lane.p95_ms")
        _bounded_int(lane.get("overlap_measurements"), f"{label}.lane.overlap_measurements")
    if seen != set(spec_by_pair):
        raise ValueError(f"{label} analytics lanes are incomplete")
    account = value.get("account")
    if not isinstance(account, dict) or account.get("warmups") != STEP5_WARMUPS or account.get("requests") != STEP5_REQUESTS:
        raise ValueError(f"{label} account protocol is invalid")
    _bounded_int(account.get("overlap_measurements"), f"{label}.account_overlap_measurements")
    latencies = account.get("latencies_ms")
    if not isinstance(latencies, list) or len(latencies) != STEP5_REQUESTS:
        raise ValueError(f"{label}.account.latencies_ms is invalid")
    values = [_bounded_number(item, f"{label}.account.latency") for item in latencies]
    numeric = [float(item) for item in values if item is not None]
    account_p95 = _p95(numeric)
    if (
        not math.isclose(account.get("p95_ms"), account_p95, rel_tol=1e-12)
        or account.get("min_ms") != min(numeric)
        or account.get("max_ms") != max(numeric)
    ):
        raise ValueError(f"{label} account latency summaries are invalid")
    if account.get("target_ms") != STEP5_P95_TARGET_MS or account.get("target_passed") is not (account_p95 < STEP5_P95_TARGET_MS):
        raise ValueError(f"{label} account latency target is invalid")
    overlaps = value.get("overlap_counts")
    expected_overlaps = {f"{selection}/{lens}": next(lane["overlap_measurements"] for lane in value["analytics_lanes"] if lane["selection"] == selection and lane["lens"] == lens) for selection, lens in spec_by_pair}
    if overlaps != expected_overlaps or value.get("account_overlap_measurements") != account["overlap_measurements"]:
        raise ValueError(f"{label} overlap counts are invalid")
    nested = _failure_codes(value.get("hard_failures"))
    if value.get("hard_failures") != nested:
        raise ValueError(f"{label} hard failures are invalid")
    cycle = value.get("collection_cycle")
    _validate_duplicate_protocol(
        cycle,
        {"duplicate_observations": DUPLICATE_EXECUTION_CAP, "duplicate_cycles": 1},
        f"{label}.collection_cycle",
        overlap_cycle=True,
        fixed_acceptance_failure="fixed_acceptance_failure"
        in _duplicate_hard_failure_codes(cycle, {}),
    )
    summary = cycle.get("processing_summary")
    expected: list[str] = []
    if any(count < STEP5_REQUESTS for count in overlaps.values()):
        expected.append("step5_overlap_incomplete")
    if summary.get("count") != DUPLICATE_EXECUTION_CAP:
        expected.append("step5_collection_result_count_mismatch")
    if summary.get("outcomes", {}).get("processed") != summary.get("count"):
        expected.append("step5_non_processed_result")
    if any(not lane["target_passed"] for lane in value["analytics_lanes"]):
        expected.append("step5_p95_exceeded")
    if account["overlap_measurements"] < STEP5_REQUESTS:
        expected.append("step5_account_overlap_incomplete")
    if not account["target_passed"]:
        expected.append("step5_account_p95_exceeded")
    if cycle["cycle_elapsed_seconds"] >= STEP5_COLLECTION_LIMIT_SECONDS:
        expected.append("step5_collection_cycle_too_slow")
    if nested != _failure_codes(expected):
        raise ValueError(f"{label} hard failures are incomplete")


def _validate_step5_statistics(
    value: Any, database: Any, label: str
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "relations",
        "readiness_timeout_seconds",
        "analyze_completed",
        "active_analyzes",
        "ready",
    }:
        raise ValueError(f"{label} schema is invalid")
    completed = value["analyze_completed"]
    active = value["active_analyzes"]
    if (completed is not None and not isinstance(completed, bool)) or (
        active is not None
        and _bounded_int(active, f"{label}.active_analyzes") > 64
    ):
        raise ValueError(f"{label} facts are invalid")
    if (
        value["relations"] != list(STEP5_STATISTICS_RELATIONS)
        or value["readiness_timeout_seconds"] != STEP5_STATISTICS_TIMEOUT_SECONDS
        or not isinstance(value["ready"], bool)
        or value["ready"] is not (completed is True and active == 0)
    ):
        raise ValueError(f"{label} facts are invalid")
    if not isinstance(database, dict):
        raise TypeError(f"{label} database facts are invalid")
    required = set(STEP5_STATISTICS_RELATIONS)
    for name in ("relations", "relation_sizes", "relation_stats"):
        relation_map = database.get(name)
        if not isinstance(relation_map, dict) or not required.issubset(relation_map):
            raise ValueError(f"{label} database facts are incomplete")
    if completed is True:
        for relation in STEP5_STATISTICS_RELATIONS:
            stats = database["relation_stats"][relation]
            if not isinstance(stats, dict):
                raise TypeError(f"{label} analyze timestamp is invalid")
            try:
                analyzed_at = datetime.fromisoformat(
                    _bounded_text(
                        stats.get("last_analyze"),
                        f"{label}.{relation}.last_analyze",
                        limit=64,
                    )
                )
            except ValueError as error:
                raise ValueError(f"{label} analyze timestamp is invalid") from error
            if analyzed_at.tzinfo is None:
                raise ValueError(f"{label} analyze timestamp is invalid")


def _validate_army_semantics(
    value: dict[str, Any], provenance: dict[str, Any], artifact_failures: list[str], label: str
) -> None:
    configuration = provenance.get("configuration", {})
    if (
        configuration.get("army_warmups") != STEP5_WARMUPS
        or configuration.get("army_requests") != STEP5_REQUESTS
        or configuration.get("analytics_lanes") != STEP5_ANALYTICS_LANES
    ):
        raise ValueError(f"{label} configuration is not the fixed army protocol")
    protocol = {
        "population": STEP5_POPULATION,
        "query_work_mem": "256MB",
        "days": STEP5_DAYS,
        "facts_per_member_day_per_lens": STEP5_FACTS_PER_MEMBER_DAY,
        "selected_members": STEP5_SELECTED_MEMBERS,
        "missing_trophy_rate": f"1/{STEP5_MISSING_TROPHY_RATE}",
        "troop_keys": len(STEP5_TROOP_KEYS),
        "warmups": STEP5_WARMUPS,
        "requests": STEP5_REQUESTS,
        "p95_target_ms": STEP5_P95_TARGET_MS,
        "forced_miss_target_seconds": STEP5_FORCED_MISS_TARGET_SECONDS,
        "forced_miss_pool_max_size": 2,
        "forced_miss_read_snapshot": "repeatable_read_exported",
        "mixed_lane_pool_max_size": STEP5_MIXED_LANE_POOL_MAX_SIZE,
        "mixed_lane_read_snapshot": "repeatable_read_exported",
        "analytics_lanes": STEP5_ANALYTICS_LANES,
        "duplicate_cycle_observations": DUPLICATE_EXECUTION_CAP,
    }
    if value.get("protocol") != protocol:
        raise ValueError(f"{label}.protocol is invalid")
    seed = {
        "population": STEP5_POPULATION,
        "days": STEP5_DAYS,
        "facts_per_lens": STEP5_POPULATION * STEP5_DAYS * STEP5_FACTS_PER_MEMBER_DAY,
        "missing_trophies_per_lens": STEP5_POPULATION * STEP5_DAYS * STEP5_FACTS_PER_MEMBER_DAY // STEP5_MISSING_TROPHY_RATE,
        "snapshots": STEP5_DAYS,
        "snapshot_entries": STEP5_DAYS * STEP5_SELECTED_MEMBERS,
        "completed_days": STEP5_DAYS,
        "selected_facts_per_lens": STEP5_SELECTED_MEMBERS * STEP5_DAYS * STEP5_FACTS_PER_MEMBER_DAY,
        "troop_keys": len(STEP5_TROOP_KEYS),
    }
    if value.get("seed") != seed:
        raise ValueError(f"{label}.seed is invalid")
    _validate_step5_statistics(
        value.get("statistics_readiness"),
        value.get("database"),
        f"{label}.statistics_readiness",
    )
    specs = _army_selection_specs()
    selections = value.get("selections")
    if not isinstance(selections, list) or len(selections) > len(specs):
        raise ValueError(f"{label}.selections are invalid")
    for selection, spec in zip(selections, specs):
        _validate_army_selection(selection, spec, f"{label}.{selection.get('selection')}/{selection.get('lens')}")
    if value["queue_drained"] is not (not value["database"].get("queue_residue")):
        raise ValueError(f"{label}.queue_drained is invalid")
    _validate_postgres_identity(value["postgres"], f"{label}.postgres")
    if value["postgres"] != provenance.get("postgres") or provenance.get("execution", {}).get("postgres") != {
        "version": value["postgres"]["version"],
        "settings": value["postgres"]["settings"],
    }:
        raise ValueError(f"{label}.postgres provenance is invalid")
    _validate_resources(value, label)
    _validate_memory_facts(value["memory_pressure_before"], value["memory_pressure_after"], value["memory_pressure_delta"], label)
    expected_failures = _army_completed_read_failures(selections)
    resource_failures: list[str] = []
    if value["database"].get("queue_residue"):
        resource_failures.append("queue_residue")
    if any(value["memory_pressure_before"][key] != 1 or value["memory_pressure_after"][key] != 1 for key in ("process_cgroup_available", "database_cgroup_available")):
        resource_failures.append("memory_pressure_unavailable")
    if any(value["memory_pressure_delta"].values()):
        resource_failures.append("memory_pressure_increased")
    failed_phase = value.get("failed_phase")
    failure = value.get("failure")
    if failure is not None:
        expected_failures.extend(resource_failures)
        expected_failures.append("army_read_sample_unavailable")
        expected_failures = _failure_codes(expected_failures)
        if (
            value.get("status") != "failed"
            or value.get("mixed_load") is not None
            or value.get("hard_failures") != expected_failures
            or artifact_failures != expected_failures
            or failed_phase
            not in {"statistics_readiness", "selection_reads", "mixed_load"}
            or failure
            not in {
                "statistics_timeout",
                "statistics_not_ready",
                "statistics_unavailable",
                "request_timeout",
                "workload_error",
            }
        ):
            raise ValueError(f"{label} bounded failure is invalid")
        readiness = value["statistics_readiness"]
        if failed_phase == "statistics_readiness":
            if selections or failure not in {
                "statistics_timeout",
                "statistics_not_ready",
                "statistics_unavailable",
            }:
                raise ValueError(f"{label} readiness failure is invalid")
            if failure == "statistics_timeout" and (
                readiness["analyze_completed"] is None
                or (
                    readiness["analyze_completed"] is True
                    and readiness["active_analyzes"] is not None
                )
            ):
                raise ValueError(f"{label} readiness timeout is invalid")
            if failure == "statistics_not_ready" and (
                readiness["analyze_completed"] is not True
                or not isinstance(readiness["active_analyzes"], int)
                or readiness["active_analyzes"] == 0
            ):
                raise ValueError(f"{label} readiness state is invalid")
            if failure == "statistics_unavailable" and (
                readiness["ready"]
                or readiness["active_analyzes"] is not None
            ):
                raise ValueError(f"{label} readiness availability is invalid")
        elif (
            failure not in {"request_timeout", "workload_error"}
            or not readiness["ready"]
            or (failed_phase == "selection_reads" and len(selections) == len(specs))
            or (failed_phase == "mixed_load" and len(selections) != len(specs))
        ):
            raise ValueError(f"{label} timed read failure is invalid")
        return

    if failed_phase is not None or not value["statistics_readiness"]["ready"]:
        raise ValueError(f"{label} complete execution state is invalid")
    if len(selections) != len(specs):
        raise ValueError(f"{label}.selections are incomplete")
    _validate_army_mixed(value["mixed_load"], specs, f"{label}.mixed_load")
    cycle_counts = value["mixed_load"]["collection_cycle"]["occurrence_counts_by_endpoint"]
    expected_counts = dict(cycle_counts)
    expected_counts["profile"] += 1
    if value["database"]["response_counts_by_endpoint"] != expected_counts or value["database"]["occurrence_counts_by_endpoint"] != expected_counts:
        raise ValueError(f"{label}.database counts are invalid")
    expected_failures.extend(value["mixed_load"]["hard_failures"])
    expected_failures.extend(resource_failures)
    expected_failures = _failure_codes(expected_failures)
    if value.get("hard_failures") != expected_failures or value.get("hard_failures") != artifact_failures:
        raise ValueError(f"{label} hard failures are incomplete")
    if value.get("status") != ("passed" if not expected_failures else "failed"):
        raise ValueError(f"{label}.status contradicts hard failures")


def _reject_retained_job_details(value: Any) -> None:
    """Reject unbounded per-job detail anywhere in a retained artifact."""
    if isinstance(value, dict):
        if _RETAINED_JOB_DETAIL_KEYS.intersection(value) or any(
            isinstance(key, str) and key.startswith("_") for key in value
        ):
            raise ValueError("artifact retains internal or per-job details")
        for child in value.values():
            _reject_retained_job_details(child)
    elif isinstance(value, list):
        if len(value) > MAX_RETAINED_SEQUENCE:
            raise ValueError("artifact retains an unbounded sequence")
        for child in value:
            _reject_retained_job_details(child)


def _write_artifact(path: Path, payload: str) -> None:
    """Publish one complete artifact without overwriting retained evidence."""
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    linked = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        linked = True
        temporary.unlink()
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except FileExistsError as error:
        raise RuntimeError("artifact output is already occupied") from error
    except (OSError, UnicodeError) as error:
        if linked:
            path.unlink(missing_ok=True)
        raise RuntimeError("artifact could not be written atomically") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def validate_artifact(artifact: dict[str, Any]) -> None:
    """Reject artifacts that cannot support the current performance review."""
    if not isinstance(artifact, dict):
        raise TypeError("artifact must be an object")
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            "artifact schema_version is missing or old; "
            f"expected {ARTIFACT_SCHEMA_VERSION}"
        )
    if artifact.get("artifact_digest") != _artifact_digest(artifact):
        raise ValueError("artifact digest is missing or invalid")
    _reject_retained_job_details(artifact)
    if set(artifact) != _ARTIFACT_KEYS:
        raise ValueError("artifact schema is invalid")
    required = (
        "artifact_digest",
        "mode",
        "started_at",
        "finished_at",
        "provenance",
        "execution",
        "prepared_candidate_images",
        "candidate_receipt",
        "official_api_requests",
        "collector_probe",
        "samples",
        "army_read_sample",
        "hard_failures",
    )
    missing = [key for key in required if key not in artifact]
    if missing:
        raise ValueError("artifact missing metrics: " + ", ".join(missing))
    mode = artifact["mode"]
    if mode not in MODES:
        raise ValueError(f"artifact has unknown mode: {mode}")
    samples = artifact["samples"]
    if not isinstance(samples, list):
        raise TypeError("artifact samples metric must be a list")
    if mode != STEP5_MODE and not samples:
        raise ValueError("artifact missing metrics: samples")
    database_metrics = (
        "wal_bytes",
        "wal_retained_bytes",
        "wal_retained_growth_bytes",
        "sql_statement_calls",
        "application_sql_calls",
        "pending_remote_verification",
        "response_counts_by_endpoint",
        "occurrence_counts_by_endpoint",
        "relations",
        "relation_sizes",
        "relation_stats",
        "affected_relations",
        "queues",
        "queue_age_seconds",
        "queue_residue",
    )
    archive_metrics = (
        "get",
        "get_bytes",
        "head",
        "conditional_put",
        "put",
        "put_bytes",
        "conflicts",
    )

    def require(mapping: Any, keys: tuple[str, ...], label: str) -> None:
        if not isinstance(mapping, dict):
            raise TypeError(f"{label} metric must be an object")
        missing_keys = [key for key in keys if key not in mapping]
        if missing_keys:
            raise ValueError(
                f"{label} missing metrics: {', '.join(missing_keys)}"
            )

    def require_exact(mapping: Any, keys: frozenset[str], label: str) -> None:
        if not isinstance(mapping, dict) or set(mapping) != keys:
            raise ValueError(f"{label} schema is invalid")

    def require_failure_codes(value: Any, label: str) -> None:
        if (
            not isinstance(value, list)
            or len(value) > MAX_RETAINED_FAILURE_CODES
            or any(
                not isinstance(code, str) or code not in HARD_FAILURE_CODES
                for code in value
            )
            or len(value) != len(set(value))
        ):
            raise ValueError(f"{label} hard failures are invalid")

    def require_processing_summary(value: Any, label: str) -> None:
        expected_keys = {
            "count",
            "expected_count",
            "count_matches_expected",
            "outcomes",
            "statuses",
            "work_types",
            "kinds",
            "retry_count",
            "completed_count",
            "failed_count",
            "elapsed_ms",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise ValueError(f"{label} result summary is invalid")
        if (
            set(value["outcomes"]) != set(_RESULT_OUTCOMES)
            or set(value["statuses"]) != set(_RESULT_STATUSES)
            or set(value["work_types"]) != set(_RESULT_WORK_TYPES)
            or set(value["kinds"]) != {"live", "backfill", "other"}
        ):
            raise ValueError(f"{label} result distributions are invalid")
        for distribution_name in ("outcomes", "statuses", "work_types", "kinds"):
            distribution = value[distribution_name]
            if any(
                not isinstance(count, int) or isinstance(count, bool) or count < 0
                for count in distribution.values()
            ):
                raise ValueError(f"{label} result distributions are invalid")
            if sum(distribution.values()) != value["count"]:
                raise ValueError(f"{label} result distributions contradict count")
        if any(
            not isinstance(value.get(name), int)
            or isinstance(value.get(name), bool)
            or value[name] < 0
            for name in ("count", "retry_count", "completed_count", "failed_count")
        ):
            raise ValueError(f"{label} result counts are invalid")
        if not isinstance(value["count_matches_expected"], bool):
            raise TypeError(f"{label} result count status is invalid")
        expected_count = value["expected_count"]
        if (
            expected_count is not None
            and (
                not isinstance(expected_count, int)
                or isinstance(expected_count, bool)
                or expected_count < 0
            )
        ) or value["count_matches_expected"] != (
            expected_count is None or value["count"] == expected_count
        ):
            raise ValueError(f"{label} expected result count is invalid")
        if (
            value["retry_count"] != value["outcomes"]["retrying"]
            or value["failed_count"]
            != value["outcomes"]["failed"] + value["outcomes"]["lease_lost"]
            or value["completed_count"]
            != (
                value["outcomes"]["processed"]
                if value["statuses"]["other"] == value["count"]
                else value["statuses"]["complete"]
            )
        ):
            raise ValueError(f"{label} derived result counts are invalid")
        if not isinstance(value["elapsed_ms"], dict) or set(value["elapsed_ms"]) != {
            "count",
            "sum",
            "maximum",
            "p95_upper",
        }:
            raise ValueError(f"{label} result latency summary is invalid")
        elapsed = value["elapsed_ms"]
        if (
            not isinstance(elapsed["count"], int)
            or isinstance(elapsed["count"], bool)
            or not 0 <= elapsed["count"] <= value["count"]
            or not isinstance(elapsed["sum"], (int, float))
            or isinstance(elapsed["sum"], bool)
            or not math.isfinite(float(elapsed["sum"]))
            or elapsed["sum"] < 0
            or any(
                item is not None
                and (
                    not isinstance(item, (int, float))
                    or isinstance(item, bool)
                    or not math.isfinite(float(item))
                    or item < 0
                )
                for item in (elapsed["maximum"], elapsed["p95_upper"])
            )
            or (elapsed["count"] == 0)
            != (elapsed["maximum"] is None and elapsed["p95_upper"] is None)
        ):
            raise ValueError(f"{label} result latency values are invalid")

    provenance = artifact["provenance"]
    require(
        provenance,
        (
            "source_sha",
            "source_dirty",
            "runner_sha256",
            "migrations",
            "applied_migration_versions",
            "configuration_fingerprint",
            "configuration",
            "host",
            "execution",
            "prepared_candidate_images",
            "candidate_receipt",
        ),
        "provenance",
    )
    require_exact(provenance, _PROVENANCE_KEYS, "provenance")
    source_sha = provenance["source_sha"]
    expected_source_sha = _clean_source()
    if (
        not isinstance(source_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or source_sha != expected_source_sha
        or provenance["source_dirty"] is not False
    ):
        raise ValueError("provenance source is not a clean exact revision")
    runner_sha = provenance["runner_sha256"]
    expected_runner_sha = _sha(Path(__file__).read_bytes())
    if (
        not isinstance(runner_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", runner_sha) is None
        or runner_sha != expected_runner_sha
    ):
        raise ValueError("provenance runner hash is stale or invalid")
    migrations = provenance["migrations"]
    expected_migrations = _source_migrations()
    if migrations != expected_migrations:
        raise ValueError("provenance migration files are stale or invalid")
    applied = provenance["applied_migration_versions"]
    if applied != list(REQUIRED_MIGRATION_VERSIONS):
        raise ValueError("provenance applied migration state is incomplete or invalid")
    postgres = provenance.get("postgres")
    require(postgres, ("version", "settings", "applied_migration_versions"), "provenance.postgres")
    if (
        not isinstance(postgres["version"], str)
        or not isinstance(postgres["settings"], dict)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in postgres["settings"].items()
        )
    ):
        raise ValueError("provenance PostgreSQL identity is invalid")
    _validate_postgres_identity(postgres, "provenance.postgres")
    if postgres["applied_migration_versions"] != applied:
        raise ValueError("provenance PostgreSQL migration state contradicts the artifact")
    configuration = provenance["configuration"]
    expected_configuration_fingerprint = _sha(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    )
    if (
        not isinstance(configuration, dict)
        or provenance["configuration_fingerprint"] != expected_configuration_fingerprint
        or set(configuration) != CONFIGURATION_KEYS
        or configuration["mode"] != mode
        or not isinstance(configuration["post_fix"], bool)
        or configuration["duplicate_endpoint_mix"] != dict(DUPLICATE_ENDPOINT_MIX)
        or not isinstance(configuration["populations"], list)
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in configuration["populations"]
        )
        ):
        raise ValueError("provenance configuration fingerprint is invalid")
    execution = artifact["execution"]
    if execution != provenance["execution"]:
        raise ValueError("artifact execution provenance contradicts its source provenance")
    if (
        not isinstance(execution, dict)
        or set(execution)
        != {"kind", "executor_images", "host_identity", "runtime", "postgres"}
        or execution.get("kind") != "host"
        or execution.get("executor_images") != []
        or not isinstance(execution.get("host_identity"), dict)
        or set(execution["host_identity"]) != {"platform", "uname"}
        or not isinstance(execution.get("runtime"), dict)
        or set(execution["runtime"]) != {"python"}
        or execution.get("postgres")
        != {"version": postgres["version"], "settings": postgres["settings"]}
        or not isinstance(provenance["host"], dict)
        or execution["host_identity"]
        != {
            "platform": provenance["host"].get("platform"),
            "uname": provenance["host"].get("uname"),
        }
    ):
        raise ValueError("artifact execution must identify the host executor")
    if artifact["prepared_candidate_images"] != provenance["prepared_candidate_images"]:
        raise ValueError("artifact prepared-image provenance contradicts its source provenance")
    prepared = artifact["prepared_candidate_images"]
    if not isinstance(prepared, list) or len(prepared) not in {0, 3}:
        raise ValueError("artifact prepared-image provenance is invalid")
    applications: set[str] = set()
    for identity in prepared:
        if not isinstance(identity, dict):
            raise TypeError("artifact prepared-image provenance is invalid")
        if (
            set(identity)
            != {
                "application",
                "identity_type",
                "requested_reference",
                "image_id",
                "registry_digest",
                "source_label",
                "revision_label",
            }
            or identity["identity_type"] != "prepared_candidate_image_id"
            or identity["application"] not in {"collector", "python", "website"}
            or identity["application"] in applications
            or not isinstance(identity["image_id"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", identity["image_id"]) is None
            or not isinstance(identity["requested_reference"], str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}",
                identity["requested_reference"],
            )
            is None
            or not isinstance(identity["source_label"], str)
            or identity["source_label"] != CANONICAL_REPOSITORY_URL
            or identity["revision_label"] != source_sha
        ):
            raise ValueError("artifact prepared-image provenance is invalid")
        registry_digest = identity["registry_digest"]
        if registry_digest is not None and (
            not isinstance(registry_digest, str)
            or re.fullmatch(r"(?:[A-Za-z0-9._:/@+-]+@)?sha256:[0-9a-f]{64}", registry_digest)
            is None
        ):
            raise ValueError("artifact prepared-image registry digest is invalid")
        applications.add(identity["application"])
    if applications != ({"collector", "python", "website"} if prepared else set()):
        raise ValueError("artifact prepared-image provenance is incomplete")
    candidate_receipt = artifact["candidate_receipt"]
    if candidate_receipt != provenance["candidate_receipt"]:
        raise ValueError("artifact candidate receipt provenance contradicts its source provenance")
    if (candidate_receipt is None) != (not prepared):
        raise ValueError("artifact prepared images require a candidate receipt")
    if candidate_receipt is not None and (
        not isinstance(candidate_receipt, dict)
        or set(candidate_receipt)
        != {"schema_version", "receipt_scope", "receipt_digest", "source_sha"}
        or candidate_receipt["schema_version"] != CANDIDATE_RECEIPT_SCHEMA_VERSION
        or candidate_receipt["receipt_scope"] != "candidate-preparation"
        or candidate_receipt["source_sha"] != source_sha
        or not isinstance(candidate_receipt["receipt_digest"], str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", candidate_receipt["receipt_digest"])
        is None
    ):
        raise ValueError("artifact candidate receipt provenance is invalid")
    if (
        not isinstance(artifact["official_api_requests"], dict)
        or artifact["official_api_requests"]
        != {"count": 0, "source": "committed fixtures"}
    ):
        raise ValueError("artifact official API request count is invalid")
    collector_probe = artifact["collector_probe"]
    if collector_probe is not None:
        expected_probe_keys = (
            frozenset({"executed", "elapsed_seconds", "test"})
            if collector_probe.get("executed") is True
            else frozenset({"executed", "reason"})
        )
        require_exact(collector_probe, expected_probe_keys, "collector probe")
    require_failure_codes(artifact["hard_failures"], "artifact")

    if mode == "duplicate-heavy":
        require(artifact["provenance"], ("postgres",), "provenance")
        require(
            artifact["provenance"]["postgres"],
            ("version", "settings"),
            "provenance.postgres",
        )

    if len(samples) > MAX_ARTIFACT_SAMPLES:
        raise ValueError("artifact samples are unbounded")
    for index, sample in enumerate(samples):
        label = f"sample {index}"
        require(sample, ("database", "archive_operations", "storage_runway"), label)
        require(sample["database"], database_metrics, f"{label} database")
        require_exact(sample["database"], frozenset(database_metrics), f"{label} database")
        require(
            sample["archive_operations"], archive_metrics, f"{label} archive_operations"
        )
        require_exact(
            sample["archive_operations"],
            frozenset(archive_metrics),
            f"{label} archive_operations",
        )
        require(
            sample["storage_runway"],
            ("measured_local_growth_bytes", "days_to_80_percent", "checks"),
            f"{label} storage_runway",
        )
        if isinstance(sample.get("workload"), dict) and "hard_failures" in sample["workload"]:
            require_failure_codes(sample["workload"]["hard_failures"], f"{label} workload")
        if isinstance(sample.get("workload"), dict) and "processing_summary" in sample["workload"]:
            summary = sample["workload"]["processing_summary"]
            if isinstance(summary, dict) and "total" in summary:
                for name in ("official", "dependent", "correction", "total"):
                    require_processing_summary(summary.get(name), f"{label} workload {name}")
            else:
                require_processing_summary(summary, f"{label} workload")
        if mode == "duplicate-heavy":
            require(
                sample.get("workload"),
                (
                    "response_counts_by_endpoint",
                    "occurrence_counts_by_endpoint",
                    "fixture_bytes_by_endpoint",
                    "exact_bytes",
                    "contract",
                    "canonical_content",
                ),
                f"{label} duplicate workload",
            )
            require(
                sample["workload"]["contract"],
                ("expected_occurrences", "executed_occurrences", "endpoint_mix"),
                f"{label} duplicate contract",
            )
            require(
                sample["workload"]["canonical_content"],
                (
                    "parsed_payloads_by_endpoint",
                    "profile_semantic_versions",
                    "profile_occurrence_effects",
                    "battle_canonical_rows",
                    "battle_occurrence_rows",
                    "ranking_canonical_rows",
                    "ranking_occurrence_links",
                ),
                f"{label} canonical content",
            )
            require_exact(
                sample["workload"],
                _DUPLICATE_WORKLOAD_KEYS,
                f"{label} duplicate workload",
            )
            required_failures = _duplicate_hard_failure_codes(
                sample["workload"], sample["database"]
            )
            if not set(required_failures).issubset(artifact["hard_failures"]):
                raise ValueError(
                    f"{label} duplicate hard failures are incomplete"
                )
        if mode == "coordinator-12500":
            require_exact(sample, _COORDINATOR_SAMPLE_KEYS, label)
            require_exact(
                sample["workload"],
                _COORDINATOR_WORKLOAD_KEYS,
                f"{label} coordinator workload",
            )
        else:
            require_exact(sample, _STANDARD_SAMPLE_KEYS, label)

    if mode == STEP5_MODE:
        army_sample = artifact["army_read_sample"]
        require(army_sample, ("database",), "army_read_sample")
        require(army_sample["database"], database_metrics, "army_read_sample database")
        if "hard_failures" in army_sample:
            require_failure_codes(army_sample["hard_failures"], "army_read_sample")
        require_exact(
            army_sample,
            frozenset(
                {
                    "status",
                    "failed_phase",
                    "failure",
                    "protocol",
                    "seed",
                    "statistics_readiness",
                    "selections",
                    "mixed_load",
                    "database",
                    "postgres",
                    "elapsed_seconds",
                    "cpu_seconds",
                    "peak_rss_kib",
                    "memory_pressure_before",
                    "memory_pressure_after",
                    "memory_pressure_delta",
                    "hard_failures",
                    "queue_drained",
                }
            ),
            "army_read_sample",
        )
    if mode in {"reset-boundary", "correction"}:
        for index, sample in enumerate(samples):
            workload = sample.get("workload") if isinstance(sample, dict) else None
            if not isinstance(workload, dict):
                raise TypeError(f"sample {index} workload is invalid")
            require(
                workload,
                (
                    "status",
                    "hard_failures",
                    "population",
                    "processing_summary",
                    "fact_counts",
                    "fanout_evidence",
                    "queue_residue",
                    "boundary_admission",
                ),
                f"sample {index} workload",
            )
            require_exact(
                workload,
                _RESET_WORKLOAD_KEYS,
                f"sample {index} reset workload",
            )
            summary = workload.get("processing_summary")
            for name in ("official", "dependent", "correction", "total"):
                require_processing_summary(
                    summary.get(name) if isinstance(summary, dict) else None,
                    f"sample {index} workload {name}",
                )
            population = workload["population"]
            if (
                not isinstance(population, int)
                or isinstance(population, bool)
                or population < 1
                or workload["status"]
                != ("passed" if not workload["hard_failures"] else "failed")
            ):
                raise ValueError(f"sample {index} reset result is invalid")
            require(
                workload["fact_counts"],
                (
                    "ranked_day_versions",
                    "snapshot_headers",
                    "snapshot_entries",
                    "analytics_summaries",
                    "army_facts",
                ),
                f"sample {index} reset fact counts",
            )
            require(
                workload["fanout_evidence"],
                (
                    "expected",
                    "matches_expected",
                    "snapshot_entries_per_population",
                    "generation_states",
                ),
                f"sample {index} reset fanout",
            )
            generations = 2 if mode == "correction" else 1
            expected_counts = {
                "ranked_day_versions": generations * population,
                "snapshot_headers": 2 * generations,
                "snapshot_entries": 2 * generations * population,
            }
            actual_counts = {
                name: workload["fact_counts"][name] for name in expected_counts
            }
            generation_states = workload["fanout_evidence"]["generation_states"]
            if (
                workload["fanout_evidence"]["expected"] != expected_counts
                or not isinstance(generation_states, list)
                or len(generation_states) > generations + 1
                or workload["fanout_evidence"]["matches_expected"]
                != (
                    len(generation_states) == generations
                    and actual_counts == expected_counts
                )
                or workload["fanout_evidence"]["snapshot_entries_per_population"]
                != 2 * len(generation_states)
                or any(
                    not isinstance(state, dict)
                    or set(state) != {"generation", "snapshot_state", "army_state"}
                    or state["generation"] != position
                    or state["snapshot_state"] not in {"pending", "ready", "published", "failed", "superseded"}
                    or state["army_state"] not in {"pending", "ready", "published", "failed", "superseded"}
                    for position, state in enumerate(generation_states, 1)
                )
                or not isinstance(workload["queue_residue"], list)
                or len(workload["queue_residue"]) > 128
                or any(
                    not isinstance(row, dict)
                    or set(row) != {"owner", "work_type", "count"}
                    for row in workload["queue_residue"]
                )
            ):
                raise ValueError(f"sample {index} reset fanout evidence is invalid")
            if mode == "reset-boundary":
                admission = workload["boundary_admission"]
                if not isinstance(admission, dict) or set(admission) != {
                    "admit",
                    "handoff",
                }:
                    raise ValueError(
                        f"sample {index} boundary admission evidence is invalid"
                    )
                _validate_boundary_admission_evidence(
                    admission["admit"], "admit", population
                )
                _validate_boundary_admission_evidence(
                    admission["handoff"], "handoff", population
                )
                if not workload["hard_failures"] and (
                    actual_counts != expected_counts
                    or generation_states
                    != [
                        {
                            "generation": 1,
                            "snapshot_state": "published",
                            "army_state": "published",
                        }
                    ]
                    or workload["queue_residue"]
                ):
                    raise ValueError(
                        f"sample {index} passing reset evidence is contradictory"
                    )
            elif workload["boundary_admission"] is not None:
                raise ValueError(
                    f"sample {index} correction admission evidence is invalid"
                )
        army_sample = artifact["army_read_sample"]
        require(army_sample, ("status", "hard_failures"), "army_read_sample")
        require_failure_codes(army_sample["hard_failures"], "army_read_sample")
        if army_sample["status"] == "passed":
            require(
                army_sample,
                ("database", "selections", "elapsed_seconds", "cpu_seconds", "peak_rss_kib"),
                "army_read_sample",
            )
            _validate_nested_army_read_sample(army_sample, "army_read_sample")
        elif army_sample["status"] == "failed":
            require(army_sample, ("reason",), "army_read_sample")
            if army_sample["reason"] != "army_read_sample_unavailable":
                raise ValueError("army_read_sample failure reason is invalid")
        else:
            raise ValueError("army_read_sample status is invalid")
    if mode == "mixed-backfill":
        require(
            artifact["provenance"],
            ("source_sha", "migrations", "configuration_fingerprint"),
            "mixed-backfill provenance",
        )
        workload = artifact["samples"][0].get("workload") if artifact["samples"] else None
        require(
            workload,
            (
                "completion_order",
                "completion_counts",
                "live_jobs",
                "backfill_jobs",
                "live_queue_latency_seconds",
                "oldest_active_queue_age_seconds",
                "elapsed_seconds",
                "cpu_seconds",
                "peak_rss_kib",
                "memory_pressure_delta",
                "live_latency_contract",
                "five_minute_contract",
                "hard_failures",
                "official_api_traffic",
                "completion_order_complete",
                "processing_summary",
            ),
            "mixed-backfill workload",
        )
        require_exact(workload, _MIXED_WORKLOAD_KEYS, "mixed-backfill workload")
        require(
            workload["live_latency_contract"],
            (
                "target_seconds",
                "p95_seconds",
                "maximum_seconds",
                "collection_maximum_seconds",
                "passed",
            ),
            "mixed-backfill live latency contract",
        )
        require(
            workload["five_minute_contract"],
            ("target_seconds", "elapsed_seconds", "passed"),
            "mixed-backfill five-minute contract",
        )
        require_failure_codes(workload["hard_failures"], "mixed-backfill workload")
        completion_order = workload["completion_order"]
        if not isinstance(workload.get("completion_order_complete"), bool):
            raise ValueError("mixed-backfill completion order status is invalid")
        if not isinstance(workload.get("completion_counts"), dict) or set(
            workload["completion_counts"]
        ) != {"live", "backfill"} or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in workload["completion_counts"].values()
        ):
            raise ValueError("mixed-backfill completion counts are invalid")
        expected_completion_counts = {
            "live": int(workload.get("live_jobs", -1)),
            "backfill": int(workload.get("backfill_jobs", -1)),
        }
        if any(
            workload["completion_counts"][kind] > expected_completion_counts[kind]
            for kind in expected_completion_counts
        ):
            raise ValueError("mixed-backfill completion counts exceed jobs")
        if completion_order is not None and (
            not isinstance(completion_order, list)
            or len(completion_order) > MAX_COMPLETION_ORDER
            or len(completion_order)
            > int(workload.get("live_jobs", 0)) + int(workload.get("backfill_jobs", 0))
            or any(item not in {"live", "backfill"} for item in completion_order)
            or workload["completion_counts"] != expected_completion_counts
            or workload["completion_counts"] != {
                "live": completion_order.count("live"),
                "backfill": completion_order.count("backfill"),
            }
        ):
            raise ValueError("mixed-backfill completion order is invalid")
        if workload["completion_order_complete"] != (completion_order is not None):
            raise ValueError("mixed-backfill completion order status contradicts evidence")
        if completion_order is None and (
            workload["completion_counts"] == expected_completion_counts
            and sum(workload["completion_counts"].values()) <= MAX_COMPLETION_ORDER
        ):
            raise ValueError("mixed-backfill omitted a bounded completion order")

    configuration = provenance["configuration"]
    if mode == "duplicate-heavy":
        for index, sample in enumerate(samples):
            _validate_duplicate_sample_semantics(
                sample,
                configuration,
                artifact["hard_failures"],
                f"sample {index}",
            )
    elif mode in {"reset-boundary", "correction"}:
        for index, sample in enumerate(samples):
            _validate_relation_subset(
                sample["database"], mode, f"sample {index}"
            )
        _validate_reset_semantics(
            samples, configuration, mode, artifact["hard_failures"]
        )
    elif mode == "mixed-backfill":
        _validate_relation_subset(samples[0]["database"], mode, "mixed sample")
        _validate_mixed_semantics(
            samples[0], configuration, artifact["hard_failures"]
        )
    elif mode == STEP5_MODE:
        army_sample = artifact["army_read_sample"]
        _validate_relation_subset(army_sample["database"], mode, "army sample")
        _validate_army_semantics(
            army_sample,
            provenance,
            artifact["hard_failures"],
            "army sample",
        )


def _query_army_endpoint(
    connection_info: str,
    *,
    population: str = "trophies-5000-9000",
    start_day: int = 23,
    end_day: int = 23,
) -> dict[str, Any]:
    from clashlens.api_db import ApiDatabase
    from clashlens.army_analytics import (
        ArmyAnalyticsSelection,
        ArmyAnalyticsUnavailable,
        CurrentSeasonEmpty,
    )

    selection = ArmyAnalyticsSelection.parse(
        lens="offense",
        season="1783918800",
        start_day=start_day,
        end_day=end_day,
        population=population,
        category="troops",
        sort="usage-rate",
    )
    database = ApiDatabase(connection_info, max_size=1)
    started = time.perf_counter()
    try:
        try:
            result = database.get_army_analytics(
                selection, now=BOUNDARY + timedelta(days=1)
            )
            return {
                "status": "returned" if result is not None else "not-found",
                "returned_fact_count": 0
                if result is None
                else int(result["total_attacks"]),
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        except (ArmyAnalyticsUnavailable, CurrentSeasonEmpty) as error:
            return {
                "status": type(error).__name__,
                "returned_fact_count": 0,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
    finally:
        database.close()


def _seed_step5_army_database(
    connection_info: str, archive: Any
) -> dict[str, int]:
    """Seed the fixed issue #73 PR 2 workload with production-shaped rows."""
    import psycopg
    from domain_test_support import store_observation

    observation_id, job_id = store_observation(
        connection_info,
        archive,
        occurrence_key="step5-army-fixture",
        endpoint="profile",
        body=_profile_body("#F00001"),
        observed_at=DAY_START,
        normalized_tag="#F00001",
    )
    with psycopg.connect(connection_info) as connection:
        # This one profile exists only to satisfy the retained battle-evidence
        # foreign keys; the measured collection cycle owns its own jobs.
        connection.execute(
            "UPDATE python_processing_jobs SET status = 'complete' WHERE id = %s",
            (job_id,),
        )
        connection.commit()
    with psycopg.connect(connection_info) as connection:
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                SELECT '#S' || lpad(value::text, 5, '0'), true, 'eligible'
                FROM generate_series(1, %s) AS values(value)
                ON CONFLICT (normalized_tag) DO UPDATE
                    SET active = true, eligibility_state = 'eligible'
                """,
                (STEP5_POPULATION,),
            )
            # Keep the fixture's clan target eligible so profile processing does
            # not manufacture an unrelated discovery_profile queue entry.
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                VALUES ('#2CLAN', true, 'eligible')
                ON CONFLICT (normalized_tag) DO UPDATE
                    SET active = true, eligibility_state = 'eligible'
                """
            )
            connection.execute(
                """
                CREATE TEMP TABLE perf_step5_players ON COMMIT PRESERVE ROWS AS
                SELECT row_number() OVER (ORDER BY id)::integer AS ordinal, id
                FROM players WHERE normalized_tag LIKE '#S%%'
                """
            )
            connection.execute(
                """
                INSERT INTO legend_battles (ranked_day_start, attacker_player_id,
                                             defender_player_id)
                SELECT %s + (day - 1) * interval '1 day', attacker.id, defender.id
                FROM generate_series(1, %s) AS days(day)
                CROSS JOIN perf_step5_players AS attacker
                CROSS JOIN generate_series(0, %s - 1) AS slots(slot)
                JOIN perf_step5_players AS defender
                  ON defender.ordinal =
                     ((attacker.ordinal - 1 + slots.slot + 1) %% %s) + 1
                """,
                (
                    DAY_START,
                    STEP5_DAYS,
                    STEP5_FACTS_PER_MEMBER_DAY,
                    STEP5_POPULATION,
                ),
            )
            connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end,
                    official_season_id, season_day_number,
                    season_anchor_rule_version, reconciliation_rule_version,
                    result_hash, version, state, confidence,
                    evidence_complete, coverage_complete, reconciled,
                    shield_state, input_hash, parser_version, processing_version
                )
                SELECT player.id, %s + (days.day - 1) * interval '1 day',
                       %s + days.day * interval '1 day', '1783918800', days.day,
                       'step5-anchor-v1', 'step5-reconciliation-v1',
                       md5(player.id::text || ':' || days.day::text) ||
                       md5('result:' || player.id::text || ':' || days.day::text),
                       1, 'Complete', 'exact', true, true, true, 'not_shielded',
                       md5('input:' || player.id::text || ':' || days.day::text) ||
                       md5('input-result:' || player.id::text || ':' || days.day::text),
                       'supercell-source-parser-v2', 'clashlens-domain-processing-v1'
                FROM perf_step5_players AS selected
                JOIN players AS player ON player.id = selected.id
                CROSS JOIN generate_series(1, %s) AS days(day)
                """,
                (DAY_START, DAY_START, STEP5_DAYS),
            )
            connection.execute(
                """
                INSERT INTO army_analytics_completed_days (
                    ranked_day_start, official_season_id, season_day_number,
                    fact_input_hash
                )
                SELECT %s + (days.day - 1) * interval '1 day', '1783918800', days.day,
                       md5('completed:' || days.day::text) ||
                       md5('completed-input:' || days.day::text)
                FROM generate_series(1, %s) AS days(day)
                """,
                (DAY_START, STEP5_DAYS),
            )
            connection.execute(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, version, state, coverage, battles,
                    ranked_day_end, official_season_id, season_day_number, confidence,
                    attack_count, attack_three_star_count, attack_gain,
                    defense_count, defense_three_star_count, defense_loss,
                    net_trophy_change
                )
                SELECT selected.id, %s + (days.day - 1) * interval '1 day', 1,
                       'Complete', 'complete', '[]'::jsonb,
                       %s + days.day * interval '1 day', '1783918800', days.day,
                       'exact', 8, 2, 160, 8, 2, 80, 80
                FROM (SELECT id FROM perf_step5_players WHERE ordinal = 1) AS selected
                CROSS JOIN generate_series(1, %s) AS days(day)
                """,
                (DAY_START, DAY_START, STEP5_DAYS),
            )
            source_battle = connection.execute(
                """
                INSERT INTO legend_battles (
                    ranked_day_start, attacker_player_id, defender_player_id
                )
                SELECT %s - interval '1 day', first_player.id, second_player.id
                FROM (SELECT id FROM perf_step5_players WHERE ordinal = 1) AS first_player
                CROSS JOIN (SELECT id FROM perf_step5_players WHERE ordinal = 2) AS second_player
                RETURNING id
                """,
                (DAY_START,),
            ).fetchone()
            if source_battle is None:
                raise RuntimeError("failed to seed army evidence battle")
            battle_log = connection.execute(
                """
                INSERT INTO battle_log_observations (
                    observation_id, player_id, parser_version, observed_at,
                    row_count, has_row_gap
                )
                SELECT %s, id, 'supercell-source-parser-v2', %s, 1, false
                FROM players WHERE normalized_tag = '#F00001'
                RETURNING id
                """,
                (observation_id, DAY_START),
            ).fetchone()
            if battle_log is None:
                raise RuntimeError("failed to seed army evidence observation")
            source_row = connection.execute(
                """
                INSERT INTO battle_source_rows (
                    battle_log_observation_id, source_row_index, outcome, source_json
                ) VALUES (%s, 0, 'valid_legend', '{}'::jsonb)
                RETURNING id
                """,
                (battle_log[0],),
            ).fetchone()
            if source_row is None:
                raise RuntimeError("failed to seed army evidence source row")
            evidence = connection.execute(
                """
                INSERT INTO battle_evidence (
                    battle_id, source_row_id, observation_id, reporting_player_id,
                    perspective, battle_timestamp, stars, destruction_percentage,
                    army_share_code, reporter_trophies, opponent_trophies,
                    attacker_gain, defender_loss, trophy_rule_version,
                    source_observed_at, parser_version
                )
                SELECT %s, %s, %s, player.id, 'attacker', %s, 2, 80,
                       'step5-fixture', 6000, 6000, 20, 20,
                       'step5-trophy-v1', %s, 'supercell-source-parser-v2'
                FROM players AS player WHERE player.normalized_tag = '#F00001'
                RETURNING id
                """,
                (
                    source_battle[0],
                    source_row[0],
                    observation_id,
                    DAY_START,
                    DAY_START,
                ),
            ).fetchone()
            if evidence is None:
                raise RuntimeError("failed to seed army evidence")
            connection.execute(
                f"""
                WITH synthetic AS (
                    SELECT battle.id, days.day, attacker.ordinal, slots.slot,
                           %s + (days.day - 1) * interval '1 day' AS day_start
                    FROM generate_series(1, %s) AS days(day)
                    CROSS JOIN perf_step5_players AS attacker
                    CROSS JOIN generate_series(0, %s - 1) AS slots(slot)
                    JOIN perf_step5_players AS defender
                      ON defender.ordinal =
                         ((attacker.ordinal - 1 + slots.slot + 1) %% %s) + 1
                    JOIN legend_battles AS battle
                      ON battle.ranked_day_start =
                             %s + (days.day - 1) * interval '1 day'
                     AND battle.attacker_player_id = attacker.id
                     AND battle.defender_player_id = defender.id
                ), day_versions AS (
                    SELECT season_day_number, min(id) AS id
                    FROM ranked_day_versions
                    GROUP BY season_day_number
                )
                INSERT INTO army_analytics_battle_facts (
                    battle_id, evidence_id, source_ranked_day_version_id,
                    ranked_day_start, official_season_id, season_day_number, lens,
                    population_player_id, battle_time_trophies, stars,
                    destruction_percentage, army_state, home_troops,
                    perspective_disagreement, input_hash, version, is_current
                )
                SELECT synthetic.id, %s, day_versions.id, synthetic.day_start,
                       '1783918800', synthetic.day, lens.value,
                       CASE WHEN lens.value = 'offense' THEN attacker.id ELSE defender.id END,
                       CASE WHEN ((synthetic.day - 1) * {STEP5_POPULATION} * {STEP5_FACTS_PER_MEMBER_DAY} +
                                       (synthetic.ordinal - 1) * {STEP5_FACTS_PER_MEMBER_DAY} + synthetic.slot)
                                      %% {STEP5_MISSING_TROPHY_RATE} = 0
                            THEN NULL
                            ELSE 6000 + (((synthetic.ordinal + synthetic.slot) %% 4000))
                       END,
                       2, 80, 'decoded',
                       jsonb_build_array(jsonb_build_array(
                           'troop:' || (((synthetic.day - 1) * {STEP5_POPULATION} * {STEP5_FACTS_PER_MEMBER_DAY} +
                                         (synthetic.ordinal - 1) * {STEP5_FACTS_PER_MEMBER_DAY} + synthetic.slot)
                                        %% 27)::text, 1)),
                       false,
                       md5(synthetic.id::text || lens.value) ||
                       md5('step5:' || synthetic.id::text || lens.value),
                       1, true
                FROM synthetic
                JOIN day_versions ON day_versions.season_day_number = synthetic.day
                JOIN legend_battles AS battle ON battle.id = synthetic.id
                JOIN players AS attacker ON attacker.id = battle.attacker_player_id
                JOIN players AS defender ON defender.id = battle.defender_player_id
                CROSS JOIN (VALUES ('offense'), ('defense')) AS lens(value)
                """,
                (
                    DAY_START,
                    STEP5_DAYS,
                    STEP5_FACTS_PER_MEMBER_DAY,
                    STEP5_POPULATION,
                    DAY_START,
                    evidence[0],
                ),
            )
            connection.execute(
                """
                INSERT INTO leaderboard_snapshots (
                    snapshot_kind, boundary_at, version, ordering_rule_version,
                    freshness_rule_version, state, source_ranked_day_version_id,
                    measured_coverage, stale_entry_count, published_at, input_hash,
                    eligible_population_count, included_entry_count, fresh_entry_count,
                    excluded_missing_count, excluded_invalid_count,
                    excluded_malformed_count, excluded_conflicting_count
                )
                SELECT 'frozen', %s + days.day * interval '1 day', 1,
                       'step5-order-v1', 'step5-freshness-v1', 'published',
                       day_versions.id, 1, 0, clock_timestamp(),
                       md5('snapshot:' || days.day::text) ||
                       md5('snapshot-input:' || days.day::text),
                       %s, %s, %s, 0, 0, 0, 0
                FROM generate_series(1, %s) AS days(day)
                JOIN (SELECT season_day_number, min(id) AS id
                      FROM ranked_day_versions GROUP BY season_day_number) AS day_versions
                  ON day_versions.season_day_number = days.day
                """,
                (
                    DAY_START,
                    STEP5_POPULATION,
                    STEP5_SELECTED_MEMBERS,
                    STEP5_SELECTED_MEMBERS,
                    STEP5_DAYS,
                ),
            )
            connection.execute(
                """
                CREATE TEMP TABLE perf_step5_snapshots ON COMMIT PRESERVE ROWS AS
                SELECT id, row_number() OVER (ORDER BY boundary_at)::integer AS day
                FROM leaderboard_snapshots
                WHERE snapshot_kind = 'frozen' AND state = 'published'
                  AND boundary_at BETWEEN %s + interval '1 day' AND
                                         %s + %s * interval '1 day'
                """,
                (DAY_START, DAY_START, STEP5_DAYS),
            )
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, trophies, trophy_observation_id,
                    trophy_observed_at, observation_age_seconds, freshness, confidence,
                    tie_hash
                )
                SELECT snapshot.id, player.ordinal, player.id,
                       10000 - player.ordinal, %s, %s, 0, 'fresh', 'confirmed',
                       md5(player.id::text) || md5('step5-tie:' || player.id::text)
                FROM perf_step5_snapshots AS snapshot
                CROSS JOIN perf_step5_players AS player
                WHERE player.ordinal <= %s
                """,
                (observation_id, DAY_START, STEP5_SELECTED_MEMBERS),
            )
        with connection.transaction():
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM perf_step5_players),
                    (SELECT count(*) FROM ranked_day_versions
                     WHERE official_season_id = '1783918800'),
                    (SELECT count(*) FROM army_analytics_battle_facts
                     WHERE official_season_id = '1783918800' AND lens = 'offense'
                       AND is_current),
                    (SELECT count(*) FROM army_analytics_battle_facts
                     WHERE official_season_id = '1783918800' AND lens = 'defense'
                       AND is_current),
                    (SELECT count(*) FROM army_analytics_battle_facts
                     WHERE official_season_id = '1783918800' AND lens = 'offense'
                       AND is_current AND battle_time_trophies IS NULL),
                    (SELECT count(*) FROM army_analytics_battle_facts
                     WHERE official_season_id = '1783918800' AND lens = 'defense'
                       AND is_current AND battle_time_trophies IS NULL),
                    (SELECT count(*) FROM leaderboard_snapshots
                     WHERE snapshot_kind = 'frozen' AND state = 'published'),
                    (SELECT count(*) FROM leaderboard_snapshot_entries),
                    (SELECT count(*) FROM army_analytics_completed_days
                     WHERE official_season_id = '1783918800')
                """
            ).fetchone()
            assert counts is not None
            selected = connection.execute(
                """
                SELECT count(*)
                FROM army_analytics_battle_facts AS fact
                WHERE fact.official_season_id = '1783918800'
                  AND fact.is_current AND fact.lens = 'offense'
                  AND fact.population_player_id IN (
                      SELECT player_id FROM leaderboard_snapshot_entries
                      WHERE snapshot_id = (SELECT id FROM perf_step5_snapshots WHERE day = 1)
                  )
                """
            ).fetchone()[0]
            troop_keys = connection.execute(
                """
                SELECT count(DISTINCT fact.home_troops -> 0 ->> 0)
                FROM army_analytics_battle_facts AS fact
                WHERE fact.official_season_id = '1783918800'
                  AND fact.is_current AND fact.lens = 'offense'
                  AND fact.population_player_id IN (
                      SELECT player_id FROM leaderboard_snapshot_entries
                      WHERE snapshot_id = (SELECT id FROM perf_step5_snapshots WHERE day = 1)
                  )
                """
            ).fetchone()[0]
            expected_facts = STEP5_POPULATION * STEP5_DAYS * STEP5_FACTS_PER_MEMBER_DAY
            if tuple(map(int, counts)) != (
                STEP5_POPULATION,
                STEP5_POPULATION * STEP5_DAYS,
                expected_facts,
                expected_facts,
                expected_facts // STEP5_MISSING_TROPHY_RATE,
                expected_facts // STEP5_MISSING_TROPHY_RATE,
                STEP5_DAYS,
                STEP5_DAYS * STEP5_SELECTED_MEMBERS,
                STEP5_DAYS,
            ):
                raise RuntimeError(f"step5 seed cardinality mismatch: {counts}")
            if int(selected) != STEP5_SELECTED_MEMBERS * STEP5_DAYS * STEP5_FACTS_PER_MEMBER_DAY:
                raise RuntimeError(f"step5 selected cardinality mismatch: {selected}")
            if int(troop_keys) != len(STEP5_TROOP_KEYS):
                raise RuntimeError(f"step5 troop-key cardinality mismatch: {troop_keys}")
            return {
                "population": int(counts[0]),
                "days": STEP5_DAYS,
                "facts_per_lens": int(counts[2]),
                "missing_trophies_per_lens": int(counts[4]),
                "snapshots": int(counts[6]),
                "snapshot_entries": int(counts[7]),
                "completed_days": int(counts[8]),
                "selected_facts_per_lens": int(selected),
                "troop_keys": int(troop_keys),
            }


def _step5_active_analyzes(
    connection_info: str, deadline: float
) -> int | None:
    import psycopg

    remaining = deadline - time.monotonic()
    if remaining < 1:
        return None
    with psycopg.connect(
        connection_info, connect_timeout=max(1, int(remaining))
    ) as connection:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return None
        with connection.transaction():
            connection.execute(
                f"SET LOCAL statement_timeout = '{remaining_ms}ms'"
            )
            row = connection.execute(
                """
                SELECT least(count(*), 64)::integer
                FROM pg_stat_progress_analyze AS progress
                WHERE progress.datid = (
                    SELECT oid FROM pg_database WHERE datname = current_database()
                )
                  AND progress.relid IN (
                      SELECT oid
                      FROM pg_class
                      WHERE relnamespace = current_schema()::regnamespace
                        AND relname = ANY(%s::text[])
                  )
                """,
                (list(STEP5_STATISTICS_RELATIONS),),
            ).fetchone()
    if row is None:
        raise RuntimeError("step5 statistics readiness could not be read")
    return int(row[0])


def _prepare_step5_statistics(
    connection_info: str,
) -> tuple[dict[str, Any], str | None]:
    """Analyze the six read-path relations once before any timed request."""
    import psycopg
    from psycopg import sql

    deadline = time.monotonic() + STEP5_STATISTICS_TIMEOUT_SECONDS

    def result(completed: bool | None, active: int | None) -> dict[str, Any]:
        return {
            "relations": list(STEP5_STATISTICS_RELATIONS),
            "readiness_timeout_seconds": STEP5_STATISTICS_TIMEOUT_SECONDS,
            "analyze_completed": completed,
            "active_analyzes": active,
            "ready": completed is True and active == 0,
        }

    completed = False
    try:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            return result(completed, None), "statistics_timeout"
        with psycopg.connect(
            connection_info, connect_timeout=max(1, int(remaining))
        ) as connection:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return result(completed, None), "statistics_timeout"
            with connection.transaction():
                connection.execute(
                    f"SET LOCAL statement_timeout = '{remaining_ms}ms'"
                )
                connection.execute(
                    sql.SQL("ANALYZE {}").format(
                        sql.SQL(", ").join(
                            sql.Identifier(name)
                            for name in STEP5_STATISTICS_RELATIONS
                        )
                    )
                )
        completed = True
    except (psycopg.errors.QueryCanceled, psycopg.errors.TransactionTimeout):
        completed = False
    except Exception:  # noqa: BLE001 - retain only explicit unavailable facts.
        if time.monotonic() >= deadline:
            return result(completed, None), "statistics_timeout"
        return result(None, None), "statistics_unavailable"

    try:
        active = _step5_active_analyzes(connection_info, deadline)
    except (psycopg.errors.QueryCanceled, psycopg.errors.TransactionTimeout):
        return result(completed, None), "statistics_timeout"
    except Exception:  # noqa: BLE001 - retain only explicit unavailable facts.
        if time.monotonic() >= deadline:
            return result(completed, None), "statistics_timeout"
        return result(completed, None), "statistics_unavailable"
    if active is None or not completed:
        return result(completed, active), "statistics_timeout"
    if active:
        return result(completed, active), "statistics_not_ready"
    return result(completed, active), None


def _step5_selection(spec: dict[str, Any]) -> Any:
    from clashlens.army_analytics import ArmyAnalyticsSelection

    return ArmyAnalyticsSelection.parse(
        lens=spec["lens"],
        season="1783918800",
        start_day=1,
        end_day=STEP5_DAYS,
        population=spec["selection"],
        category="troops",
        sort="usage-rate",
    )


def _step5_result(result: dict[str, Any] | None, spec: dict[str, Any], phase: str) -> tuple[str, ...]:
    if result is None or int(result["total_attacks"]) != spec["expected_facts"]:
        raise RuntimeError(f"army endpoint {phase} cardinality mismatch for {spec}")
    keys = tuple(sorted(str(row["key"]) for row in result["rows"]))
    expected = tuple(sorted(STEP5_TROOP_KEYS))
    if keys != expected:
        raise RuntimeError(
            f"army endpoint {phase} troop keys mismatch for {spec}: {keys} != {expected}"
        )
    return keys


def _explain_endpoint_statements(
    connection_info: str,
    calls: list[dict[str, Any]],
    *,
    selection: str,
    lens: str,
) -> list[dict[str, Any]]:
    import psycopg

    from clashlens.api_db import ARMY_ANALYTICS_QUERY_WORK_MEM

    plans: list[dict[str, Any]] = []
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            f"SET LOCAL work_mem = '{ARMY_ANALYTICS_QUERY_WORK_MEM}'"
        )
        for statement_id, call in enumerate(calls, 1):
            sql = str(call["sql"]).strip().rstrip(";")
            try:
                payload = connection.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql,
                    call["params"],
                ).fetchone()[0]
            except psycopg.Error as error:
                connection.rollback()
                raise RuntimeError(
                    f"endpoint statement {selection}/{lens}/{statement_id} could not be explained"
                ) from error
            public_payload = _public_explain_payload(payload)
            plan = public_payload[0]["Plan"]
            scanned, returned = _plan_counts(plan)
            identity = _army_query_identity(sql)
            shape = _army_parameter_shape(call["params"])
            plans.append(
                {
                    "correlation": {
                        "selection": selection,
                        "lens": lens,
                        "statement_id": statement_id,
                    },
                    "sql": identity,
                    "parameters": {"arity": len(shape), "types": list(shape)},
                    "rows_scanned": scanned,
                    "rows_returned": returned,
                    "explain_analyze_buffers": public_payload,
                }
            )
    return plans


def _measure_army_pair(
    connection_info: str,
    spec: dict[str, Any],
    *,
    warmups: int = STEP5_WARMUPS,
    requests: int = STEP5_REQUESTS,
) -> dict[str, Any]:
    from clashlens.api_db import ApiDatabase

    if warmups != STEP5_WARMUPS or requests != STEP5_REQUESTS:
        raise ValueError("issue #73 army measurements require five warmups and 100 requests")
    selection = _step5_selection(spec)
    database = ApiDatabase(connection_info, max_size=2)
    try:
        # The forced miss is also the production query capture used by the
        # untimed EXPLAIN diagnostic pass; it is excluded from warmups/p95.
        pressure_before = _memory_pressure(connection_info)
        forced_started = time.perf_counter()
        with capture_sql_calls() as calls:
            first = database.get_army_analytics(
                selection, now=BOUNDARY + timedelta(days=STEP5_DAYS + 1)
            )
        forced_miss_seconds = time.perf_counter() - forced_started
        pressure_after = _memory_pressure(connection_info)
        pressure_delta = _memory_pressure_delta(
            pressure_before, pressure_after
        )
        troop_keys = _step5_result(first, spec, "forced miss")
        for _ in range(warmups):
            result = database.get_army_analytics(
                selection, now=BOUNDARY + timedelta(days=STEP5_DAYS + 1)
            )
            _step5_result(result, spec, "warmup")
        if len(calls) == 0:
            raise RuntimeError(f"army endpoint emitted no SQL for {spec}")
        diagnostics = _explain_endpoint_statements(
            connection_info,
            calls,
            selection=spec["selection"],
            lens=spec["lens"],
        )
        latencies: list[float] = []
        for _ in range(requests):
            started = time.perf_counter()
            result = database.get_army_analytics(
                selection, now=BOUNDARY + timedelta(days=STEP5_DAYS + 1)
            )
            latencies.append((time.perf_counter() - started) * 1000)
            _step5_result(result, spec, "measurement")
        return {
            "selection": spec["selection"],
            "lens": spec["lens"],
            "warmups": warmups,
            "requests": requests,
            "forced_miss_seconds": forced_miss_seconds,
            "forced_miss_target_seconds": STEP5_FORCED_MISS_TARGET_SECONDS,
            "forced_miss_passed": _army_forced_miss_passed(
                forced_miss_seconds,
                pressure_before,
                pressure_after,
                pressure_delta,
            ),
            "forced_miss_memory_before": pressure_before,
            "forced_miss_memory_after": pressure_after,
            "forced_miss_memory_delta": pressure_delta,
            "p95_ms": _p95(latencies),
            "min_ms": min(latencies),
            "max_ms": max(latencies),
            "latencies_ms": latencies,
            "selected_fact_count": int(first["total_attacks"]),
            "expected_fact_count": spec["expected_facts"],
            "troop_keys": list(troop_keys),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "endpoint_sql": diagnostics,
            "target_ms": STEP5_P95_TARGET_MS,
            "target_passed": _p95(latencies) < STEP5_P95_TARGET_MS,
        }
    finally:
        database.close()


def _run_account_read_gate(
    connection_info: str,
    *,
    warmups: int = STEP5_WARMUPS,
    requests: int = STEP5_REQUESTS,
    processing_started: Event | None = None,
    cycle_finished: Event | None = None,
    cycle_failed: Event | None = None,
    overlap_counts: list[int] | None = None,
    overlap_lock: Lock | None = None,
) -> dict[str, Any]:
    from fastapi.testclient import TestClient
    from test_private_api import (
        DISCORD_CURRENT,
        NOW,
        NOW_SECONDS,
        TS_CURRENT,
        TS_PREVIOUS,
        json_body,
        signed_headers,
    )

    from clashlens.api import create_app
    from clashlens.api_db import ApiDatabase

    database = ApiDatabase(connection_info, max_size=1)
    keys = {
        ("typescript-website", "current"): TS_CURRENT,
        ("typescript-website", "previous"): TS_PREVIOUS,
        ("discord-bot", "current"): DISCORD_CURRENT,
    }
    target = "/v1/account"
    subject = "step5-performance-account"
    body = json_body({"username": "step5performance", "display_name": "Step 5"})
    try:
        app = create_app(
            database,
            keys=keys,
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        with TestClient(app) as client:
            created = client.post(
                target,
                content=body,
                headers=signed_headers(
                    target,
                    method="POST",
                    body=body,
                    provider="google",
                    subject=subject,
                ),
            )
            if created.status_code != 201:
                raise RuntimeError(f"account warmup creation failed: {created.status_code}")
            if processing_started is not None:
                processing_started.wait()
            if cycle_failed is not None and cycle_failed.is_set():
                raise RuntimeError("collection cycle failed before processing started")
            for _ in range(warmups):
                response = client.get(
                    target,
                    headers=signed_headers(target, provider="google", subject=subject),
                )
                if response.status_code != 200:
                    raise RuntimeError(f"account warmup failed: {response.status_code}")
            latencies: list[float] = []
            overlap_measurements = 0
            for _ in range(requests):
                started = time.perf_counter()
                response = client.get(
                    target,
                    headers=signed_headers(target, provider="google", subject=subject),
                )
                elapsed = (time.perf_counter() - started) * 1000
                latencies.append(elapsed)
                if response.status_code != 200:
                    raise RuntimeError(f"account measurement failed: {response.status_code}")
                if (
                    processing_started is not None
                    and cycle_finished is not None
                    and processing_started.is_set()
                    and not cycle_finished.is_set()
                ):
                    overlap_measurements += 1
            if overlap_counts is not None:
                assert overlap_lock is not None
                with overlap_lock:
                    overlap_counts[0] = overlap_measurements
            return {
                "warmups": warmups,
                "requests": requests,
                "p95_ms": _p95(latencies),
                "min_ms": min(latencies),
                "max_ms": max(latencies),
                "latencies_ms": latencies,
                "target_ms": STEP5_P95_TARGET_MS,
                "target_passed": _p95(latencies) < STEP5_P95_TARGET_MS,
                "overlap_measurements": overlap_measurements,
            }
    finally:
        database.close()


def _run_step5_overlap(
    connection_info: str,
    archive: Any,
    specs: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Run the retained 25,024-observation cycle beside four API lanes."""
    processing_started = Event()
    cycle_finished = Event()
    cycle_failed = Event()
    analytics_warmups_resolved = Event()
    analytics_warmups_failed = Event()
    analytics_warmup_lock = Lock()
    overlap_lock = Lock()
    warmed_lanes = [0]
    overlap_counts: dict[str, int] = {f"{s['selection']}/{s['lens']}": 0 for s in specs}
    account_overlap = [0]

    def duplicate_cycle() -> dict[str, Any]:
        try:
            if not analytics_warmups_resolved.wait(120):
                raise RuntimeError("analytics warmups did not finish")
            if analytics_warmups_failed.is_set():
                raise RuntimeError("analytics warmup failed")
            started = time.perf_counter()
            overlap_day = BOUNDARY + timedelta(days=STEP5_DAYS + 1)
            result = _run_duplicate(
                connection_info,
                archive,
                DUPLICATE_EXECUTION_CAP,
                cycles=1,
                # The Step 5 seed owns the fixed season-day rows around
                # DAY_START. Keep overlap fixtures in a later, unseeded
                # ranked day so their decoded battle observations cannot
                # enqueue an unrelated legacy population-wide army job.
                observation_start=overlap_day,
                battle_fixture=_battle_fixture_for_day(overlap_day),
                processing_started=processing_started,
            )
            result.pop("_spool_root", None)
            result["cycle_elapsed_seconds"] = time.perf_counter() - started
            return result
        except Exception:
            cycle_failed.set()
            processing_started.set()
            raise
        finally:
            cycle_finished.set()

    def analytics_lane(lane_specs: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        from clashlens.api_db import ApiDatabase

        database = ApiDatabase(
            connection_info, max_size=STEP5_MIXED_LANE_POOL_MAX_SIZE
        )
        results: list[dict[str, Any]] = []
        try:
            selections = [(spec, _step5_selection(spec)) for spec in lane_specs]
            try:
                for spec, selection in selections:
                    with analytics_warmup_lock:
                        if analytics_warmups_failed.is_set():
                            raise RuntimeError("analytics warmup failed")
                        for _ in range(STEP5_WARMUPS):
                            result = database.get_army_analytics(
                                selection,
                                now=BOUNDARY + timedelta(days=STEP5_DAYS + 1),
                            )
                            _step5_result(result, spec, "mixed warmup")
            except Exception:
                analytics_warmups_failed.set()
                analytics_warmups_resolved.set()
                raise
            else:
                with overlap_lock:
                    warmed_lanes[0] += 1
                    if warmed_lanes[0] == STEP5_ANALYTICS_LANES:
                        analytics_warmups_resolved.set()
            processing_started.wait()
            if cycle_failed.is_set():
                raise RuntimeError("collection cycle failed before processing started")
            latencies: dict[str, list[float]] = {
                f"{spec['selection']}/{spec['lens']}": []
                for spec, _selection in selections
            }
            overlaps: dict[str, int] = dict.fromkeys(latencies, 0)
            last_results: dict[str, dict[str, Any]] = {}
            # Alternate paired selections so each pair is measured while the
            # exact collection-processing cycle remains active.
            for _ in range(STEP5_REQUESTS):
                for spec, selection in selections:
                    key = f"{spec['selection']}/{spec['lens']}"
                    started = time.perf_counter()
                    result = database.get_army_analytics(
                        selection, now=BOUNDARY + timedelta(days=STEP5_DAYS + 1)
                    )
                    elapsed = (time.perf_counter() - started) * 1000
                    _step5_result(result, spec, "mixed measurement")
                    assert result is not None
                    latencies[key].append(elapsed)
                    last_results[key] = result
                    if not cycle_finished.is_set():
                        overlaps[key] += 1
            for spec, _selection in selections:
                key = f"{spec['selection']}/{spec['lens']}"
                p95 = _p95(latencies[key])
                with overlap_lock:
                    overlap_counts[key] += overlaps[key]
                results.append(
                    {
                        "selection": spec["selection"],
                        "lens": spec["lens"],
                        "warmups": STEP5_WARMUPS,
                        "requests": STEP5_REQUESTS,
                        "p95_ms": p95,
                        "selected_fact_count": int(last_results[key]["total_attacks"]),
                        "troop_keys": list(
                            _step5_result(last_results[key], spec, "mixed result")
                        ),
                        "overlap_measurements": overlaps[key],
                        "target_ms": STEP5_P95_TARGET_MS,
                        "target_passed": p95 < STEP5_P95_TARGET_MS,
                    }
                )
            return results
        finally:
            database.close()

    lane_specs = (
        (specs[0], specs[4]),
        (specs[1], specs[5]),
        (specs[2],),
        (specs[3],),
    )
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="clashlens-step5") as executor:
        cycle_future = executor.submit(duplicate_cycle)
        lane_futures = [executor.submit(analytics_lane, lane) for lane in lane_specs]
        account_future = executor.submit(
            _run_account_read_gate,
            connection_info,
            processing_started=processing_started,
            cycle_finished=cycle_finished,
            cycle_failed=cycle_failed,
            overlap_counts=account_overlap,
            overlap_lock=overlap_lock,
        )
        cycle = cycle_future.result()
        lanes = [future.result() for future in lane_futures]
        account = account_future.result()
    measurements = {
        "analytics_lanes": [item for lane in lanes for item in lane],
        "account": account,
        "overlap_counts": overlap_counts,
        "account_overlap_measurements": account_overlap[0],
        "collection_cycle": cycle,
    }
    hard_failures = []
    if any(count < STEP5_REQUESTS for count in overlap_counts.values()):
        hard_failures.append("step5_overlap_incomplete")
    cycle_summary = cycle.get("processing_summary", {})
    cycle_count = int(cycle_summary.get("count", 0))
    if cycle_count != DUPLICATE_EXECUTION_CAP:
        hard_failures.append("step5_collection_result_count_mismatch")
    if int(cycle_summary.get("outcomes", {}).get("processed", 0)) != cycle_count:
        hard_failures.append("step5_non_processed_result")
    if any(not item["target_passed"] for item in measurements["analytics_lanes"]):
        hard_failures.append("step5_p95_exceeded")
    if account["overlap_measurements"] < STEP5_REQUESTS:
        hard_failures.append("step5_account_overlap_incomplete")
    if not account["target_passed"]:
        hard_failures.append("step5_account_p95_exceeded")
    if cycle["cycle_elapsed_seconds"] >= STEP5_COLLECTION_LIMIT_SECONDS:
        hard_failures.append("step5_collection_cycle_too_slow")
    measurements["hard_failures"] = _failure_codes(hard_failures)
    return measurements


def _seed_worst_case_army_reads(
    connection_info: str, fact_count: int
) -> list[dict[str, Any]]:
    """Bulk-load bounded synthetic facts around production-created FK evidence."""
    import psycopg

    with psycopg.connect(connection_info) as connection:
        base = connection.execute(
            """SELECT (SELECT id FROM battle_evidence ORDER BY id LIMIT 1),
                      (SELECT id FROM ranked_day_versions ORDER BY id LIMIT 1),
                      (SELECT id FROM collector_observations ORDER BY id LIMIT 1)"""
        ).fetchone()
        if base is None or any(value is None for value in base):
            raise RuntimeError(
                "army read workload requires processed battle and reconciliation evidence"
            )
        evidence_id, version_id, observation_id = map(int, base)
        connection.execute(
            """INSERT INTO players (normalized_tag, active)
               SELECT '#Q' || lpad(g::text, 6, '0'), false FROM generate_series(1,1100) g
               ON CONFLICT (normalized_tag) DO NOTHING"""
        )
        population_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM players WHERE normalized_tag LIKE '#Q%' ORDER BY normalized_tag LIMIT 1000"
            ).fetchall()
        ]
        opponent_ids = [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM players WHERE normalized_tag LIKE '#Q%' ORDER BY normalized_tag OFFSET 1000 LIMIT 100"
            ).fetchall()
        ]
        connection.execute(
            "CREATE TEMP TABLE perf_facts (day int, player_id bigint, opponent_id bigint) ON COMMIT DROP"
        )
        connection.execute(
            """INSERT INTO perf_facts
               SELECT (g - 1) %% 28 + 1,
                      (%s::bigint[])[(g - 1) / 28 %% 1000 + 1],
                      (%s::bigint[])[(g - 1) / 28000 %% 100 + 1]
               FROM generate_series(1, %s) g""",
            (population_ids, opponent_ids, fact_count),
        )
        connection.execute(
            """INSERT INTO legend_battles (ranked_day_start, attacker_player_id, defender_player_id)
               SELECT DISTINCT %s + (day - 23) * interval '1 day', player_id, opponent_id
               FROM perf_facts ON CONFLICT DO NOTHING""",
            (DAY_START,),
        )
        connection.execute(
            """INSERT INTO army_analytics_battle_facts (
                   battle_id, evidence_id, source_ranked_day_version_id, ranked_day_start,
                   official_season_id, season_day_number, lens, population_player_id,
                   battle_time_trophies, stars, destruction_percentage, army_state,
                   home_troops, perspective_disagreement, input_hash, version)
               SELECT battle.id, %s, %s, battle.ranked_day_start, '1783918800', f.day,
                      'offense', f.player_id, 5000 + f.player_id %% 4001,
                      f.player_id %% 4, f.player_id %% 101, 'decoded',
                      '[["1000000",1],["1000001",2],["1000002",3]]'::jsonb,
                      false, md5(battle.id::text) || md5(battle.id::text), 1
               FROM perf_facts f JOIN legend_battles battle
                 ON battle.ranked_day_start=%s + (f.day - 23) * interval '1 day'
                AND battle.attacker_player_id=f.player_id AND battle.defender_player_id=f.opponent_id
               ON CONFLICT (battle_id, lens, version) DO NOTHING""",
            (evidence_id, version_id, DAY_START),
        )
        for day in range(1, 29):
            day_start = DAY_START + timedelta(days=day - 23)
            connection.execute(
                """INSERT INTO api_player_daily_logs (
                       player_id, ranked_day_start, version, state, coverage, battles,
                       ranked_day_end, official_season_id, season_day_number)
                   VALUES (%s,%s,1,'Complete','complete','[]',%s,'1783918800',%s)
                   ON CONFLICT DO NOTHING""",
                (population_ids[0], day_start, day_start + timedelta(days=1), day),
            )
            connection.execute(
                """INSERT INTO army_analytics_completed_days
                       (ranked_day_start, official_season_id, season_day_number, fact_input_hash)
                   VALUES (%s,'1783918800',%s,%s) ON CONFLICT DO NOTHING""",
                (day_start, day, "f" * 64),
            )
            snapshot_id = connection.execute(
                """INSERT INTO leaderboard_snapshots (
                       snapshot_kind,boundary_at,version,ordering_rule_version,
                       freshness_rule_version,state,measured_coverage,stale_entry_count)
                   VALUES ('frozen',%s,99,'perf-order','perf-fresh','published',1,0)
                   ON CONFLICT DO NOTHING RETURNING id""",
                (day_start + timedelta(days=1),),
            ).fetchone()
            if snapshot_id is None:
                snapshot_id = connection.execute(
                    "SELECT id FROM leaderboard_snapshots WHERE snapshot_kind='frozen' AND boundary_at=%s AND state='published'",
                    (day_start + timedelta(days=1),),
                ).fetchone()
            connection.execute(
                """INSERT INTO leaderboard_snapshot_entries (
                       snapshot_id,position,player_id,trophies,trophy_observation_id,
                       trophy_observed_at,observation_age_seconds,freshness,confidence,tie_hash)
                   SELECT %s, position, player_id, 9001-position, %s, %s, 0,
                          'fresh','confirmed',md5(player_id::text)||md5(player_id::text)
                   FROM unnest(%s::bigint[]) WITH ORDINALITY AS p(player_id,position)
                   ON CONFLICT DO NOTHING""",
                (int(snapshot_id[0]), observation_id, day_start, population_ids),
            )
        connection.commit()

    selections = ("top-1000", "trophies-5000-9999", "streak-top-1000")
    results = []
    for population in selections:
        with psycopg.connect(connection_info) as connection:
            raw_payload = connection.execute(
                """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                   SELECT id,battle_id,population_player_id,battle_time_trophies,stars,
                          destruction_percentage,army_state,failure_reason,home_troops,
                          spells,siege,cc_troops,heroes,unresolved_components,
                          perspective_disagreement,input_hash,source_ranked_day_version_id
                   FROM army_analytics_battle_facts
                   WHERE official_season_id='1783918800'
                     AND season_day_number BETWEEN 1 AND 28 AND lens='offense' AND is_current
                     ORDER BY battle_id"""
            ).fetchone()[0]
        plan_row = _public_explain_payload(raw_payload)[0]
        scanned, returned = _plan_counts(plan_row["Plan"])
        endpoint = _query_army_endpoint(
            connection_info, population=population, start_day=1, end_day=28
        )
        results.append(
            {
                "selection": population,
                "synthetic_fact_limit": fact_count,
                "rows_scanned": scanned,
                "rows_returned": returned,
                "latency_ms": endpoint["latency_ms"],
                "endpoint": endpoint,
                "explain_analyze_buffers": plan_row,
            }
        )
    return results


def _run_coordinator_writers(
    connection_info: str, archive: Any, population: int = 12_500
) -> dict[str, Any]:
    """Run the real coordinator jobs for the target population."""
    import psycopg
    from psycopg.types.json import Jsonb

    if population != 12_500:
        raise ValueError("coordinator-12500 requires population 12,500")
    day_start = BOUNDARY - timedelta(days=1)
    with psycopg.connect(connection_info) as connection, connection.transaction():
        connection.execute(
            """
            INSERT INTO players (normalized_tag, active)
            SELECT '#C' || translate(
                lpad(value::text, 5, '0'), '0123456789', '0289PYLQGR'
            ), true
            FROM generate_series(1, %s) AS values(value)
            """,
            (population,),
        )
        connection.execute(
            """
            INSERT INTO archive_instances
                (instance_id, endpoint, region, bucket, marker_key,
                 marker_hash, marker_payload_version)
            VALUES ('coordinator-writer', 'archive.test', 'test', 'evidence',
                    'marker', repeat('c', 64), 'v1')
            """
        )
        connection.execute(
            """
            WITH jobs AS (
                INSERT INTO collector_jobs
                    (work_type, player_id, normalized_tag, capacity_pool,
                     priority, due_at, coalescing_key, status)
                SELECT 'initial_collection', id, normalized_tag, 'normal', 1,
                       clock_timestamp(), 'coordinator-writer:' || id, 'complete'
                FROM players WHERE normalized_tag LIKE '#C%%'
                RETURNING id, player_id, normalized_tag
            ), attempts AS (
                INSERT INTO collector_attempts
                    (job_id, status, started_at, completed_at)
                SELECT id, 'complete', clock_timestamp(), clock_timestamp()
                FROM jobs RETURNING id, job_id
            ), source AS (
                SELECT jobs.player_id, jobs.normalized_tag, attempts.id AS attempt_id,
                       repeat(md5(jobs.player_id::text), 2) AS response_hash
                FROM jobs JOIN attempts ON attempts.job_id = jobs.id
            ), catalogued AS (
                INSERT INTO archive_catalogue
                    (response_hash, archive_reference, byte_size, archive_instance_id)
                SELECT response_hash,
                       's3://evidence/coordinator/' || response_hash,
                       2, 'coordinator-writer'
                FROM source RETURNING response_hash
            ), observations AS (
                INSERT INTO collector_observations (
                    occurrence_key, collection_job_id, attempt_id, player_id,
                    scope, normalized_tag, endpoint, request_started_at,
                    response_completed_at, http_status, response_hash,
                    archive_reference, archive_catalogue_hash, collector_version,
                    key_label, evidence_headers
                )
                SELECT 'coordinator-writer:' || source.player_id,
                       jobs.id, source.attempt_id, source.player_id, 'player',
                       source.normalized_tag, 'profile', clock_timestamp(),
                       clock_timestamp(), 200, source.response_hash,
                       's3://evidence/coordinator/' || source.response_hash,
                       source.response_hash, 'runner', 'proof', '{}'
                FROM source
                JOIN jobs ON jobs.player_id = source.player_id
                JOIN catalogued ON catalogued.response_hash = source.response_hash
                RETURNING id, player_id, normalized_tag
            )
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, current_league_season_id,
                previous_league_season_id, profile_json
            )
            SELECT player_id, id, normalized_tag, 'profile-v1',
                   'profile-schema-v1', 'runner', %s, 200,
                   'Coordinator ' || normalized_tag, 5000 + player_id,
                   105000036, 'Legend I', 'eligible', 'runner-season',
                   'runner-previous', jsonb_build_object(
                       'tag', normalized_tag, 'name', 'Coordinator ' || normalized_tag,
                       'trophies', 5000 + player_id,
                       'leagueTier', jsonb_build_object(
                           'id', 105000036, 'name', 'Legend I'
                       )
                   )
            FROM observations
            """,
            (day_start,),
        )
        source_profile_id = connection.execute(
            """
            SELECT id FROM player_profile_versions
            WHERE normalized_tag = '#C00002' ORDER BY id DESC LIMIT 1
            """
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO legend_season_anchors (
                current_league_season_id, previous_league_season_id,
                current_start, previous_start, anchor_rule_version,
                source_profile_version_id, state
            ) VALUES ('runner-season', 'runner-previous', %s, %s,
                      'legend-season-anchor-v1', %s, 'confirmed')
            """,
            (day_start, day_start - timedelta(days=28), source_profile_id),
        )
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end,
                official_season_id, season_day_number,
                season_anchor_rule_version, reconciliation_rule_version,
                result_hash, input_hash, version, state, confidence,
                evidence_complete, coverage_complete, reconciled
            )
            SELECT id, %s, %s, 'runner-season', 1,
                   'runner-anchor-v1', 'runner-rules-v1',
                   repeat(md5(id::text), 2), repeat(md5((id + 1)::text), 2),
                   1, 'Complete', 'exact', true, true, true
            FROM players WHERE normalized_tag LIKE '#C%%'
            """,
            (day_start, BOUNDARY),
        )
        sweep_id = connection.execute(
            """
            INSERT INTO collector_reset_sweeps (boundary_at, membership_rule_version)
            VALUES (%s, 'active-members-v1') RETURNING id
            """,
            (BOUNDARY,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO collector_reset_sweep_members (sweep_id, player_id)
            SELECT %s, id FROM players WHERE normalized_tag LIKE '#C%%'
            """,
            (sweep_id,),
        )
        generation_id = connection.execute(
            """
            INSERT INTO boundary_publication_generations (
                boundary_at, generation, sweep_id, ordering_rule_version,
                freshness_rule_version, expected_population_count,
                expected_population_hash, membership_rule_version,
                snapshot_rule_version, army_rule_version,
                target_rule, target_at
            ) VALUES (%s, 1, %s, 'legend-snapshot-order-v1',
                      'legend-profile-freshness-v1', %s,
                      repeat(md5('coordinator-writer-population'), 2),
                      'active-members-v1', 'legend-analytics-v1',
                      'army-analytics-v2', 'boundary-delay-v1', %s)
            RETURNING id
            """,
            (BOUNDARY, sweep_id, population, BOUNDARY + timedelta(minutes=5)),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members
                (generation_id, player_id, ranked_day_version_id, status,
                 snapshot_status, army_status)
            SELECT %s, player.id, ranked.id, 'terminal', 'complete', 'complete'
            FROM players AS player
            JOIN ranked_day_versions AS ranked
              ON ranked.player_id = player.id AND ranked.ranked_day_start = %s
            WHERE player.normalized_tag LIKE '#C%%'
            """,
            (generation_id, day_start),
        )
        connection.execute(
            """
            UPDATE boundary_publication_generations
            SET membership_captured_at = clock_timestamp()
            WHERE id = %s
            """,
            (generation_id,),
        )
        connection.execute(
            """
            INSERT INTO boundary_publication_manifests
                (generation_id, artifact_kind, rule_versions, digest)
            VALUES
                (%s, 'snapshot', %s, repeat(md5('coordinator-writer-snapshot'), 2)),
                (%s, 'army', %s, repeat(md5('coordinator-writer-army'), 2))
            """,
            (
                generation_id,
                Jsonb({
                    "ordering_rule_version": "legend-snapshot-order-v1",
                    "freshness_rule_version": "legend-profile-freshness-v1",
                    "analytics_rule_version": "legend-analytics-v1",
                }),
                generation_id,
                Jsonb({
                    "ordering_rule_version": "legend-snapshot-order-v1",
                    "freshness_rule_version": "legend-profile-freshness-v1",
                    "analytics_rule_version": "army-analytics-v2",
                }),
            ),
        )
        connection.execute(
            """
            INSERT INTO boundary_publication_manifest_rows
                (manifest_id, ordinal, player_id, ranked_day_version_id,
                 input_hash, classification, input_identity)
            SELECT manifest.id,
                   row_number() OVER (PARTITION BY manifest.id ORDER BY player.id),
                   player.id, ranked.id, repeat(md5(player.id::text), 2),
                   'Complete', jsonb_build_object(
                       'artifact_kind', manifest.artifact_kind,
                       'generation', 1, 'player_id', player.id,
                       'ranked_day_version_id', ranked.id,
                       'input_hash', repeat(md5(player.id::text), 2),
                       'classification', 'Complete', 'profile_version_id', profile.id,
                       'profile_snapshot', jsonb_build_object(
                           'tag', player.normalized_tag, 'trophies', profile.trophies,
                           'observation_id', profile.observation_id,
                           'observed_at', %s::text, 'eligibility_state', 'eligible'
                       ), 'battle_ids', '[]'::jsonb, 'decode_ids', '[]'::jsonb,
                       'evidence_ids', '[]'::jsonb
                   )
            FROM boundary_publication_manifests AS manifest
            JOIN players AS player ON player.normalized_tag LIKE '#C%%'
            JOIN ranked_day_versions AS ranked
              ON ranked.player_id = player.id AND ranked.ranked_day_start = %s
            JOIN player_profile_versions AS profile
              ON profile.player_id = player.id AND profile.observed_at = %s
            WHERE manifest.generation_id = %s
            """,
            (day_start.isoformat(), day_start, day_start, generation_id),
        )
        connection.execute(
            """
            UPDATE boundary_publication_manifests
            SET rows_sealed = true, frozen_at = clock_timestamp()
            WHERE generation_id = %s
            """,
            (generation_id,),
        )
        manifest_ids = connection.execute(
            """
            SELECT artifact_kind, id, digest
            FROM boundary_publication_manifests
            WHERE generation_id = %s ORDER BY artifact_kind
            """,
            (generation_id,),
        ).fetchall()
        by_kind = {_text(row[0]): (int(row[1]), _text(row[2])) for row in manifest_ids}
        connection.execute(
            """
            UPDATE boundary_publication_generations
            SET snapshot_manifest_id = %s, army_manifest_id = %s,
                snapshot_state = 'ready', army_state = 'ready'
            WHERE id = %s
            """,
            (by_kind["snapshot"][0], by_kind["army"][0], generation_id),
        )
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                work_type, deduplication_key, input_json, state, due_at,
                processing_version, domain_rule_version, analytics_rule_version
            ) VALUES
                ('build_snapshot', 'coordinator-writer:snapshot', %s, 'pending',
                 clock_timestamp(), 'clashlens-domain-processing-v1',
                 'clashlens-domain-rules-v1', 'legend-analytics-v1'),
                ('build_army_analytics', 'coordinator-writer:army', %s, 'pending',
                 clock_timestamp(), 'clashlens-domain-processing-v1',
                 'clashlens-domain-rules-v1', 'army-analytics-v2')
            """,
            (
                Jsonb({
                    "boundary_at": BOUNDARY.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "generation": 1, "manifest_id": by_kind["snapshot"][0],
                    "manifest_digest": by_kind["snapshot"][1],
                }),
                Jsonb({
                    "boundary_at": BOUNDARY.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "generation": 1, "manifest_id": by_kind["army"][0],
                    "manifest_digest": by_kind["army"][1],
                }),
            ),
        )

    database, processor, _metrics, _spool = _processor(connection_info, archive)
    try:
        while True:
            with psycopg.connect(connection_info) as connection:
                pending = connection.execute(
                    """
                    SELECT id FROM python_processing_jobs
                    WHERE work_type IN ('build_snapshot', 'build_analytics',
                                        'build_army_analytics')
                      AND status IN ('pending', 'waiting_retry', 'waiting_dependency')
                    ORDER BY id
                    """
                ).fetchall()
            if not pending:
                break
            for (job_id,) in pending:
                result = processor.process_job(
                    int(job_id), owner=f"coordinator-writer-{job_id}", lease_seconds=300
                )
                if result is None or result.outcome != "processed":
                    with psycopg.connect(connection_info) as error_connection:
                        error_row = error_connection.execute(
                            "SELECT work_type, input_json, status, failure_category, failure_detail FROM python_processing_jobs WHERE id = %s",
                            (job_id,),
                        ).fetchone()
                    raise RuntimeError(
                        f"coordinator writer job {job_id} did not process: {result}; {error_row}"
                    )
    finally:
        database.close()

    with psycopg.connect(connection_info) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM boundary_publication_manifests
                 WHERE generation_id = %s AND rows_sealed),
                (SELECT count(*) FROM boundary_publication_manifest_rows
                 WHERE manifest_id IN (SELECT id FROM boundary_publication_manifests
                                       WHERE generation_id = %s)),
                (SELECT count(*) FROM leaderboard_snapshots WHERE boundary_at = %s),
                (SELECT count(*) FROM leaderboard_snapshot_entries AS entry
                 JOIN leaderboard_snapshots AS snapshot ON snapshot.id = entry.snapshot_id
                 WHERE snapshot.boundary_at = %s),
                (SELECT count(*) FROM boundary_publication_artifact_identities
                 WHERE generation_id = %s),
                (SELECT count(*) FROM boundary_publication_events
                 WHERE boundary_at = %s AND generation = 1)
            """,
            (generation_id, generation_id, BOUNDARY, BOUNDARY, generation_id, BOUNDARY),
        ).fetchone()
        generation = connection.execute(
            """
            SELECT generation, snapshot_state, army_state,
                   snapshot_manifest_id, army_manifest_id, snapshot_id,
                   snapshot_analytics_publication_id, army_publication_id,
                   snapshot_coverage, army_coverage
            FROM boundary_publication_generations WHERE id = %s
            """,
            (generation_id,),
        ).fetchone()
        job_counts = connection.execute(
            """
            SELECT work_type, count(*) FROM python_processing_jobs
            WHERE input_json->>'generation' = '1'
              AND work_type IN ('build_snapshot', 'build_analytics',
                                'build_army_analytics')
            GROUP BY work_type ORDER BY work_type
            """
        ).fetchall()
        residue = connection.execute(
            """
            SELECT work_type, count(*) FROM python_processing_jobs
            WHERE status IN ('pending','leased','waiting_retry','waiting_dependency')
            GROUP BY work_type ORDER BY work_type
            """
        ).fetchall()
    if generation is None or _text(generation[1]) != "published" or _text(generation[2]) != "published":
        raise RuntimeError(f"coordinator writer generation did not publish: {generation}")
    headers, entries = int(counts[2]), int(counts[3])
    if entries != population * 2 or headers != 2 or int(counts[1]) != population * 2:
        raise RuntimeError(
            f"coordinator writer cardinality mismatch: headers={headers}, entries={entries}, manifest_rows={counts[1]}"
        )
    return {
        "population": population,
        "official_responses": 0,
        "coordinator_job_counts": {
            _text(row[0]): int(row[1]) for row in job_counts
        },
        "manifest_publication": {
            "manifest_count": int(counts[0]),
            "manifest_rows": int(counts[1]),
            "classification_counts": {"Complete": int(counts[1])},
            "identities_frozen": True,
        },
        "contract": {"database_version": 5, "required_version": 5},
        "coverage": {"expected": population, "included": population, "excluded": 0},
        "publication_identities": int(counts[4]),
        "generation": {
            "number": int(generation[0]),
            "snapshot_state": _text(generation[1]),
            "army_state": _text(generation[2]),
            "snapshot_manifest_id": int(generation[3]),
            "army_manifest_id": int(generation[4]),
            "snapshot_id": int(generation[5]),
            "snapshot_analytics_publication_id": int(generation[6]),
            "army_publication_id": int(generation[7]),
            "snapshot_coverage": generation[8],
            "army_coverage": generation[9],
        },
        "coordinator_links": {
            "sealed_manifests": int(counts[0]),
            "completed_manifest_jobs": 3,
            "generation_identities": int(counts[4]),
            "publication_signals": int(counts[5]),
        },
        "coordinator_residue": {
            "jobs": sum(int(row[1]) for row in residue),
            "corrections": 0,
            "generations": 0,
        },
        "snapshot_headers": headers,
        "snapshot_entries": entries,
        "full_large_reset": {
            "status": "completed",
            "execution_method": "real Python snapshot, analytics, and army writers",
        },
        "statement_ceiling": {
            "snapshot_entry_application": 1,
            "snapshot_entry_postgresql": 1,
            "army_fact_input_queries": 4,
            "army_fact_bulk_writes": 4,
        },
        "queue_residue": [
            {"work_type": _text(row[0]), "count": int(row[1])} for row in residue
        ],
        "coordinator_processing": {
            "snapshot": "production Python writer",
            "analytics": "production Python writer",
            "army": "production Python writer",
        },
    }


def _run_reset(
    connection_info: str, archive: Any, population: int, correction: bool
) -> dict[str, Any]:
    import psycopg

    database, processor, metrics, _spool = _processor(connection_info, archive)
    database._disable_decode_corrections = True
    try:
        if correction:
            admission_evidence = None
            source_jobs = _store_reconciliation_population(
                connection_info, archive, population
            )
        else:
            source_jobs, admission_evidence = _store_production_reset_population(
                connection_info, archive, population
            )
        # Qualify fixed fixture discoveries only after production membership
        # capture so the retained population is exactly the requested count.
        fixture_discoveries = _seed_fixture_discoveries(
            connection_info, BATTLE_FIXTURE
        )
        profile_results = _process_jobs(
            processor, source_jobs, "reset-evidence", serial=True
        )
        first = _drain(processor, population * 20 + 100)
        if correction:
            correction_jobs = _store_reconciliation_corrections(
                connection_info, archive, population
            )
            profile_results.extend(
                _process_jobs(
                    processor, correction_jobs, "correction-evidence", serial=True
                )
            )
            second = _drain(processor, population * 20 + 100)
            # A correction can be queued after the source reconciliation has
            # observed the published generation. Re-run the coordinator's
            # durable recovery seam so that queued work is activated before
            # asserting the correction generation.
            database.reevaluate_boundary_publications()
            second.extend(_drain(processor, population * 20 + 100))
        else:
            second = []
        army_endpoint = _query_army_endpoint(connection_info)
        with psycopg.connect(connection_info) as connection:
            counts = connection.execute(
                """SELECT
                    (SELECT count(*) FROM ranked_day_versions
                     WHERE ranked_day_start = %s),
                    (SELECT count(*) FROM leaderboard_snapshots
                     WHERE boundary_at = %s),
                    (SELECT count(*) FROM leaderboard_snapshot_entries entry
                     JOIN leaderboard_snapshots snapshot ON snapshot.id = entry.snapshot_id
                     WHERE snapshot.boundary_at = %s),
                    (SELECT count(*) FROM analytics_summaries summary
                     JOIN leaderboard_snapshots snapshot ON snapshot.id = summary.snapshot_id
                     WHERE snapshot.boundary_at = %s),
                    (SELECT count(*) FROM army_analytics_battle_facts
                     WHERE ranked_day_start = %s),
                    (SELECT count(*) FROM boundary_publication_generations
                     WHERE boundary_at = %s),
                    (SELECT json_agg(json_build_object('generation', generation, 'snapshot_state', snapshot_state, 'army_state', army_state) ORDER BY generation)
                     FROM boundary_publication_generations WHERE boundary_at = %s)""",
                (
                    DAY_START,
                    BOUNDARY,
                    BOUNDARY,
                    BOUNDARY,
                    DAY_START,
                    BOUNDARY,
                    BOUNDARY,
                ),
            ).fetchone()
            active_rows = connection.execute(
                """SELECT 'python', work_type, count(*) FROM python_processing_jobs
                   WHERE status IN ('pending','leased','waiting_retry','waiting_dependency') GROUP BY work_type
                   UNION ALL
                   SELECT 'collector', work_type, count(*) FROM collector_jobs
                   WHERE status IN ('pending','leased','waiting_retry','waiting_dependency') GROUP BY work_type
                   ORDER BY 1, 2"""
            ).fetchall()
            correction_evidence = None
            if correction:
                correction_evidence = {
                    "superseded_snapshots": int(
                        connection.execute(
                            "SELECT count(*) FROM leaderboard_snapshots WHERE state='superseded'"
                        ).fetchone()[0]
                    ),
                    "snapshots_with_prior_reference": int(
                        connection.execute(
                            "SELECT count(*) FROM leaderboard_snapshots WHERE correction_of_id IS NOT NULL"
                        ).fetchone()[0]
                    ),
                }
        generations = 2 if correction else 1
        generation_count = int(counts[5])
        generation_states = (counts[6] or [])[: generations + 1]
        expected_counts = {
            "ranked_day_versions": generations * population,
            "snapshot_headers": 2 * generations,
            "snapshot_entries": 2 * generations * population,
        }
        actual_counts = {
            "ranked_day_versions": int(counts[0]),
            "snapshot_headers": int(counts[1]),
            "snapshot_entries": int(counts[2]),
        }
        outcomes = profile_results + first + second
        hard_failures = []
        if generation_count != generations:
            hard_failures.append("reset_generation_count_mismatch")
        if actual_counts != expected_counts:
            hard_failures.append("reset_fanout_mismatch")
        if any(result["outcome"] != "processed" for result in outcomes):
            hard_failures.append("reset_non_processed_result")
        if active_rows:
            hard_failures.append("reset_queue_residue")
        processing_summary = {
            "official": _result_summary(profile_results, expected=len(profile_results)),
            "dependent": _result_summary(first, expected=len(first)),
            "correction": _result_summary(second, expected=len(second)),
            "total": _result_summary(outcomes, expected=len(outcomes)),
        }
        return {
            "status": "passed" if not hard_failures else "failed",
            "hard_failures": _failure_codes(hard_failures),
            "population": population,
            "official_responses": len(profile_results),
            "processing_summary": processing_summary,
            "fact_counts": {
                **actual_counts,
                "analytics_summaries": int(counts[3]),
                "army_facts": int(counts[4]),
            },
            "fanout_evidence": {
                "expected": expected_counts,
                "matches_expected": (
                    generation_count == generations
                    and actual_counts == expected_counts
                ),
                "snapshot_entries_per_population": 2 * len(generation_states),
                "generation_states": generation_states,
            },
            "boundary_admission": admission_evidence,
            "queue_residue": [
                {
                    "owner": _text(row[0]),
                    "work_type": _text(row[1]),
                    "count": int(row[2]),
                }
                for row in active_rows
            ],
            "fixture_discoveries_prequalified": fixture_discoveries,
            "stage_metrics": metrics.snapshot(),
            "spool": _spool.stats(),
            "evidence_counters": _spool.counters(),
            "_spool_root": str(_spool.spool.root),
            "army_endpoint": army_endpoint,
            "correction_evidence": correction_evidence,
        }
    finally:
        database.close()


def _run_duplicate(
    connection_info: str,
    archive: Any,
    count: int,
    *,
    cycles: int = 1,
    observation_start: datetime = DAY_START,
    battle_fixture: bytes | None = None,
    processing_started: Event | None = None,
) -> dict[str, Any]:
    import psycopg
    from domain_test_support import store_observation

    database, processor, metrics, _spool = _processor(connection_info, archive)
    try:
        selected_battle_fixture = (
            BATTLE_FIXTURE if battle_fixture is None else battle_fixture
        )
        fixture_discoveries = _seed_fixture_discoveries(
            connection_info, selected_battle_fixture, RANKING_FIXTURE
        )
        executed_count = min(count, DUPLICATE_EXECUTION_CAP)
        endpoint_mix = _duplicate_endpoint_mix(count)
        results: list[dict[str, Any]] = []
        cycle_elapsed: list[float] = []
        profile_bodies: dict[tuple[str, int], bytes] = {}
        fixture_bodies: dict[str, bytes] = {}
        source_bytes: dict[str, int] = {
            "profile": len(_profile_body(_tag(1))),
            "battle_log": len(selected_battle_fixture),
            "global_player_rankings": len(RANKING_FIXTURE),
        }
        exact_bytes = 0
        executed_endpoint_mix: dict[str, int] = dict.fromkeys(endpoint_mix, 0)
        for cycle in range(max(1, cycles)):
            jobs: list[int] = []
            position = 0
            remaining = executed_count
            # One transaction per cycle models the collector's batched handoff
            # without a new PostgreSQL connection per response.
            with psycopg.connect(connection_info) as seed_connection:
                for endpoint, planned_count in endpoint_mix.items():
                    endpoint_count = min(planned_count, remaining)
                    for index in range(endpoint_count):
                        tag, body = _duplicate_fixture_body(
                            endpoint,
                            index,
                            planned_count,
                            profile_bodies,
                            fixture_bodies,
                            battle_fixture,
                        )
                        source_bytes[endpoint] = len(body)
                        exact_bytes += len(body)
                        jobs.append(
                            store_observation(
                                connection_info,
                                archive,
                                occurrence_key=f"duplicate-c{cycle}-{position}",
                                endpoint=endpoint,
                                body=body,
                                observed_at=observation_start
                                + timedelta(hours=1, minutes=position),
                                normalized_tag=tag,
                                existing_connection=seed_connection,
                                commit=False,
                            )[1]
                        )
                        position += 1
                    executed_endpoint_mix[endpoint] += endpoint_count
                    remaining -= endpoint_count
                    if remaining == 0:
                        break
                seed_connection.commit()
            started = time.perf_counter()
            if processing_started is not None:
                processing_started.set()
            results.extend(_process_jobs(processor, jobs, f"duplicate-c{cycle}"))
            cycle_elapsed.append(time.perf_counter() - started)
        with psycopg.connect(connection_info) as connection:
            payload_rows = connection.execute(
                """
                SELECT endpoint, count(*)
                FROM parsed_source_payloads
                GROUP BY endpoint ORDER BY endpoint
                """
            ).fetchall()
            profile_rows = connection.execute(
                "SELECT count(*) FROM player_profile_versions"
            ).fetchone()[0]
            profile_effects = connection.execute(
                "SELECT count(*) FROM player_profile_effects"
            ).fetchone()[0]
            if getattr(database, "_supports_content_dedup", False):
                battle_canonical_rows = connection.execute(
                    "SELECT count(*) FROM battle_source_rows WHERE parsed_payload_id IS NOT NULL"
                ).fetchone()[0]
                battle_occurrence_rows = connection.execute(
                    "SELECT count(*) FROM battle_log_observation_rows"
                ).fetchone()[0]
                ranking_canonical_rows = connection.execute(
                    "SELECT count(*) FROM official_top200_entries WHERE parsed_payload_id IS NOT NULL"
                ).fetchone()[0]
                ranking_occurrence_links = connection.execute(
                    "SELECT count(*) FROM official_top200_version_entries"
                ).fetchone()[0]
            else:
                battle_canonical_rows = 0
                battle_occurrence_rows = connection.execute(
                    "SELECT count(*) FROM battle_source_rows"
                ).fetchone()[0]
                ranking_canonical_rows = 0
                ranking_occurrence_links = connection.execute(
                    "SELECT count(*) FROM official_top200_entries"
                ).fetchone()[0]
            connection.commit()
        canonical_content = {
            "parsed_payloads_by_endpoint": {
                _text(row[0]): int(row[1]) for row in payload_rows
            },
            "profile_semantic_versions": int(profile_rows),
            "profile_occurrence_effects": int(profile_effects),
            "battle_canonical_rows": int(battle_canonical_rows),
            "battle_occurrence_rows": int(battle_occurrence_rows),
            "ranking_canonical_rows": int(ranking_canonical_rows),
            "ranking_occurrence_links": int(ranking_occurrence_links),
        }
        cycle_count = max(1, cycles)
        response_counts = {
            endpoint: value * cycle_count
            for endpoint, value in _duplicate_response_mix(count, endpoint_mix).items()
        }
        executed_counts = dict(executed_endpoint_mix)
        steady = sorted(cycle_elapsed)[len(cycle_elapsed) // 2]
        processing_summary = _result_summary(
            results, expected=executed_count * cycle_count
        )
        return {
            "observations": count,
            "official_responses": count * cycle_count,
            "executed_observations": executed_count * cycle_count,
            "measured_cycles": len(cycle_elapsed),
            "cycle_elapsed_seconds": cycle_elapsed,
            "median_cycle_seconds": steady,
            "daily_288_cycle_projection_seconds": steady * 288,
            "aggregation_factor": count / executed_count,
            "aggregation_method": (
                "exact bounded cycle"
                if cycles == 1 and count == executed_count
                else "24h-equivalent aggregate: each response executes the full raw-evidence/local/Python/PostgreSQL semantics; the measured fixture hashes are replayed as verified duplicates in later cycles; the 288-cycle day projection multiplies the median measured five-minute cycle"
            ),
            "endpoint_mix": response_counts,
            "response_counts_by_endpoint": response_counts,
            "occurrence_counts_by_endpoint": executed_counts,
            "fixture_bytes_by_endpoint": source_bytes,
            "exact_bytes": exact_bytes,
            "official_api_traffic": {"requests": 0, "source": "committed fixtures"},
            "canonical_content": canonical_content,
            "contract": {
                "expected_occurrences": DUPLICATE_EXECUTION_CAP,
                "executed_occurrences": executed_count,
                "matches_expected": count == DUPLICATE_EXECUTION_CAP,
                "endpoint_mix": dict(DUPLICATE_ENDPOINT_MIX),
            },
            "fixture_discoveries_prequalified": fixture_discoveries,
            "processing_summary": processing_summary,
            "stage_metrics": metrics.snapshot(),
            "spool": _spool.stats(),
            "evidence_counters": _spool.counters(),
            "_spool_root": str(_spool.spool.root),
        }
    finally:
        database.close()


def _run_mixed(
    connection_info: str, archive: Any, live: int, backfill: int
) -> dict[str, Any]:
    import psycopg
    from domain_test_support import store_observation
    from psycopg.types.json import Jsonb

    from clashlens.worker import process_concurrently

    started = time.perf_counter()
    cpu_start = time.process_time()
    pressure_before = _memory_pressure(connection_info)
    wal_start, statement_start, wal_retained_start = _start_metrics(connection_info)
    database, processor, metrics, spool = _processor(connection_info, archive)
    try:
        fixture_discoveries = _seed_fixture_discoveries(
            connection_info, BATTLE_FIXTURE
        )
        jobs: list[tuple[str, int]] = []
        effective_lanes = min(_LANES, 32)
        for kind, count in (("backfill", backfill), ("live", live)):
            for index in range(count):
                tag = _tag(index + 1)
                if kind == "backfill":
                    observation, collection_job = store_observation(
                        connection_info,
                        archive,
                        occurrence_key=f"{kind}-battle-{index}",
                        endpoint="battle_log",
                        body=BATTLE_FIXTURE,
                        observed_at=DAY_START + timedelta(hours=1),
                        normalized_tag=tag,
                        deduplication_key=f"perf-{kind}-battle-{index}",
                    )
                    seeded = processor.process_job(
                        collection_job, owner=f"perf-mixed-seed-{index}"
                    )
                    if seeded is None or seeded.outcome != "processed":
                        raise RuntimeError(f"mixed battle seed failed: {seeded}")
                    with psycopg.connect(connection_info) as connection:
                        battle_id = connection.execute(
                            """SELECT battle_id FROM battle_evidence
                               WHERE observation_id = %s
                               ORDER BY id LIMIT 1""",
                            (observation,),
                        ).fetchone()
                        if battle_id is None:
                            raise RuntimeError("mixed battle seed produced no battle")
                        battle_id = int(battle_id[0])
                        job = connection.execute(
                            """INSERT INTO python_processing_jobs (
                                   work_type, deduplication_key, input_json,
                                   processing_version, domain_rule_version,
                                   analytics_rule_version, priority
                               ) VALUES (
                                   'redecode_army', %s, %s,
                                   'clashlens-domain-processing-v1',
                                   'clashlens-domain-rules-v1', 'army-analytics-v2', 25
                               ) RETURNING id""",
                            (
                                f"redecode_army:army-decoder-v2:unit-catalog-v1:{battle_id}:{battle_id}",
                                Jsonb({"battle_ids": [battle_id]}),
                            ),
                        ).fetchone()
                        assert job is not None
                        job = int(job[0])
                else:
                    _observation, job = store_observation(
                        connection_info,
                        archive,
                        occurrence_key=f"{kind}-{index}",
                        endpoint="profile",
                        body=_profile_body(tag),
                        observed_at=DAY_START + timedelta(hours=1),
                        normalized_tag=tag,
                        deduplication_key=f"perf-{kind}-{index}",
                    )
                jobs.append((kind, job))
        by_id = {job: kind for kind, job in jobs}
        results = process_concurrently(
            processor,
            concurrency=effective_lanes,
            owner="perf-mixed",
            max_jobs=len(jobs),
            lease_seconds=300,
        )
        result_by_id = {
            result.job_id: {
                "job_id": result.job_id,
                "outcome": result.outcome,
                "category": result.category,
            }
            for result in results
        }
        with psycopg.connect(connection_info) as connection:
            rows = connection.execute(
                """SELECT job.id, job.status, job.work_type, job.created_at,
                          job.completed_at, attempt.started_at
                   FROM python_processing_jobs AS job
                   LEFT JOIN LATERAL (
                       SELECT started_at
                       FROM python_processing_attempts
                       WHERE job_id = job.id
                       ORDER BY attempt_number DESC
                       LIMIT 1
                   ) AS attempt ON true
                   WHERE job.id = ANY(%s::bigint[])""",
                ([job for _, job in jobs],),
            ).fetchall()
        records = []
        for row in rows:
            job_id = int(row[0])
            result = result_by_id.get(job_id)
            if result is None or row[4] is None:
                continue
            records.append(
                {
                    **result,
                    "kind": by_id[job_id],
                    "work_type": str(row[2]),
                    "status": str(row[1]),
                    "completed_at": row[4].astimezone(UTC).isoformat(),
                    "queue_latency_seconds": max(
                        0.0,
                        ((row[5] or row[4]) - row[3]).total_seconds(),
                    ),
                    "collection_latency_seconds": max(
                        0.0, (row[4] - row[3]).total_seconds()
                    ),
                }
            )
        records.sort(key=lambda item: (item["completed_at"], item["job_id"]))
        order = [item["kind"] for item in records]
        live_latencies = sorted(
            item["queue_latency_seconds"] for item in records if item["kind"] == "live"
        )
        live_collection_latencies = [
            item["collection_latency_seconds"]
            for item in records
            if item["kind"] == "live"
        ]
        p95_index = max(0, (len(live_latencies) * 95 + 99) // 100 - 1)
        live_p95 = live_latencies[p95_index] if live_latencies else None
        live_max = max(live_latencies, default=None)
        live_collection_max = max(live_collection_latencies, default=None)
        pressure_after = _memory_pressure(connection_info)
        pressure_delta = _memory_pressure_delta(pressure_before, pressure_after)
        database_metrics = _db_snapshot(
            connection_info,
            wal_start,
            statement_start,
            wal_retained_start=wal_retained_start,
        )
        queue_residue = database_metrics["queue_residue"]
        elapsed_seconds = time.perf_counter() - started
        live_latency_passed = (
            len(live_latencies) == live
            and live_collection_max is not None
            and live_collection_max <= STEP5_COLLECTION_LIMIT_SECONDS
        )
        five_minute_passed = elapsed_seconds <= STEP5_COLLECTION_LIMIT_SECONDS
        processing_summary = _result_summary(records, expected=len(jobs))
        hard_failures = []
        if len(records) != len(jobs):
            hard_failures.append("mixed_result_count_mismatch")
        if any(
            item["outcome"] != "processed" or item["status"] != "complete"
            for item in records
        ):
            hard_failures.append("mixed_non_processed_result")
        if not live_latency_passed:
            hard_failures.append("mixed_live_latency_exceeded")
        if not five_minute_passed:
            hard_failures.append("mixed_collection_latency_exceeded")
        if queue_residue:
            hard_failures.append("mixed_queue_residue")
        hard_failures.extend(
            _memory_pressure_failure_codes(
                pressure_before, pressure_after, pressure_delta
            )
        )
        completion_order_valid = (
            len(order) <= MAX_COMPLETION_ORDER
            and len(order) <= live + backfill
            and all(item in {"live", "backfill"} for item in order)
            and order.count("live") == live
            and order.count("backfill") == backfill
        )
        retained_completion_order = order if completion_order_valid else None
        return {
            "completion_order": retained_completion_order,
            "completion_order_complete": completion_order_valid,
            "completion_counts": {
                "live": order.count("live"),
                "backfill": order.count("backfill"),
            },
            "live_jobs": live,
            "backfill_jobs": backfill,
            "configured_lanes": _LANES,
            "effective_lanes": effective_lanes,
            "official_responses": len(jobs),
            "official_api_traffic": {"requests": 0, "source": "committed fixtures"},
            "fixture_discoveries_prequalified": fixture_discoveries,
            "live_first_completion_index": order.index("live")
            if retained_completion_order is not None and "live" in order
            else None,
            "live_queue_latency_seconds": {
                "count": len(live_latencies),
                "p95": live_p95,
                "maximum": live_max,
                "collection_maximum": live_collection_max,
            },
            "oldest_active_queue_age_seconds": max(
                (
                    age
                    for age in database_metrics["queue_age_seconds"].values()
                    if age is not None
                ),
                default=None,
            ),
            "elapsed_seconds": elapsed_seconds,
            "cpu_seconds": time.process_time() - cpu_start,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "memory_pressure_before": pressure_before,
            "memory_pressure_after": pressure_after,
            "memory_pressure_delta": pressure_delta,
            "live_latency_contract": {
                "target_seconds": STEP5_COLLECTION_LIMIT_SECONDS,
                "p95_seconds": live_p95,
                "maximum_seconds": live_max,
                "collection_maximum_seconds": live_collection_max,
                "passed": live_latency_passed,
            },
            "five_minute_contract": {
                "target_seconds": STEP5_COLLECTION_LIMIT_SECONDS,
                "elapsed_seconds": elapsed_seconds,
                "passed": five_minute_passed,
            },
            "hard_failures": _failure_codes(hard_failures),
            "processing_summary": processing_summary,
            "database": database_metrics,
            "stage_metrics": metrics.snapshot(),
            "spool": spool.stats(),
            "evidence_counters": spool.counters(),
            "_spool_root": str(spool.spool.root),
        }
    finally:
        database.close()


def _collector_probe(skip: bool) -> dict[str, Any]:
    if skip:
        return {"executed": False, "reason": "explicit test-only skip"}
    started = time.perf_counter()
    completed = subprocess.run(
        [
            "go",
            "test",
            "./internal/collector",
            "-run",
            "^TestGoCollectorHandoffToPythonSignedPlayerPage$",
            "-count=1",
            "-timeout=120s",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=150,
    )
    if completed.returncode:
        raise RuntimeError(
            "collector transitive probe failed: " + completed.stderr[-1000:]
        )
    return {
        "executed": True,
        "elapsed_seconds": time.perf_counter() - started,
        "test": "TestGoCollectorHandoffToPythonSignedPlayerPage",
    }


ARCHIVE_PROBE_MARKER = "PERF_DUPLICATE_ARCHIVE_PROBE "


def _parse_archive_probe_marker(output: str) -> dict[str, int]:
    markers = [
        line.removeprefix(ARCHIVE_PROBE_MARKER)
        for line in output.splitlines()
        if line.startswith(ARCHIVE_PROBE_MARKER)
    ]
    if len(markers) != 1:
        raise RuntimeError(
            f"archive probe emitted {len(markers)} markers, want exactly 1"
        )
    try:
        parsed = json.loads(markers[0])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"malformed archive probe marker: {error}") from error
    required = (
        "count",
        "head",
        "get",
        "put",
        "raw_count",
        "raw_head",
        "raw_put",
        "raw_get",
        "raw_duplicate_bucket_requests",
        "hash_us",
        "operation_total_us",
        "stage_put_us",
        "stage_get_verify_us",
        "local_verify_us",
    )
    if not isinstance(parsed, dict) or any(
        not isinstance(parsed.get(key), int) or isinstance(parsed.get(key), bool)
        for key in required
    ):
        raise RuntimeError(
            "archive probe marker must contain integer totals for keys: "
            + ",".join(required)
        )
    return parsed


def _collector_archive_probe(count: int) -> dict[str, Any]:
    """Probe the production Go s3Archive.store duplicate path via a real HTTP S3 fake."""
    started = time.perf_counter()
    completed = subprocess.run(
        [
            "go",
            "test",
            "./internal/collector",
            "-run",
            "^TestS3ArchiveDuplicateStoreProbe$",
            # The probe issues two real HTTP operations per duplicate, so its
            # budget must scale with the requested observation count.
            "-count=1",
            "-timeout=1800s",
            "-v",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=1860,
        env={**os.environ, "CLASHLENS_PERF_DUPLICATE_ARCHIVE_COUNT": str(count)},
    )
    if completed.returncode:
        raise RuntimeError(
            "collector archive probe failed:\n"
            + "stdout[-1000:]: "
            + completed.stdout[-1000:]
            + "\n"
            + "stderr[-1000:]: "
            + completed.stderr[-1000:]
        )
    totals = _parse_archive_probe_marker(completed.stdout)
    # Legacy seam baseline plus the production raw-evidence module contract:
    # one conditional PUT + one verification GET for a new hash, then
    # zero-request duplicates for every later occurrence.
    legacy_count = int(totals.get("legacy_count", count))
    expected = {
        "count": count,
        "head": legacy_count,
        "get": legacy_count - 1,
        "put": 1,
        "raw_count": count,
        "raw_head": 0,
        "raw_put": 1,
        "raw_get": 1,
        "raw_duplicate_bucket_requests": 0,
    }
    mismatched = {
        key: (totals.get(key), wanted)
        for key, wanted in expected.items()
        if totals.get(key) != wanted
    }
    if mismatched:
        raise RuntimeError(
            f"archive probe totals {totals} do not match expected {expected}"
        )
    return {
        "executed": True,
        "test": "TestS3ArchiveDuplicateStoreProbe",
        **totals,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _run_army_read_sample(database_url: str, fact_count: int) -> dict[str, Any]:
    from domain_test_support import domain_database

    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        archive_server() as archive,
    ):
        wal_start, statement_start, wal_retained_start = _start_metrics(connection_info)
        relation_start = _relation_snapshot(connection_info)
        cpu_start = time.process_time()
        elapsed_start = time.perf_counter()
        _seed_fixture_discoveries(connection_info, BATTLE_FIXTURE)
        database, processor, _metrics, _spool = _processor(connection_info, archive)
        try:
            with count_sql_calls() as sql_calls:
                jobs = _store_reconciliation_population(connection_info, archive, 1)
                _process_jobs(
                    processor, jobs, "army-read-evidence", serial=True
                )
                _drain(processor, 120)
                reads = _seed_worst_case_army_reads(connection_info, fact_count)
            measurements = _db_snapshot(
                connection_info,
                wal_start,
                statement_start,
                relation_start,
                wal_retained_start,
            )
            measurements["application_sql_calls"] = sql_calls[0]
            return {
                "synthetic_fact_limit": fact_count,
                "selections": reads,
                "archive_operations": {
                    "get": archive[3].gets,
                    "head": archive[3].heads,
                },
                "database": measurements,
                "elapsed_seconds": time.perf_counter() - elapsed_start,
                "cpu_seconds": time.process_time() - cpu_start,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "spool": _spool.stats(),
                "evidence_counters": _spool.counters(),
            }
        finally:
            database.close()


def _retained_army_read_sample(
    database_url: str, fact_count: int
) -> dict[str, Any]:
    """Return a bounded failure sample when the post-reset read gate fails."""
    try:
        result = _run_army_read_sample(database_url, fact_count)
    except Exception:  # noqa: BLE001 - preserve completed reset evidence safely.
        return {
            "status": "failed",
            "reason": "army_read_sample_unavailable",
            "hard_failures": ["army_read_sample_unavailable"],
        }
    return {"status": "passed", "hard_failures": [], **result}


def _run_step5_army(database_url: str) -> dict[str, Any]:
    """Run the fixed PR 2 army protocol in one isolated production schema."""
    import psycopg
    from domain_test_support import domain_database
    from psycopg_pool import PoolTimeout

    pressure_before = _memory_pressure(database_url)
    started = time.perf_counter()
    cpu_start = time.process_time()
    specs = _army_selection_specs()
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        archive_server() as archive,
    ):
        wal_start, statement_start, wal_retained_start = _start_metrics(connection_info)
        relation_start = _relation_snapshot(connection_info)
        seed = _seed_step5_army_database(connection_info, archive)
        readiness, readiness_failure = _prepare_step5_statistics(connection_info)
        reads: list[dict[str, Any]] = []
        overlap: dict[str, Any] | None = None
        failed_phase: str | None = None
        failure = readiness_failure
        if failure is not None:
            failed_phase = "statistics_readiness"
        else:
            phase = "selection_reads"
            try:
                for spec in specs:
                    reads.append(_measure_army_pair(connection_info, spec))
                phase = "mixed_load"
                overlap = _run_step5_overlap(connection_info, archive, specs)
            except (psycopg.errors.TransactionTimeout, PoolTimeout):
                failed_phase = phase
                failure = "request_timeout"
            except Exception:  # noqa: BLE001 - retain only the finite failure code.
                failed_phase = phase
                failure = "workload_error"
        database = _db_snapshot(
            connection_info,
            wal_start,
            statement_start,
            relation_start,
            wal_retained_start,
        )
        postgres = _postgres_provenance(connection_info)
        pressure_after = _memory_pressure(database_url)
        pressure_delta = _memory_pressure_delta(
            pressure_before, pressure_after
        )
        active_queue_rows = [
            row
            for queue in database["queues"].values()
            for row in queue
            if row["status"]
            in {"pending", "leased", "waiting_retry", "waiting_dependency"}
        ]
        hard_failures = _army_completed_read_failures(reads)
        if failure is None:
            assert overlap is not None
            hard_failures.extend(overlap["hard_failures"])
        if active_queue_rows:
            hard_failures.append("queue_residue")
        if (
            pressure_before["process_cgroup_available"] != 1
            or pressure_after["process_cgroup_available"] != 1
            or pressure_before["database_cgroup_available"] != 1
            or pressure_after["database_cgroup_available"] != 1
        ):
            hard_failures.append("memory_pressure_unavailable")
        if any(pressure_delta.values()):
            hard_failures.append("memory_pressure_increased")
        if failure is not None:
            hard_failures.append("army_read_sample_unavailable")
        return {
            "status": "passed" if not hard_failures else "failed",
            "failed_phase": failed_phase,
            "failure": failure,
            "protocol": {
                "population": STEP5_POPULATION,
                "query_work_mem": "256MB",
                "days": STEP5_DAYS,
                "facts_per_member_day_per_lens": STEP5_FACTS_PER_MEMBER_DAY,
                "selected_members": STEP5_SELECTED_MEMBERS,
                "missing_trophy_rate": f"1/{STEP5_MISSING_TROPHY_RATE}",
                "troop_keys": len(STEP5_TROOP_KEYS),
                "warmups": STEP5_WARMUPS,
                "requests": STEP5_REQUESTS,
                "p95_target_ms": STEP5_P95_TARGET_MS,
                "forced_miss_target_seconds": STEP5_FORCED_MISS_TARGET_SECONDS,
                "forced_miss_pool_max_size": 2,
                "forced_miss_read_snapshot": "repeatable_read_exported",
                "mixed_lane_pool_max_size": STEP5_MIXED_LANE_POOL_MAX_SIZE,
                "mixed_lane_read_snapshot": "repeatable_read_exported",
                "analytics_lanes": STEP5_ANALYTICS_LANES,
                "duplicate_cycle_observations": DUPLICATE_EXECUTION_CAP,
            },
            "seed": seed,
            "statistics_readiness": readiness,
            "selections": reads,
            "mixed_load": overlap,
            "database": database,
            "postgres": postgres,
            "elapsed_seconds": time.perf_counter() - started,
            "cpu_seconds": time.process_time() - cpu_start,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "memory_pressure_before": pressure_before,
            "memory_pressure_after": pressure_after,
            "memory_pressure_delta": pressure_delta,
            "hard_failures": _failure_codes(hard_failures),
            "queue_drained": not active_queue_rows,
        }


def _pending_age_seconds(connection_info: str) -> float | None:
    import psycopg

    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """SELECT extract(epoch FROM clock_timestamp() - min(response_completed_at))
               FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'"""
        ).fetchone()
        return None if row[0] is None else float(row[0])


def _orphan_metrics(connection_info: str, spool_root: Path) -> dict[str, int]:
    """Count spool final objects with neither a catalogue row nor a live
    pending-remote-verification reference. These are measured from the same
    disposable database the workload used."""
    import psycopg

    verified: set[str] = set()
    pending: set[str] = set()
    with psycopg.connect(connection_info) as connection:
        for hash_value in connection.execute(
            "SELECT response_hash FROM archive_catalogue"
        ).fetchall():
            verified.add(hash_value[0])
        # Any committed observation referencing the hash is canonical evidence
        # regardless of catalogue coverage (e.g. pre-catalogue legacy rows).
        for hash_value in connection.execute(
            "SELECT DISTINCT response_hash FROM collector_observations WHERE response_hash IS NOT NULL"
        ).fetchall():
            verified.add(hash_value[0])
        for hash_value in connection.execute(
            "SELECT response_hash FROM collector_endpoint_results"
            " WHERE outcome = 'pending_remote_verification' AND response_hash IS NOT NULL"
        ).fetchall():
            pending.add(hash_value[0])
    count = bytes_total = 0
    for prefix_dir in (
        sorted((spool_root / "sha256").glob("[0-9a-f]" * 2))
        if spool_root.exists()
        else []
    ):
        for final_file in prefix_dir.iterdir():
            if final_file.name in verified or final_file.name in pending:
                continue
            try:
                bytes_total += final_file.stat().st_size
                count += 1
            except FileNotFoundError:
                continue
    return {"count": count, "bytes": bytes_total}


def _free_inodes(path: Path) -> int | None:
    try:
        import os

        return os.statvfs(path).f_favail
    except OSError:
        return None


def _filesystem_usage(path: Path) -> dict[str, Any]:
    """Return measured user-usable capacity and use for ``path``."""
    filesystem = os.statvfs(path)
    raw_capacity = int(filesystem.f_blocks * filesystem.f_frsize)
    available = int(filesystem.f_bavail * filesystem.f_frsize)
    reserved = max(
        0, int(filesystem.f_bfree - filesystem.f_bavail) * filesystem.f_frsize
    )
    usable_capacity = max(0, raw_capacity - reserved)
    used = max(0, usable_capacity - available)
    return {
        "path": str(path),
        "capacity_bytes": usable_capacity,
        "raw_capacity_bytes": raw_capacity,
        "usable_capacity_bytes": usable_capacity,
        "available_bytes": available,
        "used_bytes": used,
        "used_ratio": used / usable_capacity if usable_capacity else 0.0,
        "free_inodes": int(filesystem.f_favail),
    }


def _runway_inputs(
    filesystem_before: dict[str, Any],
    filesystem_after: dict[str, Any],
    database: dict[str, Any],
    relation_start: dict[str, dict[str, Any]],
    spool: dict[str, Any],
    archived_bytes: int,
    measured_intervals: int = 1,
) -> dict[str, Any]:
    relation_growth = sum(
        max(
            0,
            int(database["relation_sizes"].get(name, {}).get("total_bytes", 0))
            - int(relation_start.get(name, {}).get("total_bytes", 0)),
        )
        for name in database["relation_sizes"]
    )
    wal_bytes = int(database["wal_bytes"])
    retained_wal_growth = max(0, int(database["wal_retained_growth_bytes"]))
    measured_growth = relation_growth + retained_wal_growth
    projected_daily_growth = measured_growth / measured_intervals * 288
    capacity = int(filesystem_after["usable_capacity_bytes"])
    target = int(capacity * 0.80)
    headroom = max(0, target - int(filesystem_after["used_bytes"]))
    days = (
        headroom / projected_daily_growth
        if projected_daily_growth > 0
        else None
    )
    return {
        "filesystem_path": filesystem_after["path"],
        "filesystem_capacity_bytes": capacity,
        "filesystem_usable_capacity_bytes": capacity,
        "filesystem_raw_capacity_bytes": int(
            filesystem_after["raw_capacity_bytes"]
        ),
        "target_utilization": 0.80,
        "target_used_bytes": target,
        "filesystem_used_bytes_before": int(filesystem_before["used_bytes"]),
        "filesystem_used_bytes_after": int(filesystem_after["used_bytes"]),
        "filesystem_growth_bytes": max(
            0,
            int(filesystem_after["used_bytes"])
            - int(filesystem_before["used_bytes"]),
        ),
        "postgres_relation_growth_bytes": relation_growth,
        "postgres_wal_bytes": wal_bytes,
        "postgres_wal_retained_growth_bytes": retained_wal_growth,
        "local_spool_bytes": int(spool.get("final_bytes", 0))
        + int(spool.get("temporary_bytes", 0)),
        "remote_bucket_bytes_excluded": int(archived_bytes),
        "measured_local_growth_bytes": measured_growth,
        "projected_daily_local_growth_bytes": projected_daily_growth,
        "days_to_80_percent": days,
        "measurement_intervals_per_day": 288,
        "checks": {
            "capacity_measured": capacity > 0,
            "usable_capacity_measured": capacity > 0,
            "usable_capacity_excludes_reserved": capacity
            <= int(filesystem_after["raw_capacity_bytes"]),
            "target_is_80_percent": target == int(capacity * 0.80),
            "relation_sizes_present": bool(database.get("relation_sizes")),
            "wal_present": "wal_bytes" in database,
            "wal_retained_growth_present": "wal_retained_growth_bytes" in database,
            "spool_reported_separately": True,
            "remote_bucket_excluded": True,
        },
    }


def _host_provenance() -> dict[str, Any]:
    memory: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value, unit = line.split(maxsplit=2)
            if name in {"MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"}:
                memory[name.removesuffix(":").lower()] = int(value) * (
                    1024 if unit == "kB" else 1
                )
    except (OSError, ValueError):
        memory = {}
    swap_devices: list[dict[str, int | str]] = []
    try:
        for line in Path("/proc/swaps").read_text().splitlines()[1:]:
            path, _kind, size, used, priority = line.split()
            swap_devices.append(
                {
                    "path": path,
                    "size_bytes": int(size) * 1024,
                    "used_bytes": int(used) * 1024,
                    "priority": int(priority),
                }
            )
    except (OSError, ValueError):
        swap_devices = []
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "uname": dict(platform.uname()._asdict()),
        "memory_bytes": memory,
        "swap": {"devices": swap_devices, "total_used_bytes": sum(item["used_bytes"] for item in swap_devices)},
    }


def _parse_memory_events(value: str) -> dict[str, int]:
    return {
        key: int(count)
        for key, count in (line.split() for line in value.splitlines())
    }


def _memory_pressure(database_url: str) -> dict[str, int]:
    import psycopg

    host = _host_provenance()
    process_events: dict[str, int] = {}
    process_swap = 0
    try:
        cgroup = next(
            line.split(":", 2)[2]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        cgroup_path = Path("/sys/fs/cgroup") / cgroup.lstrip("/")
        process_events = _parse_memory_events(
            (cgroup_path / "memory.events").read_text()
        )
        process_swap = int((cgroup_path / "memory.swap.current").read_text())
    except (OSError, StopIteration, ValueError):
        process_events = {}
    database_events: dict[str, int] = {}
    database_swap = 0
    try:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                """
                SELECT pg_read_file('/sys/fs/cgroup/memory.events'),
                       pg_read_file('/sys/fs/cgroup/memory.swap.current')
                """
            ).fetchone()
        database_events = _parse_memory_events(_text(row[0]))
        database_swap = int(_text(row[1]))
    except (psycopg.Error, ValueError):
        database_events = {}
    return {
        "host_swap_used_bytes": int(host["swap"]["total_used_bytes"]),
        "process_cgroup_available": int(bool(process_events)),
        "process_swap_used_bytes": process_swap,
        "process_oom": process_events.get("oom", 0),
        "process_oom_kill": process_events.get("oom_kill", 0),
        "database_cgroup_available": int(bool(database_events)),
        "database_swap_used_bytes": database_swap,
        "database_oom": database_events.get("oom", 0),
        "database_oom_kill": database_events.get("oom_kill", 0),
    }


def _memory_pressure_delta(
    before: dict[str, int], after: dict[str, int]
) -> dict[str, int]:
    keys = (
        "process_swap_used_bytes",
        "process_oom",
        "process_oom_kill",
        "database_swap_used_bytes",
        "database_oom",
        "database_oom_kill",
    )
    return {
        key: max(0, after.get(key, 0) - before.get(key, 0)) for key in keys
    }


def _memory_pressure_failure_codes(
    before: dict[str, int], after: dict[str, int], delta: dict[str, int]
) -> list[str]:
    failures = []
    if any(
        before[key] == 0 or after[key] == 0
        for key in ("process_cgroup_available", "database_cgroup_available")
    ):
        failures.append("memory_pressure_unavailable")
    if any(delta.values()):
        failures.append("memory_pressure_increased")
    return failures


def _postgres_provenance(connection_info: str) -> dict[str, Any]:
    import psycopg

    settings = (
        "server_version",
        "server_version_num",
        "shared_buffers",
        "work_mem",
        "maintenance_work_mem",
        "max_connections",
        "track_io_timing",
    )
    with psycopg.connect(connection_info) as connection:
        version = _text(connection.execute("SELECT version()").fetchone()[0])
        rows = connection.execute(
            "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
            (list(settings),),
        ).fetchall()
        migration_rows = connection.execute(
            """
            SELECT version
            FROM clash_lens_schema_migrations
            ORDER BY version
            """
        ).fetchall()
    applied = [int(row[0]) for row in migration_rows]
    if tuple(applied) != REQUIRED_MIGRATION_VERSIONS:
        raise RuntimeError("database migrations are incomplete or out of date")
    return {
        "version": version,
        "settings": {_text(row[0]): _text(row[1]) for row in rows},
        "applied_migration_versions": applied,
    }


def _candidate_receipt_provenance(
    path: Path | None, source_sha: str, migrations: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if path is None:
        return [], None
    try:
        from scripts import deployment_receipt

        payload = json.loads(path.read_text(encoding="utf-8"))
        deployment_receipt.validate_receipt(payload, require_digest=True)
    except Exception as error:
        raise RuntimeError("candidate receipt is invalid or unavailable") from error
    if payload.get("receipt_scope") != "candidate-preparation":
        raise RuntimeError("candidate receipt has the wrong scope")
    source = payload.get("source")
    if not isinstance(source, dict) or source.get("revision") != source_sha:
        raise RuntimeError("candidate receipt source does not match this checkout")
    receipt_migrations = payload.get("migrations")
    if not isinstance(receipt_migrations, list) or len(receipt_migrations) != len(migrations):
        raise RuntimeError("candidate receipt migrations do not match this checkout")
    for expected, actual in zip(migrations, receipt_migrations, strict=True):
        if (
            not isinstance(actual, dict)
            or actual.get("filename") != expected["name"]
            or actual.get("sha256") != expected["sha256"]
            or actual.get("applied") is not True
        ):
            raise RuntimeError("candidate receipt migrations do not match this checkout")
    application_images = payload.get("application_images")
    if not isinstance(application_images, dict):
        raise TypeError("candidate receipt images are unavailable")
    prepared: list[dict[str, Any]] = []
    for application in ("collector", "python", "website"):
        identity = application_images.get(application)
        if not isinstance(identity, dict):
            raise TypeError("candidate receipt images are incomplete")
        prepared.append(
            {
                "application": application,
                "identity_type": "prepared_candidate_image_id",
                "requested_reference": identity["requested_reference"],
                "image_id": identity["image_id"],
                "registry_digest": identity["registry_digest"],
                "source_label": identity["source_label"],
                "revision_label": identity["revision_label"],
            }
        )
    return prepared, {
        "schema_version": payload["schema_version"],
        "receipt_scope": payload["receipt_scope"],
        "receipt_digest": payload["receipt_digest"],
        "source_sha": source_sha,
    }


def _provenance(
    arguments: argparse.Namespace,
    *,
    postgres: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_sha = _clean_source()
    migrations = _source_migrations()
    applied_migrations = None
    if postgres is not None:
        applied_migrations = postgres.get("applied_migration_versions")
        if applied_migrations != list(REQUIRED_MIGRATION_VERSIONS):
            raise RuntimeError("database migration state is incomplete or out of date")
    configuration = {
        "mode": arguments.mode,
        "post_fix": bool(arguments.post_fix),
        "populations": arguments.populations,
        "duplicate_observations": arguments.duplicate_observations,
        "live_jobs": arguments.live_jobs,
        "backfill_jobs": arguments.backfill_jobs,
        "army_facts": arguments.army_facts,
        "lanes": arguments.lanes,
        "effective_lanes": (
            min(arguments.lanes, 32)
            if arguments.mode == "mixed-backfill"
            else arguments.lanes
        ),
        "army_warmups": getattr(arguments, "army_warmups", STEP5_WARMUPS),
        "army_requests": getattr(arguments, "army_requests", STEP5_REQUESTS),
        "analytics_lanes": getattr(arguments, "analytics_lanes", STEP5_ANALYTICS_LANES),
        "duplicate_cycles": arguments.duplicate_cycles,
        "duplicate_endpoint_mix": dict(DUPLICATE_ENDPOINT_MIX),
        "skip_collector_probe": arguments.skip_collector_probe,
    }
    configuration_fingerprint = _sha(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    )
    host = _host_provenance()
    prepared_images, candidate_receipt = _candidate_receipt_provenance(
        getattr(arguments, "candidate_receipt", None), source_sha, migrations
    )
    execution = {
        "kind": "host",
        "executor_images": [],
        "host_identity": {
            "platform": host["platform"],
            "uname": host["uname"],
        },
        "runtime": {"python": host["python"]},
        "postgres": None
        if postgres is None
        else {
            "version": postgres["version"],
            "settings": postgres["settings"],
        },
    }
    result = {
        "source_sha": source_sha,
        "source_dirty": False,
        "runner_sha256": _sha(Path(__file__).read_bytes()),
        "migrations": migrations,
        "applied_migration_versions": applied_migrations,
        "configuration_fingerprint": configuration_fingerprint,
        "configuration": configuration,
        "host": host,
        "execution": execution,
        "prepared_candidate_images": prepared_images,
        "candidate_receipt": candidate_receipt,
    }
    if postgres is not None:
        result["postgres"] = postgres
    return result


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    if not arguments.database_url:
        raise RuntimeError("--database-url or CLASHLENS_TEST_DATABASE_URL is required")
    # Check source and migration inputs before any expensive workload starts;
    # applied migration state is captured from the isolated schema below.
    source_sha = _clean_source()
    migrations = _source_migrations()
    _candidate_receipt_provenance(
        getattr(arguments, "candidate_receipt", None), source_sha, migrations
    )
    if arguments.mode == STEP5_MODE:
        started = datetime.now(tz=UTC)
        with count_sql_calls() as sql_calls:
            workload = _run_step5_army(arguments.database_url)
        workload["database"]["application_sql_calls"] = sql_calls[0]
        provenance = _provenance(arguments, postgres=workload["postgres"])
        finished = datetime.now(tz=UTC)
        artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "mode": arguments.mode,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "provenance": provenance,
            "execution": provenance["execution"],
            "prepared_candidate_images": provenance["prepared_candidate_images"],
            "candidate_receipt": provenance["candidate_receipt"],
            "official_api_requests": {"count": 0, "source": "committed fixtures"},
            "collector_probe": None,
            "samples": [],
            "army_read_sample": workload,
            "hard_failures": _failure_codes(workload.get("hard_failures", [])),
        }
        artifact["artifact_digest"] = _artifact_digest(artifact)
        validate_artifact(artifact)
        return artifact
    if arguments.mode in {"reset-boundary", "correction"}:
        validate_reset(arguments.populations, arguments.post_fix)
    from domain_test_support import domain_database

    started = datetime.now(tz=UTC)
    collector_probe = (
        None
        if arguments.mode == "coordinator-12500"
        else _collector_probe(arguments.skip_collector_probe)
    )
    duplicate_archive_probe = (
        _collector_archive_probe(arguments.duplicate_observations)
        if arguments.mode == "duplicate-heavy"
        else None
    )
    samples = []
    hard_failures: list[str] = []
    provenance: dict[str, Any] | None = None
    populations = (
        arguments.populations
        if arguments.mode in {"reset-boundary", "correction"}
        else [12_500]
        if arguments.mode == "coordinator-12500"
        else [0]
    )
    for population in populations:
        with (
            domain_database(
                arguments.database_url, include_coordinator=True
            ) as connection_info,
            archive_server() as archive,
        ):
            if provenance is None:
                provenance = _provenance(
                    arguments, postgres=_postgres_provenance(connection_info)
                )
            wal_start, statement_start, wal_retained_start = _start_metrics(connection_info)
            relation_start = _relation_snapshot(connection_info)
            filesystem_before = _filesystem_usage(ROOT)
            cpu_start = time.process_time()
            elapsed_start = time.perf_counter()
            with count_sql_calls() as sql_calls:
                if arguments.mode == "reset-boundary":
                    workload = _run_reset(connection_info, archive, population, False)
                elif arguments.mode == "coordinator-12500":
                    workload = _run_coordinator_writers(
                        connection_info, archive, population
                    )
                elif arguments.mode == "correction":
                    workload = _run_reset(connection_info, archive, population, True)
                elif arguments.mode == "duplicate-heavy":
                    workload = _run_duplicate(
                        connection_info,
                        archive,
                        arguments.duplicate_observations,
                        cycles=arguments.duplicate_cycles,
                    )
                    workload["collector_archive_operations"] = duplicate_archive_probe
                else:
                    workload = _run_mixed(
                        connection_info,
                        archive,
                        arguments.live_jobs,
                        arguments.backfill_jobs,
                    )
                hard_failures.extend(workload.get("hard_failures", []))
            if arguments.mode == "coordinator-12500":
                measurements = _db_snapshot(
                    connection_info,
                    wal_start,
                    statement_start,
                    relation_start,
                    wal_retained_start,
                )
                measurements["application_sql_calls"] = sql_calls[0]
                filesystem_after = _filesystem_usage(ROOT)
                samples.append(
                    {
                        "workload": workload,
                        "database": measurements,
                        "archive_operations": {
                            "get": 0,
                            "get_bytes": 0,
                            "head": 0,
                            "conditional_put": 0,
                            "put": 0,
                            "put_bytes": 0,
                            "conflicts": 0,
                        },
                        "storage_runway": _runway_inputs(
                            filesystem_before,
                            filesystem_after,
                            measurements,
                            relation_start,
                            {},
                            0,
                        ),
                        "evidence": {
                            "execution_method": workload["full_large_reset"]["execution_method"],
                            "writer_guard": post_fix_source_ready(),
                        },
                        "queue_residue": [],
                    }
                )
                continue
            measurements = _db_snapshot(
                connection_info,
                wal_start,
                statement_start,
                relation_start,
                wal_retained_start,
            )
            filesystem_after = _filesystem_usage(ROOT)
            measurements["application_sql_calls"] = sql_calls[0]
            if arguments.mode == "duplicate-heavy":
                hard_failures.extend(
                    _duplicate_hard_failure_codes(workload, measurements)
                )
            if "official_responses" not in workload:
                raise RuntimeError(
                    "workload did not report its exact official response count"
                )
            response_count = int(workload["official_responses"])
            spool_root_value = workload.pop("_spool_root", None)
            if not isinstance(spool_root_value, str) or not spool_root_value:
                raise RuntimeError("workload spool identity is unavailable")
            spool_root = Path(spool_root_value)
            # Executed responses actually ran through the full pipeline;
            # projected ones are represented by measured-cycle aggregation
            # (duplicate-heavy 24h-equivalent mode). Never mix the two.
            executed_count = int(
                workload.get(
                    "executed_observations",
                    workload.get("official_responses", response_count),
                )
            )
            projected_count = max(0, response_count - executed_count)
            distinct_hashes = len(archive[3].objects)
            spool = workload.get("spool", {})
            counters = workload.get("evidence_counters", {})
            stages = workload.get("stage_metrics", {})
            # Read latency covers the full local-verify-or-fallback path;
            # repair latency is measured separately inside the spool reader.
            latency = {
                name: (float(stages[stage]["average_ms"]) if stage in stages else None)
                for name, stage in {
                    "python_read": "python_archive_get_verify",
                    "local_verify": "python_archive_local_verify",
                    "transaction": "python_domain_profile",
                    "repair": "python_archive_repair",
                }.items()
            }
            probe = duplicate_archive_probe or {}
            if probe.get("executed"):
                # Collector-side raw-evidence latencies are measured inside the
                # checked-in Go probe; python-side stage latencies above come
                # from the StageMetrics histograms of this workload.
                latency.update(
                    {
                        "collector_hashing_us": probe["hash_us"],
                        "collector_operation_total_us": probe["operation_total_us"],
                        "collector_remote_put_us": probe["stage_put_us"],
                        "collector_get_verify_us": probe["stage_get_verify_us"],
                        "collector_local_verify_us": probe["local_verify_us"],
                    }
                )
            orphans = _orphan_metrics(connection_info, spool_root)
            processing_summary = workload.get("processing_summary", {})
            if "total" in processing_summary:
                processing_summary = processing_summary["total"]
            retries_measured = int(processing_summary.get("retry_count", 0))
            archived_bytes = sum(map(len, archive[3].objects.values()))
            exact_bytes = int(workload.get("exact_bytes", archived_bytes))
            storage_runway = _runway_inputs(
                filesystem_before,
                filesystem_after,
                measurements,
                relation_start,
                spool,
                archived_bytes,
                measured_intervals=int(workload.get("measured_cycles", 1)),
            )
            samples.append(
                {
                    "workload": workload,
                    "database": measurements,
                    "archive_operations": {
                        "get": archive[3].gets,
                        "get_bytes": archive[3].get_bytes,
                        "head": archive[3].heads,
                        "conditional_put": archive[3].conditional_puts,
                        "put": archive[3].puts,
                        "put_bytes": archive[3].put_bytes,
                        "conflicts": archive[3].conflicts,
                    },
                    "storage_runway": storage_runway,
                    "evidence": {
                        "response_count": response_count,
                        "executed_responses": executed_count,
                        "projected_responses": projected_count,
                        "execution_method": workload.get(
                            "aggregation_method", "exact bounded cycle"
                        ),
                        "distinct_hashes": distinct_hashes,
                        "novelty_rate": (
                            distinct_hashes / executed_count if executed_count else 0.0
                        ),
                        "exact_bytes": exact_bytes,
                        "archived_bytes": archived_bytes,
                        "pending_verification_count": measurements.get(
                            "pending_remote_verification"
                        ),
                        "pending_verification_age_seconds": _pending_age_seconds(
                            connection_info
                        ),
                        "orphan_count": orphans["count"],
                        "orphan_bytes": orphans["bytes"],
                        "local_hits": counters.get("local_hits", 0),
                        "local_misses": counters.get("local_misses", archive[3].gets),
                        "repairs": counters.get("repairs", 0),
                        "provider_errors": counters.get("provider_errors", 0),
                        "retries": retries_measured,
                        "concurrency_lanes": _LANES,
                        "latency_ms": latency,
                    },
                    "spool": {
                        "final_bytes": int(spool.get("final_bytes", 0)),
                        "temporary_bytes": int(spool.get("temporary_bytes", 0)),
                        "high_water_bytes": int(spool.get("high_water_bytes", 0)),
                        "final_object_count": int(spool.get("final_objects", 0)),
                        "temporary_object_count": int(
                            spool.get("temporary_objects", 0)
                        ),
                        "live_reservations": int(spool.get("reserved_objects", 0)),
                        "allocated_blocks": spool.get("allocated_blocks"),
                        "free_inodes": spool.get(
                            "free_inodes", _free_inodes(spool_root)
                        ),
                    },
                    "elapsed_seconds": time.perf_counter() - elapsed_start,
                    "cpu_seconds": time.process_time() - cpu_start,
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                }
            )
    army_read_sample = (
        _retained_army_read_sample(arguments.database_url, arguments.army_facts)
        if arguments.mode in {"reset-boundary", "correction"}
        else None
    )
    if army_read_sample is not None:
        hard_failures.extend(army_read_sample["hard_failures"])
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "mode": arguments.mode,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "provenance": provenance,
        "execution": provenance["execution"],
        "prepared_candidate_images": provenance["prepared_candidate_images"],
        "candidate_receipt": provenance["candidate_receipt"],
        "official_api_requests": {"count": 0, "source": "committed fixtures"},
        "collector_probe": collector_probe,
        "samples": samples,
        "army_read_sample": army_read_sample,
        "hard_failures": _failure_codes(hard_failures),
    }
    artifact["artifact_digest"] = _artifact_digest(artifact)
    validate_artifact(artifact)
    return artifact


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument(
        "--database-url", default=os.environ.get("CLASHLENS_TEST_DATABASE_URL")
    )
    parser.add_argument("--populations", default="2,4,8")
    parser.add_argument("--post-fix", action="store_true")
    parser.add_argument("--duplicate-observations", type=int, default=20)
    parser.add_argument("--live-jobs", type=int, default=5)
    parser.add_argument("--backfill-jobs", type=int, default=20)
    parser.add_argument("--army-facts", type=int, default=1_000)
    parser.add_argument("--lanes", type=int, default=32)
    parser.add_argument("--duplicate-cycles", type=int, default=1)
    parser.add_argument("--army-warmups", type=int, default=STEP5_WARMUPS)
    parser.add_argument("--army-requests", type=int, default=STEP5_REQUESTS)
    parser.add_argument("--analytics-lanes", type=int, default=STEP5_ANALYTICS_LANES)
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="deprecated; candidate image identities must come from --candidate-receipt",
    )
    parser.add_argument(
        "--candidate-receipt",
        type=Path,
        help="candidate-preparation receipt for separately reported prepared images",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--skip-collector-probe", action="store_true", help=argparse.SUPPRESS
    )
    arguments = parser.parse_args(argv)
    try:
        arguments.populations = [
            int(value) for value in arguments.populations.split(",")
        ]
    except ValueError:
        parser.error("--populations must contain integers")
    if arguments.mode in {"reset-boundary", "correction"}:
        try:
            validate_reset(arguments.populations, arguments.post_fix)
        except ValueError as error:
            parser.error(str(error))
    if (
        arguments.duplicate_observations < 2
        or arguments.duplicate_observations > DUPLICATE_EXECUTION_CAP
        or arguments.live_jobs < 1
        or arguments.backfill_jobs < 1
    ):
        parser.error(
            "workload counts must be positive; duplicates must be between 2 and "
            f"{DUPLICATE_EXECUTION_CAP}"
        )
    if not 1 <= arguments.army_facts <= 100_000:
        parser.error("--army-facts must be between 1 and 100000")
    if not 1 <= arguments.lanes <= 64:
        parser.error("--lanes must be between 1 and 64")
    if arguments.duplicate_cycles < 1 or arguments.duplicate_cycles > 4:
        parser.error("--duplicate-cycles must be between 1 and 4")
    if arguments.mode == STEP5_MODE and (
        arguments.army_warmups != STEP5_WARMUPS
        or arguments.army_requests != STEP5_REQUESTS
        or arguments.analytics_lanes != STEP5_ANALYTICS_LANES
    ):
        parser.error("issue #73 army workload requires 4 lanes, 5 warmups, and 100 requests")
    if arguments.analytics_lanes < 1:
        parser.error("--analytics-lanes must be positive")
    global _LANES
    _LANES = arguments.lanes
    if arguments.image:
        parser.error(
            "--image is deprecated and ambiguous; use --candidate-receipt"
        )
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    if not arguments.database_url:
        print(
            "performance runner: --database-url or CLASHLENS_TEST_DATABASE_URL is required",
            file=sys.stderr,
        )
        return 2
    try:
        import psycopg
    except ImportError:
        print("performance runner: ImportError", file=sys.stderr)
        return 2
    try:
        result = run(arguments)
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if arguments.output:
            _write_artifact(arguments.output, payload)
        else:
            print(payload, end="")
        hard_failures = list(result.get("hard_failures", []))
        if hard_failures:
            print(
                "performance runner: hard acceptance failures: " + "; ".join(hard_failures[:8]),
                file=sys.stderr,
            )
            return 2
        return 0
    except (
        RuntimeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        psycopg.Error,
    ) as error:
        print(f"performance runner: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
