from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from domain_test_support import store_observation
from psycopg.conninfo import make_conninfo
from test_api_db_public_ops import (
    NOW,
    anonymous_binding,
    seed_league_history,
    seed_profile,
)
from test_api_migration import migrated_production_database
from test_collector_db_postgres import _handoff, _hash
from test_domain_processing_postgres import _processor

from clashlens import api_player_lookup, api_players
from clashlens.api_db import ApiDatabase
from clashlens.collector_db import CollectorDatabase
from clashlens.profile import PROFILE_PARSER_VERSION


def submit(database: ApiDatabase, tag: str = "#2PP"):
    return api_player_lookup.submit_lookup(
        database,
        anonymous_binding("refresh.submit", f"/v1/players/{tag}/lookup", tag),
        normalized_tag=tag,
    ).payload


def test_concurrent_anonymous_lookups_schedule_one_immediate_interactive_first_collection(
    database_url,
):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        # Run admission with the deployed API role, not migration-owner powers.
        with psycopg.connect(info) as connection:
            schema = connection.execute("SELECT current_schema()").fetchone()[0]
        database = ApiDatabase(
            make_conninfo(
                info, options=f"-c search_path={schema} -c role=clashlens_python_api"
            )
        )
        try:
            assert api_player_lookup.get_lookup(database, "#2PP")["state"] == "unknown"
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: submit(database), range(8)))
            assert all(
                result == {"tag": "#2PP", "state": "checking"} for result in results
            )
            assert database.scalar("SELECT count(*) FROM players") == 1
            assert database.scalar("SELECT count(*) FROM collector_work") == 1
            with database.pool.connection() as connection:
                row = connection.execute(
                    "SELECT kind, lane, due_at <= clock_timestamp(), league_history_status FROM collector_work"
                ).fetchone()
                assert row == ("initial_collection", "interactive", True, "pending")
            collector = CollectorDatabase(info)
            try:
                assert len(collector.pending_intents(10, interactive=True)) == 1
                assert collector.pending_intents(10, interactive=False) == []
            finally:
                collector.close()
        finally:
            database.close()


@pytest.mark.parametrize(
    ("tier", "state", "active"),
    [
        ({"id": 105000036, "name": "Legend I"}, "tracking", True),
        ({"id": 105000035, "name": "Legend II"}, "not_in_legend", False),
        (None, "uncertain", False),
        ({"id": 123, "name": "Unknown tier"}, "uncertain", False),
    ],
)
def test_first_profile_retains_real_identity_and_uses_existing_eligibility(
    database_url, archive_server, tier, state, active
):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        worker, processor = _processor(info, archive_server)
        try:
            assert submit(database)["state"] == "checking"
            body = json.loads(
                (
                    Path(__file__).parents[1] / "testdata/legend_i_profile_v1.json"
                ).read_bytes()
            )
            body["leagueTier"] = tier
            body["townHallLevel"] = 1
            store_observation(
                info,
                archive_server,
                occurrence_key="lookup-profile",
                endpoint="profile",
                body=json.dumps(body).encode(),
                observed_at=NOW,
                normalized_tag="#2PP",
                parser_version=PROFILE_PARSER_VERSION,
            )
            result = processor.process_once(owner="lookup-test")
            assert result is not None and result.outcome == "processed"
            assert api_player_lookup.get_lookup(database, "#2PP")["state"] == state
            assert (
                database.scalar(
                    "SELECT active FROM players WHERE normalized_tag = '#2PP'"
                )
                is active
            )
            assert database.scalar("SELECT count(*) FROM players") == 1
            assert submit(database)["state"] == state
            assert (
                database.scalar(
                    "SELECT count(*) FROM collector_work WHERE kind = 'initial_collection'"
                )
                == 1
            )
        finally:
            worker.close()
            database.close()


def test_official_not_found_is_distinct_from_transport_failure_and_retries_are_bounded(
    database_url,
):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        collector = CollectorDatabase(info)
        try:
            assert submit(database)["state"] == "checking"
            with database.pool.connection() as connection:
                work_id, player_id = connection.execute(
                    "SELECT id, player_id FROM collector_work"
                ).fetchone()
            collector.record_response(
                _handoff(
                    occurrence_key="lookup-404",
                    response_hash=_hash("lookup-404"),
                    player_id=player_id,
                    collector_work_id=work_id,
                    http_status=404,
                )
            )
            assert (
                api_player_lookup.get_lookup(database, "#2PP")["state"] == "not_found"
            )
            assert collector.complete_intent(work_id) is False
            assert submit(database)["state"] == "not_found"
            assert database.scalar("SELECT count(*) FROM collector_work") == 1
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE collector_work SET updated_at = clock_timestamp() - interval '31 seconds'"
                )
            assert submit(database)["state"] == "checking"
            assert database.scalar("SELECT count(*) FROM collector_work") == 2
            work_id = database.scalar("SELECT max(id) FROM collector_work")
            collector.fail_intent(work_id, category="provider_failure")
            assert api_player_lookup.get_lookup(database, "#2PP")["state"] == "failed"
            assert database.scalar("SELECT active FROM players") is False
            assert database.scalar("SELECT count(*) FROM player_profile_versions") == 0
        finally:
            collector.close()
            database.close()


def test_name_results_only_include_active_or_historical_players(database_url):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        try:
            for tag in ("#2PP", "#8PY", "#2PY"):
                seed_profile(database, tag, 6000)
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE players SET active = false, eligibility_state = 'ineligible' WHERE normalized_tag IN ('#8PY', '#2PY')"
                )
                connection.execute(
                    "DELETE FROM api_player_daily_logs WHERE player_id = (SELECT id FROM players WHERE normalized_tag = '#2PP')"
                )
                connection.execute(
                    "UPDATE api_player_daily_logs SET partial_reasons = '[\"player_not_eligible\"]'::jsonb WHERE player_id = (SELECT id FROM players WHERE normalized_tag = '#2PY')"
                )
            results = api_players.search_known_players(
                database, "Player", now=NOW, freshness_seconds=900
            )
            assert {result["tag"] for result in results} == {"#2PP", "#8PY"}
            assert (
                api_player_lookup.get_lookup(database, "#2PY")["state"]
                == "not_in_legend"
            )
            assert submit(database, "#2PY")["state"] == "not_in_legend"
            assert database.scalar("SELECT count(*) FROM collector_work") == 3
        finally:
            database.close()


def test_name_search_preserves_history_membership_order_and_limit(database_url):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        try:
            tags = ("#2PP", "#2PY", "#2PQ", "#2PR", "#2PV", "#2P0", "#2P2", "#2P8", "#2P9")
            for tag in tags:
                seed_profile(database, tag, 6000)
            seed_league_history(database, "#2PV", "202608")
            with database.pool.connection() as connection:
                # The first two matching names must be excluded before LIMIT.
                connection.execute("UPDATE players SET active = false")
                connection.execute(
                    "UPDATE players SET active = true WHERE normalized_tag IN ('#2PP', '#2P8', '#2P9')"
                )
                connection.execute(
                    "UPDATE player_profile_versions SET source_contract_state = 'quarantined' WHERE normalized_tag = '#2P8'"
                )
                connection.execute(
                    "UPDATE players SET current_profile_version_id = NULL WHERE normalized_tag = '#2P9'"
                )
                connection.execute(
                    """
                    UPDATE player_profile_versions SET name = CASE
                        WHEN normalized_tag IN ('#2P0', '#2P2') THEN 'aaa Hiroya'
                        WHEN normalized_tag = '#2PY' THEN 'HIROYA'
                        ELSE 'Hiroya' END
                    """
                )
                connection.execute(
                    """
                    DELETE FROM api_player_daily_logs WHERE player_id IN (
                        SELECT id FROM players
                        WHERE normalized_tag NOT IN ('#2PY', '#2PQ', '#2P2')
                    )
                    """
                )
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET partial_reasons = '["player_not_eligible"]'::jsonb,
                        battles = CASE WHEN player_id = (
                            SELECT id FROM players WHERE normalized_tag = '#2PQ'
                        ) THEN '[{}]'::jsonb ELSE '[]'::jsonb END
                    WHERE player_id IN (
                        SELECT id FROM players WHERE normalized_tag IN ('#2PQ', '#2P2')
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO player_season_summaries (
                        player_id, official_season_id, projection_version, content_digest
                    ) SELECT id, '202608', 'test', repeat('a', 64)
                      FROM players WHERE normalized_tag = '#2PR'
                    """
                )
                schema = connection.execute("SELECT current_schema()").fetchone()[0]
            reader = ApiDatabase(
                make_conninfo(
                    info, options=f"-c search_path={schema} -c role=clashlens_python_api"
                )
            )
            try:
                for limit in (1, 3, 50):
                    results = api_players.search_known_players(
                        reader, "iRoY", now=NOW, freshness_seconds=900, limit=limit
                    )
                    assert [row["tag"] for row in results] == [
                        "#2PP", "#2PQ", "#2PR", "#2PV", "#2PY"
                    ][:limit]
            finally:
                reader.close()
        finally:
            database.close()


@pytest.mark.parametrize("query", ["%", "_", "\\", "東京"])
def test_name_search_matches_literal_substrings_and_ignores_old_names(
    database_url, query
):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        try:
            seed_profile(database, "#2PP", 6000)
            seed_profile(database, "#2PY", 6100)
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE player_profile_versions SET name = %s",
                    (f"Before {query} After",),
                )
                current_id = connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag, endpoint_version,
                        schema_version, parser_version, observed_at, source_http_status,
                        name, trophies, league_tier_id, league_tier_name,
                        eligibility_state, profile_json
                    ) SELECT player_id, observation_id, normalized_tag, endpoint_version,
                             schema_version, 'search-test-current', observed_at, 200,
                             'Renamed', trophies, league_tier_id, league_tier_name,
                             eligibility_state, profile_json
                      FROM player_profile_versions WHERE normalized_tag = '#2PY'
                    RETURNING id
                    """
                ).fetchone()[0]
                connection.execute(
                    "UPDATE players SET current_profile_version_id = %s WHERE normalized_tag = '#2PY'",
                    (current_id,),
                )
            results = api_players.search_known_players(
                database, query, now=NOW, freshness_seconds=900
            )
            assert [row["tag"] for row in results] == ["#2PP"]
            assert results[0]["name"] == f"Before {query} After"
        finally:
            database.close()


@pytest.mark.parametrize(
    "processing", ["pending", "leased", "waiting_retry", "waiting_dependency"]
)
@pytest.mark.parametrize("endpoint", ["battle_log", "league_history"])
def test_ancillary_failure_waits_for_profile_processing_without_recollecting(
    database_url, processing, endpoint
):
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        collector = CollectorDatabase(info)
        try:
            assert submit(database)["state"] == "checking"
            with database.pool.connection() as connection:
                work_id, player_id = connection.execute(
                    "SELECT id, player_id FROM collector_work"
                ).fetchone()
            collector.record_response(
                _handoff(
                    occurrence_key="lookup-profile",
                    response_hash=_hash("lookup-profile"),
                    player_id=player_id,
                    collector_work_id=work_id,
                )
            )
            assert collector.fail_intent(work_id, category=f"{endpoint}_failure")
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs SET status = %s,
                        lease_owner = CASE WHEN %s = 'leased' THEN 'lookup-test' END,
                        lease_token = CASE WHEN %s = 'leased' THEN gen_random_uuid() END,
                        lease_expires_at = CASE WHEN %s = 'leased'
                            THEN clock_timestamp() + interval '60 seconds' END
                    WHERE observation_id = (
                        SELECT profile_observation_id FROM collector_work WHERE id = %s
                    ) AND work_type = 'process_observation'
                    """,
                    (processing, processing, processing, processing, work_id),
                )
                connection.execute(
                    "UPDATE collector_work SET updated_at = clock_timestamp() - interval '31 seconds' WHERE id = %s",
                    (work_id,),
                )
            assert api_player_lookup.get_lookup(database, "#2PP")["state"] == "checking"
            assert submit(database)["state"] == "checking"
            assert database.scalar("SELECT count(*) FROM collector_work") == 1
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE python_processing_jobs SET status = 'failed',
                        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                    WHERE work_type = 'process_observation'
                    """
                )
            assert api_player_lookup.get_lookup(database, "#2PP")["state"] == "failed"
        finally:
            collector.close()
            database.close()
