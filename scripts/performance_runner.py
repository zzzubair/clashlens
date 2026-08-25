#!/usr/bin/env python3
"""Run issue #60 Step 1 workloads against an isolated PostgreSQL schema."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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
from threading import Thread
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "python"
sys.path[:0] = [str(PYTHON / "src"), str(PYTHON / "tests")]

MODES = ("reset-boundary", "correction", "duplicate-heavy", "mixed-backfill")
DUPLICATE_EXECUTION_CAP = 25_024
_LANES = 32
BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY_START = BOUNDARY - timedelta(days=1)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *arguments], check=True, text=True,
        capture_output=True,
    ).stdout.strip()


def post_fix_source_ready() -> bool:
    """Require both coordinator ownership and a set-based snapshot writer."""
    migrations = "\n".join(
        path.read_text() for path in sorted((ROOT / "deploy/migrations").glob("*.sql"))
    )
    database = (PYTHON / "src/clashlens/db.py").read_text()
    coordinator = "boundary_publication_generation" in migrations
    set_based = "INSERT INTO leaderboard_snapshot_entries" in database and (
        "executemany(" in database or "INSERT INTO leaderboard_snapshot_entries" in migrations
    )
    per_player_key_removed = "build_snapshot:ranked-day-version:" not in database
    return coordinator and set_based and per_player_key_removed


def validate_reset(populations: list[int], post_fix: bool) -> None:
    if not populations or any(value < 1 for value in populations):
        raise ValueError("--populations requires positive integers")
    if any(value >= 12_500 for value in populations):
        if not post_fix:
            raise ValueError("refusing reset population >= 12,500 without --post-fix")
        if not post_fix_source_ready():
            raise ValueError("--post-fix refused: coordinator and set-based writer are not present")


class _ArchiveHandler(BaseHTTPRequestHandler):
    objects: ClassVar[dict[str, bytes]] = {}
    gets = 0
    heads = 0
    puts = 0
    conditional_puts = 0
    conflicts = 0

    def log_message(self, format: str, *arguments: object) -> None:
        del format, arguments

    def do_GET(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = type(self).objects.get(key)
        type(self).gets += 1
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Amz-Meta-Sha256", _sha(body))
        self.end_headers(); self.wfile.write(body)

    def do_HEAD(self) -> None:
        type(self).heads += 1
        self.send_response(200); self.end_headers()

    def do_PUT(self) -> None:
        type(self).puts += 1
        if self.headers.get("If-None-Match") == "*": type(self).conditional_puts += 1
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if key in type(self).objects:
            type(self).conflicts += 1
            self.send_response(412); self.end_headers(); return
        type(self).objects[key] = body
        self.send_response(200); self.end_headers()


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
def archive_server() -> Iterator[tuple[str, str, str, type[_ArchiveHandler]]]:
    handler = type("PerformanceArchiveHandler", (_ArchiveHandler,), {"objects": {}, "gets": 0, "heads": 0, "puts": 0, "conditional_puts": 0, "conflicts": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", "", "", handler
    finally:
        server.shutdown(); thread.join(timeout=2); server.server_close()


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
        endpoint=archive[0], bucket="evidence", access_key="test", secret_key="test",
        secure=False, allow_insecure_test_origin=True, pool_size=_LANES,
    )
    spool = SpoolFirstReader(
        s3, spool_root=tempfile.mkdtemp(prefix="clashlens-perf-spool-"), stage_metrics=metrics
    )
    return database, ObservationProcessor(database, spool, metrics), metrics, spool


def _profile_body(tag: str, variant: int = 0) -> bytes:
    source = json.loads((PYTHON / "testdata/legend_i_profile_v1.json").read_text())
    source["tag"] = tag
    if variant:
        source["expLevel"] = int(source.get("expLevel", 1)) + variant
    return json.dumps(source, separators=(",", ":")).encode()


def _process_jobs(processor: Any, jobs: list[int], prefix: str) -> list[dict[str, Any]]:
    """Process production-shaped batches with the configured 12 worker lanes."""
    def process(index: int, job: int) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        result = processor.process_job(job, owner=f"perf-{prefix}-{index}", lease_seconds=300)
        if result is None:
            raise RuntimeError(f"job {job} was not claimable")
        return index, {
            "job_id": job, "outcome": result.outcome, "category": result.category,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        }

    results: list[tuple[int, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=_LANES, thread_name_prefix="clashlens-perf") as executor:
        futures = [executor.submit(process, index, job) for index, job in enumerate(jobs)]
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
        results.append({
            "job_id": result.job_id, "outcome": result.outcome, "category": result.category,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
        })
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
                connection_info, archive, key=f"perf-{index}-{label}",
                boundary=boundary, trophies=trophies, empty_battle_log=empty,
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
            connection_info, archive, key=f"perf-{index}-correction",
            boundary=BOUNDARY, trophies=6039 + index, empty_battle_log=False,
            observed_at=BOUNDARY + timedelta(seconds=1), normalized_tag=_tag(index + 1),
        )
        jobs.extend(pair[2:])
    return jobs


def _db_snapshot(connection_info: str, wal_start: str, statement_start: int | None) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(connection_info) as connection:
        wal = connection.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s::pg_lsn)::bigint", (wal_start,)
        ).fetchone()[0]
        relations = connection.execute(
            """SELECT relname, pg_total_relation_size(oid) FROM pg_class
               WHERE relnamespace=current_schema()::regnamespace AND relkind IN ('r','m')
               ORDER BY relname"""
        ).fetchall()
        queues = {}
        for table in ("collector_jobs", "python_processing_jobs"):
            rows = connection.execute(
                f"""SELECT status, work_type, count(*),
                           CASE WHEN status IN ('pending','leased','waiting_retry','waiting_dependency')
                                THEN extract(epoch FROM clock_timestamp() - min(created_at))
                           END
                    FROM {table} GROUP BY status, work_type ORDER BY status, work_type"""
            ).fetchall()
            queues[table] = [
                {"status": _text(row[0]), "work_type": _text(row[1]),
                 "count": int(row[2]),
                 "oldest_active_age_seconds": None if row[3] is None else float(row[3])}
                for row in rows
            ]
        pending_remote = 0
        try:
            pending_remote = int(connection.execute("SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'").fetchone()[0])
        except psycopg.Error:
            connection.rollback()
        statement_calls = None
        try:
            current = int(connection.execute("SELECT COALESCE(sum(calls),0)::bigint FROM pg_stat_statements").fetchone()[0])
            statement_calls = current - statement_start if statement_start is not None else None
        except psycopg.Error:
            connection.rollback()
        return {
            "wal_bytes": int(wal), "sql_statement_calls": statement_calls,
            "pending_remote_verification": pending_remote,
            "relations": {_text(row[0]): int(row[1]) for row in relations}, "queues": queues,
        }


def _start_metrics(connection_info: str) -> tuple[str, int | None]:
    import psycopg

    with psycopg.connect(connection_info) as connection:
        wal = _text(connection.execute("SELECT pg_current_wal_insert_lsn()::text").fetchone()[0])
        try:
            calls = int(connection.execute("SELECT COALESCE(sum(calls),0)::bigint FROM pg_stat_statements").fetchone()[0])
        except psycopg.Error:
            connection.rollback(); calls = None
    return wal, calls


def _plan_counts(node: dict[str, Any]) -> tuple[int, int]:
    children = node.get("Plans", [])
    if not children:
        scanned = int(node.get("Actual Rows", 0)) * int(node.get("Actual Loops", 1))
        scanned += int(node.get("Rows Removed by Filter", 0))
        return scanned, int(node.get("Actual Rows", 0))
    scanned = sum(_plan_counts(child)[0] for child in children)
    return scanned, int(node.get("Actual Rows", 0))


def _query_army_endpoint(
    connection_info: str, *, population: str = "trophies-5000-9000",
    start_day: int = 23, end_day: int = 23,
) -> dict[str, Any]:
    from clashlens.api_db import ApiDatabase
    from clashlens.army_analytics import (
        ArmyAnalyticsSelection,
        ArmyAnalyticsUnavailable,
        CurrentSeasonEmpty,
    )

    selection = ArmyAnalyticsSelection.parse(
        lens="offense", season="1783918800", start_day=start_day, end_day=end_day,
        population=population, category="troops", sort="usage-rate",
    )
    database = ApiDatabase(connection_info, max_size=1)
    started = time.perf_counter()
    try:
        try:
            result = database.get_army_analytics(selection, now=BOUNDARY + timedelta(days=1))
            return {
                "status": "returned" if result is not None else "not-found",
                "returned_fact_count": 0 if result is None else int(result["total_attacks"]),
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        except (ArmyAnalyticsUnavailable, CurrentSeasonEmpty) as error:
            return {
                "status": type(error).__name__, "returned_fact_count": 0,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
    finally:
        database.close()


def _seed_worst_case_army_reads(connection_info: str, fact_count: int) -> list[dict[str, Any]]:
    """Bulk-load bounded synthetic facts around production-created FK evidence."""
    import psycopg

    with psycopg.connect(connection_info) as connection:
        base = connection.execute(
            """SELECT (SELECT id FROM battle_evidence ORDER BY id LIMIT 1),
                      (SELECT id FROM ranked_day_versions ORDER BY id LIMIT 1),
                      (SELECT id FROM collector_observations ORDER BY id LIMIT 1)"""
        ).fetchone()
        if base is None or any(value is None for value in base):
            raise RuntimeError("army read workload requires processed battle and reconciliation evidence")
        evidence_id, version_id, observation_id = map(int, base)
        connection.execute(
            """INSERT INTO players (normalized_tag, active)
               SELECT '#Q' || lpad(g::text, 6, '0'), false FROM generate_series(1,1100) g
               ON CONFLICT (normalized_tag) DO NOTHING"""
        )
        population_ids = [int(row[0]) for row in connection.execute(
            "SELECT id FROM players WHERE normalized_tag LIKE '#Q%' ORDER BY normalized_tag LIMIT 1000"
        ).fetchall()]
        opponent_ids = [int(row[0]) for row in connection.execute(
            "SELECT id FROM players WHERE normalized_tag LIKE '#Q%' ORDER BY normalized_tag OFFSET 1000 LIMIT 100"
        ).fetchall()]
        connection.execute("CREATE TEMP TABLE perf_facts (day int, player_id bigint, opponent_id bigint) ON COMMIT DROP")
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
               FROM perf_facts ON CONFLICT DO NOTHING""", (DAY_START,)
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
                   ON CONFLICT DO NOTHING RETURNING id""", (day_start + timedelta(days=1),)
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
        results.append({
            "selection": population, "synthetic_fact_limit": fact_count,
            "rows_scanned": scanned, "rows_returned": returned,
            "latency_ms": endpoint["latency_ms"], "endpoint": endpoint,
            "explain_analyze_buffers": plan_row,
        })
    return results


def _run_reset(
    connection_info: str, archive: Any, population: int, correction: bool
) -> dict[str, Any]:
    import psycopg

    database, processor, metrics, _spool = _processor(connection_info, archive)
    try:
        source_jobs = _store_reconciliation_population(connection_info, archive, population)
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
                     WHERE ranked_day_start = %s)""",
                (DAY_START, BOUNDARY, BOUNDARY, BOUNDARY, DAY_START),
            ).fetchone()
            active_rows = connection.execute(
                """SELECT 'python', work_type, count(*) FROM python_processing_jobs
                   WHERE status IN ('pending','leased','waiting_retry') GROUP BY work_type
                   UNION ALL
                   SELECT 'collector', work_type, count(*) FROM collector_jobs
                   WHERE status IN ('pending','leased','waiting_retry') GROUP BY work_type
                   ORDER BY 1, 2"""
            ).fetchall()
            correction_evidence = None
            if correction:
                correction_evidence = {
                    "superseded_snapshots": int(connection.execute(
                        "SELECT count(*) FROM leaderboard_snapshots WHERE state='superseded'"
                    ).fetchone()[0]),
                    "snapshots_with_prior_reference": int(connection.execute(
                        "SELECT count(*) FROM leaderboard_snapshots WHERE correction_of_id IS NOT NULL"
                    ).fetchone()[0]),
                }
        generations = 2 if correction else 1
        expected_counts = {
            "ranked_day_versions": generations * population,
            "snapshot_headers": 2 * generations * population,
            "snapshot_entries": 2 * generations * population * population,
        }
        actual_counts = {
            "ranked_day_versions": int(counts[0]),
            "snapshot_headers": int(counts[1]),
            "snapshot_entries": int(counts[2]),
        }
        if actual_counts != expected_counts:
            raise RuntimeError(
                f"reset fan-out mismatch: expected {expected_counts}, got {actual_counts}"
            )
        outcomes = profile_results + first + second
        if any(result["outcome"] != "processed" for result in outcomes):
            raise RuntimeError("reset workload contains a non-processed result")
        return {
            "population": population, "official_responses": len(profile_results),
            "profile_results": profile_results,
            "dependent_results": first, "correction_results": second,
            "fact_counts": {
                **actual_counts, "analytics_summaries": int(counts[3]),
                "army_facts": int(counts[4]),
            },
            "fanout_evidence": {
                "expected": expected_counts,
                "matches_expected": True,
                "snapshot_entries_per_population_squared": 2 * generations,
            },
            "queue_residue": [
                {"owner": _text(row[0]), "work_type": _text(row[1]), "count": int(row[2])}
                for row in active_rows
            ], "stage_metrics": metrics.snapshot(), "spool": _spool.stats(), "evidence_counters": _spool.counters(), "spool_root": str(_spool.spool.root),
            "army_endpoint": army_endpoint,
            "correction_evidence": correction_evidence,
        }
    finally:
        database.close()


def _run_duplicate(connection_info: str, archive: Any, count: int, *, cycles: int = 1) -> dict[str, Any]:
    import psycopg
    from domain_test_support import store_observation

    database, processor, metrics, _spool = _processor(connection_info, archive)
    try:
        executed_count = min(count, DUPLICATE_EXECUTION_CAP)
        results: list[dict[str, Any]] = []
        cycle_elapsed: list[float] = []
        # Production-shaped multi-hash fixture: occurrences are distributed
        # across ~200 tracked players (~window occurrences each) instead of
        # concentrating every write on one player row, which matches the
        # real duplicate shape where identical responses repeat per player.
        window = max(1, executed_count // 200)
        for cycle in range(max(1, cycles)):
            jobs = []
            # Cycle 0 seeds the measured novelty sample (~1% new hashes);
            # every later cycle replays the same hashes as pure duplicates,
            # which is the steady-state shape of a duplicate-heavy day.
            # One transaction per cycle models the collector's batched
            # handoff without a new PostgreSQL connection per response.
            with psycopg.connect(connection_info) as seed_connection:
                for index in range(executed_count):
                    tag = _tag(index // window + 1)
                    variant = ((index // window) % 8) if cycle == 0 else 0
                    jobs.append(store_observation(
                        connection_info, archive, occurrence_key=f"duplicate-c{cycle}-{index}", endpoint="profile",
                        body=_profile_body(tag, variant), observed_at=DAY_START + timedelta(hours=1, minutes=index), normalized_tag=tag,
                        existing_connection=seed_connection, commit=False,
                    )[1])
                seed_connection.commit()
            started = time.perf_counter()
            results.extend(_process_jobs(processor, jobs, f"duplicate-c{cycle}"))
            cycle_elapsed.append(time.perf_counter() - started)
        steady = sorted(cycle_elapsed)[len(cycle_elapsed) // 2]
        return {
            "observations": count,
            "official_responses": count * max(1, cycles),
            "executed_observations": executed_count * max(1, cycles),
            "measured_cycles": len(cycle_elapsed),
            "cycle_elapsed_seconds": cycle_elapsed,
            "median_cycle_seconds": steady,
            "daily_288_cycle_projection_seconds": steady * 288,
            "aggregation_factor": count / executed_count,
            "aggregation_method": (
                "exact bounded cycle" if cycles == 1 and count == executed_count
                else "24h-equivalent aggregate: each response executes the full raw-evidence/local/Python/PostgreSQL semantics; the first cycle carries ~1% hash novelty and later measured cycles are 100% verified-duplicate steady state; the 288-cycle day projection multiplies the median measured five-minute cycle"
            ),
            "results": results,
            "stage_metrics": metrics.snapshot(),
            "spool": _spool.stats(),
            "evidence_counters": _spool.counters(),
            "spool_root": str(_spool.spool.root),
        }
    finally:
        database.close()


def _run_mixed(connection_info: str, archive: Any, live: int, backfill: int) -> dict[str, Any]:
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
                    connection_info, archive, occurrence_key=f"{kind}-{index}", endpoint="profile",
                    body=_profile_body(tag), observed_at=DAY_START + timedelta(hours=1), normalized_tag=tag,
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
            "completion_order": order, "live_jobs": live, "backfill_jobs": backfill,
            "official_responses": len(jobs),
            "live_first_completion_index": order.index("live") if "live" in order else None,
            "results": drained, "stage_metrics": metrics.snapshot(), "spool": _spool.stats(), "evidence_counters": _spool.counters(), "spool_root": str(_spool.spool.root),
        }
    finally:
        database.close()


def _collector_probe(skip: bool) -> dict[str, Any]:
    if skip:
        return {"executed": False, "reason": "explicit test-only skip"}
    started = time.perf_counter()
    completed = subprocess.run(
        [
            "go", "test", "./internal/collector",
            "-run", "^TestGoCollectorHandoffToPythonSignedPlayerPage$",
            "-count=1", "-timeout=120s",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=150,
    )
    if completed.returncode:
        raise RuntimeError("collector transitive probe failed: " + completed.stderr[-1000:])
    return {
        "executed": True, "elapsed_seconds": time.perf_counter() - started,
        "test": "TestGoCollectorHandoffToPythonSignedPlayerPage",
    }


ARCHIVE_PROBE_MARKER = "PERF_DUPLICATE_ARCHIVE_PROBE "


def _parse_archive_probe_marker(output: str) -> dict[str, int]:
    markers = [
        line.removeprefix(ARCHIVE_PROBE_MARKER)
        for line in output.splitlines() if line.startswith(ARCHIVE_PROBE_MARKER)
    ]
    if len(markers) != 1:
        raise RuntimeError(f"archive probe emitted {len(markers)} markers, want exactly 1")
    try:
        parsed = json.loads(markers[0])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"malformed archive probe marker: {error}") from error
    required = (
        "count", "head", "get", "put",
        "raw_count", "raw_head", "raw_put", "raw_get", "raw_duplicate_bucket_requests",
        "hash_us", "operation_total_us", "stage_put_us", "stage_get_verify_us",
        "local_verify_us",
    )
    if not isinstance(parsed, dict) or any(
        not isinstance(parsed.get(key), int) or isinstance(parsed.get(key), bool)
        for key in required
    ):
        raise RuntimeError("archive probe marker must contain integer totals for keys: " + ",".join(required))
    return parsed


def _collector_archive_probe(count: int) -> dict[str, Any]:
    """Probe the production Go s3Archive.store duplicate path via a real HTTP S3 fake."""
    started = time.perf_counter()
    completed = subprocess.run(
        [
            "go", "test", "./internal/collector",
            "-run", "^TestS3ArchiveDuplicateStoreProbe$",
            # The probe issues two real HTTP operations per duplicate, so its
            # budget must scale with the requested observation count.
            "-count=1", "-timeout=1800s", "-v",
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
            + "stdout[-1000:]: " + completed.stdout[-1000:] + "\n"
            + "stderr[-1000:]: " + completed.stderr[-1000:]
        )
    totals = _parse_archive_probe_marker(completed.stdout)
    # Legacy seam baseline plus the production raw-evidence module contract:
    # one conditional PUT + one verification GET for a new hash, then
    # zero-request duplicates for every later occurrence.
    legacy_count = int(totals.get("legacy_count", count))
    expected = {
        "count": count, "head": legacy_count, "get": legacy_count - 1, "put": 1,
        "raw_count": count, "raw_head": 0, "raw_put": 1, "raw_get": 1,
        "raw_duplicate_bucket_requests": 0,
    }
    mismatched = {key: (totals.get(key), wanted) for key, wanted in expected.items() if totals.get(key) != wanted}
    if mismatched:
        raise RuntimeError(f"archive probe totals {totals} do not match expected {expected}")
    return {
        "executed": True, "test": "TestS3ArchiveDuplicateStoreProbe", **totals,
        "elapsed_seconds": time.perf_counter() - started,
    }


def _run_army_read_sample(database_url: str, fact_count: int) -> dict[str, Any]:
    from domain_test_support import domain_database

    with domain_database(database_url) as connection_info, archive_server() as archive:
        wal_start, statement_start = _start_metrics(connection_info)
        cpu_start = time.process_time()
        elapsed_start = time.perf_counter()
        database, processor, _metrics, _spool = _processor(connection_info, archive)
        try:
            with count_sql_calls() as sql_calls:
                jobs = _store_reconciliation_population(connection_info, archive, 1)
                _process_jobs(processor, jobs, "army-read-evidence")
                _drain(processor, 120)
                reads = _seed_worst_case_army_reads(connection_info, fact_count)
            measurements = _db_snapshot(connection_info, wal_start, statement_start)
            measurements["application_sql_calls"] = sql_calls[0]
            return {
                "synthetic_fact_limit": fact_count,
                "selections": reads,
                "archive_operations": {"get": archive[3].gets, "head": archive[3].heads},
                "database": measurements,
                "elapsed_seconds": time.perf_counter() - elapsed_start,
                "cpu_seconds": time.process_time() - cpu_start,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "spool": _spool.stats(), "evidence_counters": _spool.counters(), "spool_root": str(_spool.spool.root),
            }
        finally:
            database.close()


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
        for hash_value in connection.execute("SELECT response_hash FROM archive_catalogue").fetchall():
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
    for prefix_dir in sorted((spool_root / "sha256").glob("[0-9a-f]" * 2)) if spool_root.exists() else []:
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


def _provenance(arguments: argparse.Namespace) -> dict[str, Any]:
    migrations = [
        {"name": path.name, "sha256": _sha(path.read_bytes())}
        for path in sorted((ROOT / "deploy/migrations").glob("*.sql"))
    ]
    configuration = {
        "mode": arguments.mode, "populations": arguments.populations,
        "duplicate_observations": arguments.duplicate_observations,
        "live_jobs": arguments.live_jobs, "backfill_jobs": arguments.backfill_jobs,
        "army_facts": arguments.army_facts,
    }
    return {
        "source_sha": _git("rev-parse", "HEAD"), "source_dirty": bool(_git("status", "--porcelain")),
        "runner_sha256": _sha(Path(__file__).read_bytes()), "migrations": migrations,
        "configuration_fingerprint": _sha(json.dumps(configuration, sort_keys=True).encode()),
        "configuration": configuration, "images": arguments.image,
        "host": {"platform": platform.platform(), "python": platform.python_version()},
    }


def run(arguments: argparse.Namespace) -> dict[str, Any]:
    if not arguments.database_url:
        raise RuntimeError("--database-url or CLASHLENS_TEST_DATABASE_URL is required")
    if arguments.mode in {"reset-boundary", "correction"}:
        validate_reset(arguments.populations, arguments.post_fix)
    from domain_test_support import domain_database

    started = datetime.now(tz=UTC)
    collector_probe = _collector_probe(arguments.skip_collector_probe)
    duplicate_archive_probe = (
        _collector_archive_probe(arguments.duplicate_observations)
        if arguments.mode == "duplicate-heavy" else None
    )
    samples = []
    populations = arguments.populations if arguments.mode in {"reset-boundary", "correction"} else [0]
    for population in populations:
        with domain_database(arguments.database_url) as connection_info, archive_server() as archive:
            wal_start, statement_start = _start_metrics(connection_info)
            cpu_start = time.process_time()
            elapsed_start = time.perf_counter()
            with count_sql_calls() as sql_calls:
                if arguments.mode == "reset-boundary":
                    workload = _run_reset(connection_info, archive, population, False)
                elif arguments.mode == "correction":
                    workload = _run_reset(connection_info, archive, population, True)
                elif arguments.mode == "duplicate-heavy":
                    workload = _run_duplicate(connection_info, archive, arguments.duplicate_observations, cycles=arguments.duplicate_cycles)
                    workload["collector_archive_operations"] = duplicate_archive_probe
                else:
                    workload = _run_mixed(connection_info, archive, arguments.live_jobs, arguments.backfill_jobs)
            measurements = _db_snapshot(connection_info, wal_start, statement_start)
            measurements["application_sql_calls"] = sql_calls[0]
            if "official_responses" not in workload:
                raise RuntimeError("workload did not report its exact official response count")
            response_count = int(workload["official_responses"])
            # Executed responses actually ran through the full pipeline;
            # projected ones are represented by measured-cycle aggregation
            # (duplicate-heavy 24h-equivalent mode). Never mix the two.
            executed_count = int(workload.get("executed_observations", workload.get("official_responses", response_count)))
            projected_count = max(0, response_count - executed_count)
            distinct_hashes = len(archive[3].objects)
            spool = workload.get("spool", {})
            counters = workload.get("evidence_counters", {})
            stages = workload.get("stage_metrics", {})
            # Read latency covers the full local-verify-or-fallback path;
            # repair latency is measured separately inside the spool reader.
            latency = {name: (float(stages[stage]["average_ms"]) if stage in stages else None) for name, stage in {
                "python_read": "python_archive_get_verify", "local_verify": "python_archive_local_verify",
                "transaction": "python_domain_profile", "repair": "python_archive_repair"}.items()}
            probe = duplicate_archive_probe or {}
            if probe.get("executed"):
                # Collector-side raw-evidence latencies are measured inside the
                # checked-in Go probe; python-side stage latencies above come
                # from the StageMetrics histograms of this workload.
                latency.update({
                    "collector_hashing_us": probe["hash_us"],
                    "collector_operation_total_us": probe["operation_total_us"],
                    "collector_remote_put_us": probe["stage_put_us"],
                    "collector_get_verify_us": probe["stage_get_verify_us"],
                    "collector_local_verify_us": probe["local_verify_us"],
                })
            orphans = _orphan_metrics(connection_info, Path(workload["spool_root"]))
            retries_measured = sum(int(result.get("outcome") == "retrying") for result in workload.get("results", []))
            samples.append({
                "workload": workload, "database": measurements,
                "archive_operations": {"get": archive[3].gets, "head": archive[3].heads, "conditional_put": archive[3].conditional_puts, "put": archive[3].puts, "conflicts": archive[3].conflicts},
                "evidence": {"response_count": response_count, "executed_responses": executed_count, "projected_responses": projected_count, "execution_method": workload.get("aggregation_method", "exact bounded cycle"), "distinct_hashes": distinct_hashes, "novelty_rate": (distinct_hashes / executed_count if executed_count else 0.0), "exact_bytes": sum(map(len, archive[3].objects.values())), "archived_bytes": sum(map(len, archive[3].objects.values())), "pending_verification_count": measurements.get("pending_remote_verification"), "pending_verification_age_seconds": _pending_age_seconds(connection_info), "orphan_count": orphans["count"], "orphan_bytes": orphans["bytes"], "local_hits": counters.get("local_hits", 0), "local_misses": counters.get("local_misses", archive[3].gets), "repairs": counters.get("repairs", 0), "provider_errors": counters.get("provider_errors", 0), "retries": retries_measured, "concurrency_lanes": _LANES, "latency_ms": latency},
                "spool": {"final_bytes": int(spool.get("final_bytes", 0)), "temporary_bytes": int(spool.get("temporary_bytes", 0)), "high_water_bytes": int(spool.get("high_water_bytes", 0)), "final_object_count": int(spool.get("final_objects", 0)), "temporary_object_count": int(spool.get("temporary_objects", 0)), "live_reservations": int(spool.get("reserved_objects", 0)), "allocated_blocks": spool.get("allocated_blocks"), "free_inodes": spool.get("free_inodes", _free_inodes(Path(workload["spool_root"])))},
                "elapsed_seconds": time.perf_counter() - elapsed_start,
                "cpu_seconds": time.process_time() - cpu_start,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            })
    army_read_sample = (
        _run_army_read_sample(arguments.database_url, arguments.army_facts)
        if arguments.mode in {"reset-boundary", "correction"}
        else None
    )
    return {
        "schema_version": 3, "mode": arguments.mode, "started_at": started.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(), "provenance": _provenance(arguments),
        "collector_probe": collector_probe, "samples": samples,
        "army_read_sample": army_read_sample,
    }


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--database-url", default=os.environ.get("CLASHLENS_TEST_DATABASE_URL"))
    parser.add_argument("--populations", default="2,4,8")
    parser.add_argument("--post-fix", action="store_true")
    parser.add_argument("--duplicate-observations", type=int, default=20)
    parser.add_argument("--live-jobs", type=int, default=5)
    parser.add_argument("--backfill-jobs", type=int, default=20)
    parser.add_argument("--army-facts", type=int, default=1_000)
    parser.add_argument("--lanes", type=int, default=32)
    parser.add_argument("--duplicate-cycles", type=int, default=1)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-collector-probe", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        arguments.populations = [int(value) for value in arguments.populations.split(",")]
    except ValueError:
        parser.error("--populations must contain integers")
    if arguments.mode in {"reset-boundary", "correction"}:
        try:
            validate_reset(arguments.populations, arguments.post_fix)
        except ValueError as error:
            parser.error(str(error))
    if arguments.duplicate_observations < 2 or arguments.live_jobs < 1 or arguments.backfill_jobs < 1:
        parser.error("workload counts must be positive; duplicates must be at least 2")
    if not 1 <= arguments.army_facts <= 100_000:
        parser.error("--army-facts must be between 1 and 100000")
    if not 1 <= arguments.lanes <= 64:
        parser.error("--lanes must be between 1 and 64")
    if arguments.duplicate_cycles < 1 or arguments.duplicate_cycles > 4:
        parser.error("--duplicate-cycles must be between 1 and 4")
    global _LANES
    _LANES = arguments.lanes
    if any("=sha256:" not in image for image in arguments.image):
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
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, psycopg.Error) as error:
        print(f"performance runner: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
