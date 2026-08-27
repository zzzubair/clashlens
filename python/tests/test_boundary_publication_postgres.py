from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from domain_test_support import domain_database, store_observation, text
from psycopg.types.json import Jsonb
from test_army_analytics_publication_postgres import _insert_confirmed_anchor

from clashlens.db import Database

BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY_START = BOUNDARY - timedelta(days=1)


def _player_and_version(
    connection, tag: str, version: int, input_hash: str
) -> tuple[int, int]:
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
    connection.execute(
        """
        INSERT INTO collector_boundary_admission (
            boundary_at, reset_sweep_id, regular_drain_complete,
            reset_drain_complete, safe_handoff, state
        ) VALUES (%s, %s, true, true, true, 'safe_handoff')
        """,
        (BOUNDARY, sweep_id),
    )
    return sweep_id


def test_complete_ranked_day_without_profile_is_not_snapshot_complete(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, "#NOPROFILE", 1, "a" * 64
                )
                _sweep_with_members(connection, [player_id])
                assert database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                statuses = connection.execute(
                    """
                    SELECT snapshot_status, army_status
                    FROM boundary_publication_generation_members
                    WHERE player_id = %s
                    """,
                    (player_id,),
                ).fetchone()
                assert statuses == ("missing", "unavailable")
                generation_id = connection.execute(
                    """
                    SELECT id FROM boundary_publication_generations
                    WHERE boundary_at = %s
                    """,
                    (BOUNDARY,),
                ).fetchone()[0]
                manifest = database._freeze_boundary_manifest(
                    connection,
                    generation_id=int(generation_id),
                    artifact_kind="snapshot",
                )
                classification = connection.execute(
                    """
                    SELECT classification
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s AND player_id = %s
                    """,
                    (manifest[0], player_id),
                ).fetchone()[0]
                assert classification == "Missing"
        finally:
            database.close()


def test_baseline_recomputes_army_status_until_decodes_exist(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, "#BASELINEARMY", 1, "a" * 64
                )
            with database.pool.connection() as connection:
                _sweep_with_members(connection, [player_id])
                observation_id, _ = store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key="baseline-army-profile",
                    endpoint="profile",
                    body=b"baseline-army-profile",
                    observed_at=BOUNDARY - timedelta(hours=1),
                    normalized_tag="#BASELINEARMY",
                    existing_connection=connection,
                    commit=False,
                )
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag, endpoint_version,
                        schema_version, parser_version, observed_at, source_http_status,
                        name, trophies, league_tier_id, league_tier_name,
                        eligibility_state, profile_json
                    ) VALUES (%s, %s, '#BASELINEARMY', 'endpoint-v1', 'schema-v1',
                              'parser-v1', %s, 200, 'Baseline', 6000, 1, 'Legend I',
                              'eligible', %s)
                    """,
                    (
                        player_id,
                        observation_id,
                        BOUNDARY - timedelta(hours=1),
                        Jsonb({"tag": "#BASELINEARMY", "trophies": 6000}),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_version_id, version,
                        state, coverage, battles, ranked_day_end,
                        official_season_id, season_day_number
                    ) VALUES (%s, %s, %s, 1, 'Complete', 'complete', %s, %s,
                              'test-season', 1)
                    """,
                    (
                        player_id,
                        DAY_START,
                        ranked_id,
                        Jsonb([{"battle_id": "999999", "lens": "offense"}]),
                        BOUNDARY,
                    ),
                )
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                database._record_boundary_baseline(
                    connection,
                    boundary_at=BOUNDARY,
                    reset_sweep_id=_sweep_id(connection),
                    player_id=player_id,
                    state="complete",
                )
                statuses = connection.execute(
                    """
                    SELECT snapshot_status, army_status
                    FROM boundary_publication_generation_members
                    WHERE player_id = %s
                    """,
                    (player_id,),
                ).fetchone()
                assert statuses == ("complete", "pending")
        finally:
            database.close()


def _sweep_id(connection) -> int:
    return int(
        connection.execute(
            "SELECT id FROM collector_reset_sweeps ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    )


def test_snapshot_publication_uses_only_frozen_manifest_profile(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, "#P2", 1, "a" * 64
                )
                _sweep_with_members(connection, [player_id])
                connection.commit()

            def insert_profile(
                observation_id: int,
                observed_at: datetime,
                trophies: int,
                eligibility: str,
            ) -> None:
                with database.pool.connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO player_profile_versions (
                            player_id, observation_id, normalized_tag,
                            endpoint_version, schema_version, parser_version,
                            observed_at, source_http_status, name, trophies,
                            league_tier_id, league_tier_name, eligibility_state,
                            profile_json
                        ) VALUES (%s, %s, '#P2', 'endpoint-v1',
                                  'schema-v1', 'parser-v1', %s, 200, 'Manifest',
                                  %s, 1, 'Legend I', %s, %s)
                        """,
                        (
                            player_id,
                            observation_id,
                            observed_at,
                            trophies,
                            eligibility,
                            Jsonb({"tag": "#P2", "trophies": trophies}),
                        ),
                    )
                    connection.commit()

            old_observation, _ = store_observation(
                connection_info,
                archive_server,
                occurrence_key="manifest-profile-old",
                endpoint="profile",
                body=b"old-profile",
                observed_at=BOUNDARY - timedelta(hours=2),
                normalized_tag="#P2",
            )
            insert_profile(
                old_observation, BOUNDARY - timedelta(hours=2), 6123, "eligible"
            )
            with database.pool.connection() as connection:
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                snapshot_job = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                ).fetchone()[0]
                connection.commit()

            new_observation, _ = store_observation(
                connection_info,
                archive_server,
                occurrence_key="manifest-profile-new",
                endpoint="profile",
                body=b"new-profile",
                observed_at=BOUNDARY - timedelta(hours=1),
                normalized_tag="#P2",
            )
            insert_profile(
                new_observation, BOUNDARY - timedelta(hours=1), 9999, "ineligible"
            )
            claim = database.claim_job(owner="manifest-profile", job_id=snapshot_job)
            assert claim is not None
            database.complete_snapshot(claim)
            with database.pool.connection() as connection:
                entry = connection.execute(
                    """
                    SELECT trophies
                    FROM leaderboard_snapshot_entries AS entry
                    JOIN leaderboard_snapshots AS snapshot
                      ON snapshot.id = entry.snapshot_id
                    WHERE snapshot.snapshot_kind = 'frozen'
                      AND snapshot.boundary_at = %s
                    """,
                    (BOUNDARY,),
                ).fetchone()
                assert entry == (6123,)
        finally:
            database.close()


def test_boundary_generation_coalesces_population_and_corrections(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
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
                    """
                    INSERT INTO collector_boundary_admission (
                        boundary_at, reset_sweep_id, regular_drain_complete,
                        reset_drain_complete, safe_handoff, state
                    ) VALUES (%s, %s, true, true, true, 'safe_handoff')
                    """,
                    (BOUNDARY, sweep_id),
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
                    """
                    SELECT id, expected_population_count, expected_population_hash,
                           membership_rule_version, target_rule, target_at
                    FROM boundary_publication_generations
                    """
                ).fetchone()
                assert generation is not None
                assert generation[1] == 3
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_generation_members WHERE generation_id = %s",
                        (generation[0],),
                    ).fetchone()[0]
                    == generation[1]
                )
                assert len(generation[2]) == 64
                assert text(generation[3]) == "active-members-v1"
                assert text(generation[4]) == "boundary-delay-v1"
                assert generation[5] == BOUNDARY + timedelta(minutes=5)
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_generations"
                    ).fetchone()[0]
                    == 1
                )

                # Pending reset classifications block both artifacts; the
                # coordinator never publishes from incomplete membership.
                database._try_enqueue_boundary_artifacts(
                    connection, boundary_at=BOUNDARY, generation_id=int(generation[0])
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                    ).fetchone()[0]
                    == 0
                )
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
                assert (
                    connection.execute(
                        "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                    ).fetchone()[0]
                    == 1
                )
                snapshot_due_at = connection.execute(
                    "SELECT due_at FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                ).fetchone()[0]
                assert snapshot_due_at == BOUNDARY + timedelta(minutes=5)
                snapshot_job_id = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                    ).fetchone()[0]
                )
                connection.commit()
                claim = database.claim_job(
                    owner="boundary-test", job_id=snapshot_job_id
                )
                assert claim is not None
                database.complete_snapshot(claim)
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_events"
                    ).fetchone()[0]
                    == 0
                )
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
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_events"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    text(
                        connection.execute(
                            "SELECT snapshot_state FROM boundary_publication_generations WHERE id = %s",
                            (generation[0],),
                        ).fetchone()[0]
                    )
                    == "published"
                )
                army_job_id = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                    ).fetchone()[0]
                )
                connection.commit()
                army_claim = database.claim_job(
                    owner="boundary-army", job_id=army_job_id
                )
                assert army_claim is not None
                database.complete_army_analytics(army_claim)
                assert (
                    text(
                        connection.execute(
                            "SELECT army_state FROM boundary_publication_generations WHERE id = %s",
                            (generation[0],),
                        ).fetchone()[0]
                    )
                    == "published"
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_events"
                    ).fetchone()[0]
                    == 1
                )
                signal = connection.execute(
                    """
                    SELECT snapshot_id, snapshot_analytics_publication_id,
                           army_publication_id, manifest_ids
                    FROM boundary_publication_events
                    WHERE boundary_at = %s AND generation = 1
                    """,
                    (BOUNDARY,),
                ).fetchone()
                assert signal is not None
                assert all(signal[index] is not None for index in range(3))
                assert signal[3]["snapshot"] is not None
                assert signal[3]["army"] is not None

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
                        (
                            player_id,
                            DAY_START,
                            BOUNDARY,
                            corrected_hash,
                            corrected_hash,
                        ),
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
                assert [(row[0], row[1] is not None) for row in generations] == [
                    (1, False),
                    (2, True),
                ]
                assert (
                    connection.execute(
                        "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                    ).fetchone()[0]
                    == 2
                )
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
                database.complete_army_analytics(stale_claim)
                stale_state = connection.execute(
                    "SELECT status, outcome FROM python_processing_jobs WHERE id = %s",
                    (int(stale_army_job[0]),),
                ).fetchone()
                assert tuple(text(value) for value in stale_state) == (
                    "complete",
                    "stale_superseded",
                )
        finally:
            database.close()


def test_decode_only_correction_inherits_snapshot_publication_identity(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, "#DECODE1", 1, "a" * 64
                )
                _sweep_with_members(connection, [player_id])
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                generation = connection.execute(
                    """
                    SELECT id, snapshot_manifest_id, army_manifest_id
                    FROM boundary_publication_generations
                    WHERE generation = 1
                    """
                ).fetchone()
                assert generation is not None
                snapshot_id = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version,
                        ordering_rule_version, freshness_rule_version, state,
                        measured_coverage, stale_entry_count,
                        eligible_population_count, included_entry_count,
                        fresh_entry_count, input_hash
                    ) VALUES ('frozen', %s, 1, 'order', 'freshness', 'published',
                              1, 0, 1, 1, 1, repeat('b', 64))
                    RETURNING id
                    """,
                    (BOUNDARY,),
                ).fetchone()[0]
                snapshot_identity = database._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation[0]),
                    artifact_kind="analytics",
                    manifest_id=int(generation[1]),
                    input_hash="b" * 64,
                    source_identity={"snapshot_id": int(snapshot_id)},
                )
                army_identity = database._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation[0]),
                    artifact_kind="army",
                    manifest_id=int(generation[2]),
                    input_hash="c" * 64,
                    source_identity={"day": "test"},
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_state = 'published', snapshot_id = %s,
                        snapshot_input_hash = %s,
                        snapshot_analytics_publication_id = %s,
                        army_state = 'ready', army_input_hash = %s,
                        army_publication_id = %s
                    WHERE id = %s
                    """,
                    (
                        snapshot_id,
                        "b" * 64,
                        snapshot_identity,
                        "c" * 64,
                        army_identity,
                        generation[0],
                    ),
                )
                connection.execute("SAVEPOINT army_ready_correction")
                database._queue_boundary_army_correction(
                    connection, boundary_at=BOUNDARY, generation_id=int(generation[0])
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_corrections"
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_generations WHERE generation = 2"
                    ).fetchone()[0]
                    == 0
                )
                connection.execute("ROLLBACK TO SAVEPOINT army_ready_correction")
                connection.execute(
                    "UPDATE boundary_publication_generations SET army_state = 'building' WHERE id = %s",
                    (generation[0],),
                )
                connection.execute("SAVEPOINT army_building_correction")
                database._queue_boundary_army_correction(
                    connection, boundary_at=BOUNDARY, generation_id=int(generation[0])
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_corrections"
                    ).fetchone()[0]
                    == 1
                )
                connection.execute("ROLLBACK TO SAVEPOINT army_building_correction")
                connection.execute(
                    "UPDATE boundary_publication_generations SET snapshot_state = 'published', army_state = 'published' WHERE id = %s",
                    (generation[0],),
                )
                database._queue_boundary_army_correction(
                    connection, boundary_at=BOUNDARY, generation_id=int(generation[0])
                )
                inherited = connection.execute(
                    """
                    SELECT snapshot_state, snapshot_id, snapshot_input_hash,
                           snapshot_manifest_id, snapshot_analytics_publication_id,
                           affected_artifacts
                    FROM boundary_publication_generations
                    WHERE generation = 2
                    """
                ).fetchone()
                assert inherited is not None
                assert tuple(inherited[:5]) == (
                    "published",
                    snapshot_id,
                    "b" * 64,
                    generation[1],
                    snapshot_identity,
                )
                assert inherited[5] == ["army"]
                correction = connection.execute(
                    """
                    SELECT state, affected_artifacts
                    FROM boundary_publication_corrections
                    WHERE generation_id = (SELECT id FROM boundary_publication_generations WHERE generation = 2)
                    """
                ).fetchone()
                assert correction is not None
                assert tuple(correction) == ("active", ["army"])
        finally:
            database.close()


@pytest.mark.parametrize("army_state", ["ready", "building", "published"])
def test_enqueue_army_routes_frozen_drift_for_each_state_despite_unrelated_reconcile(
    database_url: str, army_state: str, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, f"#DRIFT{army_state}", 1, "a" * 64
                )
                unrelated_player_id, _ = _player_and_version(
                    connection, f"#UNRELATED{army_state}", 1, "b" * 64
                )
                _sweep_with_members(connection, [player_id])
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_version_id, version,
                        state, coverage, battles, ranked_day_end,
                        official_season_id, season_day_number
                    ) VALUES (%s, %s, %s, 1, 'Complete', 'complete', %s, %s,
                              'test-season', 1)
                    """,
                    (
                        player_id,
                        DAY_START,
                        ranked_id,
                        Jsonb([{"battle_id": "987654", "lens": "offense"}]),
                        BOUNDARY,
                    ),
                )
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                generation = connection.execute(
                    "SELECT id FROM boundary_publication_generations"
                ).fetchone()[0]
                manifest = database._freeze_boundary_manifest(
                    connection, generation_id=int(generation), artifact_kind="army"
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET army_manifest_id = %s, army_state = %s
                    WHERE id = %s
                    """,
                    (manifest[0], army_state, generation),
                )
                unrelated_job = connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        observation_id, work_type, deduplication_key, input_json,
                        status, due_at
                    ) VALUES (
                        NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp()
                    ) RETURNING id
                    """,
                    (
                        f"unrelated-reconcile:{army_state}",
                        Jsonb(
                            {
                                "player_id": unrelated_player_id,
                                "ranked_day_start": (
                                    DAY_START - timedelta(days=1)
                                ).strftime("%Y-%m-%dT05:00:00Z"),
                            }
                        ),
                    ),
                ).fetchone()[0]
                assert (
                    connection.execute(
                        "SELECT status FROM python_processing_jobs WHERE id = %s",
                        (unrelated_job,),
                    ).fetchone()[0]
                    == "pending"
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_corrections"
                    ).fetchone()[0]
                    == 0
                )
                monkeypatch.setattr(
                    database,
                    "_boundary_army_manifest_needs_correction",
                    lambda *_args, **_kwargs: True,
                )
                database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)
                correction = connection.execute(
                    """
                    SELECT state, affected_artifacts
                    FROM boundary_publication_corrections
                    WHERE source_generation_id = %s
                    """,
                    (generation,),
                ).fetchone()
                assert correction is not None
                assert correction[0] == "queued"
                assert list(correction[1]) == ["army"]
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_corrections"
                    ).fetchone()[0]
                    == 1
                )
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'complete' WHERE id = %s",
                    (unrelated_job,),
                )
            database.close()
            restarted = Database(connection_info)
            try:
                restarted.reevaluate_boundary_publications()
                with restarted.pool.connection() as connection:
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM boundary_publication_corrections"
                        ).fetchone()[0]
                        == 1
                    )
            finally:
                restarted.close()
        finally:
            database.close()


def test_enqueue_army_decode_only_inherits_snapshot_after_restart(
    database_url: str, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, "#DECODE-RESTART", 1, "a" * 64
                )
                _sweep_with_members(connection, [player_id])
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                generation = connection.execute(
                    """
                    SELECT id, snapshot_manifest_id, army_manifest_id
                    FROM boundary_publication_generations
                    WHERE generation = 1
                    """
                ).fetchone()
                assert generation is not None
                snapshot_manifest = database._freeze_boundary_manifest(
                    connection,
                    generation_id=int(generation[0]),
                    artifact_kind="snapshot",
                )
                army_manifest = database._freeze_boundary_manifest(
                    connection, generation_id=int(generation[0]), artifact_kind="army"
                )
                snapshot_id = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version,
                        ordering_rule_version, freshness_rule_version, state,
                        measured_coverage, stale_entry_count,
                        eligible_population_count, included_entry_count,
                        fresh_entry_count, input_hash
                    ) VALUES ('frozen', %s, 1, 'order', 'freshness', 'published',
                              1, 0, 1, 1, 1, repeat('b', 64))
                    RETURNING id
                    """,
                    (BOUNDARY,),
                ).fetchone()[0]
                snapshot_identity = database._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation[0]),
                    artifact_kind="analytics",
                    manifest_id=int(snapshot_manifest[0]),
                    input_hash="b" * 64,
                    source_identity={"snapshot_id": int(snapshot_id)},
                )
                army_identity = database._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation[0]),
                    artifact_kind="army",
                    manifest_id=int(army_manifest[0]),
                    input_hash="c" * 64,
                    source_identity={"day": "decode-restart"},
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_manifest_id = %s, snapshot_state = 'published',
                        snapshot_id = %s, snapshot_input_hash = %s,
                        snapshot_analytics_publication_id = %s,
                        army_manifest_id = %s, army_state = 'published',
                        army_input_hash = %s, army_publication_id = %s
                    WHERE id = %s
                    """,
                    (
                        snapshot_manifest[0],
                        snapshot_id,
                        "b" * 64,
                        snapshot_identity,
                        army_manifest[0],
                        "c" * 64,
                        army_identity,
                        generation[0],
                    ),
                )
                monkeypatch.setattr(
                    database,
                    "_boundary_army_manifest_needs_correction",
                    lambda *_args, **_kwargs: True,
                )
                database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)
                deferred = connection.execute(
                    """
                    SELECT snapshot_state, snapshot_id, snapshot_input_hash,
                           snapshot_manifest_id, snapshot_analytics_publication_id,
                           affected_artifacts
                    FROM boundary_publication_generations
                    WHERE generation = 2
                    """
                ).fetchone()
                assert deferred == ("pending", None, None, None, None, ["army"])
                assert (
                    connection.execute(
                        """
                        SELECT count(*)
                        FROM python_processing_jobs
                        WHERE work_type = 'build_snapshot'
                          AND input_json->>'generation' = '2'
                        """
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_corrections"
                    ).fetchone()[0]
                    == 1
                )
            database.close()
            restarted = Database(connection_info)
            try:
                restarted.reevaluate_boundary_publications()
                with restarted.pool.connection() as connection:
                    inherited = connection.execute(
                        """
                        SELECT snapshot_state, snapshot_id, snapshot_input_hash,
                               snapshot_manifest_id,
                               snapshot_analytics_publication_id,
                               affected_artifacts
                        FROM boundary_publication_generations
                        WHERE generation = 2
                        """
                    ).fetchone()
                    assert inherited == (
                        "published",
                        snapshot_id,
                        "b" * 64,
                        snapshot_manifest[0],
                        snapshot_identity,
                        ["army"],
                    )
                    assert (
                        connection.execute(
                            """
                            SELECT count(*)
                            FROM python_processing_jobs
                            WHERE work_type = 'build_snapshot'
                              AND input_json->>'generation' = '2'
                            """
                        ).fetchone()[0]
                        == 0
                    )
                    assert (
                        connection.execute(
                            "SELECT count(*) FROM boundary_publication_corrections"
                        ).fetchone()[0]
                        == 1
                    )
            finally:
                restarted.close()
        finally:
            database.close()


def test_boundary_correction_recovery_activates_pending_inputs(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, ranked_id = _player_and_version(
                    connection, "#RECOVER1", 1, "a" * 64
                )
                _sweep_with_members(connection, [player_id])
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=ranked_id,
                    ranked_day_input_hash="a" * 64,
                )
                generation = connection.execute(
                    "SELECT id FROM boundary_publication_generations"
                ).fetchone()[0]
                snapshot_manifest = database._freeze_boundary_manifest(
                    connection, generation_id=int(generation), artifact_kind="snapshot"
                )
                army_manifest = database._freeze_boundary_manifest(
                    connection, generation_id=int(generation), artifact_kind="army"
                )
                snapshot_id = connection.execute(
                    """
                    INSERT INTO leaderboard_snapshots (
                        snapshot_kind, boundary_at, version,
                        ordering_rule_version, freshness_rule_version, state,
                        measured_coverage, stale_entry_count,
                        eligible_population_count, included_entry_count,
                        fresh_entry_count, input_hash
                    ) VALUES ('frozen', %s, 1, 'order', 'freshness', 'published',
                              1, 0, 1, 1, 1, repeat('b', 64))
                    RETURNING id
                    """,
                    (BOUNDARY,),
                ).fetchone()[0]
                snapshot_identity = database._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation),
                    artifact_kind="analytics",
                    manifest_id=int(snapshot_manifest[0]),
                    input_hash="b" * 64,
                    source_identity={"snapshot_id": int(snapshot_id)},
                )
                army_identity = database._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(generation),
                    artifact_kind="army",
                    manifest_id=int(army_manifest[0]),
                    input_hash="c" * 64,
                    source_identity={"day": "recovery"},
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_state = 'published', snapshot_id = %s,
                        snapshot_input_hash = %s,
                        snapshot_manifest_id = %s,
                        snapshot_analytics_publication_id = %s,
                        army_state = 'published', army_input_hash = %s,
                        army_manifest_id = %s, army_publication_id = %s
                    WHERE id = %s
                    """,
                    (
                        snapshot_id,
                        "b" * 64,
                        snapshot_manifest[0],
                        snapshot_identity,
                        "c" * 64,
                        army_manifest[0],
                        army_identity,
                        generation,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO boundary_publication_corrections (
                        boundary_at, source_generation_id, affected_artifacts,
                        pending_inputs, state
                    ) VALUES (%s, %s, ARRAY['army'], '[]'::jsonb, 'pending_inputs')
                    """,
                    (BOUNDARY, generation),
                )
                connection.commit()
            assert database.reevaluate_boundary_publications() == 1
            with database.pool.connection() as connection:
                generations = connection.execute(
                    "SELECT count(*) FROM boundary_publication_generations"
                ).fetchone()[0]
                correction = connection.execute(
                    """
                    SELECT state, generation_id
                    FROM boundary_publication_corrections
                    WHERE source_generation_id = %s
                    """,
                    (generation,),
                ).fetchone()
                assert generations == 2
                assert correction[0] == "active"
                assert correction[1] is not None
                assert (
                    connection.execute(
                        """
                    SELECT count(*)
                    FROM boundary_publication_corrections
                    WHERE state IN ('queued', 'pending_inputs')
                    """
                    ).fetchone()[0]
                    == 0
                )
                database.reevaluate_boundary_publications()
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_generations"
                    ).fetchone()[0]
                    == 2
                )
        finally:
            database.close()


def test_source_correction_marks_both_artifacts_when_one_manifest_is_frozen(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                first = _player_and_version(connection, "#QUEUE1", 1, "a" * 64)
                second = _player_and_version(connection, "#QUEUE2", 1, "b" * 64)
                _sweep_with_members(connection, [first[0], second[0]])
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=first[0],
                    ranked_day_version_id=first[1],
                    ranked_day_input_hash="a" * 64,
                )
                generation = connection.execute(
                    "SELECT id FROM boundary_publication_generations"
                ).fetchone()[0]
                snapshot_manifest = database._freeze_boundary_manifest(
                    connection, generation_id=int(generation), artifact_kind="snapshot"
                )
                assert snapshot_manifest is not None
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_manifest_id = %s, snapshot_state = 'published'
                    WHERE id = %s
                    """,
                    (snapshot_manifest[0], generation),
                )
                corrected = int(
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
                        (first[0], DAY_START, BOUNDARY, "c" * 64, "c" * 64),
                    ).fetchone()[0]
                )
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=first[0],
                    ranked_day_version_id=corrected,
                    ranked_day_input_hash="c" * 64,
                )
                assert connection.execute(
                    """
                    SELECT ranked_day_version_id, ranked_day_input_hash
                    FROM boundary_publication_generation_members
                    WHERE generation_id = %s AND player_id = %s
                    """,
                    (generation, first[0]),
                ).fetchone() == (first[1], "a" * 64)
                correction = connection.execute(
                    """
                    SELECT affected_artifacts, state
                    FROM boundary_publication_corrections
                    WHERE source_generation_id = %s
                    """,
                    (generation,),
                ).fetchone()
                assert correction is not None
                assert tuple(correction) == (["snapshot", "army"], "queued")
        finally:
            database.close()


def test_boundary_correction_coalesces_before_any_manifest_freezes(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                first = _player_and_version(connection, "#COALESCE1", 1, "a" * 64)
                second = _player_and_version(connection, "#COALESCE2", 1, "b" * 64)
                _sweep_with_members(connection, [first[0], second[0]])
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=first[0],
                    ranked_day_version_id=first[1],
                    ranked_day_input_hash="a" * 64,
                )
                corrected = int(
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
                        (first[0], DAY_START, BOUNDARY, "c" * 64, "c" * 64),
                    ).fetchone()[0]
                )
                database._record_boundary_generation(
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=first[0],
                    ranked_day_version_id=corrected,
                    ranked_day_input_hash="c" * 64,
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_generations"
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM boundary_publication_corrections"
                    ).fetchone()[0]
                    == 0
                )
                assert connection.execute(
                    """
                    SELECT ranked_day_version_id, ranked_day_input_hash
                    FROM boundary_publication_generation_members
                    WHERE player_id = %s
                    """,
                    (first[0],),
                ).fetchone() == (corrected, "c" * 64)
        finally:
            database.close()


def test_mixed_boundary_last_unavailable_still_enqueues_army(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
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
                assert (
                    connection.execute(
                        """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'build_army_analytics'
                      AND input_json->>'generation' = '1'
                    """
                    ).fetchone()[0]
                    == 1
                )
                assert (
                    text(
                        connection.execute(
                            """
                        SELECT snapshot_state
                        FROM boundary_publication_generations
                        WHERE generation = 1
                        """
                        ).fetchone()[0]
                    )
                    == "ready"
                )
        finally:
            database.close()


def test_all_unavailable_boundary_publishes_empty_army_with_anchor_metadata(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
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
            claim = database.claim_job(
                owner="all-unavailable-army", job_id=int(army_job[0])
            )
            assert claim is not None
            database.complete_army_analytics(claim)
            with database.pool.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM ranked_day_versions"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM army_analytics_battle_facts"
                    ).fetchone()[0]
                    == 0
                )
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_day_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_season_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_breakdowns"
                ).fetchone()[0] == 0
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
    with domain_database(database_url, include_coordinator=True) as connection_info:
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
