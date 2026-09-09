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
        "archive_eur_per_gib": None,
    }
    defaults.update(overrides)
    return mock.Mock(**defaults)


def _receipt_scope(scope: str = "deployed-stack", discovery: str = "false",
                   budget: bool = True) -> dict:
    fields = {"player_discovery_enabled": discovery}
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
    receipt_path.write_text(json.dumps(_receipt_scope()))
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
        "resource_facts": lambda run, db, metrics: {
            "filesystems": {}, "memory": {}, "archive": {}},
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
    for statement in step9.ALL_RO_STATEMENTS:
        assert ("%s" in statement or "IN ('pending'" in statement
                or "pg_control_system" in statement
                or "pg_current_wal_lsn" in statement
                or "to_regclass" in statement
                or "pending_remote_verification" in statement
                or "archive_catalogue" in statement)
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
    """One timely gate-open event per window plus tail coverage."""
    core_start = step9._parse_utc(run["core_start"])
    mode = step9.MODES[run.get("mode", "live-day")]
    total = mode["windows"] + (6 if extra_tail else 0)
    bounds = db.admission_run(run["run_id"])
    events, roots, profiles = [], [], {}
    for window in range(total):
        cycle = core_start + timedelta(seconds=300 * window)
        at = cycle + timedelta(seconds=1)
        event_id = window + 1
        job = job_base + window
        due = cycle - timedelta(seconds=60)
        events.append({
            "id": event_id, "invocation_id": f"{event_id:032x}",
            "cycle_at": cycle, "scheduler_at": cycle, "database_at": at,
            "gate_allowed": True, "gate_handoff_at": None, "batch_limit": 1000,
            "capture_start": bounds["capture_start"],
            "capture_end": bounds["capture_end"],
            "visible_due_count": 1, "visible_due_min_at": due,
            "unselected_visible_due_count": 0,
            "unselected_visible_due_min_at": None,
            "unselected_visible_past_deadline_count": 0,
            "unselected_visible_past_deadline_min_at": None,
            "selected_past_deadline_count": 0,
            "selected_player_ids": [pid], "selected_due_ats": [due],
            "selected_profile_version_ids": [101],
            "selected_eligibility_states": ["eligible"],
            "inserted_job_ids": [job], "advanced_count": 1,
            "selected_count": 1, "inserted_count": 1})
        key = f"regular:{pid}:{int(cycle.timestamp())}"
        roots.append((pid, key, job, "complete"))
        profiles[event_id] = (0, 1)
    db.admission_events_data = events
    db.admission_roots_data = roots
    db.admission_profile_data = profiles


def _sealed_run(tmp_path: Path, name: str, db: FakeDB,
                boundary=(datetime(2026, 10, 5, 5, 0, tzinfo=UTC), 1,
                          True, True, True, 2, 2, 0),
                late_slots: set = frozenset()):
    cohort = _write_cohort(tmp_path / f"{name}-cohort.txt", TAGS)
    receipt_path = tmp_path / f"{name}-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    run_dir = tmp_path / name
    arguments = _start_args(run_dir, cohort, run_id=name.replace("-", ""),
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
            pressure={}, fs={}, watchdog_active=True)
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
            "resource_facts": lambda run, db, metrics: {
                "filesystems": {}, "memory": {}, "archive": {}},
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
        kill_receipt.write_text(json.dumps(_receipt_scope()))
        run_dir = base / "run-killed"
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None):
            step9.cmd_start(_start_args(run_dir, cohort, run_id="killed1",
                                        deployed_receipt=str(kill_receipt)),
                            database)
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
                                        deployed_receipt=str(kill_receipt)),
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
            pressure={}, fs={}, watchdog_active=True)
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
            ]
            for name, sql, params in readable:
                connection.execute(sql, params if params else None)
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
            "resource_facts": lambda run, db, metrics: {
                "filesystems": {}, "memory": {}, "archive": {}},
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
        "memory": {"used_bytes": 1024**3, "swap_used_bytes": 0,
                   "oom_kills": 0, "psi_avg10": 0.0, "error": None},
        "archive": {"logical_bytes": 100, "objects": 2,
                    "physical_bytes": 100, "cost_eur": 0.01, "error": None}}


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
    breach["memory"] = {"used_bytes": 6 * 1024**3, "swap_used_bytes": 99,
                        "oom_kills": 3, "psi_avg10": 1.0, "error": None}
    breach["archive"] = {"logical_bytes": 20 * 1024**3, "objects": 200_000,
                         "physical_bytes": 70 * 1024**3, "cost_eur": 9.0,
                         "error": None}
    failures, unknown, strikes = step9.evaluate_resource_gates(base, breach, 1)
    for code in ("filesystem_use_breach", "filesystem_free_breach",
                 "physical_growth_breach", "btrfs_metadata_breach",
                 "btrfs_unallocated_breach", "btrfs_diagnostic_stderr",
                 "btrfs_new_error", "oom_kill_observed", "swap_growth",
                 "memory_over", "archive_logical_breach",
                 "archive_objects_breach", "archive_physical_breach",
                 "archive_cost_breach"):
        assert code in failures, code
    assert strikes == 2
    # memory needs two consecutive samples
    current = _quiet_facts()
    current["memory"]["used_bytes"] = 6 * 1024**3
    failures, _unknown, strikes = step9.evaluate_resource_gates(base, current, 0)
    assert "memory_over" not in failures and strikes == 1
    # shared pool evaluated once: spool+postgres labels share one entry
    assert len(base["filesystems"]) == 1
    # null stays unknown, never zero
    empty = {"filesystems": {}, "memory": {}, "archive": {}}
    failures, unknown, _strikes = step9.evaluate_resource_gates(empty, empty, 0)
    assert failures == [] and set(unknown) >= {
        "oom_unknown", "swap_unknown", "memory_unknown", "archive_unknown",
        "archive_physical_unknown", "archive_cost_unknown"}


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
