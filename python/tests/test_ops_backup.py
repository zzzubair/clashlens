"""Exercise the ops command boundary with a disposable container-manager substitute."""

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[2] / "ops"


@pytest.fixture
def runtime(tmp_path):
    state = tmp_path / "state" / "clashlens"
    state.mkdir(parents=True)
    image = "sha256:" + "a" * 64
    manifest = state / "active-release.env"
    manifest.write_text(
        "RELEASE_MODE=production\n"
        + "".join(
            f"{name}_IMAGE={image}\n"
            for name in ("POSTGRES", "PYTHON", "COLLECTOR", "WEBSITE")
        )
    )
    manifest.chmod(0o600)
    manager = tmp_path / "manager"
    manager.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if "--format" in args:
    fmt = args[args.index("--format") + 1]
    if "managed" in fmt: print("ops")
    elif "Rootless" in fmt or "Running" in fmt: print("true")
    elif "Image" in fmt: print("sha256:" + "a" * 64)
    elif "Pod" in fmt or "Id" in fmt: print("test-pod")
elif "backup-list" in args:
    print(os.environ["BACKUPS"])
elif "backup-push" in args:
    sys.exit(int(os.environ.get("UPLOAD_EXIT", "0")))
elif "delete" in args:
    if "--confirm" in args:
        boundary = args[args.index("before") + 1]
        rows = json.loads(os.environ["BACKUPS"])
        start = next(r["start_time"] for r in rows if r["backup_name"] == boundary)
        Path(os.environ["REMAINING"]).write_text(json.dumps([
            r["backup_name"] for r in rows if r["start_time"] >= start
        ]))
elif args[:2] == ["--user", "is-active"]:
    print("active")
"""
    )
    manager.chmod(0o700)
    env = dict(
        os.environ,
        PODMAN_BIN=str(manager),
        SYSTEMCTL_BIN=str(manager),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        REMAINING=str(tmp_path / "remaining"),
    )
    return env, Path(env["REMAINING"])


def backup_row(number, days, *, duration_hours=0):
    start = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return {
        "backup_name": f"base_{number:024X}",
        "start_time": start.isoformat(),
        "finish_time": (start + dt.timedelta(hours=duration_hours)).isoformat(),
    }


def run_ops(runtime, rows, *args, upload_exit=0):
    env, _ = runtime
    return subprocess.run(
        ["bash", str(OPS), *args],
        env=dict(env, BACKUPS=json.dumps(rows), UPLOAD_EXIT=str(upload_exit)),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_extra_manual_backups_do_not_shorten_recovery_window(runtime):
    rows = [backup_row(i, days) for i, days in enumerate((21, 14, 6, 1, 0.1), 1)]
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(runtime[1].read_text()) == [r["backup_name"] for r in rows[1:]]


def test_retention_preview_does_not_delete(runtime):
    result = run_ops(runtime, [backup_row(1, 20), backup_row(2, 10)], "backup-prune")
    assert result.returncode == 0, result.stderr
    assert not runtime[1].exists()


def test_backup_too_new_or_not_finished_before_boundary_is_kept(runtime):
    rows = [backup_row(1, 8, duration_hours=48), backup_row(2, 1)]
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode == 0, result.stderr
    assert not runtime[1].exists()


@pytest.mark.parametrize("rows", [[], [backup_row(1, 2)]])
def test_initial_window_never_deletes(runtime, rows):
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode == 0, result.stderr
    assert not runtime[1].exists()


def test_failed_upload_does_not_prune(runtime):
    result = run_ops(
        runtime, [backup_row(1, 21), backup_row(2, 14)], "backup", upload_exit=1
    )
    assert result.returncode != 0
    assert not runtime[1].exists()


def test_invalid_catalogue_refuses_deletion(runtime):
    rows = [backup_row(1, 21), backup_row(2, 14)]
    rows[1]["finish_time"] = "0001-01-01T00:00:00Z"
    result = run_ops(runtime, rows, "backup-prune", "--apply")
    assert result.returncode != 0
    assert not runtime[1].exists()


@pytest.mark.parametrize("rows", [[], [backup_row(1, 9)]])
def test_status_reports_missing_or_stale_remote_backup(runtime, rows):
    result = run_ops(runtime, rows, "backup-status")
    assert result.returncode != 0
