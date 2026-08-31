from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/src"))
SCRIPT = ROOT / "scripts/operating_check.py"
SPEC = importlib.util.spec_from_file_location("operating_check", SCRIPT)
assert SPEC and SPEC.loader
operating_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operating_check)


def _metrics() -> str:
    scalars = {
        "clashlens_collector_process_start_time_seconds": 1_777_500_000,
        "clashlens_collector_database_pool_max_connections": 32,
        "clashlens_collector_database_pool_acquired_connections": 1,
        "clashlens_collector_database_pool_idle_connections": 3,
        "clashlens_collector_database_pool_empty_acquires_total": 2,
        "clashlens_collector_database_pool_cancelled_acquires_total": 0,
        "clashlens_collector_database_pool_acquire_duration_seconds_total": 0.25,
        "clashlens_spool_final_bytes": 10,
        "clashlens_spool_temporary_bytes": 2,
        "clashlens_spool_abandoned_temporary_bytes": 3,
        "clashlens_spool_final_objects": 1,
        "clashlens_spool_temporary_objects": 1,
        "clashlens_spool_abandoned_temporary_objects": 1,
        "clashlens_spool_reserved_bytes": 4,
        "clashlens_spool_live_reservations": 1,
        "clashlens_spool_high_water_bytes": 19,
        "clashlens_spool_free_bytes": 10_000,
        "clashlens_spool_free_inodes": 1000,
    }
    lines = [f"{name} {value}" for name, value in scalars.items()]
    lines.append(
        'clashlens_collector_process_identity_info{process_id="11111111111111111111111111111111"} 1'
    )
    lines.append(
        'clashlens_collector_jobs_total{work_type="regular_poll",pool="normal",outcome="handled"} 3'
    )
    lines.append(
        'clashlens_collector_api_outcomes_total{endpoint="profile",outcome="2xx"} 4'
    )
    for bound in operating_check.LATENCY_BUCKETS_SECONDS:
        value = 1 if bound >= 0.01 else 0
        lines.append(
            "clashlens_collector_stage_duration_seconds_bucket"
            f'{{stage="claim",le="{bound:g}"}} {value}'
        )
    lines.extend(
        (
            'clashlens_collector_stage_duration_seconds_bucket{stage="claim",le="+Inf"} 1',
            'clashlens_collector_stage_duration_seconds_count{stage="claim"} 1',
            'clashlens_collector_stage_duration_seconds_sum{stage="claim"} 0.01',
        )
    )
    return "\n".join(lines) + "\n"


def test_collector_metrics_are_typed_and_include_hard_spool_facts() -> None:
    result = operating_check.parse_collector_metrics(_metrics())

    assert result["process"]["id"] == "11" * 16
    assert result["outcomes"]["jobs"]["handled"] == 3
    assert result["outcomes"]["official_api"]["success"] == 4
    assert result["stages"]["claim"]["count"] == 1
    assert result["spool"]["abandoned_temporary_bytes"] == 3
    assert result["spool"]["free_bytes"] == 10_000
    assert "key_label" not in json.dumps(result)


def test_dynamic_selected_metric_label_is_rejected() -> None:
    injected = _metrics() + (
        'clashlens_collector_stage_duration_seconds_count{stage="player-SECRET"} 1\n'
    )

    with pytest.raises(ValueError, match="metrics_invalid"):
        operating_check.parse_collector_metrics(injected)


def test_unknown_clashlens_metric_is_rejected_without_echoing_its_label() -> None:
    injected = _metrics() + 'clashlens_secret_metric{tag="player-#SECRET"} 1\n'

    with pytest.raises(ValueError, match="metrics_invalid"):
        operating_check.parse_collector_metrics(injected)


def test_missing_required_metric_is_rejected_instead_of_becoming_zero() -> None:
    missing = "\n".join(
        line
        for line in _metrics().splitlines()
        if not line.startswith("clashlens_spool_free_bytes ")
    )

    with pytest.raises(ValueError, match="metrics_invalid"):
        operating_check.parse_collector_metrics(missing)


def test_database_contract_is_one_repeatable_read_only_transaction() -> None:
    sql = operating_check.DATABASE_SQL

    assert sql.count("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY") == 1
    assert sql.count("COMMIT;") == 1
    assert "normalized_tag" not in sql
    assert "archive_reference" not in sql
    assert "input_json" in sql
    assert "boundary_publication_manifests" in sql
    assert "terminal_history_rank <= 8" in sql
    assert "pg_ls_waldir" in sql
    assert "legal.status = 'pending'\n                AND legal.due_at <= clock.captured_at" in sql
    assert "job.status = 'pending'\n                AND job.due_at <= clock.captured_at" in sql


def test_relation_contract_covers_every_current_migration_table() -> None:
    current_tables: set[str] = set()
    pattern = re.compile(
        r"CREATE TABLE(?: IF NOT EXISTS)? ([a-z_][a-z0-9_]*)"
    )
    for migration in sorted((ROOT / "deploy/migrations").glob("*.sql")):
        current_tables.update(pattern.findall(migration.read_text(encoding="utf-8")))

    assert current_tables == set(operating_check.RELATION_NAMES)


def test_unreadable_source_returns_bounded_indeterminate_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        operating_check,
        "collect_snapshot",
        lambda _arguments: (_ for _ in ()).throw(ValueError("SECRET-player-#TAG")),
    )
    arguments = [
        "--postgres-container",
        "postgres",
        "--postgres-user",
        "operator",
        "--postgres-database",
        "clashlens",
        "--collector-metrics-url",
        "http://127.0.0.1:8081/metrics",
        "--python-api-container",
        "api",
        "--api-hmac-caller",
        "typescript-website",
        "--api-hmac-key-id",
        "current",
        "--api-hmac-secret-file",
        "/run/secrets/api-hmac",
        "--python-worker-container",
        "worker",
        "--worker-replicas",
        "1",
        "--spool-max-body-bytes",
        "100",
        "--spool-max-bytes",
        "1000",
        "--spool-max-objects",
        "100",
        "--spool-free-space-floor",
        "100",
        "--spool-free-inode-floor",
        "10",
    ]

    assert operating_check.main(arguments) == 2
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["check"]["status"] == "indeterminate"
    assert "SECRET" not in output.out + output.err
