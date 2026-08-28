from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_domain_processing_postgres import _processor

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKING_FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
NOW = datetime(2026, 8, 6, 6, tzinfo=UTC)


def _replay_job(
    connection: psycopg.Connection, observation_id: int, parser_version: str
) -> int:
    return connection.execute(
        """
        INSERT INTO python_processing_jobs (
            replay_observation_id, work_type, deduplication_key, input_json,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version
        ) VALUES (
            %s, 'replay_observation', %s, '{\"replay_request_id\": 1}'::jsonb,
            %s, 'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
            'legend-analytics-v1'
        )
        RETURNING id
        """,
        (
            observation_id,
            f"dedup-replay:{observation_id}:{parser_version}",
            parser_version,
        ),
    ).fetchone()[0]


def test_one_observation_replays_under_both_parser_versions_without_domain_change(
    database_url: str, archive_server
) -> None:
    body = PROFILE_FIXTURE.read_bytes()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation_id, initial_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-parser-versioned-observation",
            endpoint="profile",
            body=body,
            observed_at=NOW,
            normalized_tag="#2PP",
            parser_version="supercell-source-parser-v2",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            first = processor.process_job(initial_job_id, owner="dedup-parser-v2")
            assert first is not None and first.outcome == "processed"
            public_result = database.get_player("#2PP")
            with psycopg.connect(connection_info) as connection:
                replay_v1_job_id = _replay_job(
                    connection, observation_id, "supercell-source-parser-v1"
                )
                replay_v2_job_id = _replay_job(
                    connection, observation_id, "supercell-source-parser-v2"
                )
                connection.commit()
            for job_id, owner in (
                (replay_v1_job_id, "dedup-parser-v1"),
                (replay_v2_job_id, "dedup-parser-v2-replay"),
            ):
                replay = processor.process_job(job_id, owner=owner)
                assert replay is not None and replay.outcome == "processed"

            assert database.get_player("#2PP") == public_result
            with psycopg.connect(connection_info) as connection:
                payloads = connection.execute(
                    """
                    SELECT endpoint, response_hash, parser_version
                    FROM parsed_source_payloads
                    WHERE endpoint = 'profile'
                    ORDER BY parser_version
                    """
                ).fetchall()
                profile_counts = connection.execute(
                    """
                    SELECT count(DISTINCT profile.id), count(effect.id)
                    FROM player_profile_versions AS profile
                    LEFT JOIN player_profile_effects AS effect
                      ON effect.profile_version_id = profile.id
                    WHERE profile.player_id = (
                        SELECT id FROM players WHERE normalized_tag = '#2PP'
                    )
                    """
                ).fetchone()
                outcomes = connection.execute(
                    """
                    SELECT parser_version, outcome, failure_category
                    FROM observation_processing_outcomes
                    WHERE observation_id = %s
                    ORDER BY parser_version
                    """,
                    (observation_id,),
                ).fetchall()
            assert [tuple(text(value) for value in row) for row in payloads] == [
                ("profile", payloads[0][1], "supercell-source-parser-v1"),
                ("profile", payloads[1][1], "supercell-source-parser-v2"),
            ]
            assert payloads[0][1] == payloads[1][1]
            assert profile_counts == (1, 2)
            assert [tuple(text(value) for value in row) for row in outcomes] == [
                ("supercell-source-parser-v1", "processed", None),
                ("supercell-source-parser-v2", "processed", None),
            ]
        finally:
            database.close()


def test_reused_semantic_profile_keeps_retained_season_anchor_conflict(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _anchor_observation_id, anchor_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-anchor-seed",
            endpoint="profile",
            body=PROFILE_FIXTURE.read_bytes(),
            observed_at=NOW,
            normalized_tag="#2PP",
        )
        payload = json.loads(PROFILE_FIXTURE.read_bytes())
        payload["tag"] = "#8PP"
        payload["currentLeagueSeasonId"] = "1781499600"
        payload["previousLeagueSeasonId"] = "1779080400"
        observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-anchor-conflict",
            endpoint="profile",
            body=json.dumps(payload).encode(),
            observed_at=NOW + timedelta(minutes=1),
            normalized_tag="#8PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            seed = processor.process_job(anchor_job_id, owner="dedup-anchor-seed")
            assert seed is not None and seed.outcome == "processed"
            first = processor.process_job(job_id, owner="dedup-anchor-conflict-v2")
            assert first is not None and first.outcome == "processed"
            with psycopg.connect(connection_info) as connection:
                replay_job_id = _replay_job(
                    connection, observation_id, "supercell-source-parser-v1"
                )
                connection.commit()
            replay = processor.process_job(
                replay_job_id, owner="dedup-anchor-conflict-v1"
            )
            assert replay is not None and replay.outcome == "processed"

            with psycopg.connect(connection_info) as connection:
                outcome = connection.execute(
                    """
                    SELECT failure_category
                    FROM observation_processing_outcomes
                    WHERE observation_id = %s AND parser_version = 'supercell-source-parser-v1'
                    """,
                    (observation_id,),
                ).fetchone()
                retained = connection.execute(
                    """
                    SELECT profile.source_contract_state, anchor.outcome
                    FROM player_profile_versions AS profile
                    JOIN season_anchor_evidence AS anchor
                      ON anchor.profile_version_id = profile.id
                    WHERE profile.observation_id = %s
                    """,
                    (observation_id,),
                ).fetchone()
            assert text(outcome[0]) == "season_anchor_conflict"
            assert tuple(text(value) for value in retained) == ("conflict", "conflict")
        finally:
            database.close()


def test_duplicate_profiles_reuse_canonical_and_semantic_rows(
    database_url: str, archive_server
) -> None:
    body = PROFILE_FIXTURE.read_bytes()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            for index in range(2):
                store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key=f"dedup-profile-{index}",
                    endpoint="profile",
                    body=body,
                    observed_at=NOW + timedelta(minutes=index),
                    normalized_tag="#2PP",
                )
            assert processor.process_once(owner="dedup-profile-1") is not None
            with psycopg.connect(connection_info) as connection:
                first_updated_at = connection.execute(
                    "SELECT updated_at FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            assert processor.process_once(owner="dedup-profile-2") is not None
            with psycopg.connect(connection_info) as connection:
                second_updated_at = connection.execute(
                    "SELECT updated_at FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            assert second_updated_at == first_updated_at
            changed = json.loads(body)
            changed["clan"]["name"] = "Changed Clan"
            store_observation(
                connection_info,
                archive_server,
                occurrence_key="dedup-profile-clan-change",
                endpoint="profile",
                body=json.dumps(changed).encode(),
                observed_at=NOW + timedelta(minutes=2),
                normalized_tag="#2PP",
            )
            assert processor.process_once(owner="dedup-profile-3") is not None

            with psycopg.connect(connection_info) as connection:
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM parsed_source_payloads),
                        (SELECT count(*) FROM player_profile_versions),
                        (SELECT count(*) FROM player_profile_effects),
                        (SELECT count(*) FROM observation_processing_outcomes),
                        (SELECT count(*) FROM player_profile_effects
                         WHERE parsed_payload_id IS NOT NULL)
                    """
                ).fetchone()
                timestamps = connection.execute(
                    """
                    SELECT players.updated_at, players.current_observed_at,
                           payload.created_at, payload.parsed_json
                    FROM players
                    JOIN parsed_source_payloads AS payload
                      ON payload.endpoint = 'profile'
                    WHERE players.normalized_tag = '#2PP'
                    """
                ).fetchone()
                with pytest.raises(psycopg.errors.RaiseException):
                    connection.execute(
                        """
                        UPDATE parsed_source_payloads
                        SET parsed_json = '{\"contradiction\": true}'::jsonb
                        WHERE endpoint = 'profile'
                        """
                    )
                connection.rollback()
            assert counts == (2, 2, 3, 3, 3)
            assert timestamps is not None
            assert timestamps[0] > first_updated_at
            assert timestamps[1] > NOW
            assert timestamps[2] is not None
            assert timestamps[3] == json.loads(body)
        finally:
            database.close()


def test_duplicate_battles_and_rankings_reuse_source_rows(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            jobs = []
            for index in range(2):
                jobs.append(
                    store_observation(
                        connection_info,
                        archive_server,
                        occurrence_key=f"dedup-battle-{index}",
                        endpoint="battle_log",
                        body=BATTLE_FIXTURE.read_bytes(),
                        observed_at=NOW + timedelta(minutes=index),
                        normalized_tag="#2PP",
                    )[1]
                )
            for index in range(2):
                jobs.append(
                    store_observation(
                        connection_info,
                        archive_server,
                        occurrence_key=f"dedup-ranking-{index}",
                        endpoint="global_player_rankings",
                        body=RANKING_FIXTURE.read_bytes(),
                        observed_at=NOW + timedelta(minutes=index),
                        normalized_tag=None,
                    )[1]
                )
            for index, job_id in enumerate(jobs):
                result = processor.process_job(job_id, owner=f"dedup-source-{index}")
                assert result is not None and result.outcome == "processed"

            with psycopg.connect(connection_info) as connection:
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM parsed_source_payloads
                         WHERE endpoint = 'battle_log'),
                        (SELECT count(*) FROM battle_source_rows
                         WHERE parsed_payload_id IS NOT NULL),
                        (SELECT count(*) FROM battle_log_observation_rows),
                        (SELECT count(*) FROM battle_evidence),
                        (SELECT count(*) FROM parsed_source_payloads
                         WHERE endpoint = 'global_player_rankings'),
                        (SELECT count(*) FROM official_top200_entries
                         WHERE parsed_payload_id IS NOT NULL),
                        (SELECT count(*) FROM official_top200_attempt_entries),
                        (SELECT count(*) FROM official_top200_version_entries)
                    """
                ).fetchone()
            battle_items = json.loads(BATTLE_FIXTURE.read_bytes())["items"]
            valid_battles = sum(item["battleType"] == "legend" for item in battle_items)
            assert counts == (
                1,
                len(battle_items),
                len(battle_items) * 2,
                valid_battles * 2,
                1,
                200,
                400,
                400,
            )
        finally:
            database.close()
