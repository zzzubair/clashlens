from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_domain_processing_postgres import _processor

BATTLE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKINGS = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
OBSERVED_AT = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)


def test_live_discovery_sources_enqueue_once_and_replay_does_not(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="discovery-battle-live",
            endpoint="battle_log",
            body=BATTLE.read_bytes(),
            observed_at=OBSERVED_AT,
            normalized_tag="#2PP",
        )
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="discovery-ranking-live",
            endpoint="global_player_rankings",
            body=RANKINGS.read_bytes(),
            observed_at=OBSERVED_AT,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_once(owner="discovery-battle") is not None
            assert processor.process_once(owner="discovery-ranking") is not None
            with database.pool.connection() as connection:
                before = connection.execute(
                    "SELECT count(*) FROM collector_jobs WHERE work_type = 'discovery_profile'"
                ).fetchone()[0]
                ranking_discoveries = connection.execute(
                    """SELECT count(*), count(DISTINCT player_id),
                              min(source_row_index), max(source_row_index)
                       FROM known_player_discoveries
                       WHERE source_kind = 'official_ranking'"""
                ).fetchone()
                official_entries = connection.execute(
                    "SELECT count(DISTINCT player_id) FROM official_top200_entries"
                ).fetchone()[0]
            assert before == 201
            assert ranking_discoveries == (200, 200, 0, 199)
            assert official_entries == 200

            replay_observation, replay_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="discovery-battle-replay",
                endpoint="battle_log",
                body=BATTLE.read_bytes(),
                observed_at=OBSERVED_AT,
                normalized_tag="#2PP",
            )
            with database.pool.connection() as connection:
                connection.execute(
                    """UPDATE python_processing_jobs
                       SET work_type = 'replay_observation', observation_id = NULL,
                           replay_observation_id = %s,
                           deduplication_key = 'replay:discovery-battle:v1',
                           input_json = '{"replay_request_id":1}'::jsonb
                       WHERE id = %s""",
                    (replay_observation, replay_job),
                )
                connection.commit()
            assert processor.process_once(owner="discovery-replay") is not None
            with database.pool.connection() as connection:
                after = connection.execute(
                    "SELECT count(*) FROM collector_jobs WHERE work_type = 'discovery_profile'"
                ).fetchone()[0]
            assert after == before
        finally:
            database.close()


def test_contract_changed_rankings_enqueue_more_than_500_valid_discoveries(
    database_url: str, archive_server
) -> None:
    tags = ["#" + "".join(value) for value in product("0289PYLQGRJCUV", repeat=3)][:501]
    body = json.dumps(
        {
            "items": [
                {"rank": rank, "tag": tag} for rank, tag in enumerate(tags, start=1)
            ]
            + [{}],
            "paging": {"cursors": {}},
        }
    ).encode()
    with domain_database(database_url) as connection_info:
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="discovery-ranking-over-500",
            endpoint="global_player_rankings",
            body=body,
            observed_at=OBSERVED_AT,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_once(owner="discovery-ranking-over-500")
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM known_player_discoveries"
                    ).fetchone()[0]
                    == 501
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM collector_jobs WHERE work_type = 'discovery_profile'"
                    ).fetchone()[0]
                    == 501
                )
                assert text(
                    connection.execute(
                        "SELECT outcome FROM official_top200_attempts"
                    ).fetchone()[0]
                ) == "official_contract_changed"
        finally:
            database.close()


def test_enqueue_cycle_coalescing_terminal_rediscovery_inputs_and_privileges(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            ids = connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                VALUES ('#2PP', false, 'unknown'), ('#2PQ', true, 'eligible'),
                       ('#2PY', false, 'unknown')
                RETURNING id, normalized_tag
                """
            ).fetchall()
            by_tag = {text(row[1]): row[0] for row in ids}
            unknown, eligible, terminal = by_tag["#2PP"], by_tag["#2PQ"], by_tag["#2PY"]
            connection.commit()

        def enqueue() -> int:
            with psycopg.connect(connection_info) as connection:
                return connection.execute(
                    "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                    ([unknown, unknown, eligible],),
                ).fetchone()[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(lambda _index: enqueue(), range(2))) == [0, 1]

        with psycopg.connect(connection_info) as connection:
            first_job = connection.execute(
                "SELECT id FROM collector_jobs WHERE player_id = %s", (unknown,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE collector_jobs SET status = 'complete' WHERE id = %s", (first_job,)
            )
            connection.execute(
                """INSERT INTO discovery_profile_intents (player_id, cycle_at)
                   VALUES (%s, date_bin(interval '5 minutes', clock_timestamp(),
                                       timestamptz '2000-01-01') - interval '5 minutes')
                   ON CONFLICT DO NOTHING""",
                (terminal,),
            )
            connection.execute(
                """INSERT INTO collector_jobs (
                       work_type, scope, player_id, normalized_tag, capacity_pool,
                       priority, due_at, coalescing_key, required_endpoint, status)
                   SELECT 'discovery_profile', 'player', id, normalized_tag, 'normal',
                          300, clock_timestamp(), 'discovery-profile:' || id,
                          'profile', 'complete' FROM players WHERE id = %s""",
                (terminal,),
            )
            assert connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                ([terminal],),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM collector_jobs WHERE player_id = %s", (terminal,)
            ).fetchone()[0] == 2
            connection.commit()
            for invalid in (None, [0], [999999999], list(range(1, 502))):
                with pytest.raises(psycopg.Error):
                    connection.execute(
                        "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                        (invalid,),
                    )
                connection.rollback()
            shadow_player = connection.execute(
                """INSERT INTO players (normalized_tag, active, eligibility_state)
                   VALUES ('#2P8', false, 'unknown') RETURNING id"""
            ).fetchone()[0]
            connection.commit()
            connection.execute(
                """CREATE TEMP TABLE players (
                       id bigint, normalized_tag text, active boolean, eligibility_state text
                   )"""
            )
            connection.execute("SET ROLE clashlens_python_worker")
            assert connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                ([shadow_player],),
            ).fetchone()[0] == 1
            connection.execute("RESET ROLE")
            assert connection.execute(
                "SELECT count(*) FROM collector_jobs WHERE player_id = %s",
                (shadow_player,),
            ).fetchone()[0] == 1

            privileges = connection.execute(
                """SELECT
                    has_function_privilege('clashlens_python_worker',
                      'clashlens_enqueue_discovery_profiles(bigint[])', 'EXECUTE'),
                    has_function_privilege('clashlens_python_api',
                      'clashlens_enqueue_discovery_profiles(bigint[])', 'EXECUTE'),
                    has_function_privilege('clashlens_collector',
                      'clashlens_enqueue_discovery_profiles(bigint[])', 'EXECUTE'),
                    has_table_privilege('clashlens_python_worker', 'collector_jobs', 'INSERT')"""
            ).fetchone()
            assert privileges == (True, False, False, False)


def test_enqueue_failure_rolls_back_discovery_provenance(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="discovery-rollback",
            endpoint="battle_log",
            body=BATTLE.read_bytes(),
            observed_at=OBSERVED_AT,
            normalized_tag="#2PP",
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """CREATE OR REPLACE FUNCTION clashlens_enqueue_discovery_profiles(requested_player_ids bigint[])
                   RETURNS integer LANGUAGE plpgsql AS $$ BEGIN
                     RAISE EXCEPTION 'forced enqueue failure';
                   END $$"""
            )
            connection.commit()
        database, processor = _processor(connection_info, archive_server)
        try:
            with pytest.raises(psycopg.Error, match="forced enqueue failure"):
                processor.process_once(owner="discovery-rollback")
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM known_player_discoveries"
                ).fetchone()[0] == 0
        finally:
            database.close()
