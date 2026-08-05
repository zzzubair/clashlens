from __future__ import annotations

import hashlib
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


class _FixtureS3Handler(BaseHTTPRequestHandler):
    objects: ClassVar[dict[str, bytes]] = {}
    get_count = 0

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = type(self).objects.get(key)
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

    def do_HEAD(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = type(self).objects.get(key)
        if body is None and key:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Amz-Meta-Sha256", hashlib.sha256(body).hexdigest())
        self.end_headers()


@pytest.fixture()
def archive_server():
    body = FIXTURE.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    key = f"sha256/{digest[:2]}/{digest}"
    handler = type(
        "FixtureS3Handler",
        (_FixtureS3Handler,),
        {"objects": {key: body}, "get_count": 0},
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


@pytest.fixture()
def database_url() -> str:
    value = os.environ.get("CLASHLENS_TEST_DATABASE_URL")
    if not value:
        pytest.skip("set CLASHLENS_TEST_DATABASE_URL for PostgreSQL integration tests")
    return value
