from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def _store_baseline_pair(
    connection_info: str,
    archive_server,
    *,
    key: str,
    boundary: datetime,
    trophies: int,
    empty_battle_log: bool,
) -> tuple[int, int]:
    profile_observation, _ = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-profile",
        endpoint="profile",
        body=_profile(trophies),
        observed_at=boundary,
        normalized_tag="#2PP",
    )
    battle_observation, _ = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-battle",
        endpoint="battle_log",
        body=_battle_log(empty=empty_battle_log),
        observed_at=boundary,
        normalized_tag="#2PP",
    )
    return profile_observation, battle_observation


def _link_reset_evidence(
    database: Database,
    *,
    boundary: datetime,
    profile_observation_id: int,
    battle_observation_id: int,
) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            sweep = connection.execute(
                """
                INSERT INTO collector_reset_sweeps (boundary_at)
                VALUES (%s)
                RETURNING id
                """,
                (boundary,),
            ).fetchone()
            player = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()
            assert sweep is not None and player is not None
            connection.execute(
                """
                INSERT INTO reset_baseline_evidence (
                    sweep_id, player_id, boundary_at,
                    profile_observation_id, battle_log_observation_id,
                    profile_valid, battle_log_valid, legacy_profile_only
                ) VALUES (%s, %s, %s, %s, %s, true, true, false)
                """,
                (
                    sweep[0],
                    player[0],
                    boundary,
                    profile_observation_id,
                    battle_observation_id,
                ),
            )


def test_durable_reconciliation_versions_late_corrections_without_rewriting_history(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url) as connection_info:
        start_profile, start_battle = _store_baseline_pair(
            connection_info,
            archive_server,
            key="start",
            boundary=DAY_START,
            trophies=6000,
            empty_battle_log=True,
        )
        store_observation(
            connection_info,
            archive_server,
            occurrence_key="middle-battle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )
        end_profile, end_battle = _store_baseline_pair(
            connection_info,
            archive_server,
            key="end",
            boundary=DAY_END,
            trophies=6040,
            empty_battle_log=False,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            results = processor.process_until_idle(owner="source-worker")
            assert len(results) == 5
            assert all(result.outcome == "processed" for result in results)
            _link_reset_evidence(
                database,
                boundary=DAY_START,
                profile_observation_id=start_profile,
                battle_observation_id=start_battle,
            )
            _link_reset_evidence(
                database,
                boundary=DAY_END,
                profile_observation_id=end_profile,
                battle_observation_id=end_battle,
            )

            first_job = database.enqueue_reconciliation(
                player_tag="#2PP",
                day_start=DAY_START,
                now=DAY_END + timedelta(minutes=1),
                request_key="first",
            )
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

            corrected_profile, corrected_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="corrected-end-profile",
                endpoint="profile",
                body=_profile(6039),
                observed_at=DAY_END + timedelta(seconds=1),
                normalized_tag="#2PP",
            )
            corrected_source = processor.process_job(
                corrected_job, owner="corrected-source"
            )
            assert corrected_source is not None
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE reset_baseline_evidence
                    SET profile_observation_id = %s
                    WHERE boundary_at = %s
                    """,
                    (corrected_profile, DAY_END),
                )
                connection.commit()

            second_job = database.enqueue_reconciliation(
                player_tag="#2PP",
                day_start=DAY_START,
                now=DAY_END + timedelta(minutes=2),
                request_key="late-correction",
            )
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
