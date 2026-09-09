"""Focused tests for scripts/step9_check.py (stdlib + real PostgreSQL, no skips).

Unit tests use fake adapters only. PostgreSQL tests run against a migrated
disposable schema and fail (never skip) when no database is available. Fake
stop adapters never touch real containers: they refuse any container name
that does not start with "test-".
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "python" / "tests"))
SPEC = importlib.util.spec_from_file_location(
    "step9_check", ROOT / "scripts/step9_check.py"
)
assert SPEC and SPEC.loader
step9 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(step9)

# Valid Clash-style tags (charset 0289PYLQGRJCUV).
TAGS = ["#2000QVLG", "#2829PYLU", "#289QJR90", "#28CGVQ2G"]


def _write_cohort(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _start_args(run_dir: Path, cohort: Path, **overrides):
    core_start = "2026-10-04T05:00:00Z"
    core_end = "2026-10-05T05:00:00Z"
    defaults = {
        "run_dir": str(run_dir), "podman_bin": "podman",
        "cohort_file": str(cohort), "deployed_receipt": "/nonexistent",
        "core_start": core_start, "core_end": core_end,
        "collector_container": "test-collector", "postgres_container": "test-pg",
        "python_api_container": "test-api",
        "python_worker_container": "test-worker", "worker_replicas": 1,
        "runtime_metrics_url": "http://127.0.0.1:9/runtime-metrics",
        "spool_path": "/tmp", "postgres_path": "/tmp",
        "lead_in_seconds": 0, "tail_seconds": 0,
        "deadline": "2026-10-05T05:10:00Z",
        "max_sample_age_seconds": 125, "watchdog_unit": "test-unit",
        "run_id": "testrun01", "database_url": None, "mode": "live-day",
        "max_invocation_gap_seconds": 5, "bootstrap_run_id": None,
        "archive_tariff_file": None, "archive_interfaces": ["test-eth0"],
        "archive_route_host": None, "prior_transfer_bytes": None,
        "prior_transfer_provenance": None, "prior_s3_attempts": None,
        "prior_s3_provenance": None,
    }
    defaults.update(overrides)
    if defaults.get("archive_tariff_file") is None:
        tariff_path = run_dir.parent / "tariff.json"
        if not tariff_path.exists():
            tariff_path.write_text(json.dumps(_canonical_tariff()))
        defaults["archive_tariff_file"] = str(tariff_path)
    return mock.Mock(**defaults)


def _canonical_tariff(**overrides):
    payload = {
        "source": "https://www.scaleway.com/en/pricing/storage/",
        "verified_utc_date": "2026-09-09",
        "tariff_eur_per_decimal_gb_hour": "0.000022",
        "egress_eur_per_decimal_gb": "0.01",
        "payload_cap_gib": 16,
        "aggregate_transfer_cap_gib": 64,
        "retention_projection_days": 186,
        "rounded_storage_decimal_gb": 18,
        "conservative_all_transfer_egress_decimal_gb_rounded": 69,
        "storage_projection_eur": 1.767744,
        "egress_projection_eur": 0.69,
        "combined_projection_eur": 2.457744,
        "uncertainty_multiplier": 1.5,
        "with_uncertainty_eur": 3.686616,
        "operational_stop_eur": 4.5,
        "absolute_preparation_ceiling_eur": 5,
    }
    payload.update(overrides)
    return payload


def _receipt_scope(scope: str = "deployed-stack", discovery: str = "false",
                   budget: bool = True, admission_run_id: str = "testrun01",
                   admission_start: str = "2026-10-04T05:00:00Z",
                   admission_end: str = "2026-10-05T05:00:00Z") -> dict:
    fields = {"player_discovery_enabled": discovery,
              "admission_evidence_run_id": admission_run_id,
              "admission_evidence_start": admission_start,
              "admission_evidence_end": admission_end,
              "admission_evidence_max_events": "108000",
              "admission_evidence_max_selected_entries": "5000000"}
    if budget:
        fields.update({
            "endpoint_budget_enabled": "true",
            "endpoint_budget_profile": "13500",
            "endpoint_budget_global_rankings": "1",
            "endpoint_budget_battle_log": "0",
            "endpoint_budget_run_id": "boot1",
            "endpoint_budget_deadline_at": "2026-10-04T06:00:00+00:00",
        })
    return {"receipt_scope": scope, "source": {"revision": "a" * 40},
            "receipt_digest": "sha256:" + "b" * 64,
            "configuration": {"fields": fields}}


class FakeDB:
    """In-memory stand-in for step9.Database."""

    def __init__(self, rows=(), outside: int = 0, roots: int = 0,
                 fail: str | None = None, resets=(),
                 admission_present: bool = True) -> None:
        self.rows = list(rows)
        self.outside = outside
        self.roots = roots
        self.fail = fail
        self.resets = list(resets)
        self.fixed_ids: list[int] = [r[0] for r in self.rows]
        self.admission_tables = admission_present
        self.admission_events_data: list[dict] = []
        self.admission_profile_data: dict = {}
        self.admission_roots_data: list[tuple] = []
        self.admission_header_data: dict | None = None
        self.preflight_data: dict | None = None

    def identity(self):
        return {"system_identifier": "123", "database_name": "test",
                "captured_at": "2026-10-04T05:00:00+00:00"}

    def snapshot_population(self, tags):
        if self.fail:
            raise RuntimeError(self.fail)
        return [r for r in self.rows if r[1] in set(tags)]

    def outside_active(self, ids):
        return self.outside

    def outside_roots(self, ids):
        return self.roots

    def minute_snapshot(self, ids):
        if self.fail:
            raise RuntimeError(self.fail)
        fixed = [(r[0], r[2], r[3], None, None, None) for r in self.rows]
        return {"fixed": fixed, "queues": [("pending", 1, None)],
                "python_queues": [], "counters": (7, 8, 9, 10, "0/1", "now")}

    def reset_identity(self, start, end):
        return self.resets

    # --- admission (0022-final read side) ---

    def admission_present(self):
        return self.admission_tables

    def admission_run(self, run_id):
        if self.admission_header_data is not None:
            return self.admission_header_data
        return {"run_id": run_id,
                "capture_start": datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
                "capture_end": datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
                "max_events": 108000, "max_selected_entries": 5000000,
                "events_written": 0, "selected_entries_written": 0,
                "state": "active", "stopped_at": None, "failure_code": None}

    def admission_events(self, run_id, start, end):
        return self.admission_events_data

    def admission_profile_counts(self, run_id, start, end):
        return self.admission_profile_data

    def admission_latest(self, run_id):
        if not self.admission_events_data:
            return None
        last = self.admission_events_data[-1]
        return {"id": last["id"], "database_at": last["database_at"],
                "gate_allowed": last["gate_allowed"],
                "selected_count": last["selected_count"],
                "inserted_count": last["inserted_count"],
                "advanced_count": last["advanced_count"]}

    def semantic_roots(self, start, end):
        return self.admission_roots_data

    # --- preflight ---

    def preflight_probes(self, start, end):
        if self.preflight_data is not None:
            return self.preflight_data
        return {"workcounts": [], "observations": [],
                "intents": (0, None, None), "pending_remote": 0}

    def transitions(self, ids, start, end):
        return []

    def budgets_present(self):
        return getattr(self, "budgets_tables", False)

    def bootstrap_budgets(self, run_id):
        if getattr(self, "budgets_error", None):
            raise self.budgets_error
        return getattr(self, "budgets_data",
                       {"run": None, "budgets": []})

    def wal_generated(self, since_lsn):
        return (0, 0)

    def operating_snapshot(self, relations):
        return getattr(self, "operating_data", {
            "status": "complete",
            "identity": {"status": "complete", "rows": []},
            "collector_queues": {"status": "complete", "rows": []},
            "python_queues": {"status": "complete", "rows": []},
            "relations": {"status": "complete", "rows": []},
            "processed": {"status": "complete", "rows": []},
            "failures": {"status": "complete", "rows": []}})

    def reset_members(self, sweep_id):
        return [1]

    def generation_members(self, sweep_id, boundary):
        return [(1, 1, "ab" * 64, 1)]

    def paired_baselines(self, sweep_id):
        return {1: 1}

    def active_queues(self):
        return getattr(self, "queue_residue_data",
                       {"collector": [], "python": []})


def _eligible_row(pid: int, tag: str):
    return (pid, tag, True, "eligible", None, None, 101,
            None, 105000036, "Legend I", "eligible", "accepted")


def _args_with_receipt(tmp_path: Path, name: str, cohort: Path, **overrides):
    receipt_path = tmp_path / f"{name}-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    return _start_args(tmp_path / name, cohort,
                       deployed_receipt=str(receipt_path), **overrides)


def _started_run(tmp_path: Path, db: FakeDB, **overrides):
    cohort = _write_cohort(tmp_path / "cohort.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    run_id = overrides.get("run_id", "testrun01")
    receipt_path.write_text(json.dumps(_receipt_scope(admission_run_id=run_id)))
    run_dir = tmp_path / overrides.pop("run_dir_name", "run")
    arguments = _start_args(run_dir, cohort,
                            deployed_receipt=str(receipt_path), **overrides)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        header = step9.cmd_start(arguments, db)
    return run_dir, header


def test_cohort_ok_and_digests(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS + ["", "  "])
    tags, raw, canonical = step9._read_cohort(str(cohort))
    assert tags == sorted(TAGS)
    assert len(raw) == 64 and len(canonical) == 64


def test_cohort_rejects_bad_inputs(tmp_path: Path) -> None:
    good = _write_cohort(tmp_path / "c.txt", TAGS)
    with pytest.raises(step9.Step9Error):
        step9._read_cohort("relative/path.txt")
    link = tmp_path / "link.txt"
    link.symlink_to(good)
    with pytest.raises(step9.Step9Error):
        step9._read_cohort(str(link))
    _write_cohort(tmp_path / "bad.txt", ["#NOPE"])
    with pytest.raises(step9.Step9Error):
        step9._read_cohort(str(tmp_path / "bad.txt"))
    _write_cohort(tmp_path / "dup.txt", [TAGS[0], TAGS[0].lower()])
    with pytest.raises(step9.Step9Error):
        step9._read_cohort(str(tmp_path / "dup.txt"))
    big = tmp_path / "big.txt"
    big.write_bytes(b"#2000QVLG\n" * 200000)
    with pytest.raises(step9.Step9Error):
        step9._read_cohort(str(big))


def test_start_creates_exclusive_header(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    assert header["cohort"]["input_count"] == len(TAGS)
    assert header["admission"]["status"] == "integrated"
    assert header["admission"]["schema"] == step9.ADMISSION_SCHEMA_VERSION
    assert (run_dir / "run.json").stat().st_mode & 0o777 == 0o600
    assert run_dir.stat().st_mode & 0o777 == 0o700
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error):
            step9.cmd_start(_start_args(run_dir, tmp_path / "cohort.txt",
                                        deployed_receipt=str(tmp_path /
                                                             "receipt.json")),
                            db)


def test_start_rejects_bad_contract(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error):  # 23h core
            step9.cmd_start(_args_with_receipt(tmp_path, "r1", cohort,
                                               core_end="2026-10-05T04:00:00Z"),
                            db)
        with pytest.raises(step9.Step9Error):  # non-05:00 edge
            step9.cmd_start(_args_with_receipt(tmp_path, "r2", cohort,
                                               core_start="2026-10-04T06:00:00Z",
                                               core_end="2026-10-05T06:00:00Z"),
                            db)
        with pytest.raises(step9.Step9Error):  # candidate scope receipt
            receipt_path = tmp_path / "receipt.json"
            receipt_path.write_text(
                json.dumps(_receipt_scope("candidate-preparation")))
            step9.cmd_start(_start_args(tmp_path / "r3", cohort,
                                        deployed_receipt=str(receipt_path)), db)


def test_start_fewer_than_12500_eligible_succeeds(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    _run_dir, header = _started_run(tmp_path, db)
    assert header["initial"]["eligible_count"] == 1


def test_start_zero_eligible_and_foreign_failures(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_args_with_receipt(tmp_path, "z", cohort),
                            FakeDB(rows=[]))
        assert error.value.code == "zero_eligible" and error.value.gate
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_args_with_receipt(tmp_path, "f", cohort),
                            FakeDB(rows=[_eligible_row(1, TAGS[0])], outside=2))
        assert error.value.code == "foreign_population" and error.value.gate
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_args_with_receipt(tmp_path, "g", cohort),
                            FakeDB(rows=[_eligible_row(1, TAGS[0])], roots=1))
        assert error.value.code == "foreign_lineage" and error.value.gate


def test_start_rejects_discovery_receipts(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        for scope, discovery in (("deployed-stack", "true"),
                                 ("deployed-stack", "")):
            receipt_path = tmp_path / f"r-{discovery or 'missing'}.json"
            receipt = _receipt_scope(scope, discovery)
            if not discovery:
                del receipt["configuration"]["fields"][
                    "player_discovery_enabled"]
            receipt_path.write_text(json.dumps(receipt))
            with pytest.raises(step9.Step9Error) as error:
                step9.cmd_start(_start_args(tmp_path / f"d-{discovery or 'm'}",
                                            cohort,
                                            deployed_receipt=str(receipt_path)),
                                db)
            assert error.value.code == "discovery_not_disabled"


def test_start_preflight_budget_gates(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    base = {"mode": "preflight", "core_start": "2026-10-04T05:00:00Z",
            "core_end": "2026-10-04T06:15:00Z"}
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        receipt_path = tmp_path / "b.json"
        receipt_path.write_text(json.dumps(_receipt_scope(budget=False)))
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(tmp_path / "b0", cohort,
                                        deployed_receipt=str(receipt_path),
                                        **base), db)
        assert error.value.code == "budget_not_enabled"
        receipt = _receipt_scope()
        receipt["configuration"]["fields"]["endpoint_budget_profile"] = "99999"
        receipt_path.write_text(json.dumps(receipt))
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(tmp_path / "b1", cohort,
                                        deployed_receipt=str(receipt_path),
                                        **base), db)
        assert error.value.code == "budget_exceeds_envelope"
        (tmp_path / "receipt.json").write_text(json.dumps(_receipt_scope()))
        header = step9.cmd_start(_start_args(
            tmp_path / "b2", cohort,
            deployed_receipt=str(tmp_path / "receipt.json"), **base), db)
        assert header["budget_receipt"]["run_id"] == "boot1"

def test_parse_runtime_metrics_wire_format() -> None:
    text = ("# HELP clashlens_collector_jobs_total jobs\n"
            "# TYPE clashlens_collector_jobs_total counter\n"
            'clashlens_collector_process_identity_info{process_id="abc123"} 1\n'
            "clashlens_collector_process_start_time_seconds 1700000000\n"
            'clashlens_collector_jobs_total{work_type="regular_poll",'
            'pool="normal",outcome="admitted"} 42\n'
            "clashlens_collector_database_pool_idle_connections 3\n"
            "unrelated_metric 7\n")
    parsed = step9.parse_runtime_metrics(text)
    assert parsed["process_id"] == "abc123"
    assert parsed["started_at"] == 1700000000
    key = ('clashlens_collector_jobs_total{outcome=admitted,pool=normal,'
           'work_type=regular_poll}')
    assert parsed["counters"][key] == 42
    assert ("clashlens_collector_database_pool_idle_connections{}" in
            parsed["counters"])
    assert "unrelated_metric" not in str(parsed["counters"])
    with pytest.raises(step9.Step9Error):
        step9.parse_runtime_metrics("clashlens_collector_jobs_total 1\n")
    with pytest.raises(step9.Step9Error):
        step9.parse_runtime_metrics("bogus line here\n")
    # gauges may fall without tripping counter-reset
    assert step9._check_counters_decreased(
        {"clashlens_collector_database_pool_idle_connections{}": 5},
        {"clashlens_collector_database_pool_idle_connections{}": 2}) is None
    assert step9._check_counters_decreased({"a_total{}": 5},
                                            {"a_total{}": 4}) == "a_total{}"


def test_start_requires_deployed_receipt_digest(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    seen: dict = {}

    def _validate(receipt, *, require_digest=False):
        seen["require_digest"] = require_digest
        if receipt.get("receipt_scope") != "deployed-stack":
            raise RuntimeError("scope")

    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    arguments = _start_args(tmp_path / "ok", cohort,
                            deployed_receipt=str(receipt_path))
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           side_effect=_validate):
        header = step9.cmd_start(arguments, db)
    assert seen["require_digest"] is True
    assert header["receipt_digest"] == "sha256:" + "b" * 64


def test_classify_slot_outcomes() -> None:
    base = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    assert step9.classify_slot(base, base, 0.0, 0.0, False)["outcome"] == "on_time"
    late = step9.classify_slot(base, base + timedelta(seconds=70),
                                 70.0, 70.0, False)
    assert late["outcome"] == "late" and late["failure_code"] == "sample_late"
    assert late["shift_seconds"] == 70.0
    drifted = step9.classify_slot(base, base + timedelta(seconds=3),
                                  1.0, 1.0, False)
    assert drifted["outcome"] == "on_time"
    jump = step9.classify_slot(base, base, 60.0, 5.0, False)
    assert jump["outcome"] == "clock_jump"
    boot = step9.classify_slot(base, base, 1.0, 1.0, True)
    assert boot["outcome"] == "boot_change"
    assert step9.classify_slot(base, base, 1.0, -1.0, False)["outcome"] == \
        "non_monotonic"
    assert step9.classify_slot(base, base, -0.5, 0.5, False)["outcome"] == \
        "out_of_order"


def _sample_hooks(db: FakeDB, **overrides):
    mono = [0]
    walls = [datetime(2026, 10, 4, 5, 0, tzinfo=UTC) - timedelta(seconds=60)]

    def clock():
        return mono[0]

    def now_utc():
        mono[0] += 60_000_000_000
        walls[0] += timedelta(seconds=60)
        return walls[0]

    hooks = {
        "db": db, "fixed_ids": db.fixed_ids,
        "fetch_metrics": lambda url: {"process_id": "p1", "started_at": 1.0,
                                        "counters": {"jobs_total{}": 5},
                                        "digest": "x"},
        "watchdog_check": lambda run: True, "clock": clock, "now_utc": now_utc,
        "resource_facts": lambda run, db, metrics: _quiet_facts(),
        "pgdata_probe": lambda run: {
            "status": "captured", "failure_code": None,
            "captured_at": "2026-10-04T05:00:00+00:00",
            "container": "test-pg", "image": "sha256:pg",
            "pgdata": "/var/lib/postgresql/data",
            "source": "podman-exec:test-pg",
            "pgdata_bytes": 1000, "pg_wal_bytes": 100},
        "wire_facts": lambda run: {
            "status": "captured", "failure_code": None,
            "boot_id": run.get("boot_id"),
            "interfaces": {"test-eth0": {
                "present": True, "rx_bytes": 1000, "tx_bytes": 500,
                "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}},
        "worker_probe": lambda run: [
            {"archive": {"remote_attempts": {"get": 3, "bucket": 1,
                                                "marker": 1}}}],
        "no_sleep": True, "single_pass": True, "max_slots": 1,
    }
    hooks.update(overrides)
    return hooks


def test_sample_writes_one_slot(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_sample(arguments, _sample_hooks(db)) == 0
    sample = json.loads((run_dir / "samples" / "minute-0000.json").read_text())
    assert sample["slot"] == 0 and sample["outcome"] == "on_time"
    assert sample["eligibility"]["active_fixed_count"] == 1
    assert "normalized_tag" not in json.dumps(sample)
    # Duplicate slot is a hard gate failure.
    assert step9.cmd_sample(arguments, _sample_hooks(db)) == 1
    failures = list((run_dir / "failures").glob("*.json"))
    assert failures


def test_sample_two_consecutive_unavailable_fails(tmp_path: Path) -> None:
    good = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, good)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    down = FakeDB(fail="down")

    def no_metrics(url):
        raise step9.Step9Error("metrics_unavailable", "down")

    hooks = _sample_hooks(down, fetch_metrics=no_metrics, max_slots=3,
                          single_pass=False)
    assert step9.cmd_sample(arguments, hooks) == 1
    assert (run_dir / "failures").glob("*.json")


def test_sample_records_counter_reset(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    _run_dir, run = _started_run(tmp_path, db)
    counters = [{"process_id": "p1", "started_at": 1.0,
                   "counters": {"jobs_total{}": 9}, "digest": "a"},
                {"process_id": "p1", "started_at": 1.0,
                 "counters": {"jobs_total{}": 4}, "digest": "b"}]
    sample = step9.build_sample(
        run=run, index=0, expected_utc=datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
        captured_utc=datetime(2026, 10, 4, 5, 0, 1, tzinfo=UTC),
        mono_elapsed=1.0, wall_delta=1.0, mono_delta=1.0, boot_id=run["boot_id"],
        db_facts=db.minute_snapshot([]), db_error=None,
        metrics=counters[1], metrics_error=None, previous_metrics=counters[0],
        pressure={}, fs={}, watchdog_active=True)
    assert sample["counter_reset"] == "jobs_total{}"


def test_sql_is_read_only_and_bound() -> None:
    static = {step9.SQL_OP_QUEUES, step9.SQL_OP_PYTHON_QUEUES,
              step9.SQL_OP_PROCESSED, step9.SQL_OP_FAILURES}
    for statement in step9.ALL_RO_STATEMENTS:
        assert ("%s" in statement or "IN ('pending'" in statement
                or "pg_control_system" in statement
                or "pg_current_wal_lsn" in statement
                or "to_regclass" in statement
                or "pending_remote_verification" in statement
                or "archive_catalogue" in statement
                or statement in static)
        assert "f\"" not in statement and "\" + " not in statement
        with pytest.raises(step9.Step9Error):
            step9.assert_read_only("SELECT 1; INSERT INTO players VALUES (1)")
        with pytest.raises(step9.Step9Error):
            step9.assert_read_only("VACUUM collector_jobs")


class FakePodman:
    """Fake stop adapter; refuses anything but test- containers."""

    def __init__(self, running: bool = True, stop_fails: bool = False) -> None:
        self.running = running
        self.stop_fails = stop_fails
        self.updated = False
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.commands.append(list(command))
        targets = [part for part in command if part.startswith("test-")]
        assert targets, f"refusing non-test container: {command}"
        if command[1:3] == ["container", "inspect"]:
            if any("HostConfig" in part for part in command):
                return "no\n" if self.updated else "unless-stopped\n"
            return "true\nsha256:image\n" if self.running else "false\nsha256:image\n"
        if command[1] == "update":
            self.updated = True
            return ""
        if command[1] == "stop":
            if not self.stop_fails:
                self.running = False
            return ""
        raise AssertionError(f"unexpected podman command: {command}")


def _pin_resources(run_dir: Path) -> None:
    """Test-only: align the start baseline with quiet fake loop facts."""
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resource_baseline"] = _quiet_facts()
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def _pin_wire(run_dir: Path, rx: int = 1000, tx: int = 500) -> None:
    """Test-only: set a matching wire baseline (production pins at start)."""
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["wire_baseline"] = {
        "status": "captured", "failure_code": None,
        "boot_id": payload.get("boot_id"),
        "interfaces": {"test-eth0": {
            "present": True, "rx_bytes": rx, "tx_bytes": tx,
            "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def _pin_image(run_dir: Path, image: str = "sha256:image") -> None:
    """Test-only: set the start-time image pin (production pins via podman)."""
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["containers"]["collector_image"] = image
    payload["containers"]["collector_image_error"] = None
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def _watchdog_args(run_dir: Path, **overrides):
    defaults = {"run_dir": str(run_dir), "podman_bin": "podman",
                "collector_container": "test-collector",
                "deadline": "2026-10-05T05:10:00Z",
                "max_sample_age_seconds": 125, "systemd_unit": "test-unit"}
    defaults.update(overrides)
    if defaults.get("archive_tariff_file") is None:
        tariff_path = run_dir.parent / "tariff.json"
        if not tariff_path.exists():
            tariff_path.write_text(json.dumps(_canonical_tariff()))
        defaults["archive_tariff_file"] = str(tariff_path)
    return mock.Mock(**defaults)




def test_watchdog_single_pass_and_deadline(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    _pin_image(run_dir)
    podman = FakePodman()
    arguments = _watchdog_args(run_dir)
    # inside startup grace with no samples yet -> wait, no stop
    hooks = {"podman_run": podman, "single_pass": True, "max_iterations": 1,
             "no_sleep": True,
             "now_utc": lambda: datetime(2026, 10, 4, 5, 1, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments, hooks) == 0
    assert ["podman", "update", "--restart=no", "test-collector"] in podman.commands
    watchdog = json.loads((run_dir / "watchdog.json").read_text())
    assert watchdog["prior_restart_policy"] == "unless-stopped"
    assert watchdog["verified_restart"] == "no"
    assert not (run_dir / "watchdog-outcome.json").exists()
    # past grace with inactive sampler unit -> exact stop command (fresh dir:
    # watchdog.json is exclusive and never replaced)
    run_dir2, _header2 = _started_run(tmp_path, db, run_dir_name="run2",
                                        run_id="testrun02")
    _pin_image(run_dir2)
    arguments2 = _watchdog_args(run_dir2)
    hooks = {"podman_run": podman, "single_pass": True, "max_iterations": 1,
             "no_sleep": True,
             "sampler_check": lambda run: False,
             "now_utc": lambda: datetime(2026, 10, 4, 6, 0, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments2, hooks) == 1
    flat = [part for command in podman.commands for part in command]
    assert flat[:2] == ["podman", "container"]
    assert ["podman", "stop", "--ignore", "--time", "30",
            "test-collector"] in podman.commands
    outcome = json.loads((run_dir2 / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "sampler_unit_inactive"


def test_watchdog_unpinned_image_fails_closed(tmp_path: Path) -> None:
    """N2: missing start-time image pin fails the watchdog, never skips."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    podman = FakePodman()
    arguments = _watchdog_args(run_dir)
    assert step9.cmd_watchdog(arguments, {"podman_run": podman,
                                           "no_sleep": True}) == 2
    assert list((run_dir / "failures").glob("unpinned_image-*.json"))
    verbs = [command[1] for command in podman.commands]
    assert "update" not in verbs and "stop" not in verbs


def test_watchdog_rejects_container_mismatch(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    _pin_image(run_dir)
    podman = FakePodman()
    arguments = _watchdog_args(run_dir, collector_container="test-other")
    with pytest.raises(step9.Step9Error):
        step9.cmd_watchdog(arguments, {"podman_run": podman})
    assert podman.commands == []


def test_watchdog_stop_failure_is_evidence_failure(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    _pin_image(run_dir)
    podman = FakePodman(stop_fails=True)
    arguments = _watchdog_args(run_dir,
                               deadline="2026-10-03T05:00:00Z")  # already past
    hooks = {"podman_run": podman,
             "now_utc": lambda: datetime(2026, 10, 4, 5, 0, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments, hooks) == 2
    outcome = json.loads((run_dir / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "deadline_reached"
    assert outcome["stop_error"] == "container_still_running"


def _seed_admission(db: FakeDB, run: dict, pid: int, job_base: int = 1000,
                   extra_tail: bool = True) -> None:
    """Boundary-faithful events: open core, suppressed 04:55, reopen tail."""
    core_start = step9._parse_utc(run["core_start"])
    mode = step9.MODES[run.get("mode", "live-day")]
    total = mode["windows"] + (6 if extra_tail else 0)
    core_end = core_start + mode["interval"]
    bounds = db.admission_run(run["run_id"])
    events, roots, profiles = [], [], {}
    reopened = False
    for window in range(total):
        cycle = core_start + timedelta(seconds=300 * window)
        at = cycle + timedelta(seconds=1)
        event_id = window + 1
        job = job_base + window
        due = cycle - timedelta(seconds=60)
        suppressed = core_end - timedelta(minutes=5) <= cycle < core_end
        event = {
            "id": event_id, "invocation_id": f"{event_id:032x}",
            "cycle_at": cycle, "scheduler_at": cycle, "database_at": at,
            "gate_allowed": not suppressed, "gate_handoff_at": None,
            "batch_limit": 1000,
            "capture_start": bounds["capture_start"],
            "capture_end": bounds["capture_end"],
            "visible_due_count": 0, "visible_due_min_at": None,
            "unselected_visible_due_count": 0,
            "unselected_visible_due_min_at": None,
            "unselected_visible_past_deadline_count": 0,
            "unselected_visible_past_deadline_min_at": None,
            "selected_past_deadline_count": 0,
            "selected_player_ids": [], "selected_due_ats": [],
            "selected_profile_version_ids": [],
            "selected_eligibility_states": [],
            "inserted_job_ids": [], "advanced_count": 0,
            "selected_count": 0, "inserted_count": 0}
        if not suppressed:
            if cycle >= core_end and not reopened:
                event["gate_handoff_at"] = core_end
                reopened = True
            event.update({
                "visible_due_count": 1, "visible_due_min_at": due,
                "selected_player_ids": [pid], "selected_due_ats": [due],
                "selected_profile_version_ids": [101],
                "selected_eligibility_states": ["eligible"],
                "inserted_job_ids": [job], "advanced_count": 1,
                "selected_count": 1, "inserted_count": 1})
            key = f"regular:{pid}:{int(cycle.timestamp())}"
            roots.append((pid, key, job, "complete"))
            profiles[event_id] = (0, 1)
        events.append(event)
    db.admission_events_data = events
    db.admission_roots_data = roots
    db.admission_profile_data = profiles


def _sealed_run(tmp_path: Path, name: str, db: FakeDB,
                boundary=(datetime(2026, 10, 5, 5, 0, tzinfo=UTC), 1,
                          True, True, True, 2, 2, 0),
                late_slots: set = frozenset()):
    cohort = _write_cohort(tmp_path / f"{name}-cohort.txt", TAGS)
    receipt_path = tmp_path / f"{name}-receipt.json"
    run_id = name.replace("-", "")
    receipt_path.write_text(json.dumps(_receipt_scope(admission_run_id=run_id)))
    run_dir = tmp_path / name
    arguments = _start_args(run_dir, cohort, run_id=run_id,
                            deployed_receipt=str(receipt_path),
                            max_invocation_gap_seconds=3600)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        run = step9.cmd_start(arguments, db)
    db.resets = [boundary]
    _seed_admission(db, run, pid=1)
    core_start = step9._parse_utc(run["core_start"])
    mode = step9.MODES[run.get("mode", "live-day")]
    samples = run_dir / "samples"
    samples.mkdir()
    for index in range(mode["slots"]):
        expected = step9.slot_expected_utc(core_start, index)
        captured = expected + timedelta(seconds=1)
        if index in late_slots:
            captured = expected + timedelta(seconds=70)
        sample = step9.build_sample(
            run=run, index=index, expected_utc=expected,
            captured_utc=captured,
            mono_elapsed=float(index * 60), wall_delta=60.0, mono_delta=60.0,
            boot_id=run["boot_id"], db_facts=db.minute_snapshot([]),
            db_error=None,
            metrics={"process_id": "p1", "started_at": 1.0,
                     "counters": {"jobs_total{}": index},
                     "digest": str(index)},
            metrics_error=None,
            previous_metrics={"counters": {"jobs": index - 1}} if index else None,
            pressure={}, fs=step9.filesystem_facts("/tmp", "/tmp"),
            watchdog_active=True,
            pgdata={"status": "captured", "failure_code": None,
                    "captured_at": "2026-10-04T05:00:00+00:00",
                    "container": "test-pg", "image": "sha256:pg",
                    "pgdata": "/var/lib/postgresql/data",
                    "source": "podman-exec:test-pg",
                    "pgdata_bytes": 1000, "pg_wal_bytes": 100})
        if (index + 1) % 60 == 0:
            sample["operating"] = db.operating_snapshot([])
        sample["wire"] = {"failures": [], "unknown": [],
                          "conservative_host_wire_bytes": 1000}
        sample["s3"] = {"go": {}, "go_total": 0, "python": {}, "python_total": 0,
                        "total": 0, "error": None}
        sample["s3_attempts_cumulative"] = 21
        step9._exclusive_json(samples / f"minute-{index:04d}.json", sample)
    return run_dir, run


def test_finalize_validate_roundtrip(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "sealed", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    final = json.loads((run_dir / "final.json").read_text())
    assert final["core_windows"] == 288
    assert final["reset"]["safe_handoffs"] == 1
    assert step9.cmd_validate(arguments) == 0


def test_validate_negative_cases(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    # Tampered sample -> digest mismatch (evidence failure, exit 2).
    run_dir, _run = _sealed_run(tmp_path, "tamper", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    victim = run_dir / "samples" / "minute-0010.json"
    victim.write_text(victim.read_text().replace("on_time", "late"),
                      encoding="utf-8")
    assert step9.cmd_validate(arguments) == 2
    # Missing sample is never zero (gate failure, exit 1).
    run_dir, _run = _sealed_run(tmp_path, "missing", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    (run_dir / "samples" / "minute-0010.json").unlink()
    # Manifest lists it, so validate reports a missing listed artifact (exit 2).
    assert step9.cmd_validate(arguments) == 2
    # Cohort changed after start is tamper evidence (exit 2).
    run_dir, _run = _sealed_run(tmp_path, "changed", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    cohort = tmp_path / "changed-cohort.txt"
    cohort.write_text(cohort.read_text() + "#289QJR91\n", encoding="utf-8")
    assert step9.cmd_validate(arguments) == 2


def test_finalize_rejects_wrong_window_count(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "short", db)
    (run_dir / "samples" / "minute-1439.json").unlink()
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 1  # gate: missing window
    assert list((run_dir / "failures").glob("sample_missing-*.json"))


def test_finalize_unproven_reset_is_gate_failure(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(
        tmp_path, "noreset", db,
        boundary=(datetime(2026, 10, 5, 5, 0, tzinfo=UTC), 1,
                  True, False, False, 0, 0, 3))
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 1
    assert step9.cmd_validate(arguments) == 1  # gate: reset_handoff_unproven


# --- Real PostgreSQL tests (migrated disposable schema, no skips) ----------

def _pg_url() -> str:
    """Fail fast without a database: no skips, no silent embedded boot.

    Booting a second cluster per session wasted disk and contended with the
    shared scratch server; CI always provides the service URL.
    """
    url = os.environ.get("CLASHLENS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("set CLASHLENS_TEST_DATABASE_URL for real PostgreSQL tests")
    return url


def _seed_player(connection, tag: str, *, active: bool = True,
                 state: str = "eligible", tier_id: int = 105000036,
                 tier_name: str = "Legend I",
                 velig: str = "eligible", contract: str = "accepted") -> int:
    player = connection.execute(
        "INSERT INTO players (normalized_tag, active, eligibility_state)"
        " VALUES (%s, %s, %s) RETURNING id", (tag, active, state)).fetchone()[0]
    job = connection.execute(
        "INSERT INTO collector_jobs (work_type, scope, player_id, normalized_tag,"
        " capacity_pool, priority, due_at, coalescing_key, status)"
        " VALUES ('initial_collection', 'player', %s, %s, 'normal', 100,"
        " now(), %s, 'complete') RETURNING id",
        (player, tag, f"seed:{player}")).fetchone()[0]
    attempt = connection.execute(
        "INSERT INTO collector_attempts (job_id, status, started_at,"
        " completed_at) VALUES (%s, 'complete', now(), now()) RETURNING id",
        (job,)).fetchone()[0]
    observation = None
    digest = "cd" * 32
    reference = f"seed/{player}/{tag}"
    connection.execute(
        "INSERT INTO archive_instances (instance_id, endpoint, region,"
        " bucket, marker_key, marker_hash, marker_payload_version)"
        " VALUES ('seed-instance', 'seed', 'seed', 'seed', 'seed', %s, 'v1')"
        " ON CONFLICT (instance_id) DO NOTHING", (digest,))
    connection.execute(
        "INSERT INTO archive_catalogue (response_hash, archive_reference,"
        " byte_size, archive_instance_id) VALUES (%s, %s, 2, 'seed-instance')"
        " ON CONFLICT (response_hash, archive_reference) DO NOTHING", (digest, reference))
    observation = connection.execute(
        "INSERT INTO collector_observations (occurrence_key, collection_job_id,"
        " attempt_id, player_id, normalized_tag, endpoint, request_started_at,"
        " response_completed_at, http_status, response_hash, archive_reference,"
        " archive_catalogue_hash, collector_version, key_label, evidence_headers)"
        " VALUES (%s, %s, %s, %s, %s, 'profile', now(), now(), 200,"
        " %s, %s, %s, 'test', 'test', '{}') RETURNING id",
        (f"seed-obs-{player}-{tag}", job, attempt, player, tag,
         digest, reference, digest)).fetchone()[0]
    version = connection.execute(
        "INSERT INTO player_profile_versions (player_id, observation_id,"
        " normalized_tag, endpoint_version, schema_version, parser_version,"
        " observed_at, source_http_status, name, trophies, league_tier_id,"
        " league_tier_name, eligibility_state, profile_json)"
        " VALUES (%s, %s, %s, 'v1', 'v1', 'v1', now(), 200, 'n', 6000,"
        " %s, %s, %s, '{}') RETURNING id",
        (player, observation, tag, tier_id, tier_name, velig)).fetchone()[0]
    connection.execute(
        "INSERT INTO player_profile_effects (profile_version_id, observation_id,"
        " effect_kind, observed_at, source_http_status, endpoint_version,"
        " schema_version, parser_version)"
        " VALUES (%s, %s, 'current_profile', now(), 200, 'v1', 'v1', 'v1')",
        (version, observation))
    connection.execute(
        "UPDATE players SET current_profile_version_id = %s,"
        " current_observed_at = now() WHERE id = %s", (version, player))
    if contract != "accepted":
        connection.execute(
            "UPDATE player_profile_versions SET source_contract_state = %s"
            " WHERE id = %s", (contract, version))
    return player


def _seed_reset(connection, player_ids: list[int],
                boundary: str = "2026-10-05T05:00:00+00:00") -> None:
    sweep = connection.execute(
        "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s) RETURNING id",
        (boundary,)).fetchone()[0]
    for player in player_ids:
        connection.execute(
            "INSERT INTO collector_reset_sweep_members (sweep_id, player_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING", (sweep, player))
        baseline = connection.execute(
            "INSERT INTO collector_reset_baseline_sweeps"
            " (reset_sweep_id, player_id, boundary_at, evidence_kind, state)"
            " VALUES (%s, %s, %s, 'paired_v2', 'complete') RETURNING id",
            (sweep, player, boundary)).fetchone()[0]
        connection.execute(
            "INSERT INTO collector_jobs (work_type, scope, player_id,"
            " normalized_tag, capacity_pool, priority, due_at, coalescing_key,"
            " status, sweep_id, reset_baseline_sweep_id)"
            " VALUES ('reset_baseline', 'player', %s, %s, 'normal', 100,"
            " now(), %s, 'complete', %s, %s)",
            (player, "#SEED", f"reset:{player}:{sweep}", sweep, baseline))
    generation = connection.execute(
        "INSERT INTO boundary_publication_generations (boundary_at, target_at,"
        " generation, sweep_id, ordering_rule_version, freshness_rule_version,"
        " expected_population_count, expected_population_hash)"
        " VALUES (%s, %s, 1, %s, 'v1', 'v1', %s, %s) RETURNING id",
        (boundary, boundary, sweep, len(player_ids), "ab" * 32)).fetchone()[0]
    for player in player_ids:
        connection.execute(
            "INSERT INTO boundary_publication_generation_members"
            " (generation_id, player_id, status)"
            " VALUES (%s, %s, 'terminal') ON CONFLICT DO NOTHING",
            (generation, player))
    connection.execute(
        "INSERT INTO collector_boundary_admission (boundary_at, reset_sweep_id,"
        " regular_drain_complete, reset_drain_complete, safe_handoff,"
        " state, handoff_at) VALUES (%s, %s, true, true, true,"
        " 'safe_handoff', %s)"
        " ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff = true",
        (boundary, sweep, boundary))


def test_population_sql_against_real_schema() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            good = _seed_player(connection, TAGS[0])
            bad_tier = _seed_player(connection, TAGS[1], tier_id=105000035,
                                    tier_name="Legend II", velig="ineligible",
                                    state="ineligible")
            outsider = _seed_player(connection, "#289QJR91")
            connection.commit()
        rows = database.snapshot_population(TAGS[:2])
        assert len(rows) == 2
        buckets = {row[1]: step9._classify_eligible(row) for row in rows}
        assert buckets[TAGS[0]] == "eligible"
        assert buckets[TAGS[1]] == "ineligible_or_inactive"
        assert database.outside_active([good, bad_tier]) == 1
        assert database.outside_active([good, bad_tier, outsider]) == 0


def test_queue_probes_are_active_only() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            players = [_seed_player(connection, TAGS[0])]
            # One active regular_poll root per player (production dedup rule).
            extra_tags = ["#289QJR90", "#28CGVQ2G", "#2000QVLR",
                          "#2829PYLR", "#289QJRC0"]
            for tag in extra_tags:
                players.append(_seed_player(connection, tag))
            for player, status in zip(players + [players[0]],
                                      ("pending", "leased", "waiting_retry",
                                       "waiting_dependency", "complete", "failed")):
                connection.execute(
                    "INSERT INTO collector_jobs (work_type, scope, player_id,"
                    " normalized_tag, capacity_pool, priority, due_at,"
                    " coalescing_key, status) VALUES ('regular_poll', 'player',"
                    " %s, %s, 'normal', 100, now(), %s, %s)",
                    (player, TAGS[0], f"q-{status}-{player}", status))
            player = players[0]
            connection.execute(
                "INSERT INTO python_processing_jobs (observation_id, status,"
                " due_at, work_type, input_json) SELECT id, 'pending', now(),"
                " 'process_observation', '{}' FROM collector_observations"
                " LIMIT 1")
            connection.commit()
        snapshot = database.minute_snapshot([player])
        statuses = {row[0] for row in snapshot["queues"]}
        assert "complete" not in statuses and "failed" not in statuses
        assert {"pending", "leased", "waiting_retry",
                "waiting_dependency"} <= statuses
        assert snapshot["python_queues"][0][0] == "pending"


def test_outside_roots_against_real_schema() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            good = _seed_player(connection, TAGS[0])
            outsider = _seed_player(connection, "#289QJR91", active=False,
                                    state="unknown")
            connection.execute(
                "INSERT INTO collector_jobs (work_type, scope, player_id,"
                " normalized_tag, capacity_pool, priority, due_at,"
                " coalescing_key, status) VALUES ('discovery_profile', 'player',"
                " %s, %s, 'normal', 100, now(), %s, 'pending')",
                (outsider, "#289QJR91", f"outside-{outsider}"))
            connection.commit()
        assert database.outside_roots([good]) == 2  # seed + discovery roots,
        # any status counts since P2-12 (terminal history included)
        assert database.outside_roots([good, outsider]) == 0


def test_reset_identity_against_real_schema() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            first = _seed_player(connection, TAGS[0])
            second = _seed_player(connection, TAGS[1])
            _seed_reset(connection, [first, second])
            connection.commit()
        rows = database.reset_identity("2026-10-04T05:00:00Z",
                                       "2026-10-05T05:00:00Z")
        assert len(rows) == 1
        (_at, _sweep, regular_done, reset_done, handoff,
         members, gen_members, nonterminal) = rows[0]
        assert handoff and regular_done and reset_done
        assert members == 2 and gen_members == 2 and nonterminal == 0


def test_all_statements_execute_on_migrated_schema() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            player = _seed_player(connection, TAGS[0])
            connection.commit()
        assert database.identity()["database_name"]
        assert database.snapshot_population(TAGS[:1])
        assert database.outside_active([player]) == 0
        assert database.outside_roots([player]) == 0
        assert database.minute_snapshot([player])["fixed"]
        assert database.reset_identity("2026-10-04T05:00:00Z",
                                       "2026-10-05T05:00:00Z") == []
        assert database.transitions([player], "2026-10-04T05:00:00Z",
                                    "2026-10-05T05:00:00Z") == []


def _rehearsal_hooks(database, fixed_ids, *, slots: int):
    mono = [0]
    walls = [datetime(2026, 10, 4, 5, 0, tzinfo=UTC) - timedelta(seconds=60)]

    def clock():
        return mono[0]

    def now_utc():
        mono[0] += 60_000_000_000
        walls[0] += timedelta(seconds=60)
        return walls[0]

    return {"db": database, "fixed_ids": fixed_ids,
            "fetch_metrics": lambda url: {"process_id": "p1", "started_at": 1.0,
                                          "counters": {"jobs_total{}": mono[0]},
                                          "digest": str(mono[0])},
            "container_probe": lambda run: {"running": True,
                                              "image": "sha256:test",
                                              "started_at": "t",
                                              "stats": None},
            "watchdog_check": lambda run: True,
            "resource_facts": lambda run, db, metrics: _quiet_facts(),
            "wire_facts": lambda run: {
                "status": "captured", "failure_code": None,
                "boot_id": run.get("boot_id"),
                "interfaces": {"test-eth0": {
                    "present": True, "rx_bytes": 1000, "tx_bytes": 500,
                    "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}},
            "worker_probe": lambda run: [
                {"archive": {"remote_attempts": {"get": 1}}}],
            "pgdata_probe": lambda run: {
                "status": "captured", "failure_code": None,
                "captured_at": "2026-10-04T05:00:00+00:00",
                "container": "test-pg", "image": "sha256:pg",
                "pgdata": "/var/lib/postgresql/data",
                "source": "podman-exec:test-pg",
                "pgdata_bytes": 1000, "pg_wal_bytes": 100},
            "clock": clock,
            "now_utc": now_utc, "no_sleep": True, "max_slots": slots}


def _mirror_live_run(connection, run_id: str, player: int, tag: str,
                    version: int, core_start: datetime,
                    windows: int = 288, tail_windows: int = 6) -> None:
    """Seed one timely gate-open admission event per window plus tail."""
    connection.execute(
        "INSERT INTO collector_regular_admission_evidence_runs"
        " (run_id, capture_start, capture_end, max_events,"
        " max_selected_entries) VALUES (%s, %s, %s, 108000, 5000000)",
        (run_id, core_start, core_start + timedelta(hours=25)))
    total = windows + tail_windows
    jobs = connection.execute(
        "INSERT INTO collector_jobs (work_type, scope, player_id,"
        " normalized_tag, capacity_pool, priority, due_at, coalescing_key,"
        " status, created_at) SELECT 'regular_poll', 'player', %(pid)s,"
        " %(tag)s, 'normal', 100, cycle, 'regular:' || %(pid)s || ':' ||"
        " extract(epoch FROM cycle)::bigint, 'complete', cycle"
        " FROM generate_series(%(start)s::timestamptz,"
        " %(start)s::timestamptz + make_interval(secs => %(total)s * 300),"
        " interval '5 minutes') AS cycle RETURNING id",
        {"pid": player, "tag": tag, "start": core_start,
         "total": total - 1}).fetchall()
    assert len(jobs) == total
    cap_start, cap_end = core_start, core_start + timedelta(hours=25)
    for window in range(total):
        cycle = core_start + timedelta(seconds=300 * window)
        at = cycle + timedelta(seconds=1)
        due = cycle - timedelta(seconds=60)
        connection.execute(
            "INSERT INTO collector_regular_admission_evidence"
            " (run_id, invocation_id, cycle_at, scheduler_at, database_at,"
            " capture_start, capture_end,"
            " gate_allowed, batch_limit, visible_due_count, visible_due_min_at,"
            " unselected_visible_due_count,"
            " unselected_visible_past_deadline_count,"
            " selected_past_deadline_count,"
            " selected_player_ids,"
            " selected_due_ats, selected_profile_version_ids,"
            " selected_eligibility_states, inserted_job_ids, advanced_count)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, true, 1000, 1, %s, 0, 0, 0,"
            " %s, %s, %s, %s, %s, 1)",
            (run_id, f"{window + 1:032x}", cycle, cycle, at,
             cap_start, cap_end, due,
             [player], [due], [version], ["eligible"], [jobs[window][0]]))


def test_no_official_traffic_rehearsal() -> None:
    """Preflight end-to-end on a disposable migrated DB; no official calls.

    Full 75-slot loop (60 bootstrap + 15 drain) through the real Database:
    start, sample, finalize, validate. Live-day 1440 counts stay covered by
    sealed runs; per-statement live-day SQL stays covered by PG probe tests.
    """
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        cohort = Path(tempfile.mkdtemp(prefix="step9-cohort-")) / "cohort.txt"
        _write_cohort(cohort, TAGS)
        run_dir = cohort.parent / "run"
        arguments = _start_args(run_dir, cohort, database_url=info,
                                run_id="rehearsal1", mode="preflight",
                                core_start="2026-10-04T05:00:00Z",
                                core_end="2026-10-04T06:15:00Z")
        with psycopg.connect(info) as connection:
            player = _seed_player(connection, TAGS[0])
            connection.execute(
                "INSERT INTO collector_jobs (work_type, scope, player_id,"
                " normalized_tag, capacity_pool, priority, due_at,"
                " coalescing_key, status, required_endpoint, created_at)"
                " VALUES ('discovery_profile', 'player', %s, %s, 'normal',"
                " 100, now(), %s, 'complete', 'profile',"
                " '2026-10-04T05:30:00+00:00')",
                (player, TAGS[0], f"rehearsal-{player}"))
            connection.execute(
                "INSERT INTO global_rankings_intents (cycle_at)"
                " VALUES ('2026-10-04T05:00:00+00:00')")
            connection.commit()
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None):
            receipt_path = cohort.parent / "receipt.json"
            receipt_path.write_text(json.dumps(_receipt_scope()))
            arguments.deployed_receipt = str(receipt_path)
            header = step9.cmd_start(arguments, database)
        assert header["initial"]["eligible_count"] == 1
        assert header["mode"] == "preflight"
        _pin_resources(run_dir)
        sample_args = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
        assert step9.cmd_sample(sample_args,
                                _rehearsal_hooks(database, [player],
                                                 slots=75)) == 0
        assert step9.cmd_finalize(sample_args, {"db": database}) == 0
        assert step9.cmd_validate(sample_args) == 0
        # No tags, raw bodies, or official hosts leak into retained artifacts.
        retained = "".join(
            path.read_text(encoding="utf-8")
            for path in run_dir.rglob("*.json"))
        for tag in TAGS:
            assert tag not in retained
        assert "api.clashofclans.com" not in retained
        assert "supercell" not in retained.lower()


def test_rehearsal_kill_and_deadline_paths() -> None:
    """A killed sampler leaves durable partials; a deadline stops traffic."""
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            _mirror_0022(connection)
            player = _seed_player(connection, TAGS[0])
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence_runs"
                " (run_id, capture_start, capture_end, max_events,"
                " max_selected_entries) VALUES ('killed1',"
                " '2026-10-04T05:00:00+00:00', '2026-10-05T06:00:00+00:00',"
                " 108000, 5000000)")
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence_runs"
                " (run_id, capture_start, capture_end, max_events,"
                " max_selected_entries) VALUES ('deadline1',"
                " '2026-10-04T05:00:00+00:00', '2026-10-05T06:00:00+00:00',"
                " 108000, 5000000)")
            connection.commit()
        base = Path(tempfile.mkdtemp(prefix="step9-kill-"))
        # Killed sampler: 3 slots then stop; finalize must refuse a partial run.
        cohort = _write_cohort(base / "cohort.txt", TAGS)
        kill_receipt = base / "receipt.json"
        kill_receipt.write_text(
            json.dumps(_receipt_scope(admission_run_id="killed1")))
        deadline_receipt = base / "deadline-receipt.json"
        deadline_receipt.write_text(
            json.dumps(_receipt_scope(admission_run_id="deadline1")))
        run_dir = base / "run-killed"
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None):
            step9.cmd_start(_start_args(run_dir, cohort, run_id="killed1",
                                        deployed_receipt=str(kill_receipt)),
                            database)
        _pin_resources(run_dir)
        sample_args = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
        assert step9.cmd_sample(sample_args,
                                _rehearsal_hooks(database, [player],
                                                 slots=3)) == 0
        assert len(list((run_dir / "samples").glob("*.json"))) == 3
        assert step9.cmd_finalize(sample_args, {"db": database}) == 1
        # Expired deadline on a fresh run: exact stop command, outcome kept.
        run_dir2 = base / "run-deadline"
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None):
            step9.cmd_start(_start_args(run_dir2, cohort, run_id="deadline1",
                                        deployed_receipt=str(deadline_receipt)),
                            database)
        _pin_image(run_dir2)
        podman = FakePodman()
        watchdog_args = _watchdog_args(run_dir2,
                                       deadline="2026-10-03T05:00:00Z")
        # injected clock: past the fixture deadline (real now predates it)
        assert step9.cmd_watchdog(watchdog_args, {
            "podman_run": podman, "single_pass": True, "no_sleep": True,
            "now_utc": lambda: datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
        }) == 1
        assert ["podman", "stop", "--ignore", "--time", "30",
                "test-collector"] in podman.commands


def _admission_run_header(**overrides):
    header = {"run_id": "t", "state": "active", "failure_code": None,
              "capture_start": datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
              "capture_end": datetime(2026, 10, 5, 6, 0, tzinfo=UTC)}
    header.update(overrides)
    return header


def _admission_event(event_id: int, cycle: datetime, pid: int = 1, job: int = 1000,
                     timely: bool = True, gate: bool = True, **overrides):
    at = cycle + timedelta(seconds=1)
    due = cycle - timedelta(seconds=60)
    if not timely:
        due = cycle - timedelta(minutes=10)
    event = {"id": event_id, "cycle_at": cycle, "database_at": at,
             "capture_start": datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
             "capture_end": datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
             "gate_allowed": gate, "selected_count": 1, "inserted_count": 1,
             "advanced_count": 1, "selected_past_deadline_count": 0,
             "unselected_visible_past_deadline_count": 0,
             "selected_player_ids": [pid], "selected_due_ats": [due],
             "inserted_job_ids": [job]}
    event.update(overrides)
    return event


def _admission_run_dict(**overrides):
    run = {"core_start": "2026-10-04T05:00:00+00:00",
           "core_end": "2026-10-05T05:00:00+00:00", "mode": "live-day",
           "max_invocation_gap_seconds": 3600}
    run.update(overrides)
    return run


def test_evaluate_admission_failures_and_unknown() -> None:
    base = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    run = _admission_run_dict()
    key = f"regular:1:{int(base.timestamp())}"
    good = _admission_event(1, base)
    roots = [(1, key, 1000, "complete")]
    profiles = {1: (0, 1)}
    result = step9.evaluate_admission(
        run=run, header=_admission_run_header(), events=[good],
        profile_counts=profiles, roots=roots, max_gap_seconds=3600)
    assert result["failures"] == []
    assert result["unknown"] == ["admission_tail_insufficient"]
    # full tail coverage passes
    tail = _admission_event(2, base + timedelta(hours=24, minutes=6), job=1001)
    tail_key = f"regular:1:{int((base + timedelta(hours=24, minutes=6)).timestamp())}"
    result = step9.evaluate_admission(
        run=run, header=_admission_run_header(), events=[good, tail],
        profile_counts={1: (0, 1), 2: (0, 1)},
        roots=roots + [(1, tail_key, 1001, "complete")], max_gap_seconds=90000)
    assert result["failures"] == [] and result["unknown"] == []
    # late selected recompute + stored past-deadline counts fail the window
    late = _admission_event(3, base, timely=False, job=1002,
                            selected_past_deadline_count=1,
                            unselected_visible_past_deadline_count=2)
    result = step9.evaluate_admission(
        run=run, header=_admission_run_header(), events=[late],
        profile_counts={3: (0, 1)}, roots=[], max_gap_seconds=3600)
    assert "admission_selected_late_recomputed" in result["failures"]
    assert "admission_past_deadline_selected" in result["failures"]
    assert "admission_past_deadline_unselected" in result["failures"]
    # count mismatch, invalid profile, out-of-range event, gap
    bad = _admission_event(4, base + timedelta(seconds=10), job=1003)
    bad["inserted_count"] = 0
    result = step9.evaluate_admission(
        run=run, header=_admission_run_header(), events=[good, bad],
        profile_counts={1: (0, 1), 4: (2, 1)}, roots=roots, max_gap_seconds=3600)
    assert "admission_count_mismatch" in result["failures"]
    assert "invalid_selected_profile" in result["failures"]
    far = _admission_event(5, base, job=1004)
    far["database_at"] = datetime(2026, 10, 6, 5, 0, tzinfo=UTC)
    result = step9.evaluate_admission(
        run=run, header=_admission_run_header(), events=[far],
        profile_counts={5: (0, 1)}, roots=[], max_gap_seconds=5)
    assert "admission_event_out_of_range" in result["failures"]
    header = _admission_run_header(state="capacity_exceeded",
                                   failure_code="admission_evidence_capacity_exceeded")
    result = step9.evaluate_admission(
        run=run, header=header, events=[], profile_counts={}, roots=[],
        max_gap_seconds=5)
    assert "admission_evidence_capacity_exceeded" in result["failures"]
    header = _admission_run_header(state="bogus")
    result = step9.evaluate_admission(
        run=run, header=header, events=[], profile_counts={}, roots=[],
        max_gap_seconds=5)
    assert "admission_run_state_invalid" in result["failures"]


def test_reconcile_semantic_roots_cases() -> None:
    run = _admission_run_dict()
    base = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    event = _admission_event(1, base)
    key = f"regular:1:{int(base.timestamp())}"
    ok = step9.reconcile_semantic_roots(run, [event], [(1, key, 1000, "complete")])
    assert ok["failures"] == []
    dup = step9.reconcile_semantic_roots(
        run, [event], [(1, key, 1000, "complete"), (1, key, 1001, "complete")])
    assert "admission_root_count" in dup["failures"]
    missing = step9.reconcile_semantic_roots(run, [event], [])
    assert "admission_root_count" in missing["failures"]
    foreign = step9.reconcile_semantic_roots(
        run, [event], [(1, key, 1000, "complete"), (9, "regular:9:1", 1002, "x")])
    assert "admission_unexplained_regular_root" in foreign["failures"]
    malformed = step9.reconcile_semantic_roots(run, [], [(1, "bogus", 1000, "x")])
    assert "admission_malformed_coalescing_key" in malformed["failures"]
    wrong_player = step9.reconcile_semantic_roots(
        run, [], [(1, "regular:2:123", 1000, "x")])
    assert "admission_malformed_coalescing_key" in wrong_player["failures"]
    wrong_job = step9.reconcile_semantic_roots(
        run, [event], [(1, key, 9999, "complete")])
    assert "admission_root_identity" in wrong_job["failures"]


def test_start_preflight_mode(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    arguments = _start_args(tmp_path / "pf", cohort,
                            deployed_receipt=str(receipt_path),
                            mode="preflight",
                            core_start="2026-10-04T05:00:00Z",
                            core_end="2026-10-04T06:15:00Z",
                            run_id="preflight1")
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        header = step9.cmd_start(arguments, db)
    assert header["mode"] == "preflight"
    assert header["schema"] == step9.SCHEMA_PREFLIGHT
    assert header["admission"] == {"schema": None, "status": "not_applicable"}
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error):  # 24h rejected in preflight
            step9.cmd_start(_args_with_receipt(tmp_path, "pf2", cohort,
                                              mode="preflight",
                                              core_end="2026-10-05T06:00:00Z"), db)
        with pytest.raises(step9.Step9Error):  # 1h rejected in live-day
            step9.cmd_start(_args_with_receipt(tmp_path, "lv2", cohort,
                                              core_start="2026-10-04T05:00:00Z",
                                              core_end="2026-10-04T06:15:00Z"), db)


def test_start_fails_closed_without_admission_tables(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])], admission_present=False)
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_args_with_receipt(tmp_path, "noadm", cohort), db)
        assert error.value.code == "admission_schema_absent"


def test_preflight_envelope_cases() -> None:
    ok = step9.evaluate_preflight_envelope(
        workcounts=[("discovery_profile", "complete", 100)],
        observations=[("profile", 100)],
        intents=(1, "2026-10-04T05:00:00+00:00", "2026-10-04T05:00:00+00:00"))
    assert ok["failures"] == []
    bad = step9.evaluate_preflight_envelope(
        workcounts=[("regular_poll", "pending", 2),
                    ("discovery_profile", "complete", 13501)],
        observations=[("profile", 13501), ("battle_log", 3)],
        intents=(2, None, None))
    assert "unexpected_scheduler_traffic" in bad["failures"]
    assert "preflight_profile_budget_exceeded" in bad["failures"]
    assert "preflight_unexpected_battle_traffic" in bad["failures"]
    assert "preflight_rankings_budget_exceeded" in bad["failures"]


def _sealed_preflight(tmp_path: Path, name: str, db: FakeDB, bad_traffic: bool = False):
    cohort = _write_cohort(tmp_path / f"{name}-cohort.txt", TAGS)
    receipt_path = tmp_path / f"{name}-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    run_dir = tmp_path / name
    arguments = _start_args(run_dir, cohort, run_id=name.replace("-", ""),
                            deployed_receipt=str(receipt_path),
                            mode="preflight",
                            core_start="2026-10-04T05:00:00Z",
                            core_end="2026-10-04T06:15:00Z")
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        run = step9.cmd_start(arguments, db)
    db.preflight_data = {
        "workcounts": ([("regular_poll", "pending", 1)] if bad_traffic
                        else [("discovery_profile", "complete", 4)]),
        "observations": [("profile", 4 if not bad_traffic else 1)],
        "intents": (1, datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
                    datetime(2026, 10, 4, 5, 0, tzinfo=UTC)),
        "pending_remote": 0}
    core_start = step9._parse_utc(run["core_start"])
    samples = run_dir / "samples"
    samples.mkdir()
    for index in range(step9.MODES["preflight"]["slots"]):
        expected = step9.slot_expected_utc(core_start, index)
        sample = step9.build_sample(
            run=run, index=index, expected_utc=expected,
            captured_utc=expected + timedelta(seconds=1),
            mono_elapsed=float(index * 60), wall_delta=60.0, mono_delta=60.0,
            boot_id=run["boot_id"], db_facts=db.minute_snapshot([]),
            db_error=None,
            metrics={"process_id": "p1", "started_at": 1.0,
                     "counters": {"jobs_total{}": index},
                     "digest": str(index)},
            metrics_error=None, previous_metrics=None,
            pressure={}, fs=step9.filesystem_facts("/tmp", "/tmp"),
            watchdog_active=True,
            pgdata={"status": "captured", "failure_code": None,
                    "captured_at": "2026-10-04T05:00:00+00:00",
                    "container": "test-pg", "image": "sha256:pg",
                    "pgdata": "/var/lib/postgresql/data",
                    "source": "podman-exec:test-pg",
                    "pgdata_bytes": 1000, "pg_wal_bytes": 100})
        if (index + 1) % 60 == 0:
            sample["operating"] = db.operating_snapshot([])
        sample["wire"] = {"failures": [], "unknown": [],
                          "conservative_host_wire_bytes": 1000}
        sample["s3"] = {"go": {}, "go_total": 0, "python": {}, "python_total": 0,
                        "total": 0, "error": None}
        sample["s3_attempts_cumulative"] = 21
        step9._exclusive_json(samples / f"minute-{index:04d}.json", sample)
    return run_dir, run


def test_preflight_roundtrip(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_preflight(tmp_path, "pfsealed", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    final = json.loads((run_dir / "final.json").read_text())
    assert final["core_windows"] == 15
    assert final["preflight"]["status"] == "complete"
    assert final["admission"]["status"] == "not_applicable"
    assert step9.cmd_validate(arguments) == 0
    # scheduler traffic during preflight fails the gate (fresh run dir)
    db2 = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir2, _run2 = _sealed_preflight(tmp_path, "pfbad", db2,
                                        bad_traffic=True)
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    assert step9.cmd_finalize(arguments2, {"db": db2}) == 1
    assert step9.cmd_validate(arguments2) == 1

def _mirror_0022(connection) -> None:
    """Require the real migration 0022 (merged); the frozen mirror is deleted."""
    row = connection.execute(
        "SELECT 1 FROM clash_lens_schema_migrations WHERE version = 22"
    ).fetchone()
    assert row is not None, "migration 0022 must be applied"


def test_admission_tables_present_via_migration() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        assert database.admission_present() is True
        assert database.admission_run("nope") is None
        assert database.admission_latest("nope") is None


def test_admission_queries_against_mirrored_schema() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info, autocommit=True) as connection:
            _mirror_0022(connection)
            assert database.admission_present() is True
            good = _seed_player(connection, TAGS[0])
            bad = _seed_player(connection, TAGS[1], tier_id=105000035,
                               tier_name="Legend II", velig="ineligible",
                               state="ineligible")
            version = connection.execute(
                "SELECT current_profile_version_id FROM players WHERE id = %s",
                (good,)).fetchone()[0]
            bad_version = connection.execute(
                "SELECT current_profile_version_id FROM players WHERE id = %s",
                (bad,)).fetchone()[0]
            cycle = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence_runs"
                " (run_id, capture_start, capture_end, max_events,"
                " max_selected_entries)"
                " VALUES ('run1', %s, %s, 108000, 5000000)",
                (cycle, cycle + timedelta(hours=30)))
            key = f"regular:{good}:{int(cycle.timestamp())}"
            job = connection.execute(
                "INSERT INTO collector_jobs (work_type, scope, player_id,"
                " normalized_tag, capacity_pool, priority, due_at,"
                " coalescing_key, status, created_at)"
                " VALUES ('regular_poll', 'player',"
                " %s, %s, 'normal', 100, %s, %s, 'complete',"
                " '2026-10-04T05:00:01+00:00') RETURNING id",
                (good, TAGS[0], cycle, key)).fetchone()[0]
            due = cycle - timedelta(seconds=60)
            at = cycle + timedelta(seconds=1)
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence"
                " (run_id, invocation_id, cycle_at, scheduler_at, database_at,"
                " capture_start, capture_end,"
                " gate_allowed, batch_limit, visible_due_count,"
                " visible_due_min_at, unselected_visible_due_count,"
                " unselected_visible_past_deadline_count,"
                " selected_past_deadline_count,"
                " selected_player_ids, selected_due_ats,"
                " selected_profile_version_ids, selected_eligibility_states,"
                " inserted_job_ids, advanced_count)"
                " VALUES ('run1', %s, %s, %s, %s, %s, %s,"
                " true, 1000, 1, %s, 0, 0, 0,"
                " %s, %s, %s, %s, %s, 1)",
                ("ab" * 16, cycle, cycle, at, cycle,
                 cycle + timedelta(hours=30), due, [good], [due],
                 [version], ["eligible"], [job]))
            # gate-blocked empty event: no rows selected, nothing invalid
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence"
                " (run_id, invocation_id, cycle_at, scheduler_at, database_at,"
                " capture_start, capture_end,"
                " gate_allowed, batch_limit, visible_due_count,"
                " visible_due_min_at, unselected_visible_due_count,"
                " unselected_visible_due_min_at,"
                " unselected_visible_past_deadline_count,"
                " selected_past_deadline_count,"
                " selected_player_ids, selected_due_ats,"
                " selected_profile_version_ids, selected_eligibility_states,"
                " inserted_job_ids, advanced_count)"
                " VALUES ('run1', %s, %s, %s, %s, %s, %s,"
                " false, 1000, 3, %s, 3, %s, 0, 0,"
                " '{}', '{}', '{}', '{}', '{}', 0)",
                ("cd" * 16, cycle, cycle + timedelta(seconds=1),
                 at + timedelta(seconds=1), cycle,
                 cycle + timedelta(hours=30), due, due))
            # event selecting the ineligible player: retained, fails validation
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence"
                " (run_id, invocation_id, cycle_at, scheduler_at, database_at,"
                " capture_start, capture_end,"
                " gate_allowed, batch_limit, visible_due_count,"
                " visible_due_min_at, unselected_visible_due_count,"
                " unselected_visible_past_deadline_count,"
                " selected_past_deadline_count,"
                " selected_player_ids, selected_due_ats,"
                " selected_profile_version_ids, selected_eligibility_states,"
                " inserted_job_ids, advanced_count)"
                " VALUES ('run1', %s, %s, %s, %s, %s, %s,"
                " true, 1000, 1, %s, 0, 0, 0,"
                " %s, %s, %s, %s, '{}', 1)",
                ("ef" * 16, cycle, cycle + timedelta(seconds=2),
                 at + timedelta(seconds=2),
                 cycle, cycle + timedelta(hours=30),
                 due, [bad], [due], [bad_version], ["ineligible"]))
        header = database.admission_run("run1")
        assert header["state"] == "active"
        events = database.admission_events(
            "run1", "2026-10-04T05:00:00Z", "2026-10-05T05:00:00Z")
        assert len(events) == 3
        assert events[0]["selected_count"] == 1
        assert events[0]["inserted_count"] == 1
        assert events[1]["selected_count"] == 0  # gate-blocked empty event
        counts = database.admission_profile_counts(
            "run1", "2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z")
        assert counts[events[0]["id"]] == (0, 1)
        assert events[1]["id"] not in counts  # empty event has no rows
        bad_id = next(row["id"] for row in database.admission_events(
            "run1", "2026-10-04T00:00:00Z", "2026-10-05T00:00:00Z")
            if row["selected_player_ids"] == [bad])
        assert counts[bad_id][0] == 1
        latest = database.admission_latest("run1")
        assert latest is not None and latest["selected_count"] == 1
        roots = database.semantic_roots(
            "2026-10-04T05:00:00Z", "2026-10-05T06:00:00Z")
        assert (good, key, job, "complete") in roots


def test_preflight_probes_against_real_schema() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            player = _seed_player(connection, TAGS[0])
            connection.execute(
                "INSERT INTO collector_jobs (work_type, scope, player_id,"
                " normalized_tag, capacity_pool, priority, due_at,"
                " coalescing_key, status, required_endpoint, created_at)"
                " VALUES ('discovery_profile', 'player', %s, %s, 'normal',"
                " 100, now(), %s, 'complete', 'profile',"
                " '2026-10-04T05:30:00+00:00')",
                (player, TAGS[0], f"preflight-{player}"))
            connection.execute(
                "INSERT INTO global_rankings_intents (cycle_at)"
                " VALUES ('2026-10-04T05:00:00+00:00')")
            connection.commit()
        probes = database.preflight_probes(
            "2026-10-04T04:00:00Z", "2026-10-04T07:00:00Z")
        work = {(w, s): c for w, s, c in probes["workcounts"]}
        assert work[("discovery_profile", "complete")] == 1
        assert ("regular_poll", "pending") not in work
        endpoints = dict(probes["observations"])
        assert endpoints == {}
        assert probes["intents"][0] == 1
        assert probes["pending_remote"] == 0
        result = step9.evaluate_preflight_envelope(
            workcounts=probes["workcounts"],
            observations=probes["observations"], intents=probes["intents"])
        assert result["failures"] == []


def test_observer_role_least_privilege() -> None:
    """Worker-role read set for the observer (0022 grants merged).

    Least-privilege existing role: clashlens_python_worker (never the public
    API role, never superuser). Migration 0022 carries the evidence-table
    and rankings cycle_at SELECT grants; this test proves the full read set.
    """
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        with psycopg.connect(info, autocommit=True) as connection:
            _mirror_0022(connection)
            player = _seed_player(connection, TAGS[0])
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence_runs"
                " (run_id, capture_start, capture_end, max_events,"
                " max_selected_entries) VALUES ('role1',"
                " '2026-10-04T05:00:00+00:00', '2026-10-05T06:00:00+00:00',"
                " 108000, 5000000)")
            connection.execute("SET ROLE clashlens_python_worker")
            readable = [
                ("population", step9.SQL_POPULATION_MAP, (TAGS,)),
                ("outside", step9.SQL_OUTSIDE_ACTIVE, ([player],)),
                ("fixed", step9.SQL_FIXED_IDS, ([player],)),
                ("queues", step9.SQL_ACTIVE_QUEUES, ()),
                ("identity", step9.SQL_DATABASE_IDENTITY, ()),
                ("reset", step9.SQL_RESET_IDENTITY,
                 ("2026-10-04T05:00:00Z", "2026-10-05T05:00:00Z")),
                ("run", step9.SQL_ADMISSION_RUN, ("role1",)),
                ("events", step9.SQL_ADMISSION_EVENTS,
                 ("role1", "2026-10-04T05:00:00Z", "2026-10-05T05:00:00Z")),
                ("latest", step9.SQL_ADMISSION_LATEST, ("role1",)),
                ("roots", step9.SQL_SEMANTIC_ROOTS,
                 ("2026-10-04T05:00:00Z", "2026-10-05T06:00:00Z")),
                ("preflight_work", step9.SQL_PREFLIGHT_WORKCOUNTS,
                 ("2026-10-04T04:00:00Z", "2026-10-04T07:00:00Z")),
                ("preflight_obs", step9.SQL_PREFLIGHT_OBSERVATIONS,
                 ("2026-10-04T04:00:00Z", "2026-10-04T07:00:00Z")),
                ("pending_remote", step9.SQL_PREFLIGHT_PENDING_REMOTE, ()),
                ("transport",
                 "SELECT count(*) FROM collector_transport_failures", ()),
                ("processed_versions",
                 "SELECT count(*) FROM processed_observation_versions", ()),
                ("source_parses",
                 "SELECT count(*) FROM source_response_parses", ()),
                ("catalogue", "SELECT count(*) FROM archive_catalogue", ()),
            ]
            for name, sql, params in readable:
                connection.execute(sql, params if params else None)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "INSERT INTO archive_catalogue (response_hash,"
                    " archive_reference, byte_size, archive_instance_id)"
                    " VALUES (%s, 'x', 1, 'i1')", ("ab" * 32,))
            connection.execute("RESET ROLE")


def test_script_main_path_binds_all_definitions() -> None:
    """P1-2: everything must bind when executed as a script, not just import."""
    import subprocess

    path = ROOT / "scripts" / "step9_check.py"
    completed = subprocess.run(
        [sys.executable, str(path), "--help"],
        check=False, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0
    probe = ("import ast\n"
               "tree = ast.parse(open(r'" + str(path) + "').read())\n"
               "seen_guard = False\n"
               "late = []\n"
               "for node in tree.body:\n"
               "    if isinstance(node, ast.If):\n"
               "        src = ast.dump(node.test)\n"
               "        if \"__name__\" in src and \"__main__\" in src:\n"
               "            seen_guard = True\n"
               "            continue\n"
               "    if seen_guard and isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom)):\n"
               "        late.append(getattr(node, 'name', type(node).__name__))\n"
               "assert not late, f'definitions after CLI guard: {late}'\n")
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr


def test_admission_present_reraises_non_missing_errors() -> None:
    assert step9.Database._missing_table(
        type("E", (Exception,), {"sqlstate": "42P01"})()) is True
    assert step9.Database._missing_table(RuntimeError("x")) is False

    class RefusingDB:
        def __call__(self):
            raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError):
        step9.Database(RefusingDB()).admission_present()


def test_shifted_sampler_fails_lateness_gate(tmp_path: Path) -> None:
    """P1-8: a sampler started late cannot certify shifted boundaries."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    mono = [0]
    walls = [datetime(2026, 10, 4, 5, 0, tzinfo=UTC) - timedelta(seconds=60)
             + timedelta(seconds=70)]

    def clock():
        return mono[0]

    def now_utc():
        mono[0] += 60_000_000_000
        walls[0] += timedelta(seconds=60)
        return walls[0]

    hooks = {"db": db, "fixed_ids": db.fixed_ids,
             "fetch_metrics": lambda url: {"process_id": "p1", "started_at": 1.0,
                                           "counters": {}, "digest": "x"},
             "watchdog_check": lambda run: True,
            "resource_facts": lambda run, db, metrics: _quiet_facts(),
            "wire_facts": lambda run: {
                "status": "captured", "failure_code": None,
                "boot_id": run.get("boot_id"),
                "interfaces": {"test-eth0": {
                    "present": True, "rx_bytes": 1000, "tx_bytes": 500,
                    "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}},
            "worker_probe": lambda run: [
                {"archive": {"remote_attempts": {"get": 1}}}],
            "pgdata_probe": lambda run: {
                "status": "captured", "failure_code": None,
                "captured_at": "2026-10-04T05:00:00+00:00",
                "container": "test-pg", "image": "sha256:pg",
                "pgdata": "/var/lib/postgresql/data",
                "source": "podman-exec:test-pg",
                "pgdata_bytes": 1000, "pg_wal_bytes": 100},
            "clock": clock,
             "now_utc": now_utc, "no_sleep": True, "max_slots": 5,
             "single_pass": False}
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob("sample_late-*.json"))
    # a sealed run with one shifted slot fails validation on outcomes
    db2 = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir2, _run2 = _sealed_run(tmp_path, "shifted", db2, late_slots={5})
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    assert step9.cmd_finalize(arguments2, {"db": db2}) == 0
    assert step9.cmd_validate(arguments2) == 1


def test_admission_handoff_semantics() -> None:
    base = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    handoff = base + timedelta(hours=24)
    run = _admission_run_dict()
    header = _admission_run_header()
    def _key(pid, at):
        return f"regular:{pid}:{int(at.timestamp())}"
    key = _key
    # suppressed pre-handoff event passes
    blocked = _admission_event(1, base, pid=0, job=0)
    blocked.update({"gate_allowed": False, "selected_count": 0,
                    "inserted_count": 0, "advanced_count": 0,
                    "selected_player_ids": [], "selected_due_ats": [],
                    "inserted_job_ids": [], "database_at": handoff - timedelta(seconds=10)})
    # reopening event at handoff with an old due passes via grace
    reopen_cycle = handoff + timedelta(seconds=1)
    reopen = _admission_event(2, reopen_cycle, pid=1, job=1000)
    reopen.update({"gate_handoff_at": handoff,
                   "database_at": handoff + timedelta(seconds=2),
                   "selected_due_ats": [handoff - timedelta(hours=2)]})
    reopen_key = key(1, reopen_cycle)
    # normal gate-open core event long before suppression: never a breach
    early_open = _admission_event(0, base, pid=1, job=999)
    early_key = key(1, base)
    result = step9.evaluate_admission(
        run=run, header=header, events=[early_open, blocked, reopen],
        profile_counts={0: (0, 1), 1: (0, 0), 2: (0, 1)},
        roots=[(1, early_key, 999, "complete"),
               (1, reopen_key, 1000, "complete")], max_gap_seconds=90000)
    assert result["failures"] == []
    # head gap: first event far after capture_start is unknown, not a pass
    result = step9.evaluate_admission(
        run=run, header=header, events=[reopen],
        profile_counts={2: (0, 1)},
        roots=[(1, reopen_key, 1000, "complete")], max_gap_seconds=5)
    assert "admission_head_gap" in result["unknown"]
    result = step9.evaluate_admission(
        run=run, header=header, events=[blocked, reopen],
        profile_counts={1: (0, 0), 2: (0, 1)},
        roots=[(1, reopen_key, 1000, "complete")], max_gap_seconds=90000)
    assert result["failures"] == []
    # suppressed interval admitting work breaches
    breach = dict(blocked)
    breach.update({"gate_allowed": True, "id": 3,
                   "selected_count": 1, "inserted_count": 1})
    result = step9.evaluate_admission(
        run=run, header=header, events=[breach, reopen],
        profile_counts={3: (0, 1), 2: (0, 1)},
        roots=[(1, reopen_key, 1000, "complete")], max_gap_seconds=90000)
    assert "admission_suppression_breach" in result["failures"]
    # reopening before the handoff timestamp fails
    early = dict(reopen)
    early.update({"id": 4, "database_at": handoff - timedelta(seconds=1)})
    result = step9.evaluate_admission(
        run=run, header=header, events=[blocked, early],
        profile_counts={1: (0, 0), 4: (0, 1)},
        roots=[(1, reopen_key, 1000, "complete")], max_gap_seconds=90000)
    assert "admission_early_reopen" in result["failures"]


def test_migrated_0023_budget_reads() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        assert database.budgets_present() is True  # migrated 0023
        with psycopg.connect(info, autocommit=True) as connection:
            connection.execute("SET ROLE clashlens_python_worker")
            connection.execute(step9.SQL_ENDPOINT_BUDGETS, ("none",))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "INSERT INTO collector_endpoint_budgets"
                    " (run_id, endpoint, cap, consumed, deadline_at)"
                    " VALUES ('none', 'profile', 1, 0, now())")
            connection.execute("RESET ROLE")
        with psycopg.connect(info, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO population_bootstrap_runs"
                " (run_id, manifest_sha256, manifest_count,"
                " normalized_set_sha256, status, batch_size, completed_at)"
                " VALUES ('boot1', %s, 12857, %s, 'complete', 500, now())",
                ("ab" * 32, "cd" * 32))
            for endpoint, cap, consumed in (
                    ("profile", 13500, 120), ("global_player_rankings", 1, 1),
                    ("battle_log", 0, 0)):
                connection.execute(
                    "INSERT INTO collector_endpoint_budgets"
                    " (run_id, endpoint, cap, consumed, deadline_at)"
                    " VALUES ('boot1', %s, %s, %s, now() + interval '1 hour')",
                    (endpoint, cap, consumed))
        data = database.bootstrap_budgets("boot1")
        assert data["run"]["status"] == "complete"
        assert {b["endpoint"]: b["consumed"] for b in data["budgets"]} == {
            "profile": 120, "global_player_rankings": 1, "battle_log": 0}
        assert database.bootstrap_budgets("missing") == {
            "run": None, "budgets": []}


def test_finalize_budget_manifest_match() -> None:
    run = {"bootstrap_run_id": "boot1",
           "cohort": {"raw_sha256": "ab" * 32, "input_count": 12857,
                        "canonical_sha256": "cd" * 32}}

    class BudgetDB(FakeDB):
        budgets_tables = True

        def budgets_present(self):
            return True

        def bootstrap_budgets(self, run_id):
            return {"run": {"manifest_sha256": "ab" * 32,
                              "manifest_count": 12857,
                              "normalized_set_sha256": "cd" * 32,
                              "status": "complete"},
                    "budgets": [{"endpoint": "profile", "cap": 13500,
                                   "consumed": 120}]}

    result = step9._finalize_budget(run, BudgetDB(), {"profile": 100})
    assert result["status"] == "complete" and result["failure"] is None
    result = step9._finalize_budget(run, BudgetDB(), {"profile": 121})
    assert result["failure"] == "budget_evidence_mismatch"
    result = step9._finalize_budget({"bootstrap_run_id": None},
                                    BudgetDB(), {})
    assert result["status"] == "unknown_pending_0023"
    result = step9._finalize_budget(run, FakeDB(), {})
    assert result["status"] == "unknown_pending_0023"

    class DeniedDB(FakeDB):
        def budgets_present(self):
            return True

        def bootstrap_budgets(self, run_id):
            raise type("E", (Exception,), {"sqlstate": "42501"})(
                "permission denied")

    result = step9._finalize_budget(run, DeniedDB(), {})
    assert result["status"] == "unknown_pending_grant"

    bound_run = dict(run, budget_receipt={
        "caps": {"endpoint_budget_profile": 13500,
                 "endpoint_budget_global_rankings": 1,
                 "endpoint_budget_battle_log": 0},
        "run_id": "boot1",
        "deadline_at": "2026-10-04T06:00:00+00:00"})

    class BoundDB(FakeDB):
        def budgets_present(self):
            return True

        def bootstrap_budgets(self, run_id):
            return {"run": {"run_id": "boot1",
                              "manifest_sha256": "ab" * 32,
                              "manifest_count": 12857,
                              "normalized_set_sha256": "cd" * 32,
                              "status": "complete"},
                    "budgets": [
                        {"endpoint": "profile", "cap": 13500,
                         "consumed": 120,
                         "deadline_at": datetime(
                             2026, 10, 4, 6, 0, tzinfo=UTC)},
                        {"endpoint": "global_player_rankings", "cap": 1,
                         "consumed": 1,
                         "deadline_at": datetime(
                             2026, 10, 4, 6, 0, tzinfo=UTC)},
                        {"endpoint": "battle_log", "cap": 0, "consumed": 0,
                         "deadline_at": datetime(
                             2026, 10, 4, 6, 0, tzinfo=UTC)}]}

    result = step9._finalize_budget(bound_run, BoundDB(), {"profile": 100})
    assert result["status"] == "complete" and result["failure"] is None
    tampered = dict(bound_run,
                    budget_receipt=dict(bound_run["budget_receipt"],
                                       run_id="other"))
    result = step9._finalize_budget(tampered, BoundDB(), {})
    assert result["failure"] == "budget_binding_mismatch"


def _quiet_facts():
    return {"filesystems": {
        "pool": {"key": "pool", "mount_point": "/", "source": "/dev/sda",
                 "mnt_id": 1, "filesystem_type": "btrfs",
                 "total_bytes": 1000 * 1024**3, "free_bytes": 900 * 1024**3,
                 "used_bytes": 100 * 1024**3, "use_pct": 10.0,
                 "labels": ["spool", "postgres"],
                 "btrfs": {"metadata_pct": 10.0,
                           "unallocated_bytes": 500 * 1024**3,
                           "error": None, "stderr": None},
                 "error": None}},
        "memory": {"used_bytes": 1024**3, "available_bytes": 8 * 1024**3,
                   "swap_used_bytes": 0,
                   "oom_kills": 0, "psi_avg10": 0.0, "error": None},
        "archive": {"logical_bytes": 100, "objects": 2,
                    "physical_bytes": 100, "error": None}}


def test_parse_btrfs_usage_cases() -> None:
    separate = ("Data,single: Size: 1000, Used: 100\n"
                "Metadata,DUP: Size: 200, Used: 50\n"
                "Unallocated: 5000\n")
    parsed = step9._parse_btrfs_usage(separate)
    assert parsed == {"metadata_pct": 25.0, "unallocated_bytes": 5000}
    combined = "Data+Metadata,single: Size: 4, Used: 1\nUnallocated: 8\n"
    parsed = step9._parse_btrfs_usage(combined)
    assert parsed == {"metadata_pct": None, "unallocated_bytes": 8}
    assert step9._parse_btrfs_usage("") == {
        "metadata_pct": None, "unallocated_bytes": None}


def test_resource_gates_all_thresholds() -> None:
    base = _quiet_facts()
    failures, unknown, strikes = step9.evaluate_resource_gates(base, base, 0)
    assert failures == [] and strikes == 0
    assert unknown == []  # fully known quiet facts prove no false positive
    breach = _quiet_facts()
    pool = breach["filesystems"]["pool"]
    pool["use_pct"] = 85.0
    pool["free_bytes"] = 100 * 1024**3
    pool["used_bytes"] = 200 * 1024**3
    pool["btrfs"] = {"metadata_pct": 90.0,
                     "unallocated_bytes": 10 * 1024**3,
                     "error": "probe_failed", "stderr": "x"}
    breach["memory"] = {"used_bytes": 6 * 1024**3, "available_bytes": 1024**3,
                        "swap_used_bytes": 99,
                        "oom_kills": 3, "psi_avg10": 1.0, "error": None}
    breach["archive"] = {"logical_bytes": 20 * 1024**3, "objects": 200_000,
                         "physical_bytes": 70 * 1024**3,
                         "error": None}
    failures, unknown, strikes = step9.evaluate_resource_gates(base, breach, 1)
    for code in ("filesystem_use_breach", "filesystem_free_breach",
                 "physical_growth_breach", "btrfs_metadata_breach",
                 "btrfs_unallocated_breach", "btrfs_diagnostic_stderr",
                 "btrfs_new_error", "oom_kill_observed", "swap_growth",
                 "memory_low", "archive_logical_breach",
                 "archive_objects_breach", "archive_physical_breach"):
        assert code in failures, code
    assert strikes == 2
    # memory needs two consecutive samples
    current = _quiet_facts()
    current["memory"]["available_bytes"] = 1024**3
    failures, _unknown, strikes = step9.evaluate_resource_gates(base, current, 0)
    assert "memory_low" not in failures and strikes == 1
    # shared pool evaluated once: spool+postgres labels share one entry
    assert len(base["filesystems"]) == 1
    # null stays unknown, never zero
    empty = {"filesystems": {}, "memory": {}, "archive": {}}
    failures, unknown, _strikes = step9.evaluate_resource_gates(empty, empty, 0)
    assert failures == [] and set(unknown) >= {
        "oom_unknown", "swap_unknown", "memory_unknown", "archive_unknown",
        "archive_physical_unknown"}


def _write_url_file(path: Path, data: bytes, mode: int = 0o600) -> str:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)
    return str(path)


def test_database_url_file_contract(tmp_path: Path) -> None:
    url = "postgresql://observer:S3cret-X@127.0.0.1:55440/clashlens"
    good = _write_url_file(tmp_path / "db.url", (url + "\n").encode())
    assert step9._resolve_database_url(
        mock.Mock(database_url=None, database_url_file=good)) == url
    assert step9._resolve_database_url(
        mock.Mock(database_url=None, database_url_file=None)) is None
    with pytest.raises(step9.Step9Error):
        step9._resolve_database_url(
            mock.Mock(database_url=url, database_url_file=good))
    link = tmp_path / "link.url"
    link.symlink_to(good)
    with pytest.raises(step9.Step9Error):
        step9._resolve_database_url(
            mock.Mock(database_url=None, database_url_file=str(link)))
    open_mode = _write_url_file(tmp_path / "open.url", b"x\n", mode=0o644)
    with pytest.raises(step9.Step9Error):
        step9._resolve_database_url(
            mock.Mock(database_url=None, database_url_file=open_mode))
    for name, data in (("empty.url", b""), ("multi.url", b"a\nb\n"),
                       ("nul.url", b"a\0b\n")):
        bad = _write_url_file(tmp_path / name, data)
        with pytest.raises(step9.Step9Error):
            step9._resolve_database_url(
                mock.Mock(database_url=None, database_url_file=bad))
    with pytest.raises(step9.Step9Error):
        step9._resolve_database_url(
            mock.Mock(database_url=None,
                      database_url_file=str(tmp_path / "missing.url")))
    with pytest.raises(step9.Step9Error):
        step9._resolve_database_url(
            mock.Mock(database_url=None, database_url_file="relative.url"))


def test_no_credential_in_artifacts(tmp_path: Path) -> None:
    password = "S3cret-X9q moon"
    url = f"postgresql://observer:{password}@127.0.0.1:55440/clashlens"
    url_file = _write_url_file(tmp_path / "db.url", (url + "\n").encode())
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    cohort = _write_cohort(tmp_path / "cohort.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    run_dir = tmp_path / "run"
    arguments = _start_args(run_dir, cohort, deployed_receipt=str(receipt_path),
                            database_url=None, database_url_file=url_file)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        step9.cmd_start(arguments, db)
    sample_args = mock.Mock(run_dir=str(run_dir), podman_bin="podman",
                            database_url=None, database_url_file=None)
    assert step9.cmd_sample(sample_args, _sample_hooks(db)) == 0
    retained = "".join(path.read_text(encoding="utf-8", errors="replace")
                       for path in run_dir.rglob("*") if path.is_file())
    assert password not in retained
    assert "S3cret" not in retained
    header = json.loads((run_dir / "run.json").read_text())
    assert header["database_url_source"] == "file"


def test_cli_start_accepts_url_file(tmp_path: Path) -> None:
    db_url = _write_url_file(tmp_path / "db.url", b"postgresql://x\n")
    assert db_url.endswith("db.url")
    tariff_path = tmp_path / "tariff.json"
    tariff_path.write_text(json.dumps(_canonical_tariff()))
    cohort = _write_cohort(tmp_path / "cohort.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        fake_db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
        with mock.patch.object(step9, "Database", lambda url: fake_db):
            code = step9.main([
                "start", "--run-dir", str(tmp_path / "run"),
                "--cohort-file", str(cohort),
                "--deployed-receipt", str(receipt_path),
                "--core-start", "2026-10-04T05:00:00Z",
                "--core-end", "2026-10-05T05:00:00Z",
                "--collector-container", "test-collector",
                "--postgres-container", "test-pg",
                "--python-api-container", "test-api",
                "--python-worker-container", "test-worker",
                "--runtime-metrics-url", "http://127.0.0.1:9/x",
                "--spool-path", "/tmp", "--postgres-path", "/tmp",
                "--deadline", "2026-10-05T05:10:00Z",
                "--watchdog-unit", "test-unit",
                "--run-id", "testrun01",
                "--max-invocation-gap-seconds", "5",
                "--archive-egress-interface", "test-eth0",
                "--archive-tariff-file", str(tariff_path),
                "--database-url-file", db_url])
    assert code == 0


def test_start_requires_explicit_gap(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    arguments = _start_args(tmp_path / "nogap", cohort,
                            deployed_receipt=str(receipt_path),
                            max_invocation_gap_seconds=None)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(arguments, db)
        assert error.value.code == "invocation_gap_unpinned"


def test_sample_resource_gate_stops(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    breached = _quiet_facts()
    breached["filesystems"]["pool"]["use_pct"] = 95.0
    hooks = _sample_hooks(db)
    hooks["resource_facts"] = lambda run, db, metrics: breached
    hooks["max_slots"] = 3
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 1
    sample = json.loads((run_dir / "samples" / "minute-0000.json").read_text())
    assert sample["outcome"] == "resource_gate"
    assert sample["resources"]["failures"] == ["filesystem_use_breach"]
    assert list((run_dir / "failures").glob("filesystem_use_breach-*.json"))


def test_archive_usage_real_sql() -> None:
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info, autocommit=True) as connection:
            assert database.archive_usage() == (0, 0)
            connection.execute(
                "INSERT INTO archive_instances (instance_id, endpoint, region,"
                " bucket, marker_key, marker_hash, marker_payload_version)"
                " VALUES ('i1', 'e', 'r', 'b', 'm', %s, 'v1')", ("ab" * 32,))
            for digest, size in (("cd" * 32, 100), ("ef" * 32, 200)):
                connection.execute(
                    "INSERT INTO archive_catalogue (response_hash,"
                    " archive_reference, byte_size, archive_instance_id)"
                    " VALUES (%s, %s, %s, 'i1')",
                    (digest, f"ref-{digest[:8]}", size))
        assert database.archive_usage() == (300, 2)


def test_start_admission_receipt_binding(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    base = {"core_start": "2026-10-04T05:00:00Z",
            "core_end": "2026-10-05T05:00:00Z"}
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        receipt = _receipt_scope(admission_run_id="disabled",
                                 admission_start="disabled",
                                 admission_end="disabled")
        receipt["configuration"]["fields"][
            "admission_evidence_max_events"] = "0"
        receipt["configuration"]["fields"][
            "admission_evidence_max_selected_entries"] = "0"
        path = tmp_path / "disabled.json"
        path.write_text(json.dumps(receipt))
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(tmp_path / "d0", cohort,
                                        deployed_receipt=str(path), **base), db)
        assert error.value.code == "admission_disabled"
        path = tmp_path / "mismatch.json"
        path.write_text(json.dumps(_receipt_scope(admission_run_id="other")))
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(tmp_path / "d1", cohort,
                                        deployed_receipt=str(path), **base), db)
        assert error.value.code == "admission_run_mismatch"
        path = tmp_path / "interval.json"
        path.write_text(json.dumps(_receipt_scope(
            admission_end="2026-10-06T05:00:00Z")))
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(tmp_path / "d2", cohort,
                                        deployed_receipt=str(path), **base), db)
        assert error.value.code == "admission_interval_mismatch"


def _seed_admission_event(connection, run_id: str, invocation: str,
                          cycle: datetime, at: datetime, *, gate: bool,
                          player=None, version=None, job=None,
                          handoff=None,
                          cap_start: datetime = datetime(
                              2026, 10, 4, 5, 0, tzinfo=UTC)) -> None:
    due = cycle - timedelta(seconds=60)
    selected = [player] if player is not None else []
    connection.execute(
        "INSERT INTO collector_regular_admission_evidence"
        " (run_id, invocation_id, cycle_at, scheduler_at, database_at,"
        " capture_start, capture_end, gate_allowed, gate_handoff_at,"
        " batch_limit, visible_due_count, visible_due_min_at,"
        " unselected_visible_due_count, unselected_visible_past_deadline_count,"
        " selected_past_deadline_count, selected_player_ids, selected_due_ats,"
        " selected_profile_version_ids, selected_eligibility_states,"
        " inserted_job_ids, advanced_count)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1000, 1, %s, 0, 0, 0,"
        " %s, %s, %s, %s, %s, %s)",
        (run_id, invocation, cycle, cycle, at, cap_start,
         cap_start + timedelta(hours=30),
         gate, handoff, due, selected,
         [due] if selected else [], [version] if selected else [],
         ["eligible"] if selected else [], [job] if selected else [],
         1 if selected else 0))


def test_db_suppression_and_tail_semantics() -> None:
    """DB-evidence negatives: suppression breach vs late-handoff tail."""
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info, autocommit=True) as connection:
            player = _seed_player(connection, TAGS[0])
            version = connection.execute(
                "SELECT current_profile_version_id FROM players WHERE id = %s",
                (player,)).fetchone()[0]
            cycle0 = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
            handoff = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence_runs"
                " (run_id, capture_start, capture_end, max_events,"
                " max_selected_entries) VALUES ('sup1', %s, %s, 108000,"
                " 5000000)", (cycle0, cycle0 + timedelta(hours=30)))
            key = f"regular:{player}:{int(cycle0.timestamp())}"
            job = connection.execute(
                "INSERT INTO collector_jobs (work_type, scope, player_id,"
                " normalized_tag, capacity_pool, priority, due_at,"
                " coalescing_key, status, created_at)"
                " VALUES ('regular_poll', 'player', %s, %s, 'normal', 100,"
                " %s, %s, 'complete', %s) RETURNING id",
                (player, TAGS[0], cycle0, key, cycle0)).fetchone()[0]
            # normal gate-open core event: no breach
            _seed_admission_event(connection, "sup1", "ab" * 16, cycle0,
                                  cycle0 + timedelta(seconds=1), gate=True,
                                  player=player, version=version, job=job)
            # suppressed-window gate-open event: breach (04:55 aligned cycle)
            bad_cycle = handoff - timedelta(minutes=5)
            _seed_admission_event(connection, "sup1", "cd" * 16, bad_cycle,
                                  handoff - timedelta(seconds=30), gate=True,
                                  player=player, version=version, job=job)
        header = database.admission_run("sup1")
        events = database.admission_events(
            "sup1", "2026-10-04T05:00:00Z", "2026-10-05T05:10:00Z")
        assert len(events) == 2
        counts = database.admission_profile_counts(
            "sup1", "2026-10-04T05:00:00Z", "2026-10-05T05:10:00Z")
        roots = database.semantic_roots(
            "2026-10-04T05:00:00Z", "2026-10-05T05:10:00Z")
        run = {"core_start": "2026-10-04T05:00:00+00:00",
               "core_end": "2026-10-05T05:00:00+00:00", "mode": "live-day",
               "max_invocation_gap_seconds": 3600}
        result = step9.evaluate_admission(
            run=run, header=header, events=events, profile_counts=counts,
            roots=roots, max_gap_seconds=3600)
        assert "admission_suppression_breach" in result["failures"]
        # late handoff with short tail: tail unknown from real evidence
        with psycopg.connect(info, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO collector_regular_admission_evidence_runs"
                " (run_id, capture_start, capture_end, max_events,"
                " max_selected_entries) VALUES ('sup2', %s, %s, 108000,"
                " 5000000)", (cycle0, cycle0 + timedelta(hours=30)))
            reopen = handoff + timedelta(minutes=20)
            _seed_admission_event(connection, "sup2", "ef" * 16, cycle0,
                                  cycle0 + timedelta(seconds=1), gate=True,
                                  player=player, version=version, job=job)
            _seed_admission_event(connection, "sup2", "aa" * 16, reopen,
                                  reopen + timedelta(seconds=1), gate=True,
                                  player=player, version=version, job=job,
                                  handoff=reopen)
        header2 = database.admission_run("sup2")
        events2 = database.admission_events(
            "sup2", "2026-10-04T05:00:00Z", "2026-10-05T05:40:00Z")
        counts2 = database.admission_profile_counts(
            "sup2", "2026-10-04T05:00:00Z", "2026-10-05T05:40:00Z")
        result2 = step9.evaluate_admission(
            run=run, header=header2, events=events2, profile_counts=counts2,
            roots=roots, max_gap_seconds=3600)
        assert "admission_tail_insufficient" in result2["unknown"]


def test_device_stats_probe_parsing() -> None:
    out = ("[--/dev/sda].write_io_errs   3\n"
           "[--/dev/sda].read_io_errs   0\n"
           "[--/dev/sdb].write_io_errs   1\n")
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stdout=out, stderr="")
        result = step9._btrfs_device_stats("/mnt")
    assert result == {"errors": {"write_io_errs": 4, "read_io_errs": 0},
                      "error": None}
    with mock.patch("subprocess.run") as run:
        run.return_value = mock.Mock(returncode=1, stdout="", stderr="x")
        assert step9._btrfs_device_stats("/mnt")["error"] == "device_stats_failed"
    with mock.patch("subprocess.run",
                           side_effect=FileNotFoundError):
        assert step9._btrfs_device_stats("/mnt")["error"] == "tool_missing"


def test_device_error_increase_fails() -> None:
    base = _quiet_facts()
    for fs in base["filesystems"].values():
        fs["filesystem_type"] = "btrfs"
        fs["btrfs"]["device"] = {"errors": {"write_io_errs": 0}, "error": None}
    current = _quiet_facts()
    for fs in current["filesystems"].values():
        fs["filesystem_type"] = "btrfs"
        fs["btrfs"]["device"] = {"errors": {"write_io_errs": 2}, "error": None}
    failures, _unknown, _s = step9.evaluate_resource_gates(base, current, 0)
    assert "btrfs_device_error" in failures
    current["filesystems"]["pool"]["btrfs"]["device"] = {
        "errors": {}, "error": "tool_missing"}
    failures, _unknown, _s = step9.evaluate_resource_gates(base, current, 0)
    assert "btrfs_device_stats_unavailable" in failures


def test_cgroup_probe_uses_inspect_path(tmp_path: Path) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.events").write_text("low 0\nhigh 0\noom_kill 2\n")
    (cgroup / "memory.swap.current").write_text("4096\n")
    (cgroup / "memory.current").write_text("8192\n")
    result = step9._read_cgroup_files(cgroup)
    assert result == {"oom_kills": 2, "swap_current_bytes": 4096,
                      "mem_current_bytes": 8192}

    calls: list = []

    def fake_run(command, **kwargs):
        calls.append(command[0])
        return mock.Mock(returncode=1, stdout="", stderr="")

    with mock.patch("subprocess.run", side_effect=fake_run):
        failed = step9._cgroup_numbers("podman-test", "c1")
    assert failed["error"] == "cgroup_path_unavailable"
    assert calls == ["podman-test"]


def test_podman_probe_honors_bin() -> None:
    seen: list = []

    def fake_run(command, **kwargs):
        seen.append(command[0])
        return mock.Mock(returncode=1, stdout="", stderr="")

    with mock.patch("subprocess.run", side_effect=fake_run):
        assert step9._podman_container_probe(
            {"containers": {"collector": "c"}, "podman_bin": "podman-x"}) is None
    assert seen == ["podman-x"]


def test_operating_regression_gates(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "opreg", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    run_dir2, _run2 = _sealed_run(tmp_path, "opreg2", db)
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    db.operating_data = {
        "status": "complete",
        "identity": {"status": "complete", "rows": []},
        "collector_queues": {"status": "complete", "rows": []},
        "python_queues": {"status": "complete", "rows": []},
        "relations": {"status": "complete", "rows": []},
        "processed": {"status": "complete", "rows": []},
        "failures": {"status": "complete",
                     "rows": [["transport", 3]]}}
    assert step9.cmd_finalize(arguments2, {"db": db}) == 1
    final = json.loads((run_dir2 / "final.json").read_text())
    assert final["operating"]["failure_code"] == "operating_regressed"
    assert step9.cmd_validate(arguments2) == 1
    db.operating_data = {"status": "unknown", "failure_code": "denied"}
    run_dir3, _run3 = _sealed_run(tmp_path, "opreg3", db)
    arguments3 = mock.Mock(run_dir=str(run_dir3), podman_bin="podman")
    assert step9.cmd_finalize(arguments3, {"db": db}) == 2


def test_cgroup_oom_increase_fails() -> None:
    base = _quiet_facts()
    base["cgroup"] = {"oom_kills": 0, "swap_current_bytes": 0,
                      "mem_current_bytes": 1, "mem_peak_bytes": 1,
                      "error": None}
    current = _quiet_facts()
    current["cgroup"] = {"oom_kills": 1, "swap_current_bytes": 99,
                         "mem_current_bytes": 1, "mem_peak_bytes": 1,
                         "error": None}
    failures, _unknown, _s = step9.evaluate_resource_gates(base, current, 0)
    assert "oom_kill_observed" in failures
    assert "swap_growth" in failures


def test_preflight_drain_authorized_stop(tmp_path: Path) -> None:
    """Metrics may be absent after the proven minute-60 stop only."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    states = {"running": True}

    def container_probe(run):
        return {"running": states["running"], "image": "sha256:test",
                "started_at": "t", "stats": None,
                "cgroup": {"oom_kills": 0, "swap_current_bytes": 0,
                           "mem_current_bytes": 1, "mem_peak_bytes": 1,
                           "error": None}}

    def metrics(url):
        if not states["running"]:
            raise step9.Step9Error("metrics_unavailable", "stopped")
        return {"process_id": "p1", "started_at": 1.0, "counters": {},
                "digest": "x"}

    run_dir, run = _sealed_preflight(tmp_path, "drain", db)
    _pin_resources(run_dir)
    # rewrite: drive the loop directly with a stopping collector
    import shutil
    shutil.rmtree(run_dir / "samples")
    (run_dir / "samples").mkdir()
    mono = [0]
    walls = [step9._parse_utc(run["core_start"]) - timedelta(seconds=60)]

    def clock():
        return mono[0]

    def now_utc():
        mono[0] += 60_000_000_000
        walls[0] += timedelta(seconds=60)
        return walls[0]

    quiet_wire = {
        "status": "captured", "failure_code": None, "boot_id": run.get("boot_id"),
        "interfaces": {"test-eth0": {
            "present": True, "rx_bytes": 1000, "tx_bytes": 500,
            "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}}
    benign_pgdata = {
        "status": "captured", "failure_code": None,
        "captured_at": "2026-10-04T05:00:00+00:00",
        "container": "test-pg", "image": "sha256:pg",
        "pgdata": "/var/lib/postgresql/data",
        "source": "podman-exec:test-pg",
        "pgdata_bytes": 1000, "pg_wal_bytes": 100}
    benign_s3 = [{"archive": {"remote_attempts": {"get": 1}}}]
    hooks = {"db": db, "fixed_ids": db.fixed_ids, "fetch_metrics": metrics,
             "container_probe": container_probe,
             "watchdog_check": lambda run: True, "clock": clock,
             "now_utc": now_utc, "no_sleep": True, "max_slots": 75,
             "resource_facts": lambda run, db, metrics: _quiet_facts(),
             "wire_facts": lambda run: quiet_wire,
             "worker_probe": lambda run: benign_s3,
             "pgdata_probe": lambda run: benign_pgdata}
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    # collector never stops but metrics vanish at slot 62: failure
    def flaky_metrics(url):
        slot = len(list((run_dir / "samples").glob("*.json")))
        if slot >= 62:
            raise step9.Step9Error("metrics_unavailable", "gone")
        return metrics(url)

    hooks["fetch_metrics"] = flaky_metrics
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob("two_consecutive_unavailable-*.json"))
    # authorized stop at minute 60: absent metrics pass through the drain
    db2 = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir2, _run2 = _sealed_preflight(tmp_path, "drain2", db2)
    _pin_resources(run_dir2)
    shutil.rmtree(run_dir2 / "samples")
    (run_dir2 / "samples").mkdir()
    stopped = {"at": 60}

    def stopping_probe(run):
        slot = len(list((run_dir2 / "samples").glob("*.json")))
        return {"running": slot < stopped["at"], "image": "sha256:test",
                "started_at": "t", "stats": None,
                "cgroup": {"oom_kills": 0, "swap_current_bytes": 0,
                             "mem_current_bytes": 1, "mem_peak_bytes": 1,
                             "error": None}}

    def drain_metrics(url):
        slot = len(list((run_dir2 / "samples").glob("*.json")))
        if slot >= stopped["at"]:
            raise step9.Step9Error("metrics_unavailable", "stopped")
        return {"process_id": "p1", "started_at": 1.0, "counters": {},
                "digest": "x"}

    mono2 = [0]
    walls2 = [step9._parse_utc(run["core_start"]) - timedelta(seconds=60)]

    def clock2():
        return mono2[0]

    def now_utc2():
        mono2[0] += 60_000_000_000
        walls2[0] += timedelta(seconds=60)
        return walls2[0]

    hooks2 = {"db": db2, "fixed_ids": db2.fixed_ids,
              "fetch_metrics": drain_metrics,
              "container_probe": stopping_probe,
              "watchdog_check": lambda run: True,
              "clock": clock2, "now_utc": now_utc2,
              "no_sleep": True, "max_slots": 75,
              "resource_facts": lambda run, db, metrics: _quiet_facts(),
              "wire_facts": lambda run: {
                  "status": "captured", "failure_code": None,
                  "boot_id": run.get("boot_id"),
                  "interfaces": {"test-eth0": {
                      "present": True, "rx_bytes": 1000, "tx_bytes": 500,
                      "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}},
              "worker_probe": lambda run: [
                  {"archive": {"remote_attempts": {"get": 1}}}],
              "pgdata_probe": lambda run: {
                  "status": "captured", "failure_code": None,
                  "captured_at": "2026-10-04T05:00:00+00:00",
                  "container": "test-pg", "image": "sha256:pg",
                  "pgdata": "/var/lib/postgresql/data",
                  "source": "podman-exec:test-pg",
                  "pgdata_bytes": 1000, "pg_wal_bytes": 100}}
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    assert step9.cmd_sample(arguments2, hooks2) == 0
    assert len(list((run_dir2 / "samples").glob("*.json"))) == 75
    slot60 = json.loads((run_dir2 / "samples" / "minute-0060.json").read_text())
    assert slot60["metrics_absent_authorized"] is True
    assert slot60["stop_proven"] is True
    assert slot60["metrics_error"] == "metrics_unavailable"
    assert step9.cmd_finalize(arguments2, {"db": db2}) == 0
    assert step9.cmd_validate(arguments2) == 0


def test_operating_snapshot_worker_sections() -> None:
    """Worker-role operating capture: sizes/queues work, failures wait."""
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        import re

        from clashlens.operating import RELATION_NAMES

        existing = re.search(r"options='([^']*)'", info)
        options = ((existing.group(1) + " ") if existing else "") \
            + "-c role=clashlens_python_worker"
        worker_db = step9.Database(
            lambda: psycopg.connect(info, options=options))
        snap = worker_db.operating_snapshot(list(RELATION_NAMES))
        assert snap["identity"]["status"] == "complete"
        assert snap["relations"]["status"] == "complete"
        assert len(snap["relations"]["rows"]) == len(RELATION_NAMES)
        assert snap["collector_queues"]["status"] == "complete"
        assert snap["failures"]["status"] == "complete"
        assert snap["status"] == "complete"


def test_pgdata_probe_contract() -> None:
    ok_out = {"inspect": "sha256:pgimg\nlocalhost/pg\n",
              "env": "/var/lib/postgresql/data\n",
              "du": "1048576\t/var/lib/postgresql/data\n65536\t/var/lib/postgresql/data/pg_wal\n"}

    def fake_run(command, **kwargs):
        if "inspect" in command:
            return mock.Mock(returncode=0, stdout=ok_out["inspect"], stderr="")
        if "printenv" in command:
            return mock.Mock(returncode=0, stdout=ok_out["env"], stderr="")
        if "du" in command:
            return mock.Mock(returncode=0, stdout=ok_out["du"], stderr="")
        raise AssertionError(command)

    seen = []

    def spy_run(command, **kwargs):
        seen.append(command)
        return fake_run(command, **kwargs)

    with mock.patch("subprocess.run", side_effect=spy_run):
        result = step9._pgdata_probe("podman-x", "test-pg", "sha256:pgimg")
    assert result["status"] == "captured"
    assert result["pgdata_bytes"] == 1048576
    assert result["pg_wal_bytes"] == 65536
    assert result["pgdata"] == "/var/lib/postgresql/data"
    assert all(command[0] == "podman-x" for command in seen)
    assert step9._pgdata_probe("podman-x", "bad name!")["failure_code"] == \
        "pgdata_unsafe_container"
    assert step9._pgdata_probe("/bin/pod", "test-pg")["failure_code"] == \
        "pgdata_unsafe_bin"
    with mock.patch("subprocess.run", side_effect=spy_run):
        changed = step9._pgdata_probe("podman-x", "test-pg", "sha256:other")
    assert changed["failure_code"] == "pgdata_image_changed"
    assert "password" not in json.dumps(result)

    def bad_env(command, **kwargs):
        if "printenv" in command:
            return mock.Mock(returncode=0, stdout="../../etc\n", stderr="")
        return fake_run(command, **kwargs)

    with mock.patch("subprocess.run", side_effect=bad_env):
        assert step9._pgdata_probe("podman-x", "test-pg")["failure_code"] == \
            "pgdata_unsafe_path"

    def missing_du(command, **kwargs):
        if "du" in command:
            raise FileNotFoundError("no du")
        return fake_run(command, **kwargs)

    with mock.patch("subprocess.run", side_effect=missing_du):
        result = step9._pgdata_probe("podman-x", "test-pg")
    assert result["failure_code"] == "pgdata_probe_unavailable"


def test_pgdata_unavailable_strikes(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    hooks = _sample_hooks(db)
    hooks["pgdata_probe"] = lambda run: {"status": "unknown",
                                         "failure_code": "pgdata_gone"}
    hooks["max_slots"] = 3
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob("two_consecutive_unavailable-*.json"))


def _quiet_wire(rx: int = 1000, tx: int = 500, **overrides):
    facts = {"status": "captured", "failure_code": None, "boot_id": "boot-1",
             "interfaces": {"test-eth0": {
                 "present": True, "rx_bytes": rx, "tx_bytes": tx,
                 "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}}
    facts.update(overrides)
    return facts


def test_wire_gates() -> None:
    base = _quiet_wire()
    failures, unknown, total = step9.evaluate_wire(base, _quiet_wire(), 100)
    assert failures == [] and unknown == []
    assert total == 100  # no growth: prior only, never zero-claimed traffic
    grown = _quiet_wire(rx=2000, tx=1500)
    failures, _unknown, total = step9.evaluate_wire(base, grown, 100)
    assert failures == [] and total == 100 + 2000
    over = _quiet_wire(rx=1000 + 70 * 1024**3, tx=500)
    failures, _u, _t = step9.evaluate_wire(base, over, 100)
    assert "transfer_breach" in failures
    reset = _quiet_wire(rx=10, tx=500)
    failures, _u, _t = step9.evaluate_wire(base, reset, 100)
    assert "wire_counter_reset" in failures
    gone = _quiet_wire()
    gone["interfaces"] = {}
    failures, _u, _t = step9.evaluate_wire(base, gone, 100)
    assert "wire_interface_missing" in failures
    renamed = _quiet_wire()
    renamed["interfaces"]["test-eth0"]["mac"] = "00:00:00:00:00:00"
    failures, _u, _t = step9.evaluate_wire(base, renamed, 100)
    assert "wire_identity_changed" in failures
    assert step9.evaluate_wire(base, {"status": "unknown"}, 100)[0] == [
        "wire_unavailable"]
    boot = _quiet_wire()
    boot["boot_id"] = "boot-2"
    assert step9.evaluate_wire(base, boot, 100)[0] == ["wire_boot_changed"]


def test_wire_route_continuity() -> None:
    base = _quiet_wire()
    base["interfaces"]["test-eth0"]["route_dev"] = "test-eth0"
    base["interfaces"]["test-eth0"]["route_error"] = None
    moved = _quiet_wire()
    moved["interfaces"]["test-eth0"]["route_dev"] = "other0"
    moved["interfaces"]["test-eth0"]["route_error"] = None
    failures, _u, _t = step9.evaluate_wire(base, moved, 0)
    assert "wire_route_changed" in failures
    noprobe = _quiet_wire()
    noprobe["interfaces"]["test-eth0"]["route_dev"] = None
    noprobe["interfaces"]["test-eth0"]["route_error"] = "route_lookup_failed"
    failures, _u, _t = step9.evaluate_wire(base, noprobe, 0)
    assert "wire_route_unavailable" in failures


def test_proc_net_dev_parsing(tmp_path: Path) -> None:
    sample = ("Inter-|   Receive                                                |  Transmit\n"
              " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
              "  eth0: 1000       1    0    0    0     0          0         0     2000       2    0    0    0     0       0          0\n"
              "    lo: 50       1    0    0    0     0          0         0       50       1    0    0    0     0       0          0\n")
    with mock.patch.object(step9.Path, "read_text") as reader:
        reader.return_value = sample
        parsed = step9._read_proc_net_dev()
    assert parsed == {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000},
                      "lo": {"rx_bytes": 50, "tx_bytes": 50}}


def test_s3_accounting() -> None:
    metrics = {"counters": {
        "clashlens_collector_archive_requests_total{operation=put}": 10.0,
        "clashlens_collector_archive_requests_total{operation=get}": 5.0,
        "clashlens_collector_jobs_total{work_type=x}": 99.0}}
    snapshot = step9._s3_snapshot(metrics, {"get": 2, "bucket": 1})
    assert snapshot == {"go": {
        "clashlens_collector_archive_requests_total{operation=put}": 10,
        "clashlens_collector_archive_requests_total{operation=get}": 5},
        "go_total": 15, "python": {"get": 2, "bucket": 1}, "python_total": 3,
        "total": 18}
    assert step9._s3_decreased({"go": {"a": 5}}, {"go": {"a": 4}}) is True
    assert step9._s3_decreased({"go": {"a": 5}}, {"go": {"a": 5}}) is False
    files = [{"archive": {"remote_attempts": {"get": 2}}},
             {"archive": {"remote_attempts": {"get": 1, "marker": 1}}}]
    totals, error = step9._worker_snapshots(
        {}, lambda run: files)
    assert totals == {"get": 3, "marker": 1} and error is None
    totals, error = step9._worker_snapshots(
        {}, lambda run: [{"archive": {}}])
    assert totals == {} and error is not None


def test_slot_zero_transfer_breach_records(tmp_path: Path) -> None:
    """Slot-0 early gate branches must record, never raise NameError."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    _pin_wire(run_dir)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    hooks = _sample_hooks(db)
    breached_wire = {
        "status": "captured", "failure_code": None, "boot_id": "boot-1",
        "interfaces": {"test-eth0": {
            "present": True, "rx_bytes": 1000 + 70 * 1024**3, "tx_bytes": 500,
            "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}}
    hooks["wire_facts"] = lambda run: dict(
        breached_wire, boot_id=run.get("boot_id"))
    hooks["max_slots"] = 1
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 1
    sample = json.loads((run_dir / "samples" / "minute-0000.json").read_text())
    assert sample["outcome"] == "transfer_gate"
    assert list((run_dir / "failures").glob("transfer_breach-*.json"))


def test_tariff_file_contract(tmp_path: Path) -> None:
    good = tmp_path / "tariff.json"
    good.write_text(json.dumps(_canonical_tariff()))
    block = step9._tariff_block(
        step9._read_tariff_file(str(good)),
        datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert block["with_uncertainty_eur"] == 3.686616
    assert block["digest"] and block["note"].startswith("tariff estimate")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_canonical_tariff(payload_cap_gib=17)))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block(
            step9._read_tariff_file(str(bad)),
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert error.value.code == "tariff_mismatch"
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(_canonical_tariff(verified_utc_date="2026-01-01")))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block(
            step9._read_tariff_file(str(stale)),
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert error.value.code == "tariff_stale"
    over = tmp_path / "over.json"
    over.write_text(json.dumps(_canonical_tariff(with_uncertainty_eur=9.99)))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block(
            step9._read_tariff_file(str(over)),
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert error.value.code == "tariff_mismatch"
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"source": "x"}))
    with pytest.raises(step9.Step9Error):
        step9._tariff_block(
            step9._read_tariff_file(str(missing)),
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    with pytest.raises(step9.Step9Error):
        step9._read_tariff_file(str(tmp_path / "absent.json"))
    with pytest.raises(step9.Step9Error):
        step9._read_tariff_file("relative.json")


def test_start_requires_tariff_file(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    arguments = _start_args(tmp_path / "notariff", cohort,
                            deployed_receipt=str(receipt_path),
                            archive_tariff_file="/nonexistent-tariff.json")
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(arguments, db)
        assert error.value.code == "tariff_unavailable"


def test_tariff_oracle_counterexamples(tmp_path: Path) -> None:
    """Oracle BLOCK cases: enlarged stop, altered rate, nonfinite rate."""
    core = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)

    def block(**overrides):
        payload = _canonical_tariff(**overrides)
        path = tmp_path / f"t-{len(os.listdir(tmp_path))}.json"
        path.write_text(json.dumps(payload))
        return step9._tariff_block(
            step9._read_tariff_file(str(path)), core)

    assert block()["with_uncertainty_eur"] == 3.686616
    with pytest.raises(step9.Step9Error) as error:
        block(operational_stop_eur=100, with_uncertainty_eur=99)
    assert error.value.code == "tariff_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        block(tariff_eur_per_decimal_gb_hour="1")
    assert error.value.code == "tariff_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        block(egress_eur_per_decimal_gb="NaN")
    assert error.value.code == "tariff_malformed"
    with pytest.raises(step9.Step9Error) as error:
        block(egress_eur_per_decimal_gb="Infinity")
    assert error.value.code == "tariff_malformed"
    with pytest.raises(step9.Step9Error) as error:
        block(storage_projection_eur=0.01)
    assert error.value.code == "tariff_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        block(tariff_eur_per_decimal_gb_hour="0.000044",
              storage_projection_eur=3.535488,
              combined_projection_eur=4.225488,
              with_uncertainty_eur=6.338232)
    assert error.value.code == "tariff_envelope_exceeded"


def test_s3_nonzero_initial_counters(tmp_path: Path) -> None:
    """Startup/readiness attempts before sampling must not false-reset."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    def rising_metrics(url):
        slot = len(list((run_dir / "samples").glob("*.json")))
        total = 5000 + slot * 10
        return {"process_id": "p1", "started_at": 1.0,
                "counters": {f"k{total}": float(total)},
                "digest": str(total)}

    hooks = _sample_hooks(db)
    hooks["fetch_metrics"] = rising_metrics
    hooks["max_slots"] = 3
    hooks["single_pass"] = False
    seen = []

    original_snapshot = step9._s3_snapshot

    def spy_snapshot(metrics, py):
        snapshot = original_snapshot(metrics, py)
        value = int(str(metrics["digest"]))
        snapshot["go"] = {"put": value}
        snapshot["go_total"] = value
        snapshot["total"] = value
        seen.append(value)
        return snapshot

    hooks["worker_probe"] = lambda run: []
    with mock.patch.object(step9, "_s3_snapshot", side_effect=spy_snapshot):
        assert step9.cmd_sample(arguments, hooks) == 0
    assert seen[0] == 5000 and seen[-1] == 5020
    samples = sorted((run_dir / "samples").glob("*.json"))
    assert len(samples) == 3
    import json as _json

    cumulative = [ _json.loads(p.read_text())["s3_attempts_cumulative"]
                   for p in samples]
    assert cumulative == [5021, 5031, 5041]
    assert not list((run_dir / "failures").glob("s3_counter_reset-*.json"))
    transfer = step9._finalize_transfer(
        [_json.loads(p.read_text()) for p in samples], {})
    assert transfer["status"] == "complete"
    assert transfer["s3_attempts"] == 5041


def test_s3_prior_input_contract(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        _run_dir, header = _started_run(tmp_path, db, run_dir_name="p0")
        prior = header["s3_prior"]
        assert prior["attempts"] == 21
        assert prior["provenance"] == step9.S3_PRIOR_PROVENANCE
        assert prior["record_sha256"] == step9._sha256(step9._canonical(
            {"attempts": 21, "provenance": step9.S3_PRIOR_PROVENANCE}))
        _run_dir, header = _started_run(
            tmp_path, db, run_dir_name="p1", prior_s3_attempts=7,
            prior_s3_provenance="rehearsal-partial-7")
        assert header["s3_prior"]["attempts"] == 7
        for index, (bad_attempts, bad_provenance) in enumerate((
                (-1, "x"), (100001, "x"), (True, "x"), ("7", "x"),
                (500, None), (7, ""), (7, "y" * 513))):
            with pytest.raises(step9.Step9Error) as error:
                _started_run(tmp_path, db, run_dir_name=f"pbad{index}",
                             prior_s3_attempts=bad_attempts,
                             prior_s3_provenance=bad_provenance)
            assert error.value.code == "s3_prior_invalid"



def test_all_prior_flags_coexist(tmp_path: Path) -> None:
    """Parser keeps wire+S3 prior flags adjacent; all four feed run.json."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        _run_dir, header = _started_run(
            tmp_path, db, run_dir_name="priors",
            prior_transfer_bytes=1024,
            prior_transfer_provenance="wire-partial-1k",
            prior_s3_attempts=9,
            prior_s3_provenance="rehearsal-partial-9")
        assert header["transfer_prior_bytes"] == 1024
        assert header["transfer_prior_provenance"] == "wire-partial-1k"
        assert header["s3_prior"]["attempts"] == 9


def test_cli_parses_all_prior_flags(tmp_path: Path) -> None:
    args = [
        "start", "--run-dir", str(tmp_path), "--mode", "preflight",
        "--cohort-file", "cohort", "--deployed-receipt", "receipt",
        "--core-start", "2026-10-04T05:00:00Z",
        "--core-end", "2026-10-05T05:00:00Z",
        "--collector-container", "collector", "--postgres-container", "pg",
        "--python-api-container", "api", "--python-worker-container", "worker",
        "--runtime-metrics-url", "http://metrics", "--spool-path", "/tmp",
        "--postgres-path", "/tmp", "--deadline", "2026-10-05T05:10:00Z",
        "--watchdog-unit", "unit",
        "--prior-transfer-bytes", "10", "--prior-transfer-provenance", "p",
        "--prior-s3-attempts", "11", "--prior-s3-provenance", "q"]
    namespace = step9.build_parser().parse_args(args)
    assert namespace.prior_transfer_bytes == 10
    assert namespace.prior_transfer_provenance == "p"
    assert namespace.prior_s3_attempts == 11
    assert namespace.prior_s3_provenance == "q"
