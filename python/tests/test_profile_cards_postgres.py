from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import product
from typing import Any

from test_api_db_organization import create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database
from test_group_comparison_postgres import battle, link_player, seed_day

from clashlens import api_accounts, api_leaderboard
from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for


def test_profile_shows_each_linked_players_trophies_rank_league_and_today(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_owner(database)
            for tag, trophies in [
                ("#2PP", 5300), ("#8PY", 5400), ("#9PY", 5200), ("#LQ2", 5500),
                ("#PQ2", 5600), ("#YQ2", 4900),
            ]:
                seed_profile(database, tag, trophies)
            for tag in ["#2PP", "#8PY", "#9PY", "#LQ2", "#YQ2"]:
                link_player(database, account_id, tag)
            # Every battle so far is recorded, so its net so far is known.
            seed_day(
                database, "#2PP", 0, net=None,
                battles=[battle("offense", 3, 100, 40), battle("defense", 1, 60, -12)],
                gained=40, lost=12, attacks=1, defenses=1,
            )
            # Battles may be missing, so the net so far is unknown.
            seed_day(
                database, "#8PY", 0, net=None, state="Live",
                gained=70, lost=0, attacks=3, defenses=0,
            )
            with database.pool.connection() as connection:
                # Still showing last Season's trophies.
                connection.execute(
                    """
                    UPDATE player_profile_versions
                    SET current_league_season_id = '1'
                    WHERE normalized_tag = '#9PY'
                    """
                )
                # A newer Season 0 profile: in Legend I, no battle this Season.
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag,
                        endpoint_version, schema_version, parser_version,
                        observed_at, source_http_status, name, trophies,
                        league_tier_id, league_tier_name, eligibility_state,
                        eligibility_reason, current_league_season_id,
                        profile_json, source_contract_state
                    )
                    SELECT player_id, observation_id, normalized_tag,
                           endpoint_version, schema_version, 'season-0-test',
                           observed_at + interval '1 minute', source_http_status,
                           name, 5000, league_tier_id, league_tier_name,
                           eligibility_state, 'confirmed_legend_i', '0',
                           '{"clan": {"name": "Synthetic Clan"}}'::jsonb, 'conflict'
                    FROM player_profile_versions WHERE normalized_tag = '#LQ2'
                    """
                )
                # Clan A, then Clan B, then Clan A again: the unchanged first
                # profile is observed again rather than saved a second time.
                connection.execute(
                    """
                    UPDATE player_profile_versions
                    SET profile_json = '{"clan": {"name": "Clan A"}}'::jsonb
                    WHERE normalized_tag = '#2PP'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag,
                        endpoint_version, schema_version, parser_version,
                        observed_at, source_http_status, name, trophies,
                        league_tier_id, league_tier_name, eligibility_state,
                        current_league_season_id, profile_json
                    )
                    SELECT player_id, observation_id, normalized_tag,
                           endpoint_version, schema_version, 'clan-b-test',
                           observed_at + interval '1 minute', source_http_status,
                           name, trophies, league_tier_id, league_tier_name,
                           eligibility_state, current_league_season_id,
                           '{"clan": {"name": "Clan B"}}'::jsonb
                    FROM player_profile_versions WHERE normalized_tag = '#2PP'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO player_profile_effects (
                        profile_version_id, observation_id, effect_kind,
                        observed_at, source_http_status, endpoint_version,
                        schema_version, parser_version
                    )
                    SELECT id, observation_id, 'current_profile',
                           observed_at + interval '2 minutes', source_http_status,
                           endpoint_version, schema_version, parser_version
                    FROM player_profile_versions
                    WHERE normalized_tag = '#2PP' AND profile_json -> 'clan' ->> 'name' = 'Clan A'
                    """
                )
                # Dropped out of Legend I, keeping its last accepted profile;
                # its newest saved profile names its league now.
                connection.execute(
                    """
                    UPDATE players SET active = false, eligibility_state = 'ineligible'
                    WHERE normalized_tag = '#YQ2'
                    """
                )
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag,
                        endpoint_version, schema_version, parser_version,
                        observed_at, source_http_status, name, trophies,
                        league_tier_id, league_tier_name, eligibility_state,
                        eligibility_reason, current_league_season_id,
                        profile_json, source_contract_state
                    )
                    SELECT player_id, observation_id, normalized_tag,
                           endpoint_version, schema_version, 'electro-test',
                           observed_at + interval '1 minute', source_http_status,
                           name, 0, 105000033, 'Electro League 33', 'ineligible',
                           'confirmed_non_legend_i', '0', '{}'::jsonb, 'conflict'
                    FROM player_profile_versions WHERE normalized_tag = '#YQ2'
                    """
                )
                # The weekly league record has decided it.
                connection.execute("DELETE FROM league_week_dirty")

            cards = api_accounts.get_public_user(database, "groupowner", now=NOW)[
                "verified_players"
            ]

            board = {
                entry["tag"]: entry["position"]
                for entry in api_leaderboard.get_live_leaderboard(
                    database, limit=50, now=NOW
                )["entries"]
            }
            assert cards == [
                {
                    "tag": "#2PP", "name": "Player #2PP", "clan": "Clan A",
                    "state": "tracking", "reason": None, "trophies": 5300,
                    "season_reset_pending": False, "rank": board["#2PP"], "league": "Legend I",
                    "today": {"net": 28, "attacks": 1, "defenses": 1},
                },
                {
                    "tag": "#8PY", "name": "Player #8PY", "clan": None,
                    "state": "tracking", "reason": None, "trophies": 5400,
                    "season_reset_pending": False, "rank": board["#8PY"], "league": "Legend I",
                    "today": {"net": None, "attacks": 3, "defenses": 0},
                },
                {
                    "tag": "#9PY", "name": "Player #9PY", "clan": None,
                    "state": "tracking", "reason": None, "trophies": None,
                    # With no Live Leaderboard position, its league still shows.
                    "season_reset_pending": True, "rank": None, "league": "Legend I",
                    # Today's battles still count while the trophies wait.
                    "today": {"net": None, "attacks": None, "defenses": None},
                },
                {
                    "tag": "#LQ2", "name": "Player #LQ2", "clan": "Synthetic Clan",
                    "state": "tracking", "reason": "no_legend_battles",
                    "trophies": None, "season_reset_pending": False,
                    # Its accepted profile still places it on the board.
                    "rank": board["#LQ2"], "league": "Legend I",
                    "today": None,
                },
                {
                    # Off the board, its newest saved profile's trophies, as
                    # its page shows, not its last Legend I count.
                    "tag": "#YQ2", "name": "Player #YQ2", "clan": None,
                    "state": "not_in_legend", "reason": None, "trophies": 0,
                    "season_reset_pending": False, "rank": None,
                    "league": "Electro League 33", "today": None,
                    # It has no Legend I day this Season to have left on.
                    "left_legend_i": None,
                },
            ]
            # Ranks are positions on the whole Live Leaderboard, not the list.
            assert (board["#LQ2"], board["#8PY"], board["#2PP"]) == (2, 3, 4)
            # The data service reads ranks from the Live Leaderboard's shared
            # read, with the same answer.
            live = api_leaderboard.LiveBoardCache(clock=lambda: 0.0)
            assert api_accounts.get_public_user(
                database, "groupowner", now=NOW, live_board=live
            )["verified_players"] == cards
            # A player who leaves Legend I loses its rank at once, while the
            # shared read still has it on the board; the others keep theirs.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE players SET active = false, eligibility_state = 'ineligible'
                    WHERE normalized_tag = '#8PY'
                    """
                )
            left = api_accounts.get_public_user(
                database, "groupowner", now=NOW, live_board=live
            )["verified_players"]
            assert [
                (card["tag"], card["state"], card["rank"])
                for card in left
                if card["tag"] in ("#2PP", "#8PY")
            ] == [("#2PP", "tracking", board["#2PP"]), ("#8PY", "not_in_legend", None)]
            # A newer uncertain profile names no league, rather than the
            # older Electro League 33 staying shown.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag,
                        endpoint_version, schema_version, parser_version,
                        observed_at, source_http_status, name, trophies,
                        league_tier_id, league_tier_name, eligibility_state,
                        current_league_season_id, profile_json,
                        source_contract_state
                    )
                    SELECT player_id, observation_id, normalized_tag,
                           endpoint_version, schema_version, 'unranked-test',
                           observed_at + interval '1 minute', source_http_status,
                           name, 0, 105000000, 'Unranked', 'uncertain', '0',
                           '{}'::jsonb, 'conflict'
                    FROM player_profile_versions
                    WHERE parser_version = 'electro-test'
                    """
                )
                connection.execute("DELETE FROM league_week_dirty")
            cards = api_accounts.get_public_user(database, "groupowner", now=NOW)[
                "verified_players"
            ]
            assert [card["league"] for card in cards if card["tag"] == "#YQ2"] == [None]

            # With no linked player tracked in Legend I, no rank is looked up,
            # whether each left it or Clash Lens is unsure where it is.
            with database.pool.connection() as connection:
                connection.execute(
                    "DELETE FROM player_profile_versions"
                    " WHERE parser_version = 'unranked-test'"
                )
                connection.execute(
                    """
                    UPDATE players SET active = false, eligibility_state = 'uncertain'
                    WHERE normalized_tag = '#9PY'
                    """
                )
                connection.execute(
                    """
                    DELETE FROM verified_player_links
                    WHERE player_id IN (
                        SELECT id FROM players
                        WHERE normalized_tag NOT IN ('#8PY', '#9PY', '#YQ2')
                    )
                    """
                )
            asked: list[object] = []

            class Board(api_leaderboard.LiveBoardCache):
                def current(self, database, now):  # type: ignore[override]
                    asked.append(now)
                    return super().current(database, now)

            cards = api_accounts.get_public_user(
                database, "groupowner", now=NOW, live_board=Board()
            )["verified_players"]
            assert [(card["tag"], card["state"], card["rank"]) for card in cards] == [
                ("#8PY", "not_in_legend", None),
                ("#9PY", "uncertain", None),
                ("#YQ2", "not_in_legend", None),
            ]
            assert asked == []
        finally:
            database.close()


def test_profile_without_linked_players_does_not_read_the_live_board(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            create_owner(database)
            # A copy the shared board read now would stay current.
            cache = api_leaderboard.LiveBoardCache(clock=lambda: 0.0)
            assert api_accounts.get_public_user(
                database, "nobody", now=NOW, live_board=cache
            ) is None
            assert api_accounts.get_public_user(
                database, "groupowner", now=NOW, live_board=cache
            ) == {
                "username": "groupowner", "display_name": "Group Owner",
                "verified_players": [],
            }
            seed_profile(database, "#2PP", 5300)
            # Neither profile read the board, so the first board page reads it.
            board = api_leaderboard.get_live_leaderboard(
                database, limit=50, now=NOW, cache=cache
            )
            assert [entry["tag"] for entry in board["entries"]] == ["#2PP"]
        finally:
            database.close()


def test_profile_days_read_every_linked_player_at_once_up_to_the_cards_limit(
    database_url: str,
) -> None:
    """A public profile's cards add each player's place at the last Reset and
    this Season's days in a fixed number of reads, however many players are
    linked, for at most the 500 players the cards show, and never a previous
    Season's day."""
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_owner(database)
            seed_profile(database, "#2PP", 5300)
            link_player(database, account_id, "#2PP")
            # NOW is the Season's Day 25: today, yesterday, and 25 days ago
            # the previous Season's Day 28.
            assert ranked_day_for(NOW).day_number == 25
            seed_day(database, "#2PP", 0, net=None, state="Live",
                     gained=40, lost=12, attacks=1, defenses=1)
            seed_day(database, "#2PP", 1, net=-20, gained=280, lost=300,
                     attacks=8, defenses=8)
            seed_day(database, "#2PP", 25, net=99, gained=399, lost=300,
                     attacks=8, defenses=8)
            # 500 more, past what the cards show.
            more = ["#2" + "".join(code) for code in product("89PYLQGRJCUV", repeat=3)][:500]
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    SELECT unnest(%s::text[]), true, 'eligible'
                    """,
                    (more,),
                )
            for tag in more:
                link_player(database, account_id, tag)
            # Each of them has played every day of the Season so far.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_end, version, state,
                        coverage, confidence, net_trophy_change, attack_gain,
                        defense_loss, attack_count, defense_count
                    )
                    SELECT player.id, start, start + interval '1 day', 1, 'Complete',
                           'complete', 'exact', -1234, 1000, 2234, 8, 8
                    FROM players AS player
                    CROSS JOIN generate_series(
                        %s::timestamptz, %s::timestamptz, interval '1 day') AS start
                    WHERE player.normalized_tag = ANY(%s)
                    """,
                    (ranked_day_for(NOW).season_start, ranked_day_for(NOW).start, more),
                )

            counted = _CountedReads(database)
            days = api_accounts.get_public_user_days(counted, "groupowner", now=NOW)
            assert api_accounts.get_public_user_days(database, "nobody", now=NOW) is None
        finally:
            database.close()

    assert days is not None
    # The account, its players, which board tables exist, their days and
    # Reset places, and their weeks: the same five for any number of players.
    assert counted.reads == 5
    players = days["players"]
    assert [player["tag"] for player in players] == sorted(["#2PP", *more])[:500]
    assert days["season_start"] == "2026-07-13T05:00:00+00:00"
    shown = next(player for player in players if player["tag"] == "#2PP")
    # No Daily board is published here, so no place at the last Reset.
    assert shown["reset_position"] is None
    assert shown["season_weeks"] == []
    # Yesterday and today as [Season day, attacks, defenses, result,
    # unplayed, listed], oldest first; the previous Season's day is left out.
    assert shown["days"] == [[24, 8, 8, -20, False, True], [25, 1, 1, None, False, True]]
    # Every other one has all 25 days, and 500 players' Seasons so far stay
    # well under the website's 2 MiB limit for one answer.
    assert {len(player["days"]) for player in players if player["tag"] != "#2PP"} == {25}
    assert players[1]["days"][0] == [1, 8, 8, -1234, False, True]
    assert len(json.dumps(days).encode()) < 1024 * 1024


class _CountedReads:
    """``database`` with each statement its connections run counted."""

    def __init__(self, database: ApiDatabase) -> None:
        self.database = database
        self.pool = self
        self.reads = 0

    @contextmanager
    def connection(self) -> Iterator[Any]:
        counter = self

        with self.database.pool.connection() as connection:

            class Counted:
                def execute(self, *args: Any, **kwargs: Any) -> Any:
                    counter.reads += 1
                    return connection.execute(*args, **kwargs)

            yield Counted()
