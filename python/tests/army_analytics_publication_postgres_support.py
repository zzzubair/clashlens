"""Shared setup for army analytics publication postgres tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from domain_test_support import (
    enable_direct_army_fixture,
)

from clashlens import boundary_publication
from clashlens.archive import S3ArchiveReader
from clashlens.army_analytics import (
    ArmyAnalyticsSelection,
)
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)


SEASON_ID = "1783918800"


DAY_NUMBER = 23


FIXTURE_CODE = "h0p9e14_32d1x53u2x58-1x97s2x2"


PARTIAL_CODE = "u2x58-3x9999"


DEFENDER_CODE = "u1x51"


def _processor(connection_info: str, archive_server, monkeypatch):
    database = Database(connection_info)
    enable_direct_army_fixture(database, monkeypatch)
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


def _row(attack: bool, opponent: str, code: str, ts: datetime, stars: int, dest: int):
    return {
        "battleType": "legend",
        "attack": attack,
        "battleTime": ts.strftime("%Y%m%dT%H%M%S.000Z"),
        "stars": stars,
        "destructionPercentage": dest,
        "opponentPlayerTag": opponent,
        "opponentName": "Opp",
        "opponentTownHallLevel": 17,
        "armyShareCode": code,
    }


def _event(battle_id: int, lens: str, ts: datetime, stars: int, dest: int, change: int):
    return {
        "battle_id": str(battle_id),
        "lens": lens,
        "battle_timestamp": ts.isoformat().replace("+00:00", "Z"),
        "stars": stars,
        "destruction_percentage": dest,
        "trophy_change": change,
        "opponent": {"tag": "#8PP" if lens == "offense" else "#2PP", "name": "Opp"},
        "included": True,
    }


def _publish_day(
    database: Database,
    player_tag: str,
    events: list[dict],
    *,
    start_trophies: int | None = 6000,
    day_start: datetime = DAY_START,
    day_number: int = DAY_NUMBER,
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (player_tag,)
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
                %s, %s, %s, %s, %s, 'legend-season-anchor-v1',
                'legend-ranked-day-v1', repeat('a', 64), 1,
                'Complete', 'exact', repeat('b', 64), true, true, %s
            )
            """,
            (
                player_id,
                day_start,
                day_start + timedelta(days=1),
                SEASON_ID,
                day_number,
                start_trophies,
            ),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                battles, ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, 1, 'Complete', 'complete', %s, %s, %s, %s)
            """,
            (
                player_id,
                day_start,
                json.dumps(events),
                day_start + timedelta(days=1),
                SEASON_ID,
                day_number,
            ),
        )
        boundary_publication._enqueue_army_analytics(database, connection, ranked_day_start=day_start)


def _army_job(database: Database) -> int:
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


def _selection(**changes) -> ArmyAnalyticsSelection:
    values = {
        "lens": "offense",
        "season": SEASON_ID,
        "start_day": DAY_NUMBER,
        "end_day": DAY_NUMBER,
        "population": "trophies-5000-9000",
        "category": "troops",
        "sort": "usage-rate",
    }
    values.update(changes)
    return ArmyAnalyticsSelection.parse(**values)


def _publish_day_correction(
    database: Database,
    player_tag: str,
    events: list[dict],
    *,
    version: int = 2,
) -> None:
    result_hash = hashlib.sha256(f"result:{version}".encode()).hexdigest()
    input_hash = hashlib.sha256(f"input:{version}".encode()).hexdigest()
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (player_tag,)
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
                %s, %s, %s, %s, %s, 'legend-season-anchor-v1',
                'legend-ranked-day-v1', %s, %s,
                'Complete', 'exact', %s, true, true, 6000
            )
            """,
            (
                player_id,
                DAY_START,
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
                result_hash,
                version,
                input_hash,
            ),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                battles, ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, %s, 'Complete', 'complete', %s, %s, %s, %s)
            """,
            (
                player_id,
                DAY_START,
                version,
                json.dumps(events),
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
            ),
        )
        boundary_publication._enqueue_army_analytics(database, connection, ranked_day_start=DAY_START)
