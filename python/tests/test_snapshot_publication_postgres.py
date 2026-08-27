from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from psycopg.types.json import Jsonb

from clashlens.analytics import deterministic_tag_hash
from clashlens.api_db import ApiDatabase
from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"


def _processor(
    connection_info: str, archive_server
) -> tuple[Database, ObservationProcessor]:
    database = Database(connection_info)
    processor = ObservationProcessor(
        database,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )
    return database, processor


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
) -> int:
    ranked_day_start = boundary_at - timedelta(days=1)
    with psycopg.connect(connection_info) as connection:
        if ranked_day_version_id is None:
            ranked_day_version_id = connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end, official_season_id,
                    season_day_number, season_anchor_rule_version,
                    reconciliation_rule_version, result_hash, version,
                    state, confidence, input_hash
                ) VALUES (
                    %s, %s, %s, '1783918800', 24, 'legend-season-anchor-v1',
                    'legend-ranked-day-v1', repeat('a', 64), 1,
                    'Complete', 'exact', repeat('b', 64)
                )
                RETURNING id
                """,
                (player_id, ranked_day_start, boundary_at),
            ).fetchone()[0]
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
              AND state = 'building'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
        ).fetchone()
        assert snapshot is not None
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


def _process_malformed_profile(
    connection_info: str,
    archive_server,
    *,
    occurrence_key: str,
    tag: str,
    observed_at: datetime,
) -> int:
    observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="profile",
        body=b"not-json",
        observed_at=observed_at,
        normalized_tag=tag,
    )
    database, processor = _processor(connection_info, archive_server)
    try:
        result = processor.process_job(job_id, owner=f"{occurrence_key}-worker")
        assert result is not None and result.outcome == "failed"
        assert result.category == "malformed_json"
    finally:
        database.close()
    return observation_id


def _process_rankings(
    connection_info: str,
    archive_server,
    *,
    occurrence_key: str,
    observed_at: datetime,
    first_tag: str = "#22",
) -> None:
    ranking_fixture = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
    payload = json.loads(ranking_fixture.read_bytes())
    original = payload["items"][0]["tag"]
    payload["items"][0]["tag"] = first_tag
    for item in payload["items"][1:]:
        if item["tag"] == first_tag:
            item["tag"] = original
            break
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    _observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="global_player_rankings",
        body=body,
        observed_at=observed_at,
        normalized_tag=None,
    )
    database, processor = _processor(connection_info, archive_server)
    try:
        result = processor.process_job(job_id, owner=f"{occurrence_key}-worker")
        assert result is not None and result.outcome == "processed"
    finally:
        database.close()


def _snapshot_insert_calls(connection_info: str) -> int | None:
    try:
        with psycopg.connect(connection_info) as connection:
            return int(
                connection.execute(
                    """
                    SELECT COALESCE(sum(calls), 0)::bigint
                    FROM pg_stat_statements
                    WHERE query ILIKE '%INSERT INTO leaderboard_snapshot_entries%'
                    """
                ).fetchone()[0]
            )
    except (
        psycopg.errors.ObjectNotInPrerequisiteState,
        psycopg.errors.UndefinedTable,
    ):
        return None


def _publish_snapshot_population(
    connection_info: str,
    archive_server,
    count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, int]:
    observation_id, _job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=f"snapshot-set-based-{count}",
        endpoint="profile",
        body=_profile_body(trophies=6000),
        observed_at=datetime(2026, 8, 5, 4, tzinfo=UTC),
        normalized_tag="#SEED",
    )
    with psycopg.connect(connection_info) as connection:
        player_ids = [
            int(row[0])
            for row in connection.execute(
                """
                INSERT INTO players (normalized_tag, active)
                SELECT '#S' || %s::text || lpad(g::text, 6, '0'), false
                FROM generate_series(1, %s) AS g
                RETURNING id
                """,
                (count, count),
            ).fetchall()
        ]
        boundary = datetime(2026, 8, 5, 5, tzinfo=UTC) + timedelta(days=count)
        entries = [
            {
                "player_id": player_id,
                "profile_version_id": None,
                "trophies": 6000 - position,
                "observation_id": observation_id,
                "observed_at": boundary,
                "age_seconds": 0,
                "freshness": "fresh",
                "confidence": "confirmed",
                "tie_hash": f"{position:064x}",
                "official": None,
                "tag": f"#S{position:06d}",
            }
            for position, player_id in enumerate(player_ids, start=1)
        ]
        quality = {
            "eligible_population_count": count,
            "included_entry_count": count,
            "stale_entry_count": 0,
            "fresh_entry_count": count,
            "excluded_missing_count": 0,
            "excluded_unavailable_count": 0,
            "excluded_invalid_count": 0,
            "excluded_malformed_count": 0,
            "excluded_partial_count": 0,
            "excluded_inconsistent_count": 0,
            "excluded_conflicting_count": 0,
        }
        calls = [0]
        original_execute = psycopg.Cursor.execute

        def counted_execute(cursor, *args, **kwargs):
            calls[0] += 1
            return original_execute(cursor, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", counted_execute)
        database = Database(connection_info)
        try:
            before_pg = _snapshot_insert_calls(connection_info)
            before_app = calls[0]
            database._publish_snapshot_kind(
                connection,
                snapshot_kind="frozen",
                boundary_at=boundary,
                ranked_day_version_id=None,
                entries=entries,
                coverage=1.0,
                quality=quality,
                input_hash=f"{count:064x}",
                publish=False,
            )
            connection.commit()
        finally:
            database.close()
        after_pg = _snapshot_insert_calls(connection_info)
        return (
            calls[0] - before_app,
            None if before_pg is None or after_pg is None else after_pg - before_pg,
        )


def test_snapshot_entry_statements_are_bounded_as_population_grows(
    database_url: str,
    archive_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        small_app, small_postgres = _publish_snapshot_population(
            connection_info, archive_server, 2, monkeypatch
        )
        large_app, large_postgres = _publish_snapshot_population(
            connection_info, archive_server, 5, monkeypatch
        )
        assert small_app <= 10 and large_app <= 10
        assert large_app <= small_app + 1
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM leaderboard_snapshot_entries"
                ).fetchone()[0]
                == 7
            )
        if small_postgres is not None:
            assert small_postgres <= 1
        if large_postgres is not None:
            assert large_postgres <= 1
        if small_postgres is None or large_postgres is None:
            pytest.skip(
                "pg_stat_statements is unavailable; PostgreSQL statement ceilings not asserted"
            )
        assert large_postgres <= small_postgres + 1


def test_snapshot_uses_only_profile_evidence_observed_at_or_before_boundary(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        old_observation_id, old_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="snapshot-as-of-old",
            endpoint="profile",
            body=_profile_body(trophies=6123),
            observed_at=boundary - timedelta(hours=1),
            normalized_tag="#2PP",
        )
        _future_observation_id, future_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="snapshot-as-of-future",
            endpoint="profile",
            body=_profile_body(trophies=7000),
            observed_at=boundary + timedelta(hours=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert (
                processor.process_job(old_job_id, owner="snapshot-as-of-old")
                is not None
            )
            assert (
                processor.process_job(future_job_id, owner="snapshot-as-of-future")
                is not None
            )
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            snapshot_job_id = _seed_snapshot_job(
                connection_info,
                player_id=player_id,
                boundary_at=boundary,
            )

            _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="snapshot-as-of-builder",
            )
            with database.pool.connection() as connection:
                entry = connection.execute(
                    """
                    SELECT e.trophies, e.trophy_observation_id,
                           e.trophy_observed_at, e.observation_age_seconds,
                           e.freshness
                    FROM leaderboard_snapshot_entries AS e
                    JOIN leaderboard_snapshots AS s ON s.id = e.snapshot_id
                    WHERE s.snapshot_kind = 'frozen'
                      AND s.state = 'published'
                    """
                ).fetchone()
            assert entry is not None
            assert entry[0] == 6123
            assert entry[1] == old_observation_id
            assert entry[2] == boundary - timedelta(hours=1)
            assert entry[3] == 3600
            assert text(entry[4]) == "stale"
        finally:
            database.close()


def test_snapshot_orders_with_stable_hash_and_persists_temporal_provenance(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        old_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-old",
            tag="#2PP",
            trophies=6123,
            observed_at=boundary - timedelta(hours=1),
        )
        fresh_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-fresh",
            tag="#28",
            trophies=6123,
            observed_at=boundary - timedelta(seconds=30),
        )
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-post-boundary",
            tag="#2PP",
            trophies=7000,
            observed_at=boundary + timedelta(hours=1),
            tier_id=105000035,
            tier_name="Legend II",
        )
        _process_rankings(
            connection_info,
            archive_server,
            occurrence_key="snapshot-provenance-official",
            observed_at=boundary - timedelta(minutes=2),
            first_tag="#2PP",
        )
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
        snapshot_job_id = _seed_snapshot_job(
            connection_info,
            player_id=player_id,
            boundary_at=boundary,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            first_snapshot_id, first_analytics_job_id = _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="snapshot-provenance",
            )
            with database.pool.connection() as connection:
                snapshots = connection.execute(
                    """
                    SELECT id, snapshot_kind, boundary_at, version,
                           correction_of_id, ordering_rule_version,
                           freshness_rule_version, input_hash,
                           eligible_population_count, included_entry_count,
                           stale_entry_count, fresh_entry_count,
                           excluded_missing_count, excluded_invalid_count,
                           excluded_malformed_count, excluded_conflicting_count,
                           measured_coverage
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind
                    """,
                    (boundary,),
                ).fetchall()
                entries = connection.execute(
                    """
                    SELECT s.snapshot_kind, e.position, p.normalized_tag,
                           e.trophies, e.trophy_observation_id,
                           e.profile_observation_id, e.profile_observed_at,
                           e.profile_age_seconds, e.profile_freshness,
                           e.profile_confidence, e.tie_hash,
                           e.official_rank, e.official_rank_version_id,
                           e.official_rank_observed_at
                    FROM leaderboard_snapshot_entries AS e
                    JOIN leaderboard_snapshots AS s ON s.id = e.snapshot_id
                    JOIN players AS p ON p.id = e.player_id
                    WHERE s.boundary_at = %s AND s.snapshot_kind = 'frozen'
                    ORDER BY e.position
                    """,
                    (boundary,),
                ).fetchall()
            assert len(snapshots) == 2
            assert [text(row[5]) for row in snapshots] == [
                "tracked-player-order-v1",
                "tracked-player-order-v1",
            ]
            for row in snapshots:
                assert text(row[6]) == "profile-freshness-10m-v1"
                assert len(row[7]) == 64
                assert row[8:16] == (2, 2, 1, 1, 0, 0, 0, 0)
                assert row[16] == 1
            assert {text(row[2]) for row in entries} == {"#2PP", "#28"}
            expected_order = [
                tag
                for tag, _observation_id in sorted(
                    (("#2PP", old_id), ("#28", fresh_id)),
                    key=lambda item: (-6123, deterministic_tag_hash(item[0]), item[0]),
                )
            ]
            assert [text(row[2]) for row in entries] == expected_order
            by_tag = {text(row[2]): row for row in entries}
            assert (
                by_tag["#2PP"][4],
                by_tag["#2PP"][5],
                by_tag["#2PP"][6],
                by_tag["#2PP"][7],
                text(by_tag["#2PP"][8]),
                text(by_tag["#2PP"][9]),
            ) == (
                old_id,
                old_id,
                boundary - timedelta(hours=1),
                3600,
                "stale",
                "confirmed",
            )
            assert (
                by_tag["#28"][4],
                by_tag["#28"][5],
                by_tag["#28"][6],
                by_tag["#28"][7],
                text(by_tag["#28"][8]),
                text(by_tag["#28"][9]),
            ) == (
                fresh_id,
                fresh_id,
                boundary - timedelta(seconds=30),
                30,
                "fresh",
                "confirmed",
            )
            assert text(by_tag["#2PP"][10]) == deterministic_tag_hash("#2PP")
            assert text(by_tag["#28"][10]) == deterministic_tag_hash("#28")
            assert by_tag["#2PP"][11] == 1
            assert by_tag["#2PP"][13] == boundary - timedelta(minutes=2)

            database.requeue_completed_job(snapshot_job_id)
            replay = processor.process_job(snapshot_job_id, owner="snapshot-replay")
            assert replay is not None and replay.outcome == "processed"
            with database.pool.connection() as connection:
                replay_counts = connection.execute(
                    """
                    SELECT count(*),
                           (SELECT count(*) FROM leaderboard_snapshot_entries)
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    """,
                    (boundary,),
                ).fetchone()
            assert replay_counts == (2, 4)

            _process_profile(
                connection_info,
                archive_server,
                occurrence_key="snapshot-provenance-after-boundary",
                tag="#28",
                trophies=9000,
                observed_at=boundary + timedelta(hours=2),
            )
            database.requeue_completed_job(snapshot_job_id)
            post_boundary = processor.process_job(
                snapshot_job_id, owner="snapshot-post-boundary-replay"
            )
            assert post_boundary is not None and post_boundary.outcome == "processed"
            with database.pool.connection() as connection:
                post_hashes = connection.execute(
                    """
                    SELECT snapshot_kind, input_hash
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind
                    """,
                    (boundary,),
                ).fetchall()
            assert [row[1] for row in post_hashes] == [snapshots[0][7], snapshots[1][7]]

            _process_profile(
                connection_info,
                archive_server,
                occurrence_key="snapshot-provenance-late-correction",
                tag="#28",
                trophies=6500,
                observed_at=boundary - timedelta(seconds=1),
            )
            database.requeue_completed_job(snapshot_job_id)
            corrected_snapshot_id, corrected_analytics_job_id = (
                _process_snapshot_and_analytics(
                    connection_info,
                    database,
                    processor,
                    snapshot_job_id,
                    owner_prefix="snapshot-correction",
                )
            )
            with database.pool.connection() as connection:
                corrected_rows = connection.execute(
                    """
                    SELECT id, snapshot_kind, version, correction_of_id, input_hash
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind, version
                    """,
                    (boundary,),
                ).fetchall()
            assert len(corrected_rows) == 4
            assert corrected_rows[0][3] is None
            assert corrected_rows[1][3] == corrected_rows[0][0]
            assert corrected_rows[1][4] != corrected_rows[0][4]
            assert corrected_rows[2][3] is None
            assert corrected_rows[3][3] == corrected_rows[2][0]
            assert corrected_rows[3][4] != corrected_rows[2][4]
            assert all(text(row[1]) in {"frozen", "live"} for row in corrected_rows)
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                )
                == 1
            )
            assert (
                database.scalar(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_analytics'"
                )
                == 2
            )
            assert corrected_snapshot_id != first_snapshot_id
            assert corrected_analytics_job_id != first_analytics_job_id
        finally:
            database.close()


def test_snapshot_quality_counts_and_reader_ignore_building_candidate(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-fresh",
            tag="#2PP",
            trophies=6100,
            observed_at=boundary - timedelta(minutes=1),
        )
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-stale",
            tag="#28",
            trophies=6099,
            observed_at=boundary - timedelta(hours=1),
        )
        invalid_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-invalid",
            tag="#29",
            trophies=6098,
            observed_at=boundary - timedelta(minutes=2),
        )
        conflict_id = _process_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-conflict",
            tag="#2L",
            trophies=6097,
            observed_at=boundary - timedelta(minutes=3),
            tier_id=999999999,
            tier_name="Unexpected Tier",
        )
        malformed_id = _process_malformed_profile(
            connection_info,
            archive_server,
            occurrence_key="snapshot-quality-malformed",
            tag="#2Y",
            observed_at=boundary - timedelta(minutes=4),
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE player_profile_versions SET eligibility_state = 'uncertain' WHERE observation_id = %s",
                (invalid_id,),
            )
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                VALUES ('#2U', true, 'eligible')
                """
            )
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
            connection.commit()
        snapshot_job_id = _seed_snapshot_job(
            connection_info,
            player_id=player_id,
            boundary_at=boundary,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="snapshot-quality",
            )
            with database.pool.connection() as connection:
                quality = connection.execute(
                    """
                    SELECT eligible_population_count, included_entry_count,
                           stale_entry_count, fresh_entry_count,
                           excluded_missing_count, excluded_invalid_count,
                           excluded_malformed_count, excluded_conflicting_count,
                           measured_coverage
                    FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND state = 'published'
                    """
                ).fetchone()
                assert quality is not None
                assert quality[:8] == (2, 2, 1, 1, 1, 1, 1, 1)
                assert quality[8] == 1
                old_snapshot = connection.execute(
                    """
                    SELECT id, boundary_at, version, state
                    FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND state = 'published'
                    ORDER BY version DESC LIMIT 1
                    """
                ).fetchone()
                assert old_snapshot is not None
                connection.commit()
            _process_profile(
                connection_info,
                archive_server,
                occurrence_key="snapshot-quality-late-correction",
                tag="#28",
                trophies=6101,
                observed_at=boundary - timedelta(seconds=30),
            )
            database.requeue_completed_job(snapshot_job_id)
            correction = processor.process_job(
                snapshot_job_id,
                owner="snapshot-quality-correction",
            )
            assert correction is not None and correction.outcome == "processed"
            with database.pool.connection() as connection:
                candidates = connection.execute(
                    """
                    SELECT version, state, correction_of_id
                    FROM leaderboard_snapshots
                    WHERE snapshot_kind = 'frozen' AND boundary_at = %s
                    ORDER BY version
                    """,
                    (boundary,),
                ).fetchall()
            assert len(candidates) == 2
            assert text(candidates[0][1]) == "published"
            assert text(candidates[1][1]) == "building"
            assert candidates[1][2] == old_snapshot[0]
            api = ApiDatabase(connection_info)
            try:
                frozen = api.get_frozen_leaderboard(limit=10)
            finally:
                api.close()
            assert frozen is not None
            assert frozen["boundary_at"] == boundary.isoformat()
            assert frozen["version"] == old_snapshot[2]
            assert frozen["entries"]
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="published leaderboard snapshots are immutable",
            ):
                with database.pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            "UPDATE leaderboard_snapshots SET boundary_at = boundary_at + interval '1 second' WHERE id = %s",
                            (old_snapshot[0],),
                        )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="leaderboard snapshot entries are immutable",
            ):
                with database.pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            """
                            UPDATE leaderboard_snapshot_entries
                            SET trophies = trophies + 1
                            WHERE snapshot_id = %s AND position = 1
                            """,
                            (old_snapshot[0],),
                        )
            with psycopg.connect(connection_info) as connection:
                malformed_outcome = connection.execute(
                    """
                    SELECT outcome FROM observation_processing_outcomes
                    WHERE observation_id = %s
                    """,
                    (malformed_id,),
                ).fetchone()[0]
                assert text(malformed_outcome) == "malformed"
                conflict_state = connection.execute(
                    """
                    SELECT source_contract_state FROM player_profile_versions
                    WHERE observation_id = %s
                    """,
                    (conflict_id,),
                ).fetchone()[0]
                assert text(conflict_state) == "conflict"
        finally:
            database.close()
