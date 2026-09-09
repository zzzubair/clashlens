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
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python" / "src")]

PROBE_TIMEOUT_SECONDS = 30
PROBE_COMMAND = ("btrfs", "filesystem", "usage", "-b")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> str:
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


def _btrfs_probe(target: Path) -> dict | None:
    try:
        completed = subprocess.run(
            [*PROBE_COMMAND, str(target)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        return {
            "command": [*PROBE_COMMAND, str(target)],
            "exit_status": completed.returncode,
            "stdout": completed.stdout[-16384:],
            "stderr": completed.stderr[-4096:],
            "timeout_seconds": PROBE_TIMEOUT_SECONDS,
            "error": None if completed.returncode == 0 else "probe_failed",
        }
    except FileNotFoundError:
        return {
            "command": [*PROBE_COMMAND, str(target)],
            "exit_status": None,
            "stdout": "",
            "stderr": "",
            "timeout_seconds": PROBE_TIMEOUT_SECONDS,
            "error": "tool_missing",
        }
    except subprocess.TimeoutExpired as error:
        return {
            "command": [*PROBE_COMMAND, str(target)],
            "exit_status": None,
            "stdout": (error.stdout or b"")[-16384:]
            if isinstance(error.stdout, (bytes, str))
            else "",
            "stderr": (error.stderr or b"")[-4096:]
            if isinstance(error.stderr, (bytes, str))
            else "",
            "timeout_seconds": PROBE_TIMEOUT_SECONDS,
            "error": "timeout",
        }
    except OSError as error:
        return {
            "command": [*PROBE_COMMAND, str(target)],
            "exit_status": None,
            "stdout": "",
            "stderr": "",
            "timeout_seconds": PROBE_TIMEOUT_SECONDS,
            "error": f"probe_error:{type(error).__name__}",
        }


def _candidate_reference(path: Path | None) -> dict:
    if path is None:
        return {"path": None, "digest": None}
    try:
        return {"path": str(path), "digest": _sha_file(path)}
    except OSError:
        return {"path": str(path), "digest": None}


def collect(spool_path: Path, postgres_path: Path, candidate_receipt: Path | None) -> tuple[dict, bool]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = "unknown"
    if not revision:
        revision = "unknown"
    entries = {}
    complete = True
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
        "candidate_receipt": _candidate_reference(candidate_receipt),
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
