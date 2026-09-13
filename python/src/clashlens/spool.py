from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import Any, Self

from .filesystem import filesystem_capacity


class SpoolError(RuntimeError):
    """A spool admission, integrity, or safety failure."""


def _fsync_dir(fd: int) -> None:
    os.fsync(fd)


def validate_root(root: str | Path) -> Path:
    path = Path(root)
    if not path.is_absolute() or path == Path("/"):
        raise ValueError("spool root must be an absolute non-root path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("spool root must not traverse a symlink")
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ValueError("spool root must be a real directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("spool root must be a real directory")
    os.chmod(path, 0o700)
    return path


class SpoolReservation:
    def __init__(self, spool: Spool, limit: int) -> None:
        self.spool = spool
        self.limit = limit
        self._active = False
        self._temporary_name: str | None = None

    @property
    def active(self) -> bool:
        return self._active

    def __enter__(self) -> Self:
        if not self._active:
            self.spool._activate_reservation(self)
        return self

    def __exit__(
        self, _exception_type: object, _exception: object, _traceback: object
    ) -> None:
        self.release()

    def release(self) -> None:
        self.spool._release_reservation(self)

    def publish(self, body: bytes, digest: str) -> None:
        self.spool.publish(body, digest, reservation=self)


class Spool:
    """Bounded private spool with one flock and actual-file reconciliation."""

    _COUNT_KEYS = (
        "final_bytes",
        "final_objects",
        "temporary_bytes",
        "temporary_objects",
        "abandoned_temp_bytes",
        "abandoned_temp_objects",
    )

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
        self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._capacity_mutex = threading.RLock()
        self._capacity_depth = 0
        self._publication_condition = threading.Condition()
        self._active_publications = 0
        self._cleanup_active = False
        self._reservations: dict[int, SpoolReservation] = {}
        self._actual_counts = {key: 0 for key in self._COUNT_KEYS}
        self._temporary_sizes: dict[str, int | None] = {}
        self._high_water_bytes = 0
        self._closed = False
        try:
            self._ensure_descendants()
            self._capacity = self._child_fd(
                "capacity.lock", os.O_CREAT | os.O_RDWR, ".control"
            )
            self.reconcile()
        except BaseException:
            os.close(self._root_fd)
            raise

    def _ensure_descendants(self) -> None:
        for chain in ((".control",), (".handoff",), ("tmp",), ("sha256",)):
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
                    os.fchmod(nxt, 0o700)
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
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
        finally:
            os.close(control_fd)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._capacity_mutex:
            for reservation in self._reservations.values():
                reservation._active = False
            self._reservations.clear()
            os.close(self._capacity)
            os.close(self._root_fd)

    def _sub_dir_fd(self, *parts: str) -> int:
        fd = os.dup(self._root_fd)
        try:
            for part in parts:
                nxt = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = nxt
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _child_fd(self, name: str, flags: int, *parts: str, mode: int = 0o600) -> int:
        directory_fd = self._sub_dir_fd(*parts)
        try:
            return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def _open_unique_at(
        self, parent_fd: int, prefix: str, suffix: str, mode: int = 0o600
    ) -> tuple[int, str]:
        for _ in range(32):
            name = f"{prefix}{os.getpid()}-{os.urandom(12).hex()}{suffix}"
            try:
                return (
                    os.open(
                        name,
                        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                        mode,
                        dir_fd=parent_fd,
                    ),
                    name,
                )
            except FileExistsError:
                continue
        raise SpoolError("could not allocate a private spool name")

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise SpoolError("short spool write")
            view = view[written:]

    @staticmethod
    def _read_all(fd: int, limit: int | None = None) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
            size += len(chunk)
            if limit is not None and size > limit:
                return None
        return b"".join(chunks)

    @staticmethod
    def _handoff_name(name: str) -> str:
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise SpoolError("invalid handoff name")
        return name

    def write_handoff(self, name: str, payload: bytes) -> None:
        name = self._handoff_name(name)
        handoff_fd = self._sub_dir_fd(".handoff")
        temporary = ""
        try:
            fd, temporary = self._open_unique_at(handoff_fd, "handoff-", ".tmp")
            try:
                self._write_all(fd, payload)
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(temporary, name, src_dir_fd=handoff_fd, dst_dir_fd=handoff_fd)
            _fsync_dir(handoff_fd)
        except BaseException:
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=handoff_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(handoff_fd)

    def iter_handoffs(self) -> list[tuple[str, bytes]]:
        handoff_fd = self._sub_dir_fd(".handoff")
        try:
            records: list[tuple[str, bytes]] = []
            removed_temporary = False
            for name in sorted(os.listdir(handoff_fd)):
                self._handoff_name(name)
                if name.startswith("handoff-") and name.endswith(".tmp"):
                    try:
                        os.unlink(name, dir_fd=handoff_fd)
                        removed_temporary = True
                    except FileNotFoundError:
                        pass
                    continue
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=handoff_fd)
                except FileNotFoundError:
                    continue
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise SpoolError("unsafe handoff path")
                    payload = self._read_all(fd)
                    assert payload is not None
                    records.append((name, payload))
                finally:
                    os.close(fd)
            if removed_temporary:
                _fsync_dir(handoff_fd)
            return records
        finally:
            os.close(handoff_fd)

    def remove_handoff(self, name: str) -> None:
        name = self._handoff_name(name)
        handoff_fd = self._sub_dir_fd(".handoff")
        try:
            try:
                os.unlink(name, dir_fd=handoff_fd)
            except FileNotFoundError:
                return
            _fsync_dir(handoff_fd)
        finally:
            os.close(handoff_fd)

    def _final(self, digest: str) -> Path:
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SpoolError("invalid evidence hash")
        return self.root / "sha256" / digest[:2] / digest

    @contextmanager
    def _capacity_lock(self) -> Iterator[None]:
        with self._capacity_mutex:
            outermost = self._capacity_depth == 0
            if outermost:
                fcntl.flock(self._capacity, fcntl.LOCK_EX)
            self._capacity_depth += 1
            try:
                yield
            finally:
                self._capacity_depth -= 1
                if outermost:
                    fcntl.flock(self._capacity, fcntl.LOCK_UN)

    @contextmanager
    def _publication(self) -> Iterator[None]:
        with self._publication_condition:
            while self._cleanup_active:
                self._publication_condition.wait()
            self._active_publications += 1
        try:
            yield
        finally:
            with self._publication_condition:
                self._active_publications -= 1
                if self._active_publications == 0:
                    self._publication_condition.notify_all()

    @contextmanager
    def _cleanup(self) -> Iterator[None]:
        with self._publication_condition:
            while self._cleanup_active or self._active_publications:
                self._publication_condition.wait()
            self._cleanup_active = True
        try:
            yield
        finally:
            with self._publication_condition:
                self._cleanup_active = False
                self._publication_condition.notify_all()

    @contextmanager
    def lock(self, digest: str, *, exclusive: bool = False) -> Iterator[None]:
        """Preserve the old locking seam while using the single spool lock."""
        self._final(digest)
        with self._capacity_lock():
            yield

    def _cached_counts_locked(self) -> dict[str, int]:
        counts = self._actual_counts.copy()
        counts["reserved_bytes"] = sum(
            item.limit for item in self._reservations.values() if item._active
        )
        counts["reserved_objects"] = sum(
            1 for item in self._reservations.values() if item._active
        )
        logical = (
            counts["final_bytes"] + counts["temporary_bytes"] + counts["reserved_bytes"]
        )
        self._high_water_bytes = max(self._high_water_bytes, logical)
        counts["high_water_bytes"] = self._high_water_bytes
        return counts

    def _final_files_locked(self) -> list[tuple[str, int]]:
        files: list[tuple[str, int]] = []
        sha_fd = self._sub_dir_fd("sha256")
        try:
            for prefix in os.listdir(sha_fd):
                if len(prefix) != 2 or any(c not in "0123456789abcdef" for c in prefix):
                    raise SpoolError("unsafe final spool path")
                prefix_fd = self._sub_dir_fd("sha256", prefix)
                try:
                    for name in os.listdir(prefix_fd):
                        if (
                            len(name) != 64
                            or not name.startswith(prefix)
                            or any(c not in "0123456789abcdef" for c in name)
                        ):
                            raise SpoolError("unsafe final spool path")
                        info = os.stat(name, dir_fd=prefix_fd, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            raise SpoolError("unsafe final spool path")
                        files.append((name, info.st_size))
                finally:
                    os.close(prefix_fd)
        finally:
            os.close(sha_fd)
        return files

    def final_hashes(self) -> set[str]:
        with self._capacity_lock():
            return {digest for digest, _size in self._final_files_locked()}

    def _scan_locked(self) -> dict[str, int]:
        counts = {key: 0 for key in self._COUNT_KEYS}
        tracked_temporary_sizes: dict[str, int | None] = {}
        for _digest, size in self._final_files_locked():
            counts["final_bytes"] += size
            counts["final_objects"] += 1
        tmp_fd = self._sub_dir_fd("tmp")
        try:
            for name in os.listdir(tmp_fd):
                info = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise SpoolError("unsafe temporary spool path")
                counts["temporary_bytes"] += info.st_size
                counts["temporary_objects"] += 1
                if name in self._temporary_sizes:
                    tracked_temporary_sizes[name] = info.st_size
        finally:
            os.close(tmp_fd)
        self._actual_counts = counts
        self._temporary_sizes = tracked_temporary_sizes
        return self._cached_counts_locked()

    def reconcile(self) -> dict[str, int]:
        """Recompute capacity from files; no durable ledger is involved."""
        with self._capacity_lock():
            return self._scan_locked()

    def _capacity_facts_locked(self, limit: int) -> None:
        capacity = filesystem_capacity(self.root)
        if capacity["inode_model"] == "unknown":
            raise SpoolError("degraded_capacity: spool unknown filesystem capacity")
        if (
            self.free_space_floor
            and int(capacity["free_bytes"]) < self.free_space_floor + limit
        ):
            raise SpoolError("degraded_capacity: spool free-space floor reached")
        if (
            capacity["inode_model"] == "finite"
            and int(capacity["free_inodes"]) < self.free_inode_floor + 1
        ):
            raise SpoolError("degraded_capacity: spool free-inode floor reached")

    def _check_reservation_locked(self, limit: int) -> None:
        counts = self._cached_counts_locked()
        bytes_used = (
            counts["final_bytes"] + counts["temporary_bytes"] + counts["reserved_bytes"]
        )
        objects_used = (
            counts["final_objects"]
            + counts["temporary_objects"]
            + counts["reserved_objects"]
        )
        if bytes_used + limit > self.max_bytes or objects_used + 1 > self.max_objects:
            raise SpoolError("degraded_capacity: spool reservation denied")
        self._capacity_facts_locked(limit)

    def _activate_reservation(self, reservation: SpoolReservation) -> None:
        if reservation.spool is not self:
            raise SpoolError("reservation belongs to another spool")
        if reservation.limit <= 0 or reservation.limit > self.max_body_bytes:
            raise SpoolError("reservation limit exceeds configured body limit")
        with self._capacity_lock():
            if reservation._active:
                return
            self._check_reservation_locked(reservation.limit)
            reservation._active = True
            self._reservations[id(reservation)] = reservation

    def _release_reservation(self, reservation: SpoolReservation) -> None:
        if reservation.spool is not self:
            return
        with self._capacity_lock():
            self._reservations.pop(id(reservation), None)
            reservation._active = False
            reservation._temporary_name = None

    def reserve(self, limit: int | None = None) -> SpoolReservation:
        reservation = SpoolReservation(
            self, self.max_body_bytes if limit is None else limit
        )
        self._activate_reservation(reservation)
        return reservation

    def reservation(self, limit: int | None = None) -> SpoolReservation:
        return self.reserve(limit)

    def _verify_unlocked(
        self, digest: str, expected_size: int | None = None
    ) -> bytes | None:
        self._final(digest)
        try:
            parent_fd = self._sub_dir_fd("sha256", digest[:2])
            try:
                fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SpoolError("unsafe final spool path") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SpoolError("unsafe final spool path")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_body_bytes:
                    return None
            body = b"".join(chunks)
        finally:
            os.close(fd)
        if expected_size is not None and len(body) != expected_size:
            return None
        return body if hashlib.sha256(body).hexdigest() == digest else None

    def verify(self, digest: str, expected_size: int | None = None) -> bytes | None:
        with self._capacity_lock():
            return self._verify_unlocked(digest, expected_size)

    def _write_temp(self, body: bytes, reservation: SpoolReservation) -> str:
        with self._capacity_lock():
            tmp_fd = self._sub_dir_fd("tmp")
            try:
                fd, name = self._open_unique_at(tmp_fd, "evidence-", ".tmp")
            finally:
                os.close(tmp_fd)
            reservation._temporary_name = name
            self._temporary_sizes[name] = None
        try:
            view = memoryview(body)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise SpoolError("short spool write")
                view = view[written:]
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        with self._capacity_lock():
            previous_size = self._temporary_sizes.get(name)
            if previous_size is None:
                self._actual_counts["temporary_bytes"] += len(body)
                self._actual_counts["temporary_objects"] += 1
            else:
                self._actual_counts["temporary_bytes"] += len(body) - previous_size
            self._temporary_sizes[name] = len(body)
        return name

    def _remove_temp_locked(self, name: str) -> None:
        size = self._temporary_sizes.pop(name, None)
        try:
            self._unlink_at(name, "tmp")
        except FileNotFoundError:
            pass
        if size is not None:
            self._actual_counts["temporary_bytes"] = max(
                0, self._actual_counts["temporary_bytes"] - size
            )
            self._actual_counts["temporary_objects"] = max(
                0, self._actual_counts["temporary_objects"] - 1
            )
        tmp_fd = self._sub_dir_fd("tmp")
        try:
            _fsync_dir(tmp_fd)
        finally:
            os.close(tmp_fd)

    def _unlink_at(self, name: str, *parts: str) -> None:
        directory_fd = self._sub_dir_fd(*parts)
        try:
            os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def _publish_reserved(
        self, body: bytes, digest: str, reservation: SpoolReservation
    ) -> None:
        with self._capacity_lock():
            if self._verify_unlocked(digest, len(body)) is not None:
                return
        temporary_name = ""
        try:
            temporary_name = self._write_temp(body, reservation)
            with self._capacity_lock():
                prefix_parent_fd = self._sub_dir_fd("sha256")
                try:
                    try:
                        os.mkdir(digest[:2], 0o700, dir_fd=prefix_parent_fd)
                    except FileExistsError:
                        pass
                    prefix_fd = self._sub_dir_fd("sha256", digest[:2])
                    try:
                        winner = self._verify_unlocked(digest, len(body))
                        if winner is None:
                            tmp_fd = self._sub_dir_fd("tmp")
                            try:
                                try:
                                    os.link(
                                        temporary_name,
                                        digest,
                                        src_dir_fd=tmp_fd,
                                        dst_dir_fd=prefix_fd,
                                        follow_symlinks=False,
                                    )
                                except FileExistsError:
                                    if self._verify_unlocked(digest, len(body)) is None:
                                        try:
                                            info = os.stat(
                                                digest,
                                                dir_fd=prefix_fd,
                                                follow_symlinks=False,
                                            )
                                        except FileNotFoundError:
                                            os.link(
                                                temporary_name,
                                                digest,
                                                src_dir_fd=tmp_fd,
                                                dst_dir_fd=prefix_fd,
                                                follow_symlinks=False,
                                            )
                                            self._actual_counts["final_bytes"] += len(
                                                body
                                            )
                                            self._actual_counts["final_objects"] += 1
                                        else:
                                            if not stat.S_ISREG(info.st_mode):
                                                raise SpoolError(
                                                    "unsafe final spool path"
                                                )
                                            os.unlink(digest, dir_fd=prefix_fd)
                                            self._actual_counts["final_bytes"] = max(
                                                0,
                                                self._actual_counts["final_bytes"]
                                                - info.st_size,
                                            )
                                            self._actual_counts["final_objects"] = max(
                                                0,
                                                self._actual_counts["final_objects"]
                                                - 1,
                                            )
                                            os.link(
                                                temporary_name,
                                                digest,
                                                src_dir_fd=tmp_fd,
                                                dst_dir_fd=prefix_fd,
                                                follow_symlinks=False,
                                            )
                                            self._actual_counts["final_bytes"] += len(
                                                body
                                            )
                                            self._actual_counts["final_objects"] += 1
                                else:
                                    self._actual_counts["final_bytes"] += len(body)
                                    self._actual_counts["final_objects"] += 1
                            finally:
                                os.close(tmp_fd)
                            _fsync_dir(prefix_fd)
                        self._remove_temp_locked(temporary_name)
                    finally:
                        os.close(prefix_fd)
                finally:
                    _fsync_dir(prefix_parent_fd)
                    os.close(prefix_parent_fd)
            reservation._temporary_name = None
        except BaseException:
            if temporary_name:
                try:
                    with self._capacity_lock():
                        self._remove_temp_locked(temporary_name)
                except (OSError, SpoolError):
                    pass
            reservation._temporary_name = None
            raise

    def publish(
        self,
        body: bytes,
        digest: str,
        reservation: SpoolReservation | None = None,
    ) -> None:
        if len(body) > self.max_body_bytes:
            raise SpoolError("archive body exceeds configured limit")
        if hashlib.sha256(body).hexdigest() != digest:
            raise SpoolError("archive checksum mismatch")
        if reservation is None:
            with self.reservation() as owned:
                self._publish_reserved(body, digest, owned)
            return
        if reservation.spool is not self or not reservation._active:
            raise SpoolError("invalid spool reservation")
        if len(body) > reservation.limit:
            raise SpoolError("body exceeds reservation limit")
        self._publish_reserved(body, digest, reservation)
        reservation.release()

    def publish_handoff(
        self,
        body: bytes,
        digest: str,
        name: str,
        payload: bytes,
        reservation: SpoolReservation,
    ) -> None:
        with self._publication():
            self.publish(body, digest, reservation)
            self.write_handoff(name, payload)

    def _handoff_hashes_locked(self) -> set[str]:
        hashes: set[str] = set()
        for _name, payload in self.iter_handoffs():
            try:
                digest = json.loads(payload)["response_hash"]
                self._final(digest)
            except (KeyError, TypeError, ValueError) as error:
                raise SpoolError("invalid response handoff") from error
            hashes.add(digest)
        return hashes

    def _delete_locked(self, digest: str) -> bool:
        try:
            prefix_fd = self._sub_dir_fd("sha256", digest[:2])
        except FileNotFoundError:
            return False
        try:
            try:
                info = os.stat(digest, dir_fd=prefix_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            try:
                os.unlink(digest, dir_fd=prefix_fd)
            except FileNotFoundError:
                return False
            if stat.S_ISREG(info.st_mode):
                self._actual_counts["final_bytes"] = max(
                    0, self._actual_counts["final_bytes"] - info.st_size
                )
                self._actual_counts["final_objects"] = max(
                    0, self._actual_counts["final_objects"] - 1
                )
            _fsync_dir(prefix_fd)
            return True
        finally:
            os.close(prefix_fd)

    def delete_if_unreferenced(self, digest: str) -> bool:
        self._final(digest)
        with self._cleanup():
            with self._capacity_lock():
                if digest in self._handoff_hashes_locked():
                    return False
                self._delete_locked(digest)
                return True

    def remove_unreferenced(self, referenced: set[str]) -> int:
        with self._cleanup():
            with self._capacity_lock():
                protected = referenced | self._handoff_hashes_locked()
                orphaned = {
                    digest
                    for digest, _size in self._final_files_locked()
                    if digest not in protected
                }
                return sum(self._delete_locked(digest) for digest in orphaned)

    def delete(self, digest: str) -> bool:
        self._final(digest)
        with self._capacity_lock():
            return self._delete_locked(digest)

    def stats(self) -> dict[str, Any]:
        with self._capacity_lock():
            counts = self._cached_counts_locked()
            capacity = filesystem_capacity(self.root)
            block_size = max(1, int(capacity["block_size"]))
            counts.update(
                {
                    "filesystem_type": str(capacity["filesystem_type"]),
                    "inode_model": str(capacity["inode_model"]),
                    "free_inodes": int(capacity["free_inodes"]),
                    "free_bytes": int(capacity["free_bytes"]),
                    "allocated_blocks": (
                        counts["final_bytes"]
                        + counts["temporary_bytes"]
                        + block_size
                        - 1
                    )
                    // block_size,
                }
            )
            return counts.copy()

    def readiness(self) -> tuple[bool, str]:
        try:
            stats = self.stats()
        except (OSError, ValueError, SpoolError) as error:
            return False, f"storage_error:{type(error).__name__}"
        logical = (
            stats["final_bytes"] + stats["temporary_bytes"] + stats["reserved_bytes"]
        )
        objects = (
            stats["final_objects"]
            + stats["temporary_objects"]
            + stats["reserved_objects"]
        )
        if (
            logical + self.max_body_bytes > self.max_bytes
            or objects + 1 > self.max_objects
        ):
            return False, "degraded_capacity"
        if stats["free_bytes"] < self.free_space_floor + self.max_body_bytes:
            return False, "degraded_free_space"
        if stats["inode_model"] == "unknown":
            return False, "degraded_capacity"
        if (
            stats["inode_model"] != "dynamic"
            and stats["free_inodes"] < self.free_inode_floor + 1
        ):
            return False, "degraded_free_inodes"
        return True, "ready"

    def cleanup_stale(self, age_seconds: float) -> int:
        if age_seconds <= 0:
            raise ValueError("stale age must be positive")
        removed = 0
        cutoff = time() - age_seconds
        with self._capacity_lock():
            tmp_fd = self._sub_dir_fd("tmp")
            try:
                active = {
                    reservation._temporary_name
                    for reservation in self._reservations.values()
                    if reservation._active and reservation._temporary_name
                }
                for name in os.listdir(tmp_fd):
                    info = os.stat(name, dir_fd=tmp_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise SpoolError("unsafe temporary spool path")
                    if name in active or info.st_mtime > cutoff:
                        continue
                    os.unlink(name, dir_fd=tmp_fd)
                    self._temporary_sizes.pop(name, None)
                    self._actual_counts["temporary_bytes"] = max(
                        0, self._actual_counts["temporary_bytes"] - info.st_size
                    )
                    self._actual_counts["temporary_objects"] = max(
                        0, self._actual_counts["temporary_objects"] - 1
                    )
                    removed += 1
                if removed:
                    _fsync_dir(tmp_fd)
            finally:
                os.close(tmp_fd)
        return removed


__all__ = ["Spool", "SpoolError", "SpoolReservation", "validate_root"]
