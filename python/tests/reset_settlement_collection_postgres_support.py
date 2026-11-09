"""Shared setup for reset settlement collection postgres tests."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import psycopg

from clashlens.collector import Collector
from clashlens.collector_db import CollectorDatabase
from clashlens.collector_http import ApiKey, KeyPool, OfficialApiClient
from clashlens.spool import Spool

SEASON_RESET = datetime(2026, 9, 7, 5, tzinfo=UTC)


MONDAY_RESET = datetime(2026, 9, 14, 5, tzinfo=UTC)


class _Provider(BaseHTTPRequestHandler):
    """A fake official API that records each request's path and start."""

    protocol_version = "HTTP/1.1"
    status: ClassVar[dict[str, int]] = {}
    requests: ClassVar[list[tuple[str, float]]] = []
    profile_delay: ClassVar[float] = 0.0

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        endpoint = self.path.rsplit("/", 1)[-1]
        endpoint = endpoint if endpoint in {"battlelog", "leaguehistory"} else "profile"
        type(self).requests.append((endpoint, time.time()))
        if endpoint == "profile":
            time.sleep(type(self).profile_delay)
        status = type(self).status[endpoint]
        body = (b'{"items":[]}' if endpoint != "profile" else b'{"tag":"#2PP"}')
        body = body if status == 200 else b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _provider():
    _Provider.status = {"profile": 200, "battlelog": 200, "leaguehistory": 200}
    _Provider.requests = []
    _Provider.profile_delay = 0.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _collector(origin: str, database: CollectorDatabase, tmp_path) -> Collector:
    def keys(label: str) -> KeyPool:
        return KeyPool(
            [ApiKey(label, "secret")], starts_per_second=25, concurrency_per_key=6
        )

    return Collector(
        database=database,
        spool=Spool(tmp_path / "spool", max_body_bytes=4096),
        archive=None,
        client=OfficialApiClient(
            origin, allow_insecure_test_origin=True, max_body_bytes=4096
        ),
        regular_keys=keys("regular-1"),
        interactive_keys=keys("interactive-1"),
        archive_instance_id="fixture",
        collector_version="settlement-test",
        max_body_bytes=4096,
    )


def _players(connection_info: str, *tags: str, active: bool = True) -> list[int]:
    with psycopg.connect(connection_info) as connection:
        return [
            connection.execute(
                "INSERT INTO players (normalized_tag, active, next_due_at)"
                " VALUES (%s, %s, %s) RETURNING id",
                (tag, active, SEASON_RESET if active else None),
            ).fetchone()[0]
            for tag in tags
        ]


def _finish_reset_pairs(connection_info: str) -> None:
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "UPDATE collector_work SET status = 'failed', updated_at = clock_timestamp()"
            " WHERE kind = 'reset_baseline'"
        )
