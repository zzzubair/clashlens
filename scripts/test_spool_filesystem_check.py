from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/src"))
SPEC = importlib.util.spec_from_file_location(
    "spool_filesystem_check", ROOT / "scripts/spool_filesystem_check.py"
)
assert SPEC and SPEC.loader
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def _payload(**overrides):
    base = {
        "mount": {
            "resolved_path": "/spool",
            "mount_point": "/",
            "source": "/dev/sda",
            "filesystem_type": "ext4",
            "options": "rw",
            "mnt_id": 1,
            "error": None,
        },
        "capacity": {
            "free_bytes": 1000,
            "free_inodes": 100,
            "inode_total": 1000,
            "filesystem_type": "ext4",
            "inode_model": "finite",
            "error": None,
        },
    }
    base.update(overrides)
    return base


def test_collect_success_writes_artifact(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    mount = _payload()["mount"] | {"filesystem_type": "btrfs"}
    capacity = _payload()["capacity"] | {
        "filesystem_type": "btrfs",
        "inode_model": "dynamic",
    }
    probe = check._btrfs_result(
        tmp_path / "spool",
        exit_status=0,
        stdout="Data,single: 1\nMetadata,DUP: 2\n",
        stderr="",
        error=None,
    )
    with (
        mock.patch.object(check, "_source_provenance", return_value=("a" * 40, True)),
        mock.patch.object(check, "_mount_facts", return_value=mount),
        mock.patch.object(check, "_capacity_facts", return_value=capacity),
        mock.patch.object(check, "_btrfs_probe", return_value=probe),
    ):
        payload, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
    assert complete is True
    assert payload["source_clean"] is True
    assert payload["paths"]["spool"]["btrfs_usage"]["allocation_evidence"] == "separate"
    digest = check._atomic_write_json(output, payload)
    assert json.loads(output.read_text())["paths"]["spool"]["capacity"]["free_inodes"] == 100
    sidecar = Path(str(output) + ".sha256")
    assert sidecar.read_text().split()[0] == digest


def test_btrfs_probe_failure_blocks_qualification(tmp_path: Path) -> None:
    mount = _payload()["mount"] | {"filesystem_type": "btrfs"}
    capacity = _payload()["capacity"] | {
        "filesystem_type": "btrfs",
        "inode_model": "dynamic",
        "free_inodes": 0,
    }
    for probe_error in ("probe_failed", "tool_missing", "timeout"):
        probe = {
            "command": ["btrfs", "filesystem", "usage", "-b", "/spool"],
            "exit_status": 1 if probe_error == "probe_failed" else None,
            "stdout": "",
            "stderr": "",
            "timeout_seconds": 30,
            "error": probe_error,
        }
        with (
            mock.patch.object(check, "_mount_facts", return_value=mount),
            mock.patch.object(check, "_capacity_facts", return_value=capacity),
            mock.patch.object(check, "_btrfs_probe", return_value=probe),
        ):
            _, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
        assert complete is False


def test_btrfs_probe_requires_complete_allocation_output(tmp_path: Path) -> None:
    target = tmp_path / "spool"
    cases = (
        ("", "empty_output", None),
        ("Data,single: Size: 1, Used: 1\n", "allocation_evidence_missing", None),
        ("Data+Metadata,single: Size: 2, Used: 1\n", None, "combined"),
        ("Data,single: Size: 1, Used: 1\nMetadata,DUP: Size: 1, Used: 1\n", None, "separate"),
    )
    for stdout, expected_error, expected_evidence in cases:
        completed = subprocess.CompletedProcess(
            args=["btrfs"], returncode=0, stdout=stdout, stderr=""
        )
        with mock.patch.object(check.subprocess, "run", return_value=completed):
            result = check._btrfs_probe(target)
        assert result["error"] == expected_error
        assert result["allocation_evidence"] == expected_evidence
        assert result["stdout"] == stdout

    long_output = (
        "Data,single: Size: 1, Used: 1\nMetadata,DUP: Size: 1, Used: 1\n"
        + "x" * check.PROBE_STDOUT_LIMIT
    )
    completed = subprocess.CompletedProcess(
        args=["btrfs"], returncode=0, stdout=long_output, stderr=""
    )
    with mock.patch.object(check.subprocess, "run", return_value=completed):
        result = check._btrfs_probe(target)
    assert result["error"] == "output_truncated"
    assert result["stdout_truncated"] is True
    assert len(result["stdout"]) == check.PROBE_STDOUT_LIMIT


def test_btrfs_probe_maps_subprocess_outcomes(tmp_path: Path) -> None:
    target = tmp_path / "spool"
    with mock.patch.object(check.subprocess, "run", side_effect=FileNotFoundError("btrfs")):
        result = check._btrfs_probe(target)
    assert result["error"] == "tool_missing" and result["exit_status"] is None
    expired = subprocess.TimeoutExpired(cmd=["btrfs"], timeout=30, output="partial-out", stderr="partial-err")
    with mock.patch.object(check.subprocess, "run", side_effect=expired):
        result = check._btrfs_probe(target)
    assert result["error"] == "timeout" and "partial-out" in result["stdout"] and "partial-err" in result["stderr"]
    completed = subprocess.CompletedProcess(args=["btrfs"], returncode=1, stdout="out", stderr="err")
    with mock.patch.object(check.subprocess, "run", return_value=completed):
        result = check._btrfs_probe(target)
    assert result["error"] == "probe_failed" and result["exit_status"] == 1 and result["stdout"] == "out"
    with mock.patch.object(check.subprocess, "run", side_effect=OSError("boom")):
        result = check._btrfs_probe(target)
    assert result["error"] == "probe_error:OSError"


def test_btrfs_mount_capacity_consistency_gates_completeness(tmp_path: Path) -> None:
    btrfs_mount = _payload()["mount"] | {"filesystem_type": "btrfs"}
    ext4_mount = _payload()["mount"] | {"filesystem_type": "ext4"}
    unknown_capacity = _payload()["capacity"] | {"filesystem_type": "unknown", "inode_model": "unknown", "free_inodes": 0}
    finite_ext4 = _payload()["capacity"] | {"filesystem_type": "ext4", "inode_model": "finite"}
    dynamic_btrfs = _payload()["capacity"] | {"filesystem_type": "btrfs", "inode_model": "dynamic", "free_inodes": 0}
    incomplete = [
        (btrfs_mount, unknown_capacity),
        (btrfs_mount, finite_ext4),
        (ext4_mount, dynamic_btrfs),
    ]
    for mount, capacity in incomplete:
        with (
            mock.patch.object(check, "_mount_facts", return_value=mount),
            mock.patch.object(check, "_capacity_facts", return_value=capacity),
        ):
            payload, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
        assert complete is False
        assert payload["paths"]["spool"]["mount"]["filesystem_type"] == mount["filesystem_type"]
        assert payload["paths"]["spool"]["capacity"]["filesystem_type"] == capacity["filesystem_type"]
    success_probe = {
        "command": ["btrfs", "filesystem", "usage", "-b", "/spool"],
        "exit_status": 0,
        "stdout": "Data,single: 1\nMetadata,DUP: 2\n",
        "stderr": "",
        "timeout_seconds": 30,
        "error": None,
    }
    with (
        mock.patch.object(check, "_source_provenance", return_value=("a" * 40, True)),
        mock.patch.object(check, "_mount_facts", return_value=btrfs_mount),
        mock.patch.object(check, "_capacity_facts", return_value=dynamic_btrfs),
        mock.patch.object(check, "_btrfs_probe", return_value=success_probe),
    ):
        payload, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
    assert complete is True
    assert payload["paths"]["spool"]["btrfs_usage"]["error"] is None


def test_non_btrfs_paths_cannot_qualify_without_allocation_evidence(tmp_path: Path) -> None:
    with (
        mock.patch.object(check, "_source_provenance", return_value=("a" * 40, True)),
        mock.patch.object(check, "_mount_facts", return_value=_payload()["mount"]),
        mock.patch.object(check, "_capacity_facts", return_value=_payload()["capacity"]),
    ):
        payload, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
    assert complete is False
    assert payload["paths"]["spool"]["btrfs_usage"] is None


def test_source_provenance_requires_clean_successful_git_results(tmp_path: Path) -> None:
    revision = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout="a" * 40 + "\n", stderr=""
    )
    clean = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    dirty = subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout=" M scripts/spool_filesystem_check.py\n", stderr=""
    )
    unavailable = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="")

    for status, expected in ((clean, True), (dirty, False), (unavailable, None)):
        with mock.patch.object(check.subprocess, "run", side_effect=(revision, status)):
            assert check._source_provenance() == ("a" * 40, expected)
    with mock.patch.object(check.subprocess, "run", side_effect=OSError("git missing")):
        assert check._source_provenance() == ("unknown", None)

    mount = _payload()["mount"] | {"filesystem_type": "btrfs"}
    capacity = _payload()["capacity"] | {
        "filesystem_type": "btrfs",
        "inode_model": "dynamic",
    }
    with (
        mock.patch.object(check, "_source_provenance", return_value=("a" * 40, False)),
        mock.patch.object(check, "_mount_facts", return_value=mount),
        mock.patch.object(check, "_capacity_facts", return_value=capacity),
        mock.patch.object(check, "_btrfs_probe", return_value={"error": None}),
    ):
        payload, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
    assert complete is False
    assert payload["source_clean"] is False


def test_supplied_candidate_provenance_must_be_available_and_match_source(
    tmp_path: Path,
) -> None:
    missing = check._candidate_reference(tmp_path / "missing.json", "a" * 40)
    assert missing["error"] == "unavailable"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    assert check._candidate_reference(invalid, "a" * 40)["error"] == "unavailable"

    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "receipt_scope": "candidate-preparation",
                "receipt_digest": "sha256:" + "b" * 64,
                "source": {"revision": "a" * 40},
            }
        ),
        encoding="utf-8",
    )
    with mock.patch("scripts.deployment_receipt.validate_receipt"):
        reference = check._candidate_reference(candidate, "a" * 40)
        mismatch = check._candidate_reference(candidate, "c" * 40)
        unknown = check._candidate_reference(candidate, "unknown")
    assert reference["error"] is None
    assert reference["digest"] == check._sha_file(candidate)
    assert mismatch["error"] == "source_mismatch"
    assert unknown["error"] == "source_mismatch"


def test_unavailable_candidate_or_unknown_revision_blocks_qualification(tmp_path: Path) -> None:
    mount = _payload()["mount"]
    capacity = _payload()["capacity"]
    with (
        mock.patch.object(check, "_mount_facts", return_value=mount),
        mock.patch.object(check, "_capacity_facts", return_value=capacity),
    ):
        payload, complete = check.collect(
            tmp_path / "spool", tmp_path / "pg", tmp_path / "missing.json"
        )
    assert complete is False
    assert payload["candidate_receipt"]["error"] == "unavailable"

    with (
        mock.patch.object(check, "_mount_facts", return_value=mount),
        mock.patch.object(check, "_capacity_facts", return_value=capacity),
        mock.patch.object(check, "_source_provenance", return_value=("unknown", None)),
    ):
        payload, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
    assert complete is False
    assert payload["source_revision"] == "unknown"


def test_unknown_identity_or_model_blocks_qualification(tmp_path: Path) -> None:
    base_mount = _payload()["mount"]
    base_capacity = _payload()["capacity"]
    cases = [
        (base_mount | {"filesystem_type": "unknown", "error": "unresolved_or_ambiguous"}, base_capacity),
        (base_mount, base_capacity | {"inode_model": "unknown", "filesystem_type": "unknown"}),
        (base_mount | {"error": "fdinfo_unavailable", "filesystem_type": "unknown"}, base_capacity),
    ]
    for mount, capacity in cases:
        with (
            mock.patch.object(check, "_mount_facts", return_value=mount),
            mock.patch.object(check, "_capacity_facts", return_value=capacity),
        ):
            _, complete = check.collect(tmp_path / "spool", tmp_path / "pg", None)
        assert complete is False


def test_artifact_publication_is_exclusive_and_cleans_partial_pair(tmp_path: Path) -> None:
    output = tmp_path / "evidence.json"
    digest = check._atomic_write_json(output, {"complete": True})
    original = output.read_bytes()
    assert output.stat().st_mode & 0o777 == 0o600
    assert Path(str(output) + ".sha256").stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="occupied"):
        check._atomic_write_json(output, {"replacement": True})
    assert output.read_bytes() == original
    assert Path(str(output) + ".sha256").read_text().startswith(digest)

    partial = tmp_path / "partial.json"
    real_link = check.os.link
    calls = 0

    def fail_second_link(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        real_link(source, destination)

    with (
        mock.patch.object(check.os, "link", side_effect=fail_second_link),
        pytest.raises(RuntimeError, match="atomically"),
    ):
        check._atomic_write_json(partial, {"complete": False})
    assert not partial.exists()
    assert not Path(str(partial) + ".sha256").exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_main_returns_nonzero_for_incomplete_evidence(tmp_path: Path, capsys) -> None:
    output = tmp_path / "out.json"
    spool = tmp_path / "spool"
    pg = tmp_path / "pg"
    spool.mkdir()
    pg.mkdir()
    with mock.patch.object(
        check,
        "collect",
        return_value=({"captured_at": "now", "paths": {}}, False),
    ):
        assert (
            check.main(
                [
                    "--spool-path",
                    str(spool),
                    "--postgres-path",
                    str(pg),
                    "--output",
                    str(output),
                ]
            )
            == 1
        )
    assert output.exists()
    assert Path(str(output) + ".sha256").exists()


def test_main_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        check.main(
            [
                "--spool-path",
                "relative/spool",
                "--postgres-path",
                str(tmp_path / "pg"),
                "--output",
                str(tmp_path / "out.json"),
            ]
        )
