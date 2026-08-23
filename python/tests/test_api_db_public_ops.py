from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb
from test_api_migration import migrated_production_database

from clashlens.api_db import ApiDatabase, RequestBinding, _public_army, _screen_events

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


def seed_profile(database: ApiDatabase, tag: str, trophies: int) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            """
            INSERT INTO players (normalized_tag, active, eligibility_state)
            VALUES (%s, true, 'eligible')
            RETURNING id
            """,
            (tag,),
        ).fetchone()[0]
        job_id = connection.execute(
            """
            INSERT INTO collector_jobs (
                work_type, player_id, normalized_tag, scope, capacity_pool, priority, due_at,
                coalescing_key, status, required_endpoint
            ) VALUES (
                'initial_collection', %s, %s, 'player', 'interactive', 300, %s,
                %s, 'complete', 'profile'
            ) RETURNING id
            """,
            (player_id, tag, NOW, f"seed:{tag}"),
        ).fetchone()[0]
        attempt_id = connection.execute(
            """
            INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
            VALUES (%s, 'complete', %s, %s)
            RETURNING id
            """,
            (job_id, NOW, NOW),
        ).fetchone()[0]
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, collection_job_id, attempt_id, player_id,
                normalized_tag, endpoint, request_started_at, response_completed_at,
                http_status, response_hash, archive_reference, collector_version,
                key_label, evidence_headers
            ) VALUES (
                %s, %s, %s, %s, %s, 'profile', %s, %s, 200,
                %s, %s, 'test', 'normal-test', '{}'::jsonb
            ) RETURNING id
            """,
            (
                f"seed:{tag}:profile",
                job_id,
                attempt_id,
                player_id,
                tag,
                NOW,
                NOW,
                "a" * 64,
                f"s3://fixture/{tag.removeprefix('#')}",
            ),
        ).fetchone()[0]
        profile_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, profile_json
            ) VALUES (
                %s, %s, %s, 'profile-v1', 'profile-schema-v1',
                'profile-parser-v1', %s, 200, %s, %s, 105000036,
                'Legend I', 'eligible', '{}'::jsonb
            ) RETURNING id
            """,
            (player_id, observation_id, tag, NOW, f"Player {tag}", trophies),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE players
            SET current_profile_version_id = %s, current_observed_at = %s
            WHERE id = %s
            """,
            (profile_id, NOW, player_id),
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


def test_public_saved_operations_are_bounded_and_screen_ready(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            seed_profile(database, "#8PY", 6100)

            player = database.get_player_page("#2PP", now=NOW, freshness_seconds=900)
            live = database.get_live_leaderboard(
                limit=100, now=NOW, freshness_seconds=900
            )
            analytics = database.get_basic_analytics(now=NOW, freshness_seconds=900)

            assert player is not None
            assert player["tag"] == "#2PP"
            assert player["freshness"] == "fresh"
            assert player["screen_ready"]["current_day"] is None
            assert player["screen_ready"]["season"] is None
            assert player["screen_ready"]["season_days"] == []
            assert player["screen_ready"]["recent_days"][0]["offense_events"] == []
            assert player["screen_ready"]["recent_days"][0]["defense_events"] == []
            assert player["screen_ready"]["data_quality"][0]["code"] == "unavailable"
            assert player["daily_logs"] == [
                {
                    "ranked_day_start": "2026-08-06T05:00:00+00:00",
                    "ranked_day_end": None,
                    "official_season_id": None,
                    "season_day_number": None,
                    "version": 1,
                    "state": "Live",
                    "coverage": "partial",
                    "confidence": None,
                    "attack_count": None,
                    "attack_three_star_count": None,
                    "attack_gain": None,
                    "defense_count": None,
                    "defense_three_star_count": None,
                    "defense_loss": None,
                    "net_trophy_change": None,
                    "adjustments": [],
                    "battles": [],
                    "partial_reasons": ["active_day"],
                }
            ]
            assert [entry["tag"] for entry in live["entries"]] == ["#8PY", "#2PP"]
            assert live["kind"] == "live"
            assert live["ordering_rule_version"] == "tracked-trophies-md5-v1"
            assert analytics["population"] == "tracked_players"
            assert analytics["sample_size"] == 2
            assert analytics["classification_state"] == "unclassified"
            assert analytics["freshness"] == {"fresh": 2, "stale": 0}
        finally:
            database.close()


def test_known_player_name_search_uses_current_profiles_and_escapes_wildcards(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#8PY", 6100)
            seed_profile(database, "#2PP", 6000)

            assert database.search_known_players(
                "Player", now=NOW, freshness_seconds=900
            ) == [
                {
                    "tag": "#2PP",
                    "name": "Player #2PP",
                    "clan": None,
                    "trophies": 6000,
                    "freshness": "fresh",
                    "age_seconds": 0,
                    "observed_at": "2026-08-06T12:00:00+00:00",
                    "public_confidence": "high",
                },
                {
                    "tag": "#8PY",
                    "name": "Player #8PY",
                    "clan": None,
                    "trophies": 6100,
                    "freshness": "fresh",
                    "age_seconds": 0,
                    "observed_at": "2026-08-06T12:00:00+00:00",
                    "public_confidence": "high",
                },
            ]
            assert (
                database.search_known_players("%", now=NOW, freshness_seconds=900) == []
            )
        finally:
            database.close()


def test_player_screen_ready_current_day_preserves_partial_inferred_evidence(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET ranked_day_end = %s,
                        official_season_id = '1783918800',
                        season_day_number = 23,
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
                connection.commit()

            player = database.get_player_page("#2PP", now=NOW, freshness_seconds=900)

            assert player is not None
            current_day = player["screen_ready"]["current_day"]
            assert current_day is not None
            assert current_day["season_day_number"] == 23
            assert current_day["public_confidence"] == "partial"
            assert current_day["completeness"] == {
                "state": "partial",
                "reason": "active_day",
            }
            assert current_day["uncertainty_reasons"] == ["active_day"]
            assert current_day["offense_events"] == []
            assert current_day["defense_events"] == []
            assert player["screen_ready"]["season_days"] == [current_day]
            assert player["screen_ready"]["season"] == {
                "id": "1783918800",
                "current_day_number": 23,
                "start": "2026-08-06T05:00:00+00:00",
                "end": "2026-08-06T13:00:00+00:00",
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


def test_player_screen_ready_limits_season_days_to_current_official_season(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
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
                        partial_reasons = '[]'::jsonb
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

            player = database.get_player_page("#2PP", now=NOW, freshness_seconds=900)

            assert player is not None
            screen = player["screen_ready"]
            assert [day["season_day_number"] for day in screen["season_days"]] == [
                3,
                2,
            ]
            assert all(
                day["official_season_id"] == "current-season"
                for day in screen["season_days"]
            )
            assert screen["season_days"][0] == screen["current_day"]
            assert screen["current_day"]["offense_events"] == [
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
            assert screen["current_day"]["defense_events"][0]["trophy_change"] == -20
            assert screen["current_day"]["attack_count"] == len(
                screen["current_day"]["offense_events"]
            )
            assert screen["current_day"]["defense_count"] == len(
                screen["current_day"]["defense_events"]
            )
            assert screen["season_days"][1]["offense_events"] == []
            assert screen["season_days"][1]["defense_events"] == []
        finally:
            database.close()


def test_concurrent_refreshes_share_one_collector_job_and_public_refresh_identity(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info, max_size=12)
        try:

            def submit(_index: int):
                return database.submit_refresh(
                    anonymous_binding(
                        "refresh.submit",
                        "/v1/players/%232PP/refresh",
                        "#2PP",
                    ),
                    normalized_tag="#2PP",
                    cooldown_seconds=300,
                )

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(submit, range(10)))

            refresh_ids = {result.payload["refresh_id"] for result in results}
            assert len(refresh_ids) == 1
            assert database.scalar("SELECT count(*) FROM collector_jobs") == 1
            assert database.scalar("SELECT count(*) FROM api_refresh_requests") == 1
            assert (
                database.scalar(
                    "SELECT count(*) FROM collector_interactive_intent_events"
                )
                == 10
            )
            status = database.get_refresh_status(next(iter(refresh_ids)))
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
        "#" + alphabet[(index // 196) % 14] + alphabet[(index // 14) % 14] + alphabet[index % 14]
        for index in range(101)
    ]
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            for tag in tags:
                seed_profile(database, tag, 6000)
            with database.pool.connection() as connection:
                connection.execute(
                    """UPDATE player_profile_versions SET observed_at = %s - interval '900.5 seconds'
                       WHERE player_id = (SELECT id FROM players WHERE normalized_tag = %s)""",
                    (NOW, tags[0]),
                )
                connection.commit()
            first = database.get_live_leaderboard(
                limit=100, offset=0, now=NOW, freshness_seconds=900
            )
            second = database.get_live_leaderboard(
                limit=100, offset=100, now=NOW, freshness_seconds=900
            )
            assert first is not None and second is not None
            assert [entry["position"] for entry in first["entries"]] == list(range(1, 101))
            assert [entry["position"] for entry in second["entries"]] == [101]
            assert len({entry["tag"] for entry in first["entries"] + second["entries"]}) == 101
            assert first["total_entries"] == second["total_entries"] == 101
            assert first["page_count"] == second["page_count"] == 2
            assert first["has_next"] is True and second["has_previous"] is True
            assert first["provenance"]["freshness"] == "stale"
            stale_entry = next(
                entry
                for entry in first["entries"] + second["entries"]
                if entry["tag"] == tags[0]
            )
            assert stale_entry["age_seconds"] == 900
            assert stale_entry["freshness"] == "stale"
            assert database.get_live_leaderboard(
                limit=100, offset=200, now=NOW, freshness_seconds=900
            ) is None
        finally:
            database.close()
