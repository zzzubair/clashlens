from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_MAX_BODY_BYTES = 2_000_000


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
        secure: bool = False,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("archive body limit must be positive")
        self.bucket = bucket
        self.max_body_bytes = max_body_bytes
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region="us-east-1",
        )

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
        try:
            response = self.client.get_object(bucket, object_key)
            try:
                body = response.read(self.max_body_bytes + 1)
            finally:
                response.close()
                response.release_conn()
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject", "NotFound"} or error.status_code == 404:
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
