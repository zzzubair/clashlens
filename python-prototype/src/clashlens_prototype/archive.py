from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import certifi
import urllib3
from minio import Minio
from minio.error import S3Error

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_BODY_BYTES = 2_000_000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 0.1


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


class S3ArchiveReader:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        secure: bool = True,
        allow_insecure_test_origin: bool = False,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ) -> None:
        if not endpoint or "://" in endpoint or "/" in endpoint:
            raise ValueError(
                "archive endpoint must be a host:port value without a URL scheme or path"
            )
        if not bucket:
            raise ValueError("archive bucket is required")
        if max_body_bytes <= 0:
            raise ValueError("archive body limit must be positive")
        if connect_timeout_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("archive connect and read timeouts must be positive")
        if max_retries < 0:
            raise ValueError("archive retry count must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("archive retry backoff must not be negative")
        if not secure and not allow_insecure_test_origin:
            raise ValueError(
                "insecure archive origin requires an explicit test-only override"
            )
        self.bucket = bucket
        self.secure = secure
        self.max_body_bytes = max_body_bytes
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.http_client = urllib3.PoolManager(
            maxsize=4,
            block=True,
            timeout=urllib3.Timeout(
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
            region="us-east-1",
            http_client=self.http_client,
        )

    def check_ready(self) -> bool:
        for attempt in range(self.max_retries + 1):
            try:
                return bool(self.client.bucket_exists(self.bucket))
            except Exception:  # noqa: BLE001 - readiness must remain a safe boolean
                if attempt == self.max_retries:
                    return False
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * (attempt + 1))
        return False

    def read_verified(self, reference: str, expected_hash: str) -> ArchiveReadResult:
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
                if self.retry_backoff_seconds:
                    time.sleep(self.retry_backoff_seconds * (attempt + 1))
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
            if (
                error.code in {"NoSuchKey", "NoSuchObject", "NotFound"}
                or error.status_code == 404
            ):
                raise ArchiveReadError(
                    "archive_missing",
                    "immutable archive object was not found",
                    retryable=True,
                ) from error
            raise ArchiveReadError(
                "archive_unavailable",
                "S3-compatible archive read failed",
                retryable=True,
            ) from error
        except Exception as error:
            if "404" in str(error):
                raise ArchiveReadError(
                    "archive_missing",
                    "immutable archive object was not found",
                    retryable=True,
                ) from error
            raise ArchiveReadError(
                "archive_unavailable",
                "S3-compatible archive read failed",
                retryable=True,
            ) from error

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


def _parse_reference(reference: str) -> tuple[str, str]:
    parsed = urlsplit(reference)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ArchiveReadError(
            "invalid_archive_reference",
            "archive reference must be an s3://bucket/key URI",
            retryable=False,
        )
    return parsed.netloc, parsed.path.lstrip("/")
