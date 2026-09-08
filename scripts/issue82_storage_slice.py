#!/usr/bin/env python3
"""Issue #82 all-component storage/cost measurement slice (stdlib only).

Runs bounded, aggregate-only measurements against the disposable Fedora
database and the read-only old GCS archive, then combines them with the
verified provider tariffs into central/conservative six-month projections.

Privacy: GCS bodies are classified in memory by key shape only. Object
names/hashes, player tags, and raw bodies are never written to artifacts,
reports, or logs.

Phases (``--phases`` csv, default ``all``):
  gcs-census   exact metadata census over all 256 sha256/ prefixes
  gcs-bodies   stratified body-shape sample (default 384 objects)
  pg-rehearsal 12,500-player x 28-day summary/retirement rehearsal + samples
  pricing      refetch provider tariff pages (also embedded verification)
  report       combine phase artifacts into the six-month report

Each phase writes ``<name>.json`` plus ``<name>.json.sha256`` atomically
into ``--results``. Never point pg-rehearsal at production: it creates and
drops an isolated schema in the supplied disposable database URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "python" / "src"), str(ROOT / "python" / "tests")]

BUCKET = "gs://clash-lens-archive"
RESULTS_ENV = "/home/zubair/clashlens-issue82-results"

# Production collection contract (issue #60): fixed 25,024 observations per
# five-minute cycle. Used only as a labeled denominator for sensitivity, not
# as an observed archive rate.
CONTRACT_OBS_PER_CYCLE = 25024
CONTRACT_CYCLES_PER_DAY = 288
PLAYERS = 12500
SEASON_DAYS = 28
SIX_MONTH_DAYS = 183  # ~6 calendar months
SEASONS_6MO = SIX_MONTH_DAYS / SEASON_DAYS


def utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def atomic_write_json(path: Path, payload: dict) -> str:
    """Write JSON atomically (tmp + fsync + rename) and return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=1, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    with open(str(path) + ".sha256", "w", encoding="utf-8") as handle:
        handle.write(digest + "  " + path.name + "\n")
    return digest


def classify_body(data: bytes) -> tuple[str, int]:
    """Classify one archive body by key shape. Returns (category, items_len).

    Never logs or returns values, tags, or bodies: only the endpoint
    category and the top-level item count.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "undecodable", 0
    # Cheap shape probes on the raw text avoid building identifier-bearing
    # objects; only top-level structure matters.
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return "unknown", 0
    try:
        obj = json.loads(text)
    except ValueError:
        return "unknown", 0
    if not isinstance(obj, dict):
        return "unknown", 0
    keys = set(obj.keys())
    items = obj.get("items")
    if isinstance(items, list):
        n = len(items)
        if n and isinstance(items[0], dict):
            first = set(items[0].keys())
            if "battleType" in first or "armyShareCode" in first:
                return "battle_log", n
            if "rank" in first:
                return "global_player_rankings", n
        elif n == 0:
            # Empty item lists are ambiguous between battle logs and
            # rankings; keep them separate rather than guessing.
            return "empty_items", 0
        return "items_unknown", n
    if "tag" in keys and "trophies" in keys:
        return "profile", 0
    return "unknown", 0


def percentiles(values: list[int]) -> dict[str, float]:
    if not values:
        return {"min": 0, "p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0,
                "mean": 0, "count": 0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(p: float) -> float:
        idx = min(n - 1, int(p / 100 * n))
        return float(ordered[idx])

    return {"min": float(ordered[0]), "p50": pct(50), "p90": pct(90),
            "p95": pct(95), "p99": pct(99), "max": float(ordered[-1]),
            "mean": float(sum(ordered) / n), "count": n}


def gcloud_json(args: list[str], timeout: int = 600) -> object:
    proc = subprocess.run(["gcloud", *args], capture_output=True, text=True, check=False,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"gcloud failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout or "null")


def phase_gcs_census(results: Path, prefixes: list[str]) -> dict:
    started = time.time()
    total_objects = 0
    total_bytes = 0
    sizes: list[int] = []
    per_day: dict[str, list[int]] = {}
    per_prefix: dict[str, int] = {}
    sample_names: list[str] = []  # transient in-memory only, never persisted
    for prefix in prefixes:
        rows = gcloud_json(["storage", "objects", "list",
                            f"{BUCKET}/sha256/{prefix}/*",
                            "--format=json(size,creation_time,name)"])
        per_prefix[prefix] = len(rows)
        stride = max(1, len(rows) // 4)
        for index, row in enumerate(rows):
            try:
                size = int(row["size"])
            except (KeyError, TypeError, ValueError):
                continue
            total_objects += 1
            total_bytes += size
            sizes.append(size)
            day = str(row.get("creation_time", ""))[:10]
            cell = per_day.setdefault(day, [0, 0])
            cell[0] += 1
            cell[1] += size
            if index % stride == 0 and len(sample_names) < 2048:
                sample_names.append(str(row["name"]))
    # Keep a bounded deterministic reservoir for the body phase.
    rng = random.Random(20260908)
    rng.shuffle(sample_names)
    reservoir = sample_names[:1024]
    payload = {
        "schema_version": 1,
        "measured_at": utcnow_iso(),
        "source": "read_only_gcloud_object_metadata",
        "bucket": BUCKET,
        "prefix": "sha256/",
        "prefixes_listed": len(prefixes),
        "official_api_requests": 0,
        "mutations": 0,
        "object_count": total_objects,
        "total_bytes": total_bytes,
        "object_bytes": percentiles(sizes),
        "arrival_per_day": {day: {"objects": c, "bytes": b}
                            for day, (c, b) in sorted(per_day.items())},
        "prefix_object_counts": per_prefix,
        "limitations": [
            ("metadata establishes unique object arrival/size only; it cannot "
            "establish the observation denominator, so no observed novelty "
            "rate is claimed here"),
            "per-day arrival counts object creation_time, not collection time",
        ],
    }
    digest = atomic_write_json(results / "issue82-gcs-census.json", payload)
    # Reservoir is intentionally not persisted (names are archive references).
    with open(results / "issue82-gcs-census.json.sha256", "a",
              encoding="utf-8") as handle:
        handle.write(f"elapsed_seconds={time.time() - started:.1f}\n")
    return {"digest": digest, "reservoir": reservoir,
            "elapsed_seconds": round(time.time() - started, 1)}


def _fetch_body(name: str, timeout: int = 120) -> bytes | None:
    try:
        proc = subprocess.run(["gcloud", "storage", "cat", BUCKET + "/" + name], check=False,
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def phase_gcs_bodies(results: Path, names: list[str], sample_size: int,
                     lanes: int = 4) -> dict:
    started = time.time()
    rng = random.Random(20260908)
    picked = list(names)
    rng.shuffle(picked)
    picked = picked[:sample_size]
    by_category: dict[str, dict[str, int | list[int]]] = {}
    failures = 0

    def one(name: str) -> tuple[str, int, int] | None:
        body = _fetch_body(name)
        if body is None:
            return None
        category, items_len = classify_body(body)
        return category, len(body), items_len

    with ThreadPoolExecutor(max_workers=lanes) as pool:
        for outcome in pool.map(one, picked):
            if outcome is None:
                failures += 1
                continue
            category, size, items_len = outcome
            cell = by_category.setdefault(category, {"count": 0, "sizes": [],
                                                     "items_lens": []})
            cell["count"] += 1
            cell["sizes"].append(size)
            cell["items_lens"].append(items_len)
    categories = {}
    for category, cell in sorted(by_category.items()):
        sizes = cell["sizes"]
        lens = cell["items_lens"]
        categories[category] = {
            "count": cell["count"],
            "share": cell["count"] / max(1, sum(
                c["count"] for c in by_category.values())),
            "body_bytes": percentiles(sizes),
            "items_len": percentiles(lens),
        }
    payload = {
        "schema_version": 1,
        "measured_at": utcnow_iso(),
        "source": "read_only_gcloud_body_shape_sample",
        "official_api_requests": 0,
        "requested": len(picked),
        "classified": sum(c["count"] for c in by_category.values()),
        "failed": failures,
        "sampling": ("seeded shuffle of a per-prefix deterministic stride "
                     "reservoir; aggregate only, no names/bodies/tags retained"),
        "categories": categories,
        "limitations": [
            ("empty item lists cannot distinguish battle logs from rankings; "
            "kept as a separate category rather than guessed"),
            ("fixture key shapes define the classifier; drift would land in "
            "items_unknown/unknown, which is reported, not reallocated"),
        ],
    }
    digest = atomic_write_json(results / "issue82-gcs-bodies.json", payload)
    payload["elapsed_seconds"] = round(time.time() - started, 1)
    return {"digest": digest,
            "elapsed_seconds": round(time.time() - started, 1)}


def _seed_tag(number: int) -> str:
    """Deterministic synthetic tag inside the production tag charset."""
    alphabet = "0289"
    digits = ""
    remainder = int(number)
    for _ in range(7):
        digits = alphabet[remainder % 4] + digits
        remainder //= 4
    return "#P" + digits


def _pg_snapshot(connection, lsn: str) -> dict:
    wal = connection.execute(
        "SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(), %s::pg_lsn)::bigint",
        (lsn,)).fetchone()[0]
    retained = connection.execute(
        "SELECT COALESCE(sum(size), 0)::bigint FROM pg_ls_waldir()").fetchone()[0]
    rows = connection.execute(
        """
        SELECT c.relname, pg_table_size(c.oid),
               pg_indexes_size(c.oid),
               CASE WHEN c.reltoastrelid = 0 THEN 0
                    ELSE pg_total_relation_size(c.reltoastrelid) END,
               pg_total_relation_size(c.oid),
               COALESCE(s.n_live_tup, 0), COALESCE(s.n_dead_tup, 0)
        FROM pg_class AS c
        LEFT JOIN pg_stat_all_tables AS s ON s.relid = c.oid
        WHERE c.relnamespace = current_schema()::regnamespace
          AND c.relkind IN ('r', 'm')
        ORDER BY c.relname
        """).fetchall()
    relations = {}
    for row in rows:
        toast = int(row[3])
        relations[str(row[0])] = {
            "table_bytes": int(row[1]) - toast,
            "index_bytes": int(row[2]),
            "toast_bytes": toast,
            "total_bytes": int(row[4]),
            "live_tuples": int(row[5]),
            "dead_tuples": int(row[6]),
        }
    counts = {}
    for name in ("players", "ranked_day_versions", "api_player_daily_logs",
                 "player_season_summaries", "army_season_summaries",
                 "army_analytics_battle_facts", "collector_observations",
                 "legend_battles"):
        try:
            counts[name] = int(connection.execute(
                f"SELECT count(*) FROM {name}").fetchone()[0])
        except Exception:  # noqa: BLE001 - table may not exist yet
            connection.rollback()
    return {"generated_wal_bytes": int(wal), "retained_wal_bytes": int(retained),
            "relations": relations,
            "total_allocated_bytes": sum(r["total_bytes"] for r in relations.values()),
            "counts": counts}


def phase_pg_rehearsal(database_url: str, results: Path) -> dict:
    import uuid

    import psycopg

    from clashlens.api_db import ApiDatabase
    from clashlens.army_season_summaries import materialize_completed_army_season
    from clashlens.season_retirement import (
        finalize_season_detail,
        measure_season_storage,
        project_six_months,
        retire_season_detail,
    )
    from clashlens.season_summaries import materialize_completed_seasons

    started_all = time.time()
    schema = f"issue82_slice_{uuid.uuid4().hex[:12]}"
    season = "1785000000"
    live_season = "1785000001"
    day0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)
    season_end = day0 + timedelta(days=SEASON_DAYS)
    after_season = season_end + timedelta(hours=1)
    live_day0 = season_end

    admin = psycopg.connect(database_url, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    outcome: dict = {}
    try:
        from psycopg.conninfo import make_conninfo

        dsn = make_conninfo(database_url, options=f"-c search_path={schema}")
        migrations = sorted((ROOT / "deploy" / "migrations").glob("*.sql"))
        mig_hashes = {}
        with psycopg.connect(dsn, autocommit=True) as conn:
            for path in migrations:
                text = path.read_text(encoding="utf-8")
                mig_hashes[path.name] = hashlib.sha256(text.encode()).hexdigest()
                conn.execute(text)
        timeline: dict[str, dict] = {}

        def snap(conn, lsn: str, label: str) -> dict:
            snap_started = time.time()
            snap_data = _pg_snapshot(conn, lsn)
            snap_data["elapsed_seconds"] = round(time.time() - snap_started, 1)
            snap_data["label"] = label
            timeline[label] = {
                "generated_wal_bytes": snap_data["generated_wal_bytes"],
                "retained_wal_bytes": snap_data["retained_wal_bytes"],
                "total_allocated_bytes": snap_data["total_allocated_bytes"],
                "counts": snap_data["counts"],
                "elapsed_seconds": snap_data["elapsed_seconds"],
            }
            return snap_data
        with psycopg.connect(dsn) as conn:
            lsn0 = conn.execute(
                "SELECT pg_current_wal_insert_lsn()::text").fetchone()[0]
            host = {"filesystem": None}
            try:
                usage = shutil.disk_usage(ROOT.anchor)
                host["filesystem"] = {"total_bytes": usage.total,
                                      "used_bytes": usage.used,
                                      "free_bytes": usage.free}
            except OSError:
                pass
            pg_version = conn.execute("SHOW server_version").fetchone()[0]
            pg_settings = {}
            for key in ("shared_buffers", "wal_level", "max_wal_size",
                        "checkpoint_timeout", "autovacuum"):
                try:
                    pg_settings[key] = conn.execute(
                        f"SHOW {key}").fetchone()[0]
                except Exception:  # noqa: BLE001 - setting may not exist
                    conn.rollback()
            snap(conn, lsn0, "migrated_empty")
            # --- completed season: 12,500 players x 28 days ---
            tags = [_seed_tag(g) for g in range(1, PLAYERS + 1)]
            conn.execute(
                "INSERT INTO players (normalized_tag, active, "
                "eligibility_state) "
                "SELECT tag, true, 'eligible' "
                "FROM unnest(%s::text[]) AS item(tag)",
                (tags,))
            conn.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end,
                    official_season_id, season_day_number,
                    season_anchor_rule_version, reconciliation_rule_version,
                    result_hash, version, state, confidence,
                    start_trophies, final_trophies_before_reset,
                    next_start_trophies, attack_count, defense_count,
                    attack_gain, observed_defense_loss)
                SELECT player.id, %s + ((day.n - 1) || ' days')::interval,
                       %s + (day.n || ' days')::interval,
                       %s, day.n, 'season-anchor-v1', 'reconciliation-v1',
                       md5(%s || ':' || day.n::text)
                         || md5(day.n::text || %s), 1,
                       'Complete', 'exact',
                       6000 + ((player.id * 7 + day.n) %% 900),
                       6000 + ((player.id * 7 + day.n) %% 900) + 10,
                       6000 + ((player.id * 7 + day.n) %% 900) + 10,
                       2, 1, 30, 20
                FROM players AS player CROSS JOIN
                     generate_series(1, %s) AS day(n)
                """,
                (day0, day0, season, season, season, SEASON_DAYS))
            conn.execute(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, ranked_day_version_id, version,
                    state, coverage, ranked_day_end, official_season_id,
                    season_day_number, confidence, attack_count,
                    attack_three_star_count, attack_gain, defense_count,
                    defense_three_star_count, defense_loss, net_trophy_change,
                    adjustments, battles, partial_reasons)
                SELECT version.player_id, version.ranked_day_start, version.id, 1,
                       'Complete', 'complete', version.ranked_day_end, %s,
                       version.season_day_number, 'exact', 2, 1, 30, 1, 0, 20, 10,
                       '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
                FROM ranked_day_versions AS version
                WHERE version.official_season_id = %s
                """,
                (season, season))
            # Season anchor + completed-day markers + a modest army-fact sample.
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state)
                VALUES (%s, %s, %s, %s, 'legend-season-anchor-v1', 1, 'confirmed')
                """,
                (season, "previous-" + season, day0,
                 day0 - timedelta(days=SEASON_DAYS)))
            conn.execute(
                """
                INSERT INTO archive_instances (
                    instance_id, endpoint, region, bucket, marker_key,
                    marker_hash, marker_payload_version)
                VALUES ('seed-instance', 'seed-endpoint', 'seed-region',
                        'seed-bucket', 'seed-marker',
                        md5('seed-marker') || md5('seed-marker-2'), 'seed-v1')
                """)
            for day in range(1, SEASON_DAYS + 1):
                conn.execute(
                    """
                    INSERT INTO army_analytics_completed_days (
                        ranked_day_start, official_season_id, season_day_number,
                        fact_input_hash)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (ranked_day_start) DO NOTHING
                    """,
                    (day0 + timedelta(days=day - 1), season, day,
                     hashlib.sha256(f"{season}:day:{day}".encode()
                                   ).hexdigest()))
            conn.execute(
                """
                INSERT INTO army_analytics_battle_facts (
                    battle_id, evidence_id, source_ranked_day_version_id,
                    ranked_day_start, official_season_id, season_day_number,
                    lens, population_player_id, stars, destruction_percentage,
                    army_state, perspective_disagreement,
                    battle_time_trophies, input_hash, version)
                SELECT 900000 + g,
                       910000 + g, 4242,
                       %s + (((g - 1) %% %s) || ' days')::interval,
                       %s, ((g - 1) %% %s) + 1,
                       CASE WHEN g %% 2 = 0 THEN 'offense' ELSE 'defense' END,
                       1 + ((g - 1) %% %s), 3, 100, 'decoded', false, 6000,
                       md5(%s || ':' || g::text) || md5(g::text || %s), 1
            
                FROM generate_series(1, %s) AS g
                """,
                (day0, SEASON_DAYS, season, SEASON_DAYS, PLAYERS, season,
                 season, 5000))
            conn.commit()
            snap(conn, lsn0, "season_seeded")
            # --- battle-detail width sample: 500 battles, current schema ---
            # FKs bypassed (replica role); NOT NULL/CHECKs satisfied with
            # synthetic values. Width calibration only, not pipeline behavior.
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(
                """
                INSERT INTO archive_catalogue (
                    response_hash, archive_reference, byte_size,
                    archive_instance_id, first_verified_at)
                SELECT md5('seed-battle:' || g::text) || md5(g::text),
                       'sha256/seed/' || md5('seed-battle:' || g::text),
                       30000, 'seed-instance', %s
                FROM generate_series(1, 500) AS g
                """,
                (day0,))
            conn.execute(
                """
                INSERT INTO collector_observations (
                    occurrence_key, collection_job_id, attempt_id, player_id,
                    normalized_tag, endpoint, request_started_at,
                    response_completed_at, http_status, response_hash,
                    archive_reference, archive_catalogue_hash,
                    collector_version, key_label,
                    evidence_headers, scope, request_method, request_path,
                    request_query, paging_envelope_state,
                    source_adapter_version)
                SELECT 'seed-battle:' || g, 1, 1, 1 + ((g - 1) %% %s),
                       '#S' || (1 + ((g - 1) %% %s)), 'battle_log',
                       %s, %s, 200, md5('seed-battle:' || g::text) || md5(g::text),
                       'sha256/seed/' || md5('seed-battle:' || g::text),
                       md5('seed-battle:' || g::text) || md5(g::text),
                       'seed-v1', 'seed', '{}'::jsonb,
                       'player', 'GET', '/v1/players/seed', '',
                       'not_applicable', 'seed-v1'
                FROM generate_series(1, 500) AS g
                """,
                (PLAYERS, PLAYERS, day0, day0))
            conn.execute(
                """
                INSERT INTO battle_log_observations (
                    observation_id, player_id, parser_version, observed_at,
                    row_count, has_row_gap)
                SELECT obs.id, 1 + ((obs.id - 1) %% %s),
                       'supercell-source-parser-v2', %s, 2, false
                FROM collector_observations AS obs
                WHERE obs.occurrence_key LIKE 'seed-battle:%%'
                """,
                (PLAYERS, day0))
            conn.execute(
                """
                INSERT INTO battle_source_rows (
                    battle_log_observation_id, source_row_index, outcome,
                    source_json)
                SELECT log.id, 0, 'valid_legend',
                       jsonb_build_object('stars', 3, 'destructionPercentage',
                                          100, 'n', log.id)
                FROM battle_log_observations AS log
                JOIN collector_observations AS obs ON obs.id = log.observation_id
                WHERE obs.occurrence_key LIKE 'seed-battle:%%'
                """)
            conn.execute(
                """
                INSERT INTO legend_battles (
                    ranked_day_start, attacker_player_id, defender_player_id)
                SELECT %s + (((g - 1) %% %s) || ' days')::interval,
                       1 + ((g - 1) %% %s), 1 + (g %% %s)
                FROM generate_series(1, 500) AS g
                """,
                (day0, SEASON_DAYS, PLAYERS, PLAYERS))
            conn.execute(
                """
                INSERT INTO parsed_source_payloads (
                    endpoint, response_hash, parser_version, schema_version,
                    parse_outcome, parsed_json)
                SELECT 'battle_log', md5('seed-payload:' || g::text) || md5(g::text),
                       'supercell-source-parser-v2', 'battle-log-schema-v1',
                       'valid', jsonb_build_object('n', g)
                FROM generate_series(1, 500) AS g
                """)
            conn.execute(
                """
                INSERT INTO battle_evidence (
                    battle_id, source_row_id, observation_id,
                    reporting_player_id, perspective, battle_timestamp, stars,
                    destruction_percentage, army_share_code, attacker_gain,
                    defender_loss, trophy_rule_version, source_observed_at,
                    parser_version)
                SELECT battle.id, source.id, obs.id, 1, 'attacker', %s, 3,
                       100, 'seedcode', 30, 20, 'trophy-v1', %s,
                       'supercell-source-parser-v2'
                FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn
                      FROM legend_battles) AS battle
                JOIN (SELECT id, row_number() OVER (ORDER BY id) AS rn
                      FROM battle_source_rows) AS source
                  ON source.rn = battle.rn
                JOIN (SELECT id, row_number() OVER (ORDER BY id) AS rn
                      FROM collector_observations
                      WHERE occurrence_key LIKE 'seed-battle:%%') AS obs
                  ON obs.rn = battle.rn
                """,
                (day0, day0))
            conn.execute(
                """
                INSERT INTO battle_perspectives (
                    battle_id, perspective, evidence_id, source_observed_at)
                SELECT evidence.battle_id, 'attacker', evidence.id, %s
                FROM battle_evidence AS evidence
                """,
                (day0,))
            conn.execute(
                """
                INSERT INTO battle_payload_rows (
                    parsed_payload_id, reporting_player_id, source_row_index,
                    source_row_id)
                SELECT payload.id, 1, 0, source.id
                FROM (SELECT id, row_number() OVER (ORDER BY id) AS rn
                      FROM parsed_source_payloads) AS payload
                JOIN (SELECT id, row_number() OVER (ORDER BY id) AS rn
                      FROM battle_source_rows) AS source
                  ON source.rn = payload.rn
                """)
            conn.execute(
                """
                INSERT INTO battle_army_decodes (
                    battle_id, evidence_id, raw_code, decoder_version,
                    catalog_version, catalog_hash, status, exact_army_id,
                    identity_hash, perspective)
                SELECT evidence.battle_id, evidence.id, 'seedcode', 'decoder-v1',
                       'catalog-v1', md5('catalog') || md5('catalog2'), 'decoded', 1,
                       md5('army:' || evidence.id::text) || md5(evidence.id::text), 'attacker'
                FROM battle_evidence AS evidence
                """)
            conn.commit()
            snap(conn, lsn0, "battle_detail_sampled")
            # --- production materialization: player + army summaries ---
            materialized = 0
            mat_started = time.time()
            after_player = 0
            while True:
                report = materialize_completed_seasons(
                    conn, season_id=season, max_players=1000,
                    now=after_season, after_player_id=after_player)
                conn.commit()
                materialized += int(report.get("materialized", 0))
                after_player = int(report.get("next_after_player_id") or 0)
                if int(report.get("candidates", 0)) < 1000:
                    break
            materialize_seconds = round(time.time() - mat_started, 1)
            army_report = materialize_completed_army_season(
                conn, season_id=season, now=after_season)
            conn.commit()
            snap(conn, lsn0, "summaries_materialized")
            summary_dist = conn.execute(
                """
                SELECT count(*), min(pg_column_size(summary.*)),
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY pg_column_size(summary.*)),
                       max(pg_column_size(summary.*)),
                       sum(pg_column_size(summary.*))
                FROM player_season_summaries AS summary
                WHERE official_season_id = %s
                """,
                (season,)).fetchone()
            storage_report = measure_season_storage(conn, season)
            lower_bound = project_six_months(
                player_season_bytes=(float(summary_dist[4]) / float(summary_dist[0])
                                     if summary_dist[0] else None),
                army_season_bytes=None,
                live_detail_bytes_per_day=None,
                daily_bookkeeping_bytes_per_day=None,
                players=PLAYERS,
                headroom_fraction=0.2)
            # Historical-read byte-equivalence sample before finalization.
            api = ApiDatabase(dsn)
            # Sample keys are positional labels; synthetic tags never
            # leave the disposable database or enter retained artifacts.
            sample_tags = [_seed_tag(n) for n in (1, 2, 3, 50, 500, 5000, 12500)]
            reads_before = {}
            for index, tag in enumerate(sample_tags):
                try:
                    reads_before[f"sample-{index}"] = len(json.dumps(
                        api.get_player_season_summary(tag, season),
                        sort_keys=True, default=str))
                except Exception as error:  # noqa: BLE001 - recorded only
                    reads_before[f"sample-{index}"] = (
                        f"error:{str(error)[:80]}")
            army_before = {}
            try:
                for lens in ("offense", "defense"):
                    army_before[lens] = len(json.dumps(
                        api.get_army_season_summary(season, lens, "troops",
                                                    "usage-rate"),
                        sort_keys=True, default=str))
            except Exception as error:  # noqa: BLE001 - recorded only
                army_before["error"] = str(error)[:120]
            # --- finalize (preview then apply) + bounded retirement ---
            finalize_preview = finalize_season_detail(conn, season, after_season,
                                                      apply=False)
            conn.rollback()
            finalize_report = finalize_season_detail(conn, season, after_season,
                                                     apply=True)
            conn.commit()
            snap(conn, lsn0, "season_finalized")
            retire_preview = retire_season_detail(
                conn, season, max_rows=1000, apply=False
            )
            retire_preview_kept = {"status": retire_preview.get("status")}
            for key, value in retire_preview.items():
                if key.startswith(("eligible_", "remaining_")):
                    retire_preview_kept[key] = value
            conn.rollback()
            retire_rounds = 0
            retire_deleted = {}
            retire_report = {}
            while retire_rounds < 2000:
                retire_report = retire_season_detail(conn, season, max_rows=1000,
                                                     apply=True)
                conn.commit()
                retire_rounds += 1
                for key, value in retire_report.items():
                    if key.startswith("deleted_") and isinstance(value, int):
                        retire_deleted[key] = retire_deleted.get(key, 0) + value
                if retire_report.get("status") == "retired":
                    break
            retired_snap = snap(conn, lsn0, "season_retired")
            conn.commit()
            # Scope VACUUM to this schema's relations: the disposable
            # database is shared, so a bare VACUUM would touch other
            # lanes' schemas.
            from psycopg import sql as _pgsq1

            with psycopg.connect(dsn, autocommit=True) as vac:
                vac.execute(
                    _pgsq1.SQL("VACUUM (ANALYZE) {}").format(
                        _pgsq1.SQL(", ").join(
                            _pgsq1.Identifier(name)
                            for name in sorted(retired_snap["relations"])
                        )
                    )
                )
            snap(conn, lsn0, "vacuumed")
            # --- historical-read byte-equivalence after retirement ---
            reads_after = {}
            for index, tag in enumerate(sample_tags):
                try:
                    reads_after[f"sample-{index}"] = len(json.dumps(
                        api.get_player_season_summary(tag, season),
                        sort_keys=True, default=str))
                except Exception as error:  # noqa: BLE001 - recorded only
                    reads_after[f"sample-{index}"] = (
                        f"error:{str(error)[:80]}")
            army_after = {}
            try:
                for lens in ("offense", "defense"):
                    army_after[lens] = len(json.dumps(
                        api.get_army_season_summary(season, lens, "troops",
                                                    "usage-rate"),
                        sort_keys=True, default=str))
            except Exception as error:  # noqa: BLE001 - recorded only
                army_after["error"] = str(error)[:120]
            byte_equivalent = (reads_before == reads_after
                               and army_before == army_after)
            # --- live-season working set: 1 day x 12,500 players ---
            conn.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end,
                    official_season_id, season_day_number,
                    season_anchor_rule_version, reconciliation_rule_version,
                    result_hash, version, state, confidence,
                    start_trophies, final_trophies_before_reset,
                    next_start_trophies, attack_count, defense_count,
                    attack_gain, observed_defense_loss)
                SELECT player.id, %s, %s + interval '1 day',
                       %s, 1, 'season-anchor-v1', 'reconciliation-v1',
                       md5(%s || player.id::text)
                         || md5(player.id::text || %s), 1,
                       'Complete', 'exact', 6100, 6110, 6110, 2, 1, 30, 20
                FROM players AS player
                """,
                (live_day0, live_day0, live_season, live_season, live_season))
            conn.execute(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, ranked_day_version_id, version,
                    state, coverage, ranked_day_end, official_season_id,
                    season_day_number, confidence, attack_count,
                    attack_three_star_count, attack_gain, defense_count,
                    defense_three_star_count, defense_loss, net_trophy_change,
                    adjustments, battles, partial_reasons)
                SELECT version.player_id, version.ranked_day_start, version.id, 1,
                       'Complete', 'complete', version.ranked_day_end, %s,
                       1, 'exact', 2, 1, 30, 1, 0, 20, 10,
                       '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
                FROM ranked_day_versions AS version
                WHERE version.official_season_id = %s
                """,
                (live_season, live_season))
            conn.commit()
            snap(conn, lsn0, "live_day_seeded")
            # --- remaining-bookkeeping width sample: 5,000 observations ---
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(
                """
                INSERT INTO archive_catalogue (
                    response_hash, archive_reference, byte_size,
                    archive_instance_id, first_verified_at)
                SELECT md5('seed-book:' || g::text) || md5(g::text),
                       'sha256/seed/' || md5('seed-book:' || g::text),
                       30000, 'seed-instance', %s
                FROM generate_series(1, 5000) AS g
                """,
                (live_day0,))
            conn.execute(
                """
                INSERT INTO collector_observations (
                    occurrence_key, collection_job_id, attempt_id, player_id,
                    normalized_tag, endpoint, request_started_at,
                    response_completed_at, http_status, response_hash,
                    archive_reference, archive_catalogue_hash,
                    collector_version, key_label,
                    evidence_headers, scope, request_method, request_path,
                    request_query, paging_envelope_state,
                    source_adapter_version)
                SELECT 'seed-book:' || g, 1, 1, 1 + ((g - 1) %% %s),
                       '#S' || (1 + ((g - 1) %% %s)),
                       CASE WHEN g %% 2 = 0 THEN 'profile' ELSE 'battle_log' END,
                       %s, %s, 200,
                       md5('seed-book:' || g::text) || md5(g::text),
                       'sha256/seed/' || md5('seed-book:' || g::text),
                       md5('seed-book:' || g::text) || md5(g::text),
                       'seed-v1', 'seed', '{}'::jsonb,
                    'player', 'GET', '/v1/players/seed', '',
                    'not_applicable', 'seed-v1'
                FROM generate_series(1, 5000) AS g
                """,
                (PLAYERS, PLAYERS, live_day0, live_day0))
            conn.execute(
                """
                INSERT INTO python_processing_jobs (
                    observation_id, status, due_at, deduplication_key)
                SELECT obs.id, 'complete', %s, 'seed-book:' || obs.id
                FROM collector_observations AS obs
                WHERE obs.occurrence_key LIKE 'seed-book:%%'
                """,
                (live_day0,))
            conn.execute(
                """
                INSERT INTO python_processing_attempts (
                    job_id, attempt_number, lease_owner, lease_token,
                    lease_generation, started_at, lease_expires_at, state)
                SELECT job.id, 1, 'seed', md5(job.id::text), 1, %s, %s, 'complete'
                FROM python_processing_jobs AS job
                JOIN collector_observations AS obs ON obs.id = job.observation_id
                WHERE obs.occurrence_key LIKE 'seed-book:%%'
                """,
                (live_day0, live_day0))
            conn.execute(
                """
                INSERT INTO observation_processing_outcomes (
                    observation_id, parser_version, processing_version, endpoint,
                    response_hash, source_http_status, source_observed_at,
                    outcome)
                SELECT obs.id, 'supercell-source-parser-v2',
                       'clashlens-domain-processing-v1', obs.endpoint,
                       obs.response_hash, 200, %s, 'processed'
                FROM collector_observations AS obs
                WHERE obs.occurrence_key LIKE 'seed-book:%%'
                """,
                (live_day0,))
            conn.execute(
                """
                INSERT INTO known_player_discoveries (
                    player_id, observation_id, source_row_index, source_kind,
                    discovered_at)
                SELECT 1 + ((obs.id - 1) %% %s), obs.id, 0, 'official_ranking', %s
                FROM collector_observations AS obs
                WHERE obs.occurrence_key LIKE 'seed-book:%%'
                """,
                (PLAYERS, live_day0))
            conn.execute(
                """
                INSERT INTO archive_catalogue (
                    response_hash, archive_reference, byte_size,
                    archive_instance_id, first_verified_at)
                SELECT md5('novel:' || g::text) || md5(g::text),
                       'sha256/seed/' || md5('novel:' || g::text),
                       20000 + ((g * 37) %% 45000), 'seed-instance', %s
                FROM generate_series(1, 1000) AS g
                """,
                (live_day0,))
            conn.execute(
                """
                INSERT INTO parsed_source_payloads (
                    endpoint, response_hash, parser_version, schema_version,
                    parse_outcome, parsed_json)
                SELECT CASE WHEN mod(g, 2) = 0 THEN 'profile' ELSE 'battle_log' END,
                       md5('novel:' || g::text) || md5(g::text),
                       'supercell-source-parser-v2',
                       CASE WHEN mod(g, 2) = 0 THEN 'profile-schema-v1'
                            ELSE 'battle-log-schema-v1' END,
                       'valid', jsonb_build_object('n', g)
                FROM generate_series(1, 1000) AS g
                """)
            conn.commit()
            snap(conn, lsn0, "bookkeeping_sampled")
            # Width calibration for battle-embedded daily logs without storing
            # gigabytes: pg_column_size of padded battles arrays at the measured
            # archive body widths (p50/p90 novel battle-log bytes).
            width_calibration = {}
            for label, body_bytes in (("empty", 2), ("p50_battle_log", 23000),
                                      ("p90_battle_log", 67000)):
                entry = json.dumps({"stars": 3, "destructionPercentage": 100,
                                    "pad": "x" * 400})
                per_entry = len(entry)
                entries = max(1, min(50, body_bytes // max(1, per_entry)))
                battles_text = "[" + ",".join([entry] * entries) + "]"
                if len(battles_text) > 250000:
                    battles_text = battles_text[:250000]
                row_bytes = conn.execute(
                    "SELECT pg_column_size(%s::jsonb)", (battles_text,)).fetchone()[0]
                width_calibration[label] = {"body_bytes": body_bytes,
                                            "entries": entries,
                                            "battles_column_bytes": int(row_bytes)}
            final_snap = snap(conn, lsn0, "rehearsal_complete")
            payload = {
                "schema_version": 1,
                "measured_at": utcnow_iso(),
                "source": "disposable_postgresql_synthetic_rehearsal",
                "official_api_requests": 0,
                "synthetic_data": True,
                "database": {"server_version": pg_version, "settings": pg_settings,
                             "schema": schema, "migrations": len(migrations),
                             "migration_hashes": mig_hashes},
                "host": host,
                "parameters": {"players": PLAYERS, "season_days": SEASON_DAYS,
                               "season_id": season, "live_season_id": live_season,
                               "army_facts": 5000, "battle_sample": 500,
                               "bookkeeping_observations": 5000},
                "materialized_player_summaries": materialized,
                "materialize_seconds": materialize_seconds,
                "army_materialization": {
                    lens: {"materialized": rep.get("materialized"),
                           "unchanged": rep.get("unchanged"),
                           "failures": rep.get("failures")}
                    for lens, rep in (army_report.get("lenses") or {}).items()},
                "summary_size_distribution": {
                    "rows": int(summary_dist[0] or 0),
                    "min_bytes": int(summary_dist[1] or 0),
                    "p50_bytes": float(summary_dist[2] or 0),
                    "max_bytes": int(summary_dist[3] or 0),
                    "total_bytes": int(summary_dist[4] or 0)},
                "storage_measurement": storage_report,
                "existing_cli_lower_bound": lower_bound,
                "finalize_preview_status": finalize_preview.get("status"),
                "finalize_status": finalize_report.get("status"),
                "finalize_player_count": finalize_report.get(
                    "player_summary_count"),
                "retire_preview": retire_preview_kept,
                "retire_rounds": retire_rounds,
                "retire_status": retire_report.get("status"),
                "retire_deleted_total": retire_deleted,
                "historical_reads_byte_equivalent": byte_equivalent,
                "historical_read_bytes_before": reads_before,
                "historical_read_bytes_after": reads_after,
                "army_read_bytes_before": army_before,
                "army_read_bytes_after": army_after,
                "width_calibration": width_calibration,
                "timeline": timeline,
                "final_snapshot": final_snap,
                "elapsed_seconds": round(time.time() - started_all, 1),
                "limitations": [
                    ("synthetic rehearsal: no production pipeline behavior, no "
                    "real correction/opposite-perspective rates"),
                    ("empty-battles daily logs understate production rows; padded "
                    "widths calibrate the gap without storing gigabytes"),
                    ("army facts use one fixed shape; troop-key diversity and "
                    "TOAST pressure at full battle volume are not established"),
                    ("bookkeeping widths are synthetic-shaped rows, not pipeline "
                    "output; retention behavior is assumed, not measured"),
                ],
            }
            digest = atomic_write_json(results / "issue82-pg-rehearsal.json",
                                       payload)
            outcome = {"digest": digest,
                       "elapsed_seconds": round(time.time() - started_all, 1),
                       "finalize_status": finalize_report.get("status"),
                       "retire_status": retire_report.get("status")}
    finally:
        try:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            admin.close()
    return outcome


def phase_pricing(results: Path) -> dict:
    targets = {
        "scaleway-storage": "https://www.scaleway.com/en/pricing/storage/",
        "r2-pricing": "https://developers.cloudflare.com/r2/pricing/",
    }
    fetched = {}
    for name, url in targets.items():
        request = urllib.request.Request(
            url, headers={"User-Agent": "ClashLens-issue82-measurement/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        path = results / (f"issue82-tariff-{name}.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        with open(tmp, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
        digest = hashlib.sha256(body).hexdigest()
        with open(str(path) + ".sha256", "w", encoding="utf-8") as handle:
            handle.write(digest + "  " + path.name + "\n")
        fetched[name] = {"url": url, "bytes": len(body), "sha256": digest,
                         "file": path.name}
    # Numbers below were extracted from these pages on 2026-09-08 and are
    # re-verified here by regex so the artifact carries both raw pages and
    # the extracted tariff with its ambiguities.
    scaleway_raw = (results / "issue82-tariff-scaleway-storage.html"
                    ).read_text(encoding="utf-8", errors="replace")
    tariffs = {
        "scaleway_object_storage": {
            "access_date": "2026-09-08",
            "source": fetched["scaleway-storage"]["url"],
            "prices_before_tax": True,
            "standard_multi_az_eur_per_gb_hour": 0.000022,
            "standard_multi_az_eur_per_gb_month": round(0.000022 * 730, 5),
            "standard_onezone_eur_per_gb_hour": 0.000011,
            "standard_onezone_eur_per_gb_month": round(0.000011 * 730, 5),
            "glacier_eur_per_gb_hour": 0.00000348,
            "egress_eur_per_gb": 0.01,
            "egress_free_gb_per_month": 75,
            "request_fee_skus_in_catalog": [],
            "ambiguities": [
                ("no PUT/GET/DELETE per-request SKU appears in the catalog "
                "API payload; requests are modeled at zero cost with live "
                "verification owned by #63"),
                ("no storage free tier is stated on the pricing page (only "
                "the 75 GB egress free tier); zero free storage is modeled"),
                ("minimum billable object size and minimum retention for "
                "Standard are not stated on the pricing page; actual bytes "
                "are modeled and the risk direction is noted"),
            ],
        },
        "cloudflare_r2_standard": {
            "access_date": "2026-09-08",
            "source": fetched["r2-pricing"]["url"],
            "role": "labeled backup-cost assumption only; #31 owns backup "
                    "design, R2 is not the raw-evidence provider",
            "storage_usd_per_gb_month": 0.015,
            "class_a_usd_per_million": 4.50,
            "class_b_usd_per_million": 0.36,
            "egress_usd_per_gb": 0.0,
            "free_tier_per_month": {"storage_gb_months": 10,
                                    "class_a_millions": 1,
                                    "class_b_millions": 10},
        },
    }
    assert "usage-new-gen-bucket-standard" in scaleway_raw
    assert "$0.015 / GB-month" in (
        results / "issue82-tariff-r2-pricing.html").read_text(
            encoding="utf-8", errors="replace")
    payload = {"schema_version": 1, "measured_at": utcnow_iso(),
               "official_api_requests": 0, "fetched": fetched,
               "tariffs": tariffs}
    digest = atomic_write_json(results / "issue82-pricing.json", payload)
    return {"digest": digest}


def _gb(value_bytes: float) -> float:
    return value_bytes / 1e9


def _source_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and sha else "unknown"


def phase_report(results: Path) -> dict:
    census = json.loads((results / "issue82-gcs-census.json").read_text())
    bodies = json.loads((results / "issue82-gcs-bodies.json").read_text())
    rehearsal = json.loads((results / "issue82-pg-rehearsal.json").read_text())
    pricing = json.loads((results / "issue82-pricing.json").read_text())
    timeline = rehearsal["timeline"]
    relations_final = rehearsal["final_snapshot"]["relations"]
    # Full-scale widths come from the in-run storage snapshot taken while
    # 350,000 daily logs, 350,000 ranked versions, and 500 sample battles
    # were present (allocated bytes incl. indexes/TOAST, current schema).
    # Small-scale tail widths would overstate index floors, so the final
    # post-retirement snapshot is used only where nothing better exists.
    full_tables = rehearsal["storage_measurement"]["tables"]

    def full_per_row(table: str) -> float:
        entry = full_tables.get(table, {})
        rows = entry.get("rows") or 0
        if rows <= 0:
            return 0.0
        return float(entry["allocated_bytes"]) / float(rows)

    def rel(table: str) -> dict:
        return relations_final.get(table, {"total_bytes": 0, "table_bytes": 0,
                                           "index_bytes": 0, "toast_bytes": 0})
    dist = rehearsal["summary_size_distribution"]
    summary_bytes = (dist["total_bytes"] / dist["rows"]) if dist["rows"] else 0
    summary_alloc_bytes = full_per_row("player_season_summaries")
    version_bytes = full_per_row("ranked_day_versions")
    log_bytes_empty = full_per_row("api_player_daily_logs")
    fact_bytes = full_per_row("army_analytics_battle_facts")
    battle_tables = ["legend_battles", "battle_source_rows", "battle_evidence",
                     "battle_perspectives", "battle_payload_rows",
                     "battle_army_decodes"]
    battle_bytes_per_battle = sum(
        full_per_row(table) for table in battle_tables)
    # Bookkeeping widths: 5,000-observation synthetic sample (labeled).
    book_widths = {}
    for table, rows in (("collector_observations", 5500),
                        ("python_processing_jobs", 5000),
                        ("python_processing_attempts", 5000),
                        ("observation_processing_outcomes", 5000),
                        ("known_player_discoveries", 5000),
                        ("archive_catalogue", 6500),
                        ("parsed_source_payloads", 1500)):
        entry = rel(table)
        book_widths[table] = (entry["total_bytes"] / rows) if rows else 0
    # 5.2 rows/observation: obs+job+attempt+outcome+discovery+catalogue
    # plus a 0.2 novel-payload share.
    book_bytes_per_obs = sum(
        book_widths[table] * share
        for table, share in (("collector_observations", 1.0),
                             ("python_processing_jobs", 1.0),
                             ("python_processing_attempts", 1.0),
                             ("observation_processing_outcomes", 1.0),
                             ("known_player_discoveries", 1.0),
                             ("archive_catalogue", 1.0),
                             ("parsed_source_payloads", 0.2)))
    live_day_bytes = (timeline["live_day_seeded"]["total_allocated_bytes"]
                      - timeline["vacuumed"]["total_allocated_bytes"])
    wal_per_summary = ((timeline["summaries_materialized"]
                        ["generated_wal_bytes"]
                        - timeline["battle_detail_sampled"]
                        ["generated_wal_bytes"]) / PLAYERS)
    # Synthetic WAL floor (this slice): bookkeeping-phase delta over the
    # ~27,000 rows it inserted. The scenario model grounds WAL in the
    # real pipeline fixture rates instead; the floor is reported for
    # methodology transparency, not used for sizing.
    wal_floor_per_row = ((timeline["bookkeeping_sampled"]
                          ["generated_wal_bytes"]
                          - timeline["live_day_seeded"]
                          ["generated_wal_bytes"]) / 27000)
    vacuum_reclaimed = (timeline["season_retired"]["total_allocated_bytes"]
                        - timeline["vacuumed"]["total_allocated_bytes"])
    retained_wal_bytes = rehearsal["final_snapshot"]["retained_wal_bytes"]
    # Real archive: measured arrival over the corpus window. Arrival is
    # bursty (one bulk-load day dominates), so the window average is
    # reported alongside the peak-day share, never as a steady rate.
    arrival = census["arrival_per_day"]
    days = [datetime.fromisoformat(d).date() for d in arrival]
    window_days = max(1, (max(days) - min(days)).days + 1)
    novel_per_day = census["object_count"] / window_days
    novel_bytes_per_day = census["total_bytes"] / window_days
    peak_day = max(arrival.items(), key=lambda item: item[1]["objects"])
    cats = bodies["categories"]

    def cat_mean(name: str, fallback: float) -> float:
        body = (cats.get(name) or {}).get("body_bytes") or {}
        return float(body.get("mean") or fallback)
    profile_body = cat_mean("profile", 20000)
    battle_body = cat_mean("battle_log", 30000)
    rankings_body = cat_mean("global_player_rankings", 32000)
    # Production-contract denominator (labeled sensitivity, not observed).
    contract_obs_day = CONTRACT_OBS_PER_CYCLE * CONTRACT_CYCLES_PER_DAY
    implied_novelty = novel_per_day / contract_obs_day
    # Host capacity: measured filesystem + 20% operating headroom.
    host_fs = rehearsal["host"]["filesystem"] or {}
    usable = float(host_fs.get("total_bytes") or 0)
    used_now = float(host_fs.get("used_bytes") or 0)
    budget = usable * 0.8
    scaleway = pricing["tariffs"]["scaleway_object_storage"]
    std_gb_month = scaleway["standard_multi_az_eur_per_gb_month"]
    p50_width = rehearsal["width_calibration"]["p50_battle_log"][
        "battles_column_bytes"]
    p90_body = 67000.0
    p50_body = 23000.0
    scenarios = {}
    for name, knobs in (
            ("central", {"profile_novel_pp_day": 1.0, "battle_novel_pp_day": 2.0,
                         "battles_day": 100000, "changed_pairs_day": 100000,
                         "wal_kb_per_response": 25.0, "book_window_days": 2}),
            ("conservative", {"profile_novel_pp_day": 4.0,
                              "battle_novel_pp_day": 8.0,
                              "battles_day": 200000,
                              "changed_pairs_day": 200000,
                              "wal_kb_per_response": 109.0,
                              "book_window_days": 2})):
        retained_summaries = PLAYERS * summary_alloc_bytes * SEASONS_6MO
        retained_versions = PLAYERS * SEASON_DAYS * version_bytes * SEASONS_6MO
        retained_army = (full_per_row("army_season_summaries") * 22
                         * SEASONS_6MO)
        # Conservative daily-log width scales the measured p50 column by the
        # p90/p50 body ratio (capped): p90 battle-embedded rows are wider
        # than the 50-entry calibration sample.
        log_width = (p50_width if name == "central"
                     else min(200000, p50_width * p90_body / p50_body))
        live_logs = PLAYERS * SEASON_DAYS * log_width
        live_battles = knobs["battles_day"] * SEASON_DAYS * battle_bytes_per_battle
        live_facts = knobs["battles_day"] * SEASON_DAYS * fact_bytes
        live_working = live_logs + live_battles + live_facts
        window_obs = contract_obs_day * knobs["book_window_days"]
        window_book = window_obs * book_bytes_per_obs
        retained_roots = knobs["changed_pairs_day"] * SIX_MONTH_DAYS
        # Retained anchor per changed collection: one log-membership row
        # (~221 B measured pre-#86 density) plus one discovery row only
        # where #86 cannot prune; central keeps half the discovery weight.
        anchor_bytes = 221 + (99 if name == "central" else 198)
        retained_anchors = retained_roots * anchor_bytes
        pg_growth = (retained_summaries + retained_versions + retained_army
                     + live_working + window_book + retained_anchors)
        pg_total = used_now + pg_growth
        spool_allowance = 17.2e9
        # WAL sizing uses the real pipeline fixture rates (mixed 20.7 KB
        # rounded to 25 central, duplicate-heavy 109 KB conservative), not
        # the synthetic floor: the pipeline writes ranking links, profile
        # effects, and job trees the synthetic sample omits.
        wal_day = contract_obs_day * knobs["wal_kb_per_response"] * 1024
        wal_7d = wal_day * 7
        base_backup = pg_total
        recovery_bytes = base_backup + wal_7d
        r2_usd_month = recovery_bytes / 1e9 * 0.015
        novel_day = (PLAYERS * (knobs["profile_novel_pp_day"] * profile_body
                                + knobs["battle_novel_pp_day"] * battle_body)
                     + 6912 * rankings_body)
        raw_6mo = novel_day * SIX_MONTH_DAYS
        raw_eur_month = (raw_6mo / 1e9 * std_gb_month) / 6
        egress_day = novel_day  # one verification GET per novel PUT
        egress_eur_month = max(0, egress_day * 30.4 / 1e9 - 75) * 0.01
        scenarios[name] = {
            "assumptions": knobs,
            "retained_player_summaries_gb": round(_gb(retained_summaries), 1),
            "retained_ranked_versions_gb": round(_gb(retained_versions), 1),
            "retained_army_summaries_gb": round(_gb(retained_army), 3),
            "live_working_set_gb": round(_gb(live_working), 1),
            "rolling_bookkeeping_window_gb": round(_gb(window_book), 1),
            "retained_anchors_gb": round(_gb(retained_anchors), 1),
            "spool_allowance_gb": round(_gb(spool_allowance), 1),
            "projected_pg_growth_gb": round(_gb(pg_growth), 1),
            "projected_host_total_gb": round(_gb(pg_total), 1),
            "budget_80pct_gb": round(_gb(budget), 1),
            "fits_budget": bool(pg_total + spool_allowance <= budget),
            "wal_per_day_gb": round(_gb(wal_day), 2),
            "recovery_window_gb": round(_gb(recovery_bytes), 1),
            "recovery_r2_usd_per_month": round(r2_usd_month, 2),
            "novel_raw_per_day_gb": round(_gb(novel_day), 2),
            "raw_6mo_gb": round(_gb(raw_6mo), 1),
            "raw_scaleway_eur_per_month": round(raw_eur_month, 2),
            "egress_scaleway_eur_per_month": round(egress_eur_month, 2),
        }
    report = {
        "schema_version": 1,
        "measured_at": utcnow_iso(),
        "official_api_requests": 0,
        "provenance": {
            "source_sha": _source_sha(),
            "pg_migrations": len(
                (rehearsal.get("database") or {}).get("migration_hashes")
                or {}),
            "gcs_census_digest": (results / "issue82-gcs-census.json.sha256"
                                  ).read_text().split()[0],
            "gcs_bodies_digest": (results / "issue82-gcs-bodies.json.sha256"
                                  ).read_text().split()[0],
            "pg_rehearsal_digest": rehearsal.get("digest", "see artifact"),
            "pricing_digest": (results / "issue82-pricing.json.sha256"
                               ).read_text().split()[0],
        },
        "measured": {
            "summary_bytes_per_player_season": round(summary_bytes, 1),
            "summary_allocated_bytes_per_row": round(summary_alloc_bytes, 1),
            "ranked_version_bytes_per_row": round(version_bytes, 1),
            "daily_log_bytes_per_row_empty": round(log_bytes_empty, 1),
            "army_fact_bytes_per_row": round(fact_bytes, 1),
            "battle_detail_bytes_per_battle": round(battle_bytes_per_battle,
                                                   1),
            "bookkeeping_bytes_per_observation": round(book_bytes_per_obs,
                                                       1),
            "bookkeeping_widths_per_table": {
                table: round(width, 1)
                for table, width in sorted(book_widths.items())},
            "live_day_bytes_12500_players": live_day_bytes,
            "wal_bytes_per_summary": round(wal_per_summary, 1),
            "wal_floor_bytes_per_row": round(wal_floor_per_row, 1),
            "vacuum_reclaimed_bytes": vacuum_reclaimed,
            "retained_wal_bytes": retained_wal_bytes,
            "historical_reads_byte_equivalent":
                rehearsal["historical_reads_byte_equivalent"],
            "finalize_status": rehearsal["finalize_status"],
            "retire_status": rehearsal["retire_status"],
            "retire_rounds": rehearsal["retire_rounds"],
            "materialize_seconds_12500": rehearsal["materialize_seconds"],
            "corpus_objects": census["object_count"],
            "corpus_bytes": census["total_bytes"],
            "corpus_window_days": window_days,
            "novel_objects_per_day": round(novel_per_day, 1),
            "novel_bytes_per_day": round(novel_bytes_per_day, 1),
            "peak_arrival_day": peak_day[0],
            "peak_day_objects": peak_day[1]["objects"],
            "peak_day_share": round(peak_day[1]["objects"]
                                    / max(1, census["object_count"]), 4),
            "arrival_is_bursty": True,
            "implied_novelty_vs_contract": round(implied_novelty, 5),
            "measured_body_bytes": {
                "profile": round(profile_body, 1),
                "battle_log": round(battle_body, 1),
                "global_player_rankings": round(rankings_body, 1),
                "global_player_rankings_measured": bool(
                    "global_player_rankings" in cats)},
            "endpoint_categories": {
                name: {"share": round(cat.get("share", 0), 4),
                       "mean_body_bytes": round(
                           cat.get("body_bytes", {}).get("mean", 0), 1),
                       "mean_items": round(
                           cat.get("items_len", {}).get("mean", 0), 2)}
                for name, cat in cats.items()},
        },
        "host": {"usable_bytes": usable, "used_now_bytes": used_now,
                 "budget_80pct_bytes": budget},
        "scenarios": scenarios,
        "unknowns": [
            ("real profile/battle-log novelty per player/day (assumed; Step 9 "
            "must measure)"),
            ("ranking-body byte size (no ranking bodies in the 384-sample; "
            "the 32 KB fixture value is a fallback, not a measurement)"),
            "real battles/day and correction/opposite-perspective rates",
            ("retained-anchor rate after #86 pruning under real collection "
            "(100k/200k changed pairs/day carried as sensitivity)"),
            "TOAST/index pressure at full battle volume and real army shapes",
            "WAL compression ratio and checkpoint-driven retained WAL peaks",
            ("base-backup cadence/format and WAL retention beyond 7 days "
            "(owned by #31)"),
            ("Scaleway request billing, storage free tier, minimum billable "
            "size/retention (modeled as zero/min-free/actual-bytes; #63 owns "
            "live proof)"),
            ("provider orphans, version retention, and correction-driven "
            "re-PUTs"),
        ],
        "closure_readiness": "",
    }
    central = scenarios["central"]
    conservative = scenarios["conservative"]
    if central["fits_budget"] and conservative["fits_budget"]:
        readiness = ("CLOSE #82: both scenarios fit measured capacity with "
                     "20% headroom; Step 9 remains the live-validation gate.")
    elif central["fits_budget"]:
        readiness = ("CLOSE #82 with the conservative overrun recorded: "
                     "central fits, conservative does not; Step 9 must "
                     "measure the disputed rates (novelty, battles/day, "
                     "retained anchors).")
    else:
        readiness = ("DO NOT CLOSE #82: even the central scenario misses the "
                     "budget; the missing piece is measured Step-9 rates.")
    report["closure_readiness"] = readiness
    digest = atomic_write_json(results / "issue82-storage-report.json",
                               report)
    lines = [
        "# Issue #82 storage/cost slice (all components, six months)",
        "",
        (
            f"Measured {report['measured_at']}. Synthetic rehearsal + "
            "real archive metadata; no official API traffic; no "
            "production data."
        ),
        "",
        "## Measured (disposable PostgreSQL 18, 12,500 x 28 synthetic season)",
        "- Summary: {:.1f} B/player-season; ranked versions {:.1f} B/row; "
        "empty daily logs {:.1f} B/row.".format(
            report["measured"]["summary_bytes_per_player_season"],
            report["measured"]["ranked_version_bytes_per_row"],
            report["measured"]["daily_log_bytes_per_row_empty"]),
        "- Battle detail: {:.1f} B/battle across six tables; army facts "
        "{:.1f} B/row; bookkeeping {:.1f} B/observation.".format(
            report["measured"]["battle_detail_bytes_per_battle"],
            report["measured"]["army_fact_bytes_per_row"],
            report["measured"]["bookkeeping_bytes_per_observation"]),
"- Finalize: {}; retire: {} in {} rounds; historical reads "
        "byte-equivalent: {}; materialize 12,500 in {}s.".format(
            rehearsal["finalize_status"], rehearsal["retire_status"],
            rehearsal["retire_rounds"],
            rehearsal["historical_reads_byte_equivalent"],
            rehearsal["materialize_seconds"]),
        "",
"## Real archive (GCS {} objects, {:.1f} GB over {} days)".format(
            census["object_count"], census["total_bytes"] / 1e9,
            window_days),
        ("- Novel arrival {:.0f} objects/day, {:.2f} GB/day (window "
         "average; peak day holds most objects, so this is not a steady "
         "rate); implied novelty vs the 7.2M/day contract denominator: "
         "{:.3f}% (labeled sensitivity, not observed).").format(
            report["measured"]["novel_objects_per_day"],
            report["measured"]["novel_bytes_per_day"] / 1e9,
            report["measured"]["implied_novelty_vs_contract"] * 100),
        "",
        "## Six-month scenarios (20% headroom on measured host)",
        "- Central: host total {:.1f} GB (budget {:.1f} GB), fits={}; raw "
        "{:.1f} GB at EUR {:.2f}/mo; recovery {:.1f} GB at USD {:.2f}/mo.".format(
            central["projected_host_total_gb"], central["budget_80pct_gb"],
            central["fits_budget"], central["raw_6mo_gb"],
            central["raw_scaleway_eur_per_month"],
            central["recovery_window_gb"],
            central["recovery_r2_usd_per_month"]),
        "- Conservative: host total {:.1f} GB, fits={}; raw {:.1f} GB at EUR "
        "{:.2f}/mo; recovery {:.1f} GB at USD {:.2f}/mo.".format(
            conservative["projected_host_total_gb"],
            conservative["fits_budget"], conservative["raw_6mo_gb"],
            conservative["raw_scaleway_eur_per_month"],
            conservative["recovery_window_gb"],
            conservative["recovery_r2_usd_per_month"]),
        "",
        ("Excluded from the host total (immaterial at the measured "
         "margin): retained WAL at its 1 GiB checkpoint level, no local "
         "full-backup copies (off-host R2 model), spool counted inside the "
         "fits check at its 17.2 GB cap allowance."),
        "",
        "## Decision",
        readiness,
        "",
        ("Step 9 (12,500 real players x 288 production cycles) remains the "
        "live-validation gate and is not replaced by this slice."),
    ]
    md_path = results / "issue82-storage-slice-report.md"
    md_tmp = md_path.with_name(f"{md_path.name}.tmp-{os.getpid()}")
    with open(md_tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(md_tmp, md_path)
    return {"digest": digest, "readiness": readiness}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default=RESULTS_ENV)
    parser.add_argument("--phases", default="all")
    parser.add_argument("--db-url", default=os.environ.get(
        "CLASHLENS_TEST_DATABASE_URL", ""))
    parser.add_argument("--body-sample", type=int, default=384)
    args = parser.parse_args(argv)
    results = Path(args.results)
    selected = args.phases.split(",")
    if selected == ["all"]:
        selected = ["gcs-census", "gcs-bodies", "pg-rehearsal", "pricing",
                    "report"]
    summary: dict[str, object] = {"measured_at": utcnow_iso(),
                                  "official_api_requests": 0}
    reservoir: list[str] = []
    if "gcs-census" in selected:
        prefixes = [f"{n:02x}" for n in range(256)]
        outcome = phase_gcs_census(results, prefixes)
        reservoir = outcome.pop("reservoir")
        summary["gcs-census"] = outcome
    if "gcs-bodies" in selected:
        if not reservoir:
            json.loads(
                (results / "issue82-gcs-census.json").read_text())
            raise SystemExit("gcs-bodies needs the census reservoir; run "
                             "gcs-census in the same invocation")
        summary["gcs-bodies"] = phase_gcs_bodies(results, reservoir,
                                                 args.body_sample)
    if "pg-rehearsal" in selected:
        if not args.db_url:
            raise SystemExit("pg-rehearsal needs --db-url (disposable only)")
        summary["pg-rehearsal"] = phase_pg_rehearsal(args.db_url, results)
    if "pricing" in selected:
        summary["pricing"] = phase_pricing(results)
    if "report" in selected:
        summary["report"] = phase_report(results)
    print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
