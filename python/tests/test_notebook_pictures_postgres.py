"""Notebook pictures end to end: a Clasher's upload through the private API
into a local fake bucket, read back through the notebook's signed links,
against a real PostgreSQL. Never the production bucket. See docs/testing.md.
"""

from __future__ import annotations

import base64
import io
import os
import threading
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import urlsplit

import pytest
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from PIL import Image
from test_crews_api import call
from test_discord_bot_postgres import _account
from test_private_api import NOW_SECONDS, TS_CURRENT

from clashlens import alerts, api_notebook_pictures
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.api_notebook_pictures import PictureStore

NOTEBOOK = "/v1/account/notebook"
PICTURE = "/v1/account/notebook/picture"
NOW = datetime.fromtimestamp(NOW_SECONDS, UTC)


class _Bucket(BaseHTTPRequestHandler):
    """An S3 bucket in memory: PUT, GET and DELETE, signatures unchecked."""

    objects: ClassVar[dict[str, bytes]] = {}
    failing = False

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _name(self) -> str:
        return urlsplit(self.path).path.removeprefix("/pictures/")

    def do_PUT(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if type(self).failing:
            self.send_response(503)
        else:
            type(self).objects[self._name()] = body
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        body = type(self).objects.get(self._name())
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def do_DELETE(self) -> None:
        type(self).objects.pop(self._name(), None)
        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def bucket():
    handler = type("Bucket", (_Bucket,), {"objects": {}, "failing": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                              daemon=True)
    thread.start()
    try:
        yield handler, PictureStore(
            endpoint=f"127.0.0.1:{server.server_port}", bucket="pictures",
            region="us-east-1", access_key="test", secret_key="test", secure=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _image(size: tuple[int, int], kind: str = "PNG", *, noise: bool = False,
           **options) -> bytes:
    if noise:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (200, 120, 40))
    buffer = io.BytesIO()
    image.save(buffer, kind, **options)
    return buffer.getvalue()


def _fetched(url: str) -> Image.Image:
    with urllib.request.urlopen(url, timeout=5) as response:
        return Image.open(io.BytesIO(response.read()))


@pytest.fixture()
def notebook_api(database_url: str, bucket):
    """The private API as the website calls it, with two Clashers, sloothy
    and other; ``store`` False gives an API with no bucket."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        try:
            _account(owner, "google", "subject-sloothy", "sloothy")
            _account(owner, "google", "subject-other", "other")

            def client(store: bool = True) -> TestClient:
                # Each app closes its own database when its client stops.
                return TestClient(create_app(
                    ApiDatabase(as_api_role(connection_info)),
                    keys={("typescript-website", "current"): TS_CURRENT},
                    clock=lambda: NOW_SECONDS, now=lambda: NOW, dashboard_enabled=True,
                    notebook_pictures=bucket[1] if store else None,
                ))

            yield client, owner, connection_info
        finally:
            owner.close()


def _add(client: TestClient, name: str, user: str = "sloothy") -> str:
    response = call(client, user, "POST", NOTEBOOK,
                    {"action": "add", "name": name, "status": "to_try"})
    assert response.status_code == 200, response.json()
    return response.json()["base"]


def _upload(client: TestClient, code: str, raw: bytes | None, user: str = "sloothy"):
    image = None if raw is None else base64.b64encode(raw).decode("ascii")
    return call(client, user, "POST", PICTURE, {"base": code, "image": image})


def _pictures(client: TestClient, user: str = "sloothy") -> dict[str, dict | None]:
    notebook = call(client, user, "GET", NOTEBOOK).json()
    return {base["name"]: base["picture"] for base in notebook["bases"]}


def test_a_large_screenshot_becomes_a_picture_and_a_small_copy(notebook_api, bucket) -> None:
    """A phone screenshot the size of the owner's (2796 x 1290, several MB)
    comes back at most 1,600 pixels wide, with a 96-pixel square small
    copy, through links the notebook signs for an hour."""
    client_for, _, _ = notebook_api
    with client_for() as client:
        ring = _add(client, "Ring")
        raw = _image((2796, 1290), noise=True)
        assert len(raw) > 6_000_000
        response = _upload(client, ring, raw)
        assert response.status_code == 200, response.json()
        picture = _pictures(client)["Ring"]
        assert picture is not None and response.json()["picture"] == picture
        full, small = _fetched(picture["full"]), _fetched(picture["small"])
        assert (full.format, full.size) == ("WEBP", (1600, 738))
        assert (small.format, small.size) == ("WEBP", (96, 96))
        for link in picture.values():
            assert "X-Amz-Expires=3600" in link
        # Stored small: the two files are a fraction of the upload.
        assert sum(map(len, bucket[0].objects.values())) < len(raw) / 4


def test_a_photo_is_turned_upright_and_loses_its_metadata(notebook_api) -> None:
    """A phone photo held sideways is saved turned the way it was taken, and
    nothing it carried, such as where it was taken, is kept."""
    client_for, _, _ = notebook_api
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: turn 90 degrees to stand upright.
    exif[0x010F] = "PhoneMaker"
    with client_for() as client:
        ring = _add(client, "Ring")
        response = _upload(client, ring, _image((400, 300), "JPEG", exif=exif.tobytes()))
        assert response.status_code == 200, response.json()
        full = _fetched(response.json()["picture"]["full"])
        assert full.size == (300, 400)
        assert not full.getexif() and "exif" not in full.info


def test_uploads_that_are_not_pictures_or_too_big_are_refused(notebook_api) -> None:
    client_for, _, _ = notebook_api
    with client_for() as client:
        ring = _add(client, "Ring")
        for raw, status, error in (
            (b"not a picture", 422, "not_a_picture"),
            (_image((40, 40), "GIF"), 422, "not_a_picture"),
            (_image((40, 40), noise=True)[:2000], 422, "not_a_picture"),
            (_image((8000, 7000)), 422, "picture_too_large"),
            (b"\0" * (15 * 1024 * 1024 + 1), 413, "picture_too_large"),
        ):
            response = _upload(client, ring, raw)
            assert (response.status_code, response.json().get("error")) == (status, error)
        # Another Clasher's base, or one that doesn't exist.
        other = _add(client, "Theirs", user="other")
        for code in (other, "B-000000"):
            response = _upload(client, code, _image((40, 40)))
            assert (response.status_code, response.json()["error"]) == (404, "base_not_found")
        assert _pictures(client) == {"Ring": None}


def test_a_picture_request_while_another_is_in_flight_is_refused_at_once(
    notebook_api, monkeypatch
) -> None:
    """One picture request at a time: another is refused straight away,
    without waiting, and the next one after it finishes or fails goes on."""
    client_for, _, _ = notebook_api
    shrink = api_notebook_pictures.shrink
    shrinking, finish = threading.Event(), threading.Event()

    def slow(raw: bytes) -> tuple[bytes, bytes]:
        shrinking.set()
        assert finish.wait(timeout=10)
        return shrink(raw)

    with client_for() as client:
        ring = _add(client, "Ring")
        monkeypatch.setattr(api_notebook_pictures, "shrink", slow)
        first: list = []
        upload = threading.Thread(
            target=lambda: first.append(_upload(client, ring, _image((40, 40))))
        )
        upload.start()
        try:
            assert shrinking.wait(timeout=10)
            for raw in (_image((40, 40)), None):
                response = _upload(client, ring, raw)
                assert (response.status_code, response.json()["error"]) == (
                    503, "pictures_busy")
        finally:
            finish.set()
            upload.join(timeout=10)
        assert first[0].status_code == 200
        monkeypatch.setattr(api_notebook_pictures, "shrink", shrink)
        # A failed one frees the slot too.
        assert _upload(client, ring, b"not a picture").status_code == 422
        assert _upload(client, ring, _image((40, 40))).status_code == 200


def test_thirty_uploads_a_day_then_none(notebook_api) -> None:
    client_for, owner, _ = notebook_api
    with client_for() as client:
        ring = _add(client, "Ring")
        for _ in range(30):
            assert _upload(client, ring, _image((40, 40))).status_code == 200
        response = _upload(client, ring, _image((40, 40)))
        assert (response.status_code, response.json()["error"]) == (429, "daily_limit")
        # Refused before it is read: even a file that is no picture.
        response = _upload(client, ring, b"not a picture")
        assert (response.status_code, response.json()["error"]) == (429, "daily_limit")
        # The other Clasher has their own 30.
        theirs = _add(client, "Theirs", user="other")
        assert _upload(client, theirs, _image((40, 40)), user="other").status_code == 200
        # A day after the first upload, one more is allowed.
        with owner.pool.connection() as connection:
            connection.execute(
                "UPDATE notebook_pictures SET uploaded_at = uploaded_at - interval '25 hours'"
                " WHERE id = (SELECT min(id) FROM notebook_pictures)")
        assert _upload(client, ring, _image((40, 40))).status_code == 200


def test_a_replaced_picture_is_deleted_a_day_later(notebook_api, bucket) -> None:
    client_for, owner, _ = notebook_api
    objects = bucket[0].objects
    with client_for() as client:
        ring = _add(client, "Ring")
        first = _upload(client, ring, _image((40, 40))).json()["picture"]
        second = _upload(client, ring, _image((50, 50))).json()["picture"]
        assert _pictures(client)["Ring"] == second
        # The old files stay a day before they are deleted.
        assert len(objects) == 4
        with owner.pool.connection() as connection:
            connection.execute(
                "UPDATE notebook_pictures SET detached_at = detached_at - interval '25 hours'"
                " WHERE base_id IS NULL")
        box = _add(client, "Box")
        assert _upload(client, box, _image((40, 40))).status_code == 200
        names = {urlsplit(link).path.removeprefix("/pictures/") for link in first.values()}
        assert names and not names & set(objects)
        assert len(objects) == 4
        assert _fetched(second["full"]).size == (50, 50)
        # Removing a picture leaves the base with none.
        assert _upload(client, box, None).json()["picture"] is None
        assert _pictures(client) == {"Ring": second, "Box": None}


def test_undo_puts_a_deleted_bases_picture_back_for_its_owner_only(notebook_api) -> None:
    client_for, _, _ = notebook_api
    with client_for() as client:
        ring = _add(client, "Ring")
        picture = _upload(client, ring, _image((40, 40))).json()["picture"]
        deleted = call(client, "sloothy", "POST", NOTEBOOK, {"action": "delete", "base": ring})
        undo = deleted.json()["undo"]
        assert _pictures(client) == {}
        # Another Clasher replaying the Undo gets the base back without it.
        response = call(client, "other", "POST", NOTEBOOK, {"action": "undo", "undo": undo})
        assert response.status_code == 200, response.json()
        assert _pictures(client, "other") == {"Ring": None}
        call(client, "other", "POST", NOTEBOOK, {"action": "delete", "base": ring})
        response = call(client, "sloothy", "POST", NOTEBOOK, {"action": "undo", "undo": undo})
        assert response.status_code == 200, response.json()
        restored = _pictures(client)["Ring"]
        assert restored is not None and restored["full"].split("?")[0] == picture[
            "full"].split("?")[0]
        assert _fetched(restored["full"]).size == (40, 40)


def test_without_a_bucket_the_notebook_has_no_pictures(notebook_api, bucket) -> None:
    client_for, _, _ = notebook_api
    with client_for() as client:
        ring = _add(client, "Ring")
        assert _upload(client, ring, _image((40, 40))).status_code == 200
    with client_for(store=False) as client:
        assert _pictures(client) == {"Ring": None}
        response = _upload(client, ring, _image((40, 40)))
        assert (response.status_code, response.json()["error"]) == (503, "pictures_unavailable")
    bucket[0].failing = True
    with client_for() as client:
        response = _upload(client, ring, _image((60, 60)))
        assert (response.status_code, response.json()["error"]) == (503, "pictures_unavailable")
        # The picture it had stays.
        assert _fetched(_pictures(client)["Ring"]["full"]).size == (40, 40)


def test_the_weekly_usage_line_counts_uploads_and_stored_bytes(
    notebook_api, tmp_path, monkeypatch, capsys
) -> None:
    client_for, owner, connection_info = notebook_api
    with client_for() as client:
        ring = _add(client, "Ring")
        for size in (40, 50):
            assert _upload(client, ring, _image((size, size))).status_code == 200
        theirs = _add(client, "Theirs", user="other")
        assert _upload(client, theirs, _image((40, 40)), user="other").status_code == 200
    with owner.pool.connection() as connection:
        (stored,) = connection.execute("SELECT sum(bytes) FROM notebook_pictures").fetchone()
    url = tmp_path / "database-url"
    url.write_text(as_api_role(connection_info))
    monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url))
    alerts.pictures_probe()
    counts = [int(value) for value in capsys.readouterr().out.split()]
    # Three uploads by two Clashers; two on bases, the replaced one still stored.
    assert counts == [3, 2, 2, stored]
