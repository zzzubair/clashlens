from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

from clashlens.api_db import ApiDatabase
from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

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


def _processor(
    connection_info: str, archive_server
) -> tuple[Database, ObservationProcessor]:
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
            (
                player_id,
                boundary,
                f"reset-baseline-{key}",
                reset_sweep_id,
                baseline_sweep_id,
            ),
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
            (
                root_job_id,
                root_attempt_id,
                profile_observation_id,
                battle_observation_id,
            ),
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
    observed_at: datetime | None = None,
) -> tuple[int, int, int, int]:
    # A reset-baseline sweep requests its endpoints after the boundary, so the
    # stored observations may complete after ``boundary`` itself.
    observed_at = boundary if observed_at is None else observed_at
    profile_observation, profile_job = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-profile",
        endpoint="profile",
        body=_profile(trophies),
        observed_at=observed_at,
        normalized_tag="#2PP",
    )
    battle_observation, battle_job = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"{key}-battle",
        endpoint="battle_log",
        body=_battle_log(empty=empty_battle_log),
        observed_at=observed_at,
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
        _start_profile, _start_battle, start_profile_job, start_battle_job = (
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
        end_profile, end_battle, end_profile_job, end_battle_job = _store_baseline_pair(
            connection_info,
            archive_server,
            key="end",
            boundary=DAY_END,
            trophies=6040,
            empty_battle_log=False,
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
                    WHERE work_type = 'build_snapshot'
                      AND input_json->>'ranked_day_version_id' = (
                        SELECT id::text FROM ranked_day_versions WHERE version = 1
                    )
                    ORDER BY id
                    """
                ).fetchall()
            assert [(text(row[1]), text(row[2])) for row in first_dependent_jobs] == [
                ("build_snapshot", "build_snapshot:ranked-day-version:1"),
            ]
            assert [row[3] for row in first_dependent_jobs] == [
                {
                    "boundary_at": "2026-08-05T05:00:00Z",
                    "player_id": 1,
                    "ranked_day_start": "2026-08-04T05:00:00Z",
                    "ranked_day_version_id": 1,
                },
            ]
            first_snapshot_id, first_analytics_job_id = _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                int(first_dependent_jobs[0][0]),
                owner_prefix="dependent-first",
            )
            with database.pool.connection() as connection:
                first_analytics = connection.execute(
                    """
                    SELECT input_json, deduplication_key, status
                    FROM python_processing_jobs
                    WHERE id = %s AND work_type = 'build_analytics'
                    """,
                    (first_analytics_job_id,),
                ).fetchone()
                first_publication = connection.execute(
                    """
                    SELECT snapshot_kind, state
                    FROM leaderboard_snapshots
                    WHERE id IN (%s, %s)
                    ORDER BY snapshot_kind
                    """,
                    (first_snapshot_id, first_snapshot_id + 1),
                ).fetchall()
            assert first_analytics is not None
            assert text(first_analytics[2]) == "complete"
            assert first_analytics[0]["snapshot_id"] == first_snapshot_id
            assert text(first_analytics[1]).startswith("build_analytics:snapshot:")
            assert [text(row[1]) for row in first_publication] == [
                "published",
                "published",
            ]

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
                    WHERE work_type = 'build_snapshot'
                      AND input_json->>'ranked_day_version_id' = (
                        SELECT id::text FROM ranked_day_versions WHERE version = 2
                    )
                    ORDER BY id
                    """
                ).fetchall()
            assert len(second_dependent_jobs) == 1
            assert text(second_dependent_jobs[0][1]) == "build_snapshot"
            second_snapshot_id, second_analytics_job_id = (
                _process_snapshot_and_analytics(
                    connection_info,
                    database,
                    processor,
                    int(second_dependent_jobs[0][0]),
                    owner_prefix="dependent-second",
                )
            )
            database.requeue_completed_job(second_analytics_job_id)
            replayed_analytics = processor.process_job(
                second_analytics_job_id,
                owner="analytics-idempotent",
            )
            assert replayed_analytics is not None
            assert replayed_analytics.outcome == "processed"
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
                version_cursor = connection.execute(
                    """
                    SELECT id, player_id, ranked_day_start, ranked_day_end,
                           official_season_id, season_day_number,
                           season_anchor_rule_version, reconciliation_rule_version,
                           result_hash, input_hash, parser_version,
                           processing_version, domain_rule_version,
                           analytics_rule_version, trophy_allocation_rule_versions,
                           version, replaces_version_id, state, confidence,
                           failure_reasons, start_trophies,
                           final_trophies_before_reset, next_start_trophies,
                           expected_next_start_trophies, attack_count, defense_count,
                           attack_gain, observed_defense_loss,
                           automatic_defense_loss,
                           automatic_defense_evidence_state, net_trophy_change,
                           observed_trophy_change, boundary_adjustment,
                           boundary_adjustment_type, observed_boundary_adjustment,
                           unexplained_residual, formula_components, input_evidence,
                           coverage_evidence, contribution_evidence, shield_evidence,
                           evidence_complete, coverage_complete, reconciled,
                           shield_state, shield_duration_days, start_baseline_id,
                           end_baseline_id
                    FROM ranked_day_versions
                    ORDER BY version
                    """
                )
                version_columns = version_cursor.description
                assert version_columns is not None
                version_details = [
                    dict(
                        zip(
                            [column.name for column in version_columns],
                            row,
                            strict=True,
                        )
                    )
                    for row in version_cursor.fetchall()
                ]
                dependent_jobs = connection.execute(
                    """
                    SELECT work_type, count(*)
                    FROM python_processing_jobs
                    WHERE work_type IN ('build_snapshot', 'build_analytics')
                    GROUP BY work_type
                    ORDER BY work_type
                    """
                ).fetchall()
                snapshot_job_sets = connection.execute(
                    """
                    SELECT input_json->>'ranked_day_version_id', work_type, count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'build_snapshot'
                    GROUP BY input_json->>'ranked_day_version_id', work_type
                    ORDER BY input_json->>'ranked_day_version_id', work_type
                    """
                ).fetchall()
                analytics_job_sets = connection.execute(
                    """
                    SELECT input_json->>'snapshot_id', work_type, count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'build_analytics'
                    GROUP BY input_json->>'snapshot_id', work_type
                    ORDER BY input_json->>'snapshot_id', work_type
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
                daily_logs = connection.execute(
                    """
                    SELECT version, state, coverage, adjustments, battles,
                           partial_reasons
                    FROM api_player_daily_logs
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = '#2PP'
                    )
                      AND ranked_day_start = %s
                    ORDER BY version
                    """,
                    (DAY_START,),
                ).fetchall()

            assert [(row[0], text(row[1])) for row in versions] == [
                (1, "Complete"),
                (2, "Inconsistent"),
            ]
            assert versions[0][3] is None
            assert versions[1][3] is not None
            assert "trophy_equation_mismatch" in versions[1][2]
            assert len(version_details) == 2
            first_version, second_version = version_details
            assert [item["version"] for item in version_details] == [1, 2]
            assert first_version["replaces_version_id"] is None
            assert second_version["replaces_version_id"] == first_version["id"]
            assert text(first_version["state"]) == "Complete"
            assert text(first_version["confidence"]) == "exact"
            assert text(second_version["state"]) == "Inconsistent"
            assert text(second_version["confidence"]) == "uncertain"
            assert first_version["reconciled"] is True
            assert second_version["reconciled"] is False
            assert first_version["evidence_complete"] is True
            assert second_version["evidence_complete"] is True
            assert first_version["coverage_complete"] is True
            assert second_version["coverage_complete"] is True
            assert text(first_version["parser_version"]) == "supercell-source-parser-v1"
            assert (
                text(first_version["processing_version"])
                == "clashlens-domain-processing-v1"
            )
            assert (
                text(first_version["domain_rule_version"])
                == "clashlens-domain-rules-v1"
            )
            assert (
                text(first_version["analytics_rule_version"]) == "legend-analytics-v1"
            )
            assert (
                text(first_version["season_anchor_rule_version"])
                == "legend-season-anchor-v1"
            )
            assert text(first_version["reconciliation_rule_version"]) == (
                "legend-ranked-day-reconciliation-v2"
            )
            assert first_version["trophy_allocation_rule_versions"] == [
                "legend-trophy-allocation-v1"
            ]
            assert second_version["trophy_allocation_rule_versions"] == [
                "legend-trophy-allocation-v1"
            ]
            assert len(text(first_version["input_hash"])) == 64
            assert len(text(second_version["input_hash"])) == 64
            assert len(text(first_version["result_hash"])) == 64
            assert len(text(second_version["result_hash"])) == 64
            assert first_version["input_hash"] != second_version["input_hash"]
            assert first_version["result_hash"] != second_version["result_hash"]
            assert first_version["start_trophies"] == 6000
            assert first_version["final_trophies_before_reset"] == 6040
            assert first_version["next_start_trophies"] == 6040
            assert first_version["expected_next_start_trophies"] == 6040
            assert first_version["attack_count"] == 1
            assert first_version["defense_count"] == 0
            assert first_version["attack_gain"] == 40
            assert first_version["observed_defense_loss"] == 0
            assert first_version["automatic_defense_loss"] is None
            assert text(first_version["automatic_defense_evidence_state"]) == (
                "not_applicable"
            )
            assert first_version["net_trophy_change"] == 40
            assert first_version["observed_trophy_change"] == 40
            assert first_version["boundary_adjustment"] == 0
            assert first_version["boundary_adjustment_type"] is None
            assert first_version["observed_boundary_adjustment"] == 0
            assert first_version["unexplained_residual"] == 0
            assert second_version["next_start_trophies"] == 6039
            assert second_version["observed_trophy_change"] == 39
            assert second_version["observed_boundary_adjustment"] == -1
            assert second_version["unexplained_residual"] == -1
            equation = (
                "next_start = start + attack_gain - defense_loss "
                "- automatic_defense_loss + boundary_adjustment"
            )
            assert first_version["formula_components"]["equation"] == equation
            assert second_version["formula_components"]["equation"] == equation
            assert first_version["formula_components"]["unexplained_residual"] == 0
            assert second_version["formula_components"]["unexplained_residual"] == -1
            assert first_version["formula_components"]["attack_gain"] == 40
            assert first_version["formula_components"]["observed_defense_loss"] == 0
            assert first_version["formula_components"]["automatic_defense_loss"] is None
            assert first_version["input_evidence"]["player_eligible"] is True
            assert (
                first_version["input_evidence"]["coverage_observations"]
                == (first_version["coverage_evidence"])
            )
            assert (
                first_version["input_evidence"]["contributions"]
                == (first_version["contribution_evidence"])
            )
            assert isinstance(first_version["coverage_evidence"], list)
            assert isinstance(first_version["contribution_evidence"], list)
            assert isinstance(first_version["shield_evidence"], dict)
            included = [
                item
                for item in first_version["contribution_evidence"]
                if item["included"]
            ]
            excluded = [
                item
                for item in first_version["contribution_evidence"]
                if not item["included"]
            ]
            assert len(included) == 1
            assert all(item["lens"] == "offense" for item in included)
            assert all(item["lens"] == "offense" for item in excluded)
            assert text(first_version["shield_state"]) == "not_inferred"
            assert first_version["shield_duration_days"] is None
            assert first_version["shield_evidence"]["attack_count_zero"] is False
            assert first_version["shield_evidence"]["coverage_complete"] is True
            assert first_version["start_baseline_id"] is not None
            assert first_version["end_baseline_id"] is not None
            assert second_version["end_baseline_id"] != first_version["end_baseline_id"]
            assert [
                (text(row[0]), text(row[1]), row[2]) for row in snapshot_job_sets
            ] == [
                ("1", "build_snapshot", 1),
                ("2", "build_snapshot", 1),
            ]
            assert [
                (int(row[0]), text(row[1]), row[2]) for row in analytics_job_sets
            ] == [
                (first_snapshot_id, "build_analytics", 1),
                (second_snapshot_id, "build_analytics", 1),
            ]
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
                text(row[3]) == "army-classifier-unavailable-v1" for row in summaries
            )
            assert all(text(row[4]) == "legend-analytics-v1" for row in summaries)
            assert [text(row[0]) for row in breakdowns] == ["Unclassified"] * 4
            assert [(row[0], text(row[1]), text(row[2])) for row in daily_logs] == [
                (1, "Complete", "complete"),
                (2, "Partial", "complete"),
            ]
            assert daily_logs[0][3] == []
            assert daily_logs[1][3] == []
            assert len(daily_logs[0][4]) == 1
            assert len(daily_logs[1][4]) == 1
            assert daily_logs[0][4][0]["included"] is True
            assert daily_logs[1][4][0]["included"] is True
            assert daily_logs[0][5] == []
            assert daily_logs[1][5] == [
                "trophy_equation_mismatch",
                "ranked_day_state:Inconsistent",
            ]

            api_database = ApiDatabase(connection_info)
            try:
                player_page = api_database.get_player_page(
                    "#2PP",
                    now=DAY_END + timedelta(minutes=10),
                    freshness_seconds=900,
                )
            finally:
                api_database.close()
            assert player_page is not None
            assert player_page["coverage"] == "ranked_days"
            assert player_page["daily_logs"] == [
                {
                    "ranked_day_start": DAY_START.isoformat(),
                    "version": 2,
                    "state": "Partial",
                    "coverage": "complete",
                    "adjustments": [],
                    "battles": daily_logs[1][4],
                    "partial_reasons": [
                        "trophy_equation_mismatch",
                        "ranked_day_state:Inconsistent",
                    ],
                }
            ]
        finally:
            database.close()


def test_postgres_persists_complete_inferred_shield_evidence(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url) as connection_info:
        _start_profile, _start_battle, start_profile_job, start_battle_job = (
            _store_baseline_pair(
                connection_info,
                archive_server,
                key="shield-start",
                boundary=DAY_START,
                trophies=6000,
                empty_battle_log=True,
            )
        )
        _end_profile, _end_battle, end_profile_job, end_battle_job = (
            _store_baseline_pair(
                connection_info,
                archive_server,
                key="shield-end",
                boundary=DAY_END,
                trophies=6000,
                empty_battle_log=True,
            )
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in (
                start_profile_job,
                start_battle_job,
                end_profile_job,
                end_battle_job,
            ):
                result = processor.process_job(job_id, owner=f"shield-source-{job_id}")
                assert result is not None and result.outcome == "processed"
            ranked_day_start_text = DAY_START.strftime("%Y-%m-%dT%H:%M:%SZ")
            with database.pool.connection() as connection:
                reconcile_job = connection.execute(
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
            assert reconcile_job is not None
            result = processor.process_job(
                int(reconcile_job[0]), owner="shield-reconcile"
            )
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT version, state, confidence, failure_reasons,
                           attack_count, defense_count, attack_gain,
                           observed_defense_loss, automatic_defense_loss,
                           automatic_defense_evidence_state, net_trophy_change,
                           observed_trophy_change, expected_next_start_trophies,
                           unexplained_residual, formula_components,
                           input_evidence, coverage_evidence,
                           contribution_evidence, shield_evidence,
                           coverage_complete, reconciled, shield_state,
                           shield_duration_days
                    FROM ranked_day_versions
                    WHERE ranked_day_start = %s
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (DAY_START,),
                ).fetchone()
            assert row is not None
            assert row[0] == 1
            assert text(row[1]) == "Complete"
            assert text(row[2]) == "inferred"
            assert row[3] == []
            assert row[4:9] == (0, 0, 0, 0, None)
            assert text(row[9]) == "not_applicable"
            assert row[10:14] == (0, 0, 6000, 0)
            assert row[14]["equation"] == (
                "next_start = start + attack_gain - defense_loss "
                "- automatic_defense_loss + boundary_adjustment"
            )
            assert row[14]["unexplained_residual"] == 0
            assert row[15]["player_eligible"] is True
            assert row[16] == row[15]["coverage_observations"]
            assert row[17] == row[15]["contributions"] == []
            assert row[18]["attack_count_zero"] is True
            assert row[18]["defense_count_zero"] is True
            assert row[19] is True
            assert row[20] is True
            assert text(row[21]) == "inferred_shielded"
            assert row[22] == 1
        finally:
            database.close()


def test_completion_binds_coverage_chain_to_sweep_battle_log_observation_ids(
    database_url: str,
    archive_server,
) -> None:
    with domain_database(database_url) as connection_info:
        _start_profile, start_battle, start_profile_job, start_battle_job = (
            _store_baseline_pair(
                connection_info,
                archive_server,
                key="identity-start",
                boundary=DAY_START,
                trophies=6000,
                empty_battle_log=True,
                observed_at=DAY_START + timedelta(seconds=5),
            )
        )
        # A normal poll observed before the start sweep response must not
        # become the chain head.
        _pre_start_poll, pre_start_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="identity-pre-start-poll",
            endpoint="battle_log",
            body=_battle_log(empty=True),
            observed_at=DAY_START + timedelta(seconds=2),
            normalized_tag="#2PP",
        )
        middle_observation, middle_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="identity-middle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )
        _end_profile, end_battle, end_profile_job, end_battle_job = (
            _store_baseline_pair(
                connection_info,
                archive_server,
                key="identity-end",
                boundary=DAY_END,
                trophies=6040,
                empty_battle_log=True,
                observed_at=DAY_END + timedelta(seconds=5),
            )
        )
        # A normal poll observed after the end sweep response must not become
        # the chain tail.
        _post_end_poll, post_end_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="identity-post-end-poll",
            endpoint="battle_log",
            body=_battle_log(empty=True),
            observed_at=DAY_END + timedelta(seconds=30),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in (
                start_profile_job,
                start_battle_job,
                pre_start_job,
                middle_job,
                end_profile_job,
                end_battle_job,
                post_end_job,
            ):
                result = processor.process_job(
                    job_id, owner=f"identity-source-{job_id}"
                )
                assert result is not None and result.outcome == "processed"
            ranked_day_start_text = DAY_START.strftime("%Y-%m-%dT%H:%M:%SZ")
            with database.pool.connection() as connection:
                reconcile_job = connection.execute(
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
            assert reconcile_job is not None
            result = processor.process_job(
                int(reconcile_job[0]), owner="identity-reconcile"
            )
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT state, confidence, failure_reasons, coverage_complete,
                           coverage_evidence, input_evidence
                    FROM ranked_day_versions
                    WHERE ranked_day_start = %s
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (DAY_START,),
                ).fetchone()
                sweep_rows = connection.execute(
                    """
                    SELECT DISTINCT ON (boundary_at) boundary_at,
                           battle_log_observation_id
                    FROM reset_baseline_evidence
                    WHERE player_id = %s AND boundary_at IN (%s, %s)
                    ORDER BY boundary_at, version DESC
                    """,
                    (1, DAY_START, DAY_END),
                ).fetchall()
            assert row is not None
            assert len(sweep_rows) == 2
            assert int(sweep_rows[0][1]) == start_battle
            assert int(sweep_rows[1][1]) == end_battle
            # This test asserts coverage identity only: the coverage chain is
            # bound to the exact battle-log observations the start and end
            # reset-baseline sweeps selected. The fixture does not supply all
            # unrelated automatic-defense/adjustment evidence, so the overall
            # state or confidence must not be asserted here.
            assert row[3] is True
            assert "missing_start_battle_log_baseline" not in row[2]
            assert "missing_end_battle_log_baseline" not in row[2]
            coverage_evidence = row[4]
            assert isinstance(coverage_evidence, list)
            coverage_ids = [item["observation_id"] for item in coverage_evidence]
            assert coverage_ids[0] == int(sweep_rows[0][1])
            assert coverage_ids[-1] == int(sweep_rows[1][1])
            assert middle_observation in coverage_ids
            assert _pre_start_poll not in coverage_ids
            assert _post_end_poll not in coverage_ids
            assert row[5]["start_baseline_battle_log_observation_id"] == int(
                sweep_rows[0][1]
            )
            assert row[5]["end_baseline_battle_log_observation_id"] == int(
                sweep_rows[1][1]
            )
        finally:
            database.close()
