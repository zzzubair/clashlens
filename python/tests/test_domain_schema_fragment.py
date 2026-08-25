from __future__ import annotations

import psycopg
from domain_test_support import domain_database, text


def test_python_production_migration_adds_required_versioned_domain_relations(
    database_url: str,
) -> None:
    expected = {
        "observation_processing_outcomes",
        "parsed_source_payloads",
        "season_anchor_evidence",
        "known_player_discoveries",
        "battle_log_observations",
        "battle_source_rows",
        "legend_battles",
        "battle_evidence",
        "battle_perspectives",
        "reset_baseline_evidence",
        "ranked_day_versions",
        "ranked_day_adjustments",
        "leaderboard_snapshots",
        "leaderboard_snapshot_entries",
        "official_top200_attempts",
        "official_top200_versions",
        "official_top200_entries",
        "analytics_summaries",
        "analytics_breakdowns",
        "python_replay_requests",
    }

    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                """
            ).fetchall()

    assert expected <= {text(row[0]) for row in rows}


def test_python_production_migration_supports_global_observations_without_fake_player(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            collector_job_id = connection.execute(
                """
                INSERT INTO collector_jobs (
                    work_type, player_id, normalized_tag, capacity_pool, priority,
                    due_at, coalescing_key, status, scope, required_endpoint
                ) VALUES (
                    'global_player_rankings', NULL, NULL, 'normal', 500,
                    clock_timestamp(), 'global-ranking-test', 'complete', 'global',
                    'global_player_rankings'
                )
                RETURNING id
                """
            ).fetchone()[0]
            attempt_id = connection.execute(
                """
                INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
                VALUES (%s, 'complete', clock_timestamp(), clock_timestamp())
                RETURNING id
                """,
                (collector_job_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO archive_instances (
                    instance_id, endpoint, region, bucket, marker_key,
                    marker_hash, marker_payload_version
                ) VALUES ('fixture-instance', 'archive.test:443', 'us-east-1',
                          'evidence', 'clashlens/archive-instance.json',
                          repeat('f', 64), 'v1')
                ON CONFLICT (instance_id) DO NOTHING
                """
            )
            connection.execute(
                """
                INSERT INTO archive_catalogue (
                    response_hash, archive_reference, byte_size, archive_instance_id
                ) VALUES (repeat('a', 64), 's3://evidence/test', 0, 'fixture-instance')
                ON CONFLICT (response_hash) DO NOTHING
                """
            )
            observation_id = connection.execute(
                """
                INSERT INTO collector_observations (
                    occurrence_key, collection_job_id, attempt_id, player_id,
                    scope, normalized_tag, endpoint, request_started_at, response_completed_at,
                    http_status, response_hash, archive_reference, archive_catalogue_hash,
                    collector_version, key_label, evidence_headers
                ) VALUES (
                    'global-ranking-test', %s, %s, NULL, 'global', NULL,
                    'global_player_rankings', clock_timestamp(), clock_timestamp(),
                    200, repeat('a', 64), 's3://evidence/test', repeat('a', 64),
                    'collector-v2', 'normal-a', '{}'::jsonb
                )
                RETURNING id
                """,
                (collector_job_id, attempt_id),
            ).fetchone()[0]
            job = connection.execute(
                """
                INSERT INTO python_processing_jobs (observation_id)
                VALUES (%s)
                RETURNING work_type, deduplication_key, parser_version,
                          processing_version, domain_rule_version,
                          claim_compatibility_version
                """,
                (observation_id,),
            ).fetchone()

    assert tuple(text(value) for value in job) == (
        "process_observation",
        f"process-observation:{observation_id}",
        "supercell-source-parser-v2",
        "clashlens-domain-processing-v1",
        "clashlens-domain-rules-v1",
        2,
    )
