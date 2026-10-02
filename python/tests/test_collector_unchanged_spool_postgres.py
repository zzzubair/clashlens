from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
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
            # Under 10 minutes later, the shared body's sighting time stays put.
            assert sighting == first_sighting
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


def test_timed_out_unchanged_check_still_saves_the_response(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, ignored = _profile(1), _profile(2)
    with domain_database(database_url) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(tmp_path / "spool", max_body_bytes=4 << 20)
        collector = _collector(spool, database, _Profiles([first, ignored]))  # type: ignore[arg-type]
        work = CollectorWork(player_id, "#2PP", datetime.now(UTC))
        errors: list[Exception] = []
        check = database.record_unchanged_response

        def timed(handoff: ResponseHandoff) -> bool:
            try:
                return check(handoff)
            except psycopg.Error as error:
                errors.append(error)
                raise

        monkeypatch.setattr(database, "record_unchanged_response", timed)

        async def scenario() -> None:
            def poll() -> asyncio.Future[list[str]]:
                return asyncio.ensure_future(
                    collector.collect_player(work, lane="ordinary", endpoints=("profile",))
                )

            assert await poll() == ["recorded"]
            # A lock held past the fast path's two-second limit makes
            # PostgreSQL end its transaction; the response is then saved.
            with psycopg.connect(connection_info) as holder:
                holder.execute("SELECT 1 FROM collector_response_state FOR UPDATE")
                second = poll()
                await asyncio.sleep(3)
            assert await second == ["recorded"]

        try:
            asyncio.run(scenario())
            assert [type(error) for error in errors] == [
                psycopg.errors.TransactionTimeout
            ]
            polls, _, observations = _sightings(connection_info)
            assert (polls, observations) == (2, 1)
            assert hashlib.sha256(ignored).hexdigest() in spool.final_hashes()
            assert spool.iter_handoffs() == []
        finally:
            database.close()
            spool.close()


@pytest.mark.parametrize(
    "hold",
    [
        # Worker rows that point at the last observation key-share it.
        "SELECT 1 FROM collector_observations FOR KEY SHARE",
        # The worker locks the player it ingests, as the 0040 trigger would.
        "SELECT 1 FROM players FOR NO KEY UPDATE",
    ],
)
def test_unchanged_check_saves_at_once_when_the_worker_holds_a_row(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    first, ignored = _profile(1), _profile(2)
    with domain_database(database_url) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(tmp_path / "spool", max_body_bytes=4 << 20)
        collector = _collector(spool, database, _Profiles([first, ignored]))  # type: ignore[arg-type]
        work = CollectorWork(player_id, "#2PP", datetime.now(UTC))
        checks: list[tuple[bool | None, float]] = []
        check = database.record_unchanged_response

        def timed(handoff: ResponseHandoff) -> bool:
            started, result = time.monotonic(), None
            try:
                result = check(handoff)
                return result
            finally:
                checks.append((result, time.monotonic() - started))

        monkeypatch.setattr(database, "record_unchanged_response", timed)

        async def scenario() -> None:
            def poll() -> asyncio.Future[list[str]]:
                return asyncio.ensure_future(
                    collector.collect_player(work, lane="ordinary", endpoints=("profile",))
                )

            assert await poll() == ["recorded"]
            with psycopg.connect(connection_info) as holder:
                holder.execute(hold)
                second = poll()
                while not checks:
                    await asyncio.sleep(0.05)
            assert await second == ["recorded"]

        try:
            asyncio.run(scenario())
            assert len(checks) == 1
            assert checks[0][0] is False
            assert checks[0][1] < 1
            polls, _, observations = _sightings(connection_info)
            assert (polls, observations) == (2, 1)
            assert hashlib.sha256(ignored).hexdigest() in spool.final_hashes()
            assert spool.iter_handoffs() == []
        finally:
            database.close()
            spool.close()


# The 28-day season grid in clashlens_season_retire_after (migration 0026).
_SEASON = timedelta(days=28)
_ANCHOR = datetime.fromtimestamp(1783918800, UTC)
_SEASON_START = _ANCHOR + _SEASON * ((NOW - _ANCHOR) // _SEASON + 1)


@pytest.mark.parametrize(
    ("first_at", "again_at", "recorded_while_held"),
    [
        # Sighted 5 minutes ago: its shared rows are left alone, so a held
        # upload row does not matter.
        (NOW, NOW + timedelta(minutes=5), True),
        # A day later the sighting time moves, so the held row is skipped.
        (NOW, NOW + timedelta(days=1), False),
        # Minutes apart but across a season start, the retention deadline
        # moves, so the held row is skipped too.
        (
            _SEASON_START - timedelta(minutes=2),
            _SEASON_START + timedelta(minutes=2),
            False,
        ),
    ],
)
def test_unchanged_check_skips_a_shared_body_another_sighting_holds(
    database_url: str,
    first_at: datetime,
    again_at: datetime,
    recorded_while_held: bool,
) -> None:
    # Players can share one body (hundreds get the same not-found profile),
    # so a held upload row must never make the sighting wait.
    with domain_database(database_url) as connection_info:
        database = CollectorDatabase(connection_info)
        player_id = _player(connection_info)

        def poll(key: str, at: datetime) -> ResponseHandoff:
            return _handoff(
                occurrence_key=key, response_hash="a" * 64,
                player_id=player_id, completed_at=at,
            )

        try:
            database.record_response(poll("first", first_at))
            with psycopg.connect(connection_info) as holder:
                holder.execute("SELECT 1 FROM collector_response_uploads FOR UPDATE")
                started = time.monotonic()
                recorded = database.record_unchanged_response(poll("again", again_at))
                assert time.monotonic() - started < 1
                assert recorded is recorded_while_held
                assert _sightings(connection_info)[0] == (2 if recorded else 1)
            if not recorded:
                assert database.record_unchanged_response(poll("again", again_at))
            polls, sighting, _ = _sightings(connection_info)
            assert polls == 2
            assert sighting == (first_at if recorded_while_held else again_at)
        finally:
            database.close()


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
