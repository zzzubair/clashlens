from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import certifi
import urllib3
from minio import Minio
from minio.error import S3Error

from .spool import Spool, SpoolError

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 0.1
DEFAULT_ARCHIVE_POOL_SIZE = 4
MAX_ARCHIVE_BODY_BYTES = 64 * 1024 * 1024
MAX_CONNECT_TIMEOUT_SECONDS = 60.0
MAX_READ_TIMEOUT_SECONDS = 300.0
MAX_ARCHIVE_RETRIES = 5
MAX_RETRY_BACKOFF_SECONDS = 30.0
MAX_ARCHIVE_POOL_SIZE = 64


class _BoundedResponse:
    def __init__(self, response: Any, max_body_bytes: int) -> None:
        self._response = response
        self._max_read_bytes = max_body_bytes + 1

    def read(
        self,
        amt: int | None = None,
        decode_content: bool | None = None,
        cache_content: bool = False,
    ) -> bytes:
        if amt is None or amt < 0:
            bounded_amount = self._max_read_bytes
        else:
            bounded_amount = min(amt, self._max_read_bytes)
        return self._response.read(
            bounded_amount,
            decode_content=decode_content,
            cache_content=cache_content,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class _BoundedPoolManager(urllib3.PoolManager):
    def __init__(self, *, max_body_bytes: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._max_body_bytes = max_body_bytes
        self._pool_acquire_observer: Callable[[float], None] | None = None

    def set_pool_acquire_observer(
        self, observer: Callable[[float], None] | None
    ) -> None:
        self._pool_acquire_observer = observer

    def _new_pool(
        self,
        scheme: str,
        host: str,
        port: int,
        request_context: dict[str, Any] | None = None,
    ) -> Any:
        pool = super()._new_pool(scheme, host, port, request_context)
        get_connection = pool._get_conn

        def observed_get_connection(timeout: float | None = None) -> Any:
            started_at = time.monotonic()
            try:
                return get_connection(timeout)
            finally:
                observer = self._pool_acquire_observer
                if observer is not None:
                    observer(time.monotonic() - started_at)

        pool._get_conn = observed_get_connection  # type: ignore[method-assign]
        return pool

    def urlopen(self, *args: Any, **kwargs: Any) -> _BoundedResponse:
        response = super().urlopen(*args, **kwargs)
        return _BoundedResponse(response, self._max_body_bytes)


class ArchiveReadError(RuntimeError):
    def __init__(self, category: str, message: str, *, retryable: bool) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ArchiveReadResult:
    body: bytes
    reference: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveInstanceConfig:
    instance_id: str
    endpoint: str
    region: str
    bucket: str
    marker_key: str
    marker_hash: str
    marker_payload_version: str


class S3ArchiveReader:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        secure: bool = True,
        region: str = "us-east-1",
        allow_insecure_test_origin: bool = False,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        pool_size: int = DEFAULT_ARCHIVE_POOL_SIZE,
        instance_id: str = "",
        marker_key: str = "",
        marker_hash: str = "",
        marker_payload_version: str = "",
    ) -> None:
        if not endpoint or "://" in endpoint or "/" in endpoint:
            raise ValueError(
                "archive endpoint must be a host:port value without a URL scheme or path"
            )
        if not bucket:
            raise ValueError("archive bucket is required")
        if not region:
            raise ValueError("archive signing region is required")
        configured_instance = any((instance_id, marker_key, marker_hash, marker_payload_version))
        if configured_instance and not all((instance_id, marker_key, marker_hash, marker_payload_version)):
            raise ValueError("complete archive instance configuration is required")
        if configured_instance and not _HASH_RE.fullmatch(marker_hash):
            raise ValueError("archive marker hash must be lowercase SHA-256")
        if max_body_bytes <= 0:
            raise ValueError("archive body limit must be positive")
        if max_body_bytes > MAX_ARCHIVE_BODY_BYTES:
            raise ValueError("archive body limit exceeds the supported maximum")
        if (
            not math.isfinite(connect_timeout_seconds)
            or not math.isfinite(read_timeout_seconds)
            or connect_timeout_seconds <= 0
            or read_timeout_seconds <= 0
        ):
            raise ValueError("archive connect and read timeouts must be positive")
        if connect_timeout_seconds > MAX_CONNECT_TIMEOUT_SECONDS:
            raise ValueError("archive connect timeout exceeds the supported maximum")
        if read_timeout_seconds > MAX_READ_TIMEOUT_SECONDS:
            raise ValueError("archive read timeout exceeds the supported maximum")
        if max_retries < 0:
            raise ValueError("archive retry count must not be negative")
        if max_retries > MAX_ARCHIVE_RETRIES:
            raise ValueError("archive retry count exceeds the supported maximum")
        if not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValueError("archive retry backoff must be finite and non-negative")
        if retry_backoff_seconds > MAX_RETRY_BACKOFF_SECONDS:
            raise ValueError("archive retry backoff exceeds the supported maximum")
        if not secure and not allow_insecure_test_origin:
            raise ValueError(
                "insecure archive origin requires an explicit test-only override"
            )
        if pool_size < 1:
            raise ValueError("archive pool size must be positive")
        if pool_size > MAX_ARCHIVE_POOL_SIZE:
            raise ValueError("archive pool size exceeds the supported maximum")
        self.bucket = bucket
        self.region = region
        self.instance_config = (
            ArchiveInstanceConfig(
                instance_id=instance_id,
                endpoint=endpoint,
                region=region,
                bucket=bucket,
                marker_key=marker_key,
                marker_hash=marker_hash,
                marker_payload_version=marker_payload_version,
            )
            if configured_instance
            else None
        )
        self._marker_checked_at = 0.0
        self._marker_error: ArchiveReadError | None = None
        self.secure = secure
        self.max_body_bytes = max_body_bytes
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.pool_size = pool_size
        self.http_client = _BoundedPoolManager(
            max_body_bytes=max_body_bytes,
            maxsize=pool_size,
            block=True,
            timeout=urllib3.Timeout(
                total=connect_timeout_seconds + read_timeout_seconds,
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
            ),
            retries=urllib3.Retry(
                total=0,
                connect=0,
                read=0,
                redirect=0,
                status=0,
                other=0,
            ),
            cert_reqs="CERT_REQUIRED" if secure else "CERT_NONE",
            ca_certs=certifi.where(),
        )
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region,
            http_client=self.http_client,
        )

    def set_pool_acquire_observer(
        self, observer: Callable[[float], None] | None
    ) -> None:
        self.http_client.set_pool_acquire_observer(observer)

    def check_ready(self) -> bool:
        # Archive reachability is telemetry in the spool-first worker. Keep
        # this legacy adapter probe only for callers without a local spool.
        for attempt in range(self.max_retries + 1):
            try:
                return bool(self.client.bucket_exists(self.bucket))
            except Exception:  # noqa: BLE001 - readiness is a safe boolean
                if attempt == self.max_retries:
                    return False
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * (attempt + 1))
        return False

    def validate_instance(self, database: Any) -> None:
        config = self.instance_config
        if config is None:
            return
        validator = getattr(database, "validate_archive_instance", None)
        if not callable(validator) or not validator(config):
            raise ValueError("archive instance configuration contradicts PostgreSQL")

    def marker_health(self) -> str:
        return self.check_marker_health()

    def check_marker_health(self) -> str:
        config = self.instance_config
        if config is None:
            return "unconfigured"
        now = time.monotonic()
        if now - self._marker_checked_at < 300:
            return "terminal" if self._marker_error and not self._marker_error.retryable else ("degraded" if self._marker_error else "ready")
        self._marker_checked_at = now
        try:
            response = self.client.get_object(self.bucket, config.marker_key)
            try:
                body = response.read(1_048_577)
            finally:
                response.close()
                response.release_conn()
            if len(body) > 1_048_576 or hashlib.sha256(body).hexdigest() != config.marker_hash:
                raise ArchiveReadError("archive_marker_mismatch", "archive marker does not match its configured hash", retryable=False)
            self._marker_error = None
        except ArchiveReadError as error:
            self._marker_error = error
        except Exception as error:  # noqa: BLE001 - marker health never gates local duplicates
            self._marker_error = ArchiveReadError("archive_unavailable", "archive marker request failed", retryable=True)
            del error
        return "terminal" if self._marker_error and not self._marker_error.retryable else ("degraded" if self._marker_error else "ready")

    def read_verified(
        self,
        reference: str,
        expected_hash: str,
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> ArchiveReadResult:
        if not _HASH_RE.fullmatch(expected_hash):
            raise ArchiveReadError(
                "invalid_observation_hash",
                "observation hash is not a lowercase SHA-256 digest",
                retryable=False,
            )
        bucket, object_key = _parse_reference(reference)
        if bucket != self.bucket:
            raise ArchiveReadError(
                "archive_reference_mismatch",
                "archive reference bucket does not match configured bucket",
                retryable=False,
            )
        for attempt in range(self.max_retries + 1):
            try:
                return self._read_once(reference, bucket, object_key, expected_hash)
            except ArchiveReadError as error:
                if not error.retryable or attempt == self.max_retries:
                    raise
                # Renew the caller's lease before backing off so the wall time
                # of a bounded provider-outage retry window stays inside it.
                if heartbeat is not None:
                    heartbeat()
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * (attempt + 1))
                if heartbeat is not None:
                    heartbeat()
        raise AssertionError("unreachable archive retry loop")

    def _read_once(
        self,
        reference: str,
        bucket: str,
        object_key: str,
        expected_hash: str,
    ) -> ArchiveReadResult:
        try:
            response = self.client.get_object(bucket, object_key)
            try:
                content_length = response.headers.get("Content-Length")
                if (
                    content_length is not None
                    and int(content_length) > self.max_body_bytes
                ):
                    raise ArchiveReadError(
                        "archive_body_too_large",
                        "retrieved archive body exceeds the configured byte limit",
                        retryable=False,
                    )
                body = response.read(self.max_body_bytes + 1)
            finally:
                response.close()
                response.release_conn()
        except ArchiveReadError:
            raise
        except S3Error as error:
            status_code = getattr(error.response, "status", None)
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"} or status_code == 404:
                raise ArchiveReadError("archive_missing", "immutable archive object was not found", retryable=True) from error
            if status_code in {401, 403} or error.code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise ArchiveReadError("archive_permission_denied", "archive credential is not authorized", retryable=False) from error
            if status_code in {301, 307} or error.code in {"NoSuchBucket", "InvalidBucketName", "AuthorizationHeaderMalformed", "PermanentRedirect", "InvalidRegion"}:
                raise ArchiveReadError("archive_configuration_error", "archive instance configuration was rejected", retryable=False) from error
            if status_code in {400, 405, 501} or error.code in {"NotImplemented", "InvalidRequest", "MethodNotAllowed"}:
                raise ArchiveReadError("archive_unsupported", "archive provider does not support the required immutable read", retryable=False) from error
            if status_code is not None and status_code >= 500:
                raise ArchiveReadError("archive_unavailable", "S3-compatible archive read failed", retryable=True) from error
            raise ArchiveReadError("archive_configuration_error", "archive request was rejected", retryable=False) from error
        except Exception as error:
            message = str(error).lower()
            if "404" in message:
                raise ArchiveReadError("archive_missing", "immutable archive object was not found", retryable=True) from error
            if any(token in message for token in ("timeout", "timed out", "connection", "reset", "temporarily")):
                raise ArchiveReadError("archive_unavailable", "S3-compatible archive read failed", retryable=True) from error
            raise ArchiveReadError("archive_configuration_error", "archive request failed", retryable=False) from error

        if len(body) > self.max_body_bytes:
            raise ArchiveReadError(
                "archive_body_too_large",
                "retrieved archive body exceeds the configured byte limit",
                retryable=False,
            )
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != expected_hash:
            raise ArchiveReadError(
                "archive_checksum_mismatch",
                "retrieved archive bytes do not match the observation hash",
                retryable=False,
            )
        return ArchiveReadResult(body=body, reference=reference, sha256=actual_hash)


class SpoolFirstReader:
    """Read verified evidence locally, repairing from the read-only archive on a miss."""

    def __init__(
        self,
        archive: S3ArchiveReader,
        *,
        spool_root: str,
        max_body_bytes: int | None = None,
        max_bytes: int = 16 << 30,
        max_objects: int = 1_000_000,
        free_space_floor: int = 0,
        free_inode_floor: int = 0,
        database: Any | None = None,
        stage_metrics: Any | None = None,
    ) -> None:
        self.archive = archive
        if database is not None:
            self.archive.validate_instance(database)
        if getattr(archive, "instance_config", None) is not None and database is None:
            raise ValueError("PostgreSQL archive instance validation is required")
        self.spool = Spool(
            spool_root,
            max_body_bytes=max_body_bytes or archive.max_body_bytes,
            max_bytes=max_bytes,
            max_objects=max_objects,
            free_space_floor=free_space_floor,
            free_inode_floor=free_inode_floor,
        )
        self.stage_metrics = stage_metrics
        # Measured evidence counters for the performance runner (#64).
        self._counters = {
            "local_hits": 0, "local_misses": 0, "repairs": 0, "provider_errors": 0,
        }

    def _record_stage(self, stage: str, started_at: float) -> None:
        if self.stage_metrics is not None:
            self.stage_metrics.record(stage, time.monotonic() - started_at)

    def counters(self) -> dict[str, int]:
        return dict(self._counters)

    def stats(self) -> dict[str, int]:
        return self.spool.stats()

    @property
    def root(self):
        return self.spool.root

    def set_pool_acquire_observer(self, observer: Callable[[float], None] | None) -> None:
        self.archive.set_pool_acquire_observer(observer)

    def check_ready(self) -> bool:
        try:
            return self.spool.readiness()[0]
        except (OSError, ValueError, SpoolError):
            return False

    def readiness(self) -> dict[str, Any]:
        ready, reason = self.spool.readiness()
        return {"ready": ready, "component": "spool", "reason": reason, "remote_health": self.archive.check_marker_health()}

    def check_marker_health(self) -> str:
        return self.archive.check_marker_health()

    def marker_health(self) -> str:
        return self.archive.check_marker_health()

    def read_verified(
        self,
        reference: str,
        expected_hash: str,
        *,
        heartbeat: Callable[[], None] | None = None,
    ) -> ArchiveReadResult:
        """Read verified evidence, renewing the worker lease via ``heartbeat``.

        ``heartbeat`` is invoked before each bounded remote retry so a long
        provider outage cannot outlive the claim's renewed lease window; a
        lease loss raises through the callback and discards any partial work.
        """
        if not _HASH_RE.fullmatch(expected_hash):
            raise ArchiveReadError("invalid_observation_hash", "observation hash is not a lowercase SHA-256 digest", retryable=False)
        bucket, _ = _parse_reference(reference)
        if bucket != self.archive.bucket:
            raise ArchiveReadError("archive_reference_mismatch", "archive reference bucket does not match configured bucket", retryable=False)
        verify_started = time.monotonic()
        local = self.spool.verify(expected_hash)
        self._record_stage("python_archive_local_verify", verify_started)
        if local is not None:
            self._counters["local_hits"] += 1
            return ArchiveReadResult(body=local, reference=reference, sha256=expected_hash)
        self._counters["local_misses"] += 1
        # Bounded remote fallback with lease-aware pacing: each transient
        # provider failure is followed by a heartbeat renewal so the retry
        # wall time stays inside the worker's renewed lease window.
        max_attempts = getattr(self.archive, "max_retries", 0) + 1
        backoff = getattr(self.archive, "retry_backoff_seconds", 0.0)
        for attempt in range(max_attempts):
            try:
                remote = self.archive.read_verified(reference, expected_hash, heartbeat=heartbeat)
                break
            except ArchiveReadError as error:
                if not error.retryable or attempt == max_attempts - 1:
                    if error.retryable:
                        self._counters["provider_errors"] += 1
                    raise
                if heartbeat is not None:
                    heartbeat()
                if backoff:
                    time.sleep(backoff)
                if heartbeat is not None:
                    heartbeat()
        try:
            repair_started = time.monotonic()
            self.spool.publish(remote.body, expected_hash)
            self._record_stage("python_archive_repair", repair_started)
            self._counters["repairs"] += 1
            return remote
        except SpoolError as error:
            raise ArchiveReadError("spool_io_failed", "local evidence repair failed", retryable=True) from error


def _parse_reference(reference: str) -> tuple[str, str]:
    parsed = urlsplit(reference)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ArchiveReadError(
            "invalid_archive_reference",
            "archive reference must be an s3://bucket/key URI",
            retryable=False,
        )
    return parsed.netloc, parsed.path.lstrip("/")
