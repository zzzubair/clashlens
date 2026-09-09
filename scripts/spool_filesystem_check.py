#!/usr/bin/env python3
"""Capture spool/PostgreSQL filesystem capacity evidence (stdlib only).

Retains mount identity, byte/inode facts, and raw Btrfs data/metadata usage
for `rogue` host qualification. Different Btrfs subvolumes may share the same
allocation pool; do not sum shared filesystem totals.

Example:
  python3 scripts/spool_filesystem_check.py \
    --spool-path /var/lib/clashlens/spool \
    --postgres-path /var/lib/clashlens/postgres \
    --output /retained/spool-filesystem.json \
    --candidate-receipt /retained/clashlens-candidate-preparation.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "python" / "src")]

from scripts import deployment_receipt

PROBE_TIMEOUT_SECONDS = 30
PROBE_COMMAND = ("btrfs", "filesystem", "usage", "-b")
PROBE_STDOUT_LIMIT = 65536
PROBE_STDERR_LIMIT = 16384


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> str:
    """Publish a complete artifact and digest without replacing evidence."""
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=1, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    sidecar = Path(str(path) + ".sha256")
    temporary_paths: list[Path] = []
    linked: list[Path] = []
    published = False
    try:
        for destination, content in (
            (path, text),
            (sidecar, digest + "  " + path.name + "\n"),
        ):
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=path.parent
            )
            temporary = Path(temporary_name)
            temporary_paths.append(temporary)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, destination in zip(temporary_paths, (path, sidecar), strict=True):
            os.link(temporary, destination)
            linked.append(destination)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if _sha_file(path) != digest or sidecar.read_text(encoding="utf-8") != (
            digest + "  " + path.name + "\n"
        ):
            raise OSError("published evidence verification failed")
        published = True
        return digest
    except FileExistsError as error:
        raise RuntimeError("evidence output is already occupied") from error
    except (OSError, UnicodeError) as error:
        raise RuntimeError("evidence could not be written atomically") from error
    finally:
        if not published:
            for destination in reversed(linked):
                destination.unlink(missing_ok=True)
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def _mount_facts(target: Path) -> dict:
    from clashlens.filesystem import mount_facts as shared_mount_facts

    try:
        facts = shared_mount_facts(target)
        return {
            "resolved_path": str(facts.get("resolved_path")),
            "mount_point": facts.get("mount_point"),
            "source": facts.get("source"),
            "filesystem_type": str(facts.get("filesystem_type", "unknown")),
            "options": facts.get("options"),
            "mnt_id": facts.get("mnt_id"),
            "error": facts.get("error"),
        }
    except OSError as error:
        return {
            "resolved_path": os.path.realpath(target),
            "mount_point": None,
            "source": None,
            "filesystem_type": "unknown",
            "options": None,
            "mnt_id": None,
            "error": f"mount_probe_failed:{type(error).__name__}",
        }


def _capacity_facts(target: Path) -> dict:
    from clashlens.filesystem import filesystem_capacity

    try:
        capacity = filesystem_capacity(str(target))
        return {
            "free_bytes": int(capacity["free_bytes"]),
            "free_inodes": int(capacity["free_inodes"]),
            "inode_total": int(capacity["inode_total"]),
            "filesystem_type": str(capacity["filesystem_type"]),
            "inode_model": str(capacity["inode_model"]),
            "error": None,
        }
    except OSError as error:
        return {
            "free_bytes": None,
            "free_inodes": None,
            "inode_total": None,
            "filesystem_type": "unknown",
            "inode_model": "unknown",
            "error": f"statvfs_failed:{type(error).__name__}",
        }


def _probe_text(value: str | bytes | None, limit: int) -> tuple[str, bool]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value or ""
    return value[:limit], len(value) > limit


def _allocation_evidence(stdout: str) -> str | None:
    kinds = {
        line.lstrip().split(",", 1)[0]
        for line in stdout.splitlines()
        if "," in line
    }
    if "Data+Metadata" in kinds:
        return "combined"
    if {"Data", "Metadata"}.issubset(kinds):
        return "separate"
    return None


def _btrfs_result(
    target: Path,
    *,
    exit_status: int | None,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    error: str | None,
) -> dict:
    stdout_text, stdout_truncated = _probe_text(stdout, PROBE_STDOUT_LIMIT)
    stderr_text, stderr_truncated = _probe_text(stderr, PROBE_STDERR_LIMIT)
    allocation_evidence = _allocation_evidence(stdout_text)
    if error is None:
        if not stdout_text.strip():
            error = "empty_output"
        elif stdout_truncated or stderr_truncated:
            error = "output_truncated"
        elif allocation_evidence is None:
            error = "allocation_evidence_missing"
    return {
        "command": [*PROBE_COMMAND, str(target)],
        "exit_status": exit_status,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "allocation_evidence": allocation_evidence,
        "timeout_seconds": PROBE_TIMEOUT_SECONDS,
        "error": error,
    }


def _btrfs_probe(target: Path) -> dict:
    try:
        completed = subprocess.run(
            [*PROBE_COMMAND, str(target)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        return _btrfs_result(
            target,
            exit_status=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=None if completed.returncode == 0 else "probe_failed",
        )
    except FileNotFoundError:
        return _btrfs_result(
            target, exit_status=None, stdout="", stderr="", error="tool_missing"
        )
    except subprocess.TimeoutExpired as error:
        return _btrfs_result(
            target,
            exit_status=None,
            stdout=error.stdout,
            stderr=error.stderr,
            error="timeout",
        )
    except OSError as error:
        return _btrfs_result(
            target,
            exit_status=None,
            stdout="",
            stderr="",
            error=f"probe_error:{type(error).__name__}",
        )


def _candidate_reference(path: Path | None, source_revision: str) -> dict:
    if path is None:
        return {"path": None, "digest": None, "receipt_digest": None, "error": None}
    reference = {
        "path": str(path),
        "digest": None,
        "receipt_digest": None,
        "error": "unavailable",
    }
    try:
        raw = path.read_bytes()
        receipt = json.loads(raw)
        deployment_receipt.validate_receipt(receipt, require_digest=True)
        reference["digest"] = hashlib.sha256(raw).hexdigest()
        reference["receipt_digest"] = receipt["receipt_digest"]
        if receipt["receipt_scope"] != "candidate-preparation":
            reference["error"] = "wrong_scope"
        elif source_revision == "unknown" or receipt["source"]["revision"] != source_revision:
            reference["error"] = "source_mismatch"
        else:
            reference["error"] = None
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        deployment_receipt.ReceiptError,
        KeyError,
        TypeError,
    ):
        # Supplied provenance is untrusted evidence. Keep only a bounded status.
        return reference
    return reference


def collect(spool_path: Path, postgres_path: Path, candidate_receipt: Path | None) -> tuple[dict, bool]:
    try:
        revision_result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        revision = revision_result.stdout.strip() if revision_result.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        revision = "unknown"
    if len(revision) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in revision
    ):
        revision = "unknown"
    candidate = _candidate_reference(candidate_receipt, revision)
    entries = {}
    complete = revision != "unknown" and candidate["error"] is None
    for name, target in (("spool", spool_path), ("postgres", postgres_path)):
        mount = _mount_facts(target)
        capacity = _capacity_facts(target)
        # Host qualification is strict: unknown/unresolved/ambiguous mount
        # identity or an unknown inode model blocks acceptance, even though
        # the runtime may still use a valid finite pool as finite.
        if mount.get("error") is not None or mount.get("filesystem_type") == "unknown":
            complete = False
        if capacity.get("error") is not None or capacity.get("inode_model") == "unknown":
            complete = False
        if capacity.get("filesystem_type") == "unknown" or (
            mount.get("error") is None
            and mount.get("filesystem_type") != "unknown"
            and capacity.get("filesystem_type") != mount.get("filesystem_type")
        ):
            complete = False
        is_btrfs = (
            mount.get("error") is None
            and mount.get("filesystem_type") == "btrfs"
            and capacity.get("filesystem_type") == "btrfs"
            and capacity.get("inode_model") == "dynamic"
        )
        probe = _btrfs_probe(target) if is_btrfs else None
        if probe is not None and probe["error"] is not None:
            complete = False
        entries[name] = {
            "requested_path": str(target),
            "mount": mount,
            "capacity": capacity,
            "btrfs_usage": probe,
        }
    payload = {
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "host": {"platform": platform.platform(), "uname": dict(platform.uname()._asdict())},
        "source_revision": revision,
        "candidate_receipt": candidate,
        "paths": entries,
        "notes": (
            "Different Btrfs subvolumes may share the same allocation pool; "
            "do not sum shared filesystem totals. `df` free bytes alone do "
            "not prove Btrfs metadata headroom; missing data/metadata "
            "evidence blocks host acceptance, not code merge."
        ),
    }
    return payload, complete


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool-path", type=Path, required=True)
    parser.add_argument("--postgres-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-receipt", type=Path, default=None)
    arguments = parser.parse_args(argv)
    for path in (arguments.spool_path, arguments.postgres_path):
        if not path.is_absolute():
            parser.error("spool and postgres paths must be absolute")
    if arguments.candidate_receipt is not None and not arguments.candidate_receipt.is_absolute():
        parser.error("candidate receipt path must be absolute")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    payload, complete = collect(
        arguments.spool_path, arguments.postgres_path, arguments.candidate_receipt
    )
    _atomic_write_json(arguments.output, payload)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
