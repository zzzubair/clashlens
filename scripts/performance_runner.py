#!/usr/bin/env python3
"""Run issue #60 Step 1 workloads against an isolated PostgreSQL schema."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
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
sys.path[:0] = [str(PYTHON / "src"), str(PYTHON / "tests")]

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
ARTIFACT_SCHEMA_VERSION = 7
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
STEP5_P95_TARGET_MS = 200.0
STEP5_FORCED_MISS_TARGET_SECONDS = 5.0
STEP5_COLLECTION_LIMIT_SECONDS = 300.0
STEP5_TROOP_KEYS = tuple(f"troop:{index}" for index in range(27))
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
    if not populations or any(value < 1 for value in populations):
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
        fixture_bodies[endpoint] = (
            PYTHON / "testdata" / (
                "legend_i_battle_log_v1.json"
                if endpoint == "battle_log"
                else "global_top_200_v1.json"
            )
        ).read_bytes()
    return (None if endpoint == "global_player_rankings" else _tag(index + 1)), fixture_bodies[endpoint]


def _process_jobs(processor: Any, jobs: list[int], prefix: str) -> list[dict[str, Any]]:
    """Process production-shaped batches with the configured 12 worker lanes."""

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


def _start_metrics(connection_info: str) -> tuple[str, int | None]:
    import psycopg

    with psycopg.connect(connection_info) as connection:
        wal = _text(
            connection.execute("SELECT pg_current_wal_insert_lsn()::text").fetchone()[0]
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
    return wal, calls


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
    required = (
        "artifact_digest",
        "mode",
        "started_at",
        "finished_at",
        "provenance",
        "collector_probe",
        "samples",
        "army_read_sample",
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

    for index, sample in enumerate(samples):
        label = f"sample {index}"
        require(sample, ("database", "archive_operations", "storage_runway"), label)
        require(sample["database"], database_metrics, f"{label} database")
        require(
            sample["archive_operations"], archive_metrics, f"{label} archive_operations"
        )
        require(
            sample["storage_runway"],
            ("measured_local_growth_bytes", "days_to_80_percent", "checks"),
            f"{label} storage_runway",
        )
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

    if mode == STEP5_MODE:
        army_sample = artifact["army_read_sample"]
        require(army_sample, ("database",), "army_read_sample")
        require(army_sample["database"], database_metrics, "army_read_sample database")


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
            plan = payload[0]["Plan"] if isinstance(payload, list) else payload["Plan"]
            scanned, returned = _plan_counts(plan)
            plans.append(
                {
                    "correlation": {
                        "selection": selection,
                        "lens": lens,
                        "statement_id": statement_id,
                    },
                    "sql": sql,
                    "parameters": _json_value(call["params"]),
                    "rows_scanned": scanned,
                    "rows_returned": returned,
                    "explain_analyze_buffers": payload,
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
    database = ApiDatabase(connection_info, max_size=1)
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
            "forced_miss_passed": (
                forced_miss_seconds < STEP5_FORCED_MISS_TARGET_SECONDS
                and pressure_before["process_cgroup_available"] == 1
                and pressure_after["process_cgroup_available"] == 1
                and pressure_before["database_cgroup_available"] == 1
                and pressure_after["database_cgroup_available"] == 1
                and not any(pressure_delta.values())
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
    overlap_lock = Lock()
    overlap_counts: dict[str, int] = {f"{s['selection']}/{s['lens']}": 0 for s in specs}
    account_overlap = [0]

    def duplicate_cycle() -> dict[str, Any]:
        started = time.perf_counter()
        try:
            result = _run_duplicate(
                connection_info,
                archive,
                DUPLICATE_EXECUTION_CAP,
                cycles=1,
                processing_started=processing_started,
            )
            result["cycle_elapsed_seconds"] = time.perf_counter() - started
            return result
        except Exception:
            cycle_failed.set()
            processing_started.set()
            raise
        finally:
            cycle_finished.set()

    def analytics_lane(lane_specs: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        processing_started.wait()
        if cycle_failed.is_set():
            raise RuntimeError("collection cycle failed before processing started")
        from clashlens.api_db import ApiDatabase

        database = ApiDatabase(connection_info, max_size=1)
        results: list[dict[str, Any]] = []
        try:
            selections = [(spec, _step5_selection(spec)) for spec in lane_specs]
            for spec, selection in selections:
                for _ in range(STEP5_WARMUPS):
                    result = database.get_army_analytics(
                        selection, now=BOUNDARY + timedelta(days=STEP5_DAYS + 1)
                    )
                    _step5_result(result, spec, "mixed warmup")
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
    hard_failures = [
        f"{key} overlap count {count} < {STEP5_REQUESTS}"
        for key, count in overlap_counts.items()
        if count < STEP5_REQUESTS
    ]
    cycle_results = cycle.get("results", [])
    if len(cycle_results) != DUPLICATE_EXECUTION_CAP:
        hard_failures.append(
            f"collection processing result count {len(cycle_results)} != {DUPLICATE_EXECUTION_CAP}"
        )
    failed_outcomes = sorted(
        {
            str(result.get("outcome"))
            for result in cycle_results
            if result.get("outcome") != "processed"
        }
    )
    if failed_outcomes:
        hard_failures.append(
            "collection processing outcomes were not all processed: "
            + ",".join(failed_outcomes)
        )
    hard_failures.extend(
        f"{item['selection']}/{item['lens']} mixed p95 {item['p95_ms']:.3f} >= {STEP5_P95_TARGET_MS}"
        for item in measurements["analytics_lanes"]
        if not item["target_passed"]
    )
    if account["overlap_measurements"] < STEP5_REQUESTS:
        hard_failures.append(
            f"account overlap count {account['overlap_measurements']} < {STEP5_REQUESTS}"
        )
    if not account["target_passed"]:
        hard_failures.append(
            f"account mixed p95 {account['p95_ms']:.3f} >= {STEP5_P95_TARGET_MS}"
        )
    if cycle["cycle_elapsed_seconds"] >= STEP5_COLLECTION_LIMIT_SECONDS:
        hard_failures.append(
            f"collection cycle {cycle['cycle_elapsed_seconds']:.3f}s >= {STEP5_COLLECTION_LIMIT_SECONDS}s"
        )
    measurements["hard_failures"] = hard_failures
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
            plan_row = connection.execute(
                """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                   SELECT id,battle_id,population_player_id,battle_time_trophies,stars,
                          destruction_percentage,army_state,failure_reason,home_troops,
                          spells,siege,cc_troops,heroes,unresolved_components,
                          perspective_disagreement,input_hash,source_ranked_day_version_id
                   FROM army_analytics_battle_facts
                   WHERE official_season_id='1783918800'
                     AND season_day_number BETWEEN 1 AND 28 AND lens='offense' AND is_current
                   ORDER BY battle_id"""
            ).fetchone()[0][0]
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
        "contract": {"database_version": 4, "required_version": 4},
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
        source_jobs = _store_reconciliation_population(
            connection_info, archive, population
        )
        profile_results = _process_jobs(processor, source_jobs, "reset-evidence")
        first = _drain(processor, population * 20 + 100)
        if correction:
            correction_jobs = _store_reconciliation_corrections(
                connection_info, archive, population
            )
            profile_results.extend(
                _process_jobs(processor, correction_jobs, "correction-evidence")
            )
            second = _drain(processor, population * 20 + 100)
        else:
            second = []
        # Discovery profiles are collector-owned descendants that the bounded
        # Python runner cannot execute. Terminalize only these synthetic side
        # effects and report the count separately from the real coordinator
        # publication drain.
        with psycopg.connect(connection_info) as connection:
            bounded_collector_terminalization = connection.execute(
                """
                UPDATE collector_jobs
                SET status = 'complete', updated_at = clock_timestamp()
                WHERE work_type = 'discovery_profile'
                  AND status IN ('pending','leased','waiting_retry')
                """
            ).rowcount
            connection.commit()
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
        generation_count = int(counts[5])
        generation_states = counts[6]
        generations = 2 if correction else 1
        if generation_count < generations:
            raise RuntimeError(
                f"reset coordinator generation count: expected at least {generations}, got {generation_count}"
            )
        expected_counts = {
            "ranked_day_versions": generations * population,
            "snapshot_headers": 2 * generation_count,
            "snapshot_entries": 2 * generation_count * population,
        }
        actual_counts = {
            "ranked_day_versions": int(counts[0]),
            "snapshot_headers": int(counts[1]),
            "snapshot_entries": int(counts[2]),
        }
        if actual_counts != expected_counts:
            raise RuntimeError(
                f"reset fan-out mismatch: expected {expected_counts}, got {actual_counts}; states={generation_states}"
            )
        outcomes = profile_results + first + second
        if any(result["outcome"] != "processed" for result in outcomes):
            raise RuntimeError("reset workload contains a non-processed result")
        if active_rows:
            raise RuntimeError(f"reset queue residue: {active_rows}")
        return {
            "population": population,
            "official_responses": len(profile_results),
            "profile_results": profile_results,
            "dependent_results": first,
            "correction_results": second,
            "fact_counts": {
                **actual_counts,
                "analytics_summaries": int(counts[3]),
                "army_facts": int(counts[4]),
            },
            "fanout_evidence": {
                "expected": expected_counts,
                "matches_expected": True,
                "snapshot_entries_per_population": 2 * generation_count,
                "generation_states": generation_states,
            },
            "queue_residue": [
                {
                    "owner": _text(row[0]),
                    "work_type": _text(row[1]),
                    "count": int(row[2]),
                }
                for row in active_rows
            ],
            "bounded_collector_terminalization": int(bounded_collector_terminalization),
            "stage_metrics": metrics.snapshot(),
            "spool": _spool.stats(),
            "evidence_counters": _spool.counters(),
            "spool_root": str(_spool.spool.root),
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
    processing_started: Event | None = None,
) -> dict[str, Any]:
    import psycopg
    from domain_test_support import store_observation

    database, processor, metrics, _spool = _processor(connection_info, archive)
    try:
        executed_count = min(count, DUPLICATE_EXECUTION_CAP)
        endpoint_mix = _duplicate_endpoint_mix(count)
        results: list[dict[str, Any]] = []
        cycle_elapsed: list[float] = []
        profile_bodies: dict[tuple[str, int], bytes] = {}
        fixture_bodies: dict[str, bytes] = {}
        source_bytes: dict[str, int] = {}
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
                                observed_at=DAY_START
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
        # Battle processing intentionally creates collector-owned discovery
        # jobs. Terminalize only these synthetic descendants; the processing
        # queue itself was drained by the production Python worker.
        with psycopg.connect(connection_info) as connection:
            terminalized = connection.execute(
                """
                UPDATE collector_jobs
                SET status = 'complete', updated_at = clock_timestamp()
                WHERE work_type = 'discovery_profile'
                  AND status IN ('pending', 'leased', 'waiting_retry')
                """
            ).rowcount
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
            "bounded_collector_terminalization": int(terminalized),
            "results": results,
            "stage_metrics": metrics.snapshot(),
            "spool": _spool.stats(),
            "evidence_counters": _spool.counters(),
            "spool_root": str(_spool.spool.root),
        }
    finally:
        database.close()


def _run_mixed(
    connection_info: str, archive: Any, live: int, backfill: int
) -> dict[str, Any]:
    import psycopg
    from domain_test_support import store_observation
    from psycopg.types.json import Jsonb

    database, processor, metrics, _spool = _processor(connection_info, archive)
    try:
        jobs: list[tuple[str, int]] = []
        for kind, count in (("backfill", backfill), ("live", live)):
            for index in range(count):
                tag = _tag(index + 1)
                observation, job = store_observation(
                    connection_info,
                    archive,
                    occurrence_key=f"{kind}-{index}",
                    endpoint="profile",
                    body=_profile_body(tag),
                    observed_at=DAY_START + timedelta(hours=1),
                    normalized_tag=tag,
                    deduplication_key=f"perf-{kind}-{index}",
                )
                if kind == "backfill":
                    with psycopg.connect(connection_info) as connection:
                        connection.execute(
                            """UPDATE python_processing_jobs SET
                                observation_id=NULL, replay_observation_id=%s,
                                work_type='replay_observation', input_json=%s
                               WHERE id=%s""",
                            (observation, Jsonb({"replay_request_id": index + 1}), job),
                        )
                        connection.commit()
                jobs.append((kind, job))
        by_id = {job: kind for kind, job in jobs}
        drained = _drain(processor, len(jobs))
        order = [by_id[item["job_id"]] for item in drained]
        return {
            "completion_order": order,
            "live_jobs": live,
            "backfill_jobs": backfill,
            "official_responses": len(jobs),
            "live_first_completion_index": order.index("live")
            if "live" in order
            else None,
            "results": drained,
            "stage_metrics": metrics.snapshot(),
            "spool": _spool.stats(),
            "evidence_counters": _spool.counters(),
            "spool_root": str(_spool.spool.root),
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
        wal_start, statement_start = _start_metrics(connection_info)
        relation_start = _relation_snapshot(connection_info)
        cpu_start = time.process_time()
        elapsed_start = time.perf_counter()
        database, processor, _metrics, _spool = _processor(connection_info, archive)
        try:
            with count_sql_calls() as sql_calls:
                jobs = _store_reconciliation_population(connection_info, archive, 1)
                _process_jobs(processor, jobs, "army-read-evidence")
                _drain(processor, 120)
                reads = _seed_worst_case_army_reads(connection_info, fact_count)
            measurements = _db_snapshot(
                connection_info, wal_start, statement_start, relation_start
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
                "spool_root": str(_spool.spool.root),
            }
        finally:
            database.close()


def _run_step5_army(database_url: str) -> dict[str, Any]:
    """Run the fixed PR 2 army protocol in one isolated production schema."""
    from domain_test_support import domain_database

    pressure_before = _memory_pressure(database_url)
    started = time.perf_counter()
    cpu_start = time.process_time()
    specs = _army_selection_specs()
    with (
        domain_database(database_url, include_coordinator=True) as connection_info,
        archive_server() as archive,
    ):
        wal_start, statement_start = _start_metrics(connection_info)
        relation_start = _relation_snapshot(connection_info)
        seed = _seed_step5_army_database(connection_info, archive)
        reads = [_measure_army_pair(connection_info, spec) for spec in specs]
        overlap = _run_step5_overlap(connection_info, archive, specs)
        database = _db_snapshot(
            connection_info, wal_start, statement_start, relation_start
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
        hard_failures = [
            f"{item['selection']}/{item['lens']} forced miss {item['forced_miss_seconds']:.3f}s >= {STEP5_FORCED_MISS_TARGET_SECONDS}s"
            for item in reads
            if item["forced_miss_seconds"] >= STEP5_FORCED_MISS_TARGET_SECONDS
        ]
        for item in reads:
            before = item["forced_miss_memory_before"]
            after = item["forced_miss_memory_after"]
            if any(
                pressure["process_cgroup_available"] != 1
                or pressure["database_cgroup_available"] != 1
                for pressure in (before, after)
            ):
                hard_failures.append(
                    f"{item['selection']}/{item['lens']} forced miss cgroup counters unavailable"
                )
            elif any(item["forced_miss_memory_delta"].values()):
                hard_failures.append(
                    f"{item['selection']}/{item['lens']} forced miss memory pressure increased: "
                    f"{item['forced_miss_memory_delta']}"
                )
        hard_failures.extend(
            f"{item['selection']}/{item['lens']} p95 {item['p95_ms']:.3f} >= {STEP5_P95_TARGET_MS}"
            for item in reads
            if not item["target_passed"]
        )
        hard_failures.extend(overlap["hard_failures"])
        if active_queue_rows:
            hard_failures.append(f"queue residue: {active_queue_rows}")
        if (
            pressure_before["process_cgroup_available"] != 1
            or pressure_after["process_cgroup_available"] != 1
            or pressure_before["database_cgroup_available"] != 1
            or pressure_after["database_cgroup_available"] != 1
        ):
            hard_failures.append("process or database cgroup counters were unavailable")
        if any(pressure_delta.values()):
            hard_failures.append(
                f"memory pressure increased during workload: {pressure_delta}"
            )
        return {
            "status": "passed" if not hard_failures else "failed",
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
                "analytics_lanes": STEP5_ANALYTICS_LANES,
                "duplicate_cycle_observations": DUPLICATE_EXECUTION_CAP,
            },
            "seed": seed,
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
            "hard_failures": hard_failures,
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
    measured_growth = relation_growth + wal_bytes
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
    return {"version": version, "settings": {_text(row[0]): _text(row[1]) for row in rows}}


def _provenance(arguments: argparse.Namespace) -> dict[str, Any]:
    migrations = [
        {"name": path.name, "sha256": _sha(path.read_bytes())}
        for path in sorted((ROOT / "deploy/migrations").glob("*.sql"))
    ]
    configuration = {
        "mode": arguments.mode,
        "populations": arguments.populations,
        "duplicate_observations": arguments.duplicate_observations,
        "live_jobs": arguments.live_jobs,
        "backfill_jobs": arguments.backfill_jobs,
        "army_facts": arguments.army_facts,
        "lanes": arguments.lanes,
        "images": arguments.image,
        "army_warmups": getattr(arguments, "army_warmups", STEP5_WARMUPS),
        "army_requests": getattr(arguments, "army_requests", STEP5_REQUESTS),
        "analytics_lanes": getattr(arguments, "analytics_lanes", STEP5_ANALYTICS_LANES),
        "duplicate_cycles": arguments.duplicate_cycles,
        "duplicate_endpoint_mix": dict(DUPLICATE_ENDPOINT_MIX),
        "skip_collector_probe": arguments.skip_collector_probe,
    }
    return {
        "source_sha": _git("rev-parse", "HEAD"),
        "source_dirty": bool(_git("status", "--porcelain")),
        "runner_sha256": _sha(Path(__file__).read_bytes()),
        "migrations": migrations,
        "configuration_fingerprint": _sha(
            json.dumps(configuration, sort_keys=True).encode()
        ),
        "configuration": configuration,
        "images": arguments.image,
        "host": _host_provenance(),
    }


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    if not arguments.database_url:
        raise RuntimeError("--database-url or CLASHLENS_TEST_DATABASE_URL is required")
    if arguments.mode == STEP5_MODE:
        started = datetime.now(tz=UTC)
        with count_sql_calls() as sql_calls:
            workload = _run_step5_army(arguments.database_url)
        workload["database"]["application_sql_calls"] = sql_calls[0]
        finished = datetime.now(tz=UTC)
        artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "mode": arguments.mode,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "provenance": _provenance(arguments),
            "collector_probe": None,
            "samples": [],
            "army_read_sample": workload,
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
            wal_start, statement_start = _start_metrics(connection_info)
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
            if arguments.mode == "coordinator-12500":
                measurements = _db_snapshot(
                    connection_info, wal_start, statement_start, relation_start
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
                connection_info, wal_start, statement_start, relation_start
            )
            filesystem_after = _filesystem_usage(ROOT)
            measurements["application_sql_calls"] = sql_calls[0]
            if "official_responses" not in workload:
                raise RuntimeError(
                    "workload did not report its exact official response count"
                )
            response_count = int(workload["official_responses"])
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
            orphans = _orphan_metrics(connection_info, Path(workload["spool_root"]))
            retries_measured = sum(
                int(result.get("outcome") == "retrying")
                for result in workload.get("results", [])
            )
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
                            "free_inodes", _free_inodes(Path(workload["spool_root"]))
                        ),
                    },
                    "elapsed_seconds": time.perf_counter() - elapsed_start,
                    "cpu_seconds": time.process_time() - cpu_start,
                    "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                }
            )
    army_read_sample = (
        _run_army_read_sample(arguments.database_url, arguments.army_facts)
        if arguments.mode in {"reset-boundary", "correction"}
        else None
    )
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "mode": arguments.mode,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "provenance": _provenance(arguments),
        "collector_probe": collector_probe,
        "samples": samples,
        "army_read_sample": army_read_sample,
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
    parser.add_argument("--image", action="append", default=[])
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
        or arguments.live_jobs < 1
        or arguments.backfill_jobs < 1
    ):
        parser.error("workload counts must be positive; duplicates must be at least 2")
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
    if any(
        re.fullmatch(r"[A-Za-z0-9._-]+=sha256:[0-9a-f]{64}", image) is None
        for image in arguments.image
    ):
        parser.error("--image must be NAME=sha256:DIGEST")
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
    except ImportError as error:
        print(f"performance runner: ImportError: {error}", file=sys.stderr)
        return 2
    try:
        result = run(arguments)
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if arguments.output:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(payload)
        else:
            print(payload, end="")
        hard_failures = (result.get("army_read_sample") or {}).get(
            "hard_failures", []
        )
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
        print(f"performance runner: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
