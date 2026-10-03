from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database
from test_freshness_metrics_postgres import seed_check
from test_private_api import NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens import api_leaderboard, api_players
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase


@pytest.fixture()
def board_database(database_url: str):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            alphabet = "0289PYLQGRJCUV"
            for index in range(105):
                tag = "#P" + alphabet[index // 14] + alphabet[index % 14]
                seed_profile(database, tag, 6000)
            yield database
        finally:
            database.close()


def test_search_keeps_whole_board_tie_ranks_and_focus_tracks_moves(board_database):
    database = board_database
    board = api_leaderboard.get_live_leaderboard(database, limit=200, now=NOW)
    target = board["entries"][102]
    with database.pool.connection() as connection:
        connection.execute(
            "UPDATE player_profile_versions SET name = %s WHERE normalized_tag = %s",
            ("Nova 100%_winner", target["tag"]),
        )
    for query in ("nova", "100%_", target["tag"].lower(), target["tag"][1:]):
        found = api_leaderboard.search_live_leaderboard(database, query, now=NOW)
        assert found["results"] == [
            {
                "tag": target["tag"],
                "name": "Nova 100%_winner",
                "rank": 103,
                "trophies": 6000,
            }
        ]
        assert found["exact_tag"] == (target["tag"] if query.startswith("#") else None)
    focused = api_leaderboard.get_live_leaderboard(
        database, limit=100, now=NOW, focus_tag=target["tag"]
    )
    assert focused["page"] == 2
    assert [entry["position"] for entry in focused["entries"]] == list(range(98, 106))
    assert focused["entries"][5]["tag"] == target["tag"]
    with database.pool.connection() as connection:
        connection.execute(
            "UPDATE player_profile_versions SET trophies = 7000 WHERE normalized_tag = %s",
            (target["tag"],),
        )
    moved = api_leaderboard.get_live_leaderboard(
        database, limit=100, offset=100, now=NOW, focus_tag=target["tag"]
    )
    assert moved["page"] == 1
    assert moved["entries"][0]["tag"] == target["tag"]
    assert moved["entries"][0]["position"] == 1
    assert len(moved["entries"]) == 100


def test_search_hides_not_found_inactive_and_unaccepted_players(board_database):
    database = board_database
    hidden = ("#P00", "#P02", "#P08")
    with database.pool.connection() as connection:
        seed_check(connection, hidden[0], "profile", None, not_found=NOW)
        connection.execute(
            "UPDATE players SET active = false WHERE normalized_tag = %s", (hidden[1],)
        )
        connection.execute(
            "UPDATE player_profile_versions SET source_contract_state = 'quarantined' "
            "WHERE normalized_tag = %s",
            (hidden[2],),
        )
    board = api_leaderboard.get_live_leaderboard(database, limit=200, now=NOW)
    for tag in hidden:
        assert api_leaderboard.search_live_leaderboard(database, tag, now=NOW)["results"] == []
        assert (
            api_leaderboard.get_live_leaderboard(
                database, limit=100, now=NOW, focus_tag=tag
            )
            is None
        )
    results = api_leaderboard.search_live_leaderboard(database, "Player", now=NOW)
    assert results["has_more"] is True
    assert len(results["results"]) == 20
    assert results["results"] == [
        {
            "tag": entry["tag"],
            "name": entry["name"],
            "rank": entry["position"],
            "trophies": entry["trophies"],
        }
        for entry in board["entries"][:20]
    ]
    assert board["total_entries"] == 102
    with database.pool.connection() as connection:
        connection.execute(
            "UPDATE collector_response_state SET last_success_at = %s + interval '1 second' "
            "WHERE identity_key = %s",
            (NOW, hidden[0]),
        )
    assert (
        api_leaderboard.search_live_leaderboard(database, hidden[0], now=NOW)["exact_tag"]
        == hidden[0]
    )


def test_leaderboard_search_and_focus_require_signed_requests(board_database):
    app = create_app(
        board_database,
        keys={("typescript-website", "current"): TS_CURRENT},
        clock=lambda: NOW_SECONDS,
        now=lambda: NOW,
    )
    search = "/v1/leaderboards/live/search?" + urlencode({"q": "#p00"})
    focus = "/v1/leaderboards/live?" + urlencode({"limit": 100, "focus_tag": "#P00"})
    with TestClient(app) as client:
        for target in (search, focus):
            assert client.get(target).status_code == 401
            response = client.get(target, headers=signed_headers(target))
            assert response.status_code == 200
        assert (
            client.get(search, headers=signed_headers(search)).json()["exact_tag"]
            == "#P00"
        )
        for query in (" ", "x" * 81):
            target = "/v1/leaderboards/live/search?" + urlencode({"q": query})
            assert client.get(target, headers=signed_headers(target)).status_code == 422
        for target in (
            "/v1/leaderboards/live?focus_tag=bad",
            "/v1/leaderboards/frozen?focus_tag=%23P00",
        ):
            assert client.get(target, headers=signed_headers(target)).status_code == 422


def test_focus_keeps_neighbors_across_page_edges(board_database):
    board = api_leaderboard.get_live_leaderboard(board_database, limit=200, now=NOW)

    def window(rank):
        focused = api_leaderboard.get_live_leaderboard(
            board_database,
            limit=100,
            now=NOW,
            focus_tag=board["entries"][rank - 1]["tag"],
        )
        return focused["page"], [entry["position"] for entry in focused["entries"]]

    assert window(101) == (2, list(range(96, 106)))
    assert window(100) == (1, list(range(1, 106)))
    assert window(50) == (1, list(range(1, 101)))


def test_only_hash_tags_select_and_name_matches_stay_listed(board_database):
    with board_database.pool.connection() as connection:
        connection.execute(
            "UPDATE player_profile_versions SET name = 'p00' WHERE normalized_tag = '#P02'"
        )
    board = api_leaderboard.get_live_leaderboard(board_database, limit=200, now=NOW)
    ranks = {entry["tag"]: entry["position"] for entry in board["entries"]}
    found = api_leaderboard.search_live_leaderboard(board_database, "p00", now=NOW)
    assert found["exact_tag"] is None
    assert [entry["tag"] for entry in found["results"]] == sorted(
        ("#P00", "#P02"), key=ranks.get
    )
    found = api_leaderboard.search_live_leaderboard(board_database, "#p00", now=NOW)
    assert found["exact_tag"] == "#P00"
    assert [entry["tag"] for entry in found["results"]] == ["#P00"]
    with board_database.pool.connection() as connection:
        connection.execute(
            "UPDATE player_profile_versions SET name = '#Nova' WHERE normalized_tag = '#P08'"
        )
    found = api_leaderboard.search_live_leaderboard(board_database, "#nova", now=NOW)
    assert found["exact_tag"] is None
    assert [entry["tag"] for entry in found["results"]] == ["#P08"]
    assert (
        api_leaderboard.search_live_leaderboard(board_database, "nobody", now=NOW)["results"]
        == []
    )


def test_players_waiting_for_their_season_reset_stay_off_the_live_board(
    database_url: str,
):
    from datetime import UTC, datetime, timedelta

    season_start = datetime(2026, 10, 5, 5, tzinfo=UTC)
    old_season = str(int((season_start - timedelta(days=28)).timestamp()))
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            read_at = season_start + timedelta(minutes=10)
            for tag, trophies in (
                ("#PP0", 6400), ("#PP2", 5000), ("#PP8", 5040), ("#PP9", 4900)
            ):
                seed_profile(database, tag, trophies, observed_at=read_at)

            def name_season(tag, season, trophies=None):
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE player_profile_versions SET"
                        " current_league_season_id = %s,"
                        " trophies = coalesce(%s, trophies) WHERE normalized_tag = %s",
                        (season, trophies, tag),
                    )

            name_season("#PP0", old_season)
            name_season("#PP9", old_season)
            now = season_start + timedelta(minutes=20)

            def ranks():
                board = api_leaderboard.get_live_leaderboard(
                    database, limit=100, now=now
                )
                found = api_leaderboard.search_live_leaderboard(
                    database, "Player", now=now
                )["results"]
                assert [(r["tag"], r["rank"]) for r in found] == [
                    (e["tag"], e["position"]) for e in board["entries"]
                ]
                return board, [e["tag"] for e in board["entries"]]

            def waiting(tag):
                page = api_players.get_player_page(
                    database, tag, now=now, freshness_seconds=900
                )
                labels = [q["label"] for q in page["screen_ready"]["data_quality"]]
                notice = "Waiting for this player's Season reset" in labels
                assert page["season_reset_pending"] is notice
                return notice

            board, tags = ranks()
            assert tags == ["#PP8", "#PP2"]
            assert board["total_entries"] == 2
            assert board["tracked_population"] == 4
            assert board["season_reset_pending"] == 2
            for tag in ("#PP0", "#PP9"):
                assert api_leaderboard.search_live_leaderboard(
                    database, tag, now=now
                )["results"] == []
                assert api_leaderboard.get_live_leaderboard(
                    database, limit=100, now=now, focus_tag=tag
                ) is None
            focused = api_leaderboard.get_live_leaderboard(
                database, limit=100, now=now, focus_tag="#PP2"
            )
            assert [e["tag"] for e in focused["entries"]] == ["#PP8", "#PP2"]
            metrics = api_leaderboard.live_freshness_metrics(database, now=now)
            assert metrics["entries"] == 2
            assert [waiting(tag) for tag in ("#PP0", "#PP2")] == [True, False]

            # Once the player's profile names the new Season, they rank again.
            name_season("#PP0", str(int(season_start.timestamp())), 5000)
            board, tags = ranks()
            assert sorted(tags[1:]) == ["#PP0", "#PP2"] and tags[0] == "#PP8"
            assert board["season_reset_pending"] == 1
            assert not waiting("#PP0")

            # The minutes before the Reset still belong to the old Season.
            now = season_start - timedelta(minutes=1)
            for tag in ("#PP2", "#PP8"):
                name_season(tag, old_season)
            name_season("#PP0", old_season, 6400)
            assert ranks()[1] == ["#PP0", "#PP8", "#PP2", "#PP9"]

            # A weekly Monday Reset inside a Season changes nothing.
            now = season_start - timedelta(days=7) + timedelta(minutes=20)
            assert ranks()[1] == ["#PP0", "#PP8", "#PP2", "#PP9"]
            assert not waiting("#PP0")
        finally:
            database.close()
