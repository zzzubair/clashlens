from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from domain_test_support import domain_database, store_observation, text

from clashlens.archive import S3ArchiveReader
from clashlens.catalog import CATALOG_HASH, CATALOG_VERSION
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)
SEASON_ID = "1783918800"
FIXTURE_CODE = "h0p9e14_32d1x53u2x58-1x97s2x2"


def _processor(connection_info: str, archive_server):
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


def _battle_row(
    *,
    opponent: str,
    offset_hours: int,
    code: str | None,
    stars: int,
    destruction: int,
) -> dict:
    row = {
        "battleType": "legend",
        "attack": True,
        "battleTimestamp": (DAY_START + timedelta(hours=offset_hours))
        .isoformat()
        .replace("+00:00", "Z"),
        "stars": stars,
        "destructionPercentage": destruction,
        "opponentPlayerTag": opponent,
        "opponentName": opponent,
        "opponentTownHallLevel": 17,
    }
    if code is not None:
        row["armyShareCode"] = code
    return row


def _store_battles(connection_info: str, archive_server) -> list[int]:
    rows = (
        (
            "#2PP",
            _battle_row(
                opponent="#8PP",
                offset_hours=1,
                code=FIXTURE_CODE,
                stars=3,
                destruction=100,
            ),
        ),
        (
            "#2PP",
            _battle_row(
                opponent="#9PP",
                offset_hours=2,
                code="h0m5p9e14_32d1x53u1x51-2x58-1x97i2x0s2x2",
                stars=2,
                destruction=75,
            ),
        ),
        (
            "#2PP",
            _battle_row(
                opponent="#YPP",
                offset_hours=3,
                code=None,
                stars=1,
                destruction=50,
            ),
        ),
        (
            "#8PY",
            _battle_row(
                opponent="#2PY",
                offset_hours=4,
                code=FIXTURE_CODE,
                stars=0,
                destruction=0,
            ),
        ),
    )
    jobs = []
    for index, (reporting_tag, row) in enumerate(rows):
        _observation, job = store_observation(
            connection_info,
            archive_server,
            occurrence_key=f"army-analytics-{index}",
            endpoint="battle_log",
            body=json.dumps({"items": [row]}).encode(),
            observed_at=DAY_START + timedelta(hours=index + 1, minutes=1),
            normalized_tag=reporting_tag,
        )
        jobs.append(job)
    return jobs


def _mark_day_complete(database: Database, *, trophies: int = 6000) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end,
                official_season_id, season_day_number,
                season_anchor_rule_version, reconciliation_rule_version,
                result_hash, version, state, confidence, input_hash,
                evidence_complete, coverage_complete, start_trophies
            ) VALUES (
                %s, %s, %s, %s, 23, 'legend-season-anchor-v1',
                'legend-ranked-day-v1', repeat('a', 64), 1,
                'Complete', 'exact', repeat('b', 64), true, true, %s
            )
            """,
            (player_id, DAY_START, DAY_START + timedelta(days=1), SEASON_ID, trophies),
        )
        database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)
        connection.commit()


def _job_id(database: Database, kind: str) -> int:
    with database.pool.connection() as connection:
        return int(
            connection.execute(
                """
                SELECT id FROM python_processing_jobs
                WHERE work_type = 'build_army_analytics'
                  AND deduplication_key LIKE %s
                ORDER BY id DESC LIMIT 1
                """,
                (f"build_army_analytics:{kind}:%",),
            ).fetchone()[0]
        )


def test_completed_day_and_season_publish_required_army_metrics(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            for index, job_id in enumerate(
                _store_battles(connection_info, archive_server)
            ):
                assert (
                    processor.process_job(job_id, owner=f"battle-{index}").outcome
                    == "processed"
                )
            _mark_day_complete(database)

            day_job = _job_id(database, "day")
            season_job = _job_id(database, "season")
            assert (
                processor.process_job(day_job, owner="army-day").outcome == "processed"
            )
            assert (
                processor.process_job(season_job, owner="army-season").outcome
                == "processed"
            )

            with database.pool.connection() as connection:
                day = connection.execute(
                    """
                    SELECT id, total_attacks, sample_size, excluded_attacks,
                           excluded_breakdown, decoder_version,
                           catalog_version, exact_trophies
                    FROM army_analytics_day_summaries
                    WHERE is_published AND exact_trophies = -1
                    """
                ).fetchone()
                assert day[1:4] == (4, 3, 1)
                assert day[4] == {"missing_army_share_code": 1}
                assert text(day[6]) == CATALOG_VERSION

                cohort = connection.execute(
                    """
                    SELECT total_attacks, sample_size, excluded_attacks
                    FROM army_analytics_day_summaries
                    WHERE is_published AND exact_trophies = 6000
                    """
                ).fetchone()
                assert cohort == (3, 2, 1)
                assert (
                    connection.execute(
                        """
                    SELECT count(*) FROM army_analytics_day_summaries
                    WHERE is_published
                    """
                    ).fetchone()[0]
                    == 2
                )

                breakdowns = {
                    (text(row[0]), text(row[1]), text(row[2])): row[3:]
                    for row in connection.execute(
                        """
                        SELECT category, typed_id, combination_key,
                               usage_count, usage_rate, star_counts,
                               avg_destruction, hit_rate, hero_typed_id
                        FROM army_analytics_breakdowns
                        WHERE summary_kind = 'day' AND summary_id = %s
                        """,
                        (day[0],),
                    ).fetchall()
                }
                assert breakdowns[("home_troop", "troop:58", None)][0:2] == (
                    3,
                    1,
                )
                siege = breakdowns[("siege", "troop:51", None)]
                assert siege[0] == 1
                assert float(siege[1]) == pytest.approx(1 / 3)
                assert siege[2] == {"0": 0, "1": 0, "2": 1, "3": 0}
                assert float(siege[3]) == 75
                assert float(siege[4]) == 0
                assert breakdowns[("cc_troop", "troop:0", None)][0] == 1
                assert any(key[0] == "hero_pet" for key in breakdowns)
                assert any(key[0] == "hero_equipment" for key in breakdowns)
                assert any(key[0] == "cc_composition" for key in breakdowns)
                conditional = next(
                    values
                    for (category, typed_id, _combo), values in breakdowns.items()
                    if category == "equipment_for_hero" and typed_id == "equipment:14"
                )
                assert conditional[0] == 3
                assert float(conditional[1]) == 1
                assert text(conditional[5]) == "hero:0"

                season = connection.execute(
                    """
                    SELECT total_attacks, sample_size, excluded_attacks
                    FROM army_analytics_season_summaries
                    WHERE is_published AND official_season_id = %s
                      AND exact_trophies = -1
                    """,
                    (SEASON_ID,),
                ).fetchone()
                assert season == (4, 3, 1)

                catalog = connection.execute(
                    "SELECT content_hash FROM unit_catalog_versions WHERE version = %s",
                    (CATALOG_VERSION,),
                ).fetchone()
                assert text(catalog[0]) == CATALOG_HASH

            corrected = _battle_row(
                opponent="#8PP",
                offset_hours=1,
                code="h0p9e14_32d1x53u1x58-1x97s2x2",
                stars=3,
                destruction=100,
            )
            _observation, correction_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="army-analytics-correction",
                endpoint="battle_log",
                body=json.dumps({"items": [corrected]}).encode(),
                observed_at=DAY_START + timedelta(hours=5),
                normalized_tag="#2PP",
            )
            assert (
                processor.process_job(correction_job, owner="correction").outcome
                == "processed"
            )
            corrected_day_job = _job_id(database, "day")
            assert corrected_day_job != day_job
            assert (
                processor.process_job(corrected_day_job, owner="corrected-day").outcome
                == "processed"
            )
            with database.pool.connection() as connection:
                history = connection.execute(
                    """
                    SELECT version, is_published
                    FROM army_analytics_day_summaries
                    WHERE exact_trophies = -1
                    ORDER BY version
                    """
                ).fetchall()
                assert history == [(1, False), (2, True)]
                assert (
                    connection.execute(
                        """
                    SELECT count(*) FROM battle_army_decodes
                    WHERE is_active AND battle_id = (
                        SELECT id FROM legend_battles
                        WHERE attacker_player_id = (
                            SELECT id FROM players WHERE normalized_tag = '#2PP'
                        )
                          AND defender_player_id = (
                            SELECT id FROM players WHERE normalized_tag = '#8PP'
                        )
                    )
                    """
                    ).fetchone()[0]
                    == 1
                )
        finally:
            database.close()


def test_active_or_incomplete_day_is_withheld_and_retried(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            future_day = datetime.now(UTC).replace(
                hour=5, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            with database.pool.connection() as connection:
                job_id = connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json,
                        processing_version, domain_rule_version,
                        analytics_rule_version
                    ) VALUES (
                        'build_army_analytics', 'army-future-test', %s,
                        'clashlens-domain-processing-v1',
                        'clashlens-domain-rules-v1', 'legend-analytics-v1'
                    ) RETURNING id
                    """,
                    (json.dumps({"ranked_day_start": future_day.isoformat()}),),
                ).fetchone()[0]
                connection.commit()
            result = processor.process_job(job_id, owner="future")
            assert result.outcome == "retrying"
            assert result.category == "dependency_not_ready"
            with database.pool.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM army_analytics_day_summaries"
                    ).fetchone()[0]
                    == 0
                )
        finally:
            database.close()


def test_army_tables_have_only_required_runtime_privileges(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                privileges = connection.execute(
                    """
                    SELECT
                      has_table_privilege('clashlens_python_worker', 'unit_catalog_versions', 'SELECT'),
                      has_table_privilege('clashlens_python_worker', 'unit_catalog_versions', 'UPDATE'),
                      has_table_privilege('clashlens_python_worker', 'battle_army_decodes', 'INSERT'),
                      has_table_privilege('clashlens_python_worker', 'army_analytics_breakdowns', 'DELETE'),
                      has_table_privilege('clashlens_python_api', 'exact_armies', 'SELECT')
                    """
                ).fetchone()
                assert privileges == (True, False, True, True, False)
        finally:
            database.close()
