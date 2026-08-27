from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.types.json import Jsonb

from clashlens.archive import S3ArchiveReader
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


def _job_id(database: Database) -> int:
    with database.pool.connection() as connection:
        return int(
            connection.execute(
                """
                SELECT id FROM python_processing_jobs
                WHERE work_type = 'build_army_analytics'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()[0]
        )


def _fact_insert_calls(connection_info: str) -> int | None:
    try:
        with psycopg.connect(connection_info) as connection:
            return int(
                connection.execute(
                    """
                    SELECT COALESCE(sum(calls), 0)::bigint
                    FROM pg_stat_statements
                    WHERE query ILIKE '%INSERT INTO army_analytics_battle_facts%'
                    """
                ).fetchone()[0]
            )
    except (
        psycopg.errors.ObjectNotInPrerequisiteState,
        psycopg.errors.UndefinedTable,
    ):
        return None


def _build_fact_population(
    database_url: str,
    archive_server,
    count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, int, int, tuple]:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            opponents = ["#8PP", "#9PP"]
            rows = [
                _battle_row(
                    opponent=opponents[index],
                    offset_hours=index + 1,
                    code=None if index == 0 else FIXTURE_CODE,
                    stars=index % 4,
                    destruction=index * 20,
                )
                for index in range(count)
            ]
            _observation, battle_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"army-set-based-{count}",
                endpoint="battle_log",
                body=json.dumps({"items": rows}).encode(),
                observed_at=DAY_START + timedelta(hours=6),
                normalized_tag="#2PP",
            )
            assert processor.process_job(
                battle_job, owner=f"set-based-{count}"
            ).outcome in {"processed", "processed_with_gaps"}
            _mark_day_complete(database)
            with psycopg.connect(connection_info) as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                battle_ids = [
                    int(row[0])
                    for row in connection.execute(
                        """
                        SELECT id FROM legend_battles
                        WHERE attacker_player_id = %s
                        ORDER BY id
                        """,
                        (player_id,),
                    ).fetchall()
                ]
                assert len(battle_ids) == count
                events = [
                    {
                        "battle_id": battle_id,
                        "lens": "offense",
                        "included": True,
                        "battle_timestamp": (
                            DAY_START + timedelta(hours=index + 1)
                        ).isoformat(),
                        "trophy_change": 20,
                        "stars": index % 4,
                        "destruction_percentage": index * 20,
                    }
                    for index, battle_id in enumerate(battle_ids)
                ]
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, version, state, coverage,
                        adjustments, battles, partial_reasons, ranked_day_end,
                        official_season_id, season_day_number, confidence,
                        attack_count, attack_three_star_count, attack_gain,
                        defense_count, defense_three_star_count, defense_loss,
                        net_trophy_change
                    ) VALUES (
                        %s, %s, 1, 'Complete', 'complete', %s, %s, %s, %s,
                        %s, 23, 'exact', %s, %s, %s, 0, 0, 0, %s
                    )
                    """,
                    (
                        player_id,
                        DAY_START,
                        Jsonb([]),
                        Jsonb(events),
                        Jsonb([]),
                        DAY_START + timedelta(days=1),
                        SEASON_ID,
                        count,
                        sum(event["stars"] == 3 for event in events),
                        count * 20,
                        count * 20,
                    ),
                )
                connection.commit()
            calls = [0]
            original_execute = psycopg.Cursor.execute

            def counted_execute(cursor, *args, **kwargs):
                calls[0] += 1
                return original_execute(cursor, *args, **kwargs)

            monkeypatch.setattr(psycopg.Cursor, "execute", counted_execute)
            before_pg = _fact_insert_calls(connection_info)
            before_app = calls[0]
            with database.pool.connection() as connection:
                database._build_army_facts(connection, DAY_START.isoformat())
                connection.commit()
            after_app = calls[0]
            after_pg = _fact_insert_calls(connection_info)

            with psycopg.connect(connection_info) as connection:
                facts = connection.execute(
                    "SELECT count(*) FROM army_analytics_battle_facts WHERE is_current"
                ).fetchone()[0]
                failed = connection.execute(
                    """
                    SELECT decode.id, decode.battle_id
                    FROM battle_army_decodes AS decode
                    WHERE decode.status = 'failed'
                    ORDER BY decode.id
                    LIMIT 1
                    """
                ).fetchone()
                assert failed is not None
                connection.execute(
                    "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                    (failed[0],),
                )
                database._build_army_facts(
                    connection,
                    DAY_START.isoformat(),
                    battle_ids=[int(failed[1])],
                    decode_ids=[int(failed[0])],
                )
                connection.commit()
                failed_fact = connection.execute(
                    """
                    SELECT army_state, home_troops, spells, siege, cc_troops,
                           heroes, unresolved_components, decode_id
                    FROM army_analytics_battle_facts
                    WHERE battle_id = %s AND lens = 'offense' AND is_current
                    """,
                    (failed[1],),
                ).fetchone()
            assert failed_fact is not None
            return (
                after_app - before_app,
                None if before_pg is None or after_pg is None else after_pg - before_pg,
                int(facts),
                failed_fact,
            )
        finally:
            database.close()


def test_army_fact_statements_are_bounded_and_pinned_failed_decodes_survive(
    database_url: str,
    archive_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small_app, small_postgres, small_facts, _small_failed = _build_fact_population(
        database_url, archive_server, 1, monkeypatch
    )
    large_app, large_postgres, large_facts, failed_fact = _build_fact_population(
        database_url, archive_server, 2, monkeypatch
    )
    assert small_app <= 9 and large_app <= 9
    assert large_app <= small_app + 1
    assert small_facts == 1
    assert large_facts == 2
    assert failed_fact[0] == "missing_army_share_code"
    assert failed_fact[1:7] == ([], [], [], [], [], [])
    assert failed_fact[7] is not None
    if small_postgres is not None:
        assert small_postgres <= 1
    if large_postgres is not None:
        assert large_postgres <= 1
    if small_postgres is None or large_postgres is None:
        pytest.skip(
            "pg_stat_statements is unavailable; PostgreSQL statement ceilings not asserted"
        )
    assert large_postgres <= small_postgres + 1


def test_completed_day_publishes_facts_without_legacy_rollups(
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

            analytics_job = _job_id(database)
            with database.pool.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                    ).fetchone()[0]
                    == 1
                )
            assert (
                processor.process_job(analytics_job, owner="army-analytics").outcome
                == "processed"
            )

            with database.pool.connection() as connection:
                facts = connection.execute(
                    """
                    SELECT count(*), count(*) FILTER (WHERE is_current)
                    FROM army_analytics_battle_facts
                    """
                ).fetchone()
                assert facts == (0, 0)
                marker = connection.execute(
                    """
                    SELECT official_season_id, season_day_number
                    FROM army_analytics_completed_days
                    WHERE ranked_day_start = %s
                    """,
                    (DAY_START,),
                ).fetchone()
                assert marker is None
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_day_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_season_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_breakdowns"
                ).fetchone()[0] == 0

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
            corrected_job = _job_id(database)
            assert corrected_job != analytics_job
            assert (
                processor.process_job(corrected_job, owner="corrected").outcome
                == "processed"
            )
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_day_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_season_summaries"
                ).fetchone()[0] == 0
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


def test_army_fact_retries_leave_legacy_rollups_untouched(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _observation, battle_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="army-all-excluded",
                endpoint="battle_log",
                body=json.dumps(
                    {
                        "items": [
                            _battle_row(
                                opponent="#8PP",
                                offset_hours=1,
                                code=None,
                                stars=1,
                                destruction=50,
                            )
                        ]
                    }
                ).encode(),
                observed_at=DAY_START + timedelta(hours=2),
                normalized_tag="#2PP",
            )
            assert (
                processor.process_job(battle_job, owner="all-excluded").outcome
                == "processed"
            )
            _mark_day_complete(database, trophies=6100)
            assert (
                processor.process_job(_job_id(database), owner="first-build").outcome
                == "processed"
            )

            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_day_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_season_summaries"
                ).fetchone()[0] == 0
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
                        'legend-ranked-day-v1', repeat('c', 64), 2,
                        'Complete', 'exact', repeat('d', 64), true, true, 6200
                    )
                    """,
                    (player_id, DAY_START, DAY_START + timedelta(days=1), SEASON_ID),
                )
                database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)
                connection.commit()

            assert (
                processor.process_job(
                    _job_id(database), owner="replacement-build"
                ).outcome
                == "processed"
            )
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_day_summaries"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_season_summaries"
                ).fetchone()[0] == 0
        finally:
            database.close()


def test_army_fact_build_rolls_back_atomically(
    database_url: str, archive_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _observation, battle_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="army-atomic-rollback",
                endpoint="battle_log",
                body=json.dumps(
                    {
                        "items": [
                            _battle_row(
                                opponent="#8PP",
                                offset_hours=1,
                                code=FIXTURE_CODE,
                                stars=3,
                                destruction=100,
                            )
                        ]
                    }
                ).encode(),
                observed_at=DAY_START + timedelta(hours=2),
                normalized_tag="#2PP",
            )
            assert (
                processor.process_job(battle_job, owner="atomic-battle").outcome
                == "processed"
            )
            _mark_day_complete(database)

            def fail_day(*_args, **_kwargs) -> None:
                raise ValueError("dependency_not_ready: forced day failure")

            monkeypatch.setattr(database, "_ensure_army_day_dependency", fail_day)
            result = processor.process_job(_job_id(database), owner="atomic-build")
            assert result.outcome == "retrying"
            with database.pool.connection() as connection:
                assert (
                    connection.execute(
                        "SELECT count(*) FROM army_analytics_day_summaries"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    connection.execute(
                        "SELECT count(*) FROM army_analytics_season_summaries"
                    ).fetchone()[0]
                    == 0
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
                        'clashlens-domain-rules-v1', 'army-analytics-v2'
                    ) RETURNING id
                    """,
                    (
                        json.dumps(
                            {
                                "ranked_day_start": future_day.isoformat(),
                                "official_season_id": SEASON_ID,
                            }
                        ),
                    ),
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
