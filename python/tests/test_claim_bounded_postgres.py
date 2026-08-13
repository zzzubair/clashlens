from __future__ import annotations

import re

import psycopg
from test_api_migration import migrated_production_database
from test_claim_jobs_postgres import (
    _insert_job,
    _insert_observation,
    _production_database,
)

import clashlens.db as db_module
from clashlens.db import (
    _CLAIM_CANDIDATE_LIMIT,
    Database,
    _claim_select_statement,
    _supported_job_filter,
)
from clashlens.worker import MAX_CONCURRENCY

EXECUTION_TIME_PATTERN = re.compile(r"Execution Time: ([0-9.]+) ms")


def _database_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii")
    return value


def _seed_production_depth(connection: psycopg.Connection) -> None:
    """Fill an adversarial production-depth queue over 116,460 observations.

    It combines 19,220 supported due jobs with 12,500 old future-schema jobs,
    12,500 older future-due jobs, and 12,500 expired leases. The plan must find
    eligible work through compatibility/due/expiry indexes without scanning
    or sorting any adversarial prefix wholesale.
    """
    connection.execute(
        """
        INSERT INTO players (normalized_tag, active)
        SELECT '#' || lpad(i::text, 8, '0'), true
        FROM generate_series(1, 20000) AS i
        """
    )
    connection.execute(
        """
        INSERT INTO collector_jobs (
            work_type, player_id, normalized_tag, capacity_pool, priority,
            due_at, coalescing_key, status, created_at
        )
        SELECT 'initial_collection', player.id, player.normalized_tag,
               'interactive', 300,
               clock_timestamp() - ((i % 120) || ' minutes')::interval,
               'seed-observe:' || i, 'complete',
               clock_timestamp() - ((i % 480) || ' minutes')::interval
        FROM generate_series(1, 116460) AS i
        JOIN players AS player ON player.id = ((i - 1) % 20000) + 1
        """
    )
    connection.execute(
        """
        INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
        SELECT job.id, 'complete', clock_timestamp() - interval '1 minute',
               clock_timestamp()
        FROM collector_jobs AS job
        WHERE job.coalescing_key LIKE 'seed-observe:%'
        """
    )
    connection.execute(
        """
        INSERT INTO collector_observations (
            occurrence_key, collection_job_id, attempt_id, player_id,
            normalized_tag, endpoint, request_started_at, response_completed_at,
            http_status, response_hash, archive_reference, collector_version,
            key_label, evidence_headers
        )
        SELECT 'seed-observation:' || job.id, job.id, attempt.id, job.player_id,
               job.normalized_tag, 'profile',
               clock_timestamp() - interval '1 minute', clock_timestamp(), 200,
               lpad(to_hex(job.id), 64, '0'), 's3://evidence/seed-' || job.id,
               'collector-v1', 'normal-a', '{}'::jsonb
        FROM collector_jobs AS job
        JOIN collector_attempts AS attempt ON attempt.job_id = job.id
        """
    )
    connection.execute(
        """
        INSERT INTO python_processing_jobs (
            observation_id, work_type, status, due_at, priority, created_at,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version
        )
        SELECT observation.id, 'process_observation', 'pending',
               clock_timestamp() - ((i % 30) || ' minutes')::interval, 100,
               clock_timestamp() - ((i % 600) || ' minutes')::interval,
               'supercell-source-parser-v1', 'clashlens-domain-processing-v1',
               'clashlens-domain-rules-v1', 'legend-analytics-v1'
        FROM generate_series(1, 19220) AS i
        JOIN collector_observations AS observation ON observation.id = i
        """
    )
    connection.execute(
        """
        INSERT INTO python_processing_jobs (
            observation_id, work_type, status, due_at, priority, created_at,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version
        )
        SELECT observation.id, 'process_observation', 'pending',
               clock_timestamp() - interval '1 hour', 100,
               clock_timestamp() - interval '60 days',
               'supercell-source-parser-v1', 'clashlens-domain-processing-v1',
               'clashlens-domain-rules-v1', 'legend-analytics-v1'
        FROM generate_series(19221, 31720) AS i
        JOIN collector_observations AS observation ON observation.id = i
        """
    )
    connection.execute(
        """
        INSERT INTO python_processing_jobs (
            observation_id, work_type, status, due_at, priority, created_at,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version
        )
        SELECT observation.id, 'process_observation', 'pending',
               clock_timestamp() + interval '1 day', 100,
               clock_timestamp() - interval '90 days',
               'supercell-source-parser-v1', 'clashlens-domain-processing-v1',
               'clashlens-domain-rules-v1', 'legend-analytics-v1'
        FROM generate_series(31721, 44220) AS i
        JOIN collector_observations AS observation ON observation.id = i
        """
    )
    # Model a forward migration that admits a future source schema, then make
    # its existing jobs recalculate the denormalized planner marker. The
    # authoritative worker predicate rejects the schema; migration 0003's
    # trigger must also keep this entire older-due prefix out of the partial
    # claim index.
    connection.execute(
        """
        ALTER TABLE collector_observations
            ALTER COLUMN schema_version DROP EXPRESSION;
        UPDATE collector_observations
        SET schema_version = 'profile-schema-v99'
        WHERE id BETWEEN 19221 AND 31720;
        UPDATE python_processing_jobs
        SET parser_version = parser_version
        WHERE observation_id BETWEEN 19221 AND 31720
        """
    )
    connection.execute(
        """
        INSERT INTO python_processing_jobs (
            observation_id, work_type, status, due_at, priority, created_at,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version, lease_owner, lease_token, lease_expires_at,
            attempt_count, max_attempts
        )
        SELECT observation.id, 'process_observation', 'leased',
               clock_timestamp() - interval '1 day', 100,
               clock_timestamp() - interval '120 days',
               'supercell-source-parser-v1', 'clashlens-domain-processing-v1',
               'clashlens-domain-rules-v1', 'legend-analytics-v1',
               'retired-worker', 'expired-' || i,
               clock_timestamp() - interval '1 day', 5, 5
        FROM generate_series(44221, 56720) AS i
        JOIN collector_observations AS observation ON observation.id = i
        """
    )
    connection.commit()
    connection.execute("ANALYZE collector_observations")
    connection.execute("ANALYZE python_processing_jobs")
    connection.commit()


def _explain_claim(
    connection: psycopg.Connection,
) -> tuple[str, float]:
    """EXPLAIN ANALYZE the exact claim statement and return (plan, millis)."""
    statement, params = _claim_select_statement("python_processing_jobs_worker")
    plan = connection.execute(
        f"EXPLAIN (ANALYZE, COSTS OFF) {statement}", params
    ).fetchall()
    plan_text = "\n".join(_database_text(row[0]) for row in plan)
    match = EXECUTION_TIME_PATTERN.search(plan_text)
    assert match is not None, f"claim plan has no execution time:\n{plan_text}"
    return plan_text, float(match.group(1))


def test_claim_statement_uses_postgresql_statement_clock() -> None:
    statement, params = _claim_select_statement("python_processing_jobs_worker")

    assert "statement_timestamp()" in statement
    assert "%(now)s" not in statement
    assert "now" not in params


def test_claim_candidate_window_covers_maximum_parallel_lanes() -> None:
    assert _CLAIM_CANDIDATE_LIMIT == MAX_CONCURRENCY


def test_claim_plan_at_production_depth_is_bounded(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            _seed_production_depth(connection)
            future_contracts = connection.execute(
                """
                SELECT count(*)
                FROM python_processing_jobs AS job
                JOIN collector_observations AS observation
                  ON observation.id = job.observation_id
                WHERE observation.schema_version = 'profile-schema-v99'
                  AND job.claim_compatibility_version = 0
                """
            ).fetchone()[0]
            assert future_contracts == 12500
            plan_text, millis = _explain_claim(connection)
            assert "Seq Scan on python_processing_jobs" not in plan_text, (
                f"claim plan scans the whole queue:\n{plan_text}"
            )
            assert "Seq Scan on collector_observations" not in plan_text, (
                f"claim plan scans all observations:\n{plan_text}"
            )
            assert "python_processing_jobs_pending_claim_v2" in plan_text, (
                f"claim plan does not use the indexed pending probe:\n{plan_text}"
            )
            assert "python_processing_jobs_expired_leases_v2" in plan_text, (
                f"claim plan does not use the indexed expiry probe:\n{plan_text}"
            )
            assert millis < 100, (
                f"claim took {millis:.1f} ms at production depth, want < 100 ms"
            )

            supported_filter, supported_params = _supported_job_filter("job")
            maintenance_plan = connection.execute(
                f"""
                EXPLAIN (ANALYZE, COSTS OFF)
                SELECT job.id, ({supported_filter}) AS supported,
                       job.attempt_count >= job.max_attempts AS exhausted
                FROM python_processing_jobs_worker AS job
                WHERE job.state = 'leased'
                  AND job.lease_expires_at <= clock_timestamp()
                ORDER BY job.lease_expires_at, job.id
                LIMIT 100
                """,
                supported_params,
            ).fetchall()
            maintenance_plan_text = "\n".join(
                _database_text(row[0]) for row in maintenance_plan
            )
            assert "Seq Scan on python_processing_jobs" not in maintenance_plan_text
            assert (
                "python_processing_jobs_expired_maintenance_v2"
                in maintenance_plan_text
            ), maintenance_plan_text
            maintenance_match = EXECUTION_TIME_PATTERN.search(maintenance_plan_text)
            assert maintenance_match is not None
            assert float(maintenance_match.group(1)) < 100

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="plan-depth-worker")
            assert claim is not None, "claim at production depth returned no job"
        finally:
            database.close()


def test_claim_probe_skips_unsupported_head_and_keeps_age_fairness(
    database_url: str,
) -> None:
    # Unsupported jobs older than every supported job must not starve the
    # supported queue: the per-priority probe applies the full supported
    # filter, so the oldest supported job is found behind the unsupported
    # head. That old supported priority-100 job must also outrank a fresh
    # unknown-priority job (unbounded age fairness).
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            for index in range(300):
                observation_id = _insert_observation(
                    connection, occurrence_key=f"unsupported-head-{index}"
                )
                _insert_job(
                    connection,
                    work_type="process_observation",
                    deduplication_key=f"unsupported-head:{index}",
                    input_json={},
                    observation_id=observation_id,
                    parser_version="supercell-source-parser-v99",
                    priority=100,
                )
            old_supported_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="supported-behind-head",
                input_json={},
                observation_id=_insert_observation(
                    connection, occurrence_key="supported-behind-head"
                ),
                priority=100,
            )
            _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="fresh-unknown-priority",
                input_json={},
                observation_id=_insert_observation(
                    connection, occurrence_key="fresh-unknown-priority"
                ),
                priority=250,
            )
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET created_at = clock_timestamp() - interval '2 hours'
                WHERE id = %s
                """,
                (old_supported_id,),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="starvation-guard")
            assert claim is not None
            assert claim.job_id == old_supported_id, (
                f"claimed job {claim.job_id}, want the old supported job "
                f"{old_supported_id} behind the unsupported head"
            )
        finally:
            database.close()


def test_unknown_priority_jobs_are_claimable(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            aged_unknown_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="unknown:aged",
                input_json={},
                observation_id=_insert_observation(
                    connection, occurrence_key="unknown-aged"
                ),
                priority=250,
            )
            fresh_known_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="unknown:fresh-known",
                input_json={},
                observation_id=_insert_observation(
                    connection, occurrence_key="unknown-fresh-known"
                ),
                priority=100,
            )
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET created_at = clock_timestamp() - interval '30 minutes'
                WHERE id = %s
                """,
                (aged_unknown_id,),
            )
            connection.commit()

        database = Database(connection_info)
        try:
            first = database.claim_job(owner="unknown-first")
            assert first is not None and first.job_id == aged_unknown_id, (
                "aged unknown-priority job must outrank a fresh known-priority job"
            )
            second = database.claim_job(owner="unknown-second")
            assert second is not None and second.job_id == fresh_known_id
        finally:
            database.close()


def test_expired_lease_probe_claims_expired_lease_at_depth(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            expired_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="expired:probe",
                input_json={},
                observation_id=_insert_observation(
                    connection, occurrence_key="expired-probe"
                ),
            )
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET status = 'leased', lease_owner = 'retired-lane',
                    lease_token = 'retired-token',
                    lease_expires_at = clock_timestamp() - interval '1 minute',
                    attempt_count = 1, updated_at = clock_timestamp(),
                    created_at = clock_timestamp() - interval '3 hours'
                WHERE id = %s
                """,
                (expired_id,),
            )
            connection.execute(
                """
                INSERT INTO python_processing_attempts (
                    job_id, attempt_number, lease_owner, lease_token,
                    started_at, lease_expires_at, state
                ) VALUES (
                    %s, 1, 'retired-lane', 'retired-token',
                    clock_timestamp() - interval '4 hours',
                    clock_timestamp() - interval '1 minute', 'running'
                )
                """,
                (expired_id,),
            )
            for index in range(200):
                _insert_job(
                    connection,
                    work_type="process_observation",
                    deduplication_key=f"expired:fresh-{index}",
                    input_json={},
                    observation_id=_insert_observation(
                        connection, occurrence_key=f"expired-fresh-{index}"
                    ),
                )
            connection.commit()

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="expired-recovery")
            assert claim is not None
            assert claim.job_id == expired_id, (
                "the 3-hour-old expired lease must outrank 200 fresh pending jobs"
            )
            assert (
                database.scalar(
                    "SELECT lease_owner FROM python_processing_jobs WHERE id = %s",
                    (expired_id,),
                )
                == "expired-recovery"
            )
        finally:
            database.close()


def test_forward_migration_reapply_keeps_python_claim_indexes(database_url: str) -> None:
    with _production_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            observation_id = _insert_observation(
                connection, occurrence_key="reapply-keeps-jobs"
            )
            _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="reapply:keeps",
                input_json={},
                observation_id=observation_id,
            )
            from pathlib import Path

            root = Path(__file__).parents[2]
            connection.execute(
                (root / "deploy/migrations/0003_regular_poll_dedup.sql").read_text(
                    encoding="utf-8"
                )
            )
            indexes = {
                _database_text(row[0])
                for row in connection.execute(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE tablename = 'python_processing_jobs'
                      AND indexname IN (
                          'python_processing_jobs_pending_claim_v2',
                          'python_processing_jobs_unknown_priority_v2',
                          'python_processing_jobs_expired_leases_v2',
                          'python_processing_jobs_expired_maintenance_v2'
                      )
                    """
                )
            }
            assert indexes == {
                "python_processing_jobs_pending_claim_v2",
                "python_processing_jobs_unknown_priority_v2",
                "python_processing_jobs_expired_leases_v2",
                "python_processing_jobs_expired_maintenance_v2",
            }, f"claim indexes after 0003 reapply = {indexes}"
            marker = connection.execute(
                """
                SELECT claim_compatibility_version
                FROM python_processing_jobs
                WHERE deduplication_key = 'reapply:keeps'
                """
            ).fetchone()[0]
            assert marker == 1, "0003 reapply must preserve claimable work"
            assert (
                connection.execute(
                    "SELECT count(*) FROM python_processing_jobs"
                ).fetchone()[0]
                == 1
            ), "0002 reapply must be non-destructive"


def test_forward_migration_classifies_populated_v2_backlog(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_migration_0003=False
    ) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            observation_id = _insert_observation(
                connection, occurrence_key="pre-0003-supported-job"
            )
            job_id = _insert_job(
                connection,
                work_type="process_observation",
                deduplication_key="pre-0003:supported",
                input_json={},
                observation_id=observation_id,
            )
            from pathlib import Path

            migration = (
                Path(__file__).parents[2]
                / "deploy/migrations/0003_regular_poll_dedup.sql"
            ).read_text(encoding="utf-8")
            connection.execute(migration)
            assert connection.execute(
                """
                SELECT claim_compatibility_version
                FROM python_processing_jobs WHERE id = %s
                """,
                (job_id,),
            ).fetchone()[0] == 1
            connection.execute(migration)
            assert connection.execute(
                """
                SELECT claim_compatibility_version
                FROM python_processing_jobs WHERE id = %s
                """,
                (job_id,),
            ).fetchone()[0] == 1

        database = Database(connection_info)
        try:
            claim = database.claim_job(owner="populated-v2-migration")
            assert claim is not None and claim.job_id == job_id
        finally:
            database.close()


def test_declared_claim_priorities_match_enqueue_sites() -> None:
    declared = {
        int(raw.strip(" ()")) for raw in db_module._PYTHON_CLAIM_PRIORITIES.split(",")
    }
    assert declared == {100}, (
        "declared Python claim priorities must match every enqueue site "
        "(db.py, api_db.py, and the Go collector handoff all use the "
        "migration default priority 100)"
    )
