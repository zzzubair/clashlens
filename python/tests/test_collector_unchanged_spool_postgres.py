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

from clashlens.collector_db import CollectorDatabase, CollectorWork
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


def test_retried_compaction_counts_the_poll_once(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        database = CollectorDatabase(connection_info)
        first = _handoff(
            occurrence_key="first",
            response_hash="a" * 64,
            player_id=_player(connection_info),
        )
        # Same used fields as the first poll, different raw bytes.
        repeat = replace(
            first,
            occurrence_key="repeat",
            response_hash="b" * 64,
            spool_key=f"sha256/bb/{'b' * 64}",
            response_completed_at=NOW + timedelta(minutes=1),
        )
        try:
            # The first poll and work-bound responses always keep their bytes.
            assert not database.record_unchanged_response(first)
            database.record_response(first)
            assert not database.record_unchanged_response(
                replace(repeat, collector_work_id=1)
            )
            assert database.record_unchanged_response(repeat)
            assert database.record_unchanged_response(repeat)
            assert _sightings(connection_info)[0] == 2
        finally:
            database.close()
