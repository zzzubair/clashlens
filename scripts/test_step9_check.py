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
        "run_id": "testrun01", "database_url": None,
    }
    defaults.update(overrides)
    return mock.Mock(**defaults)


def _receipt_scope(scope: str = "deployed-stack") -> dict:
    return {"receipt_scope": scope, "source": {"revision": "a" * 40},
            "receipt_digest": "sha256:" + "b" * 64}


class FakeDB:
    """In-memory stand-in for step9.Database."""

    def __init__(self, rows=(), outside: int = 0, roots: int = 0,
                 fail: str | None = None, resets=()) -> None:
        self.rows = list(rows)
        self.outside = outside
        self.roots = roots
        self.fail = fail
        self.resets = list(resets)
        self.fixed_ids: list[int] = [r[0] for r in self.rows]

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
    run_dir = tmp_path / "run"
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
    assert header["admission_reconciliation"] == {"status": step9.ADMISSION_STATUS}
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
    late = step9.classify_slot(base, base, 70.0, 70.0, False)
    assert late["outcome"] == "late" and late["failure_code"] == "sample_late"
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
    walls = [datetime(2026, 10, 4, 5, 0, tzinfo=UTC)]

    def clock():
        mono[0] += 60_000_000_000
        return mono[0]

    def now_utc():
        walls[0] += timedelta(seconds=60)
        return walls[0]

    hooks = {
        "db": db, "fixed_ids": db.fixed_ids,
        "fetch_metrics": lambda url: {"counters": {"jobs": 5}, "digest": "x"},
        "watchdog_check": lambda run: True, "clock": clock, "now_utc": now_utc,
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
    counters = [{"counters": {"jobs": 9}, "digest": "a"},
                {"counters": {"jobs": 4}, "digest": "b"}]
    sample = step9.build_sample(
        run=run, index=0, expected_utc=datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
        captured_utc=datetime(2026, 10, 4, 5, 0, 1, tzinfo=UTC),
        mono_elapsed=1.0, wall_delta=1.0, mono_delta=1.0, boot_id=run["boot_id"],
        db_facts=db.minute_snapshot([]), db_error=None,
        metrics=counters[1], metrics_error=None, previous_metrics=counters[0],
        pressure={}, fs={}, watchdog_active=True)
    assert sample["counter_reset"] == "jobs"


def test_sql_is_read_only_and_bound() -> None:
    for statement in step9.ALL_RO_STATEMENTS:
        assert ("%s" in statement or "IN ('pending'" in statement
                or "pg_control_system" in statement
                or "pg_current_wal_lsn" in statement)
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
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.commands.append(list(command))
        targets = [part for part in command if part.startswith("test-")]
        assert targets, f"refusing non-test container: {command}"
        if command[1:3] == ["container", "inspect"]:
            return "true\nsha256:image\n" if self.running else "false\nsha256:image\n"
        if command[1] == "update":
            return ""
        if command[1] == "stop":
            if not self.stop_fails:
                self.running = False
            return ""
        raise AssertionError(f"unexpected podman command: {command}")


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
    podman = FakePodman()
    arguments = _watchdog_args(run_dir)
    hooks = {"podman_run": podman, "single_pass": True, "max_iterations": 1,
             "now_utc": lambda: datetime(2026, 10, 4, 5, 1, tzinfo=UTC)}
    # no samples yet -> stop path with exact command
    assert step9.cmd_watchdog(arguments, hooks) == 1
    flat = [part for command in podman.commands for part in command]
    assert flat[:2] == ["podman", "container"]
    assert ["podman", "update", "--restart=no", "test-collector"] in podman.commands
    assert ["podman", "stop", "--ignore", "--time", "30",
            "test-collector"] in podman.commands
    outcome = json.loads((run_dir / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "no_samples"


def test_watchdog_rejects_container_mismatch(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    podman = FakePodman()
    arguments = _watchdog_args(run_dir, collector_container="test-other")
    with pytest.raises(step9.Step9Error):
        step9.cmd_watchdog(arguments, {"podman_run": podman})
    assert podman.commands == []


def test_watchdog_stop_failure_is_evidence_failure(tmp_path: Path) -> None:
    db = FakeDB(rows=[_eligible_row(1, TAGS[0])])
    run_dir, _header = _started_run(tmp_path, db)
    podman = FakePodman(stop_fails=True)
    arguments = _watchdog_args(run_dir,
                               deadline="2026-10-03T05:00:00Z")  # already past
    hooks = {"podman_run": podman,
             "now_utc": lambda: datetime(2026, 10, 4, 5, 0, tzinfo=UTC)}
    assert step9.cmd_watchdog(arguments, hooks) == 2
    outcome = json.loads((run_dir / "watchdog-outcome.json").read_text())
    assert outcome["trigger"] == "deadline_reached"
    assert outcome["stop_error"] == "container_still_running"


def _sealed_run(tmp_path: Path, name: str, db: FakeDB,
                boundary=(datetime(2026, 10, 5, 5, 0, tzinfo=UTC), 1,
                          True, True, True, 2, 2, 0)):
    cohort = _write_cohort(tmp_path / f"{name}-cohort.txt", TAGS)
    receipt_path = tmp_path / f"{name}-receipt.json"
    receipt_path.write_text(json.dumps(_receipt_scope()))
    run_dir = tmp_path / name
    arguments = _start_args(run_dir, cohort, run_id=name.replace("-", ""),
                            deployed_receipt=str(receipt_path))
    with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                           return_value=None):
        run = step9.cmd_start(arguments, db)
    db.resets = [boundary]
    core_start = step9._parse_utc(run["core_start"])
    samples = run_dir / "samples"
    samples.mkdir()
    for index in range(step9.CORE_SLOTS):
        expected = step9.slot_expected_utc(core_start, index)
        sample = step9.build_sample(
            run=run, index=index, expected_utc=expected,
            captured_utc=expected + timedelta(seconds=1),
            mono_elapsed=float(index * 60), wall_delta=60.0, mono_delta=60.0,
            boot_id=run["boot_id"], db_facts=db.minute_snapshot([]),
            db_error=None,
            metrics={"counters": {"jobs": index}, "digest": str(index)},
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
    assert step9.cmd_finalize(arguments, {"db": db}) == 0
    assert step9.cmd_validate(arguments) == 1  # gate: reset_handoff_unproven


# --- Real PostgreSQL tests (migrated disposable schema, no skips) ----------

_PG_SERVER: dict = {}


def _pg_url() -> str:
    url = os.environ.get("CLASHLENS_TEST_DATABASE_URL")
    if url:
        return url
    if _PG_SERVER.get("url"):
        return _PG_SERVER["url"]
    import subprocess
    import tarfile

    cache = (Path.home() / ".embedded-postgres-go" /
             "embedded-postgres-binaries-linux-amd64-18.0.0.txz")
    if not cache.is_file():
        pytest.fail("no CLASHLENS_TEST_DATABASE_URL and no embedded pg cache")
    base = Path(tempfile.mkdtemp(prefix="step9-pg-"))
    with tarfile.open(cache) as archive:
        archive.extractall(base)
    bindir = base / "bin"
    data = base / "data"
    sock = base / "sock"
    sock.mkdir()
    for binary in ("initdb", "pg_ctl", "postgres", "psql", "createdb"):
        (bindir / binary).chmod(0o755)
    port = "55439"
    try:
        subprocess.run([str(bindir / "initdb"), "-D", str(data), "-U", "postgres",
                        "-E", "UTF8"], check=True, capture_output=True, text=True,
                       timeout=120)
        subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "-l",
                        str(base / "log"), "-o",
                        f"-p {port} -k {sock} -c listen_addresses=127.0.0.1",
                        "start"], check=True, capture_output=True, text=True,
                       timeout=120)
        subprocess.run([str(bindir / "psql"), "-h", "127.0.0.1", "-p", port,
                        "-U", "postgres", "-c",
                        "CREATE DATABASE clashlens"], check=True,
                       capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        pytest.fail(f"embedded PostgreSQL boot failed: {error}")
    _PG_SERVER["url"] = (
        f"postgresql://postgres@127.0.0.1:{port}/clashlens")
    _PG_SERVER["base"] = base
    return _PG_SERVER["url"]


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
        assert database.outside_roots([good]) == 1
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
    walls = [datetime(2026, 10, 4, 5, 0, tzinfo=UTC)]

    def clock():
        mono[0] += 1_000_000_000
        return mono[0]

    def now_utc():
        walls[0] += timedelta(seconds=60)
        return walls[0]

    return {"db": database, "fixed_ids": fixed_ids,
            "fetch_metrics": lambda url: {"counters": {"jobs": mono[0]},
                                          "digest": str(mono[0])},
            "watchdog_check": lambda run: True, "clock": clock,
            "now_utc": now_utc, "no_sleep": True, "max_slots": slots}


def test_no_official_traffic_rehearsal() -> None:
    """Full 1440-slot loop on a disposable migrated DB; no official calls."""
    import psycopg
    from domain_test_support import domain_database

    with domain_database(_pg_url(), include_coordinator=True) as info:
        database = step9.Database(lambda: psycopg.connect(info))
        with psycopg.connect(info) as connection:
            player = _seed_player(connection, TAGS[0])
            _seed_reset(connection, [player])
            connection.commit()
        cohort = Path(tempfile.mkdtemp(prefix="step9-cohort-")) / "cohort.txt"
        _write_cohort(cohort, TAGS)
        run_dir = cohort.parent / "run"
        arguments = _start_args(run_dir, cohort, database_url=info)
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None):
            receipt_path = cohort.parent / "receipt.json"
            receipt_path.write_text(json.dumps(_receipt_scope()))
            arguments.deployed_receipt = str(receipt_path)
            header = step9.cmd_start(arguments, database)
        assert header["initial"]["eligible_count"] == 1
        sample_args = mock.Mock(run_dir=str(run_dir), podman_bin="podman")
        assert step9.cmd_sample(sample_args,
                                _rehearsal_hooks(database, [player],
                                                 slots=step9.CORE_SLOTS)) == 0
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
            player = _seed_player(connection, TAGS[0])
            connection.commit()
        base = Path(tempfile.mkdtemp(prefix="step9-kill-"))
        # Killed sampler: 3 slots then stop; finalize must refuse a partial run.
        cohort = _write_cohort(base / "cohort.txt", TAGS)
        kill_receipt = base / "receipt.json"
        kill_receipt.write_text(json.dumps(_receipt_scope()))
        run_dir = base / "run-killed"
        with mock.patch.object(step9.deployment_receipt, "validate_receipt",
                               return_value=None):
            step9.cmd_start(_start_args(run_dir, cohort,
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
        podman = FakePodman()
        watchdog_args = _watchdog_args(run_dir2,
                                       deadline="2026-10-03T05:00:00Z")
        assert step9.cmd_watchdog(watchdog_args, {"podman_run": podman}) == 1
        assert ["podman", "stop", "--ignore", "--time", "30",
                "test-collector"] in podman.commands
