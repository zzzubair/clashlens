from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from clashlens_prototype.archive import S3ArchiveReader
from clashlens_prototype.db import Database
from clashlens_prototype.worker import ObservationProcessor
from domain_test_support import domain_database, store_observation, text

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


def _profile(trophies: int) -> bytes:
    payload = json.loads(PROFILE_FIXTURE.read_bytes())
    payload["trophies"] = trophies
    return json.dumps(payload).encode()


def _battle_log(*, empty: bool = False) -> bytes:
    payload = json.loads(BATTLE_FIXTURE.read_bytes())
    payload["items"] = [] if empty else payload["items"][:1]
    return json.dumps(payload).encode()


def _processor(connection_info: str, archive_server) -> tuple[Database, ObservationProcessor]:
    database = Database(connection_info)
    return database, ObservationProcessor(
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


def _seed_reset_collection_identity(
    connection_info: str,
    *,
    key: str,
    boundary: datetime,
    profile_observation_id: int,
    battle_observation_id: int,
) -> None:
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        baseline = connection.execute(
            """
            SELECT id, reset_sweep_id
            FROM collector_reset_baseline_sweeps
            WHERE player_id = %s AND boundary_at = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (player_id, boundary),
        ).fetchone()
        if baseline is None:
            reset_sweep_id = connection.execute(
                """
                INSERT INTO collector_reset_sweeps (boundary_at)
                VALUES (%s)
                RETURNING id
                """,
                (boundary,),
            ).fetchone()[0]
            baseline_sweep_id = connection.execute(
                """
                INSERT INTO collector_reset_baseline_sweeps (
                    reset_sweep_id, player_id, boundary_at, evidence_kind, state
                ) VALUES (%s, %s, %s, 'paired_v2', 'pending')
                RETURNING id
                """,
                (reset_sweep_id, player_id, boundary),
            ).fetchone()[0]
        else:
            baseline_sweep_id, reset_sweep_id = int(baseline[0]), int(baseline[1])
        root_job_id = connection.execute(
            """
            INSERT INTO collector_jobs (
                work_type, scope, player_id, normalized_tag, capacity_pool,
                priority, due_at, coalescing_key, sweep_id,
                reset_baseline_sweep_id, status
            ) VALUES (
                'reset_baseline', 'player', %s, '#2PP', 'normal', 400, %s,
                %s, %s, %s, 'complete'
            )
            RETURNING id
            """,
            (player_id, boundary, f"reset-baseline-{key}", reset_sweep_id, baseline_sweep_id),
        ).fetchone()[0]
        root_attempt_id = connection.execute(
            """
            INSERT INTO collector_attempts (
                job_id, status, started_at, completed_at
            ) VALUES (%s, 'complete', %s, %s)
            RETURNING id
            """,
            (root_job_id, boundary, boundary),
        ).fetchone()[0]
        connection.execute(
            "UPDATE collector_jobs SET result_attempt_id = %s WHERE id = %s",
            (root_attempt_id, root_job_id),
        )
        connection.execute(
            """
            UPDATE collector_observations
            SET collection_job_id = %s, attempt_id = %s
            WHERE id IN (%s, %s)
            """,
            (root_job_id, root_attempt_id, profile_observation_id, battle_observation_id),
        )
        for endpoint, observation_id in (
            ("profile", profile_observation_id),
            ("battle_log", battle_observation_id),
        ):
            source = connection.execute(
                """
                SELECT request_started_at, response_completed_at, http_status,
                       response_hash, archive_reference
                FROM collector_observations
                WHERE id = %s
                """,
                (observation_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO collector_endpoint_results (
                    attempt_id, endpoint, outcome, request_started_at,
                    response_completed_at, http_status, response_hash,
                    archive_reference, observation_id, request_count,
                    key_label
                ) VALUES (%s, %s, 'observed', %s, %s, %s, %s, %s, %s, 1, 'normal-a')
                """,
                (root_attempt_id, endpoint, *source, observation_id),
            )
        connection.commit()


def _store_baseline_pair(
    connection_info: str,
    archive_server,
    *,
    key: str,
    boundary: datetime,
    trophies: int,
    empty_battle_log: bool,
) -> tuple[int, int, int, int]:
    profile_observation, profile_job = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-profile",
        endpoint="profile",
        body=_profile(trophies),
        observed_at=boundary,
        normalized_tag="#2PP",
    )
    battle_observation, battle_job = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-battle",
        endpoint="battle_log",
        body=_battle_log(empty=empty_battle_log),
        observed_at=boundary,
        normalized_tag="#2PP",
    )
    _seed_reset_collection_identity(
        connection_info,
        key=key,
        boundary=boundary,
        profile_observation_id=profile_observation,
        battle_observation_id=battle_observation,
    )
    return profile_observation, battle_observation, profile_job, battle_job


def test_durable_reconciliation_versions_late_corrections_without_rewriting_history(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url) as connection_info:
        start_profile, start_battle, start_profile_job, start_battle_job = (
            _store_baseline_pair(
                connection_info,
                archive_server,
                key="start",
                boundary=DAY_START,
                trophies=6000,
                empty_battle_log=True,
            )
        )
        _middle_observation, middle_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="middle-battle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )
        end_profile, end_battle, end_profile_job, end_battle_job = (
            _store_baseline_pair(
                connection_info,
                archive_server,
                key="end",
                boundary=DAY_END,
                trophies=6040,
                empty_battle_log=False,
            )
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            results = []
            for job_id in (
                start_profile_job,
                start_battle_job,
                middle_job,
                end_profile_job,
                end_battle_job,
            ):
                result = processor.process_job(job_id, owner=f"source-{job_id}")
                assert result is not None and result.outcome == "processed"
                results.append(result)
            assert len(results) == 5

            ranked_day_start_text = DAY_START.strftime("%Y-%m-%dT%H:%M:%SZ")
            with database.pool.connection() as connection:
                first_job_row = connection.execute(
                    """
                    SELECT id
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                      AND input_json->>'ranked_day_start' = %s
                    ORDER BY id
                    LIMIT 1
                    """,
                    (ranked_day_start_text,),
                ).fetchone()
            assert first_job_row is not None
            first_job = int(first_job_row[0])
            first = processor.process_job(first_job, owner="reconcile-first")
            assert first is not None and first.outcome == "processed"
            with database.pool.connection() as connection:
                first_dependent_jobs = connection.execute(
                    """
                    SELECT id, work_type, deduplication_key, input_json
                    FROM python_processing_jobs
                    WHERE input_json->>'ranked_day_version_id' = (
                        SELECT id::text FROM ranked_day_versions WHERE version = 1
                    )
                    ORDER BY id
                    """
                ).fetchall()
            assert [
                (text(row[1]), text(row[2])) for row in first_dependent_jobs
            ] == [
                ("build_snapshot", "build_snapshot:ranked-day-version:1"),
                ("build_analytics", "build_analytics:ranked-day-version:1"),
            ]
            assert [row[3] for row in first_dependent_jobs] == [
                {
                    "boundary_at": "2026-08-05T05:00:00Z",
                    "player_id": 1,
                    "ranked_day_start": "2026-08-04T05:00:00Z",
                    "ranked_day_version_id": 1,
                },
                {
                    "player_id": 1,
                    "ranked_day_start": "2026-08-04T05:00:00Z",
                    "ranked_day_version_id": 1,
                    "selection": {"ranked_day_version_id": 1},
                },
            ]
            for dependent_job_id, _work_type, _deduplication_key, _input_json in first_dependent_jobs:
                result = processor.process_job(
                    int(dependent_job_id), owner=f"dependent-{dependent_job_id}"
                )
                assert result is not None and result.outcome == "processed"

            (
                corrected_profile,
                corrected_battle,
                corrected_profile_job,
                corrected_battle_job,
            ) = _store_baseline_pair(
                connection_info,
                archive_server,
                key="end-correction",
                boundary=DAY_END,
                trophies=6039,
                empty_battle_log=False,
            )
            assert corrected_profile != end_profile
            assert corrected_battle != end_battle
            for job_id in (corrected_profile_job, corrected_battle_job):
                result = processor.process_job(job_id, owner=f"correction-{job_id}")
                assert result is not None and result.outcome == "processed"

            with database.pool.connection() as connection:
                second_job_row = connection.execute(
                    """
                    SELECT id
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                      AND input_json->>'ranked_day_start' = %s
                      AND id <> %s
                    ORDER BY id
                    LIMIT 1
                    """,
                    (ranked_day_start_text, first_job),
                ).fetchone()
            assert second_job_row is not None
            second_job = int(second_job_row[0])
            second = processor.process_job(second_job, owner="reconcile-correction")
            assert second is not None and second.outcome == "processed"
            with database.pool.connection() as connection:
                second_dependent_jobs = connection.execute(
                    """
                    SELECT id, work_type FROM python_processing_jobs
                    WHERE input_json->>'ranked_day_version_id' = (
                        SELECT id::text FROM ranked_day_versions WHERE version = 2
                    )
                    ORDER BY id
                    """
                ).fetchall()
            for dependent_job_id, _work_type in second_dependent_jobs:
                result = processor.process_job(
                    int(dependent_job_id), owner=f"dependent-{dependent_job_id}"
                )
                assert result is not None and result.outcome == "processed"
            database.requeue_completed_job(second_job)
            replayed = processor.process_job(second_job, owner="reconcile-idempotent")
            assert replayed is not None and replayed.outcome == "processed"

            with database.pool.connection() as connection:
                versions = connection.execute(
                    """
                    SELECT version, state, failure_reasons, replaces_version_id
                    FROM ranked_day_versions
                    ORDER BY version
                    """
                ).fetchall()
                dependent_jobs = connection.execute(
                    """
                    SELECT work_type, count(*)
                    FROM python_processing_jobs
                    WHERE work_type IN ('build_snapshot', 'build_analytics')
                    GROUP BY work_type
                    ORDER BY work_type
                    """
                ).fetchall()
                snapshots = connection.execute(
                    """
                    SELECT snapshot_kind, version, state, correction_of_id,
                           measured_coverage, stale_entry_count
                    FROM leaderboard_snapshots
                    ORDER BY snapshot_kind, version
                    """
                ).fetchall()
                entries = connection.execute(
                    """
                    SELECT s.snapshot_kind, s.version, e.position, e.trophies,
                           e.freshness, e.confidence, e.official_rank
                    FROM leaderboard_snapshot_entries AS e
                    JOIN leaderboard_snapshots AS s ON s.id = e.snapshot_id
                    ORDER BY s.snapshot_kind, s.version, e.position
                    """
                ).fetchall()
                summaries = connection.execute(
                    """
                    SELECT lens, sample_size, unclassified_count,
                           classification_version, analytics_rule_version
                    FROM analytics_summaries
                    ORDER BY id
                    """
                ).fetchall()
                breakdowns = connection.execute(
                    "SELECT army_archetype FROM analytics_breakdowns ORDER BY summary_id"
                ).fetchall()

            assert [(row[0], text(row[1])) for row in versions] == [
                (1, "Complete"),
                (2, "Partial"),
            ]
            assert versions[0][3] is None
            assert versions[1][3] is not None
            assert "trophy_equation_mismatch" in versions[1][2]
            assert [(text(row[0]), row[1]) for row in dependent_jobs] == [
                ("build_analytics", 2),
                ("build_snapshot", 2),
            ]
            assert [(text(row[0]), row[1], text(row[2])) for row in snapshots] == [
                ("frozen", 1, "superseded"),
                ("frozen", 2, "published"),
                ("live", 1, "superseded"),
                ("live", 2, "published"),
            ]
            assert snapshots[0][3] is None and snapshots[1][3] is not None
            assert all(float(row[4]) == 1.0 and row[5] == 0 for row in snapshots)
            assert [(text(row[0]), row[1], row[3]) for row in entries] == [
                ("frozen", 1, 6040),
                ("frozen", 2, 6039),
                ("live", 1, 6040),
                ("live", 2, 6039),
            ]
            assert all(text(row[4]) == "fresh" for row in entries)
            assert all(text(row[5]) == "confirmed" for row in entries)
            assert all(row[6] is None for row in entries)
            assert len(summaries) == 4
            assert {text(row[0]) for row in summaries} == {"offense", "defense"}
            assert {int(row[1]) for row in summaries} == {0, 1}
            assert all(row[1] == row[2] for row in summaries)
            assert all(
                text(row[3]) == "army-classifier-unavailable-v1"
                for row in summaries
            )
            assert all(text(row[4]) == "legend-analytics-v1" for row in summaries)
            assert [text(row[0]) for row in breakdowns] == ["Unclassified"] * 4
        finally:
            database.close()
