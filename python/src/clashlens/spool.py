from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any

STRIPE_COUNT = 4096
LEDGER_FIELDS = (
    "final_bytes",
    "final_objects",
    "temporary_bytes",
    "temporary_objects",
    "abandoned_temp_bytes",
    "abandoned_temp_objects",
    "reserved_bytes",
    "reserved_objects",
    "high_water_bytes",
)


class SpoolError(RuntimeError):
    pass



def _fsync_dir(fd: int) -> None:
    os.fsync(fd)




def validate_root(root: str | Path) -> Path:
    path = Path(root)
    if not path.is_absolute() or path == Path("/"):
        raise ValueError("spool root must be an absolute non-root path")
    candidate = Path(path.anchor)
    for part in path.parts[1:]:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError("spool root must not traverse a symlink")
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError("spool root must be a real directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("spool root must not be a symlink")
    return path


class Spool:
    """Bounded shared spool using the Go collector's stripe/capacity protocol."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_body_bytes: int,
        max_bytes: int = 16 << 30,
        max_objects: int = 1_000_000,
        free_space_floor: int = 0,
        free_inode_floor: int = 0,
    ) -> None:
        if max_body_bytes <= 0 or max_bytes <= 0 or max_objects <= 0:
            raise ValueError("spool limits must be positive")
        if free_space_floor < 0 or free_inode_floor < 0:
            raise ValueError("spool floors must not be negative")
        self.root = validate_root(root)
        self.max_body_bytes = max_body_bytes
        self.max_bytes = max_bytes
        self.max_objects = max_objects
        self.free_space_floor = free_space_floor
        self.free_inode_floor = free_inode_floor
        # Trusted root descriptor: every descendant access below walks from
        # this inode with O_NOFOLLOW so no absolute traversal can be
        # redirected by a substitution race after startup validation.
        self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._ensure_descendants()
            self._capacity = self._child_fd("capacity.lock", os.O_RDWR, ".control")
        except BaseException:
            os.close(self._root_fd)
            raise
        self._capacity_mutex = threading.RLock()
        self.reconcile()

    def _ensure_descendants(self) -> None:
        """Create control, lock, temporary, and final directories relative to
        the trusted root descriptor; never through absolute descendant paths."""
        chains = (
            (".locks",),
            (".control", "reservations"),
            (".control", "operations"),
            ("tmp",),
            ("sha256",),
        )
        for chain in chains:
            fd = os.dup(self._root_fd)
            try:
                for part in chain:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    nxt = os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    os.close(fd)
                    fd = nxt
            finally:
                os.close(fd)
        control_fd = self._sub_dir_fd(".control")
        try:
            descriptor = os.open(
                "capacity.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=control_fd,
            )
            os.close(descriptor)
        finally:
            os.close(control_fd)
        locks_fd = self._sub_dir_fd(".locks")
        try:
            for index in range(STRIPE_COUNT):
                descriptor = os.open(
                    f"{index:04x}",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=locks_fd,
                )
                os.close(descriptor)
        finally:
            os.close(locks_fd)

    def close(self) -> None:
        os.close(self._capacity)
        os.close(self._root_fd)

    def _sub_dir_fd(self, *parts: str) -> int:
        """Open a descendant directory relative to the trusted root fd."""
        fd = os.dup(self._root_fd)
        try:
            for part in parts:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _child_fd(self, name: str, flags: int, *parts: str, mode: int = 0o600) -> int:
        """Open a descendant file relative to the trusted root fd."""
        directory_fd = self._sub_dir_fd(*parts)
        try:
            return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def _read_child(self, name: str, *parts: str) -> bytes:
        fd = self._child_fd(name, os.O_RDONLY, *parts)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        return b"".join(chunks)

    def _unlink_at(self, name: str, *parts: str) -> None:
        directory_fd = self._sub_dir_fd(*parts)
        try:
            os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def _open_unique_at(self, parent_fd: int, prefix: str, suffix: str, mode: int = 0o600) -> tuple[int, str]:
        for _ in range(32):
            name = f"{prefix}{os.getpid()}-{os.urandom(12).hex()}{suffix}"
            try:
                return (
                    os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, mode, dir_fd=parent_fd),
                    name,
                )
            except FileExistsError:
                continue
        raise SpoolError("could not allocate a private spool name")

    def _safe(self, path: Path) -> Path:
        try:
            relative = path.relative_to(self.root)
        except ValueError as error:
            raise SpoolError("spool path escapes root") from error
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise SpoolError("symlink beneath spool root")
        return path

    def _final(self, digest: str) -> Path:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise SpoolError("invalid evidence hash")
        return self._safe(self.root / "sha256" / digest[:2] / digest)

    def _stripe_path(self, digest: str) -> Path:
        return self._safe(self.root / ".locks" / f"{int(digest[:3], 16) & 0xFFF:04x}")

    @contextmanager
    def _capacity_lock(self) -> Iterator[None]:
        with self._capacity_mutex:
            fcntl.flock(self._capacity, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._capacity, fcntl.LOCK_UN)

    @contextmanager
    def lock(self, digest: str, *, exclusive: bool = False) -> Iterator[None]:
        fd = self._child_fd(f"{int(digest[:3], 16) & 0xFFF:04x}", os.O_RDWR, ".locks")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_ledger_locked(self) -> dict[str, int]:
        try:
            data = json.loads(self._read_child("capacity.json", ".control").decode("utf-8"))
        except FileNotFoundError:
            data = {}
        if not isinstance(data, dict):
            raise SpoolError("invalid capacity ledger")
        return {field: int(data.get(field, 0)) for field in LEDGER_FIELDS}

    def _write_ledger_locked(self, ledger: dict[str, int]) -> None:
        payload = {field: max(0, int(ledger.get(field, 0))) for field in LEDGER_FIELDS}
        control_fd = self._sub_dir_fd(".control")
        try:
            descriptor, temporary_name = self._open_unique_at(control_fd, "capacity-", ".tmp")
            try:
                os.write(descriptor, json.dumps(payload, sort_keys=True).encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.rename(temporary_name, "capacity.json", src_dir_fd=control_fd, dst_dir_fd=control_fd)
            finally:
                _fsync_dir(control_fd)
        finally:
            os.close(control_fd)

    def _scan_locked(self) -> dict[str, int]:
        ledger = {field: 0 for field in LEDGER_FIELDS}
        sha_fd = self._sub_dir_fd("sha256")
        try:
            for prefix in os.listdir(sha_fd):
                if len(prefix) != 2:
                    continue
                prefix_fd = self._sub_dir_fd("sha256", prefix)
                try:
                    for name in os.listdir(prefix_fd):
                        info = os.stat(name, dir_fd=prefix_fd, follow_symlinks=False)
                        import stat as stat_module
                        if not stat_module.S_ISREG(info.st_mode):
                            raise SpoolError("unsafe final spool path")
                        ledger["final_bytes"] += info.st_size
                        ledger["final_objects"] += 1
                finally:
                    os.close(prefix_fd)
        finally:
            os.close(sha_fd)
        tmp_fd = self._sub_dir_fd("tmp")
        try:
            for name in os.listdir(tmp_fd):
                info = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
                import stat as stat_module
                if not stat_module.S_ISREG(info.st_mode):
                    raise SpoolError("unsafe temporary spool path")
                ledger["temporary_bytes"] += info.st_size
                ledger["temporary_objects"] += 1
        finally:
            os.close(tmp_fd)
        reservations_fd = self._sub_dir_fd(".control", "reservations")
        try:
            for name in sorted(os.listdir(reservations_fd)):
                if not name.endswith(".json"):
                    continue
                info = os.stat(name, dir_fd=reservations_fd, follow_symlinks=False)
                import stat as stat_module
                if not stat_module.S_ISREG(info.st_mode):
                    raise SpoolError("unsafe reservation path")
                record = json.loads(self._read_child(name, ".control", "reservations").decode("utf-8"))
                ledger["reserved_bytes"] += int(record.get("limit", 0))
                ledger["reserved_objects"] += 1
        except FileNotFoundError:
            pass
        finally:
            os.close(reservations_fd)
        ledger["high_water_bytes"] = max(
            ledger["high_water_bytes"],
            ledger["final_bytes"] + ledger["temporary_bytes"] + ledger["reserved_bytes"],
        )
        return ledger

    def reconcile(self) -> dict[str, int]:
        # Startup reconciliation takes every stripe before capacity, matching Go.
        fds: list[int] = []
        try:
            for index in range(STRIPE_COUNT):
                fd = self._child_fd(f"{index:04x}", os.O_RDWR, ".locks")
                fcntl.flock(fd, fcntl.LOCK_EX)
                fds.append(fd)
            with self._capacity_lock():
                operations_fd = self._sub_dir_fd(".control", "operations")
                try:
                    for name in list(os.listdir(operations_fd)):
                        if not name.endswith(".json"):
                            continue
                        fd = -1
                        try:
                            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=operations_fd)
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                os.close(fd)
                                fd = -1
                                continue
                            record = json.loads(self._read_child(name, ".control", "operations").decode("utf-8"))
                            temporary = record.get("temporary_path")
                            if isinstance(temporary, str) and temporary:
                                parts = [part for part in temporary.split("/") if part]
                                self._unlink_at(parts.pop(), *parts)
                        except (FileNotFoundError, json.JSONDecodeError, SpoolError, OSError):
                            if fd >= 0:
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                            continue
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)
                        os.unlink(name, dir_fd=operations_fd)
                    _fsync_dir(operations_fd)
                finally:
                    os.close(operations_fd)
                ledger = self._scan_locked()
                self._write_ledger_locked(ledger)
                return ledger
        finally:
            for fd in reversed(fds):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _sweep_dead_reservations_locked(self, ledger: dict[str, int]) -> None:
        """Remove unlocked (dead) reservation records under the capacity lock.

        A reservation whose descriptor no longer holds its exclusive flock was
        left by a dead process; admission reconciles it away instead of letting
        stale reserved bytes wedge future work.
        """
        reservations_fd = self._sub_dir_fd(".control", "reservations")
        try:
            for name in sorted(os.listdir(reservations_fd)):
                if not name.endswith(".json"):
                    continue
                try:
                    info = os.stat(name, dir_fd=reservations_fd, follow_symlinks=False)
                    import stat as stat_module
                    if not stat_module.S_ISREG(info.st_mode):
                        continue
                    fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=reservations_fd)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)  # live reservation owned by another process
                    continue
                try:
                    record = json.loads(self._read_child(name, ".control", "reservations").decode("utf-8"))
                    limit = max(0, int(record.get("limit", 0)))
                except (FileNotFoundError, json.JSONDecodeError, ValueError):
                    limit = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                os.unlink(name, dir_fd=reservations_fd)
                ledger["reserved_bytes"] = max(0, ledger["reserved_bytes"] - limit)
                ledger["reserved_objects"] = max(0, ledger["reserved_objects"] - 1)
        finally:
            os.close(reservations_fd)

    def _reserve(self, limit: int) -> tuple[int, str]:
        with self._capacity_lock():
            ledger = self._read_ledger_locked()
            self._sweep_dead_reservations_locked(ledger)
            if (
                ledger["final_bytes"] + ledger["temporary_bytes"] + ledger["abandoned_temp_bytes"] + ledger["reserved_bytes"] + limit
                > self.max_bytes
                or ledger["final_objects"] + ledger["temporary_objects"] + ledger["reserved_objects"] + 1
                > self.max_objects
            ):
                raise SpoolError("degraded_capacity: spool reservation denied")
            filesystem = os.statvfs(self.root)
            if self.free_space_floor > 0 and filesystem.f_bavail * filesystem.f_frsize < self.free_space_floor + limit:
                raise SpoolError("degraded_capacity: spool free-space floor reached")
            if self.free_inode_floor > 0 and filesystem.f_favail < self.free_inode_floor + 1:
                raise SpoolError("degraded_capacity: spool free-inode floor reached")
            reservations_fd = self._sub_dir_fd(".control", "reservations")
            try:
                fd, name = self._open_unique_at(reservations_fd, "reservation-", ".json")
            finally:
                os.close(reservations_fd)
            try:
                # The live record is identified by its exclusively flocked
                # descriptor for its whole lifetime; the Go collector treats an
                # unlocked record as crash debris, so take the flock before
                # publishing any content.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                record = {"limit": limit, "size": 0, "hash": "", "operation": "write", "temporary_path": "", "created_at": int(time() * 1_000_000_000)}
                os.write(fd, json.dumps(record, sort_keys=True).encode())
                os.fsync(fd)
                reservations_fd = self._sub_dir_fd(".control", "reservations")
                try:
                    _fsync_dir(reservations_fd)
                finally:
                    os.close(reservations_fd)
                ledger["reserved_bytes"] += limit
                ledger["reserved_objects"] += 1
                ledger["high_water_bytes"] = max(ledger["high_water_bytes"], ledger["final_bytes"] + ledger["temporary_bytes"] + ledger["reserved_bytes"])
                self._write_ledger_locked(ledger)
                return fd, name
            except Exception:
                os.close(fd)
                try:
                    self._unlink_at(name, ".control", "reservations")
                except FileNotFoundError:
                    pass
                raise

    def _release(self, fd: int, name: str, *, actual_temp_bytes: int = 0) -> None:
        with self._capacity_lock():
            ledger = self._read_ledger_locked()
            record: dict[str, Any] = {}
            try:
                record = json.loads(self._read_child(name, ".control", "reservations").decode("utf-8"))
            except FileNotFoundError:
                pass
            limit = int(record.get("limit", self.max_body_bytes))
            ledger["reserved_bytes"] = max(0, ledger["reserved_bytes"] - limit)
            ledger["reserved_objects"] = max(0, ledger["reserved_objects"] - 1)
            if actual_temp_bytes:
                ledger["abandoned_temp_bytes"] += actual_temp_bytes
                ledger["abandoned_temp_objects"] += 1
            self._write_ledger_locked(ledger)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            self._unlink_at(name, ".control", "reservations")
            reservations_fd = self._sub_dir_fd(".control", "reservations")
            try:
                _fsync_dir(reservations_fd)
            finally:
                os.close(reservations_fd)

    def _verify_unlocked(self, digest: str, expected_size: int | None = None) -> bytes | None:
        self._final(digest)  # hash validation
        try:
            parent_fd = self._sub_dir_fd("sha256", digest[:2])
            try:
                fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            body = stream.read(self.max_body_bytes + 1)
        if len(body) > self.max_body_bytes or (expected_size is not None and len(body) != expected_size):
            return None
        return body if hashlib.sha256(body).hexdigest() == digest else None

    def verify(self, digest: str, expected_size: int | None = None) -> bytes | None:
        with self.lock(digest):
            return self._verify_unlocked(digest, expected_size)

    def publish(self, body: bytes, digest: str) -> None:
        if len(body) > self.max_body_bytes:
            raise SpoolError("archive body exceeds configured limit")
        if hashlib.sha256(body).hexdigest() != digest:
            raise SpoolError("archive checksum mismatch")
        if self.verify(digest, len(body)) is not None:
            return
        reservation_fd, reservation_name = self._reserve(self.max_body_bytes)
        operation_name: str | None = None
        fd = -1
        temporary_name = ""
        tmp_fd = -1
        prefix_fd = -1
        try:
            tmp_fd = self._sub_dir_fd("tmp")
            fd, temporary_name = self._open_unique_at(tmp_fd, "evidence-", ".tmp")
            os.fchmod(fd, 0o600)
            record = {"limit": self.max_body_bytes, "size": len(body), "hash": digest, "operation": "write", "temporary_path": f"tmp/{temporary_name}", "created_at": int(time() * 1_000_000_000)}
            with self._capacity_lock():
                os.ftruncate(reservation_fd, 0)
                os.lseek(reservation_fd, 0, os.SEEK_SET)
                os.write(reservation_fd, json.dumps(record, sort_keys=True).encode())
                os.fsync(reservation_fd)
                ledger = self._read_ledger_locked()
                ledger["temporary_bytes"] += len(body)
                ledger["temporary_objects"] += 1
                ledger["high_water_bytes"] = max(ledger["high_water_bytes"], ledger["final_bytes"] + ledger["temporary_bytes"] + ledger["reserved_bytes"])
                self._write_ledger_locked(ledger)
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise SpoolError("short spool write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            self._final(digest)
            prefix_fd = self._sub_dir_fd("sha256")
            try:
                os.mkdir(digest[:2], mode=0o700, dir_fd=prefix_fd)
            except FileExistsError:
                pass
            with self.lock(digest, exclusive=True):
                operations_fd = self._sub_dir_fd(".control", "operations")
                try:
                    operation_fd, operation_name = self._open_unique_at(operations_fd, "operation-", ".json")
                    os.write(operation_fd, json.dumps({"operation": "publish", "hash": digest, "temporary_path": f"tmp/{temporary_name}"}).encode())
                    os.fsync(operation_fd)
                    _fsync_dir(operations_fd)
                except Exception:
                    os.close(operation_fd)
                    if operation_name is not None:
                        try:
                            os.unlink(operation_name, dir_fd=operations_fd)
                        except FileNotFoundError:
                            pass
                        operation_name = None
                    raise
                finally:
                    os.close(operations_fd)
                with self._capacity_lock():
                    winner = self._verify_unlocked(digest, len(body))
                    if winner is None:
                        temporary_dir_fd = self._sub_dir_fd("tmp")
                        final_dir_fd = self._sub_dir_fd("sha256", digest[:2])
                        try:
                            try:
                                os.link(temporary_name, digest, src_dir_fd=temporary_dir_fd, dst_dir_fd=final_dir_fd)
                            except FileExistsError:
                                if self._verify_unlocked(digest, len(body)) is None:
                                    # Both directory descriptors were opened
                                    # without symlink traversal; replacement is
                                    # relative to those trusted inodes.
                                    os.unlink(digest, dir_fd=final_dir_fd)
                                    os.link(temporary_name, digest, src_dir_fd=temporary_dir_fd, dst_dir_fd=final_dir_fd)
                            _fsync_dir(final_dir_fd)
                        finally:
                            os.close(temporary_dir_fd)
                            os.close(final_dir_fd)
                    self._unlink_at(temporary_name, "tmp")
                    ledger = self._scan_locked()
                    ledger["reserved_bytes"] = max(0, ledger["reserved_bytes"] - self.max_body_bytes)
                    ledger["reserved_objects"] = max(0, ledger["reserved_objects"] - 1)
                    ledger["high_water_bytes"] = max(ledger["high_water_bytes"], ledger["final_bytes"] + ledger["temporary_bytes"] + ledger["reserved_bytes"])
                    self._write_ledger_locked(ledger)
                    fcntl.flock(reservation_fd, fcntl.LOCK_UN)
                    os.close(reservation_fd)
                    reservation_fd = -1
                    self._unlink_at(reservation_name, ".control", "reservations")
                    reservations_fd = self._sub_dir_fd(".control", "reservations")
                    try:
                        _fsync_dir(reservations_fd)
                    finally:
                        os.close(reservations_fd)
                fcntl.flock(operation_fd, fcntl.LOCK_UN)
                os.close(operation_fd)
                self._unlink_at(operation_name, ".control", "operations")
                operations_fd = self._sub_dir_fd(".control", "operations")
                try:
                    _fsync_dir(operations_fd)
                finally:
                    os.close(operations_fd)
                operation_name = None
        except BaseException:
            if fd >= 0:
                os.close(fd)
            if temporary_name:
                try:
                    self._unlink_at(temporary_name, "tmp")
                except OSError:
                    pass
            if operation_fd >= 0:
                fcntl.flock(operation_fd, fcntl.LOCK_UN)
                os.close(operation_fd)
                if operation_name is not None:
                    try:
                        self._unlink_at(operation_name, ".control", "operations")
                    except FileNotFoundError:
                        pass
            if tmp_fd >= 0:
                os.close(tmp_fd)
            if prefix_fd >= 0:
                os.close(prefix_fd)
            if reservation_fd >= 0:
                try:
                    self.reconcile()
                finally:
                    self._release(reservation_fd, reservation_name, actual_temp_bytes=0)
            raise

    def stats(self) -> dict[str, int]:
        with self._capacity_lock():
            ledger = self._scan_locked()
            self._write_ledger_locked(ledger)
            filesystem = os.statvfs(self.root)
            ledger["free_inodes"] = filesystem.f_favail
            ledger["free_bytes"] = filesystem.f_bavail * filesystem.f_frsize
            ledger["allocated_blocks"] = (ledger["final_bytes"] + ledger["temporary_bytes"] + filesystem.f_frsize - 1) // filesystem.f_frsize
            return ledger.copy()

    def readiness(self) -> tuple[bool, str]:
        try:
            stats = self.stats()
        except (OSError, ValueError, SpoolError) as error:
            return False, f"storage_error:{type(error).__name__}"
        logical = stats["final_bytes"] + stats["temporary_bytes"] + stats["abandoned_temp_bytes"] + stats["reserved_bytes"]
        objects = stats["final_objects"] + stats["temporary_objects"] + stats["reserved_objects"]
        if logical + self.max_body_bytes > self.max_bytes or objects + 1 > self.max_objects:
            return False, "degraded_capacity"
        if stats["free_bytes"] < self.free_space_floor + self.max_body_bytes:
            return False, "degraded_free_space"
        if stats["free_inodes"] < self.free_inode_floor + 1:
            return False, "degraded_free_inodes"
        return True, "ready"

    def cleanup_stale(self, age_seconds: float) -> int:
        if age_seconds <= 0:
            raise ValueError("stale age must be positive")
        removed = 0
        now = time()
        tmp_fd = self._sub_dir_fd("tmp")
        try:
            for name in os.listdir(tmp_fd):
                info = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
                if now - info.st_mtime <= age_seconds:
                    continue
                with self._capacity_lock():
                    os.unlink(name, dir_fd=tmp_fd)
                    removed += 1
        except FileNotFoundError:
            pass
        finally:
            os.close(tmp_fd)
        if removed:
            self.stats()
        return removed


__all__ = ["Spool", "SpoolError", "validate_root"]
