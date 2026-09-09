from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from domain_test_support import domain_database, store_observation
from test_domain_processing_postgres import _processor

from clashlens.db import Database

BATTLE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKINGS = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"
OBSERVED_AT = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)


def _store_pair(connection_info: str, archive_server) -> None:
    store_observation(
        connection_info,
        archive_server,
        occurrence_key="toggle-battle",
        endpoint="battle_log",
        body=BATTLE.read_bytes(),
        observed_at=OBSERVED_AT,
        normalized_tag="#2PP",
    )
    store_observation(
        connection_info,
        archive_server,
        occurrence_key="toggle-ranking",
        endpoint="global_player_rankings",
        body=RANKINGS.read_bytes(),
        observed_at=OBSERVED_AT,
        normalized_tag=None,
    )


def test_player_discovery_enabled_by_default_enqueues_outside_profiles(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        _store_pair(connection_info, archive_server)
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_once(owner="toggle-battle") is not None
            assert processor.process_once(owner="toggle-ranking") is not None
            with database.pool.connection() as connection:
                jobs = connection.execute(
                    "SELECT count(*) FROM collector_jobs WHERE work_type = 'discovery_profile'"
                ).fetchone()[0]
                entries = connection.execute(
                    "SELECT count(DISTINCT player_id) FROM official_top200_entries"
                ).fetchone()[0]
            assert jobs == 201
            assert entries == 200
        finally:
            database.close()


def test_player_discovery_disabled_retains_evidence_without_enqueue(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        _store_pair(connection_info, archive_server)
        database, processor = _processor(
            connection_info,
            archive_server,
            database_factory=lambda info: Database(
                info, player_discovery_enabled=False
            ),
        )
        try:
            assert processor.process_once(owner="toggle-battle") is not None
            assert processor.process_once(owner="toggle-ranking") is not None
            with database.pool.connection() as connection:
                jobs = connection.execute(
                    "SELECT count(*) FROM collector_jobs WHERE work_type = 'discovery_profile'"
                ).fetchone()[0]
                discoveries = connection.execute(
                    "SELECT count(*) FROM known_player_discoveries"
                ).fetchone()[0]
                entries = connection.execute(
                    "SELECT count(DISTINCT player_id) FROM official_top200_entries"
                ).fetchone()[0]
                battles = connection.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0]
                active = connection.execute(
                    "SELECT count(*) FROM players WHERE active"
                ).fetchone()[0]
                inactive_outside = connection.execute(
                    "SELECT count(*) FROM players WHERE NOT active"
                ).fetchone()[0]
            assert jobs == 0
            assert discoveries >= 201
            assert entries == 200
            assert battles >= 1
            assert active == 0
            assert inactive_outside > 0
        finally:
            database.close()
