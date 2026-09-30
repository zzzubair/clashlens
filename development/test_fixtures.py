from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from development.fixtures import (
    ARCHIVE_MARKER_BODY,
    ARCHIVE_MARKER_KEY,
    ArchiveHandler,
    ClashHandler,
    profile_payload,
    ranking_payload,
    tags_for,
)


def test_synthetic_populations_are_unique_and_feed_a_complete_ranking() -> None:
    small = tags_for(200)
    capacity = tags_for(12_500)

    assert small[0] == "#2PP"
    assert len(set(capacity)) == 12_500
    assert capacity[:200] == small
    ranking = ranking_payload(small)
    assert len(ranking["items"]) == 200  # type: ignore[arg-type]


def test_profiles_keep_the_weekly_previous_id_regression_case() -> None:
    profile = profile_payload("#2PP", 0)

    assert profile["leagueTier"] == {"id": 105000036, "name": "Legend I"}
    assert profile["currentLeagueSeasonId"] - profile["previousLeagueSeasonId"] == (
        7 * 24 * 60 * 60
    )


def test_clash_fixture_serves_rankings_profiles_and_verification() -> None:
    population = tags_for(200)
    handler = type(
        "TestClashHandler",
        (ClashHandler,),
        {
            "population": population,
            "tag_indexes": {tag: i for i, tag in enumerate(population)},
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    authorization = {"Authorization": "Bearer synthetic-key"}
    try:
        ranking_request = urllib.request.Request(
            f"{origin}/v1/locations/global/rankings/players", headers=authorization
        )
        with urllib.request.urlopen(ranking_request) as response:
            ranking = json.load(response)
        assert len(ranking["items"]) == 200

        profile_request = urllib.request.Request(
            f"{origin}/v1/players/%232PP", headers=authorization
        )
        with urllib.request.urlopen(profile_request) as response:
            profile = json.load(response)
        assert profile["name"] == "Synthetic Clasher 001"

        reset_request = urllib.request.Request(
            f"{origin}/_trial/reset",
            data=b"",
            headers=authorization,
            method="POST",
        )
        with urllib.request.urlopen(reset_request) as response:
            assert json.load(response)["generation"] == 1
        for _request in range(2):
            with urllib.request.urlopen(profile_request) as response:
                assert json.load(response)["_trialGeneration"] == 1
        stats_request = urllib.request.Request(
            f"{origin}/_trial/stats", headers=authorization
        )
        with urllib.request.urlopen(stats_request) as response:
            stats = json.load(response)
        assert stats["profile"]["requests"] == 2
        assert stats["profile"]["revisited_players"] == 1

        verification_request = urllib.request.Request(
            f"{origin}/v1/players/%232PP/verifytoken",
            data=b'{"token":"VERIFY-2PP"}',
            headers={**authorization, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(verification_request) as response:
            assert json.load(response) == {
                "tag": "#2PP", "token": "VERIFY-2PP", "status": "ok"
            }
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_disk_archive_persists_bytes_and_refuses_an_overwrite(tmp_path: Path) -> None:
    marker = tmp_path / ARCHIVE_MARKER_KEY
    marker.parent.mkdir(parents=True)
    marker.write_bytes(ARCHIVE_MARKER_BODY)
    handler = type("TestArchiveHandler", (ArchiveHandler,), {"root": tmp_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = b'{"synthetic":true}'
    digest = hashlib.sha256(body).hexdigest()
    url = f"http://127.0.0.1:{server.server_port}/evidence/sha256/{digest[:2]}/{digest}"
    try:
        request = urllib.request.Request(
            url,
            data=body,
            method="PUT",
            headers={"If-None-Match": "*"},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
        with urllib.request.urlopen(url) as response:
            assert response.read() == body
            assert response.headers["X-Amz-Meta-Sha256"] == digest
            assert parsedate_to_datetime(response.headers["Last-Modified"])
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 412
        overwrite = urllib.request.Request(url, data=b"different", method="PUT")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(overwrite)
        assert error.value.code == 412
        with urllib.request.urlopen(url) as response:
            assert response.read() == body
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.mark.parametrize("duplicate_body", [b"", b"different"])
@pytest.mark.parametrize("headers", [{}, {"If-None-Match": "*"}])
def test_refused_archive_upload_keeps_connection_usable(
    tmp_path: Path, duplicate_body: bytes, headers: dict[str, str]
) -> None:
    handler = type("TestArchiveHandler", (ArchiveHandler,), {"root": tmp_path})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    original = b'{"synthetic":true}'
    try:
        connection.request("PUT", "/evidence/original", body=original)
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        socket = connection.sock

        connection.request(
            "PUT", "/evidence/original", body=duplicate_body, headers=headers
        )
        response = connection.getresponse()
        assert response.status == 412
        response.read()
        assert connection.sock is socket

        connection.request("GET", "/evidence/original")
        response = connection.getresponse()
        assert response.status == 200, response.reason
        assert response.read() == original
        assert connection.sock is socket

        connection.request("PUT", "/evidence/next", body=b"next upload")
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert (tmp_path / "next").read_bytes() == b"next upload"
        assert connection.sock is socket
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
