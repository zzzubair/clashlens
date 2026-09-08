"""Shared spool filesystem capacity classification (stdlib only).

`os.statvfs` reports byte/inode counts but not the filesystem type, so Btrfs
(`0/0` inodes: no fixed pool) is indistinguishable from exhaustion without
mount identity. This helper opens the target descriptor, measures with
`os.fstatvfs(fd)`, and verifies the mount via `/proc/self/fdinfo/<fd>`
`mnt_id` strictly mapped to `/proc/self/mountinfo`:

- `dynamic`: verified Btrfs mount only; the fixed free-inode floor is skipped.
- `finite`: a valid reported pool (total > 0, 0 <= avail <= total); floors apply.
- `unknown`: 0/0, sentinel, inconsistent, or unverified identity; admission refuses.

Btrfs `getattr` reports a per-root `anon_dev` while mountinfo reports
`sb->s_dev`, so `st_dev == major:minor` must never gate Btrfs. Btrfs skips
the device check entirely; non-Btrfs validates `fstat(fd).st_dev` against the
mapped mount device, and a type mismatch degrades to `unknown` while a valid
finite pool stays `finite`. Probe failures (`open`/`fstatvfs`) propagate;
identity failures resolve to `unknown`. Descriptors close on all paths.
No subprocess, no ctypes, no caching across remounts.
"""

from __future__ import annotations

import os
from pathlib import Path

FILESYSTEM_TYPES = ("btrfs", "ext4", "xfs", "other", "unknown")
INODE_MODELS = ("finite", "dynamic", "unknown")

_MOUNTINFO = Path("/proc/self/mountinfo")
_MOUNTINFO_ESCAPES = (
    ("\\040", " "),
    ("\\011", "\t"),
    ("\\012", "\n"),
    ("\\134", "\\"),
)
_SENTINEL_64 = (1 << 64) - 1


def _decode_mountinfo_field(value: str) -> str:
    for escaped, plain in _MOUNTINFO_ESCAPES:
        value = value.replace(escaped, plain)
    return value


def _parse_mountinfo_line(line: str) -> dict[str, object] | None:
    """Parse one mountinfo line, or None when malformed."""
    try:
        pre, post = line.rstrip("\n").split(" - ", 1)
    except ValueError:
        return None
    pre_fields = pre.split()
    post_fields = post.split()
    if len(pre_fields) < 6 or len(post_fields) < 3:
        return None
    try:
        mount_id = int(pre_fields[0])
        parent_id = int(pre_fields[1])
    except ValueError:
        return None
    if ":" not in pre_fields[2]:
        return None
    try:
        major_raw, minor_raw = pre_fields[2].split(":", 1)
        int(major_raw)
        int(minor_raw)
    except ValueError:
        return None
    return {
        "mount_id": mount_id,
        "parent_id": parent_id,
        "major_minor": pre_fields[2],
        "root": _decode_mountinfo_field(pre_fields[3]),
        "mount_point": _decode_mountinfo_field(pre_fields[4]),
        "options": pre_fields[5],
        "filesystem_type": _decode_mountinfo_field(post_fields[0]),
        "source": _decode_mountinfo_field(post_fields[1]),
    }


def _parse_mountinfo_text(text: str) -> list[dict[str, object]] | None:
    mounts: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parsed = _parse_mountinfo_line(line)
        if parsed is None:
            return None
        mounts.append(parsed)
    if not mounts:
        return None
    return mounts


def _read_mounts() -> list[dict[str, object]] | None:
    try:
        text = _MOUNTINFO.read_text(encoding="utf-8", errors="strict")
    except (OSError, ValueError):
        return None
    return _parse_mountinfo_text(text)


def _parse_fdinfo_mnt_id_text(text: str) -> int | None:
    found: int | None = None
    count = 0
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            count += 1
            if count > 1:
                return None
            try:
                found = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return found


def _read_fd_mnt_id(fd: int) -> int | None:
    try:
        text = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="utf-8", errors="strict")
    except (OSError, ValueError):
        return None
    return _parse_fdinfo_mnt_id_text(text)


def _canonical_filesystem_type(raw: object) -> str:
    normalized = str(raw or "").strip().lower()
    if normalized in ("btrfs", "ext4", "xfs"):
        return normalized
    if not normalized:
        return "unknown"
    return "other"


def classify_inode_model(filesystem_type: str, files_total: int, files_avail: int) -> str:
    """Pure classifier shared by the spool and performance evidence."""
    if filesystem_type == "btrfs":
        return "dynamic"
    if (
        not isinstance(files_total, int)
        or not isinstance(files_avail, int)
        or isinstance(files_total, bool)
        or isinstance(files_avail, bool)
    ):
        return "unknown"
    if files_total <= 0 or files_total == _SENTINEL_64 or files_avail == _SENTINEL_64:
        return "unknown"
    if files_avail < 0 or files_avail > files_total:
        return "unknown"
    return "finite"


def _is_prefix(mount_point: str, resolved: str) -> bool:
    if resolved == mount_point:
        return True
    if mount_point == "/":
        return resolved.startswith("/")
    return resolved.startswith(mount_point.rstrip("/") + "/")


def _match_mount(
    resolved: str, mnt_id: int | None, mounts: list[dict[str, object]] | None
) -> dict[str, object] | None:
    """Strictly map fdinfo mnt_id to mountinfo with prefix cross-check.

    Returns the verified entry or None for missing/duplicate/malformed/
    contradictory/unresolved/ambiguous identity.
    """
    if mnt_id is None or mounts is None:
        return None
    by_id = [m for m in mounts if m.get("mount_id") == mnt_id]
    if len(by_id) != 1:
        return None
    entry = by_id[0]
    mount_point = str(entry.get("mount_point", ""))
    if not mount_point or not _is_prefix(mount_point, resolved):
        return None
    # Component-aware most-specific cross-check: the verified mount must be
    # the longest prefix match; ties or a longer unrelated match are ambiguous.
    best_len = -1
    best_ids: list[object] = []
    for mount in mounts:
        point = str(mount.get("mount_point", ""))
        if point and _is_prefix(point, resolved):
            length = len(point)
            if length > best_len:
                best_len = length
                best_ids = [mount.get("mount_id")]
            elif length == best_len:
                best_ids.append(mount.get("mount_id"))
    if best_len < 0 or len(best_ids) != 1 or best_ids[0] != mnt_id:
        return None
    if len(mount_point) != best_len:
        return None
    return entry


def _identify_via_fd(resolved: str, fd: int) -> str:
    """Return canonical type for an open descriptor, or unknown identity."""
    mnt_id = _read_fd_mnt_id(fd)
    mounts = _read_mounts()
    entry = _match_mount(resolved, mnt_id, mounts)
    if entry is None:
        return "unknown"
    raw_type = str(entry.get("filesystem_type", ""))
    if raw_type.strip().lower() == "btrfs":
        return "btrfs"
    # Non-Btrfs: validate fstat device against the mapped mount device.
    # A mismatch degrades the type to unknown; the caller keeps a valid
    # finite pool usable via classify_inode_model.
    try:
        file_stat = os.fstat(fd)
        device = (os.major(file_stat.st_dev), os.minor(file_stat.st_dev))
        major_raw, minor_raw = str(entry.get("major_minor", "")).split(":", 1)
        expected = (int(major_raw), int(minor_raw))
    except (OSError, ValueError):
        # fstat on an open descriptor should not fail; treat validation
        # failure as unknown identity rather than a capacity probe error.
        # Open/fstatvfs errors still propagate from filesystem_capacity.
        return "unknown"
    if (device[0], device[1]) != (expected[0], expected[1]):
        return "unknown"
    return _canonical_filesystem_type(raw_type)


def mount_facts(path: str | Path) -> dict[str, object]:
    """Return verified mount facts for path; identity failures are unknown.

    Opens the resolved target with O_NOFOLLOW, closes on all paths.
    Open failures raise OSError for the caller to mark evidence incomplete.
    """
    target = os.fspath(path)
    resolved = os.path.realpath(target)
    if not os.path.isabs(resolved):
        return {
            "resolved_path": resolved,
            "mount_point": None,
            "source": None,
            "filesystem_type": "unknown",
            "options": None,
            "mnt_id": None,
            "error": "unresolved",
        }
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        mnt_id = _read_fd_mnt_id(fd)
        mounts = _read_mounts()
        entry = _match_mount(resolved, mnt_id, mounts)
        if entry is None:
            reason = "mountinfo_unavailable" if mounts is None else (
                "fdinfo_unavailable" if mnt_id is None else "unresolved_or_ambiguous"
            )
            return {
                "resolved_path": resolved,
                "mount_point": None,
                "source": None,
                "filesystem_type": "unknown",
                "options": None,
                "mnt_id": mnt_id,
                "error": reason,
            }
        filesystem_type = _identify_via_fd(resolved, fd)
        if filesystem_type == "unknown":
            return {
                "resolved_path": resolved,
                "mount_point": str(entry.get("mount_point")),
                "source": str(entry.get("source")),
                "filesystem_type": "unknown",
                "options": str(entry.get("options")),
                "mnt_id": mnt_id,
                "error": "identity_mismatch_or_ambiguous",
            }
        return {
            "resolved_path": resolved,
            "mount_point": str(entry.get("mount_point")),
            "source": str(entry.get("source")),
            "filesystem_type": filesystem_type,
            "options": str(entry.get("options")),
            "mnt_id": mnt_id,
            "error": None,
        }
    finally:
        os.close(fd)


def filesystem_capacity(path: str | Path) -> dict[str, object]:
    """Return truthful capacity facts with explicit inode-model meaning.

    Opens the resolved target once, measures with fstatvfs, verifies the
    mount via fdinfo mnt_id. Open/fstatvfs failures propagate; identity
    failures degrade the type to unknown while a valid pool stays finite.
    """
    target = os.fspath(path)
    resolved = os.path.realpath(target)
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        filesystem = os.fstatvfs(fd)
        total = int(filesystem.f_files)
        avail = int(filesystem.f_favail)
        free_bytes = int(filesystem.f_bavail * filesystem.f_frsize)
        block_size = int(filesystem.f_frsize)
        filesystem_type = _identify_via_fd(resolved, fd)
    finally:
        os.close(fd)
    inode_model = classify_inode_model(filesystem_type, total, avail)
    return {
        "filesystem_type": filesystem_type,
        "inode_model": inode_model,
        "free_bytes": free_bytes,
        "free_inodes": int(avail),
        "inode_total": int(total),
        "block_size": block_size,
    }


__all__ = [
    "FILESYSTEM_TYPES",
    "INODE_MODELS",
    "classify_inode_model",
    "filesystem_capacity",
    "mount_facts",
]
