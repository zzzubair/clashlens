from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api_migration import migrated_production_database
from test_private_api import NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.clashking import (
    ClashKingClient,
    ClashKingUnavailable,
    parse_season_finishes,
)

FIXTURE = Path(__file__).parents[1] / "testdata" / "clashking_legend_history.json"
NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def _start(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T05:00:00+00:00")


def test_rows_map_to_our_seasons_without_duplicates() -> None:
    finishes = parse_season_finishes(FIXTURE.read_bytes(), now=NOW)

    assert [
        (f.season_id, f.season_start, f.season_end, f.trophies, f.global_rank)
        for f in finishes
    ] == [
        # A v2 label is one week before its Season starts.
        ("1786338000", _start("2026-08-10"), _start("2026-09-07"), 5856, 1),
        ("1783918800", _start("2026-07-13"), _start("2026-08-10"), 5909, 3),
        # The v2 row wins over the dated row for the same Season.
        ("1779080400", _start("2026-05-18"), _start("2026-06-15"), 5456, 78),
        ("1776661200", _start("2026-04-20"), _start("2026-05-18"), 5791, 100),
        ("1766984400", _start("2025-12-29"), _start("2026-01-26"), 6562, 8),
        ("1762146000", _start("2025-11-03"), _start("2025-12-01"), 5935, 10),
        # The 2025-10-06 rows only repeat September's calendar-month result.
        ("2025-09", None, None, 6232, 399),
        ("2024-07", None, None, 5011, 934651),
        ("2021-12", None, None, 4965, None),
    ]


def test_distinct_seasons_with_the_same_trophies_and_rank_are_all_kept() -> None:
    rows = [
        {"season": season, "trophies": 6562, "rank": 8}
        for season in ("2024-07", "2025-12-29", "v2-2026-04-13T05:00:00Z", "2025-10-06")
    ]
    payload = json.dumps({"items": rows}).encode()

    assert [f.season_id for f in parse_season_finishes(payload, now=NOW)] == [
        str(int(_start(day).timestamp()))
        for day in ("2026-04-20", "2025-12-29", "2025-10-06")
    ] + ["2024-07"]


def test_legend_rows_are_recognized_by_tier_id_or_name() -> None:
    rows = [
        {"season": "v2-2026-08-03T05:00:00Z", "leagueTier": {"id": 105000036}},
        {"season": "2026-07-13", "leagueTier": {"name": "Legend League II"}},
        {"season": "2026-06-15"},
        {"season": "2026-05-18", "leagueTier": {"id": 105000000}},
        {"season": "2026-04-20", "leagueTier": {"id": 105000000, "name": "Unranked"}},
    ]
    payload = json.dumps(
        {"items": [{**row, "trophies": 5500, "rank": 2} for row in rows]}
    ).encode()

    assert [f.season_start for f in parse_season_finishes(payload, now=NOW)] == [
        _start("2026-08-10"),
        _start("2026-07-13"),
        _start("2026-06-15"),
    ]


def test_unfinished_off_phase_and_unreadable_rows_are_left_out() -> None:
    finishes = parse_season_finishes(FIXTURE.read_bytes(), now=NOW)
    ids = {finish.season_id for finish in finishes}

    assert "1788757200" not in ids  # Season of 2026-09-07 is still running
    assert str(int(_start("2026-04-21").timestamp())) not in ids  # off our phase
    assert {"2026-13", "2023-05"}.isdisjoint(ids)
    assert parse_season_finishes(b'{"items":[]}', now=NOW) == []
    for payload in (b"<html>", b'{"items":{}}', b"[]"):
        with pytest.raises(ClashKingUnavailable):
            parse_season_finishes(payload, now=NOW)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_requests_are_spaced_and_paused_after_overload() -> None:
    clock = FakeClock()
    answers = [(429, {"Retry-After": "120"}, b""), (503, {}, b""), (200, {}, b"{}")]
    calls: list[str] = []

    def transport(url: str, timeout: float):
        calls.append(url)
        return answers.pop(0)

    client = ClashKingClient(transport=transport, clock=clock)
    assert client.try_acquire()
    assert not client.try_acquire()  # two requests a second at most
    clock.now += 0.5
    assert client.try_acquire()

    with pytest.raises(ClashKingUnavailable):
        client.fetch_legend_history("#2PP")
    assert calls == ["https://api.clashk.ing/v2/player/%232PP/legend-history"]
    clock.now += 119
    assert not client.try_acquire()  # ClashKing asked for two minutes
    clock.now += 1
    assert client.try_acquire()
    with pytest.raises(ClashKingUnavailable):
        client.fetch_legend_history("#2PP")
    clock.now += 119
    assert not client.try_acquire()  # second failure doubles the pause
    clock.now += 1
    assert client.try_acquire()
    assert client.fetch_legend_history("#2PP") == b"{}"


def test_no_answer_pauses_and_disabled_client_never_requests() -> None:
    clock = FakeClock()

    def transport(url: str, timeout: float):
        raise TimeoutError

    client = ClashKingClient(transport=transport, clock=clock)
    with pytest.raises(ClashKingUnavailable):
        client.fetch_legend_history("#2PP")
    clock.now += 59
    assert not client.try_acquire()
    assert not ClashKingClient(enabled=False, clock=clock).try_acquire()


class FakeClashKing:
    def __init__(self) -> None:
        self.calls = 0
        self.answer: tuple[int, dict[str, str], bytes] = (200, {}, FIXTURE.read_bytes())

    def __call__(self, url: str, timeout: float):
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def test_viewed_players_refresh_once_a_day_and_keep_rows_through_failures(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag, active, eligibility_state)"
                    " VALUES ('#2PP', true, 'eligible')"
                )
            fake = FakeClashKing()
            clock = FakeClock()
            current = NOW
            app = create_app(
                database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: current,
                clashking_client=ClashKingClient(transport=fake, clock=clock),
            )

            def view(tag: str = "%232PP"):
                clock.now += 1
                target = f"/v1/players/{tag}/past-seasons"
                return client.get(target, headers=signed_headers(target))

            with TestClient(app) as client:
                first = view()
                assert first.status_code == 200
                body = first.json()
                assert body["source"] == "clashking"
                assert body["fetched_at"] == NOW.isoformat()
                assert len(body["seasons"]) == 9
                assert body["seasons"][0] == {
                    "season_id": "1786338000",
                    "season_start": "2026-08-10T05:00:00+00:00",
                    "season_end": "2026-09-07T05:00:00+00:00",
                    "trophies": 5856,
                    "global_rank": 1,
                }
                assert body["seasons"][-1]["season_id"] == "2021-12"

                current = NOW + timedelta(hours=23)
                assert view().json() == body
                assert fake.calls == 1  # saved rows serve the rest of the day

                # A failed refresh keeps showing the saved rows and waits an hour.
                fake.answer = (500, {}, b"")
                current = NOW + timedelta(days=1)
                assert view().json() == body
                assert fake.calls == 2
                clock.now += 3600
                current += timedelta(minutes=59)
                assert view().json() == body
                assert fake.calls == 2

                fake.answer = TimeoutError()
                current += timedelta(minutes=1)
                assert view().json() == body
                assert fake.calls == 3

                fake.answer = (200, {}, b'{"items":[]}')
                clock.now += 3600
                current += timedelta(hours=1)
                refreshed = view().json()
                assert fake.calls == 4
                assert refreshed["seasons"] == []
                assert refreshed["fetched_at"] == current.isoformat()

                # Unknown players are never looked up at ClashKing.
                assert view("%23QQQ").status_code == 404
                assert fake.calls == 4
        finally:
            database.close()


def test_a_view_refused_by_the_request_limit_can_retry_promptly(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag, active, eligibility_state)"
                    " VALUES ('#2PP', true, 'eligible'), ('#2QQ', true, 'eligible')"
                )
            fake = FakeClashKing()
            clock = FakeClock()
            app = create_app(
                database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
                clashking_client=ClashKingClient(transport=fake, clock=clock),
            )

            def view(tag: str):
                target = f"/v1/players/{tag}/past-seasons"
                return client.get(target, headers=signed_headers(target)).json()

            with TestClient(app) as client:
                assert len(view("%232PP")["seasons"]) == 9
                clock.now += 0.1
                refused = view("%232QQ")
                assert fake.calls == 1
                assert refused["seasons"] == []
                assert refused["fetched_at"] is None

                clock.now += 0.5
                retried = view("%232QQ")
                assert fake.calls == 2
                assert len(retried["seasons"]) == 9
                assert retried["fetched_at"] == NOW.isoformat()
        finally:
            database.close()
