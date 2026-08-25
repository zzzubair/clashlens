from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from minio.error import S3Error

from clashlens.archive import (
    MAX_ARCHIVE_BODY_BYTES,
    ArchiveReadError,
    ArchiveReadResult,
    S3ArchiveReader,
    SpoolFirstReader,
    _BoundedResponse,
)

FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


class _S3Handler(BaseHTTPRequestHandler):
    objects: ClassVar[dict[str, bytes]] = {}
    get_count = 0

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = self.objects.get(key)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        type(self).get_count += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Amz-Meta-Sha256", hashlib.sha256(body).hexdigest())
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def archive_server():
    body = FIXTURE.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    key = f"sha256/{digest[:2]}/{digest}"
    handler = type(
        "FixtureS3Handler", (_S3Handler,), {"objects": {key: body}, "get_count": 0}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", f"s3://evidence/{key}", digest, handler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_spool_reader_requires_matching_database_archive_instance_before_reuse(tmp_path: Path) -> None:
    reader = S3ArchiveReader(
        endpoint="archive.example.test:9000",
        bucket="evidence",
        region="fr-par",
        access_key="test",
        secret_key="test",
        instance_id="instance-1",
        marker_key="clashlens/marker.json",
        marker_hash="a" * 64,
        marker_payload_version="v1",
    )

    class Database:
        def validate_archive_instance(self, _config: object) -> bool:
            return False

    with pytest.raises(ValueError, match="archive instance configuration"):
        SpoolFirstReader(reader, spool_root=str(tmp_path / "spool"), database=Database())


def test_s3_archive_reader_fetches_bytes_and_checks_sha256_before_json_parse(
    archive_server,
) -> None:
    endpoint, reference, digest, handler = archive_server
    reader = S3ArchiveReader(
        endpoint=endpoint,
        bucket="evidence",
        access_key="test",
        secret_key="test",
        secure=False,
        allow_insecure_test_origin=True,
    )
    pool_acquire_durations: list[float] = []
    reader.set_pool_acquire_observer(pool_acquire_durations.append)

    result = reader.read_verified(reference, digest)

    assert json.loads(result.body)["leagueTier"]["name"] == "Legend I"
    assert handler.get_count == 1
    assert len(pool_acquire_durations) == 1
    assert pool_acquire_durations[0] >= 0


def test_s3_archive_reader_classifies_tampered_bytes(archive_server) -> None:
    endpoint, reference, digest, handler = archive_server
    key = reference.removeprefix("s3://evidence/")
    handler.objects[key] = b"tampered"
    reader = S3ArchiveReader(
        endpoint=endpoint,
        bucket="evidence",
        access_key="test",
        secret_key="test",
        secure=False,
        allow_insecure_test_origin=True,
    )

    with pytest.raises(ArchiveReadError, match="archive_checksum_mismatch"):
        reader.read_verified(reference, digest)


@pytest.mark.parametrize(
    ("code", "status", "retryable"),
    [("AccessDenied", 403, False), ("NotImplemented", 501, False), ("InternalError", 500, True)],
)
def test_s3_archive_reader_classifies_provider_failures(code: str, status: int, retryable: bool) -> None:
    reader = S3ArchiveReader(
        endpoint="archive.example.test:9000",
        bucket="evidence",
        access_key="test",
        secret_key="test",
        max_retries=0,
    )
    error = S3Error(SimpleNamespace(status=status), code, "failure", "/object", "request", "host")
    reader.client.get_object = lambda _bucket, _key: (_ for _ in ()).throw(error)  # type: ignore[method-assign]
    with pytest.raises(ArchiveReadError) as captured:
        reader.read_verified("s3://evidence/object", "a" * 64)
    assert captured.value.retryable is retryable


def test_s3_archive_reader_classifies_missing_object(archive_server) -> None:
    endpoint, _reference, digest, _handler = archive_server
    missing_reference = f"s3://evidence/sha256/{digest[:2]}/missing"
    reader = S3ArchiveReader(
        endpoint=endpoint,
        bucket="evidence",
        access_key="test",
        secret_key="test",
        secure=False,
        allow_insecure_test_origin=True,
    )

    with pytest.raises(ArchiveReadError, match="archive_missing"):
        reader.read_verified(missing_reference, digest)


def test_s3_archive_reader_rejects_a_body_over_the_configured_limit(
    archive_server,
) -> None:
    endpoint, reference, digest, handler = archive_server
    key = reference.removeprefix("s3://evidence/")
    handler.objects[key] = b"x" * 11
    reader = S3ArchiveReader(
        endpoint=endpoint,
        bucket="evidence",
        access_key="test",
        secret_key="test",
        secure=False,
        allow_insecure_test_origin=True,
        max_body_bytes=10,
    )

    with pytest.raises(ArchiveReadError, match="archive_body_too_large") as captured:
        reader.read_verified(reference, digest)

    assert captured.value.retryable is False


def test_s3_archive_reader_defaults_to_tls_and_requires_an_explicit_test_override() -> (
    None
):
    reader = S3ArchiveReader(
        endpoint="archive.example.test:9000",
        bucket="evidence",
        access_key="test",
        secret_key="test",
    )

    assert reader.secure is True

    with pytest.raises(ValueError, match="insecure archive origin"):
        S3ArchiveReader(
            endpoint="127.0.0.1:9000",
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
        )


def test_s3_archive_reader_retries_one_transient_archive_read() -> None:
    body = b"{}"
    digest = hashlib.sha256(body).hexdigest()

    class Response:
        def __init__(self) -> None:
            self.headers = {"Content-Length": str(len(body))}

        def read(self, _limit: int) -> bytes:
            return body

        def close(self) -> None:
            return

        def release_conn(self) -> None:
            return

    reader = S3ArchiveReader(
        endpoint="archive.example.test:9000",
        bucket="evidence",
        access_key="test",
        secret_key="test",
        max_retries=1,
    )
    calls = 0

    def get_object(_bucket: str, _key: str) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary connection failure")
        return Response()

    reader.client.get_object = get_object  # type: ignore[method-assign]

    result = reader.read_verified("s3://evidence/object", digest)

    assert result.body == body
    assert calls == 2


def test_s3_archive_reader_reports_archive_readiness_without_claiming_work() -> None:
    reader = S3ArchiveReader(
        endpoint="archive.example.test:9000",
        bucket="evidence",
        access_key="test",
        secret_key="test",
        max_retries=0,
    )
    reader.client.bucket_exists = lambda _bucket: False  # type: ignore[method-assign]

    assert reader.check_ready() is False


def test_s3_archive_reader_sets_a_total_request_deadline() -> None:
    reader = S3ArchiveReader(
        endpoint="archive.example.test:9000",
        bucket="evidence",
        access_key="test",
        secret_key="test",
        connect_timeout_seconds=2,
        read_timeout_seconds=8,
    )

    timeout = reader.http_client.connection_pool_kw["timeout"]

    assert timeout.total == 10


def test_archive_error_response_reads_are_bounded() -> None:
    class Response:
        def __init__(self) -> None:
            self.amount: int | None = None

        def read(
            self,
            amount: int,
            *,
            decode_content: bool | None,
            cache_content: bool,
        ) -> bytes:
            self.amount = amount
            return b"x" * amount

    response = Response()
    bounded = _BoundedResponse(response, max_body_bytes=10)

    body = bounded.read(cache_content=True)

    assert response.amount == 11
    assert len(body) == 11


def test_s3_archive_reader_rejects_unbounded_resource_settings() -> None:
    with pytest.raises(ValueError, match="supported maximum"):
        S3ArchiveReader(
            endpoint="archive.example.test:9000",
            bucket="evidence",
            access_key="test",
            secret_key="test",
            max_body_bytes=MAX_ARCHIVE_BODY_BYTES + 1,
        )

    with pytest.raises(ValueError, match="supported maximum"):
        S3ArchiveReader(
            endpoint="archive.example.test:9000",
            bucket="evidence",
            access_key="test",
            secret_key="test",
            read_timeout_seconds=301,
        )



class _FlakyArchive:
    """Archive stub that fails with a retryable error N times, then succeeds."""

    max_body_bytes = 1024

    max_retries = 2
    retry_backoff_seconds = 0.01

    def __init__(self, body: bytes, transient_failures: int) -> None:
        self.body = body
        self.remaining = transient_failures
        self.heartbeats = 0
        self.bucket = "bucket"

    def set_pool_acquire_observer(self, observer):
        pass

    def read_verified(self, reference, expected_hash, *, heartbeat=None):
        if heartbeat is not None:
            heartbeat()  # renewal before the backoff window
        if self.remaining > 0:
            self.remaining -= 1
            raise ArchiveReadError("archive_unavailable", "transient outage", retryable=True)
        return ArchiveReadResult(body=self.body, reference=reference, sha256=expected_hash)


class LeaseLost(Exception):
    pass


def test_spool_first_renews_lease_across_remote_retries(tmp_path: Path) -> None:
    from clashlens.spool import Spool as _Spool  # noqa: F401  (import sanity)

    body = b"heartbeat body"
    digest = hashlib.sha256(body).hexdigest()
    reference = f"s3://bucket/sha256/{digest[:2]}/{digest}"
    archive = _FlakyArchive(body, transient_failures=2)

    reader = SpoolFirstReader(archive, spool_root=str(tmp_path / "spool"))
    heartbeats: list[int] = []
    result = reader.read_verified(reference, digest, heartbeat=lambda: heartbeats.append(1))

    assert result.body == body
    # At least one renewal around each transient retry window; the exact count
    # depends on the reader's pacing, but a single-shot fallback must never
    # pass through without renewals when transient failures occurred.
    assert len(heartbeats) >= 2, "lease renewals must accompany remote retries"
    assert archive.remaining == 0


def test_spool_first_lease_loss_discards_the_fallback_result(tmp_path: Path) -> None:
    from clashlens.spool import Spool

    body = b"lease lost mid-fallback"
    digest = hashlib.sha256(body).hexdigest()
    reference = f"s3://bucket/sha256/{digest[:2]}/{digest}"

    class LeaseLosingArchive(_FlakyArchive):
        def read_verified(self, reference, expected_hash, *, heartbeat=None):
            def lose():
                raise LeaseLost("lease expired during remote fallback")

            return super().read_verified(reference, expected_hash, heartbeat=lose)

    archive = LeaseLosingArchive(body, transient_failures=1)
    reader = SpoolFirstReader(archive, spool_root=str(tmp_path / "spool"))

    with pytest.raises(LeaseLost):
        reader.read_verified(reference, digest, heartbeat=lambda: None)
    # Nothing was repaired locally and no result survived.
    assert Spool(str(tmp_path / "spool"), max_body_bytes=1024).verify(digest) is None
