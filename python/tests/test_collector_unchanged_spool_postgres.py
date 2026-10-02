from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import domain_database
from test_collector import _collector
from test_collector_uploads_postgres import NOW, _handoff, _player

from clashlens.collector_db import CollectorDatabase, CollectorWork, ResponseHandoff
from clashlens.collector_http import FetchedResponse, KeyPool
from clashlens.spool import Spool


def _profile(rank: int, trophies: int = 5000) -> bytes:
    season = {"rank": rank, "trophies": trophies}
    return json.dumps(
        {"tag": "#2PP", "trophies": trophies, "legendStatistics": {"currentSeason": season}}
    ).encode()


class _Profiles:
    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = bodies

    async def fetch_player(
        self, _pool: KeyPool, _tag: str, endpoint: str
    ) -> FetchedResponse:
        now = datetime.now(UTC)
        return FetchedResponse(
            endpoint, self.bodies.pop(0), 200, now, now, "regular-1",
            {"content-type": "application/json"},
        )


def _sightings(connection_info: str) -> tuple[int, datetime, int]:
    """Polls counted, the kept body's latest sighting, and observations."""
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """
            SELECT state.request_count, upload.latest_sighting_at,
                   (SELECT count(*) FROM collector_observations)
            FROM collector_response_state AS state
            JOIN collector_observations AS observation
              ON observation.id = state.last_observation_id
            JOIN collector_response_uploads AS upload
              ON upload.response_hash = observation.response_hash
            """
        ).fetchone()
    assert row is not None
    return int(row[0]), row[1], int(row[2])


def test_unchanged_profile_is_recorded_without_saving_its_bytes(
    database_url: str, tmp_path: Path
) -> None:
    # Only the ignored rank differs, so the second body is an unchanged poll.
    first, ignored, changed = _profile(1), _profile(2), _profile(2, trophies=5032)
    with domain_database(database_url) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(tmp_path / "spool", max_body_bytes=4 << 20)
        client = _Profiles([first, ignored, changed])
        collector = _collector(spool, database, client)  # type: ignore[arg-type]
        work = CollectorWork(player_id, "#2PP", datetime.now(UTC))

        async def scenario() -> None:
            async def poll() -> list[str]:
                return await collector.collect_player(
                    work, lane="ordinary", endpoints=("profile",)
                )

            assert await poll() == ["recorded"]
            first_sighting = _sightings(connection_info)[1]
            assert await poll() == ["recorded"]
            polls, sighting, observations = _sightings(connection_info)
            assert (polls, observations) == (2, 1)
            assert sighting > first_sighting
            assert hashlib.sha256(ignored).hexdigest() not in spool.final_hashes()
            assert spool.iter_handoffs() == []

            assert await poll() == ["recorded"]
            assert _sightings(connection_info)[2] == 2
            assert hashlib.sha256(changed).hexdigest() in spool.final_hashes()

        try:
            asyncio.run(scenario())
        finally:
            database.close()
            spool.close()


def test_unsaved_sighting_keeps_a_saved_refresh_recoverable(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        database = CollectorDatabase(connection_info)
        first = _handoff(
            occurrence_key="first",
            response_hash="a" * 64,
            player_id=_player(connection_info),
            content_fingerprint="f" * 64,
        )

        def poll(key: str, digest: str, minutes: int) -> ResponseHandoff:
            return replace(
                first,
                occurrence_key=key,
                response_hash=digest * 64,
                spool_key=f"sha256/{digest * 2}/{digest * 64}",
                response_completed_at=NOW + timedelta(minutes=minutes),
            )

        try:
            database.record_response(first)
            # A saved Refresh with the same used fields commits, then an
            # overlapping unsaved sighting commits before the Refresh's saved
            # record is removed, and the collector crashes.
            refresh = poll("refresh", "b", 1)
            database.record_response(refresh)
            assert database.record_unchanged_response(poll("unsaved", "c", 2))
            with psycopg.connect(connection_info) as connection:
                marker = connection.execute(
                    "SELECT last_applied_occurrence_key FROM collector_response_state"
                ).fetchone()
            assert marker == ("refresh",)
            database.record_recovered_response(refresh, serialized=True)
            assert _sightings(connection_info)[0] == 3
        finally:
            database.close()
