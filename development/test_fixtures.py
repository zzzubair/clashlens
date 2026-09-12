from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
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

        verification_request = urllib.request.Request(
            f"{origin}/v1/players/%232PP/verifytoken",
            data=b'{"token":"VERIFY-2PP"}',
            headers={**authorization, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(verification_request) as response:
            assert json.load(response) == {"status": "ok"}
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
