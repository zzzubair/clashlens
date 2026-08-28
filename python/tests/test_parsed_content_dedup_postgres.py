from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from psycopg.conninfo import make_conninfo
from test_domain_processing_postgres import _processor

from clashlens.api_db import ApiDatabase

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKING_FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
NOW = datetime(2026, 8, 6, 6, tzinfo=UTC)


@contextmanager
def _pre_dedup_database(database_url: str):
    schema = f"python_pre_dedup_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        root = Path(__file__).parents[2]
        with psycopg.connect(connection_info, autocommit=True) as connection:
            for path in sorted((root / "deploy/migrations").glob("*.sql")):
                if path.name < "0012_":
                    connection.execute(path.read_text(encoding="utf-8"))
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


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


def test_army_backfill_migration_reclassifies_only_0008_jobs_and_live_claims_first(
    database_url: str, archive_server
) -> None:
    with _pre_dedup_database(database_url) as connection_info:
        _observation_id, battle_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="army-backfill-battle",
            endpoint="battle_log",
            body=BATTLE_FIXTURE.read_bytes(),
            observed_at=NOW,
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            result = processor.process_job(battle_job_id, owner="army-battle-seed")
            assert result is not None and result.outcome == "processed"
        finally:
            database.close()

        _live_observation_id, live_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="army-backfill-live-profile",
            endpoint="profile",
            body=PROFILE_FIXTURE.read_bytes(),
            observed_at=NOW,
            normalized_tag="#2PP",
        )
        with psycopg.connect(connection_info, autocommit=True) as connection:
            battle_id = connection.execute(
                "SELECT battle_id FROM battle_evidence ORDER BY id LIMIT 1"
            ).fetchone()[0]
            migration_key = (
                "redecode_army:army-decoder-v2:unit-catalog-v1:"
                f"{battle_id}:{battle_id}"
            )
            migration_job_id = connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, analytics_rule_version
                ) VALUES ('redecode_army', %s, %s::jsonb, 'army-analytics-v2')
                RETURNING id
                """,
                (migration_key, json.dumps({"battle_ids": [battle_id]})),
            ).fetchone()[0]
            lookalike_job_id = connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, analytics_rule_version
                ) VALUES ('redecode_army', %s, %s::jsonb, 'army-analytics-v2')
                RETURNING id
                """,
                (
                    f"{migration_key}-lookalike",
                    json.dumps({"battle_ids": [battle_id]}),
                ),
            ).fetchone()[0]

        root = Path(__file__).parents[2]
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                (root / "deploy/migrations/0012_parsed_content_dedup.sql").read_text()
            )
            migration_sql = (root / "deploy/migrations/0013_army_backfill_priority.sql").read_text()
            connection.execute(migration_sql)
            priorities_after_first = connection.execute(
                """
                SELECT id, priority FROM python_processing_jobs
                WHERE id IN (%s, %s, %s)
                """,
                (migration_job_id, lookalike_job_id, live_job_id),
            ).fetchall()
            connection.execute(migration_sql)
            priorities_after_second = connection.execute(
                """
                SELECT id, priority FROM python_processing_jobs
                WHERE id IN (%s, %s, %s)
                """,
                (migration_job_id, lookalike_job_id, live_job_id),
            ).fetchall()
        expected_priorities = {
            migration_job_id: 25,
            lookalike_job_id: 100,
            live_job_id: 100,
        }
        assert dict(priorities_after_first) == expected_priorities
        assert dict(priorities_after_second) == expected_priorities

        database, _processor_instance = _processor(connection_info, archive_server)
        try:
            claim = database.claim_job(owner="army-backfill-live-claim")
            assert claim is not None and claim.job_id == live_job_id
        finally:
            database.close()


def test_snapshot_manifest_uses_owning_ranking_version_observed_at(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _observation_id, ranking_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-manifest-ranking",
            endpoint="global_player_rankings",
            body=RANKING_FIXTURE.read_bytes(),
            observed_at=NOW,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(ranking_job, owner="dedup-manifest-ranking") is not None
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#22'"
                ).fetchone()[0]
                generation_id = connection.execute(
                    """
                    INSERT INTO boundary_publication_generations (
                        boundary_at, generation, ordering_rule_version,
                        freshness_rule_version, expected_population_count,
                        expected_population_hash, snapshot_state, army_state,
                        target_at
                    ) VALUES (%s, 1, 'ordering', 'freshness', 1, %s, 'pending', 'pending', %s)
                    RETURNING id
                    """,
                    (NOW, "a" * 64, NOW),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO boundary_publication_generation_members (
                        generation_id, player_id, status
                    ) VALUES (%s, %s, 'terminal')
                    """,
                    (generation_id, player_id),
                )
                manifest = database._freeze_boundary_manifest(
                    connection, generation_id=generation_id, artifact_kind="snapshot"
                )
                assert manifest is not None
                observed = connection.execute(
                    """
                    SELECT input_identity->>'official_rank_observed_at'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s AND player_id = %s
                    """,
                    (manifest[0], player_id),
                ).fetchone()[0]
            assert observed == NOW.isoformat()
        finally:
            database.close()


def test_worker_can_read_battle_observation_source_rows_view(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                """
                SELECT has_table_privilege(
                    'clashlens_python_worker',
                    'battle_log_observation_source_rows',
                    'SELECT'
                )
                """
            ).fetchone()[0]
            connection.execute("SET ROLE clashlens_python_worker")
            assert connection.execute(
                "SELECT count(*) FROM battle_log_observation_source_rows"
            ).fetchone()[0] == 0
            connection.execute("RESET ROLE")


def test_populated_partial_ranking_upgrade_backfills_payload_outcome_for_replay(
    database_url: str, archive_server
) -> None:
    ranking = json.loads(RANKING_FIXTURE.read_bytes())
    ranking["items"] = ranking["items"][:-1]
    body = json.dumps(ranking).encode()
    with _pre_dedup_database(database_url) as connection_info:
        observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="populated-v4-partial-ranking",
            endpoint="global_player_rankings",
            body=body,
            observed_at=NOW,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        assert processor.process_job(job_id, owner="populated-v4-partial") is not None
        database.close()
        migration = Path(__file__).parents[2] / "deploy/migrations/0012_parsed_content_dedup.sql"
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(migration.read_text(encoding="utf-8"))
            payload_outcome = connection.execute(
                """
                SELECT parse_outcome
                FROM parsed_source_payloads
                WHERE endpoint = 'global_player_rankings'
                """
            ).fetchone()[0]
            replay_job_id = _replay_job(
                connection, observation_id, "supercell-source-parser-v1"
            )
        replay_database, replay_processor = _processor(connection_info, archive_server)
        try:
            replay = replay_processor.process_job(
                replay_job_id, owner="populated-v4-partial-replay"
            )
            assert replay is not None and replay.outcome == "processed"
            with psycopg.connect(connection_info) as connection:
                replay_outcome = connection.execute(
                    """
                    SELECT outcome
                    FROM official_top200_attempts
                    WHERE observation_id = %s
                      AND parser_version = 'supercell-source-parser-v1'
                    """,
                    (observation_id,),
                ).fetchone()[0]
            assert text(replay_outcome) == "official_partial"
        finally:
            replay_database.close()
        assert text(payload_outcome) == "valid_with_gaps"


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


def test_duplicate_semantic_rows_prefer_current_pointer_without_timestamp_write(
    database_url: str, archive_server
) -> None:
    body = PROFILE_FIXTURE.read_bytes()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        observation_id, first_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-populated-first",
            endpoint="profile",
            body=body,
            observed_at=NOW,
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(first_job, owner="dedup-populated-first") is not None
            second_observation, second_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="dedup-populated-duplicate",
                endpoint="profile",
                body=body,
                observed_at=NOW + timedelta(minutes=1),
                normalized_tag="#2PP",
            )
            with psycopg.connect(connection_info) as connection:
                first_profile_id = connection.execute(
                    "SELECT id FROM player_profile_versions WHERE observation_id = %s",
                    (observation_id,),
                ).fetchone()[0]
                duplicate_id = connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag, endpoint_version,
                        schema_version, parser_version, observed_at, source_http_status,
                        name, trophies, league_tier_id, league_tier_name,
                        eligibility_state, current_league_season_id,
                        previous_league_season_id, eligibility_reason,
                        source_contract_state, season_anchor_state, profile_json,
                        parsed_payload_id, semantic_projection
                    )
                    SELECT player_id, %s, normalized_tag, endpoint_version,
                           schema_version, parser_version, %s, source_http_status,
                           name, trophies, league_tier_id, league_tier_name,
                           eligibility_state, current_league_season_id,
                           previous_league_season_id, eligibility_reason,
                           source_contract_state, season_anchor_state, profile_json,
                           parsed_payload_id, semantic_projection
                    FROM player_profile_versions
                    WHERE id = %s
                    RETURNING id
                    """,
                    (second_observation, NOW + timedelta(minutes=1), first_profile_id),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO player_profile_effects (
                        profile_version_id, observation_id, effect_kind,
                        parsed_payload_id, attempt_id, processing_outcome_id,
                        observed_at, source_http_status, endpoint_version,
                        schema_version, parser_version
                    )
                    SELECT %s, %s, 'current_profile', parsed_payload_id, NULL, NULL,
                           %s, source_http_status, endpoint_version,
                           schema_version, parser_version
                    FROM player_profile_versions WHERE id = %s
                    """,
                    (duplicate_id, second_observation, NOW + timedelta(minutes=1), duplicate_id),
                )
                connection.execute(
                    """
                    UPDATE players
                    SET current_profile_version_id = %s,
                        current_observed_at = %s, updated_at = clock_timestamp()
                    WHERE normalized_tag = '#2PP'
                    """,
                    (duplicate_id, NOW + timedelta(minutes=1)),
                )
                connection.commit()
                before = connection.execute(
                    "SELECT current_profile_version_id, updated_at FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()
            assert processor.process_job(second_job, owner="dedup-populated-second") is not None
            with psycopg.connect(connection_info) as connection:
                after = connection.execute(
                    "SELECT current_profile_version_id, current_observed_at, updated_at FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()
            assert after[0] == before[0] == duplicate_id
            assert after[1] == NOW + timedelta(minutes=1)
            assert after[2] == before[1]
        finally:
            database.close()


def test_equal_observed_profile_occurrences_keep_final_occurrence_tie_order(
    database_url: str, archive_server
) -> None:
    body_a = PROFILE_FIXTURE.read_bytes()
    body_b = json.loads(body_a)
    body_b["clan"]["name"] = "Tie-B"
    bodies = (body_a, json.dumps(body_b).encode(), body_a, body_a)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = []
        for index, body in enumerate(bodies):
            _observation_id, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"dedup-tie-{index}",
                endpoint="profile",
                body=body,
                observed_at=NOW,
                normalized_tag="#2PP",
            )
            jobs.append(job_id)
        database, processor = _processor(connection_info, archive_server)
        try:
            for index, job_id in enumerate(jobs):
                assert processor.process_job(job_id, owner=f"dedup-tie-{index}") is not None
                if index == 1:
                    with psycopg.connect(connection_info) as connection:
                        pointer_after_b, updated_after_b = connection.execute(
                            "SELECT current_profile_version_id, updated_at FROM players WHERE normalized_tag = '#2PP'"
                        ).fetchone()
                if index == 2:
                    with psycopg.connect(connection_info) as connection:
                        pointer_after_a, updated_after_a = connection.execute(
                            "SELECT current_profile_version_id, updated_at FROM players WHERE normalized_tag = '#2PP'"
                        ).fetchone()
            with psycopg.connect(connection_info) as connection:
                pointer_after_noop, updated_after_noop = connection.execute(
                    "SELECT current_profile_version_id, updated_at FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()
            assert pointer_after_a != pointer_after_b
            assert pointer_after_noop == pointer_after_a
            assert updated_after_a > updated_after_b
            assert updated_after_noop == updated_after_a
        finally:
            database.close()


def test_public_profile_uses_latest_occurrence_metadata_and_freshness(
    database_url: str, archive_server
) -> None:
    body = PROFILE_FIXTURE.read_bytes()
    first_at = NOW
    second_at = NOW + timedelta(hours=1)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_observation, first_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-api-v2",
            endpoint="profile",
            body=body,
            observed_at=first_at,
            normalized_tag="#2PP",
            parser_version="supercell-source-parser-v2",
            http_status=200,
        )
        second_observation, second_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="dedup-api-v1",
            endpoint="profile",
            body=body,
            observed_at=second_at,
            normalized_tag="#2PP",
            parser_version="supercell-source-parser-v1",
            http_status=201,
        )
        database, processor = _processor(connection_info, archive_server)
        api = ApiDatabase(connection_info)
        try:
            assert processor.process_job(first_job, owner="dedup-api-v2") is not None
            assert processor.process_job(second_job, owner="dedup-api-v1") is not None
            page = api.get_player_page(
                "#2PP", now=second_at + timedelta(minutes=1), freshness_seconds=900
            )
            assert page is not None
            assert page["source_http_status"] == 201
            assert page["endpoint_version"] == "profile-v1"
            assert page["schema_version"] == "profile-schema-v1"
            assert page["parser_version"] == "supercell-source-parser-v1"
            assert page["observed_at"] == second_at.isoformat()
            assert page["freshness"] == "fresh"
            assert first_observation != second_observation
        finally:
            api.close()
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
