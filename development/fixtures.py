"""Local Clash API and disk-archive fixtures used by ``./dev``."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlsplit

TAG_ALPHABET = "0289PYLQGRJCUV"
BOOTSTRAP_SEASON_START = 1_783_918_800
SEASON_SECONDS = 28 * 24 * 60 * 60
ARCHIVE_BUCKET = "evidence"
ARCHIVE_MARKER_KEY = "clashlens/archive-instance.json"
ARCHIVE_MARKER_BODY = b'{"fixture":"clashlens-dev-archive-v1"}\n'
VERIFY_TOKEN_PREFIX = "VERIFY-"


def tag_for(index: int) -> str:
    if index == 0:
        return "#2PP"
    value = index
    encoded = ""
    while value:
        value, remainder = divmod(value, len(TAG_ALPHABET))
        encoded = TAG_ALPHABET[remainder] + encoded
    return "#Q" + encoded.rjust(4, "0")


def tags_for(count: int) -> tuple[str, ...]:
    if count < 1 or count > 12_500:
        raise ValueError("synthetic population must be between 1 and 12500 players")
    return tuple(tag_for(index) for index in range(count))


def current_season_id(now: datetime | None = None) -> int:
    observed = now or datetime.now(UTC)
    elapsed = max(0, int(observed.timestamp()) - BOOTSTRAP_SEASON_START)
    return BOOTSTRAP_SEASON_START + elapsed // SEASON_SECONDS * SEASON_SECONDS


def completed_legend_battle_time(now: datetime | None = None) -> datetime:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    reset = observed.replace(hour=5, minute=0, second=0, microsecond=0)
    if observed < reset:
        reset -= timedelta(days=1)
    return reset - timedelta(hours=17)


def ranking_payload(tags: tuple[str, ...]) -> dict[str, object]:
    return {
        "items": [
            {
                "tag": tag,
                "name": f"Synthetic Clasher {index + 1:03d}",
                "rank": index + 1,
                "trophies": 7_000 - index,
                "leagueTier": {"id": 105000036, "name": "Legend I"},
            }
            for index, tag in enumerate(tags[:200])
        ],
        "paging": {"cursors": {}},
    }


def profile_payload(tag: str, index: int) -> dict[str, object]:
    season = current_season_id()
    return {
        "tag": tag,
        "name": f"Synthetic Clasher {index + 1:03d}",
        "expLevel": 250 + index % 40,
        "trophies": 7_000 - index % 1_500,
        "bestTrophies": 7_100 - index % 1_500,
        "leagueTier": {"id": 105000036, "name": "Legend I"},
        "currentLeagueSeasonId": season,
        # Deliberately seven days back: parser v3 must derive the 28-day
        # Legend season rather than trusting this weekly tournament field.
        "previousLeagueSeasonId": season - 7 * 24 * 60 * 60,
        "clan": {"tag": "#2CLAN", "name": "Synthetic Clan"},
    }


def battle_log_payload(
    tag: str, index: int, population: tuple[str, ...]
) -> dict[str, object]:
    opponent = population[(index + 1) % len(population)]
    return {
        "items": [
            {
                "battleType": "legend",
                "attack": True,
                "battleTimestamp": completed_legend_battle_time()
                .isoformat()
                .replace("+00:00", "Z"),
                "stars": 3,
                "destructionPercentage": 100,
                "opponentPlayerTag": opponent,
                "opponentName": f"Synthetic Clasher {(index + 1) % len(population) + 1:03d}",
                "opponentTrophies": 6_999 - index % 1_500,
                "opponentTownHallLevel": 18,
                "trophies": 7_000 - index % 1_500,
                "armyShareCode": "u1x0-2x1",
            }
        ]
    }


class QuietHandler(BaseHTTPRequestHandler):
    server_version = "ClashLensDevelopmentFixture/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class ClashHandler(QuietHandler):
    population: tuple[str, ...] = ()
    tag_indexes: ClassVar[dict[str, int]] = {}

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/healthz":
            self.send_json(200, {"ok": True, "players": len(self.population)})
            return
        if self.headers.get("Authorization", "").startswith("Bearer ") is False:
            self.send_json(401, {"reason": "accessDenied"})
            return
        if path == "/v1/locations/global/rankings/players":
            self.send_json(200, ranking_payload(self.population))
            return
        prefix = "/v1/players/"
        if not path.startswith(prefix):
            self.send_json(404, {"reason": "notFound"})
            return
        suffix = path[len(prefix) :]
        battle_log = suffix.endswith("/battlelog")
        tag = suffix[: -len("/battlelog")] if battle_log else suffix
        index = self.tag_indexes.get(tag.upper())
        if index is None:
            self.send_json(404, {"reason": "notFound"})
        elif battle_log:
            self.send_json(200, battle_log_payload(tag.upper(), index, self.population))
        else:
            self.send_json(200, profile_payload(tag.upper(), index))

    def do_POST(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if self.headers.get("Authorization", "").startswith("Bearer ") is False:
            self.send_json(401, {"reason": "accessDenied"})
            return
        prefix = "/v1/players/"
        suffix = "/verifytoken"
        if not path.startswith(prefix) or not path.endswith(suffix):
            self.send_json(404, {"reason": "notFound"})
            return
        tag = path[len(prefix) : -len(suffix)].upper()
        if tag not in self.tag_indexes:
            self.send_json(404, {"reason": "notFound"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_json(400, {"status": "invalid"})
            return
        expected = VERIFY_TOKEN_PREFIX + tag[1:]
        self.send_json(
            200, {"status": "ok" if body.get("token") == expected else "invalid"}
        )


def archive_path(root: Path, raw_path: str) -> Path | None:
    path = unquote(urlsplit(raw_path).path).strip("/")
    bucket, separator, key = path.partition("/")
    if bucket != ARCHIVE_BUCKET:
        return None
    if not separator:
        return root
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return root.joinpath(*parts)


def s3_error(handler: BaseHTTPRequestHandler, status: int, code: str) -> None:
    body = f"<Error><Code>{code}</Code></Error>".encode("ascii")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/xml")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


class ArchiveHandler(QuietHandler):
    root = Path("/data")

    def do_HEAD(self) -> None:
        self.read_object(include_body=False)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        self.read_object(include_body=True)

    def read_object(self, *, include_body: bool) -> None:
        target = archive_path(self.root, self.path)
        if target == self.root:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if target is None or not target.is_file():
            s3_error(self, 404, "NoSuchKey")
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.send_header("X-Amz-Meta-Sha256", hashlib.sha256(body).hexdigest())
        self.send_header(
            "Last-Modified", formatdate(target.stat().st_mtime, usegmt=True)
        )
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_PUT(self) -> None:
        target = archive_path(self.root, self.path)
        if target is None or target == self.root:
            s3_error(self, 400, "InvalidURI")
            return
        # Raw responses are evidence. The local archive therefore has the same
        # write-once behavior even when a client omits the conditional header.
        if target.exists():
            s3_error(self, 412, "PreconditionFailed")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            s3_error(self, 400, "InvalidRequest")
            return
        body = self.rfile.read(length)
        content_md5 = self.headers.get("Content-MD5")
        if content_md5 and content_md5 != base64.b64encode(
            hashlib.md5(body).digest()
        ).decode("ascii"):
            s3_error(self, 400, "BadDigest")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            try:
                # A hard link publishes the complete temporary file but fails
                # atomically if another request won the same content address.
                os.link(temporary, target)
            except FileExistsError:
                s3_error(self, 412, "PreconditionFailed")
                return
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.send_response(200)
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.send_header("Content-Length", "0")
        self.end_headers()


def serve(handler: type[BaseHTTPRequestHandler], host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clash Lens local-development fixtures"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, default_port in (("clash", 8080), ("archive", 9000)):
        service = subparsers.add_parser(name)
        service.add_argument("--host", default="127.0.0.1")
        service.add_argument("--port", type=int, default=default_port)
    subparsers.choices["clash"].add_argument("--players", type=int, default=200)
    subparsers.choices["archive"].add_argument(
        "--root", type=Path, default=Path("/data")
    )
    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--players", type=int, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    if arguments.command == "manifest":
        body = "".join(f"{tag}\n" for tag in tags_for(arguments.players))
        if (
            arguments.output.exists()
            and arguments.output.read_text(encoding="utf-8") != body
        ):
            raise SystemExit("refusing to replace a different synthetic population")
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(body, encoding="utf-8")
        return
    if arguments.command == "clash":
        population = tags_for(arguments.players)
        handler = type(
            "ConfiguredClashHandler",
            (ClashHandler,),
            {
                "population": population,
                "tag_indexes": {tag: i for i, tag in enumerate(population)},
            },
        )
        serve(handler, arguments.host, arguments.port)
        return
    arguments.root.mkdir(parents=True, exist_ok=True)
    marker = arguments.root / ARCHIVE_MARKER_KEY
    marker.parent.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_bytes(ARCHIVE_MARKER_BODY)
    handler = type(
        "ConfiguredArchiveHandler", (ArchiveHandler,), {"root": arguments.root}
    )
    serve(handler, arguments.host, arguments.port)


if __name__ == "__main__":
    main()
