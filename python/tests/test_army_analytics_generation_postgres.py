from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain_test_support import domain_database

from clashlens.db import Database

DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)
SEASON_ID = "1783918800"


def test_enqueue_uses_global_max_id_for_day_generation(database_url: str) -> None:
    """Interleaved per-player versions must enqueue a new build job.

    The generation must change on every newly completed ranked_day_versions row
    for that day, not only when the per-player version increases. A correction
    with a lower per-player version but a higher global id must still bump
    the deduplication key.
    """
    with domain_database(database_url) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                # Two distinct players.
                player_a = connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true) RETURNING id"
                ).fetchone()[0]
                player_b = connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#8PP', true) RETURNING id"
                ).fetchone()[0]

                def insert_version(player_id: int, version: int) -> int:
                    return int(
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
                                'legend-ranked-day-v1', %s, %s,
                                'Complete', 'exact', repeat('b', 64), true, true, 6000
                            ) RETURNING id
                            """,
                            (
                                player_id,
                                DAY_START,
                                DAY_START + timedelta(days=1),
                                SEASON_ID,
                                str(version) * 64,
                                version,
                            ),
                        ).fetchone()[0]
                    )

                # Player A v1, then player B v5 creates higher per-player version.
                insert_version(player_a, 1)
                insert_version(player_b, 5)
                database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)
                connection.commit()
                first_keys = [
                    row[0]
                    for row in connection.execute(
                        "SELECT deduplication_key FROM python_processing_jobs WHERE work_type='build_army_analytics' ORDER BY id"
                    ).fetchall()
                ]
                assert len(first_keys) == 1

                # Correction for player A with lower per-player version (2 < 5) but higher global id.
                insert_version(player_a, 2)
                database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)
                connection.commit()
                second_keys = [
                    row[0]
                    for row in connection.execute(
                        "SELECT deduplication_key FROM python_processing_jobs WHERE work_type='build_army_analytics' ORDER BY id"
                    ).fetchall()
                ]
                # Must have enqueued a distinct second job.
                assert len(second_keys) == 2
                assert second_keys[0] != second_keys[1]
                # Second generation must reflect the new max id.
                max_id = connection.execute(
                    "SELECT max(id) FROM ranked_day_versions WHERE ranked_day_start=%s AND state='Complete' AND coverage_complete",
                    (DAY_START,),
                ).fetchone()[0]
                assert str(max_id) in second_keys[1]

                # No-op when no completed versions exists for the day.
                empty_day = DAY_START + timedelta(days=7)
                database._enqueue_army_analytics(connection, ranked_day_start=empty_day)
                connection.commit()
                assert (
                    connection.execute(
                        "SELECT count(*) FROM python_processing_jobs WHERE work_type='build_army_analytics'"
                    ).fetchone()[0]
                    == 2
                )
        finally:
            database.close()
