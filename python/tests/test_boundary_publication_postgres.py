from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from domain_test_support import domain_database, store_observation, text
from test_army_analytics_publication_postgres import _insert_confirmed_anchor

from clashlens.db import Database

BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY_START = BOUNDARY - timedelta(days=1)


def _player_and_version(connection, tag: str, version: int, input_hash: str) -> tuple[int, int]:
    player_id = int(
        connection.execute(
            "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
            (tag,),
        ).fetchone()[0]
    )
    version_id = int(
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end, official_season_id,
                season_day_number, season_anchor_rule_version,
                reconciliation_rule_version, result_hash, version, state, confidence,
                input_hash, evidence_complete, coverage_complete
            ) VALUES (
                %s, %s, %s, 'test-season', 1, 'test-anchor', 'test-rules',
                %s, %s, 'Complete', 'exact', %s, true, true
            ) RETURNING id
            """,
            (player_id, DAY_START, BOUNDARY, input_hash, version, input_hash),
        ).fetchone()[0]
    )
    return player_id, version_id


def _sweep_with_members(connection, player_ids: list[int]) -> int:
    sweep_id = int(
        connection.execute(
            "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s) RETURNING id",
            (BOUNDARY,),
        ).fetchone()[0]
    )
    for player_id in player_ids:
        connection.execute(
            "INSERT INTO collector_reset_sweep_members (sweep_id, player_id) VALUES (%s, %s)",
            (sweep_id, player_id),
        )
    return sweep_id


def test_boundary_generation_coalesces_population_and_corrections(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                players = [
                    _player_and_version(connection, tag, 1, f"{char}" * 64)
                    for tag, char in (("#A1", "a"), ("#A2", "b"), ("#A3", "c"))
                ]
                sweep_id = connection.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s) RETURNING id",
                    (BOUNDARY,),
                ).fetchone()[0]
                for player_id, _ in players:
                    connection.execute(
                        "INSERT INTO collector_reset_sweep_members (sweep_id, player_id) VALUES (%s, %s)",
                        (sweep_id, player_id),
                    )
                connection.execute(
                    "UPDATE ranked_day_versions SET state = 'Live' WHERE id = %s",
                    (players[-1][1],),
                )
                for player_id, version_id in players:
                    assert database._record_boundary_generation(
                        connection,
                        boundary_at=BOUNDARY,
                        player_id=player_id,
                        ranked_day_version_id=version_id,
                        ranked_day_input_hash="a" * 64,
                    )
                generation = connection.execute(
                    "SELECT id, expected_population_count, expected_population_hash FROM boundary_publication_generations"
                ).fetchone()
                assert generation is not None
                assert generation[1] == 3
                assert len(generation[2]) == 64
                assert connection.execute(
                    "SELECT count(*) FROM boundary_publication_generations"
                ).fetchone()[0] == 1

                # Snapshot readiness is independent from army readiness; one
                # non-terminal member must not block the snapshot job.
                database._try_enqueue_boundary_artifacts(
                    connection, boundary_at=BOUNDARY, generation_id=int(generation[0])
                )
                assert connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                ).fetchone()[0] == 1
                snapshot_due_at = connection.execute(
                    "SELECT due_at FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                ).fetchone()[0]
                assert snapshot_due_at == BOUNDARY + timedelta(minutes=5)
                assert connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                ).fetchone()[0] == 0
                connection.execute(
                    "UPDATE ranked_day_versions SET state = 'Complete' WHERE id = %s",
                    (players[-1][1],),
                )
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=players[-1][0],
                    ranked_day_version_id=players[-1][1],
                    ranked_day_input_hash="a" * 64,
                )
                assert connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                ).fetchone()[0] == 1
                snapshot_job_id = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                    ).fetchone()[0]
                )
                connection.commit()
                claim = database.claim_job(owner="boundary-test", job_id=snapshot_job_id)
                assert claim is not None
                database.complete_snapshot(claim)
                assert connection.execute(
                    "SELECT count(*) FROM boundary_publication_events"
                ).fetchone()[0] == 0
                analytics_job_id = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_analytics'"
                    ).fetchone()[0]
                )
                connection.commit()
                analytics_claim = database.claim_job(
                    owner="boundary-analytics", job_id=analytics_job_id
                )
                assert analytics_claim is not None
                database.complete_analytics(analytics_claim)
                assert connection.execute(
                    "SELECT count(*) FROM boundary_publication_events"
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT snapshot_state FROM boundary_publication_generations WHERE id = %s",
                    (generation[0],),
                ).fetchone()[0] == "published"
                army_job_id = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                    ).fetchone()[0]
                )
                connection.commit()
                army_claim = database.claim_job(owner="boundary-army", job_id=army_job_id)
                assert army_claim is not None
                database.complete_army_analytics(army_claim)
                assert connection.execute(
                    "SELECT army_state FROM boundary_publication_generations WHERE id = %s",
                    (generation[0],),
                ).fetchone()[0] == "published"

                # A published generation is immutable. A changed member creates
                # one superseding generation and does not create per-player jobs.
                connection.execute(
                    "UPDATE boundary_publication_generations SET army_state = 'published' WHERE id = %s",
                    (generation[0],),
                )
                player_id, _ = players[0]
                corrected_hash = "d" * 64
                corrected_version = int(
                    connection.execute(
                        """
                        INSERT INTO ranked_day_versions (
                            player_id, ranked_day_start, ranked_day_end, official_season_id,
                            season_day_number, season_anchor_rule_version,
                            reconciliation_rule_version, result_hash, version, state,
                            confidence, input_hash, evidence_complete, coverage_complete
                        ) VALUES (%s, %s, %s, 'test-season', 1, 'test-anchor',
                                  'test-rules', %s, 2, 'Complete', 'exact', %s, true, true)
                        RETURNING id
                        """,
                        (player_id, DAY_START, BOUNDARY, corrected_hash, corrected_hash),
                    ).fetchone()[0]
                )
                assert database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=corrected_version,
                    ranked_day_input_hash=corrected_hash,
                )
                generations = connection.execute(
                    "SELECT generation, supersedes_id FROM boundary_publication_generations ORDER BY generation"
                ).fetchall()
                assert [(row[0], row[1] is not None) for row in generations] == [(1, False), (2, True)]
                assert connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                ).fetchone()[0] == 2
                stale_army_job = connection.execute(
                    """
                    SELECT id
                    FROM python_processing_jobs
                    WHERE work_type = 'build_army_analytics'
                      AND input_json->>'generation' = '2'
                    """
                ).fetchone()
                assert stale_army_job is not None
                connection.execute(
                    "UPDATE boundary_publication_generations SET army_state = 'superseded', snapshot_state = 'superseded' WHERE generation = 2"
                )
                connection.commit()
                stale_claim = database.claim_job(
                    owner="stale-army", job_id=int(stale_army_job[0])
                )
                assert stale_claim is not None
                with pytest.raises(ValueError, match="stale or superseded"):
                    database.complete_army_analytics(stale_claim)
        finally:
            database.close()


def test_mixed_boundary_last_unavailable_still_enqueues_army(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                first = _player_and_version(connection, "#M1", 1, "a" * 64)
                second = _player_and_version(connection, "#M2", 1, "b" * 64)
                third = _player_and_version(connection, "#M3", 1, "c" * 64)
                sweep_id = _sweep_with_members(
                    connection, [first[0], second[0], third[0]]
                )
                for player_id, version_id in (first, second):
                    database._record_boundary_baseline(
                        connection,
                        boundary_at=BOUNDARY,
                        reset_sweep_id=sweep_id,
                        player_id=player_id,
                        state="complete",
                    )
                    database._record_boundary_generation(
                        connection,
                        boundary_at=BOUNDARY,
                        player_id=player_id,
                        ranked_day_version_id=version_id,
                        ranked_day_input_hash="a" * 64,
                    )
                database._record_boundary_baseline(
                    connection,
                    boundary_at=BOUNDARY,
                    reset_sweep_id=sweep_id,
                    player_id=third[0],
                    state="failed",
                )
                statuses = connection.execute(
                    """
                    SELECT player_id, status
                    FROM boundary_publication_generation_members
                    ORDER BY player_id
                    """
                ).fetchall()
                assert [text(row[1]) for row in statuses] == [
                    "terminal",
                    "terminal",
                    "unavailable",
                ]
                assert connection.execute(
                    """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'build_army_analytics'
                      AND input_json->>'generation' = '1'
                    """
                ).fetchone()[0] == 1
                assert text(
                    connection.execute(
                        """
                        SELECT snapshot_state
                        FROM boundary_publication_generations
                        WHERE generation = 1
                        """
                    ).fetchone()[0]
                ) == "ready"
        finally:
            database.close()


def test_all_unavailable_boundary_publishes_empty_army_with_anchor_metadata(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                players = [
                    int(
                        connection.execute(
                            "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
                            (tag,),
                        ).fetchone()[0]
                    )
                    for tag in ("#U1", "#U2")
                ]
                sweep_id = _sweep_with_members(connection, players)
                for player_id in players:
                    database._record_boundary_baseline(
                        connection,
                        boundary_at=BOUNDARY,
                        reset_sweep_id=sweep_id,
                        player_id=player_id,
                        state="failed",
                    )
                connection.commit()

            store_observation(
                connection_info,
                archive_server,
                occurrence_key="all-unavailable-anchor-source",
                endpoint="profile",
                body=b"{}",
                observed_at=DAY_START,
                normalized_tag="#2PP",
            )
            _insert_confirmed_anchor(database, "test-current", "test-previous")
            with database.pool.connection() as connection:
                army_job = connection.execute(
                    """
                    SELECT id
                    FROM python_processing_jobs
                    WHERE work_type = 'build_army_analytics'
                      AND input_json->>'generation' = '1'
                    """
                ).fetchone()
                assert army_job is not None
                connection.commit()
            claim = database.claim_job(owner="all-unavailable-army", job_id=int(army_job[0]))
            assert claim is not None
            database.complete_army_analytics(claim)
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM ranked_day_versions"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_battle_facts"
                ).fetchone()[0] == 0
                summary = connection.execute(
                    """
                    SELECT official_season_id, total_attacks, sample_size,
                           excluded_attacks
                    FROM army_analytics_day_summaries
                    WHERE ranked_day_start = %s AND exact_trophies = -1
                    """,
                    (DAY_START,),
                ).fetchone()
                assert summary is not None
                assert text(summary[0]) == "test-current"
                assert tuple(summary[1:]) == (0, 0, 0)
                generation = connection.execute(
                    """
                    SELECT army_state
                    FROM boundary_publication_generations
                    WHERE generation = 1
                    """
                ).fetchone()
                assert generation is not None
                assert text(generation[0]) == "published"
        finally:
            database.close()


def test_boundary_progress_updates_only_the_changed_member(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                first = _player_and_version(connection, "#P1", 1, "a" * 64)
                second = _player_and_version(connection, "#P2", 1, "b" * 64)
                _sweep_with_members(connection, [first[0], second[0]])
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=first[0],
                    ranked_day_version_id=first[1],
                    ranked_day_input_hash="a" * 64,
                )
                before = connection.execute(
                    """
                    SELECT updated_at
                    FROM boundary_publication_generation_members
                    WHERE player_id = %s
                    """,
                    (first[0],),
                ).fetchone()[0]
                connection.execute("SELECT pg_sleep(0.02)")
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=second[0],
                    ranked_day_version_id=second[1],
                    ranked_day_input_hash="b" * 64,
                )
                after = connection.execute(
                    """
                    SELECT updated_at
                    FROM boundary_publication_generation_members
                    WHERE player_id = %s
                    """,
                    (first[0],),
                ).fetchone()[0]
                assert after == before
        finally:
            database.close()
