from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from clashlens_prototype.archive import (
    MAX_ARCHIVE_BODY_BYTES,
    ArchiveReadError,
    S3ArchiveReader,
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

    result = reader.read_verified(reference, digest)

    assert json.loads(result.body)["leagueTier"]["name"] == "Legend I"
    assert handler.get_count == 1


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
