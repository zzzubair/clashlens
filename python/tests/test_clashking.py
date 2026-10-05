from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from domain_test_support import store_observation
from fastapi.testclient import TestClient
from test_api_migration import migrated_production_database
from test_private_api import NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens import clashking as clashking_module
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.clashking import (
    FIRST_BACKOFF_SECONDS,
    ClashKingClient,
    ClashKingUnavailable,
    _urllib_transport,
    parse_season_finishes,
)
from clashlens.db import Database
from clashlens.league_history import (
    LEAGUE_HISTORY_PARSER_VERSION,
    complete_league_history,
    parse_league_history,
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
        # A v2 label is one week before its Season ends.
        ("1786338000", _start("2026-08-10"), _start("2026-09-07"), 5600, 4),
        ("1783918800", _start("2026-07-13"), _start("2026-08-10"), 5856, 1),
        ("1781499600", _start("2026-06-15"), _start("2026-07-13"), 5909, 3),
        # The v2 row wins over the dated row for the same Season.
        ("1776661200", _start("2026-04-20"), _start("2026-05-18"), 5456, 78),
        ("1774242000", _start("2026-03-23"), _start("2026-04-20"), 5791, 100),
        ("1764565200", _start("2025-12-01"), _start("2025-12-29"), 6562, 8),
        ("1759726800", _start("2025-10-06"), _start("2025-11-03"), 5935, 10),
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
        for day in ("2026-03-23", "2025-12-01", "2025-09-08")
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
        _start("2026-07-13"),
        _start("2026-06-15"),
        _start("2026-05-18"),
    ]


def test_unfinished_off_phase_and_unreadable_rows_are_left_out() -> None:
    finishes = parse_season_finishes(FIXTURE.read_bytes(), now=NOW)
    ids = {finish.season_id for finish in finishes}

    assert any(
        finish.source_season == "v2-2026-08-31T05:00:00Z"
        and finish.season_end == _start("2026-09-07")
        for finish in finishes
    )
    future = b'{"items":[{"season":"v2-2026-09-28T05:00:00Z","trophies":5400}]}'
    assert parse_season_finishes(future, now=NOW) == []
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
    answers = [
        (200, {}, b"{}"),
        (429, {"Retry-After": "120"}, b""),
        (503, {}, b""),
        (200, {}, b"{}"),
    ]
    calls: list[str] = []

    def transport(url: str, timeout: float):
        calls.append(url)
        return answers.pop(0)

    client = ClashKingClient(transport=transport, clock=clock)
    assert client.try_acquire()
    client.release()
    assert client.try_acquire()  # checking does not use up the gap
    client.release()
    assert client.fetch_legend_history("#2PP") == b"{}"
    assert not client.try_acquire()  # two requests a second at most
    assert client.fetch_legend_history("#2PP") is None  # refused, not sent
    assert len(calls) == 1
    clock.now += 0.5
    assert client.try_acquire()
    client.release()

    with pytest.raises(ClashKingUnavailable):
        client.fetch_legend_history("#2PP")
    assert calls == ["https://api.clashk.ing/v2/player/%232PP/legend-history"] * 2
    clock.now += 119
    assert not client.try_acquire()  # ClashKing asked for two minutes
    clock.now += 1
    assert client.try_acquire()
    client.release()
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


def test_at_most_two_requests_run_at_once() -> None:
    clock = FakeClock()
    client = ClashKingClient(
        transport=lambda url, timeout: (200, {}, b"{}"), clock=clock
    )
    assert client.try_acquire()
    assert client.try_acquire()
    started = time.monotonic()
    assert not client.try_acquire()  # both busy: refused at once, not queued
    assert time.monotonic() - started < 0.1
    client.release()
    assert client.try_acquire()


def test_redirects_are_not_followed() -> None:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/legend-history"
        assert _urllib_transport(url, 2.0)[0] == 302
    finally:
        server.shutdown()
        server.server_close()
    assert paths == ["/legend-history"]

    client = ClashKingClient(transport=lambda url, timeout: (302, {}, b""))
    with pytest.raises(ClashKingUnavailable):
        client.fetch_legend_history("#2PP")


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
                assert body["fetched_at"] == NOW.isoformat()
                # The page shows only seasons from January 2025 onwards.
                assert len(body["seasons"]) == 8
                assert body["seasons"][0] == {
                    "season_id": "1786338000",
                    "season_start": "2026-08-10T05:00:00+00:00",
                    "season_end": "2026-09-07T05:00:00+00:00",
                    "trophies": 5600,
                    "global_rank": 4,
                }
                assert body["seasons"][-1]["season_id"] == "2025-09"

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
                    " VALUES ('#2PP', true, 'eligible'), ('#2QQ', true, 'eligible'),"
                    " ('#2RR', true, 'eligible')"
                )
            fake = FakeClashKing()
            clock = FakeClock()
            clashking = ClashKingClient(transport=fake, clock=clock)
            app = create_app(
                database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
                clashking_client=clashking,
            )

            def view(tag: str):
                target = f"/v1/players/{tag}/past-seasons"
                return client.get(target, headers=signed_headers(target)).json()

            def fetch_rows(tag: str):
                with database.pool.connection() as connection:
                    return connection.execute(
                        "SELECT history.attempted_at FROM clashking_history_fetches"
                        " AS history JOIN players AS player"
                        " ON player.id = history.player_id"
                        " WHERE player.normalized_tag = %s",
                        (tag,),
                    ).fetchall()

            with TestClient(app) as client:
                assert len(view("%232PP")["seasons"]) == 8
                clock.now += 0.1
                refused = view("%232QQ")
                assert fake.calls == 1
                assert refused["seasons"] == []
                assert refused["fetched_at"] is None
                assert fetch_rows("#2QQ") == []  # refused before any write

                clock.now += 0.5
                retried = view("%232QQ")
                assert fake.calls == 2
                assert len(retried["seasons"]) == 8
                assert retried["fetched_at"] == NOW.isoformat()

                # Two requests already running also refuse without a write.
                for _ in range(2):
                    clock.now += 0.5
                    assert clashking.try_acquire()
                clock.now += 0.5
                assert view("%232RR")["seasons"] == []
                assert fake.calls == 2
                assert fetch_rows("#2RR") == []
        finally:
            database.close()


def test_a_slow_claim_cannot_send_inside_the_gap_or_a_pause(
    database_url: str, monkeypatch: pytest.MonkeyPatch
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
            clashking = ClashKingClient(transport=fake, clock=clock)
            app = create_app(
                database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
                clashking_client=clashking,
            )
            real_claim = clashking_module._claim

            def slow_claim(*args):
                claimed = real_claim(*args)
                # Another view's request goes out while this claim is running.
                try:
                    clashking.fetch_legend_history("#2QQ")
                except ClashKingUnavailable:
                    pass
                return claimed

            monkeypatch.setattr(clashking_module, "_claim", slow_claim)

            def view():
                target = "/v1/players/%232PP/past-seasons"
                return client.get(target, headers=signed_headers(target)).json()

            def attempted_at():
                with database.pool.connection() as connection:
                    return connection.execute(
                        "SELECT history.attempted_at FROM clashking_history_fetches"
                        " AS history JOIN players AS player"
                        " ON player.id = history.player_id"
                        " WHERE player.normalized_tag = '#2PP'"
                    ).fetchall()

            with TestClient(app) as client:
                assert view()["seasons"] == []
                assert fake.calls == 1  # only the other view's request was sent
                assert attempted_at() == [(None,)]  # the claim was given back

                fake.answer = (500, {}, b"")
                clock.now += 0.5
                assert view()["seasons"] == []
                assert fake.calls == 2  # the other request failed and paused us
                assert attempted_at() == [(None,)]

                monkeypatch.setattr(clashking_module, "_claim", real_claim)
                fake.answer = (200, {}, FIXTURE.read_bytes())
                clock.now += FIRST_BACKOFF_SECONDS
                assert len(view()["seasons"]) == 8
                assert fake.calls == 3
        finally:
            database.close()


@pytest.mark.parametrize(
    ("tag", "results"),
    [
        ("#RPYP0QUC", [(5437, 180), (5430, 194), (5164, 2703)]),
        ("#L2LJ9QLU", [(5049, 6542), (5222, 1771), (4980, 8485)]),
        ("#PC2QRC9QY", [(5200, 2359), (5325, 674), (5394, 290)]),
    ],
)
def test_past_seasons_use_official_results_and_remap_old_cached_finishes(
    database_url: str, archive_server, tag: str, results: list[tuple[int, int]]
) -> None:
    current = _start("2026-10-05") + timedelta(hours=7)
    ends = ["2026-10-05", "2026-09-07", "2026-08-10"]
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        fake = FakeClashKing()
        clock = FakeClock()
        try:
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id",
                    (tag,),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO clashking_history_fetches VALUES (%s, %s, %s)",
                    (player_id, current, current),
                )
                # Cache the pre-fix IDs. Both source label styles must re-map,
                # including older rows with no official replacement.
                for source, wrong_start, trophies, rank in [
                    ("v2-2026-08-03T05:00:00Z", "2026-08-10", *results[2]),
                    ("2026-07-13", "2026-07-13", 5205, 1852),
                ]:
                    start = _start(wrong_start)
                    connection.execute(
                        "INSERT INTO clashking_season_finishes VALUES"
                        " (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            player_id,
                            str(int(start.timestamp())),
                            start,
                            start + timedelta(days=28),
                            source,
                            trophies,
                            rank,
                        ),
                    )
            app = create_app(
                database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: current,
                clashking_client=ClashKingClient(transport=fake, clock=clock),
            )
            with TestClient(app) as client:
                target = f"/v1/players/{tag.replace('#', '%23')}/past-seasons"

                def view():
                    response = client.get(target, headers=signed_headers(target))
                    assert response.status_code == 200
                    return response.json()

                cached = view()["seasons"]
                assert cached[0]["season_end"] == _start("2026-08-10").isoformat()
                assert cached[0]["season_id"] == "1783918800"
                assert cached[0]["trophies"] == results[2][0]
                assert cached[1]["season_end"] == _start("2026-07-13").isoformat()
                assert fake.calls == 0
                # Deliberately disagree, proving that the official row wins.
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE clashking_season_finishes SET trophies=1, global_rank=999"
                        " WHERE player_id=%s AND source_season LIKE 'v2-%%'",
                        (player_id,),
                    )
                items = [
                    {
                        "leagueSeasonId": str(int(_start(end).timestamp())),
                        "leagueTrophies": trophies,
                        "placement": rank,
                        "leagueTierId": 105000036,
                    }
                    for end, (trophies, rank) in zip(ends, results, strict=True)
                ]
                items += [
                    {
                        **items[0],
                        "leagueSeasonId": str(int(_start(end).timestamp())),
                        "leagueTierId": tier,
                    }
                    for end, tier in [
                        ("2026-07-06", 105000036),
                        ("2026-06-15", 105000035),
                        ("2026-11-02", 105000036),
                    ]
                ]
                body = json.dumps({"items": items}).encode()
                _, job_id = store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key="official-finishes",
                    endpoint="league_history",
                    body=body,
                    observed_at=current,
                    normalized_tag=tag,
                    parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                    processing_version="clashlens-domain-processing-v1",
                    domain_rule_version="clashlens-domain-rules-v1",
                )
                worker = Database(connection_info)
                try:
                    claim = worker.claim_job(owner="past-seasons-test", job_id=job_id)
                    assert claim is not None
                    complete_league_history(
                        worker,
                        claim,
                        parse_league_history(
                            body, expected_tag=tag, observed_at=current
                        ),
                    )
                finally:
                    worker.close()
                merged = view()
                assert [
                    (
                        row["season_end"],
                        row["trophies"],
                        row["global_rank"],
                    )
                    for row in merged["seasons"]
                ] == [
                    (_start(end).isoformat(), *result)
                    for end, result in zip(ends, results, strict=True)
                ] + [(_start("2026-07-13").isoformat(), 5205, 1852)]
                assert fake.calls == 0

                current += timedelta(days=1)
                fake.answer = (503, {}, b"")
                assert view() == merged  # official and cached data survive failure
                assert fake.calls == 1
                current += timedelta(hours=1)
                clock.now += 3600
                fake.answer = (200, {}, b'{"items":[]}')
                assert view()["seasons"] == merged["seasons"][:3]
                # Missing official fields stay unknown, never borrowed from CK.
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE player_league_history_entries"
                        " SET league_trophies=NULL, placement=NULL WHERE player_id=%s",
                        (player_id,),
                    )
                current += timedelta(days=1)
                clock.now += 3600
                fake.answer = (200, {}, FIXTURE.read_bytes())
                unknown = next(
                    row for row in view()["seasons"] if row["season_id"] == "1783918800"
                )
                assert unknown["trophies"] is None and unknown["global_rank"] is None
                with database.pool.connection() as connection:
                    stored = connection.execute(
                        "SELECT season_id, season_end FROM clashking_season_finishes"
                        " WHERE player_id=%s AND source_season='v2-2026-08-03T05:00:00Z'",
                        (player_id,),
                    ).fetchone()
                assert stored == ("1783918800", _start("2026-08-10"))
        finally:
            database.close()
