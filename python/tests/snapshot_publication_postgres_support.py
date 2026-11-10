"""Shared setup for snapshot publication postgres tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import _processor, store_observation, text
from psycopg.types.json import Jsonb

from clashlens import boundary
from clashlens.analytics import SNAPSHOT_ORDERING_RULE_VERSION
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


def _profile_body(
    *,
    trophies: int,
    tag: str = "#2PP",
    tier_id: int = 105000036,
    tier_name: str = "Legend I",
) -> bytes:
    payload = json.loads(PROFILE_FIXTURE.read_bytes())
    payload["tag"] = tag
    payload["trophies"] = trophies
    payload["leagueTier"] = {"id": tier_id, "name": tier_name}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _seed_snapshot_job(
    connection_info: str,
    *,
    player_id: int,
    boundary_at: datetime,
    deduplication_key: str = "build_snapshot:test-as-of",
    ranked_day_version_id: int | None = None,
    end_trophies: int | None = None,
    ordering_rule_version: str = SNAPSHOT_ORDERING_RULE_VERSION,
) -> int:
    """``end_trophies``: the seeded Complete day's start and end, with no
    battle between."""
    ranked_day_start = boundary_at - timedelta(days=1)
    with psycopg.connect(connection_info) as connection:
        if ranked_day_version_id is None:
            ranked_day_version_id = connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end, official_season_id,
                    season_day_number, season_anchor_rule_version,
                    reconciliation_rule_version, result_hash, version,
                    state, confidence, input_hash, coverage_complete,
                    start_trophies, final_trophies_before_reset
                ) VALUES (
                    %s, %s, %s, '1783918800', 24, 'legend-season-anchor-v1',
                    'legend-ranked-day-v1', repeat('a', 64), 1,
                    'Complete', 'exact', repeat('b', 64), true, %s, %s
                )
                RETURNING id
                """,
                (player_id, ranked_day_start, boundary_at, end_trophies, end_trophies),
            ).fetchone()[0]
        known_players = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT id FROM players WHERE active
                UNION
                SELECT DISTINCT player_id FROM player_profile_versions
                UNION
                SELECT DISTINCT player_id
                FROM collector_observations
                WHERE endpoint = 'profile' AND player_id IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
        ]
        population_hash = hashlib.sha256(
            json.dumps(known_players, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        sweep_id = connection.execute(
            """
            INSERT INTO collector_reset_sweeps (boundary_at, member_ids)
            VALUES (%s, %s)
            RETURNING id
            """,
            (boundary_at, known_players),
        ).fetchone()[0]
        generation_id = connection.execute(
            """
            INSERT INTO boundary_publication_generations (
                boundary_at, generation, sweep_id, ordering_rule_version,
                freshness_rule_version, expected_population_count,
                expected_population_hash, snapshot_state, army_state,
                membership_rule_version, snapshot_rule_version, army_rule_version,
                target_rule, target_at
            ) VALUES (
                %s, 1, %s, %s,
                'legend-profile-freshness-v1', %s, %s, 'ready', 'pending',
                'active-members-v1', 'legend-analytics-v1', 'army-analytics-v2',
                'boundary-delay-v1', %s
            )
            RETURNING id
            """,
            (
                boundary_at, sweep_id, ordering_rule_version, len(known_players),
                population_hash, boundary_at,
            ),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, status, snapshot_status, army_status
            )
            SELECT %s, player_id, 'terminal', 'complete', 'complete'
            FROM unnest(%s::bigint[]) AS members(player_id)
            """,
            (generation_id, known_players),
        )
        ranked_input_hash = connection.execute(
            "SELECT input_hash FROM ranked_day_versions WHERE id = %s",
            (ranked_day_version_id,),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE boundary_publication_generation_members
            SET ranked_day_version_id = %s, ranked_day_input_hash = %s
            WHERE generation_id = %s AND player_id = %s
            """,
            (ranked_day_version_id, ranked_input_hash, generation_id, player_id),
        )
        connection.execute(
            """
            UPDATE collector_reset_sweeps
            SET membership_captured_at = clock_timestamp()
            WHERE id = %s
            """,
            (sweep_id,),
        )
        connection.execute(
            """
            UPDATE boundary_publication_generations
            SET membership_captured_at = clock_timestamp()
            WHERE id = %s
            """,
            (generation_id,),
        )
        connection.commit()
        fixture_database = Database(connection_info)
        try:
            manifest = boundary._freeze_boundary_manifest(
                connection,
                generation_id=int(generation_id),
                artifact_kind="snapshot",
            )
        finally:
            fixture_database.close()
        assert manifest is not None
        manifest_id, manifest_digest = manifest
        job_id = connection.execute(
            """
            INSERT INTO python_processing_jobs (
                observation_id, work_type, deduplication_key, input_json,
                status, due_at, max_attempts
            ) VALUES (
                NULL, 'build_snapshot', %s, %s,
                'pending', %s, 10
            )
            RETURNING id
            """,
            (
                deduplication_key,
                Jsonb(
                    {
                        "ranked_day_version_id": ranked_day_version_id,
                        "boundary_at": boundary_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "generation": 1,
                        "manifest_id": manifest_id,
                        "manifest_digest": manifest_digest,
                    }
                ),
                boundary_at,
            ),
        ).fetchone()[0]
        connection.commit()
    return int(job_id)


def _process_snapshot_and_analytics(
    connection_info: str,
    database: Database,
    processor: ObservationProcessor,
    snapshot_job_id: int,
    *,
    owner_prefix: str,
) -> tuple[int, int]:
    _prepare_snapshot_correction(
        connection_info, database, snapshot_job_id=snapshot_job_id
    )
    snapshot_result = processor.process_job(
        snapshot_job_id,
        owner=f"{owner_prefix}-snapshot",
    )
    assert snapshot_result is not None
    assert snapshot_result.outcome == "processed"

    with database.pool.connection() as connection:
        snapshot = connection.execute(
            """
            SELECT id, version, input_hash, state
            FROM leaderboard_snapshots
            WHERE snapshot_kind = 'frozen'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
        ).fetchone()
        # The build publishes the board; its statistics follow.
        assert snapshot is not None and text(snapshot[3]) == "published"
        analytics_jobs = connection.execute(
            """
            SELECT id, input_json, deduplication_key, status
            FROM python_processing_jobs
            WHERE work_type = 'build_analytics'
              AND status = 'pending'
              AND input_json->>'snapshot_id' = %s
              AND input_json->>'snapshot_version' = %s
            ORDER BY id
            """,
            (str(snapshot[0]), str(snapshot[1])),
        ).fetchall()

    assert len(analytics_jobs) == 1
    analytics_job_id, analytics_input, analytics_key, analytics_status = analytics_jobs[
        0
    ]
    assert text(analytics_status) == "pending"
    assert analytics_input["snapshot_id"] == snapshot[0]
    assert analytics_input["snapshot_version"] == snapshot[1]
    assert text(analytics_input["snapshot_input_hash"]) == text(snapshot[2])
    assert text(analytics_key).startswith("build_analytics:snapshot:")

    analytics_result = processor.process_job(
        int(analytics_job_id),
        owner=f"{owner_prefix}-analytics",
    )
    assert analytics_result is not None
    assert analytics_result.outcome == "processed"

    with database.pool.connection() as connection:
        published = connection.execute(
            """
            SELECT state
            FROM leaderboard_snapshots
            WHERE id = %s
            """,
            (snapshot[0],),
        ).fetchone()
        summaries = connection.execute(
            """
            SELECT s.lens, count(*), count(b.summary_id)
            FROM analytics_summaries AS s
            LEFT JOIN analytics_breakdowns AS b
              ON b.summary_id = s.id
             AND b.army_archetype = 'Unclassified'
            WHERE s.snapshot_id = %s
            GROUP BY s.lens
            ORDER BY s.lens
            """,
            (snapshot[0],),
        ).fetchall()
    assert published is not None and text(published[0]) == "published"
    assert [(text(row[0]), row[1], row[2]) for row in summaries] == [
        ("defense", 1, 1),
        ("offense", 1, 1),
    ]
    return int(snapshot[0]), int(analytics_job_id)


def _prepare_snapshot_correction(
    connection_info: str, database: Database, *, snapshot_job_id: int
) -> None:
    """Pin a requeued snapshot job to a fresh manifest after publication."""
    with psycopg.connect(connection_info) as connection:
        job = connection.execute(
            """
            SELECT input_json
            FROM python_processing_jobs
            WHERE id = %s AND status = 'pending'
            FOR UPDATE
            """,
            (snapshot_job_id,),
        ).fetchone()
        if job is None or not isinstance(job[0], dict):
            return
        input_json = dict(job[0])
        generation_number = input_json.get("generation")
        boundary_text = input_json.get("boundary_at")
        if generation_number is None or boundary_text is None:
            return
        boundary_at = datetime.fromisoformat(str(boundary_text)).astimezone(UTC)
        generation = connection.execute(
            """
            SELECT id, generation, sweep_id, ordering_rule_version,
                   freshness_rule_version, expected_population_count,
                   expected_population_hash, snapshot_rule_version,
                   army_rule_version, target_rule, target_at, snapshot_state
            FROM boundary_publication_generations
            WHERE boundary_at = %s AND generation = %s
            FOR UPDATE
            """,
            (boundary_at, int(generation_number)),
        ).fetchone()
        if generation is None or text(generation[11]) != "published":
            return
        new_generation = connection.execute(
            """
            INSERT INTO boundary_publication_generations (
                boundary_at, generation, sweep_id, ordering_rule_version,
                freshness_rule_version, expected_population_count,
                expected_population_hash, snapshot_state, army_state,
                membership_rule_version, snapshot_rule_version, army_rule_version,
                target_rule, target_at, supersedes_id, source_generation_id,
                correction_state, affected_artifacts
            )
            SELECT boundary_at, generation + 1, sweep_id,
                   ordering_rule_version, freshness_rule_version,
                   expected_population_count, expected_population_hash,
                   'ready', 'pending', membership_rule_version,
                   snapshot_rule_version, army_rule_version, target_rule,
                   target_at, id, id, 'active', ARRAY['snapshot']::text[]
            FROM boundary_publication_generations
            WHERE id = %s
            RETURNING id, generation
            """,
            (generation[0],),
        ).fetchone()
        assert new_generation is not None
        new_generation_id, new_generation_number = (
            int(value) for value in new_generation
        )
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            )
            SELECT %s, player_id, ranked_day_version_id,
                   ranked_day_input_hash, status, snapshot_status, army_status
            FROM boundary_publication_generation_members
            WHERE generation_id = %s
            """,
            (new_generation_id, generation[0]),
        )
        manifest = boundary._freeze_boundary_manifest(connection,
            generation_id=new_generation_id,
            artifact_kind="snapshot",
        )
        assert manifest is not None
        manifest_id, manifest_digest = manifest
        connection.execute(
            """
            UPDATE python_processing_jobs
            SET input_json = input_json || %s::jsonb,
                updated_at = clock_timestamp()
            WHERE id = %s AND status = 'pending'
            """,
            (
                Jsonb(
                    {
                        "generation": new_generation_number,
                        "manifest_id": manifest_id,
                        "manifest_digest": manifest_digest,
                    }
                ),
                snapshot_job_id,
            ),
        )
        connection.commit()


def _process_profile(
    connection_info: str,
    archive_server,
    *,
    occurrence_key: str,
    tag: str,
    trophies: int,
    observed_at: datetime,
    tier_id: int = 105000036,
    tier_name: str = "Legend I",
) -> int:
    observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="profile",
        body=_profile_body(
            trophies=trophies,
            tag=tag,
            tier_id=tier_id,
            tier_name=tier_name,
        ),
        observed_at=observed_at,
        normalized_tag=tag,
    )
    database, processor = _processor(connection_info, archive_server)
    try:
        result = processor.process_job(job_id, owner=f"{occurrence_key}-worker")
        assert result is not None and result.outcome == "processed"
    finally:
        database.close()
    return observation_id
