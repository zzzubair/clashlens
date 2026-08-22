from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import domain_database, store_observation, text
from psycopg.types.json import Jsonb
from test_snapshot_publication_postgres import (
    _process_profile,
    _process_snapshot_and_analytics,
    _processor,
    _seed_snapshot_job,
)

from clashlens.api_db import ApiDatabase

BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"


def _store_battle_log(
    connection_info: str,
    archive_server,
    *,
    occurrence_key: str,
    tag: str,
    rows: list[dict[str, object]],
    observed_at: datetime,
) -> int:
    _observation_id, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key=occurrence_key,
        endpoint="battle_log",
        body=json.dumps({"items": rows}, separators=(",", ":")).encode("utf-8"),
        observed_at=observed_at,
        normalized_tag=tag,
    )
    return job_id


def test_frozen_snapshot_waits_for_complete_analytics_publication(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="analytics-publication-profile",
            tag="#2PP",
            trophies=6123,
            observed_at=boundary - timedelta(minutes=1),
        )
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]
        snapshot_job_id = _seed_snapshot_job(
            connection_info,
            player_id=player_id,
            boundary_at=boundary,
            deduplication_key="build_snapshot:analytics-publication",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            snapshot_result = processor.process_job(
                snapshot_job_id,
                owner="analytics-publication-snapshot",
            )
            assert snapshot_result is not None
            assert snapshot_result.outcome == "processed"

            with database.pool.connection() as connection:
                snapshots = connection.execute(
                    """
                    SELECT id, snapshot_kind, state, input_hash
                    FROM leaderboard_snapshots
                    WHERE boundary_at = %s
                    ORDER BY snapshot_kind
                    """,
                    (boundary,),
                ).fetchall()
                analytics_jobs = connection.execute(
                    """
                    SELECT id, input_json, deduplication_key, status
                    FROM python_processing_jobs
                    WHERE work_type = 'build_analytics'
                    ORDER BY id
                    """
                ).fetchall()

            by_kind = {text(row[1]): row for row in snapshots}
            assert text(by_kind["frozen"][2]) == "building"
            assert text(by_kind["live"][2]) == "published"
            assert len(analytics_jobs) == 1
            analytics_job_id, analytics_input, analytics_key, analytics_status = (
                analytics_jobs[0]
            )
            assert text(analytics_status) == "pending"
            assert analytics_input["snapshot_id"] == by_kind["frozen"][0]
            assert analytics_input["snapshot_version"] == 1
            assert text(analytics_input["snapshot_input_hash"]) == text(
                by_kind["frozen"][3]
            )
            assert text(analytics_key).startswith("build_analytics:snapshot:")

            api = ApiDatabase(connection_info)
            try:
                assert api.get_frozen_leaderboard(limit=10) is None
            finally:
                api.close()

            analytics_result = processor.process_job(
                int(analytics_job_id),
                owner="analytics-publication-worker",
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
                    (by_kind["frozen"][0],),
                ).fetchone()
                summary_counts = connection.execute(
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
                    (by_kind["frozen"][0],),
                ).fetchall()
            assert published is not None and text(published[0]) == "published"
            assert [(text(row[0]), row[1], row[2]) for row in summary_counts] == [
                ("defense", 1, 1),
                ("offense", 1, 1),
            ]
        finally:
            database.close()


def test_canonical_analytics_keeps_owned_perspectives_and_raw_evidence(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 5, 5, tzinfo=UTC)
    observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="canonical-analytics-attacker-profile",
            tag="#2PP",
            trophies=6200,
            observed_at=boundary - timedelta(minutes=1),
        )
        _process_profile(
            connection_info,
            archive_server,
            occurrence_key="canonical-analytics-defender-profile",
            tag="#8PP",
            trophies=6180,
            observed_at=boundary - timedelta(minutes=1),
        )

        battle = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
        battle["battleTimestamp"] = "2026-08-04T12:00:00Z"
        battle["trophies"] = 6200
        battle["opponentPlayerTag"] = "#8PP"
        battle["opponentName"] = "Synthetic Defender"
        battle["armyShareCode"] = "attacker-share-exact"
        zero_trophy = dict(battle)
        zero_trophy["battleTimestamp"] = "2026-08-04T13:00:00Z"
        zero_trophy["opponentPlayerTag"] = "#9PP"
        zero_trophy["opponentName"] = "Synthetic Zero Trophy Defender"
        zero_trophy["stars"] = 0
        zero_trophy["destructionPercentage"] = 0
        zero_trophy["armyShareCode"] = "zero-trophy-share-exact"
        missing_code = dict(battle)
        missing_code["battleTimestamp"] = "2026-08-04T14:00:00Z"
        missing_code["opponentPlayerTag"] = "#YPP"
        missing_code["opponentName"] = "Synthetic Missing Code Defender"
        del missing_code["armyShareCode"]
        malformed_code = dict(battle)
        malformed_code["battleTimestamp"] = "2026-08-04T15:00:00Z"
        malformed_code["opponentPlayerTag"] = "#9PP"
        malformed_code["opponentName"] = "Synthetic Malformed Code Defender"
        malformed_code["stars"] = "three"
        malformed_code["armyShareCode"] = "malformed-row-code"
        attacker_rows = [battle, zero_trophy, missing_code, malformed_code]

        defender = dict(battle)
        defender["attack"] = False
        defender["trophies"] = 6180
        defender["opponentPlayerTag"] = "#2PP"
        defender["opponentName"] = "Synthetic Attacker"
        defender["armyShareCode"] = "defender-share-conflicting"

        attacker_job = _store_battle_log(
            connection_info,
            archive_server,
            occurrence_key="canonical-analytics-attacker-first",
            tag="#2PP",
            rows=attacker_rows,
            observed_at=observed_at,
        )
        attacker_repeat_job = _store_battle_log(
            connection_info,
            archive_server,
            occurrence_key="canonical-analytics-attacker-repeat",
            tag="#2PP",
            rows=attacker_rows,
            observed_at=observed_at + timedelta(minutes=5),
        )
        defender_job = _store_battle_log(
            connection_info,
            archive_server,
            occurrence_key="canonical-analytics-defender",
            tag="#8PP",
            rows=[defender],
            observed_at=observed_at + timedelta(minutes=6),
        )

        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id, owner in (
                (attacker_job, "canonical-attacker-first"),
                (attacker_repeat_job, "canonical-attacker-repeat"),
                (defender_job, "canonical-defender"),
            ):
                result = processor.process_job(job_id, owner=owner)
                assert result is not None
                assert result.outcome in {"processed", "processed_with_gaps"}

            with database.pool.connection() as connection:
                attacker_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            snapshot_job_id = _seed_snapshot_job(
                connection_info,
                player_id=attacker_id,
                boundary_at=boundary,
                deduplication_key="build_snapshot:canonical-analytics",
            )
            with database.pool.connection() as connection:
                ranked_day_version_id = connection.execute(
                    """
                    SELECT (input_json->>'ranked_day_version_id')::bigint
                    FROM python_processing_jobs
                    WHERE id = %s
                    """,
                    (snapshot_job_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO ranked_day_adjustments (
                        ranked_day_version_id, adjustment_type, amount,
                        evidence_state, rule_version, evidence_json
                    ) VALUES
                        (%s, 'automatic_defense', 7, 'calculated',
                         'canonical-adjustment-test-v1', %s),
                        (%s, 'season_reset', 5000, 'official_rule',
                         'canonical-boundary-test-v1', %s)
                    """,
                    (
                        ranked_day_version_id,
                        Jsonb({"source": "automatic"}),
                        ranked_day_version_id,
                        Jsonb({"source": "boundary"}),
                    ),
                )
                connection.commit()

            snapshot_id, analytics_job_id = _process_snapshot_and_analytics(
                connection_info,
                database,
                processor,
                snapshot_job_id,
                owner_prefix="canonical-analytics",
            )

            with database.pool.connection() as connection:
                summaries = connection.execute(
                    """
                    SELECT id, lens, sample_size, unclassified_count,
                           disagreement_count, missing_code_count,
                           malformed_code_count, measured_coverage,
                           freshness, classification_version,
                           analytics_rule_version, correction_of_id
                    FROM analytics_summaries
                    WHERE snapshot_id = %s
                    ORDER BY lens
                    """,
                    (snapshot_id,),
                ).fetchall()
                breakdowns = connection.execute(
                    """
                    SELECT s.lens, b.army_archetype, b.attack_count,
                           b.three_star_count, b.usage_rate,
                           b.three_star_rate, b.evidence_json
                    FROM analytics_summaries AS s
                    JOIN analytics_breakdowns AS b ON b.summary_id = s.id
                    WHERE s.snapshot_id = %s
                    ORDER BY s.lens
                    """,
                    (snapshot_id,),
                ).fetchall()
                evidence_owners = connection.execute(
                    """
                    SELECT e.perspective, e.army_share_code, p.normalized_tag
                    FROM battle_perspectives AS bp
                    JOIN battle_evidence AS e ON e.id = bp.evidence_id
                    JOIN players AS p ON p.id = e.reporting_player_id
                    WHERE e.army_share_code IN (
                        'attacker-share-exact',
                        'zero-trophy-share-exact',
                        'defender-share-conflicting'
                    )
                    ORDER BY e.perspective, e.army_share_code
                    """
                ).fetchall()
                adjustment_count = connection.execute(
                    """
                    SELECT count(*)
                    FROM ranked_day_adjustments
                    WHERE ranked_day_version_id = %s
                    """,
                    (ranked_day_version_id,),
                ).fetchone()[0]

            assert [(text(row[1]), row[2:7]) for row in summaries] == [
                ("defense", (1, 1, 1, 0, 4)),
                ("offense", (2, 2, 1, 0, 4)),
            ]
            assert all(float(row[7]) == 1.0 for row in summaries)
            assert all(text(row[8]) == "fresh" for row in summaries)
            assert all(
                text(row[9]) == "army-classifier-unavailable-v1" for row in summaries
            )
            assert all(text(row[10]) == "legend-analytics-v1" for row in summaries)
            assert all(row[11] is None for row in summaries)
            assert [
                (
                    text(row[0]),
                    text(row[1]),
                    row[2],
                    row[3],
                    float(row[4]),
                    float(row[5]),
                )
                for row in breakdowns
            ] == [
                ("defense", "Unclassified", 1, 1, 1.0, 1.0),
                ("offense", "Unclassified", 2, 1, 1.0, 0.5),
            ], breakdowns
            assert breakdowns[0][6]["army_share_codes"] == [
                "defender-share-conflicting"
            ]
            assert breakdowns[1][6]["army_share_codes"] == [
                "attacker-share-exact",
                "zero-trophy-share-exact",
            ]
            assert [
                (text(row[0]), text(row[1]), text(row[2])) for row in evidence_owners
            ] == [
                ("attacker", "attacker-share-exact", "#2PP"),
                ("attacker", "zero-trophy-share-exact", "#2PP"),
                ("defender", "defender-share-conflicting", "#8PP"),
            ]
            assert adjustment_count == 2

            before_replay = (
                database.scalar(
                    """
                SELECT count(*)
                FROM leaderboard_snapshots
                """
                ),
                database.scalar("SELECT count(*) FROM leaderboard_snapshot_entries"),
                database.scalar("SELECT count(*) FROM analytics_summaries"),
                database.scalar("SELECT count(*) FROM analytics_breakdowns"),
                database.scalar("SELECT count(*) FROM python_processing_jobs"),
            )
            database.requeue_completed_job(snapshot_job_id)
            snapshot_replay = processor.process_job(
                snapshot_job_id,
                owner="canonical-snapshot-replay",
            )
            assert (
                snapshot_replay is not None and snapshot_replay.outcome == "processed"
            )
            database.requeue_completed_job(analytics_job_id)
            analytics_replay = processor.process_job(
                analytics_job_id,
                owner="canonical-analytics-replay",
            )
            assert (
                analytics_replay is not None and analytics_replay.outcome == "processed"
            )
            after_replay = (
                database.scalar("SELECT count(*) FROM leaderboard_snapshots"),
                database.scalar("SELECT count(*) FROM leaderboard_snapshot_entries"),
                database.scalar("SELECT count(*) FROM analytics_summaries"),
                database.scalar("SELECT count(*) FROM analytics_breakdowns"),
                database.scalar("SELECT count(*) FROM python_processing_jobs"),
            )
            assert after_replay == before_replay

            empty_boundary = boundary + timedelta(days=1)
            empty_snapshot_job_id = _seed_snapshot_job(
                connection_info,
                player_id=attacker_id,
                boundary_at=empty_boundary,
                deduplication_key="build_snapshot:canonical-analytics-empty",
            )
            empty_snapshot_id, _empty_analytics_job_id = (
                _process_snapshot_and_analytics(
                    connection_info,
                    database,
                    processor,
                    empty_snapshot_job_id,
                    owner_prefix="canonical-analytics-empty",
                )
            )
            with database.pool.connection() as connection:
                empty_breakdowns = connection.execute(
                    """
                    SELECT s.lens, s.sample_size, b.usage_rate,
                           b.three_star_rate, b.evidence_json
                    FROM analytics_summaries AS s
                    JOIN analytics_breakdowns AS b ON b.summary_id = s.id
                    WHERE s.snapshot_id = %s
                    ORDER BY s.lens
                    """,
                    (empty_snapshot_id,),
                ).fetchall()
            assert [
                (text(row[0]), row[1], row[2], row[3]) for row in empty_breakdowns
            ] == [
                ("defense", 0, None, None),
                ("offense", 0, None, None),
            ]
            assert all(row[4]["army_share_codes"] == [] for row in empty_breakdowns)
        finally:
            database.close()
