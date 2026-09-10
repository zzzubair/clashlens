"""Focused tests for scripts/step9_check.py (stdlib + real PostgreSQL, no skips).

Unit tests use fake adapters only. PostgreSQL tests run against a migrated
disposable schema and fail (never skip) when no database is available. Fake
stop adapters never touch real containers: they refuse any container name
that does not start with "test-".
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
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
        # Per-test private spool: shared /tmp is not reliably du-clean
        # (unreadable systemd entries fail the probe), unlike a dedicated
        # production spool directory.
        "spool_path": str(run_dir.parent),
        "postgres_path": str(run_dir.parent),
        "lead_in_seconds": 0, "tail_seconds": 0,
        "deadline": "2026-10-05T05:10:00Z",
        "max_sample_age_seconds": 125, "watchdog_unit": "test-unit",
        "run_id": "testrun01", "database_url": None, "mode": "live-day",
        "max_invocation_gap_seconds": 5, "bootstrap_run_id": "boot1",
        "budget_run_id": "budget1",
        "archive_tariff_file": None, "archive_interfaces": ["test-eth0"],
        "archive_route_host": None, "prior_transfer_bytes": None,
        "prior_transfer_provenance": None, "prior_s3_attempts": None,
        "prior_s3_provenance": None,
        "archive_retained_cap_bytes": 16 * 1024**3,
        "transfer_cap_bytes": 64 * 1024**3, "s3_cap_attempts": 100_000,
        "spool_allocated_cap_bytes": 64 * 1024**3,
    }
    defaults.update(overrides)
    if defaults.get("archive_tariff_file") is None:
        live = defaults.get("mode", "live-day") == "live-day"
        tariff_path = run_dir.parent / ("tariff-live.json" if live
                                        else "tariff.json")
        if not tariff_path.exists():
            tariff_path.write_text(json.dumps(
                _canonical_live_tariff() if live else _canonical_tariff()))
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


def _canonical_live_tariff(**overrides):
    """Test-only: exact canonical prospective refresh payload."""
    payload = {
        "billable_units_round_up": True,
        "combined_tax_and_uncertainty_factor": "1.5",
        "cost_scope": ("run transfer and first186days of newly retained "
                        "storage; not lifetime retention"),
        "currency": "EUR",
        "egress_eur_per_decimal_gb": "0.01",
        "envelopes": {
            "absolute": {
                "aggregate_transfer_bytes": 600000000000,
                "before_factor_eur": "35.462400",
                "ceiling_eur": "55",
                "egress_projection_eur": "6.00",
                "fits": True,
                "new_retained_bytes": 300000000000,
                "storage_projection_eur": "29.462400",
                "with_factor_eur": "53.1936000",
            },
            "operational": {
                "aggregate_transfer_bytes": 570000000000,
                "before_factor_eur": "32.707200",
                "ceiling_eur": "50",
                "egress_projection_eur": "5.70",
                "fits": True,
                "new_retained_bytes": 275000000000,
                "storage_projection_eur": "27.007200",
                "with_factor_eur": "49.0608000",
            },
        },
        "free_egress_allowance_used": False,
        "ingress_included": True,
        "listed_prices_exclude_tax": True,
        "provider": "Scaleway",
        "region": "Paris",
        "requests_included": True,
        "retrieved_at": "2026-09-10T06:46:59.328362+00:00",
        "run_authorized": False,
        "schema": "issue92-phase5-tariff-refresh-v1",
        "source_url": "https://www.scaleway.com/en/pricing/storage/",
        "storage_class": "Standard Multi-AZ",
        "storage_eur_per_decimal_gb_hour": "0.000022",
        "storage_horizon_days": 186,
        "tax_rate_claimed": None,
        "verification_method": (
            "official pricing page content returned by web search; "
            "full-page web open exceeded size limit and direct urllib "
            "fetch returned403"),
    }
    payload.update(overrides)
    return payload


def _receipt_scope(scope: str = "deployed-stack", discovery: str = "false",
                   budget: bool = True, admission_run_id: str = "testrun01",
                   admission_start: str = "2026-10-04T05:00:00Z",
                   admission_end: str = "2026-10-05T05:00:00Z",
                   budget_run_id: str = "budget1",
                   budget_deadline_at: str = "2026-10-05T06:00:00+00:00",
                   budget_caps: tuple = (13500, 1, 0)) -> dict:
    fields = {"player_discovery_enabled": discovery,
              "admission_evidence_run_id": admission_run_id,
              "admission_evidence_start": admission_start,
              "admission_evidence_end": admission_end,
              "admission_evidence_max_events": "108000",
              "admission_evidence_max_selected_entries": "5000000"}
    if budget:
        fields.update({
            "endpoint_budget_enabled": "true",
            "endpoint_budget_profile": str(budget_caps[0]),
            "endpoint_budget_global_rankings": str(budget_caps[1]),
            "endpoint_budget_battle_log": str(budget_caps[2]),
            "endpoint_budget_run_id": budget_run_id,
            "endpoint_budget_deadline_at": budget_deadline_at,
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
        mapping = getattr(self, "budgets_by_run", None)
        if mapping is not None:
            return mapping.get(run_id, {"run": None, "budgets": []})
        return getattr(self, "budgets_data",
                       {"run": None, "budgets": []})

    def seed_endpoint_budgets(self, run_id, budgets):
        # Shares the budgets_by_run store with bootstrap_budgets so seeded
        # rows are visible to later reads, exactly like durable state.
        if getattr(self, "seed_error", None):
            raise self.seed_error
        mapping = self.__dict__.setdefault("budgets_by_run", {})
        entry = mapping.setdefault(run_id, {"run": None, "budgets": []})
        have = {row["endpoint"] for row in entry["budgets"]}
        inserted = []
        for item in budgets:
            if item["endpoint"] not in have:
                entry["budgets"].append({
                    "endpoint": item["endpoint"], "cap": item["cap"],
                    "consumed": 0,
                    "deadline_at": datetime.fromisoformat(
                        item["deadline_at"])})
                inserted.append(item["endpoint"])
        return {"inserted": inserted,
                "budgets": [dict(row) for row in entry["budgets"]]}

    def archive_usage(self):
        # A healthy catalogue reports integer bytes/objects; tests override
        # archive_usage_data for missing/malformed/negative evidence.
        return getattr(self, "archive_usage_data", (0, 0))

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
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        header = step9.cmd_start(arguments, db)
    _pin_resources(run_dir)
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
                           return_value=None), _healthy_start_hosts():
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
        _wire_budget_fixture(db, cohort)
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
            header = step9.cmd_start(_start_args(
                tmp_path / "b2", cohort,
                deployed_receipt=str(tmp_path / "receipt.json"),
                archive_retained_cap_bytes=None, transfer_cap_bytes=None,
                s3_cap_attempts=None, spool_allocated_cap_bytes=None,
                **base), db)
        assert header["budget_receipt"]["run_id"] == "budget1"


def test_start_budget_binding_before_admission(tmp_path: Path) -> None:
    """Pre-traffic binding: bootstrap provenance plus budget rows.

    The bootstrap selector pins immutable cohort provenance only and is
    never compared to the receipt budget run; the separate budget selector
    binds the receipt to freshly seeded endpoint-budget rows.
    """
    live = {"mode": "live-day", "core_start": "2026-10-04T05:00:00Z",
            "core_end": "2026-10-05T05:00:00Z"}

    def _begin(name, *, receipt_fields=None, db_setup=None,
               start_overrides=None, **mode_over):
        cohort = _write_cohort(tmp_path / f"{name}-cohort.txt", TAGS)
        fields = {"budget_run_id": "budget1",
                  "budget_deadline_at": "2026-10-05T06:00:00+00:00"}
        fields.update(receipt_fields or {})
        receipt_path = tmp_path / f"{name}-receipt.json"
        receipt_path.write_text(json.dumps(_receipt_scope(
            admission_run_id=name.replace("-", ""), **fields)))
        db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
        if db_setup is None:
            _wire_budget_fixture(db, cohort)
        else:
            db_setup(db, cohort)
        arguments = _start_args(
            tmp_path / name, cohort, run_id=name.replace("-", ""),
            deployed_receipt=str(receipt_path), **{**live, **mode_over},
            **(start_overrides or {}))
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
            return step9.cmd_start(arguments, db)

    def _fails(name, code, **kwargs):
        with pytest.raises(step9.Step9Error) as error:
            _begin(name, **kwargs)
        assert error.value.code == code
        return error

    # Budget selector mismatch is refused before run creation/traffic.
    _fails("crossbind", "budget_binding_mismatch",
           receipt_fields={"budget_run_id": "issue92-phase4-20260909T1600",
                           "budget_deadline_at": "2026-09-09T17:00:00+00:00"},
           start_overrides={"budget_run_id": "budgetX"})
    assert not (tmp_path / "crossbind").exists()
    _fails("nosel", "budget_selector_missing",
           start_overrides={"budget_run_id": None})
    assert not (tmp_path / "nosel").exists()
    _fails("noboot", "budget_bootstrap_missing",
           start_overrides={"bootstrap_run_id": None})
    _fails("notables", "budget_store_missing",
           db_setup=lambda db, cohort: None)
    _fails("norow", "budget_run_missing",
           db_setup=_wire_no_row)
    _fails("grant", "budget_grant_denied",
           db_setup=_wire_grant_denied)
    _fails("negcap", "budget_malformed",
           db_setup=lambda db, cohort: _wire_mutated(
               db, cohort, cap0=-1))
    _fails("overused", "budget_evidence_mismatch",
           db_setup=lambda db, cohort: _wire_mutated(
               db, cohort, consumed0=13501))
    _fails("endpoints", "budget_binding_mismatch",
           db_setup=lambda db, cohort: _wire_dropped_endpoint(db, cohort))
    _fails("baddeadline", "budget_deadline_malformed",
           receipt_fields={"budget_deadline_at": "soon"})
    _fails("expired", "budget_deadline_expired",
           receipt_fields={"budget_deadline_at": "2026-09-01T00:00:00+00:00"},
           db_setup=lambda db, cohort: _wire_budget_fixture(
               db, cohort, deadline_at="2026-09-01T00:00:00+00:00"))
    _fails("short", "budget_deadline_short",
           receipt_fields={"budget_deadline_at": "2026-10-04T06:00:00+00:00"},
           db_setup=lambda db, cohort: _wire_budget_fixture(
               db, cohort, deadline_at="2026-10-04T06:00:00+00:00"))
    _fails("mixeddeadline", "budget_binding_mismatch",
           db_setup=lambda db, cohort: _wire_mixed_deadline(db, cohort))
    _fails("caps", "budget_binding_mismatch",
           receipt_fields={"budget_caps": (100, 1, 0)})
    _fails("count", "budget_manifest_mismatch",
           db_setup=lambda db, cohort: _wire_mutated(
               db, cohort, count_bump=1))
    _fails("raw", "budget_manifest_mismatch",
           db_setup=lambda db, cohort: _wire_mutated(
               db, cohort, raw="ff" * 32))
    _fails("canonical", "budget_manifest_mismatch",
           db_setup=lambda db, cohort: _wire_mutated(
               db, cohort, canonical="ee" * 32))
    header = _begin("boundlive")
    assert header["budget_receipt"]["run_id"] == "budget1"
    assert header["bootstrap_run_id"] == "boot1"
    assert header["budget_run_id"] == "budget1"
    assert (tmp_path / "boundlive" / "run.json").is_file()
    header = _begin("boundpre", mode="preflight",
                    core_end="2026-10-04T06:15:00Z",
                    start_overrides={"archive_retained_cap_bytes": None,
                                     "transfer_cap_bytes": None,
                                     "s3_cap_attempts": None,
                                     "spool_allocated_cap_bytes": None})
    assert header["budget_receipt"]["run_id"] == "budget1"


def test_seed_budget_insert_verify_conflict(tmp_path: Path, capsys) -> None:
    """Pre-admission seeding: insert-once, verify, never mutate."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])

    def _args(**overrides):
        base = {"budget_run_id": "budget9", "budget_cap_profile": 100,
                "budget_cap_global_rankings": 2, "budget_cap_battle_log": 0,
                "budget_deadline_at": "2026-10-05T06:00:00+00:00"}
        base.update(overrides)
        return mock.Mock(**base)

    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(), None)
    assert error.value.code == "database_required"
    assert step9.cmd_seed_budget(_args(), db) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["run_id"] == "budget9"
    assert sorted(first["inserted"]) == [
        "battle_log", "global_player_rankings", "profile"]
    assert step9.cmd_seed_budget(_args(), db) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["inserted"] == []
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(budget_cap_profile=101), db)
    assert error.value.code == "budget_binding_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(budget_run_id="has space"), db)
    assert error.value.code == "budget_malformed"
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(budget_cap_battle_log=-1), db)
    assert error.value.code == "budget_malformed"
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(budget_deadline_at="soon"), db)
    assert error.value.code == "budget_deadline_malformed"
    denied = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    refused = RuntimeError("permission denied")
    refused.sqlstate = "42501"
    denied.seed_error = refused
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(), denied)
    assert error.value.code == "budget_grant_denied"
    missing = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    absent = RuntimeError("no such table")
    absent.sqlstate = "42P01"
    missing.seed_error = absent
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_seed_budget(_args(), missing)
    assert error.value.code == "budget_store_missing"


def test_seed_budget_then_start_binds_without_traffic(tmp_path: Path) -> None:
    """Seeded rows resolve the pre-admission chicken-and-egg."""
    cohort = _write_cohort(tmp_path / "seedstart-cohort.txt", TAGS)
    receipt_path = tmp_path / "seedstart-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope(
        admission_run_id="seedstart",
        budget_run_id="budget7",
        budget_deadline_at="2026-10-05T06:00:00+00:00",
        budget_caps=(50, 1, 0))))
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    _wire_budget_fixture(db, cohort)
    seed_args = mock.Mock(
        budget_run_id="budget7", budget_cap_profile=50,
        budget_cap_global_rankings=1, budget_cap_battle_log=0,
        budget_deadline_at="2026-10-05T06:00:00+00:00")
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(
                tmp_path / "seedstart", cohort, run_id="seedstart",
                deployed_receipt=str(receipt_path),
                bootstrap_run_id="boot1", budget_run_id="budget7"), db)
        assert error.value.code == "budget_binding_mismatch"
        assert not (tmp_path / "seedstart").exists()
        assert step9.cmd_seed_budget(seed_args, db) == 0
        header = step9.cmd_start(_start_args(
            tmp_path / "seedstart", cohort, run_id="seedstart",
            deployed_receipt=str(receipt_path),
            bootstrap_run_id="boot1", budget_run_id="budget7"), db)
    assert header["budget_run_id"] == "budget7"


def test_start_refuses_unseeded_budget_without_traffic(tmp_path: Path) -> None:
    """No seeded rows means no admission: the lazy chicken-and-egg stays shut."""
    cohort = _write_cohort(tmp_path / "seedstart-cohort.txt", TAGS)
    receipt_path = tmp_path / "seedstart-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope(
        admission_run_id="seedstart",
        budget_run_id="budget7",
        budget_deadline_at="2026-10-05T06:00:00+00:00",
        budget_caps=(50, 1, 0))))
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(_start_args(
                tmp_path / "seedstart", cohort, run_id="seedstart",
                deployed_receipt=str(receipt_path),
                bootstrap_run_id="boot1", budget_run_id="budget7"), db)
        assert error.value.code == "budget_binding_mismatch"
        assert not (tmp_path / "seedstart").exists()


def test_parse_runtime_metrics_wire_format() -> None:
    text = ("# HELP clashlens_collector_jobs_total jobs\n"
            "# TYPE clashlens_collector_jobs_total counter\n"
            'clashlens_collector_process_identity_info{process_id="abc123"} 1\n'
            "clashlens_collector_process_start_time_seconds 1700000000\n"
            'clashlens_collector_jobs_total{work_type="regular_poll",'
            'pool="normal",outcome="admitted"} 42\n'
            "clashlens_collector_database_pool_idle_connections 3\n"
            "clashlens_spool_final_bytes 123\n"
            'clashlens_spool_inode_model_info{filesystem_type="btrfs",model="dynamic"} 1\n'
            "unrelated_metric 7\n")
    parsed = step9.parse_runtime_metrics(text)
    assert parsed["process_id"] == "abc123"
    assert parsed["started_at"] == 1700000000
    key = ('clashlens_collector_jobs_total{outcome=admitted,pool=normal,'
           'work_type=regular_poll}')
    assert parsed["counters"][key] == 42
    assert ("clashlens_collector_database_pool_idle_connections{}" in
            parsed["counters"])
    assert parsed["counters"]["clashlens_spool_final_bytes{}"] == 123
    assert parsed["counters"][
        "clashlens_spool_inode_model_info{filesystem_type=btrfs,model=dynamic}"] == 1
    assert "unrelated_metric" not in str(parsed["counters"])
    with pytest.raises(step9.Step9Error):
        step9.parse_runtime_metrics("clashlens_collector_jobs_total 1\n")
    with pytest.raises(step9.Step9Error):
        step9.parse_runtime_metrics("bogus line here\n")
    # gauges may fall without tripping counter-reset
    spool_gauges = {
        "clashlens_spool_abandoned_temporary_bytes",
        "clashlens_spool_abandoned_temporary_objects",
        "clashlens_spool_final_bytes",
        "clashlens_spool_final_objects",
        "clashlens_spool_free_bytes",
        "clashlens_spool_free_inodes",
        "clashlens_spool_high_water_bytes",
        "clashlens_spool_live_reservations",
        "clashlens_spool_reserved_bytes",
        "clashlens_spool_temporary_bytes",
        "clashlens_spool_temporary_objects",
    }
    assert spool_gauges < step9._RUNTIME_GAUGES
    for gauge in spool_gauges:
        assert step9._check_counters_decreased(
            {f"{gauge}{{}}": 5}, {f"{gauge}{{}}": 2}) is None
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
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           side_effect=_validate), _healthy_start_hosts():
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


def _good_probe(run: dict, nbytes: int = 100) -> dict:
    """Test-only: a fully valid v2 allocated probe tied to the run pins."""
    spool = run["spool_path"]
    resolved = os.path.normpath(spool)
    pin = ((run.get("filesystem") or {}).get("spool") or {})
    mount = {key: pin.get(key) for key in
             ("mount_point", "source", "mnt_id", "filesystem_type")}
    return {
        "schema": step9.RESOURCE_EVIDENCE_SCHEMA,
        "definition": "gnu-du-allocated-bytes-one-filesystem",
        "configured_path": spool, "path": resolved,
        "resolved_path": resolved,
        "command": ["du", "--block-size=1", "--summarize",
                    "--one-file-system", "--no-dereference", "--",
                    resolved],
        "mount_identity": dict(mount), "mount_before": dict(mount),
        "mount_after": dict(mount), "allocated_bytes": nbytes,
        "started_at": "2026-10-04T05:00:00+00:00",
        "finished_at": "2026-10-04T05:00:01+00:00",
        "timeout_seconds": 5, "error": None,
        "btrfs_limitation": ("summed allocated blocks are not exclusive "
                              "shared-pool ownership")}


def _quiet_facts_with_probe(run: dict) -> dict:
    """Test-only: quiet loop facts carrying a fully valid v2 probe."""
    facts = _quiet_facts()
    facts["archive"] = {**facts["archive"],
                        "allocated_probe": _good_probe(run)}
    return facts


def _wire_budget_fixture(db: FakeDB, cohort: Path, *, bootstrap_id: str = "boot1",
                         budget_id: str = "budget1",
                         deadline_at: str = "2026-10-05T06:00:00+00:00",
                         caps: tuple = (13500, 1, 0)) -> None:
    """Test-only: bootstrap provenance plus fresh budget rows.

    The bootstrap entry pins immutable cohort provenance; the separate
    budget entry carries newly seeded endpoint rows. IDs stay distinct to
    prove the two identities are never conflated.
    """
    _tags, raw, canonical = step9._read_cohort(str(cohort))
    db.budgets_tables = True
    db.budgets_by_run = {
        bootstrap_id: {
            "run": {"run_id": bootstrap_id, "manifest_sha256": raw,
                     "manifest_count": len(_tags),
                     "normalized_set_sha256": canonical,
                     "status": "complete"},
            "budgets": []},
        budget_id: {
            "run": None,
            "budgets": [{"endpoint": name, "cap": cap, "consumed": cap,
                           "deadline_at": datetime.fromisoformat(deadline_at)}
                          for name, cap in zip(
                              ("profile", "global_player_rankings",
                               "battle_log"), caps)]}}


def _wire_no_row(db: FakeDB, cohort: Path) -> None:
    _wire_budget_fixture(db, cohort)
    db.budgets_by_run["boot1"]["run"] = None


def _wire_grant_denied(db: FakeDB, cohort: Path) -> None:
    db.budgets_tables = True
    denied = RuntimeError("permission denied")
    denied.sqlstate = "42501"
    db.budgets_error = denied


def _wire_mutated(db: FakeDB, cohort: Path, *, cap0=None, consumed0=None,
                  count_bump=0, raw=None, canonical=None) -> None:
    _wire_budget_fixture(db, cohort)
    row = db.budgets_by_run["budget1"]["budgets"][0]
    if cap0 is not None:
        row["cap"] = cap0
    if consumed0 is not None:
        row["consumed"] = consumed0
    if count_bump:
        db.budgets_by_run["boot1"]["run"]["manifest_count"] += count_bump
    if raw is not None:
        db.budgets_by_run["boot1"]["run"]["manifest_sha256"] = raw
    if canonical is not None:
        db.budgets_by_run["boot1"]["run"]["normalized_set_sha256"] = canonical


def _wire_dropped_endpoint(db: FakeDB, cohort: Path) -> None:
    _wire_budget_fixture(db, cohort)
    db.budgets_by_run["budget1"]["budgets"] = \
        db.budgets_by_run["budget1"]["budgets"][:2]


def _wire_mixed_deadline(db: FakeDB, cohort: Path) -> None:
    _wire_budget_fixture(db, cohort)
    db.budgets_by_run["budget1"]["budgets"][0]["deadline_at"] = datetime(
        2026, 10, 6, 6, 0, tzinfo=UTC)


def _present_wire_baseline(**kwargs):
    """Test-only: present archive-interface baseline matching sample hooks."""
    return {"status": "captured", "failure_code": None,
            "boot_id": step9._boot_id(),
            "interfaces": {"test-eth0": {
                "present": True, "rx_bytes": 1000, "tx_bytes": 500,
                "mac": "aa:bb:cc:dd:ee:ff", "operstate": "up"}}}


@contextlib.contextmanager
def _healthy_start_hosts():
    """Test-only: stopped-but-pinned collector plus present wire facts."""
    with mock.patch.object(step9, "collect_wire_facts",
                           side_effect=lambda **kwargs:
                           _present_wire_baseline()), \
         mock.patch.object(step9.Podman, "inspect_running",
                           lambda self, container: (False, "sha256:image")):
        yield


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


def _unpin_image(run_dir: Path) -> None:
    """Test-only: drop the image pin to simulate a legacy unpinned run."""
    path = run_dir / "run.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["containers"]["collector_image"] = None
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
                "max_sample_age_seconds": 125, "systemd_unit": "test-unit",
                "poll_seconds": 30, "stop_grace_seconds": 30}
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
    # past grace with inactive sampler unit -> safety return at once with
    # durable evidence and NO collector-only stop grace/inspect round trip
    # (fresh dir: watchdog.json is exclusive and never replaced). The
    # parent group-stop hook owns the immediate whole-group TERM.
    run_dir2, _header2 = _started_run(tmp_path, db, run_dir_name="run2",
                                        run_id="testrun02")
    _pin_image(run_dir2)
    arguments2 = _watchdog_args(run_dir2)
    stopped = len(podman.commands)
    hooks = {"podman_run": podman, "single_pass": True, "max_iterations": 1,
             "no_sleep": True,
             "sampler_check": lambda run: False,
             "now_utc": lambda: datetime(2026, 10, 4, 6, 0, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments2, hooks) == 1
    assert "stop" not in [command[1] for command in podman.commands[stopped:]]
    assert list((run_dir2 / "failures").glob(
        "sampler_unit_inactive-*.json"))
    outcome = json.loads((run_dir2 / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "sampler_unit_inactive"
    assert outcome["stop"] == "delegated_to_group_stop"
    # planned core-end deadline still performs the graceful stop itself
    # and succeeds, so the driver can reach the monitored drain.
    run_dir3, _header3 = _started_run(tmp_path, db, run_dir_name="run3",
                                        run_id="testrun03")
    _pin_image(run_dir3)
    arguments3 = _watchdog_args(run_dir3,
                                deadline="2026-10-04T05:10:00Z")
    hooks = {"podman_run": podman, "single_pass": True, "max_iterations": 1,
             "no_sleep": True,
             "now_utc": lambda: datetime(2026, 10, 4, 6, 0, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments3, hooks) == 0
    assert ["podman", "stop", "--ignore", "--time", "30",
            "test-collector"] in podman.commands
    outcome = json.loads((run_dir3 / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "deadline_reached"
    assert not list((run_dir3 / "failures").glob("deadline_reached-*.json"))


def test_watchdog_unpinned_image_fails_closed(tmp_path: Path) -> None:
    """N2: missing start-time image pin fails the watchdog, never skips."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    _unpin_image(run_dir)
    podman = FakePodman()
    arguments = _watchdog_args(run_dir)
    assert step9.cmd_watchdog(arguments, {"podman_run": podman,
                                           "no_sleep": True}) == 2
    assert list((run_dir / "failures").glob("unpinned_image-*.json"))
    verbs = [command[1] for command in podman.commands]
    assert "update" not in verbs and "stop" not in verbs


def _started_stopped_collector(tmp_path: Path, db: FakeDB,
                               image: str = "sha256:image",
                               run_dir_name: str = "run"):
    """Test-only: start while the collector is stopped but inspectable."""
    cohort = _write_cohort(tmp_path / "cohort.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    run_id = "testrun01"
    receipt_path.write_text(json.dumps(_receipt_scope(admission_run_id=run_id)))
    run_dir = tmp_path / run_dir_name
    arguments = _start_args(run_dir, cohort,
                            deployed_receipt=str(receipt_path))
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts(), \
            mock.patch.object(step9.Podman, "inspect_running",
                              lambda self, container: (False, image)):
        header = step9.cmd_start(arguments, db)
    _pin_resources(run_dir)
    return run_dir, header


def test_start_pins_stopped_collector_image(tmp_path: Path) -> None:
    """A stopped-but-inspectable collector still pins its image."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_stopped_collector(tmp_path, db)
    assert header["containers"]["collector_image"] == "sha256:image"
    assert header["containers"]["collector_image_error"] is None
    assert (run_dir / "run.json").is_file()


def test_start_empty_collector_image_fails_closed(tmp_path: Path) -> None:
    """Ambiguous inspection fails closed before admission, pinning nothing."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    cohort = _write_cohort(tmp_path / "cohort.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    run_dir = tmp_path / "run"
    arguments = _start_args(run_dir, cohort,
                            deployed_receipt=str(receipt_path))
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), \
            mock.patch.object(step9.Podman, "inspect_running",
                              lambda self, container: (False, "")):
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(arguments, db)
        assert error.value.code == "collector_inspect_invalid"
    assert not run_dir.exists()


def test_watchdog_accepts_stopped_start_pin_once_running(tmp_path: Path) -> None:
    """Stopped-at-start pin verifies once the same image runs."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_stopped_collector(tmp_path, db)
    podman = FakePodman()
    arguments = _watchdog_args(run_dir)
    hooks = {"podman_run": podman, "single_pass": True, "max_iterations": 1,
             "no_sleep": True,
             "now_utc": lambda: datetime(2026, 10, 4, 5, 1, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments, hooks) == 0
    assert (run_dir / "watchdog.json").is_file()


def test_watchdog_rejects_changed_image_after_stopped_start(tmp_path: Path) -> None:
    """A different running image never matches the stopped start pin."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_stopped_collector(tmp_path, db)

    def podman_run(command):
        if command[1:3] == ["container", "inspect"]:
            return "true\nsha256:other\n"
        raise AssertionError(f"unexpected podman command: {command}")

    arguments = _watchdog_args(run_dir)
    assert step9.cmd_watchdog(arguments, {"podman_run": podman_run,
                                           "no_sleep": True}) == 1
    assert list((run_dir / "failures").glob("container_image_changed-*.json"))


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
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        run = step9.cmd_start(arguments, db)
    _pin_resources(run_dir)
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
        sample["resources"] = {
            "failures": [], "unknown": [],
            "allocated_probe": _good_probe(run),
            "archive_retained": {
                "baseline_bytes": 100, "current_bytes": 100 + index,
                "newly_retained_bytes": index,
                "cap_bytes": run["archive_retained_cap_bytes"]}}
        sample["wire"] = {"failures": [], "unknown": [],
                          "conservative_host_wire_bytes": 1000}
        sample["s3"] = {"go": {}, "go_total": 0, "python": {}, "python_total": 0,
                        "total": 0, "error": None,
                        "producers": _sealed_producers(run)}
        sample["s3_attempts_cumulative"] = 21
        step9._exclusive_json(samples / f"minute-{index:04d}.json", sample)
    if run.get("schema") == step9.SCHEMA_LIVE:
        _seal_terminal_chain(run_dir, run, mode["slots"], db)
    return run_dir, run


def _sealed_producers(run: dict) -> list:
    """Test-only: core-observed identities matching the terminal fixtures."""
    try:
        replicas = int((run.get("containers", {}) or {}).get(
            "worker_replicas", 0) or 0) or 1
    except (TypeError, ValueError):
        replicas = 1
    producers = [{
        "producer": "collector", "replica": None,
        "process_id": "test-collector-pid",
        "process_started_at": run["core_start"], "terminal": False,
    }]
    for replica in range(1, replicas + 1):
        producers.append({
            "producer": "worker", "replica": replica,
            "process_id": f"test-worker-{replica}",
            "process_started_at": run["core_start"], "terminal": False,
        })
    return producers


def _seal_terminal_chain(run_dir: Path, run: dict, slots: int,
                         db: FakeDB) -> None:
    """Test-only: consistent drain/terminal/post-stop fixtures.

    Mirrors a quiesced production run with zero drain delta: the drain
    record repeats the last core sample, terminal snapshots carry matching
    zero totals, and the pinned post-stop capture repeats both. Tests for
    positive drain deltas overwrite these fixtures explicitly.
    """
    last = json.loads(
        (run_dir / "samples" / f"minute-{slots - 1:04d}.json").read_text())
    wire = last["wire"]["conservative_host_wire_bytes"]
    retained = last["resources"]["archive_retained"]["current_bytes"]
    replicas = int((run.get("containers", {}) or {}).get(
        "worker_replicas", 0) or 0) or 1
    spool = Path(run["spool_path"])
    terminal_dir = spool / ".control" / "terminal"
    terminal_dir.mkdir(parents=True, exist_ok=True)
    captured = run["core_end"]
    (terminal_dir / "collector.json").write_text(json.dumps({
        "schema": step9.TERMINAL_GO_SCHEMA, "producer": "collector",
        "process_id": "test-collector-pid",
        "process_started_at": run["core_start"],
        "captured_at": captured, "terminal": True, "operations": {},
    }), encoding="utf-8")
    for replica in range(1, replicas + 1):
        (terminal_dir / f"worker-{replica}.json").write_text(json.dumps({
            "schema": step9.TERMINAL_WORKER_SCHEMA, "producer": "worker",
            "process": {"id": f"test-worker-{replica}",
                         "started_at": run["core_start"]},
            "captured_at": captured, "terminal": True,
            "archive": {"remote_attempts": {}},
        }), encoding="utf-8")
    (run_dir / "drain-monitor.json").write_text(json.dumps({
        "schema": run.get("schema"), "run_id": run["run_id"],
        "drain_pid": 99999, "started_at": captured,
        "finished_at": captured, "poll_seconds": 5,
        "timeout_seconds": 300, "polls": 1, "outcome": "drained",
        "detail": None,
        "observations": {
            "wire_total": wire, "s3_total": 0,
            "s3_prior_attempts": 21, "retained_bytes": retained,
            "spool_allocated_bytes": 100,
        },
    }), encoding="utf-8")
    db.archive_usage_data = (retained, 2)
    _pin_live_wire(run_dir)


def _pin_live_wire(run_dir: Path) -> None:
    """Test-only: re-pin the wire baseline to this host's loopback.

    Sealed runs start against the canned test-eth0 baseline, but the
    post-stop capture observes the live host through the read-only path.
    Re-pinning to loopback keeps the sealed world self-consistent so
    live observation is meaningful: loopback counters only advance and
    interface identity is stable, so totals stay deterministic.
    """
    live = step9.collect_wire_facts(interfaces=["lo"], route_host=None)
    live["boot_id"] = step9._boot_id()
    path = run_dir / "run.json"
    payload = json.loads(path.read_text())
    payload["wire_baseline"] = live
    payload["archive_interfaces"] = ["lo"]
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


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
            "resource_facts": lambda run, db,
            metrics: _quiet_facts_with_probe(run),
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
                                core_end="2026-10-04T06:15:00Z",
                                archive_retained_cap_bytes=None,
                                transfer_cap_bytes=None, s3_cap_attempts=None,
                                spool_allocated_cap_bytes=None)
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
            _seed_bootstrap(cohort, info)
            with _healthy_start_hosts():
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


def _seed_bootstrap(cohort: Path, info: str, *, bootstrap_id: str = "boot1",
                    budget_id: str = "budget1",
                    deadline_at: str = "2026-10-05T06:00:00+00:00") -> None:
    """Test-only: bootstrap provenance plus fresh budget rows.

    IDs stay distinct: the bootstrap row pins immutable cohort provenance
    while the separate budget rows carry the receipt-bound endpoint caps.
    """
    import psycopg

    _tags, raw, canonical = step9._read_cohort(str(cohort))
    with psycopg.connect(info, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO population_bootstrap_runs"
            " (run_id, manifest_sha256, manifest_count,"
            " normalized_set_sha256, status, batch_size, completed_at)"
            " VALUES (%s, %s, %s, %s, 'complete', 500, now())",
            (bootstrap_id, raw, len(_tags), canonical))
        for endpoint, cap in (("profile", 13500),
                              ("global_player_rankings", 1),
                              ("battle_log", 0)):
            connection.execute(
                "INSERT INTO collector_endpoint_budgets"
                " (run_id, endpoint, cap, consumed, deadline_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (budget_id, endpoint, cap, cap, deadline_at))


def test_budget_binding_completed_bootstrap_seeded_budget() -> None:
    """PG18: completed bootstrap plus distinct seeded budget admits a start.

    The budget rows are seeded first (as the driver preseed does) under a
    budget ID that intentionally has no bootstrap row of its own; start
    binds them without re-bootstrapping the cohort. Env-gated like the
    other real-DB contracts: parent runs this on PG18.
    """
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        cohort = Path(tempfile.mkdtemp(prefix="step9-bind-")) / "cohort.txt"
        _write_cohort(cohort, TAGS)
        with psycopg.connect(info) as connection:
            _seed_player(connection, TAGS[0])
            connection.commit()
        _seed_bootstrap(cohort, info, bootstrap_id="pgboot1",
                        budget_id="pgbudget1")
        # Fresh budget IDs carry endpoint rows but no bootstrap row.
        data = database.bootstrap_budgets("pgbudget1")
        assert data["run"] is None
        assert {b["endpoint"] for b in data["budgets"]} == {
            "profile", "global_player_rankings", "battle_log"}
        run_dir = cohort.parent / "run"
        receipt_path = cohort.parent / "receipt.json"
        receipt_path.write_text(json.dumps(_receipt_scope(
            admission_run_id="pgbind1", budget_run_id="pgbudget1",
            budget_deadline_at="2026-10-05T06:00:00+00:00")))
        arguments = _start_args(
            run_dir, cohort, run_id="pgbind1",
            deployed_receipt=str(receipt_path),
            bootstrap_run_id="pgboot1", budget_run_id="pgbudget1",
            database_url=info, mode="preflight",
            core_start="2026-10-04T05:00:00Z",
            core_end="2026-10-04T06:15:00Z",
            archive_retained_cap_bytes=None, transfer_cap_bytes=None,
            s3_cap_attempts=None, spool_allocated_cap_bytes=None)
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
            header = step9.cmd_start(arguments, database)
        assert header["bootstrap_run_id"] == "pgboot1"
        assert header["budget_run_id"] == "pgbudget1"
        assert header["budget_receipt"]["run_id"] == "pgbudget1"


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
        _seed_bootstrap(cohort, info)
        kill_receipt = base / "receipt.json"
        kill_receipt.write_text(
            json.dumps(_receipt_scope(admission_run_id="killed1")))
        deadline_receipt = base / "deadline-receipt.json"
        deadline_receipt.write_text(
            json.dumps(_receipt_scope(admission_run_id="deadline1")))
        run_dir = base / "run-killed"
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
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
        # Planned deadline on a fresh run: exact stop command succeeds.
        run_dir2 = base / "run-deadline"
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
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
        }) == 0
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
                            run_id="preflight1",
                            archive_retained_cap_bytes=None,
                            transfer_cap_bytes=None, s3_cap_attempts=None,
                            spool_allocated_cap_bytes=None)
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
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
                           return_value=None), _healthy_start_hosts():
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
                            core_end="2026-10-04T06:15:00Z",
                            archive_retained_cap_bytes=None,
                            transfer_cap_bytes=None, s3_cap_attempts=None,
                            spool_allocated_cap_bytes=None)
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
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
        sample["resources"] = {
            "failures": [], "unknown": [],
            "allocated_probe": _good_probe(run)}
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
    run = {"bootstrap_run_id": "boot1", "budget_run_id": "boot1",
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
    spaced = ("Data,single: Size: 1000, Used: 100\n"
              "Metadata,DUP: Size: 200, Used: 50\n"
              "Unallocated: 5000\n")
    assert step9._parse_btrfs_usage(spaced) == {
        "metadata_pct": 25.0, "unallocated_bytes": 5000}
    actual = ("WARNING: cannot read detailed chunk info, per-device usage will not be shown, run as root\n"
              "Overall:\n"
              "    Device unallocated:              932285775872\n"
              "Data,single: Size:80539025408, Used:78130692096 (97.01%)\n"
              "Metadata,DUP: Size:4294967296, Used:2506653696 (58.36%)\n")
    parsed = step9._parse_btrfs_usage(actual)
    assert parsed["unallocated_bytes"] == 932285775872
    assert round(parsed["metadata_pct"], 2) == 58.36
    combined = "Data+Metadata,single: Size: 4, Used: 1\nUnallocated: 8\n"
    parsed = step9._parse_btrfs_usage(combined)
    assert parsed == {"metadata_pct": None, "unallocated_bytes": 8}
    assert step9._parse_btrfs_usage("Device unallocated: nope\n") == {
        "metadata_pct": None, "unallocated_bytes": None}
    assert step9._parse_btrfs_usage("") == {
        "metadata_pct": None, "unallocated_bytes": None}


def test_spool_allocated_usage_is_independent_of_apparent_bytes(
        tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    sparse = spool / "sparse"
    with sparse.open("wb") as body:
        body.seek(8 * 1024**2 - 1)
        body.write(b"x")
    identity = {"mount_point": "/", "source": "/dev/test", "mnt_id": 7,
                "filesystem_type": "btrfs"}
    result = step9._spool_allocated_usage(
        str(spool), identity, identity_probe=lambda _path: identity)
    assert result["error"] is None
    assert result["allocated_bytes"] < sparse.stat().st_size
    assert result["definition"] == "gnu-du-allocated-bytes-one-filesystem"
    assert result["schema"] == step9.RESOURCE_EVIDENCE_SCHEMA


def test_spool_allocated_usage_fails_closed() -> None:
    identity = {"mount_point": "/", "source": "/dev/test", "mnt_id": 7,
                "filesystem_type": "btrfs"}
    timeout = mock.Mock(side_effect=subprocess.TimeoutExpired("du", 5))
    result = step9._spool_allocated_usage("/tmp", identity,
                                          run_command=timeout)
    assert result["allocated_bytes"] is None
    assert result["error"] == "allocated_probe_timeout"

    completed = mock.Mock(returncode=0, stdout="4096\t/tmp\n", stderr="")
    changed = dict(identity, mnt_id=8)
    result = step9._spool_allocated_usage(
        "/tmp", identity, run_command=lambda *args, **kwargs: completed,
        identity_probe=lambda _path: changed)
    assert result["allocated_bytes"] is None
    assert result["error"] == "allocated_probe_unavailable:ValueError"
    denied = mock.Mock(side_effect=PermissionError("denied"))
    result = step9._spool_allocated_usage("/tmp", identity,
                                          run_command=denied)
    assert result["allocated_bytes"] is None
    assert result["error"].startswith("allocated_probe_unavailable")


def test_spool_allocated_usage_rejects_malformed_or_unbounded_output(
        tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    identity = {"mount_point": "/", "source": "/dev/test", "mnt_id": 7,
                "filesystem_type": "btrfs"}
    malformed = mock.Mock(returncode=0, stdout="4096 /wrong\n", stderr="")
    result = step9._spool_allocated_usage(
        str(spool), identity, run_command=lambda *args, **kwargs: malformed,
        identity_probe=lambda _path: identity)
    assert result["allocated_bytes"] is None
    assert result["error"] == "allocated_probe_unavailable:ValueError"
    oversized = mock.Mock(
        returncode=0, stdout="9\t" + str(spool) + "\n" + "x" * 4096,
        stderr="")
    result = step9._spool_allocated_usage(
        str(spool), identity, run_command=lambda *args, **kwargs: oversized,
        identity_probe=lambda _path: identity)
    assert result["allocated_bytes"] is None
    assert result["error"] == "allocated_probe_unavailable:ValueError"
    unterminated = mock.Mock(returncode=0, stdout=f"4096\t{spool}",
                              stderr="")
    result = step9._spool_allocated_usage(
        str(spool), identity,
        run_command=lambda *args, **kwargs: unterminated,
        identity_probe=lambda _path: identity)
    assert result["allocated_bytes"] is None
    assert result["error"] == "allocated_probe_unavailable:ValueError"


def test_spool_allocated_run_bounded_command_hard_deadline() -> None:
    import time
    completed = step9._run_bounded_command(
        [sys.executable, "-c", "print('hi')"], timeout=5,
        max_output_bytes=4096)
    assert completed.returncode == 0
    assert completed.stdout == "hi\n" and completed.stderr == ""
    with pytest.raises(ValueError):
        step9._run_bounded_command(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*5000)"],
            timeout=5, max_output_bytes=4096)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        step9._run_bounded_command(
            [sys.executable, "-c", "import time;time.sleep(30)"],
            timeout=1, max_output_bytes=4096)
    elapsed = time.monotonic() - started
    assert 0.6 <= elapsed < 2.0


def test_spool_allocated_usage_rejects_probe_failures(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    identity = {"mount_point": "/", "source": "/dev/test", "mnt_id": 7,
                "filesystem_type": "btrfs"}
    good = f"4096\t{spool}\n"
    for bad in (mock.Mock(returncode=1, stdout=good, stderr=""),
                mock.Mock(returncode=0, stdout=good, stderr="du: warning\n"),
                mock.Mock(returncode=0, stdout=good + good, stderr=""),
                mock.Mock(returncode=0, stdout="-1\t" + str(spool) + "\n",
                           stderr="")):
        result = step9._spool_allocated_usage(
            str(spool), identity,
            run_command=lambda *args, _bad=bad, **kwargs: _bad,
            identity_probe=lambda _path: identity)
        assert result["allocated_bytes"] is None
        assert result["error"].startswith("allocated_probe_unavailable")
    link = tmp_path / "link"
    link.symlink_to(spool, target_is_directory=True)
    result = step9._spool_allocated_usage(
        str(link), identity,
        run_command=lambda *a, **k: mock.Mock(
            returncode=0, stdout=f"4096\t{link}\n", stderr=""),
        identity_probe=lambda _path: identity)
    assert result["allocated_bytes"] is None
    # Success records configured/resolved paths and before/after mounts.
    result = step9._spool_allocated_usage(
        str(spool), identity,
        run_command=lambda *a, **k: mock.Mock(
            returncode=0, stdout=good, stderr=""),
        identity_probe=lambda _path: identity)
    assert result["error"] is None and result["allocated_bytes"] == 4096
    assert result["configured_path"] == str(spool)
    assert result["resolved_path"] == str(spool)
    assert result["mount_before"] == identity
    assert result["mount_after"] == identity


def test_spool_allocated_probe_failure_is_unknown_never_zero(
        tmp_path: Path) -> None:
    base_kwargs = {
        "spool_path": str(tmp_path), "postgres_path": str(tmp_path),
        "db": None,
        "btrfs_probe": lambda _path: {"metadata_pct": None,
                                       "unallocated_bytes": None,
                                       "error": None, "stderr": None},
        "device_probe": lambda _path: {"errors": {}, "error": None}}
    failed = step9.collect_resource_facts(
        metrics=None,
        spool_probe=lambda _p, _i: {"schema": step9.RESOURCE_EVIDENCE_SCHEMA,
                                    "allocated_bytes": None,
                                    "error": "allocated_probe_timeout"},
        **base_kwargs)["archive"]
    assert failed["physical_bytes"] is None
    assert failed["allocated_probe"]["error"] == "allocated_probe_timeout"
    _f, unknown, _s = step9.evaluate_resource_gates(
        _quiet_facts(), {**_quiet_facts(), "archive": failed}, 0)
    assert "archive_physical_unknown" in unknown
    breached = {**_quiet_facts(), "archive": {
        "logical_bytes": 100, "objects": 2,
        "physical_bytes": step9.RES_ARCHIVE_PHYSICAL_MAX + 1,
        "error": None}}
    failures, _u, _s = step9.evaluate_resource_gates(
        _quiet_facts(), breached, 0)
    assert "archive_physical_breach" in failures


def test_resource_facts_separate_allocated_and_runtime_ledger_bytes(
        tmp_path: Path) -> None:
    kwargs = {
        "spool_path": str(tmp_path), "postgres_path": str(tmp_path),
        "db": None,
        "btrfs_probe": lambda _path: {"metadata_pct": None,
                                       "unallocated_bytes": None,
                                       "error": None, "stderr": None},
        "device_probe": lambda _path: {"errors": {}, "error": None},
        "spool_probe": lambda _path, _identity: {
            "schema": step9.RESOURCE_EVIDENCE_SCHEMA,
            "allocated_bytes": 4096, "error": None}}
    no_metrics = step9.collect_resource_facts(metrics=None, **kwargs)["archive"]
    assert no_metrics["physical_bytes"] == 4096
    facts = step9.collect_resource_facts(
        metrics={"counters": {"clashlens_spool_final_bytes": 999}}, **kwargs)
    archive = facts["archive"]
    assert archive["physical_bytes"] == 4096
    assert archive["runtime_ledger_final_bytes"] == 999


def test_spool_allocated_close_pipes_then_sleep_times_out() -> None:
    import time
    # Pipe-close race: drains see EOF immediately, but the process still
    # sleeps past the deadline; the total wall clock (wait+kill+reap) must
    # stay bounded instead of hanging on wait after drained pipes.
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        step9._run_bounded_command(
            [sys.executable, "-c",
             ("import sys,time;sys.stdout.close();sys.stderr.close();"
              "time.sleep(30)")],
            timeout=1, max_output_bytes=4096)
    elapsed = time.monotonic() - started
    assert 0.6 <= elapsed < 2.0


def test_sample_resources_retains_allocated_probe_after_stop(
        tmp_path: Path) -> None:
    # After a proven collector stop metrics=None must not lose the host du
    # evidence: success and gate-failure samples both keep the bounded probe.
    probe = {"schema": step9.RESOURCE_EVIDENCE_SCHEMA,
             "definition": "gnu-du-allocated-bytes-one-filesystem",
             "allocated_bytes": 4096, "error": None}

    def _facts_with_probe(metrics):
        base = _quiet_facts()
        base["archive"] = {"logical_bytes": 100, "objects": 2,
                             "physical_bytes": 4096, "error": None,
                             "allocated_probe": dict(probe)}
        return base

    def no_metrics(url):
        raise step9.Step9Error("metrics_unavailable", "collector stopped")

    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db, run_dir_name="retain")
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    hooks = _sample_hooks(db, fetch_metrics=no_metrics, max_slots=1,
                          single_pass=True)
    seen: dict = {}

    def fake_facts(run, db, metrics):
        seen["metrics"] = metrics
        return _facts_with_probe(metrics)

    hooks["resource_facts"] = fake_facts
    assert step9.cmd_sample(arguments, hooks) == 0
    assert seen["metrics"] is None
    sample = json.loads((run_dir / "samples" / "minute-0000.json"
                         ).read_text())
    assert sample["metrics"] is None
    assert sample["metrics_error"] == "metrics_unavailable"
    kept = sample["resources"]["allocated_probe"]
    assert kept["allocated_bytes"] == 4096
    assert kept["schema"] == step9.RESOURCE_EVIDENCE_SCHEMA
    assert kept["definition"] == "gnu-du-allocated-bytes-one-filesystem"
    assert sample["resources"]["failures"] == []
    # Gate failure still retains the same bounded probe (never zeroed).
    db2 = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir2, _h2 = _started_run(tmp_path, db2, run_dir_name="retainbad")
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    hooks2 = _sample_hooks(db2, fetch_metrics=no_metrics, max_slots=1,
                           single_pass=True)

    def fake_breach(run, db, metrics):
        facts = _facts_with_probe(metrics)
        facts["filesystems"]["pool"]["use_pct"] = 95.0
        return facts

    hooks2["resource_facts"] = fake_breach
    assert step9.cmd_sample(arguments2, hooks2) == 1
    bad = json.loads((run_dir2 / "samples" / "minute-0000.json"
                      ).read_text())
    assert bad["resources"]["allocated_probe"]["allocated_bytes"] == 4096
    assert bad["resources"]["failures"] == ["filesystem_use_breach"]


def test_validate_v2_requires_allocated_probe_and_v1_readable(
        tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "v2probe", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    # v2 without the new probe schema/source is not valid, even with a clean
    # manifest (missing probe must fail closed, never silently gain v1 meaning).
    victim = run_dir / "samples" / "minute-0007.json"
    sample = json.loads(victim.read_text())
    sample["resources"] = {"failures": [], "unknown": []}
    victim.write_text(json.dumps(sample))
    (run_dir / "manifest.json").unlink()
    step9._write_manifest(run_dir)
    assert step9.cmd_validate(arguments) == 1
    sample["resources"] = {
        "failures": [], "unknown": [],
        "allocated_probe": {"schema": step9.SCHEMA_LIVE_V1,
                              "definition": "ledger", "allocated_bytes": 1,
                              "error": None}}
    victim.write_text(json.dumps(sample))
    (run_dir / "manifest.json").unlink()
    step9._write_manifest(run_dir)
    assert step9.cmd_validate(arguments) == 1
    # v1 historical evidence stays readable but never mixes with v2 samples.
    run_path = run_dir / "run.json"
    payload = json.loads(run_path.read_text())
    payload["schema"] = step9.SCHEMA_LIVE_V1
    run_path.write_text(json.dumps(payload))
    for path in (run_dir / "samples").glob("minute-*.json"):
        current = json.loads(path.read_text())
        current["schema"] = step9.SCHEMA_LIVE_V1
        current.pop("resources", None)
        path.write_text(json.dumps(current))
    samples = step9._read_samples(run_dir)
    assert len(samples) == 1440
    assert all(entry["schema"] == step9.SCHEMA_LIVE_V1 for entry in samples)
    mixed = run_dir / "samples" / "minute-0000.json"
    entry = json.loads(mixed.read_text())
    entry["schema"] = step9.SCHEMA_LIVE
    mixed.write_text(json.dumps(entry))
    with pytest.raises(step9.Step9Error) as error:
        step9._read_samples(run_dir)
    assert error.value.code == "sample_mismatch"


def test_sample_v1_run_refuses_v2_minute(tmp_path: Path) -> None:
    # A v1 run stays readable, but the v2 sampler must refuse before
    # writing any v2 minute sample into it.
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    run_path = run_dir / "run.json"
    payload = json.loads(run_path.read_text())
    payload["schema"] = step9.SCHEMA_LIVE_V1
    run_path.write_text(json.dumps(payload))
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    with pytest.raises(step9.Step9Error) as error:
        step9.cmd_sample(arguments, _sample_hooks(db))
    assert error.value.code == "sample_mismatch"
    assert list(run_dir.glob("samples/minute-*.json")) == []


def test_spool_allocated_bounded_command_survives_inherited_pipes() -> None:
    import threading
    import time
    # Wrapper exits at once but leaves a 30s grandchild holding the pipes:
    # the selectors drain must not block on inherited open write ends.
    outcome: dict = {}

    def _target() -> None:
        try:
            step9._run_bounded_command(
                [sys.executable, "-c",
                 ("import subprocess,sys;subprocess.Popen("
                  "[sys.executable,'-c','import time;time.sleep(30)']);")],
                timeout=1, max_output_bytes=4096)
        except BaseException as error:  # noqa: BLE001 - record any outcome
            outcome["error"] = error
        else:
            outcome["error"] = None

    worker = threading.Thread(target=_target, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=15)
    elapsed = time.monotonic() - started
    assert not worker.is_alive(), "bounded runner hung on inherited pipes"
    assert isinstance(outcome.get("error"), subprocess.TimeoutExpired)
    assert 0.6 <= elapsed < 5


def test_spool_allocated_bounded_command_reaps_direct_child() -> None:
    import time
    # Timeout must leave no zombie: the child PID has to be reaped by the
    # reserved in-budget wait, not merely abandoned when the caller returns.
    captured: dict = {}
    real_popen = subprocess.Popen

    def _spy(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured["proc"] = proc
        return proc

    started = time.monotonic()
    with mock.patch.object(subprocess, "Popen", side_effect=_spy):
        with pytest.raises(subprocess.TimeoutExpired):
            step9._run_bounded_command(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                timeout=1, max_output_bytes=4096)
    elapsed = time.monotonic() - started
    assert captured["proc"].returncode is not None
    assert 0.6 <= elapsed < 2.0


def test_spool_allocated_bounded_command_read_error_reaps_child() -> None:
    import time
    # An unexpected read failure must still kill and reap the live child
    # via centralized cleanup, not leak it through stream-closing only.
    # Only the narrow runner seam is patched (never global os.read), and
    # the real child is captured by a Popen spy before fault injection.
    real_popen = subprocess.Popen
    captured: dict = {}

    def _spy(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured["proc"] = proc
        return proc

    started = time.monotonic()
    with mock.patch.object(subprocess, "Popen", side_effect=_spy), \
            mock.patch.object(step9, "_read_pipe",
                              side_effect=OSError("injected read fault")):
        with pytest.raises(OSError, match="injected read fault"):
            step9._run_bounded_command(
                [sys.executable, "-c",
                 "print('hi', flush=True);import time;time.sleep(30)"],
                timeout=5, max_output_bytes=4096)
    assert time.monotonic() - started < 5
    assert captured["proc"].returncode is not None


def test_spool_allocated_bounded_command_streams_and_errors() -> None:
    completed = step9._run_bounded_command(
        [sys.executable, "-c",
         "import sys;sys.stdout.write('out');sys.stderr.write('err')"],
        timeout=5, max_output_bytes=4096)
    assert (completed.returncode, completed.stdout,
            completed.stderr) == (0, "out", "err")
    nonzero = step9._run_bounded_command(
        [sys.executable, "-c", "import sys;sys.exit(3)"],
        timeout=5, max_output_bytes=4096)
    assert (nonzero.returncode, nonzero.stdout,
            nonzero.stderr) == (3, "", "")
    with pytest.raises(ValueError):
        step9._run_bounded_command(
            [sys.executable, "-c",
             "import sys;sys.stderr.write('x'*5000)"],
            timeout=5, max_output_bytes=4096)


def test_validate_v2_probe_tampering_fails_closed(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_preflight(tmp_path, "pftamper", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    victim = run_dir / "samples" / "minute-0007.json"
    pristine = json.loads(victim.read_text())

    def _break(mutate) -> None:
        sample = json.loads(json.dumps(pristine))
        mutate(sample["resources"]["allocated_probe"])
        victim.write_text(json.dumps(sample))
        (run_dir / "manifest.json").unlink()
        step9._write_manifest(run_dir)
        assert step9.cmd_validate(arguments) == 1

    _break(lambda probe: probe.update(error="allocated_probe_timeout"))
    _break(lambda probe: probe.pop("allocated_bytes"))
    _break(lambda probe: probe.update(allocated_bytes=-5))
    _break(lambda probe: probe.update(allocated_bytes=True))
    _break(lambda probe: probe.update(allocated_bytes="100"))
    _break(lambda probe: probe.pop("configured_path"))
    _break(lambda probe: probe.update(configured_path="/other/spool"))
    _break(lambda probe: probe.update(path="/other/spool"))
    _break(lambda probe: probe.update(resolved_path="/other/spool"))
    _break(lambda probe: probe.__setitem__(
        "command", [part for part in probe["command"] if part != "--"]))
    _break(lambda probe: probe.__setitem__(
        "command", probe["command"][:-1] + ["/other/spool"]))
    _break(lambda probe: probe["mount_after"].update(mnt_id=-1))
    _break(lambda probe: probe["mount_before"].update(source="unknown"))
    _break(lambda probe: probe.pop("mount_after"))
    _break(lambda probe: probe.pop("mount_identity"))
    _break(lambda probe: probe["mount_identity"].update(source="/dev/other"))
    _break(lambda probe: probe.pop("started_at"))
    _break(lambda probe: probe.update(started_at=probe["finished_at"],
                                       finished_at=probe["started_at"]))
    _break(lambda probe: probe.update(started_at="2026-10-04T05:00:00"))
    _break(lambda probe: probe.update(started_at="2026-10-04T10:00:00+05:00",
                                       finished_at="2026-10-04T10:00:01+05:00"))
    _break(lambda probe: probe.update(timeout_seconds=6))
    _break(lambda probe: probe.update(timeout_seconds="5"))
    _break(lambda probe: probe.update(started_at="2026-10-04T05:00:00+00:00",
                                       finished_at="2026-10-04T05:00:06+00:00"))
    # A legitimate relative configured path still validates.
    sample = json.loads(json.dumps(pristine))
    pinned_spool = json.loads((run_dir / "run.json").read_text())[
        "spool_path"]
    sample["resources"]["allocated_probe"]["configured_path"] = (
        os.path.relpath(pinned_spool))
    victim.write_text(json.dumps(sample))
    (run_dir / "manifest.json").unlink()
    step9._write_manifest(run_dir)
    assert step9.cmd_validate(arguments) == 0


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


def test_resource_gates_compare_matching_counter_sources() -> None:
    baseline = _quiet_facts()
    baseline["cgroup"] = {"oom_kills": None, "swap_current_bytes": None}
    current = _quiet_facts()
    current["cgroup"] = {"oom_kills": None, "swap_current_bytes": None}
    current["memory"]["oom_kills"] = 1
    failures, unknown, _ = step9.evaluate_resource_gates(baseline, current, 0)
    assert "oom_kill_observed" in failures
    assert "oom_unknown" not in unknown

    current["memory"]["oom_kills"] = 0
    failures, unknown, _ = step9.evaluate_resource_gates(baseline, current, 0)
    assert "oom_kill_observed" not in failures
    assert "oom_unknown" not in unknown

    current["memory"]["oom_kills"] = None
    failures, unknown, _ = step9.evaluate_resource_gates(baseline, current, 0)
    assert "oom_kill_observed" not in failures
    assert "oom_unknown" in unknown

    for baseline_cgroup, current_cgroup in (
        ({"oom_kills": 4, "swap_current_bytes": 4},
         {"oom_kills": None, "swap_current_bytes": None}),
        ({"oom_kills": None, "swap_current_bytes": None},
         {"oom_kills": 4, "swap_current_bytes": 4}),
    ):
        baseline = _quiet_facts()
        baseline["cgroup"] = baseline_cgroup
        current = _quiet_facts()
        current["cgroup"] = current_cgroup
        current["memory"]["oom_kills"] = 1
        current["memory"]["swap_used_bytes"] = 1
        failures, unknown, _ = step9.evaluate_resource_gates(
            baseline, current, 0)
        assert {"oom_kill_observed", "swap_growth"} <= set(failures)
        assert "oom_unknown" not in unknown
        assert "swap_unknown" not in unknown

    baseline = _quiet_facts()
    baseline["cgroup"] = {"oom_kills": 2, "swap_current_bytes": 3}
    current = _quiet_facts()
    current["cgroup"] = {"oom_kills": 1, "swap_current_bytes": 2}
    failures, _, _ = step9.evaluate_resource_gates(baseline, current, 0)
    assert "oom_counter_reset" in failures
    assert "swap_growth" not in failures
    assert not any("swap" in code and "reset" in code for code in failures)


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
    _wire_budget_fixture(db, cohort)
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        step9.cmd_start(arguments, db)
    _pin_resources(run_dir)
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
    tariff_path.write_text(json.dumps(_canonical_live_tariff()))
    cohort = _write_cohort(tmp_path / "cohort.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        fake_db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
        _wire_budget_fixture(fake_db, cohort)
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
                "--spool-path", str(tmp_path), "--postgres-path",
                str(tmp_path),
                "--deadline", "2026-10-05T05:10:00Z",
                "--watchdog-unit", "test-unit",
                "--run-id", "testrun01",
                "--max-invocation-gap-seconds", "5",
                "--bootstrap-run-id", "boot1",
                "--budget-run-id", "budget1",
                "--archive-retained-cap-bytes", "17179869184",
                "--transfer-cap-bytes", "68719476736",
                "--s3-cap-attempts", "100000",
                "--spool-allocated-cap-bytes", "17179869184",
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
                           return_value=None), _healthy_start_hosts():
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
                           return_value=None), _healthy_start_hosts():
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
             "resource_facts": lambda run, db,
             metrics: _quiet_facts_with_probe(run),
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
              "resource_facts": lambda run, db,
              metrics: _quiet_facts_with_probe(run),
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
    totals, error = step9._worker_snapshots(
        {}, lambda run: [{"archive": {"remote_attempts": {}}}])
    assert totals == {} and error == "s3_worker_malformed:ValueError"
    for count in (-1, True, 1.5, "2"):
        totals, error = step9._worker_snapshots(
            {}, lambda run, count=count: [
                {"archive": {"remote_attempts": {"get": count}}}
            ])
        assert totals == {} and error == "s3_worker_malformed:ValueError"


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
        step9._read_tariff_file(str(good))[0],
        datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert block["with_uncertainty_eur"] == 3.686616
    assert block["digest"] and block["note"].startswith("tariff estimate")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_canonical_tariff(payload_cap_gib=17)))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block(
            step9._read_tariff_file(str(bad))[0],
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert error.value.code == "tariff_mismatch"
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(_canonical_tariff(verified_utc_date="2026-01-01")))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block(
            step9._read_tariff_file(str(stale))[0],
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert error.value.code == "tariff_stale"
    over = tmp_path / "over.json"
    over.write_text(json.dumps(_canonical_tariff(with_uncertainty_eur=9.99)))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block(
            step9._read_tariff_file(str(over))[0],
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    assert error.value.code == "tariff_mismatch"
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"source": "x"}))
    with pytest.raises(step9.Step9Error):
        step9._tariff_block(
            step9._read_tariff_file(str(missing))[0],
            datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    with pytest.raises(step9.Step9Error):
        step9._read_tariff_file(str(tmp_path / "absent.json"))
    with pytest.raises(step9.Step9Error):
        step9._read_tariff_file("relative.json")


_CANONICAL_TARIFF_DIGEST = (
    "d3903d0e5f1b3b303aba768324656571032667f5468809d140c2f0ed15ba759f"
)

_CANONICAL_TARIFF_RAW = b'{\n  "billable_units_round_up": true,\n  "combined_tax_and_uncertainty_factor": "1.5",\n  "cost_scope": "run transfer and first186days of newly retained storage; not lifetime retention",\n  "currency": "EUR",\n  "egress_eur_per_decimal_gb": "0.01",\n  "envelopes": {\n    "absolute": {\n      "aggregate_transfer_bytes": 600000000000,\n      "before_factor_eur": "35.462400",\n      "ceiling_eur": "55",\n      "egress_projection_eur": "6.00",\n      "fits": true,\n      "new_retained_bytes": 300000000000,\n      "storage_projection_eur": "29.462400",\n      "with_factor_eur": "53.1936000"\n    },\n    "operational": {\n      "aggregate_transfer_bytes": 570000000000,\n      "before_factor_eur": "32.707200",\n      "ceiling_eur": "50",\n      "egress_projection_eur": "5.70",\n      "fits": true,\n      "new_retained_bytes": 275000000000,\n      "storage_projection_eur": "27.007200",\n      "with_factor_eur": "49.0608000"\n    }\n  },\n  "free_egress_allowance_used": false,\n  "ingress_included": true,\n  "listed_prices_exclude_tax": true,\n  "provider": "Scaleway",\n  "region": "Paris",\n  "requests_included": true,\n  "retrieved_at": "2026-09-10T06:46:59.328362+00:00",\n  "run_authorized": false,\n  "schema": "issue92-phase5-tariff-refresh-v1",\n  "source_url": "https://www.scaleway.com/en/pricing/storage/",\n  "storage_class": "Standard Multi-AZ",\n  "storage_eur_per_decimal_gb_hour": "0.000022",\n  "storage_horizon_days": 186,\n  "tax_rate_claimed": null,\n  "verification_method": "official pricing page content returned by web search; full-page web open exceeded size limit and direct urllib fetch returned403"\n}\n'


def _write_live_tariff(tmp_path: Path, raw: bytes = _CANONICAL_TARIFF_RAW,
                       name: str = "tariff-live.json") -> Path:
    """Test-only: byte-exact canonical refresh file (hash verified)."""
    path = tmp_path / name
    path.write_bytes(raw)
    return path


def _live_core() -> datetime:
    return datetime(2026, 10, 4, 5, 0, tzinfo=UTC)


def test_live_tariff_exact_canonical_accept(tmp_path: Path) -> None:
    """The byte-exact refresh binds schema/digest/economics; no authority."""
    payload, digest = step9._read_tariff_file(
        str(_write_live_tariff(tmp_path)))
    assert digest == _CANONICAL_TARIFF_DIGEST
    block = step9._tariff_block_live(payload, digest, _live_core())
    assert block["schema"] == "issue92-phase5-tariff-refresh-v1"
    assert block["digest"] == _CANONICAL_TARIFF_DIGEST
    assert block["source_url"] == "https://www.scaleway.com/en/pricing/storage/"
    assert block["retrieved_at"] == "2026-09-10T06:46:59.328362+00:00"
    assert block["verification_method"].startswith("official pricing page")
    assert block["run_authorized"] is False
    assert block["storage_horizon_days"] == 186
    operational = block["envelopes"]["operational"]
    assert operational["new_retained_bytes"] == 275000000000
    assert operational["aggregate_transfer_bytes"] == 570000000000
    assert operational["storage_projection_eur"] == "27.007200"
    assert operational["egress_projection_eur"] == "5.70"
    assert operational["before_factor_eur"] == "32.707200"
    assert operational["with_factor_eur"] == "49.0608000"
    assert operational["ceiling_eur"] == "50"
    assert operational["fits"] is True
    absolute = block["envelopes"]["absolute"]
    assert absolute["new_retained_bytes"] == 300000000000
    assert absolute["aggregate_transfer_bytes"] == 600000000000
    assert absolute["storage_projection_eur"] == "29.462400"
    assert absolute["egress_projection_eur"] == "6.00"
    assert absolute["before_factor_eur"] == "35.462400"
    assert absolute["with_factor_eur"] == "53.1936000"
    assert absolute["ceiling_eur"] == "55"
    assert absolute["fits"] is True


def test_live_tariff_rejects_preparation_schema(tmp_path: Path) -> None:
    """The old EUR 4.50/5 preparation file can never bind live-day v2."""
    path = tmp_path / "prep.json"
    path.write_text(json.dumps(_canonical_tariff()))
    payload, digest = step9._read_tariff_file(str(path))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block_live(payload, digest, _live_core())
    assert error.value.code == "tariff_mismatch"
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    arguments = _start_args(tmp_path / "run", cohort,
                            deployed_receipt=str(receipt_path),
                            archive_tariff_file=str(path))
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
        with pytest.raises(step9.Step9Error) as error:
            step9.cmd_start(arguments, db)
        assert error.value.code == "tariff_mismatch"


def test_live_tariff_rejects_prior_280_580(tmp_path: Path) -> None:
    """Prior 280/580 GB drafts are rejected for live-day v2."""
    payload = _canonical_live_tariff()
    payload["envelopes"]["operational"]["new_retained_bytes"] = 280000000000
    payload["envelopes"]["operational"]["aggregate_transfer_bytes"] = (
        580000000000)
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(payload))
    loaded, digest = step9._read_tariff_file(str(path))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block_live(loaded, digest, _live_core())
    assert error.value.code == "tariff_mismatch"


def test_live_tariff_rejects_math_and_authority_tamper(tmp_path: Path) -> None:
    """Wrong economics, false fits, or run authority all fail."""
    def block(**overrides):
        payload = _canonical_live_tariff(**overrides)
        path = tmp_path / f"t-{len(os.listdir(tmp_path))}.json"
        path.write_text(json.dumps(payload))
        loaded, digest = step9._read_tariff_file(str(path))
        return step9._tariff_block_live(loaded, digest, _live_core())
    assert block()["envelopes"]["operational"]["fits"] is True
    bad_envelope = _canonical_live_tariff()
    bad_envelope["envelopes"]["operational"]["with_factor_eur"] = (
        "49.0608001")
    with pytest.raises(step9.Step9Error) as error:
        block(envelopes=bad_envelope["envelopes"])
    assert error.value.code == "tariff_mismatch"
    bad_storage = _canonical_live_tariff()
    bad_storage["envelopes"]["absolute"]["storage_projection_eur"] = (
        "29.462401")
    with pytest.raises(step9.Step9Error) as error:
        block(envelopes=bad_storage["envelopes"])
    assert error.value.code == "tariff_mismatch"
    bad_fits = _canonical_live_tariff()
    bad_fits["envelopes"]["operational"]["fits"] = False
    with pytest.raises(step9.Step9Error) as error:
        block(envelopes=bad_fits["envelopes"])
    assert error.value.code == "tariff_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        block(run_authorized=True)
    assert error.value.code == "tariff_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        block(tax_rate_claimed=0.2)
    assert error.value.code == "tariff_mismatch"
    with pytest.raises(step9.Step9Error) as error:
        block(storage_eur_per_decimal_gb_hour="0.000023")
    assert error.value.code == "tariff_mismatch"


def test_live_tariff_digest_binds_bytes_and_stale_rejected(
        tmp_path: Path) -> None:
    """Any byte change rebinds the digest; stale refreshes fail."""
    tampered = _CANONICAL_TARIFF_RAW.replace(b"Paris", b"Parix")
    path = _write_live_tariff(tmp_path, tampered, "tampered.json")
    payload, digest = step9._read_tariff_file(str(path))
    assert digest != _CANONICAL_TARIFF_DIGEST
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block_live(payload, digest, _live_core())
    assert error.value.code == "tariff_mismatch"
    stale = _canonical_live_tariff(retrieved_at="2026-01-01T00:00:00+00:00")
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(json.dumps(stale))
    loaded, stale_digest = step9._read_tariff_file(str(stale_path))
    with pytest.raises(step9.Step9Error) as error:
        step9._tariff_block_live(loaded, stale_digest, _live_core())
    assert error.value.code == "tariff_stale"


def test_start_binds_live_tariff_without_authority(tmp_path: Path) -> None:
    """Live-day admission pins the refresh; run_authorized stays false."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    _run_dir, header = _started_run(tmp_path, db)
    tariff = header["cost_basis"]["tariff"]
    assert tariff["schema"] == "issue92-phase5-tariff-refresh-v1"
    assert tariff["run_authorized"] is False
    assert tariff["digest"] and len(tariff["digest"]) == 64


def test_start_archive_controls_binding(tmp_path: Path) -> None:
    """Live-day v2 caps: required, exact, reused; preflight keeps constants."""
    cohort = _write_cohort(tmp_path / "c-controls.txt", TAGS)
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    _wire_budget_fixture(db, cohort)

    def _live(name, **overrides):
        args = {"archive_retained_cap_bytes": 10**9,
                "transfer_cap_bytes": 10**9, "s3_cap_attempts": 10**6}
        args.update(overrides)
        receipt_path = tmp_path / f"{name}-receipt.json"
        receipt_path.write_text(json.dumps(_receipt_scope(
            admission_run_id=name.replace("-", ""))))
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
            return step9.cmd_start(_start_args(
                tmp_path / name, cohort, deployed_receipt=str(receipt_path),
                run_id=name.replace("-", ""), **args), db)

    for index, bad in enumerate(({"archive_retained_cap_bytes": None},
                                 {"transfer_cap_bytes": -1},
                                 {"s3_cap_attempts": True},
                                 {"s3_cap_attempts": "3000"},
                                 {"spool_allocated_cap_bytes": None})):
        with pytest.raises(step9.Step9Error) as error:
            _live(f"ctlbad{index}", **bad)
        assert error.value.code == "archive_controls_invalid"
        assert not (tmp_path / f"ctlbad{index}").exists()
    receipt_path = tmp_path / "ctlpre-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope(
        admission_run_id="ctlpre")))
    with pytest.raises(step9.Step9Error) as error:
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), _healthy_start_hosts():
            step9.cmd_start(_start_args(
                tmp_path / "ctlpre", cohort,
                deployed_receipt=str(receipt_path), run_id="ctlpre",
                mode="preflight", core_start="2026-10-04T05:00:00Z",
                core_end="2026-10-04T06:15:00Z",
                archive_retained_cap_bytes=1000), db)
    assert error.value.code == "archive_controls_mixed"
    with pytest.raises(step9.Step9Error) as error:
        _live("ctlprior", prior_transfer_bytes=10**9 + 1,
              prior_transfer_provenance="p")
    assert error.value.code == "archive_controls_invalid"
    with pytest.raises(step9.Step9Error) as error:
        _live("ctls3prior", s3_cap_attempts=100, prior_s3_attempts=101,
              prior_s3_provenance="q")
    assert error.value.code == "archive_controls_invalid"
    header = _live("ctlok")
    assert (header["archive_retained_cap_bytes"],
            header["transfer_cap_bytes"],
            header["s3_cap_attempts"]) == (10**9, 10**9, 10**6)


def test_archive_retained_gates_use_pins_not_constants() -> None:
    """Newly retained remote bytes: exact subtraction against the pin."""

    def _facts(logical, physical=100, objects=2):
        facts = _quiet_facts()
        facts["archive"] = {"logical_bytes": logical, "objects": objects,
                             "physical_bytes": physical, "error": None}
        return facts

    base = _facts(1000)
    assert step9.evaluate_resource_gates(
        base, _facts(1500), 0, retained_cap=500)[0] == []
    failures, _unknown, _s = step9.evaluate_resource_gates(
        base, _facts(1501), 0, retained_cap=500)
    assert failures == ["archive_retained_breach"]
    failures, _unknown, _s = step9.evaluate_resource_gates(
        base, _facts(999), 0, retained_cap=500)
    assert failures == ["archive_retained_reset"]
    _f, unknown, _s = step9.evaluate_resource_gates(
        _facts(1000), _facts(None), 0, retained_cap=500)
    assert unknown == ["archive_unknown"]
    nobase = _quiet_facts()
    nobase["archive"] = {"logical_bytes": None, "objects": 2,
                          "physical_bytes": 100, "error": None}
    _f, unknown, _s = step9.evaluate_resource_gates(
        nobase, _facts(1500), 0, retained_cap=500)
    assert unknown == ["archive_retained_unknown"]
    # Legacy envelope still applies without a pin, and pins override it.
    failures, _u, _s = step9.evaluate_resource_gates(
        base, _facts(17 * 1024**3), 0)
    assert failures == ["archive_logical_breach"]
    assert step9.evaluate_resource_gates(
        base, _facts(1000 + 20 * 1024**3), 0,
        retained_cap=10**15)[0] == []
    failures, _u, _s = step9.evaluate_resource_gates(
        base, _facts(1200), 0, retained_cap=100)
    assert failures == ["archive_retained_breach"]


def test_live_day_v2_ignores_preparation_object_envelope() -> None:
    """Object counts never gate live-day v2 retained accounting."""

    def _facts(logical, objects):
        facts = _quiet_facts()
        facts["archive"] = {"logical_bytes": logical, "objects": objects,
                             "physical_bytes": 100, "error": None}
        return facts

    base = _facts(100, 2)
    failures, unknown, _s = step9.evaluate_resource_gates(
        base, _facts(200, 200_000), 0, retained_cap=500)
    assert failures == [] and unknown == []
    failures, _u, _s = step9.evaluate_resource_gates(
        base, _facts(200, 200_000), 0)
    assert failures == ["archive_objects_breach"]


def test_archive_retained_sample_monotonic() -> None:
    """Retained bytes must be monotonic sample-to-sample, not just capped."""

    def _facts(logical):
        facts = _quiet_facts()
        facts["archive"] = {"logical_bytes": logical, "objects": 2,
                             "physical_bytes": 100, "error": None}
        return facts

    base = _facts(0)
    failures, _u, _s = step9.evaluate_resource_gates(
        base, _facts(50), 0, retained_cap=10**9, prev_retained=100)
    assert failures == ["archive_retained_reset"]
    assert step9.evaluate_resource_gates(
        base, _facts(100), 0, retained_cap=10**9,
        prev_retained=100)[0] == []
    _f, unknown, _s = step9.evaluate_resource_gates(
        base, _facts(100), 0, retained_cap=10**9, prev_retained=None)
    assert unknown == ["archive_retained_unknown"]
    _f, unknown, _s = step9.evaluate_resource_gates(
        base, _facts(100), 0, retained_cap=10**9, prev_retained="x")
    assert unknown == ["archive_retained_unknown"]
    _f, unknown, _s = step9.evaluate_resource_gates(
        base, _facts(100), 0, retained_cap=10**9, prev_retained=-5)
    assert unknown == ["archive_retained_unknown"]


def test_spool_cap_pin_selected_over_constant() -> None:
    """Pinned spool bytes bind the pin, never shared-pool constants."""

    def _facts(physical):
        facts = _quiet_facts()
        facts["archive"] = {"logical_bytes": 100, "objects": 2,
                             "physical_bytes": physical, "error": None}
        return facts

    base = _facts(100)
    assert step9.evaluate_resource_gates(
        base, _facts(5000), 0, spool_cap=5000)[0] == []
    failures, _u, _s = step9.evaluate_resource_gates(
        base, _facts(5001), 0, spool_cap=5000)
    assert failures == ["archive_physical_breach"]
    # A pin above the legacy constant passes what the constant rejects.
    assert step9.evaluate_resource_gates(
        base, _facts(70 * 1024**3), 0, spool_cap=10**15)[0] == []
    failures, _u, _s = step9.evaluate_resource_gates(
        base, _facts(70 * 1024**3), 0)
    assert failures == ["archive_physical_breach"]


def test_spool_cap_bounds_independently_probed_bytes(tmp_path: Path) -> None:
    """Real du allocated bytes pass at cap and breach at cap minus one."""
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "blob").write_bytes(b"x" * 4096)
    facts = step9.collect_resource_facts(
        spool_path=str(spool), postgres_path=str(spool), db=None,
        metrics=None,
        btrfs_probe=lambda _path: {"metadata_pct": None,
                                       "unallocated_bytes": None,
                                       "error": None, "stderr": None},
        device_probe=lambda _path: {"errors": {}, "error": None})
    physical = facts["archive"]["physical_bytes"]
    assert type(physical) is int and physical >= 0
    quiet = _quiet_facts()
    facts["memory"] = quiet["memory"]
    facts["filesystems"] = quiet["filesystems"]
    failures, _u, _s = step9.evaluate_resource_gates(
        quiet, facts, 0, spool_cap=physical)
    assert failures == []
    failures, _u, _s = step9.evaluate_resource_gates(
        quiet, facts, 0, spool_cap=physical - 1)
    assert failures == ["archive_physical_breach"]


def test_transfer_cap_pin_selected_over_constant() -> None:
    base = {"boot_id": "b", "interfaces": {"test-eth0": {
        "present": True, "rx_bytes": 1000, "tx_bytes": 500,
        "mac": "m"}}}
    current = {"status": "captured", "boot_id": "b",
               "interfaces": {"test-eth0": {
                   "present": True, "rx_bytes": 1100, "tx_bytes": 500,
                   "mac": "m"}}}
    assert step9.evaluate_wire(base, current, 0, 100)[0] == []
    assert step9.evaluate_wire(base, current, 0, 99)[0] == [
        "transfer_breach"]
    assert step9.evaluate_wire(base, current, 0)[0] == []


def test_finalize_transfer_reuses_pinned_caps() -> None:
    samples = [{"wire": {"failures": [], "unknown": [],
                           "conservative_host_wire_bytes": 150},
                "s3": {"error": None, "go": {}, "go_total": 0,
                         "python": {}, "python_total": 0, "total": 100},
                "resources": {"failures": [], "unknown": [],
                                "allocated_probe": None,
                                "archive_retained": {
                                    "baseline_bytes": 0,
                                    "current_bytes": 100,
                                    "newly_retained_bytes": 100,
                                    "cap_bytes": 10**9}}}]
    run = {"mode": "live-day", "schema": step9.SCHEMA_LIVE,
           "archive_retained_cap_bytes": 10**9,
           "transfer_cap_bytes": 200, "s3_cap_attempts": 121,
           "spool_allocated_cap_bytes": 10**9,
           "transfer_prior_bytes": 0,
           "resource_baseline": {"archive": {"logical_bytes": 0}},
           "s3_prior": {"attempts": 21, "provenance": "p"}}
    result = step9._finalize_transfer(samples, run)
    assert result["status"] == "complete"
    assert result["s3_attempts"] == 121
    assert (result["cap_bytes"], result["attempts_cap"]) == (200, 121)
    tight = step9._finalize_transfer(samples, dict(run, s3_cap_attempts=120))
    assert tight["status"] == "failed"
    assert tight["failure"] == "s3_attempts_breach"


def test_sample_archive_retained_cap_end_to_end(tmp_path: Path) -> None:
    """Minute gates enforce the pinned retained cap, exactly."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(
        tmp_path, db, run_dir_name="retainedok",
        archive_retained_cap_bytes=500)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")

    def _hook(logical):
        def facts(run, db, metrics):
            current = _quiet_facts()
            current["archive"] = {"logical_bytes": logical, "objects": 2,
                                    "physical_bytes": 100, "error": None}
            return current
        return facts

    hooks = _sample_hooks(db)
    hooks["resource_facts"] = _hook(600)
    assert step9.cmd_sample(arguments, hooks) == 0
    db2 = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir2, _h2 = _started_run(
        tmp_path, db2, run_dir_name="retainedbad",
        archive_retained_cap_bytes=500)
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    hooks2 = _sample_hooks(db2)
    hooks2["resource_facts"] = _hook(601)
    assert step9.cmd_sample(arguments2, hooks2) == 1
    bad = json.loads((run_dir2 / "samples" / "minute-0000.json"
                      ).read_text())
    assert bad["failure_code"] == "archive_retained_breach"
    assert bad["outcome"] == "resource_gate"


def test_sample_archive_retained_monotonic_end_to_end(tmp_path: Path) -> None:
    """A mid-run retained decrease gates the minute even under the cap."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(
        tmp_path, db, run_dir_name="monotonic",
        archive_retained_cap_bytes=10**9)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    logicals = iter([100, 150, 120])

    def facts(run, db, metrics):
        current = _quiet_facts()
        current["archive"] = {"logical_bytes": next(logicals),
                                "objects": 2, "physical_bytes": 100,
                                "error": None}
        return current

    hooks = _sample_hooks(db)
    hooks["resource_facts"] = facts
    hooks["max_slots"] = 3
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 1
    bad = json.loads((run_dir / "samples" / "minute-0002.json"
                      ).read_text())
    assert bad["failure_code"] == "archive_retained_reset"
    assert bad["outcome"] == "resource_gate"
    kept = bad["resources"]["archive_retained"]
    assert kept == {"baseline_bytes": 100, "current_bytes": 120,
                    "newly_retained_bytes": 20,
                    "cap_bytes": 10**9}


def test_start_live_day_baseline_evidence_gates(tmp_path: Path) -> None:
    """Missing/malformed/negative baselines fail before admission."""
    cohort = _write_cohort(tmp_path / "c-base.txt", TAGS)

    def _begin(name, db, **overrides):
        receipt_path = tmp_path / f"{name}-receipt.json"
        receipt_path.write_text(json.dumps(_receipt_scope(
            admission_run_id=name.replace("-", ""))))
        # Podman only: wire facts stay real (or per-case mocked) so the
        # malformed-wire cases below exercise the true collection path.
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None), \
                mock.patch.object(step9.Podman, "inspect_running",
                                  lambda self, container: (False, "sha256:image")):
            return step9.cmd_start(_start_args(
                tmp_path / name, cohort, run_id=name.replace("-", ""),
                deployed_receipt=str(receipt_path), **overrides), db)

    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    with pytest.raises(step9.Step9Error) as error:
        _begin("badprior", db, prior_transfer_bytes=-5)
    assert error.value.code == "transfer_prior_invalid"
    assert not (tmp_path / "badprior").exists()
    for index, usage in enumerate((None, (-5, 2), ("x", 2), (0, -1))):
        bad = FakeDB(rows=[_eligible_row(1, TAGS[0])])
        bad.archive_usage_data = usage
        with pytest.raises(step9.Step9Error) as error:
            _begin(f"badarchive{index}", bad)
        assert error.value.code == "archive_baseline_invalid"
        assert not (tmp_path / f"badarchive{index}").exists()
    with mock.patch.object(step9, "collect_wire_facts",
                           return_value={"status": "unknown",
                                         "interfaces": {}}):
        db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
        with pytest.raises(step9.Step9Error) as error:
            _begin("badwire", db)
        assert error.value.code == "wire_baseline_invalid"
        assert not (tmp_path / "badwire").exists()
    with mock.patch.object(
            step9, "collect_wire_facts",
            return_value={"status": "captured", "boot_id": "b",
                          "interfaces": {"test-eth0": {
                              "present": True, "rx_bytes": -1,
                              "tx_bytes": 0}}}):
        db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
        with pytest.raises(step9.Step9Error) as error:
            _begin("badwirebytes", db)
        assert error.value.code == "wire_baseline_invalid"


def test_validate_recomputes_wire_and_retained(tmp_path: Path) -> None:
    """Strict validation recomputes numbers; tampered math fails closed."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "recompute", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    victim = run_dir / "samples" / "minute-0007.json"
    pristine = json.loads(victim.read_text())

    def _break(mutate) -> None:
        sample = json.loads(json.dumps(pristine))
        mutate(sample)
        victim.write_text(json.dumps(sample))
        (run_dir / "manifest.json").unlink()
        step9._write_manifest(run_dir)
        assert step9.cmd_validate(arguments) == 1

    # Retained delta must equal current minus the pinned baseline.
    _break(lambda sample: sample["resources"]["archive_retained"].update(
        newly_retained_bytes=9999))
    # Retained current below the previous sample is a missed reset.
    _break(lambda sample: sample["resources"]["archive_retained"].update(
        current_bytes=50, newly_retained_bytes=-50))
    # Missing retained facts cannot validate.
    _break(lambda sample: sample["resources"].pop("archive_retained"))
    # Cap above the run pin cannot validate.
    _break(lambda sample: sample["resources"]["archive_retained"].update(
        cap_bytes=10**15))
    # Wire bytes above the transfer pin cannot validate.
    _break(lambda sample: sample["wire"].update(
        conservative_host_wire_bytes=step9.TRANSFER_CUMULATIVE_MAX + 1))
    # Wire bytes below the previous sample is a missed counter reset.
    _break(lambda sample: sample["wire"].update(
        conservative_host_wire_bytes=500))
    # Malformed wire bytes cannot validate.
    _break(lambda sample: sample["wire"].update(
        conservative_host_wire_bytes="1000"))
    # Probed spool bytes above the pinned cap cannot validate, even with a
    # clean sample verdict: validation recomputes, never trusts the strings.
    _break(lambda sample: sample["resources"]["allocated_probe"].update(
        allocated_bytes=step9.RES_ARCHIVE_PHYSICAL_MAX + 1))


def test_validate_rejects_final_cap_mismatch(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "ctlcap", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    final_path = run_dir / "final.json"
    final = json.loads(final_path.read_text())
    final["transfer"]["cap_bytes"] -= 1
    final_path.write_text(json.dumps(final))
    (run_dir / "manifest.json").unlink()
    step9._write_manifest(run_dir)
    assert step9.cmd_validate(arguments) == 1


def test_s3_strikes_persist_across_healthy_db_samples(tmp_path: Path) -> None:
    """Repeated S3 archive misses stop even when DB/metrics stay healthy."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    def no_archive(run):
        raise RuntimeError("archive unreachable")
    hooks = _sample_hooks(db, worker_probe=no_archive)
    hooks["max_slots"] = 3
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob(
        "two_consecutive_unavailable-*.json"))


def test_sample_wire_unknown_stops_live_day(tmp_path: Path) -> None:
    """A live-day wire interface outside the pinned baseline stops at once."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    def extra_iface(run):
        facts = _quiet_wire()
        facts["boot_id"] = run.get("boot_id")
        facts["interfaces"]["eth9"] = {
            "present": True, "rx_bytes": 10, "tx_bytes": 5}
        return facts
    hooks = _sample_hooks(db, wire_facts=extra_iface)
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob("wire_unknown-*.json"))


def test_sample_wire_between_sample_decrease_stops(tmp_path: Path) -> None:
    """Wire totals falling between samples stop, even above the baseline."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    calls = []
    def falling(run):
        calls.append(1)
        facts = _quiet_wire(rx=1200 if len(calls) == 1 else 1100)
        facts["boot_id"] = run.get("boot_id")
        return facts
    hooks = _sample_hooks(db, wire_facts=falling)
    hooks["max_slots"] = 3
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob("wire_counter_reset-*.json"))


def test_sample_resource_unknown_stops_live_day(tmp_path: Path) -> None:
    """A live-day required resource fact of unknown stops at once."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    def unknown_archive(run, db, metrics):
        facts = _quiet_facts()
        facts["archive"] = {"logical_bytes": None, "objects": 2,
                            "physical_bytes": 100, "error": None}
        return facts
    hooks = _sample_hooks(db, resource_facts=unknown_archive)
    assert step9.cmd_sample(arguments, hooks) == 1
    assert list((run_dir / "failures").glob("archive_unknown-*.json"))


def test_validate_rejects_final_wire_numeric_mismatch(tmp_path: Path) -> None:
    """Sealed totals must equal the sample chain: cap+1/missing/mismatch fail."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "wirenumer", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    final_path = run_dir / "final.json"
    pristine = json.loads(final_path.read_text())
    cap = pristine["transfer"]["cap_bytes"]
    wire = pristine["transfer"]["wire_bytes"]
    attempts = pristine["transfer"]["s3_attempts"]
    assert type(wire) is int and wire <= cap and wire >= 0
    def reseal(mutator):
        final = json.loads(json.dumps(pristine))
        mutator(final)
        final_path.write_text(json.dumps(final))
        (run_dir / "manifest.json").unlink()
        step9._write_manifest(run_dir)
        assert step9.cmd_validate(arguments) == 1
    reseal(lambda final: final["transfer"].update({"wire_bytes": cap + 1}))
    # A post-stop total merely below the sealed value still satisfies the
    # monotonic chain (bounds, not point equality, bind post-stop facts);
    # decreases below the sealed drain observation fail in the chain test.
    reseal(lambda final: final["transfer"].pop("wire_bytes"))
    reseal(lambda final: final["transfer"].update(
        {"s3_attempts": attempts + 1}))


def test_validate_rejects_terminal_chain_tampering(tmp_path: Path) -> None:
    """Chain stages disagree, go missing, or decrease: validate fails."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "chaintamp", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    final_path = run_dir / "final.json"
    pristine = json.loads(final_path.read_text())
    cap = pristine["transfer"]["cap_bytes"]
    attempts_cap = pristine["transfer"]["attempts_cap"]
    def reseal(mutator):
        final = json.loads(json.dumps(pristine))
        mutator(final)
        final_path.write_text(json.dumps(final))
        (run_dir / "manifest.json").unlink()
        step9._write_manifest(run_dir)
        assert step9.cmd_validate(arguments) == 1
    # Drain record disagrees with the sealed final copy.
    reseal(lambda final: final["transfer"].update(
        {"drain_wire_bytes": pristine["transfer"]["drain_wire_bytes"] + 1}))
    # Post-stop wire below the sealed drain observation (decrease).
    reseal(lambda final: final["transfer"].update(
        {"wire_bytes": pristine["transfer"]["drain_wire_bytes"] - 1}))
    # Post-stop wire at cap+1.
    reseal(lambda final: final["transfer"].update({"wire_bytes": cap + 1}))
    # Terminal total disagrees with sealed per-producer attribution.
    reseal(lambda final: final["transfer"].update(
        {"terminal_s3_total": pristine["transfer"]["terminal_s3_total"] + 1}))
    # Attempts at cap+1 with consistent labels.
    reseal(lambda final: final["transfer"].update(
        {"s3_attempts": attempts_cap + 1,
         "terminal_s3_total": attempts_cap + 1 - 21}))
    # Duplicated producer attribution.
    def duplicate(final):
        final["transfer"]["terminal_producers"].append(
            dict(final["transfer"]["terminal_producers"][0]))
    reseal(duplicate)
    # Missing retained post-stop block.
    reseal(lambda final: final["transfer"].pop("retained_post_stop"))
    # Wrong incarnation with higher totals that still sum exactly.
    def impostor(final):
        producers = final["transfer"]["terminal_producers"]
        producers[1]["process_id"] = "impostor-worker"
        producers[1]["total"] += 100
        final["transfer"]["terminal_s3_total"] += 100
        final["transfer"]["s3_attempts"] += 100
    reseal(impostor)
    # Worker replica IDs must be exactly 1..N.
    def renumber(final):
        final["transfer"]["terminal_producers"][1]["replica"] = 2
    reseal(renumber)
    # Collector replica must be null.
    def collector_replica(final):
        final["transfer"]["terminal_producers"][0]["replica"] = 1
    reseal(collector_replica)
    # Invalid and stale capture timestamps fail.
    def bad_timestamp(final):
        final["transfer"]["terminal_producers"][1]["captured_at"] = \
            "not-a-date"
    reseal(bad_timestamp)
    def stale_timestamp(final):
        final["transfer"]["terminal_producers"][1]["captured_at"] = \
            "2026-10-03T05:00:00+00:00"
    reseal(stale_timestamp)
    # A missing worker entry fails even with consistent totals.
    def drop_worker(final):
        producers = final["transfer"]["terminal_producers"]
        dropped = producers.pop(1)
        final["transfer"]["terminal_s3_total"] -= dropped["total"]
        final["transfer"]["s3_attempts"] -= dropped["total"]
    reseal(drop_worker)


def test_validate_rejects_absent_core_identity(tmp_path: Path) -> None:
    """Live-day v2 has no unobserved-producer bypass at validate."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "noident", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 0
    last_path = run_dir / "samples" / "minute-1439.json"
    sample = json.loads(last_path.read_text())
    sample["s3"]["producers"][1] = None
    last_path.write_text(json.dumps(sample))
    (run_dir / "manifest.json").unlink()
    step9._write_manifest(run_dir)
    assert step9.cmd_validate(arguments) == 1


def test_drain_reset_below_core_fails(tmp_path: Path) -> None:
    """Reset at drain fails even when terminal totals later rise."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    # S3 reset at drain: core subtotal 30, drain observes only 10.
    run_dir, _run = _sealed_run(tmp_path, "drsthree", db)
    last_path = run_dir / "samples" / "minute-1439.json"
    sample = json.loads(last_path.read_text())
    sample["s3_attempts_cumulative"] = 51
    last_path.write_text(json.dumps(sample))
    drain_path = run_dir / "drain-monitor.json"
    drain = json.loads(drain_path.read_text())
    drain["observations"]["s3_total"] = 10
    drain_path.write_text(json.dumps(drain))
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 1
    final = json.loads((run_dir / "final.json").read_text())
    assert final["transfer"]["failure"] == "s3_counter_reset"
    # Retained reset at drain: observation below the core current.
    run_dir2, _run2 = _sealed_run(tmp_path, "drretained", db)
    drain_path2 = run_dir2 / "drain-monitor.json"
    drain2 = json.loads(drain_path2.read_text())
    assert drain2["observations"]["retained_bytes"] > 0
    drain2["observations"]["retained_bytes"] -= 5
    drain_path2.write_text(json.dumps(drain2))
    arguments2 = mock.Mock(run_dir=str(run_dir2), podman_bin="podman")
    assert step9.cmd_finalize(arguments2, {"db": db}) == 1
    final2 = json.loads((run_dir2 / "final.json").read_text())
    assert final2["transfer"]["failure"] == "archive_retained_reset"
    # Validate side: sealed drain reset below a positive core subtotal.
    run_dir3, _run3 = _sealed_run(tmp_path, "drval", db)
    arguments3 = mock.Mock(run_dir=str(run_dir3), podman_bin="podman")
    assert step9.cmd_finalize(arguments3, {"db": db}) == 0
    assert step9.cmd_validate(arguments3) == 0
    last_path3 = run_dir3 / "samples" / "minute-1439.json"
    sample3 = json.loads(last_path3.read_text())
    sample3["s3_attempts_cumulative"] = 51
    last_path3.write_text(json.dumps(sample3))
    drain_path3 = run_dir3 / "drain-monitor.json"
    drain3 = json.loads(drain_path3.read_text())
    drain3["observations"]["s3_total"] = 10
    drain_path3.write_text(json.dumps(drain3))
    (run_dir3 / "manifest.json").unlink()
    step9._write_manifest(run_dir3)
    assert step9.cmd_validate(arguments3) == 1
    drain3["observations"]["s3_total"] = 30
    drain3["observations"]["retained_bytes"] -= 5
    drain_path3.write_text(json.dumps(drain3))
    (run_dir3 / "manifest.json").unlink()
    step9._write_manifest(run_dir3)
    assert step9.cmd_validate(arguments3) == 1


def test_finalize_rejects_drain_counter_gaps(tmp_path: Path) -> None:
    """Drain retained/S3 missing, non-integer, or negative stays incomplete."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "draingaps", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    drain_path = run_dir / "drain-monitor.json"
    pristine_drain = json.loads(drain_path.read_text())
    for label, override in (
            ("s3neg", {"s3_total": -5}),
            ("s3str", {"s3_total": "40"}),
            ("s3miss", {"s3_total": None}),
            ("retneg", {"retained_bytes": -1}),
            ("retnull", {"retained_bytes": None}),
            ("wiremiss", {"wire_total": None})):
        drain = json.loads(json.dumps(pristine_drain))
        drain["observations"].update(override)
        drain_path.write_text(json.dumps(drain))
        (run_dir / "final.json").unlink(missing_ok=True)
        (run_dir / "manifest.json").unlink(missing_ok=True)
        assert step9.cmd_finalize(arguments, {"db": db}) != 0, label
        if (run_dir / "final.json").exists():
            final = json.loads((run_dir / "final.json").read_text())
            assert final["transfer"]["status"] != "complete", label
    drain_path.write_text(json.dumps(pristine_drain))
    (run_dir / "final.json").unlink(missing_ok=True)
    (run_dir / "manifest.json").unlink(missing_ok=True)
    assert step9.cmd_finalize(arguments, {"db": db}) == 0


def test_finalize_rejects_terminal_identity_gaps(tmp_path: Path) -> None:
    """Wrong/stale/missing producer identity stays incomplete or failing."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, run = _sealed_run(tmp_path, "termident", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    spool = Path(run["spool_path"]) / ".control" / "terminal"
    pristine_collector = (spool / "collector.json").read_text()
    pristine_worker = (spool / "worker-1.json").read_text()
    def attempt(label, mutate=None, remove=None):
        for name, content in (("collector.json", pristine_collector),
                              ("worker-1.json", pristine_worker)):
            (spool / name).write_text(content)
        if remove is not None:
            (spool / remove).unlink()
        if mutate is not None:
            mutate()
        (run_dir / "final.json").unlink(missing_ok=True)
        (run_dir / "manifest.json").unlink(missing_ok=True)
        assert step9.cmd_finalize(arguments, {"db": db}) != 0, label
    def rewrite_worker(payload):
        (spool / "worker-1.json").write_text(json.dumps(payload))
    worker = json.loads(pristine_worker)
    # Higher totals under the wrong incarnation still fail.
    tampered = json.loads(json.dumps(worker))
    tampered["process"]["id"] = "impostor-worker"
    tampered["archive"]["remote_attempts"] = {"put": 10**6}
    attempt("wrong-incarnation", lambda: rewrite_worker(tampered))
    # Stale capture predating the run fails.
    tampered = json.loads(json.dumps(worker))
    tampered["captured_at"] = "2026-10-03T05:00:00+00:00"
    attempt("stale-capture", lambda: rewrite_worker(tampered))
    # Invalid timestamp is malformed.
    tampered = json.loads(json.dumps(worker))
    tampered["captured_at"] = "not-a-date"
    attempt("bad-timestamp", lambda: rewrite_worker(tampered))
    # Unmarked snapshot is not terminal evidence.
    tampered = json.loads(json.dumps(worker))
    tampered["terminal"] = False
    attempt("unmarked", lambda: rewrite_worker(tampered))
    # Missing replica or collector is incomplete, never zero.
    attempt("missing-worker", remove="worker-1.json")
    attempt("missing-collector", remove="collector.json")
    for name, content in (("collector.json", pristine_collector),
                          ("worker-1.json", pristine_worker)):
        (spool / name).write_text(content)
    (run_dir / "final.json").unlink(missing_ok=True)
    (run_dir / "manifest.json").unlink(missing_ok=True)
    assert step9.cmd_finalize(arguments, {"db": db}) == 0


def test_finalize_rejects_post_stop_wire_failure(tmp_path: Path) -> None:
    """Post-stop wire failures propagate instead of being discarded."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "postwire", db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    hooks = {"db": db,
             "wire_facts": lambda run: {"status": "captured",
                                          "failure_code": None,
                                          "boot_id": run.get("boot_id"),
                                          "interfaces": {}}}
    assert step9.cmd_finalize(arguments, hooks) == 1
    final = json.loads((run_dir / "final.json").read_text())
    assert final["transfer"]["status"] == "failed"
    assert final["transfer"]["failure"] == "wire_interface_missing"


def test_post_stop_reads_catalogue_through_hooks_db(tmp_path: Path) -> None:
    """Post-stop retained facts come from the read-only catalogue path."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _run = _sealed_run(tmp_path, "catread", db)
    calls: list = []
    original = db.archive_usage
    def spy():
        calls.append(1)
        return original()
    db.archive_usage = spy
    baseline = json.loads((run_dir / "run.json").read_text())["wire_baseline"]
    grown = json.loads(json.dumps(baseline))
    grown["interfaces"]["lo"]["rx_bytes"] += 100
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    hooks = {"db": db, "wire_facts": lambda run: grown}
    assert step9.cmd_finalize(arguments, hooks) == 0
    assert calls, "catalogue archive_usage was never consulted"
    final = json.loads((run_dir / "final.json").read_text())
    assert final["transfer"]["wire_bytes"] == \
        step9.TRANSFER_PRIOR_BYTES + 100


def test_finalize_includes_positive_drain_and_shutdown_bytes(tmp_path: Path) -> None:
    """Final totals bind drain + shutdown activity, not samples[-1]."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, run = _sealed_run(tmp_path, "drainpos", db)
    last = json.loads(
        (run_dir / "samples" / "minute-1439.json").read_text())
    sample_wire = last["wire"]["conservative_host_wire_bytes"]
    sample_cumulative = last["s3_attempts_cumulative"]
    sample_retained = last["resources"]["archive_retained"]["current_bytes"]
    assert sample_cumulative == 21
    # Bounded drain activity: +400 wire bytes, 40 Python attempts.
    drain = json.loads((run_dir / "drain-monitor.json").read_text())
    drain["observations"].update({
        "wire_total": sample_wire + 400, "s3_total": 40,
        "retained_bytes": sample_retained + 10})
    (run_dir / "drain-monitor.json").write_text(json.dumps(drain))
    # Shutdown activity after drain: +100 wire bytes, +5 attempts.
    spool = Path(run["spool_path"])
    worker_file = spool / ".control" / "terminal" / "worker-1.json"
    worker_file.write_text(json.dumps({
        "schema": step9.TERMINAL_WORKER_SCHEMA, "producer": "worker",
        "process": {"id": "test-worker-1",
                     "started_at": run["core_start"]},
        "captured_at": run["core_end"], "terminal": True,
        "archive": {"remote_attempts": {"put": 40, "head": 5}},
    }), encoding="utf-8")
    # Post-stop capture through the read-only observation hooks (the same
    # seam production serves via CLI/database hooks): loopback advanced
    # 500 bytes past the pinned baseline, catalogue retained +10.
    baseline = json.loads((run_dir / "run.json").read_text())["wire_baseline"]
    grown = json.loads(json.dumps(baseline))
    grown["interfaces"]["lo"]["rx_bytes"] += 500
    retained_facts = _quiet_facts()
    retained_facts["archive"]["logical_bytes"] = sample_retained + 10
    hooks = {"db": db,
             "wire_facts": lambda run: grown,
             "resource_facts": lambda run, db, metrics: retained_facts}
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    assert step9.cmd_finalize(arguments, hooks) == 0
    final = json.loads((run_dir / "final.json").read_text())
    transfer = final["transfer"]
    assert transfer["status"] == "complete"
    assert transfer["wire_bytes"] == step9.TRANSFER_PRIOR_BYTES + 500
    assert transfer["wire_bytes"] > sample_wire
    assert transfer["drain_wire_bytes"] == sample_wire + 400
    assert transfer["s3_attempts"] == 21 + 45
    assert transfer["s3_attempts"] > sample_cumulative
    assert transfer["terminal_s3_total"] == 45
    assert transfer["retained_post_stop"]["newly_retained_bytes"] == \
        sample_retained + 10 - 100
    assert step9.cmd_validate(arguments) == 0


def test_start_requires_tariff_file(tmp_path: Path) -> None:
    cohort = _write_cohort(tmp_path / "c.txt", TAGS)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    arguments = _start_args(tmp_path / "notariff", cohort,
                            deployed_receipt=str(receipt_path),
                            archive_tariff_file="/nonexistent-tariff.json")
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None), _healthy_start_hosts():
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
            step9._read_tariff_file(str(path))[0], core)

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
        "--prior-s3-attempts", "11", "--prior-s3-provenance", "q",
        "--archive-retained-cap-bytes", "12",
        "--transfer-cap-bytes", "13", "--s3-cap-attempts", "14",
        "--spool-allocated-cap-bytes", "17179869184"]
    namespace = step9.build_parser().parse_args(args)
    assert namespace.prior_transfer_bytes == 10
    assert namespace.prior_transfer_provenance == "p"
    assert namespace.prior_s3_attempts == 11
    assert namespace.prior_s3_provenance == "q"
    assert namespace.archive_retained_cap_bytes == 12
    assert namespace.transfer_cap_bytes == 13
    assert namespace.s3_cap_attempts == 14
    assert namespace.spool_allocated_cap_bytes == 17179869184


def _monitor_args(run_dir: Path, pid: int, **overrides):
    defaults = {"run_dir": str(run_dir), "drain_pid": pid,
                "poll_seconds": 1, "timeout_seconds": 30}
    defaults.update(overrides)
    return mock.Mock(**defaults)


def _write_monitor_seed(run_dir: Path, run: dict) -> None:
    """Test-only: final-core-sample seed plus Go terminal file.

    _started_run writes neither samples nor terminal snapshots, but the
    live-day monitor requires a seeded chain and the stopped collector's
    terminal identity. Values match _healthy_monitor_hooks exactly, so
    healthy polls observe no drift; producer identities stay unverified
    (None) exactly like the hook files.
    """
    samples = run_dir / "samples"
    samples.mkdir(exist_ok=True)
    (samples / "minute-1439.json").write_text(json.dumps({
        "slot": 1439, "captured_utc": "2026-10-04T05:01:00+00:00",
        "wire": {"conservative_host_wire_bytes": 0},
        "s3_attempts_cumulative": 26,
        "resources": {"archive_retained": {"current_bytes": 100}},
        "s3": {"producers": [None, None]},
    }), encoding="utf-8")
    terminal = Path(run["spool_path"]) / ".control" / "terminal"
    terminal.mkdir(parents=True, exist_ok=True)
    (terminal / "collector.json").write_text(json.dumps({
        "schema": step9.TERMINAL_GO_SCHEMA, "producer": "collector",
        "process_id": "test-collector-pid",
        "process_started_at": run["core_start"],
        "captured_at": run["core_end"], "terminal": True,
        "operations": {},
    }), encoding="utf-8")


def _healthy_monitor_hooks(db: FakeDB, run: dict) -> dict:
    """Live-loop-equivalent facts: quiet resources, matching wire, S3 totals."""
    def wire_facts(run):
        facts = _quiet_wire()
        facts["boot_id"] = run.get("boot_id")
        return facts
    return {
        "wire_facts": wire_facts,
        "resource_facts": lambda run, db, metrics: _quiet_facts_with_probe(
            run),
        "worker_probe": lambda run: [
            {"archive": {"remote_attempts": {"get": 3, "bucket": 1,
                                             "marker": 1}}}],
    }


def test_drain_monitor_dead_child_is_drained(tmp_path: Path) -> None:
    """An already-exited drain child returns success with a record."""
    import subprocess as _subprocess
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    _write_monitor_seed(run_dir, header)
    done = _subprocess.Popen(["true"])
    assert done.wait(timeout=30) == 0
    arguments = _monitor_args(run_dir, done.pid)
    assert step9.cmd_drain_monitor(arguments, _healthy_monitor_hooks(
        db, header)) == 0
    record = json.loads((run_dir / "drain-monitor.json").read_text())
    assert record["outcome"] == "drained"
    assert record["finished_at"] >= record["started_at"]


def test_drain_monitor_zombie_counts_as_done(tmp_path: Path) -> None:
    """An unreaped zombie is done: the driver reaps only after return."""
    import subprocess as _subprocess
    import time as _time
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    _write_monitor_seed(run_dir, header)
    child = _subprocess.Popen(["sleep", "0.3"])
    while child.poll() is None:
        _time.sleep(0.05)
    # Unreaped by design here: reap only after the monitor returns.
    started = _time.monotonic()
    try:
        arguments = _monitor_args(run_dir, child.pid, timeout_seconds=20)
        assert step9.cmd_drain_monitor(arguments, _healthy_monitor_hooks(
            db, header)) == 0
    finally:
        child.wait(timeout=30)
    assert _time.monotonic() - started < 10


def test_drain_monitor_spool_breach_stops(tmp_path: Path) -> None:
    """Allocated spool past the pinned cap fails the drain at once."""
    import subprocess as _subprocess
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    _write_monitor_seed(run_dir, header)
    hooks = _healthy_monitor_hooks(db, header)
    def breached(run, db, metrics):
        facts = _quiet_facts_with_probe(run)
        facts["archive"] = {**facts["archive"],
                            "physical_bytes": 2**40}
        return facts
    hooks["resource_facts"] = breached
    child = _subprocess.Popen(["sleep", "30"])
    try:
        arguments = _monitor_args(run_dir, child.pid)
        assert step9.cmd_drain_monitor(arguments, hooks) == 1
    finally:
        child.kill()
        child.wait(timeout=30)
    record = json.loads((run_dir / "drain-monitor.json").read_text())
    assert record["outcome"] == "archive_physical_breach"


def test_drain_monitor_stall_times_out(tmp_path: Path) -> None:
    """A drain child that never exits hits the bounded timeout."""
    import subprocess as _subprocess
    import time as _time
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    _write_monitor_seed(run_dir, header)
    child = _subprocess.Popen(["sleep", "30"])
    try:
        started = _time.monotonic()
        arguments = _monitor_args(run_dir, child.pid, timeout_seconds=2)
        assert step9.cmd_drain_monitor(arguments, _healthy_monitor_hooks(
            db, header)) == 1
        assert _time.monotonic() - started < 15
    finally:
        child.kill()
        child.wait(timeout=30)
    record = json.loads((run_dir / "drain-monitor.json").read_text())
    assert record["outcome"] == "drain_timeout"


def test_drain_monitor_missing_producer_fails_with_dead_child(
        tmp_path: Path) -> None:
    """A missing producer is incomplete at once, child already gone."""
    import subprocess as _subprocess
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    _write_monitor_seed(run_dir, header)
    hooks = _healthy_monitor_hooks(db, header)
    def no_workers(run):
        raise RuntimeError("workers gone")
    hooks["worker_probe"] = no_workers
    done = _subprocess.Popen(["true"])
    assert done.wait(timeout=30) == 0
    arguments = _monitor_args(run_dir, done.pid)
    assert step9.cmd_drain_monitor(arguments, hooks) == 1
    record = json.loads((run_dir / "drain-monitor.json").read_text())
    assert record["outcome"] == "terminal_capture_missing"


def test_drain_monitor_stopped_collector_and_live_workers(tmp_path: Path) -> None:
    """Stopped collector terminal plus live workers aggregate exactly."""
    import subprocess as _subprocess
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, header = _started_run(tmp_path, db)
    _write_monitor_seed(run_dir, header)
    terminal = Path(header["spool_path"]) / ".control" / "terminal"
    (terminal / "collector.json").write_text(json.dumps({
        "schema": step9.TERMINAL_GO_SCHEMA, "producer": "collector",
        "process_id": "test-collector-pid",
        "process_started_at": header["core_start"],
        "captured_at": header["core_end"], "terminal": True,
        "operations": {"put": 7},
    }), encoding="utf-8")
    done = _subprocess.Popen(["true"])
    assert done.wait(timeout=30) == 0
    arguments = _monitor_args(run_dir, done.pid)
    assert step9.cmd_drain_monitor(arguments, _healthy_monitor_hooks(
        db, header)) == 0
    record = json.loads((run_dir / "drain-monitor.json").read_text())
    assert record["outcome"] == "drained"
    assert record["observations"]["s3_total"] == 12


def test_s3_single_miss_is_retained_failed_evidence(tmp_path: Path) -> None:
    """One S3 miss is retained failed evidence; recovery clears strikes."""
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    arguments = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
    calls = []
    def flaky(run):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("archive unreachable")
        return [{"archive": {"remote_attempts": {"get": 3, "bucket": 1,
                                                 "marker": 1}}}]
    hooks = _sample_hooks(db, worker_probe=flaky)
    hooks["max_slots"] = 2
    hooks["single_pass"] = False
    assert step9.cmd_sample(arguments, hooks) == 0
    missed = json.loads((run_dir / "samples" / "minute-0000.json").read_text())
    assert missed["outcome"] == "s3_unavailable"
    assert missed["s3"]["error"] is not None
    assert list((run_dir / "failures").glob("s3_unavailable-*.json"))
    recovered = json.loads(
        (run_dir / "samples" / "minute-0001.json").read_text())
    assert recovered["outcome"] == "on_time"
