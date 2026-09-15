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
PROFILE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
OBSERVED_AT = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)


def test_live_discovery_sources_enqueue_once_and_replay_does_not(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
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
                    "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
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
                    "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
                ).fetchone()[0]
            assert after == before
        finally:
            database.close()


def test_partial_ranking_rank_values_do_not_corrupt_source_row_provenance(
    database_url: str, archive_server
) -> None:
    payload = json.loads(RANKINGS.read_bytes())
    payload["items"][0]["rank"] = 0
    payload["items"][1]["rank"] = -1
    with domain_database(database_url) as connection_info:
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="discovery-ranking-nonpositive-ranks",
            endpoint="global_player_rankings",
            body=json.dumps(payload).encode(),
            observed_at=OBSERVED_AT,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_once(owner="discovery-ranking-nonpositive-ranks")
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                rows = connection.execute(
                    """SELECT source_row_index FROM known_player_discoveries
                       WHERE source_kind = 'official_ranking'
                       ORDER BY source_row_index"""
                ).fetchall()
                attempt = connection.execute(
                    "SELECT outcome FROM official_top200_attempts"
                ).fetchone()
            assert [row[0] for row in rows] == list(range(200))
            assert attempt is not None and text(attempt[0]) == "official_partial"
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
                            "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
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
    with domain_database(database_url, include_coordinator=True) as connection_info:
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
            first_work = connection.execute(
                "SELECT id FROM collector_work WHERE player_id = %s", (unknown,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = clock_timestamp() WHERE id = %s",
                (first_work,),
            )
            connection.execute(
                """INSERT INTO collector_work (
                       kind, lane, scope, player_id, normalized_tag, due_at,
                       coalescing_key, status, profile_status, battle_log_status,
                       league_history_status, completed_at)
                   SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag,
                          clock_timestamp(), 'discovery-profile:' || id,
                          'complete', 'observed', 'not_applicable', 'observed',
                          clock_timestamp()
                   FROM players WHERE id = %s""",
                (terminal,),
            )
            assert connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                ([terminal],),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM collector_work WHERE player_id = %s", (terminal,)
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
                "SELECT count(*) FROM collector_work WHERE player_id = %s",
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
                    has_table_privilege('clashlens_python_worker', 'collector_work', 'INSERT')"""
            ).fetchone()
            assert privileges == (True, False, False, False)


def test_ineligible_profile_cancels_only_ordinary_discovery_and_keeps_evidence(
    database_url: str, archive_server
) -> None:
    payload = json.loads(PROFILE.read_bytes())
    payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="inactive-player-profile",
            endpoint="profile",
            body=json.dumps(payload).encode(),
            observed_at=OBSERVED_AT,
            normalized_tag="#2PP",
        )
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                """UPDATE players
                   SET active = true, eligibility_state = 'eligible',
                       next_due_at = clock_timestamp()
                   WHERE normalized_tag = '#2PP'
                   RETURNING id"""
            ).fetchone()[0]
            unknown_player_id = connection.execute(
                """INSERT INTO players (
                       normalized_tag, active, eligibility_state, next_due_at
                   ) VALUES ('#2PQ', false, 'unknown', NULL)
                   RETURNING id"""
            ).fetchone()[0]
            sweep_id = connection.execute(
                """INSERT INTO collector_reset_sweeps (boundary_at, member_ids)
                   VALUES (%s, %s) RETURNING id""",
                (OBSERVED_AT.replace(hour=5, minute=0), [player_id]),
            ).fetchone()[0]
            discovery_id = connection.execute(
                """INSERT INTO collector_work (
                       kind, lane, scope, player_id, normalized_tag, due_at,
                       coalescing_key, profile_status, battle_log_status,
                       league_history_status, profile_observation_id
                   ) VALUES (
                       'discovery_profile', 'ordinary', 'player', %s, '#2PP', %s,
                       'inactive-cancel:discovery', 'observed', 'not_applicable',
                       'pending', %s
                   ) RETURNING id""",
                (player_id, OBSERVED_AT, observation_id),
            ).fetchone()[0]
            refresh_id = connection.execute(
                """INSERT INTO collector_work (
                       kind, lane, scope, player_id, normalized_tag, due_at,
                       coalescing_key, profile_status, battle_log_status,
                       league_history_status
                   ) VALUES (
                       'live_refresh', 'interactive', 'player', %s, '#2PP', %s,
                       'inactive-cancel:refresh', 'pending', 'pending',
                       'not_applicable'
                   ) RETURNING id""",
                (player_id, OBSERVED_AT),
            ).fetchone()[0]
            reset_id = connection.execute(
                """INSERT INTO collector_work (
                       kind, lane, scope, player_id, normalized_tag, sweep_id,
                       due_at, coalescing_key, profile_status, battle_log_status,
                       league_history_status
                   ) VALUES (
                       'reset_baseline', 'reset', 'player', %s, '#2PP', %s, %s,
                       'inactive-cancel:reset', 'pending', 'pending',
                       'not_applicable'
                   ) RETURNING id""",
                (player_id, sweep_id, OBSERVED_AT),
            ).fetchone()[0]
            unknown_discovery_id = connection.execute(
                """INSERT INTO collector_work (
                       kind, lane, scope, player_id, normalized_tag, due_at,
                       coalescing_key, profile_status, battle_log_status,
                       league_history_status
                   ) VALUES (
                       'discovery_profile', 'ordinary', 'player', %s, '#2PQ', %s,
                       'inactive-cancel:unknown', 'pending', 'not_applicable',
                       'pending'
                   ) RETURNING id""",
                (unknown_player_id, OBSERVED_AT),
            ).fetchone()[0]
            connection.commit()

            connection.execute("SET ROLE clashlens_python_worker")
            assert connection.execute(
                "SELECT clashlens_cancel_inactive_discovery_work(%s)",
                (unknown_player_id,),
            ).fetchone()[0] == 0
            connection.execute("RESET ROLE")

        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_job(job_id, owner="inactive-profile-worker")
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                player = connection.execute(
                    """SELECT active, eligibility_state, next_due_at
                       FROM players WHERE id = %s""",
                    (player_id,),
                ).fetchone()
                work = {
                    int(row[0]): text(row[1])
                    for row in connection.execute(
                        """SELECT id, status
                           FROM collector_work
                           WHERE id = ANY(%s::bigint[])""",
                        ([discovery_id, refresh_id, reset_id, unknown_discovery_id],),
                    ).fetchall()
                }
                retained_observation = connection.execute(
                    """SELECT profile_observation_id
                       FROM collector_work WHERE id = %s""",
                    (discovery_id,),
                ).fetchone()[0]
                connection.execute("SET ROLE clashlens_python_worker")
                reentry_count = connection.execute(
                    "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                    ([player_id],),
                ).fetchone()[0]
                connection.execute("RESET ROLE")
                discovery_states = [
                    text(row[0])
                    for row in connection.execute(
                        """SELECT status FROM collector_work
                           WHERE player_id = %s AND kind = 'discovery_profile'
                           ORDER BY id""",
                        (player_id,),
                    ).fetchall()
                ]
            assert (player[0], text(player[1]), player[2]) == (
                False,
                "ineligible",
                None,
            )
            assert work == {
                discovery_id: "cancelled",
                refresh_id: "pending",
                reset_id: "pending",
                unknown_discovery_id: "pending",
            }
            assert retained_observation == observation_id
            assert reentry_count == 1
            assert discovery_states == ["cancelled", "pending"]
        finally:
            database.close()


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
