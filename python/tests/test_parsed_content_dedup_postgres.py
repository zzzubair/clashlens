from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from test_domain_processing_postgres import _processor

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKING_FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
NOW = datetime(2026, 8, 6, 6, tzinfo=UTC)


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
