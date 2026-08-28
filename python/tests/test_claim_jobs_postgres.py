from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import make_conninfo

from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

PARSER_VERSION = "supercell-source-parser-v1"
PROCESSING_VERSION = "clashlens-domain-processing-v1"
DOMAIN_RULE_VERSION = "clashlens-domain-rules-v1"
ANALYTICS_RULE_VERSION = "legend-analytics-v1"

CURRENT_ANALYTICS_INPUT = {
    "snapshot_id": 1,
    "snapshot_version": 1,
    "snapshot_input_hash": "a" * 64,
    "source_ranked_day_version_id": 1,
}


@contextmanager
def _production_database(
    database_url: str, *, include_army_migrations: bool = False
) -> Iterator[str]:
    schema = f"claim_jobs_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    connection_info = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            from pathlib import Path

            root = Path(__file__).parents[2]
            connection.execute(
                (root / "deploy/migrations/0001_collector.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (root / "deploy/migrations/0002_python_layer.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (root / "deploy/migrations/0003_regular_poll_dedup.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (root / "deploy/migrations/0004_source_parser_v2.sql").read_text(
                    encoding="utf-8"
                )
            )
            connection.execute(
                (root / "deploy/migrations/0005_army_decoding.sql").read_text(
                    encoding="utf-8"
                )
            )
            if include_army_migrations:
                for version in ("0006_provider_identities.sql", "0007_player_discovery.sql", "0008_public_army_analytics.sql"):
                    connection.execute(
                        (root / "deploy/migrations" / version).read_text(
                            encoding="utf-8"
                        )
                    )
        yield connection_info
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _insert_observation(connection: psycopg.Connection, *, occurrence_key: str) -> int:
    observed_at = datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC)
    player_id = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, next_due_at)
        VALUES ('#2PP', false, NULL)
        ON CONFLICT (normalized_tag) DO UPDATE
            SET active = EXCLUDED.active
        RETURNING id
        """
    ).fetchone()[0]
    collector_job_id = connection.execute(
        """
        INSERT INTO collector_jobs (
            work_type, player_id, normalized_tag, capacity_pool,
            priority, due_at, coalescing_key, status
        ) VALUES (
            'initial_collection', %s, '#2PP', 'interactive',
            300, %s, %s, 'complete'
        )
        RETURNING id
        """,
        (player_id, observed_at, occurrence_key),
    ).fetchone()[0]
    attempt_id = connection.execute(
        """
        INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
        VALUES (%s, 'complete', %s, %s)
        RETURNING id
        """,
        (collector_job_id, observed_at, observed_at),
    ).fetchone()[0]
    # Migration 0009 requires a verified catalogue row for every new
    # observation; fixtures on pre-0009 schemas keep the legacy shape.
    has_catalogue = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = 'archive_catalogue'
        )
        """
    ).fetchone()[0]
    if has_catalogue:
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
    digest = _hash(archive_reference := f"s3://evidence/{occurrence_key}")
    catalogue_columns = ""
    if has_catalogue:
        connection.execute(
            """
            INSERT INTO archive_catalogue (
                response_hash, archive_reference, byte_size, archive_instance_id
            ) VALUES (%s, %s, %s, 'fixture-instance')
            ON CONFLICT (response_hash) DO NOTHING
            """,
            (digest, archive_reference, 0),
        )
        catalogue_columns = ", archive_catalogue_hash"
    observation_id = connection.execute(
        f"""
        INSERT INTO collector_observations (
            occurrence_key, collection_job_id, attempt_id, player_id,
            normalized_tag, endpoint, request_started_at, response_completed_at,
            http_status, response_hash, archive_reference{catalogue_columns},
            collector_version, key_label, evidence_headers
        ) VALUES (
            %s, %s, %s, %s, '#2PP', 'profile', %s, %s, 200, %s, %s{", %s" if has_catalogue else ""},
            'collector-v1', 'normal-a', '{{}}'::jsonb
        )
        RETURNING id
        """,
        (
            occurrence_key,
            collector_job_id,
            attempt_id,
            player_id,
            observed_at,
            observed_at,
            digest,
            archive_reference,
            *([digest] if has_catalogue else []),
        ),
    ).fetchone()[0]
    return int(observation_id)


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_job(
    connection: psycopg.Connection,
    *,
    work_type: str,
    deduplication_key: str,
    input_json: dict,
    observation_id: int | None = None,
    replay_observation_id: int | None = None,
    priority: int = 100,
    due_at: str | None = None,
    parser_version: str = PARSER_VERSION,
    processing_version: str = PROCESSING_VERSION,
    domain_rule_version: str = DOMAIN_RULE_VERSION,
    analytics_rule_version: str = ANALYTICS_RULE_VERSION,
    max_attempts: int = 2,
) -> int:
    import json as json_module

    row = connection.execute(
        """
        INSERT INTO python_processing_jobs (
            work_type, observation_id, replay_observation_id, deduplication_key,
            input_json, priority, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version, max_attempts
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, %s, COALESCE(%s::timestamptz, clock_timestamp()),
            %s, %s, %s, %s, %s
        )
        RETURNING id
        """,
        (
            work_type,
            observation_id,
            replay_observation_id,
            deduplication_key,
            json_module.dumps(input_json),
            priority,
            due_at,
            parser_version,
            processing_version,
            domain_rule_version,
            analytics_rule_version,
            max_attempts,
        ),
    ).fetchone()
    connection.commit()
    return int(row[0])


def _processor(database: Database, archive_server) -> ObservationProcessor:
    return ObservationProcessor(
        database,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="fixture-access",
            secret_key="fixture-secret",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )


def test_queue_health_reports_an_empty_active_queue(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            assert database.queue_health() == {
                "pending": 0,
                "waiting_retry": 0,
                "waiting_dependency": 0,
                "leased": 0,
                "failed": 0,
                "failed_count_capped": False,
                "oldest_due_seconds": None,
            }
        finally:
            database.close()


@pytest.mark.parametrize(
    ("endpoint", "parser_version", "claimable"),
    [
        (endpoint, parser_version, claimable)
        for endpoint in ("profile", "battle_log", "global_player_rankings")
        for parser_version, claimable in (
            ("supercell-source-parser-v1", True),
            ("supercell-source-parser-v2", True),
            ("supercell-source-parser-v99", False),
        )
    ],
)
def test_claim_job_applies_each_endpoint_parser_contract(
    database_url: str,
    archive_server,
    endpoint: str,
    parser_version: str,
    claimable: bool,
) -> None:
    with domain_database(database_url) as connection_info:
        _observation_id, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key=f"claim-contract:{endpoint}:{parser_version}",
            endpoint=endpoint,
            body=b"{}",
            observed_at=datetime(2026, 8, 3, 19, 35, 1, tzinfo=UTC),
            normalized_tag=None if endpoint == "global_player_rankings" else "#2PP",
            parser_version=parser_version,
        )
        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="source-contract-test", job_id=job_id)

            assert (claim is not None) is claimable
            if claim is not None:
                assert claim.endpoint == endpoint
                assert claim.parser_version == parser_version
            else:
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "pending"
                )
        finally:
            database.close()


def test_replay_observation_claim_carries_source_metadata_and_processes(
    database_url: str, archive_server
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_id = _insert_observation(
                connection, occurrence_key="replay-source-observation"
            )
            connection.execute(
                """
                UPDATE collector_observations
                SET response_hash = %s, archive_reference = %s
                WHERE id = %s
                """,
                (archive_server[2], archive_server[1], observation_id),
            )
            connection.commit()
            job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:source-observation:v1",
                input_json={"replay_request_id": 1},
                replay_observation_id=observation_id,
            )

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="replay-claim-test", lease_seconds=30)
            assert claim is not None
            assert claim.job_id == job_id
            assert claim.work_type == "replay_observation"
            assert claim.observation_id == observation_id
            assert claim.normalized_tag == "#2PP"
            assert claim.endpoint == "profile"
            assert claim.endpoint_version == "profile-v1"
            assert claim.schema_version == "profile-schema-v1"
            assert claim.response_hash == archive_server[2]
            assert claim.archive_reference == archive_server[1]
            assert claim.observed_at is not None
            assert claim.parser_version == PARSER_VERSION

            # Release the inspected claim so the processor can pick the same
            # job back up through its public claim path.
            database.expire_lease(job_id)
            result = _processor(database, archive_server).process_job(
                job_id, owner="replay-claim-test"
            )
            assert result is not None
            assert result.outcome == "processed"
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "complete"
            )
            assert database.scalar("SELECT count(*) FROM player_profile_versions") == 1
        finally:
            database.close()


def test_unsupported_work_types_stay_pending_and_are_not_claimed(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            # Unknown future work types cannot be inserted under the v2 check
            # constraints; drop them in this isolated schema so the claim
            # filter can be proven to ignore such rows.
            connection.execute(
                "ALTER TABLE python_processing_jobs "
                "DROP CONSTRAINT IF EXISTS python_processing_jobs_work_type_v2_check"
            )
            connection.execute(
                "ALTER TABLE python_processing_jobs "
                "DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v2_check"
            )
            export_job_id = _insert_job(
                connection,
                work_type="build_export",
                deduplication_key="export:unsupported",
                input_json={"export_request_id": 7},
            )
            unknown_job_id = _insert_job(
                connection,
                work_type="future_work_type",
                deduplication_key="future:unsupported",
                input_json={},
            )
            observation_id = _insert_observation(
                connection, occurrence_key="supported-behind-unsupported"
            )
            supported_job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:supported-behind-unsupported",
                input_json={"replay_request_id": 2},
                replay_observation_id=observation_id,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-filter-test")
            assert claim is not None
            assert claim.job_id == supported_job_id
            for job_id in (export_job_id, unknown_job_id):
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "pending"
                )
        finally:
            database.close()


def test_direct_job_id_claim_is_subject_to_the_supported_contract_filter(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            export_job_id = _insert_job(
                connection,
                work_type="build_export",
                deduplication_key="export:direct",
                input_json={"export_request_id": 9},
            )
            observation_id = _insert_observation(
                connection, occurrence_key="direct-supported"
            )
            supported_job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:direct",
                input_json={"replay_request_id": 3},
                replay_observation_id=observation_id,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            denied = database.claim_job(
                owner="direct-unsupported", job_id=export_job_id
            )
            assert denied is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                == "pending"
            )
            allowed = database.claim_job(
                owner="direct-supported", job_id=supported_job_id
            )
            assert allowed is not None
            assert allowed.job_id == supported_job_id
        finally:
            database.close()


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("processing_version", "processing-v99"),
        ("domain_rule_version", "domain-rules-v99"),
        ("parser_version", "supercell-source-parser-v99"),
    ],
)
def test_claim_skips_observation_jobs_with_unsupported_versions(
    database_url: str,
    column: str,
    replacement: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_id = _insert_observation(
                connection, occurrence_key=f"unsupported-{column}"
            )
            job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key=f"unsupported:{column}",
                input_json={},
                observation_id=observation_id,
            )
            connection.execute(
                f"UPDATE python_processing_jobs SET {column} = %s WHERE id = %s",
                (replacement, job_id),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-version-test")
            assert claim is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_claim_skips_analytics_build_with_unsupported_analytics_rule_version(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:unsupported-version",
                input_json=CURRENT_ANALYTICS_INPUT,
                analytics_rule_version="analytics-v99",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-analytics-test")
            assert claim is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_claim_skips_reconciliation_with_unsupported_analytics_rule_version(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _insert_observation(
                connection, occurrence_key="reconcile-unsupported-analytics"
            )
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            job_id = _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:unsupported-analytics",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                analytics_rule_version="analytics-v99",
                due_at="2026-08-03T19:35:01+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="unsupported-reconcile-test")
            assert claim is None
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                )
                == "pending"
            )
        finally:
            database.close()


def test_reconcile_snapshot_and_analytics_jobs_with_current_versions_are_claimable(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            _insert_observation(connection, occurrence_key="claim-build-source")
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            _insert_job(
                connection,
                work_type="reconcile_ranked_day",
                deduplication_key="reconcile:claimable",
                input_json={
                    "player_id": player_id,
                    "ranked_day_start": "2026-08-03T05:00:00Z",
                },
                due_at="2026-08-03T19:35:01+00:00",
            )
            _insert_job(
                connection,
                work_type="build_snapshot",
                deduplication_key="snapshot:claimable",
                input_json={"boundary_at": "2026-08-03T05:00:00Z"},
                due_at="2026-08-03T19:35:02+00:00",
            )
            _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:claimable",
                input_json=CURRENT_ANALYTICS_INPUT,
                due_at="2026-08-03T19:35:03+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            first = database.claim_job(owner="claim-reconcile")
            assert first is not None and first.work_type == "reconcile_ranked_day"
            second = database.claim_job(owner="claim-snapshot")
            assert second is not None and second.work_type == "build_snapshot"
            third = database.claim_job(owner="claim-analytics")
            assert third is not None and third.work_type == "build_analytics"
        finally:
            database.close()


def test_legacy_analytics_job_with_relabeled_version_but_legacy_input_stays_pending(
    database_url: str,
) -> None:
    # The migration relabels legacy analytics jobs to the current analytics
    # rule version but leaves their legacy input_json shape untouched. Such
    # jobs must stay pending and unclaimed; only complete current-shape input
    # is claimable by this image.
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            selection_legacy_job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:legacy-selection",
                input_json={"selection": {"ranked_day_version_id": 1}},
                analytics_rule_version=ANALYTICS_RULE_VERSION,
                due_at="2026-08-03T19:35:01+00:00",
            )
            v1_legacy_job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:legacy-v1",
                input_json={"snapshot_id": 1},
                analytics_rule_version=ANALYTICS_RULE_VERSION,
                due_at="2026-08-03T19:35:02+00:00",
            )
            current_job_id = _insert_job(
                connection,
                work_type="build_analytics",
                deduplication_key="analytics:current-shape",
                input_json=CURRENT_ANALYTICS_INPUT,
                analytics_rule_version=ANALYTICS_RULE_VERSION,
                due_at="2026-08-03T19:35:03+00:00",
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="legacy-analytics-test")
            assert claim is not None
            assert claim.job_id == current_job_id
            for job_id in (selection_legacy_job_id, v1_legacy_job_id):
                assert (
                    database.scalar(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (job_id,),
                    )
                    == "pending"
                )
        finally:
            database.close()


def test_explicit_live_class_outranks_aged_army_backfill(
    database_url: str,
) -> None:
    with _production_database(
        database_url, include_army_migrations=True
    ) as connection_info:
        with psycopg.connect(connection_info) as connection:
            backfill_job_id = _insert_job(
                connection,
                work_type="redecode_army",
                deduplication_key="historical-army-backfill",
                input_json={"battle_ids": [1]},
                priority=25,
                analytics_rule_version="army-analytics-v2",
            )
            connection.execute(
                "UPDATE python_processing_jobs "
                "SET created_at = clock_timestamp() - interval '2 hours' "
                "WHERE id = %s",
                (backfill_job_id,),
            )
            live_observation_id = _insert_observation(
                connection, occurrence_key="live-before-army-backfill"
            )
            live_job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="live-before-army-backfill",
                input_json={},
                observation_id=live_observation_id,
                priority=100,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            live_claim = database.claim_job(owner="live-before-backfill")
            assert live_claim is not None and live_claim.job_id == live_job_id
            assert database.scalar(
                "SELECT status FROM python_processing_jobs WHERE id = %s",
                (backfill_job_id,),
            ) == "pending"
        finally:
            database.close()


def test_low_priority_job_eventually_outranks_a_fresh_high_priority_job(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            old_observation_id = _insert_observation(
                connection, occurrence_key="priority-old"
            )
            fresh_observation_id = _insert_observation(
                connection, occurrence_key="priority-fresh"
            )
            old_job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="priority:old",
                input_json={},
                observation_id=old_observation_id,
                priority=100,
                due_at="2026-08-03T19:35:01+00:00",
            )
            fresh_job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="priority:fresh",
                input_json={},
                observation_id=fresh_observation_id,
                priority=300,
            )
            connection.execute(
                "UPDATE python_processing_jobs "
                "SET created_at = clock_timestamp() - interval '2 hours' "
                "WHERE id = %s",
                (old_job_id,),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            # An old low-priority job outranks a fresh high-priority job.
            first = database.claim_job(owner="priority-old-first")
            assert first is not None
            assert first.job_id == old_job_id
            # A fresh high-priority job still outranks any remaining fresh job.
            second = database.claim_job(owner="priority-fresh-second")
            assert second is not None
            assert second.job_id == fresh_job_id
        finally:
            database.close()


def test_claim_order_tie_breaks_by_due_at_then_id(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_ids = [
                _insert_observation(connection, occurrence_key=f"tie-{label}")
                for label in ("a", "b", "c")
            ]
            later_due = "2026-08-03T19:35:02+00:00"
            earlier_due = "2026-08-03T19:35:01+00:00"
            job_ids = [
                _insert_job(
                    connection,
                    work_type="process_observation",
                    deduplication_key=f"tie:{label}",
                    input_json={},
                    observation_id=observation_id,
                    due_at=due_at,
                )
                for label, observation_id, due_at in (
                    ("a", observation_ids[0], earlier_due),
                    ("b", observation_ids[1], later_due),
                    ("c", observation_ids[2], later_due),
                )
            ]
            # Equal creation time so the priority + age score ties.
            connection.execute(
                "UPDATE python_processing_jobs "
                "SET created_at = clock_timestamp() "
                "WHERE id = ANY(%s::bigint[])",
                (job_ids,),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            first = database.claim_job(owner="tie-one")
            assert first is not None and first.job_id == job_ids[0]
            second = database.claim_job(owner="tie-two")
            assert second is not None and second.job_id == job_ids[1]
            third = database.claim_job(owner="tie-three")
            assert third is not None and third.job_id == job_ids[2]
        finally:
            database.close()


def test_claim_does_not_sweep_expired_unsupported_lease_but_maintenance_does(
    database_url: str,
) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            export_job_id = _insert_job(
                connection,
                work_type="build_export",
                deduplication_key="export:expired-lease",
                input_json={"export_request_id": 11},
                max_attempts=2,
            )
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET status = 'leased', lease_owner = 'retired-worker',
                    lease_token = 'retired-token',
                    lease_expires_at = clock_timestamp() - interval '1 minute',
                    attempt_count = 2, updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (export_job_id,),
            )
            connection.execute(
                """
                INSERT INTO python_processing_attempts (
                    job_id, attempt_number, lease_owner, lease_token,
                    started_at, lease_expires_at, state
                ) VALUES (
                    %s, 1, 'retired-worker', 'retired-token',
                    clock_timestamp() - interval '2 minutes',
                    clock_timestamp() - interval '1 minute', 'running'
                )
                """,
                (export_job_id,),
            )
            observation_id = _insert_observation(
                connection, occurrence_key="cleanup-supported"
            )
            supported_job_id = _insert_job(
                connection,
                work_type="replay_observation",
                deduplication_key="replay:cleanup",
                input_json={"replay_request_id": 4},
                replay_observation_id=observation_id,
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="cleanup-test")
            assert claim is not None
            assert claim.job_id == supported_job_id
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                == "leased"
            )
            assert database.maintain_queue(max_jobs=100) == 1
            assert (
                database.scalar(
                    "SELECT status FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                == "pending"
            )
            assert (
                database.scalar(
                    "SELECT failure_category FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                is None
            )
            assert (
                database.scalar(
                    "SELECT lease_owner FROM python_processing_jobs WHERE id = %s",
                    (export_job_id,),
                )
                is None
            )
            assert (
                database.scalar(
                    "SELECT state FROM python_processing_attempts WHERE job_id = %s",
                    (export_job_id,),
                )
                == "stale"
            )
        finally:
            database.close()
