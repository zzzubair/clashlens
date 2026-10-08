from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.responses import JSONResponse
from psycopg.errors import QueryCanceled
from psycopg.types.json import Jsonb
from test_api_migration import migrated_production_database

from clashlens import (
    api,
    api_accounts,
    api_analytics,
    api_db,
    api_leaderboard,
    api_players,
)
from clashlens.api_db import (
    ApiDatabase,
    RequestBinding,
    _public_army,
    _screen_daily_log_with_events,
    _screen_events,
)
from clashlens.army_decoder import DECODER_VERSION
from clashlens.catalog import CATALOG_VERSION
from clashlens.domain import ranked_day_for

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def anonymous_binding(operation: str, target: str, tag: str) -> RequestBinding:
    return RequestBinding(
        request_id=str(uuid4()),
        caller="typescript-website",
        provider="",
        provider_subject="",
        account_id=None,
        operation=operation,
        method="POST",
        request_target=target,
        identity={"tag": tag},
    )


def seed_profile(
    database: ApiDatabase, tag: str, trophies: int, *, observed_at: datetime = NOW
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            """
            INSERT INTO players (normalized_tag, active, eligibility_state)
            VALUES (%s, true, 'eligible')
            RETURNING id
            """,
            (tag,),
        ).fetchone()[0]
        work_id = connection.execute(
            """
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                league_history_status
            ) VALUES (
                'initial_collection', 'interactive', 'player', %s, %s, %s,
                %s, 'pending', 'pending', 'pending', 'pending'
            ) RETURNING id
            """,
            (player_id, tag, observed_at, f"seed:{tag}"),
        ).fetchone()[0]
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, scope, player_id, normalized_tag, endpoint,
                request_started_at, response_completed_at, http_status,
                response_hash, collector_version, key_label, evidence_headers,
                request_method, request_path, request_query,
                paging_envelope_state, source_adapter_version
            ) VALUES (
                %s, 'player', %s, %s, 'profile', %s, %s, 200,
                %s, 'test', 'normal-test', '{}'::jsonb,
                'GET', %s, '', 'not_applicable', 'supercell-profile-parser-v3'
            ) RETURNING id
            """,
            (
                f"seed:{tag}:profile",
                player_id,
                tag,
                observed_at,
                observed_at,
                "a" * 64,
                f"/v1/players/%23{tag.removeprefix('#')}",
            ),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE collector_work
            SET status = 'complete', profile_status = 'observed',
                battle_log_status = 'observed', league_history_status = 'observed',
                profile_observation_id = %s,
                completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (observation_id, observed_at, observed_at, work_id),
        )
        profile_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, current_league_season_id, profile_json
            ) VALUES (
                %s, %s, %s, 'profile-v1', 'profile-schema-v1',
                'profile-parser-v1', %s, 200, %s, %s, 105000036,
                'Legend I', 'eligible', %s, '{}'::jsonb
            ) RETURNING id
            """,
            (
                player_id,
                observation_id,
                tag,
                observed_at,
                f"Player {tag}",
                trophies,
                ranked_day_for(observed_at).official_season_id,
            ),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE players
            SET current_profile_version_id = %s, current_observed_at = %s
            WHERE id = %s
            """,
            (profile_id, observed_at, player_id),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                adjustments, battles, partial_reasons
            ) VALUES (
                %s, '2026-08-06T05:00:00Z', 1, 'Live', 'partial',
                '[]'::jsonb, '[]'::jsonb, '["active_day"]'::jsonb
            )
            """,
            (player_id,),
        )
        connection.commit()


def seed_league_history(
    database: ApiDatabase,
    tag: str,
    season_id: str,
    *,
    observed_at: datetime = NOW,
    tier_id: int = 105000036,
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
        ).fetchone()[0]
        response_hash = uuid4().hex + uuid4().hex
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, scope, player_id, normalized_tag, endpoint,
                request_started_at, response_completed_at, http_status,
                response_hash, collector_version, key_label, evidence_headers,
                request_method, request_path, request_query,
                paging_envelope_state, source_adapter_version
            ) VALUES (
                %s, 'player', %s, %s, 'league_history', %s, %s, 200,
                %s, 'test', 'normal-test', '{}'::jsonb,
                'GET', %s, '', 'not_applicable', 'league-history-v1'
            ) RETURNING id
            """,
            (
                f"seed:{tag}:league-history:{season_id}",
                player_id,
                tag,
                observed_at,
                observed_at,
                response_hash,
                f"/v1/players/%23{tag.removeprefix('#')}/leaguehistory",
            ),
        ).fetchone()[0]
        payload_id = connection.execute(
            """
            INSERT INTO parsed_source_payloads (
                endpoint, response_hash, parser_version, schema_version,
                parse_outcome, parsed_json
            ) VALUES (
                'league_history', %s, 'supercell-league-history-parser-v1',
                'league-history-schema-v1', 'valid', '{}'::jsonb
            ) RETURNING id
            """,
            (response_hash,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO player_league_history_entries (
                player_id, league_season_id, observed_at, observation_id,
                parsed_payload_id, league_trophies, league_tier_id, placement,
                source_json
            ) VALUES (%s, %s, %s, %s, %s, 5812, %s, 12, '{}'::jsonb)
            """,
            (
                player_id,
                season_id,
                observed_at,
                observation_id,
                payload_id,
                tier_id,
            ),
        )
        connection.commit()


def test_public_saved_operations_are_bounded_and_screen_ready(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            seed_profile(database, "#8PY", 6100)

            player = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )
            live = api_leaderboard.get_live_leaderboard(
                database, limit=100, now=NOW
            )
            analytics = api_analytics.get_basic_analytics(
                database, now=NOW, freshness_seconds=900
            )

            assert player is not None
            assert player["tag"] == "#2PP"
            assert player["freshness"] == "fresh"
            assert player["screen_ready"]["current_day_start"] is None
            assert player["screen_ready"]["season"] is None
            assert player["screen_ready"]["season_day_starts"] == []
            assert player["screen_ready"]["days"][0]["offense_events"] == []
            assert player["screen_ready"]["days"][0]["defense_events"] == []
            assert player["screen_ready"]["data_quality"][0]["code"] == "unavailable"
            assert {
                key: player["screen_ready"]["days"][0][key]
                for key in ("ranked_day_start", "state", "uncertainty_reasons")
            } == {
                "ranked_day_start": "2026-08-06T05:00:00+00:00",
                "state": "Live",
                "uncertainty_reasons": ["active_day"],
            }
            assert [entry["tag"] for entry in live["entries"]] == ["#8PY", "#2PP"]
            assert live["kind"] == "live"
            assert live["ordering_rule_version"] == "tracked-trophies-attack-destruction-v2"
            assert live["generated_at"] == NOW.isoformat()
            assert live["source_observations"] == {
                "oldest_observed_at": "2026-08-06T12:00:00+00:00",
                "newest_observed_at": "2026-08-06T12:00:00+00:00",
                "stale_count": 0,
            }
            assert live["provenance"]["observed_at"] == NOW.isoformat()
            assert analytics["population"] == "tracked_players"
            assert analytics["sample_size"] == 2
            assert analytics["classification_state"] == "unclassified"
            assert analytics["freshness"] == {"fresh": 2, "stale": 0}
        finally:
            database.close()


def test_player_page_reports_a_player_the_game_no_longer_finds(
    database_url: str,
) -> None:
    # #2VUQ8JLC2 on 7 October 2026: saved at 4,930, then "player not found".
    from test_freshness_metrics_postgres import seed_check  # imports this module

    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2VUQ8JLC2", 4930)
            seed_profile(database, "#8PY", 6100)
            missing_at = NOW + timedelta(minutes=5)
            with database.pool.connection() as connection:
                seed_check(connection, "#2VUQ8JLC2", "profile", NOW, not_found=missing_at)
            missing = api_players.get_player_page(
                database, "#2VUQ8JLC2", now=NOW, freshness_seconds=900
            )
            live = api_leaderboard.get_live_leaderboard(database, limit=100, now=NOW)
            # A later successful check makes the profile current again.
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE collector_response_state SET last_success_at = %s",
                    (missing_at + timedelta(minutes=5),),
                )
            found = api_players.get_player_page(
                database, "#2VUQ8JLC2", now=NOW, freshness_seconds=900
            )
        finally:
            database.close()

    assert missing is not None and found is not None
    assert [entry["tag"] for entry in live["entries"]] == ["#8PY"]
    assert missing["profile_not_found_at"] == missing_at.isoformat()
    assert missing["trophies"] == 4930
    assert missing["public_confidence"] == "uncertain"
    assert missing["screen_ready"]["provenance"]["confidence"] == "uncertain"
    assert "Player not found" in [
        item["label"] for item in missing["screen_ready"]["data_quality"]
    ]
    assert missing["screen_ready"]["days"] == found["screen_ready"]["days"]
    assert found["profile_not_found_at"] is None
    assert found["public_confidence"] == "high"
    assert "Player not found" not in [
        item["label"] for item in found["screen_ready"]["data_quality"]
    ]


def test_known_player_name_search_uses_current_profiles_and_escapes_wildcards(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#8PY", 6100)
            seed_profile(database, "#2PP", 6000)
            seed_profile(database, "#9PY", 5900)
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE player_profile_versions SET name = 'player'
                    WHERE normalized_tag = '#9PY'
                    """
                )

            # An exact name match comes first, then the most trophies.
            results = api_players.search_known_players(
                database, "Player", now=NOW, freshness_seconds=900
            )
            assert [result["tag"] for result in results] == ["#9PY", "#8PY", "#2PP"]
            assert results[1:] == [
                {
                    "tag": "#8PY",
                    "name": "Player #8PY",
                    "clan": None,
                    "trophies": 6100,
                    "season_reset_pending": False,
                    "freshness": "fresh",
                    "age_seconds": 0,
                    "observed_at": "2026-08-06T12:00:00+00:00",
                    "public_confidence": "high",
                },
                {
                    "tag": "#2PP",
                    "name": "Player #2PP",
                    "clan": None,
                    "trophies": 6000,
                    "season_reset_pending": False,
                    "freshness": "fresh",
                    "age_seconds": 0,
                    "observed_at": "2026-08-06T12:00:00+00:00",
                    "public_confidence": "high",
                },
            ]
            assert (
                api_players.search_known_players(
                    database, "%", now=NOW, freshness_seconds=900
                )
                == []
            )
        finally:
            database.close()


def test_player_screen_ready_current_day_preserves_partial_inferred_evidence(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET ranked_day_end = %s,
                        official_season_id = '1783918800',
                        season_day_number = 25,
                        state = 'Partial',
                        coverage = 'complete',
                        confidence = 'inferred',
                        attack_count = 1,
                        attack_three_star_count = 1,
                        attack_gain = 40,
                        defense_count = 0,
                        defense_three_star_count = 0,
                        defense_loss = 0,
                        net_trophy_change = 40,
                        partial_reasons = '["active_day"]'::jsonb
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = '#2PP'
                    )
                    """,
                    (datetime(2026, 8, 6, 13, 0, tzinfo=UTC),),
                )
                connection.execute(
                    """
                    UPDATE player_profile_versions
                    SET current_league_season_id = '1783918800',
                        previous_league_season_id = '1783314000',
                        profile_json = '{"currentLeagueSeasonId": 1783918800,
                                         "previousLeagueSeasonId": 1783314000}'::jsonb
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = '#2PP'
                    )
                    """
                )
                connection.commit()
            seed_league_history(database, "#2PP", "1781499600")

            player = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )

            assert player is not None
            [current_day] = player["screen_ready"]["days"]
            assert player["screen_ready"]["current_day_start"] == (
                current_day["ranked_day_start"]
            )
            assert current_day["season_day_number"] == 25
            assert current_day["public_confidence"] == "partial"
            assert current_day["completeness"] == {
                "state": "partial",
                "reason": "active_day",
            }
            assert current_day["uncertainty_reasons"] == ["active_day"]
            assert current_day["offense_events"] == []
            assert current_day["defense_events"] == []
            assert player["screen_ready"]["season_day_starts"] == [
                current_day["ranked_day_start"]
            ]
            assert player["screen_ready"]["season"] == {
                "id": "1783918800",
                "current_day_number": 25,
                "start": "2026-07-13T05:00:00+00:00",
                "end": "2026-08-10T05:00:00+00:00",
                "anchor_source": "official_league_history",
                "anchor_observed_at": NOW.isoformat(),
            }
            assert player["screen_ready"]["data_quality"] == [
                {
                    "code": "partial",
                    "label": "Incomplete ranked-day data",
                    "detail": "active_day",
                }
            ]
        finally:
            database.close()


def test_player_page_withholds_season_days_when_official_history_disagrees(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            seed_league_history(database, "#2PP", "1781499600")
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET ranked_day_end = '2026-08-07T05:00:00Z',
                        official_season_id = '1783314000',
                        season_day_number = 4,
                        state = 'Complete', coverage = 'complete',
                        confidence = 'exact', partial_reasons = '[]'::jsonb
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = '#2PP'
                    )
                    """
                )
                connection.commit()

            player = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )
        finally:
            database.close()

    assert player is not None
    assert player["screen_ready"]["season"] is None
    assert player["screen_ready"]["season_day_starts"] == []
    assert player["screen_ready"]["data_quality"] == [
        {
            "code": "uncertain",
            "label": "Season boundary conflict",
            "detail": "The official league history and ranked-day publication disagree, so season days are withheld.",
        }
    ]


def test_public_army_shows_an_unknown_hero_once_as_unknown() -> None:
    army = _public_army(
        (
            1,
            "attacker",
            "partial",
            None,
            [],
            [],
            [],
            [],
            [{"hero": "hero:999", "pet": "pet:9", "equipment": []}],
            [{"numeric_id": 999, "quantity": 1, "section": "h", "origin": "hero"}],
            "army-decoder-v2",
            "unit-catalog-v1",
        )
    )

    assert [component["typed_id"] for component in army["components"]] == ["pet:9"]
    assert army["unknown_components"] == [
        {"numeric_id": 999, "quantity": 1, "section": "h", "origin": "hero"}
    ]


def test_screen_events_are_ordered_signed_normalized_and_malformed_safe() -> None:
    offense, defense = _screen_events(
        [
            {
                "lens": "offense",
                "battle_id": "100",
                "battle_timestamp": "2026-08-05T18:22:31Z",
                "opponent": {"tag": "#8py", "name": "Earlier"},
                "destruction_percentage": 50,
                "stars": 1,
                "trophy_change": 20,
            },
            {
                "lens": "offense",
                "battle_id": "101",
                "battle_timestamp": "2026-08-05T18:22:31Z",
                "opponent": {"tag": "#9Q2", "name": "Later ID"},
                "destruction_percentage": 100,
                "stars": 3,
                "trophy_change": 40,
            },
            {
                "lens": "offense",
                "battle_id": "99",
                "battle_timestamp": "2026-08-05T19:00:00Z",
                "opponent": {"tag": "#2PP", "name": None},
                "destruction_percentage": 0,
                "stars": 0,
                "trophy_change": 0,
            },
            {
                "lens": "offense",
                "battle_id": "102",
                "disagreement": True,
                "battle_timestamp": "2026-08-05T19:30:00Z",
                "opponent": {"tag": "#L92", "name": "Disputed"},
                "destruction_percentage": 60,
                "stars": 2,
                "trophy_change": 10,
            },
            {
                "lens": "defense",
                "battle_id": 200,
                "battle_timestamp": "2026-08-05T20:00:00Z",
                "opponent": {"tag": "#LQ2", "name": "Defender"},
                "destruction_percentage": 80,
                "stars": 2,
                "trophy_change": -30,
            },
            {
                "lens": "defense",
                "battle_id": 200,
                "battle_timestamp": "2026-08-05T20:00:00Z",
                "opponent": {"tag": "#LQ2", "name": "Duplicate"},
                "destruction_percentage": 80,
                "stars": 2,
                "trophy_change": -30,
            },
            {
                "lens": "offense",
                "battle_id": "excluded",
                "included": False,
                "battle_timestamp": "2026-08-05T21:00:00Z",
            },
            {"lens": "offense", "battle_id": "malformed"},
            "not an event",
        ]
    )

    assert [event["battle_id"] for event in offense] == ["102", "99", "101", "100"]
    # A disagreement battle stays visible on its row instead of being dropped.
    assert offense[0]["perspective_disagreement"] is True
    assert offense[1]["opponent"] == {"tag": "#2PP", "name": None}
    assert offense[1]["perspective_disagreement"] is False
    assert offense[2]["trophy_change"] == 40
    assert [event["battle_id"] for event in defense] == ["200"]
    assert defense[0]["trophy_change"] == -30
    assert defense[0]["perspective_disagreement"] is False
    assert _screen_events(None) == ([], [])


def _stored_day(
    start: datetime, attacks: list[int], defenses: list[int], reasons: list[str]
) -> dict[str, object]:
    def battle(lens: str, slot: int, change: int) -> dict[str, object]:
        return {
            "lens": lens,
            "battle_id": f"{lens}-{slot}",
            "battle_timestamp": (start + timedelta(hours=slot + 1)).isoformat(),
            "opponent": {"tag": "#2PY", "name": "Opponent"},
            "destruction_percentage": 100,
            "stars": 3,
            "trophy_change": change,
        }

    return {
        "ranked_day_start": start.isoformat(),
        "ranked_day_end": (start + timedelta(days=1)).isoformat(),
        "state": "Partial",
        "coverage": "partial",
        "confidence": "partial",
        "attack_count": len(attacks),
        "attack_gain": sum(attacks),
        "defense_count": len(defenses),
        "defense_loss": -sum(defenses),
        "net_trophy_change": None,
        "partial_reasons": reasons,
        "battles": [
            *(battle("offense", slot, change) for slot, change in enumerate(attacks)),
            *(battle("defense", slot, change) for slot, change in enumerate(defenses)),
        ],
    }


def test_recorded_battles_give_the_day_in_progress_a_net_so_far() -> None:
    now = datetime(2026, 10, 3, 19, 0, tzinfo=UTC)
    # Prodigi's Day 27, in progress: the net so far is left to the page.
    day_27 = _stored_day(
        datetime(2026, 10, 3, 5, 0, tzinfo=UTC),
        [40] * 7 + [15],
        [-40, -40, -40, -19],
        [
            "missing_end_battle_log_baseline",
            "automatic_defense_basis_unavailable",
            "player_not_eligible",
        ],
    )
    screen = _screen_daily_log_with_events(day_27, "high", now)
    assert screen["battles_complete"] is True
    assert screen["attack_gain"] - screen["defense_loss"] == 295 - 139 == 156
    assert screen["net_trophy_change"] is None

    unknown = [
        # A gap today means battles so far may be missing, even with eight.
        {**day_27, "partial_reasons": ["missing_start_battle_log_baseline"]},
        # The two players' battle logs disagree about a result.
        {**day_27, "partial_reasons": ["perspective_disagreement"]},
        # The totals do not match the listed battles.
        {**day_27, "defense_loss": 140},
        # A finished day without all 8 defenses may have lost later battles.
        {**day_27, "ranked_day_end": "2026-10-03T06:00:00+00:00"},
    ]
    screens = [_screen_daily_log_with_events(day, "high", now) for day in unknown]
    assert [screen["battles_complete"] for screen in screens] == [False] * 4

    # Prodigi's Day 24, finished: all 8 of each, so nothing is missing. Its
    # net comes only from its saved result.
    day_24 = _stored_day(
        datetime(2026, 9, 30, 5, 0, tzinfo=UTC),
        [40] * 7 + [20],
        [-40] * 7 + [-31],
        ["missing_start_battle_log_baseline", "missing_start_baseline"],
    )
    screen = _screen_daily_log_with_events(day_24, "high", now)
    assert screen["battles_complete"] is True
    assert screen["attack_gain"] - screen["defense_loss"] == 300 - 311 == -11
    assert screen["net_trophy_change"] is None
    mismatch = {**day_24, "partial_reasons": ["trophy_equation_mismatch"]}
    assert not _screen_daily_log_with_events(mismatch, "high", now)["battles_complete"]

    # All 8 attacks and 8 defenses today: none can be missing, whatever
    # checks were missed, unless a battle is disputed.
    all_today = _stored_day(
        datetime(2026, 10, 3, 5, 0, tzinfo=UTC),
        [40] * 7 + [20],
        [-40] * 7 + [-31],
        ["missing_start_battle_log_baseline", "missing_start_baseline"],
    )
    screen = _screen_daily_log_with_events(all_today, "high", now)
    assert screen["battles_complete"] is True
    assert screen["attack_gain"] - screen["defense_loss"] == -11
    disputed = {
        **all_today,
        "partial_reasons": [
            "missing_start_battle_log_baseline",
            "duplicate_contribution_disagreement",
        ],
    }
    assert not _screen_daily_log_with_events(disputed, "high", now)["battles_complete"]


def test_player_screen_ready_limits_season_days_to_current_official_season(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            current_battles = [
                {
                    "lens": "offense",
                    "battle_id": "2",
                    "battle_timestamp": "2026-08-06T11:00:00Z",
                    "opponent": {"tag": "#8PY", "name": "Latest attack"},
                    "destruction_percentage": 100,
                    "stars": 3,
                    "trophy_change": 40,
                },
                {
                    "lens": "offense",
                    "battle_id": "1",
                    "battle_timestamp": "2026-08-06T10:00:00Z",
                    "opponent": {"tag": "#9Q2", "name": "Earlier attack"},
                    "destruction_percentage": 80,
                    "stars": 2,
                    "trophy_change": 30,
                },
                {
                    "lens": "defense",
                    "battle_id": "3",
                    "battle_timestamp": "2026-08-06T09:00:00Z",
                    "opponent": {"tag": "#2PL", "name": None},
                    "destruction_percentage": 50,
                    "stars": 1,
                    "trophy_change": -20,
                },
            ]
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET ranked_day_end = '2026-08-07T05:00:00Z',
                        official_season_id = 'current-season', season_day_number = 3,
                        state = 'Complete', coverage = 'complete', confidence = 'exact',
                        attack_count = 2, attack_three_star_count = 1,
                        attack_gain = 70, defense_count = 1,
                        defense_three_star_count = 0, defense_loss = 20,
                        net_trophy_change = 50, battles = %s,
                        partial_reasons = '[]'::jsonb,
                        published_at = '2026-08-06T11:30:00Z'
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = '#2PP'
                    ) AND ranked_day_start = '2026-08-06T05:00:00Z'
                    """,
                    (Jsonb(current_battles),),
                )
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number, version, state, coverage,
                        confidence, attack_count, attack_three_star_count, attack_gain,
                        defense_count, defense_three_star_count, defense_loss,
                        net_trophy_change, adjustments, battles, partial_reasons
                    ) VALUES (
                        (SELECT id FROM players WHERE normalized_tag = '#2PP'),
                        '2026-08-05T05:00:00Z', '2026-08-06T05:00:00Z',
                        'current-season', 2, 1, 'Complete', 'complete', 'exact',
                        0, 0, 0, 0, 0, 0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
                    ), (
                        (SELECT id FROM players WHERE normalized_tag = '#2PP'),
                        '2026-08-04T05:00:00Z', '2026-08-05T05:00:00Z',
                        'previous-season', 28, 1, 'Complete', 'complete', 'exact',
                        8, 8, 320, 0, 0, 0, 320, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
                    )
                    """
                )
                connection.commit()

            player = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )

            assert player is not None
            screen = player["screen_ready"]
            by_start = {day["ranked_day_start"]: day for day in screen["days"]}
            season_days = [by_start[start] for start in screen["season_day_starts"]]
            current_day = by_start[screen["current_day_start"]]
            assert [day["season_day_number"] for day in season_days] == [3, 2]
            assert screen["season"] == {
                "id": "current-season",
                "current_day_number": 3,
                "start": "2026-08-04T05:00:00+00:00",
                "end": "2026-09-01T05:00:00+00:00",
                "anchor_source": "daily_publication",
                "anchor_observed_at": "2026-08-06T11:30:00+00:00",
            }
            assert all(
                day["official_season_id"] == "current-season"
                for day in season_days
            )
            assert season_days[0] is current_day
            assert current_day["offense_events"] == [
                {
                    "battle_id": "2",
                    "battle_timestamp": "2026-08-06T11:00:00Z",
                    "opponent": {"tag": "#8PY", "name": "Latest attack"},
                    "destruction_percentage": 100,
                    "stars": 3,
                    "trophy_change": 40,
                    "perspective_disagreement": False,
                },
                {
                    "battle_id": "1",
                    "battle_timestamp": "2026-08-06T10:00:00Z",
                    "opponent": {"tag": "#9Q2", "name": "Earlier attack"},
                    "destruction_percentage": 80,
                    "stars": 2,
                    "trophy_change": 30,
                    "perspective_disagreement": False,
                },
            ]
            assert current_day["defense_events"][0]["trophy_change"] == -20
            assert current_day["attack_count"] == len(current_day["offense_events"])
            assert current_day["defense_count"] == len(current_day["defense_events"])
            assert season_days[1]["offense_events"] == []
            assert season_days[1]["defense_events"] == []
        finally:
            database.close()


def test_player_page_shows_each_day_rank_on_its_reset_board(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            seed_profile(database, "#9Q2", 6100)
            with database.pool.connection() as connection:
                player, other = (
                    connection.execute(
                        "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
                    ).fetchone()[0]
                    for tag in ("#2PP", "#9Q2")
                )
                observation = connection.execute(
                    "SELECT min(id) FROM collector_observations"
                ).fetchone()[0]
                for day in (3, 4, 5):
                    connection.execute(
                        """
                        INSERT INTO api_player_daily_logs (
                            player_id, ranked_day_start, ranked_day_end, version,
                            state, coverage, adjustments, battles, partial_reasons
                        ) VALUES (%s, %s, %s, 1, 'Complete', 'complete',
                                  '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                        """,
                        (
                            player,
                            datetime(2026, 8, day, 5, tzinfo=UTC),
                            datetime(2026, 8, day + 1, 5, tzinfo=UTC),
                        ),
                    )

                def board(reset_day, positions, *, version=1, state="published"):
                    snapshot = connection.execute(
                        """
                        INSERT INTO leaderboard_snapshots (
                            snapshot_kind, boundary_at, version,
                            ordering_rule_version, freshness_rule_version, state,
                            measured_coverage, stale_entry_count
                        ) VALUES ('frozen', %s, %s, 'test', 'test', %s, 1.0, 0)
                        RETURNING id
                        """,
                        (datetime(2026, 8, reset_day, 5, tzinfo=UTC), version, state),
                    ).fetchone()[0]
                    for player_id, position in positions.items():
                        connection.execute(
                            """
                            INSERT INTO leaderboard_snapshot_entries (
                                snapshot_id, position, player_id, trophies,
                                trophy_observation_id, trophy_observed_at,
                                observation_age_seconds, freshness, confidence,
                                tie_hash
                            ) VALUES (%s, %s, %s, 6000, %s, %s, 0, 'fresh',
                                      'confirmed', repeat('c', 64))
                            """,
                            (snapshot, position, player_id, observation, NOW),
                        )

                board(4, {other: 1, player: 3})
                board(5, {other: 1})
                board(6, {player: 2}, state="superseded")
                board(6, {other: 1, player: 5}, version=2)
                connection.commit()

            page = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )

            assert page is not None
            assert {
                day["ranked_day_start"][:10]: day["reset_rank"]
                for day in page["screen_ready"]["days"]
            } == {
                "2026-08-03": 3,
                "2026-08-04": None,
                "2026-08-05": 5,
                "2026-08-06": None,
            }
        finally:
            database.close()


def test_player_page_hides_saved_net_for_days_missing_battles(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET ranked_day_end = '2026-08-07T05:00:00Z',
                        official_season_id = 'current-season', season_day_number = 4
                    """
                )
                # Saved before the rule: one attack of +20 and a gap in the
                # battle log, published with a net of 20. The other days hold
                # complete coverage, or all 8 attacks and 8 defenses.
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_end,
                        official_season_id, season_day_number, version, state, coverage,
                        confidence, attack_count, attack_three_star_count, attack_gain,
                        defense_count, defense_three_star_count, defense_loss,
                        net_trophy_change, adjustments, battles, partial_reasons
                    )
                    SELECT player.id, day.start, day.start + interval '1 day',
                           'current-season', day.number, 1, day.state, day.coverage,
                           day.confidence, day.attacks, 0, day.gain, day.defenses,
                           0, day.loss, day.net, '[]'::jsonb, '[]'::jsonb, day.reasons
                    FROM players AS player, (VALUES
                        (timestamptz '2026-08-05T05:00:00Z', 3, 'Partial', 'partial',
                         'uncertain', 1, 20, 0, 0, 20,
                         '["battle_log_overlap_gap"]'::jsonb),
                        ('2026-08-04T05:00:00Z', 2, 'Partial', 'partial', 'partial',
                         8, 240, 8, 200, 40, '["battle_log_overlap_gap"]'::jsonb),
                        ('2026-08-03T05:00:00Z', 1, 'Complete', 'complete', 'exact',
                         2, 70, 1, 20, 50, '[]'::jsonb)
                    ) AS day(start, number, state, coverage, confidence, attacks,
                             gain, defenses, loss, net, reasons)
                    WHERE player.normalized_tag = '#2PP'
                    """
                )
                connection.commit()

            player = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )

            assert player is not None
            screen = player["screen_ready"]
            by_start = {day["ranked_day_start"]: day for day in screen["days"]}
            for starts in (screen["recent_day_starts"], screen["season_day_starts"]):
                days = [by_start[start] for start in starts]
                assert [
                    (day["season_day_number"], day["net_trophy_change"])
                    for day in days
                ] == [(4, None), (3, None), (2, 40), (1, 50)]
        finally:
            database.close()


def test_concurrent_refreshes_share_one_collector_work_and_public_refresh_identity(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info, max_size=12)
        try:

            def submit(_index: int):
                return api_accounts.submit_refresh(
                    database,
                    anonymous_binding(
                        "refresh.submit",
                        "/v1/players/%232PP/refresh",
                        "#2PP",
                    ),
                    normalized_tag="#2PP",
                    cooldown_seconds=30,
                )

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(submit, range(10)))

            refresh_ids = {result.payload["refresh_id"] for result in results}
            assert len(refresh_ids) == 1
            assert database.scalar("SELECT count(*) FROM collector_work") == 1
            assert database.scalar("SELECT count(*) FROM api_refresh_requests") == 1
            status = api_accounts.get_refresh_status(database, next(iter(refresh_ids)))
            assert status == {
                "refresh_id": next(iter(refresh_ids)),
                "tag": "#2PP",
                "status": "pending",
                "outcome": "created",
            }
        finally:
            database.close()


def test_live_pagination_has_absolute_ranks_and_population_freshness(
    database_url: str,
) -> None:
    alphabet = "0289PYLQGRJCUV"
    tags = [
        "#"
        + alphabet[(index // 196) % 14]
        + alphabet[(index // 14) % 14]
        + alphabet[index % 14]
        for index in range(101)
    ]
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            for tag in tags:
                seed_profile(
                    database, tag, 6000,
                    observed_at=NOW - timedelta(seconds=600.5) if tag == tags[0] else NOW,
                )
            first = api_leaderboard.get_live_leaderboard(
                database, limit=100, offset=0, now=NOW
            )
            second = api_leaderboard.get_live_leaderboard(
                database, limit=100, offset=100, now=NOW
            )
            assert first is not None and second is not None
            assert [entry["position"] for entry in first["entries"]] == list(
                range(1, 101)
            )
            assert [entry["position"] for entry in second["entries"]] == [101]
            assert (
                len({entry["tag"] for entry in first["entries"] + second["entries"]})
                == 101
            )
            assert first["total_entries"] == second["total_entries"] == 101
            assert first["page_count"] == second["page_count"] == 2
            assert first["has_next"] is True and second["has_previous"] is True
            assert first["provenance"]["freshness"] == "stale"
            assert first["source_observations"]["stale_count"] == 1
            assert (
                first["source_observations"]["oldest_observed_at"]
                == "2026-08-06T11:49:59.500000+00:00"
            )
            assert (
                first["source_observations"]["newest_observed_at"]
                == "2026-08-06T12:00:00+00:00"
            )
            assert (
                first["provenance"]["observed_at"]
                == first["source_observations"]["newest_observed_at"]
            )
            stale_entry = next(
                entry
                for entry in first["entries"] + second["entries"]
                if entry["tag"] == tags[0]
            )
            assert stale_entry["age_seconds"] == 600
            assert stale_entry["freshness"] == "stale"
            assert (
                api_leaderboard.get_live_leaderboard(
                    database, limit=100, offset=200, now=NOW
                )
                is None
            )
        finally:
            database.close()


def test_live_leaderboard_reports_empty_population(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            empty = api_leaderboard.get_live_leaderboard(
                database, limit=25, now=NOW
            )
            assert empty is not None
            assert empty["entries"] == []
            assert empty["tracked_population"] == 0
            assert empty["total_entries"] == 0
            assert empty["page_count"] == 0
            assert empty["source_observations"] == {
                "oldest_observed_at": None,
                "newest_observed_at": None,
                "stale_count": 0,
            }
            assert (
                api_leaderboard.get_live_leaderboard(
                    database, limit=25, offset=25, now=NOW,
                )
                is None
            )
        finally:
            database.close()


def test_api_cancels_a_runaway_query_and_keeps_serving(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_db, "API_STATEMENT_TIMEOUT", "100ms")
    database = ApiDatabase(database_url, max_size=1)
    try:
        with pytest.raises(QueryCanceled):
            database.scalar("SELECT pg_sleep(5)")
        # The pool's only connection still answers the next request.
        assert database.scalar("SELECT 1") == 1
    finally:
        database.close()


# A full army: 6 troops, 5 spells, a siege, Clan Castle troops and spell, and
# 5 heroes each with a pet and 2 equipment, as the busiest attacks bring.
HEAVY_ARMY = {
    "home_troops": [[f"troop:{i}", 8, "home"] for i in (0, 1, 3, 4, 5, 6)],
    "spells": [[f"spell:{i}", 2, "home"] for i in (0, 1, 2, 3, 10)],
    "siege": [["troop:51", 1, "home"]],
    "cc_troops": [["troop:10", 3, "clan_castle"], ["spell:11", 1, "clan_castle"]],
    "heroes": [
        {"hero": f"hero:{hero}", "pet": f"pet:{pet}", "equipment": [
            f"equipment:{equipment}", f"equipment:{equipment + 1}"]}
        for hero, pet, equipment in ((0, 0, 0), (1, 1, 10), (2, 2, 12), (4, 3, 14), (6, 4, 16))
    ],
}


# A full Season is the worst case.
@pytest.mark.parametrize("days", [5, 28])
def test_busiest_player_page_stays_well_under_the_response_limit(
    database_url: str, days: int
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            today = ranked_day_for(NOW).start
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM api_player_daily_logs WHERE player_id = %s",
                    (player_id,),
                )
                # Armies need saved battles; skip those links to seed only what
                # the player page reads.
                connection.execute("SET LOCAL session_replication_role = replica")
                for day_number in range(1, days + 1):
                    start = today - timedelta(days=days - day_number)
                    battles = [
                        {
                            "lens": lens,
                            "battle_id": str(day_number * 100 + slot),
                            "battle_timestamp": (start + timedelta(minutes=slot)).isoformat(),
                            "opponent": {"tag": f"#2PP{'0289PYLQGR'[slot % 10]}", "name": "Opponent name"},
                            "destruction_percentage": 100,
                            "stars": 3,
                            "trophy_change": 40 if lens == "offense" else -40,
                            "army_share_code": "u" + "1x2" * 20,
                        }
                        for slot in range(16)
                        for lens in ["offense" if slot < 8 else "defense"]
                    ]
                    connection.execute(
                        """
                        INSERT INTO api_player_daily_logs (
                            player_id, ranked_day_start, ranked_day_end,
                            official_season_id, season_day_number, version, state,
                            coverage, confidence, attack_count, attack_three_star_count,
                            attack_gain, defense_count, defense_three_star_count,
                            defense_loss, net_trophy_change, adjustments, battles,
                            partial_reasons, published_at
                        ) VALUES (
                            %s, %s, %s, 'current-season', %s, 1, 'Complete', 'complete',
                            'exact', 8, 8, 320, 8, 8, 320, 0, '[]'::jsonb, %s,
                            '[]'::jsonb, %s
                        )
                        """,
                        (player_id, start, start + timedelta(days=1), day_number,
                         Jsonb(battles), NOW),
                    )
                    for battle in battles:
                        connection.execute(
                            """
                            INSERT INTO battle_army_decodes (
                                battle_id, evidence_id, perspective, decoder_version,
                                catalog_version, catalog_hash, status, exact_army_id,
                                identity_hash, home_troops, spells, siege, cc_troops,
                                heroes
                            ) VALUES (%s, 1, %s, %s, %s, %s, 'decoded', 1, %s,
                                      %s, %s, %s, %s, %s)
                            """,
                            (
                                int(battle["battle_id"]),
                                "attacker" if battle["lens"] == "offense" else "defender",
                                DECODER_VERSION, CATALOG_VERSION, "a" * 64, "b" * 64,
                                *(Jsonb(HEAVY_ARMY[key]) for key in (
                                    "home_troops", "spells", "siege", "cc_troops", "heroes")),
                            ),
                        )
                connection.commit()

            player = api_players.get_player_page(
                database, "#2PP", now=NOW, freshness_seconds=900
            )

            assert player is not None
            screen = player["screen_ready"]
            # Each day is sent once, even though every day is recent and in
            # the Season.
            assert len(screen["days"]) == days
            assert screen["recent_day_starts"] == screen["season_day_starts"] == [
                day["ranked_day_start"] for day in screen["days"]
            ]
            assert screen["current_day_start"] == screen["days"][0]["ranked_day_start"]
            # Every battle still reaches the page with its Copy army code, but
            # not the decoded army the page never shows.
            assert all(
                len(day["offense_events"]) == len(day["defense_events"]) == 8
                and all(event["army_share_code"] and "army" not in event for event in
                        [*day["offense_events"], *day["defense_events"]])
                for day in screen["days"]
            )
            size = len(JSONResponse(content=api._json_safe(player)).body)
            assert size < 0.6 * api._DEFAULT_MAX_RESPONSE_BYTES
        finally:
            database.close()
