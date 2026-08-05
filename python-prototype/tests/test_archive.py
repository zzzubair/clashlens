from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from clashlens_prototype.archive import (
    ArchiveReadError,
    S3ArchiveReader,
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
    handler = type("FixtureS3Handler", (_S3Handler,), {"objects": {key: body}, "get_count": 0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}", f"s3://evidence/{key}", digest, handler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_s3_archive_reader_fetches_bytes_and_checks_sha256_before_json_parse(archive_server) -> None:
    endpoint, reference, digest, handler = archive_server
    reader = S3ArchiveReader(endpoint=endpoint, bucket="evidence", access_key="test", secret_key="test")

    result = reader.read_verified(reference, digest)

    assert json.loads(result.body)["leagueTier"]["name"] == "Legend I"
    assert handler.get_count == 1


def test_s3_archive_reader_classifies_tampered_bytes(archive_server) -> None:
    endpoint, reference, digest, handler = archive_server
    key = reference.removeprefix("s3://evidence/")
    handler.objects[key] = b"tampered"
    reader = S3ArchiveReader(endpoint=endpoint, bucket="evidence", access_key="test", secret_key="test")

    with pytest.raises(ArchiveReadError, match="archive_checksum_mismatch"):
        reader.read_verified(reference, digest)


def test_s3_archive_reader_classifies_missing_object(archive_server) -> None:
    endpoint, _reference, digest, _handler = archive_server
    missing_reference = f"s3://evidence/sha256/{digest[:2]}/missing"
    reader = S3ArchiveReader(endpoint=endpoint, bucket="evidence", access_key="test", secret_key="test")

    with pytest.raises(ArchiveReadError, match="archive_missing"):
        reader.read_verified(missing_reference, digest)


def test_s3_archive_reader_rejects_a_body_over_the_configured_limit(archive_server) -> None:
    endpoint, reference, digest, handler = archive_server
    key = reference.removeprefix("s3://evidence/")
    handler.objects[key] = b"x" * 11
    reader = S3ArchiveReader(
        endpoint=endpoint,
        bucket="evidence",
        access_key="test",
        secret_key="test",
        max_body_bytes=10,
    )

    with pytest.raises(ArchiveReadError, match="archive_body_too_large") as captured:
        reader.read_verified(reference, digest)

    assert captured.value.retryable is False
