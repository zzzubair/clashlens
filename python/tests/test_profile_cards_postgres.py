from __future__ import annotations

from test_api_db_organization import create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database
from test_group_comparison_postgres import battle, link_player, seed_day

from clashlens import api_accounts, api_leaderboard
from clashlens.api_db import ApiDatabase


def test_profile_shows_each_linked_players_trophies_rank_and_today(
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
                # Dropped out of Legend I, keeping its last accepted profile.
                connection.execute(
                    """
                    UPDATE players SET active = false, eligibility_state = 'ineligible'
                    WHERE normalized_tag = '#YQ2'
                    """
                )

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
                    "season_reset_pending": False, "rank": board["#2PP"],
                    "today": {"net": 28, "attacks": 1, "defenses": 1},
                },
                {
                    "tag": "#8PY", "name": "Player #8PY", "clan": None,
                    "state": "tracking", "reason": None, "trophies": 5400,
                    "season_reset_pending": False, "rank": board["#8PY"],
                    "today": {"net": None, "attacks": 3, "defenses": 0},
                },
                {
                    "tag": "#9PY", "name": "Player #9PY", "clan": None,
                    "state": "tracking", "reason": None, "trophies": None,
                    "season_reset_pending": True, "rank": None,
                    # Today's battles still count while the trophies wait.
                    "today": {"net": None, "attacks": None, "defenses": None},
                },
                {
                    "tag": "#LQ2", "name": "Player #LQ2", "clan": "Synthetic Clan",
                    "state": "tracking", "reason": "no_legend_battles",
                    "trophies": None, "season_reset_pending": False, "rank": None,
                    "today": None,
                },
                {
                    "tag": "#YQ2", "name": "Player #YQ2", "clan": None,
                    "state": "not_in_legend", "reason": None, "trophies": 4900,
                    "season_reset_pending": False, "rank": None,
                    "today": {"net": None, "attacks": None, "defenses": None},
                },
            ]
            # Ranks are positions on the whole Live Leaderboard, not the list.
            assert (board["#8PY"], board["#2PP"]) == (3, 4)
        finally:
            database.close()
