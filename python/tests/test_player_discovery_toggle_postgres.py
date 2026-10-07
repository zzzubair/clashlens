from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_domain_processing_postgres import _processor

from clashlens.db import DISCOVERY_QUEUE_CAP, Database, enqueue_discovered_players

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
                    "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
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
                    "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
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


def test_discovery_queues_each_new_player_once_and_stops_at_the_cap(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            new_id, known_id, blocked_id, *others = [
                row[0]
                for row in connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    SELECT '#Q' || n, n = 1, CASE n WHEN 1 THEN 'eligible' ELSE 'unknown' END
                    FROM generate_series(0, 503) AS n ORDER BY n RETURNING id
                    """
                ).fetchall()
            ]
            # An older unfinished weekly check makes the database refuse this player.
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                    profile_status, battle_log_status, league_history_status,
                    eligibility_recheck
                )
                SELECT 'discovery_profile', 'ordinary', 'player', id, normalized_tag,
                       now() - interval '8 days', 'discovery-profile:' || id || ':old',
                       'pending', 'not_applicable', 'pending', true
                FROM players WHERE id = %s
                """,
                (blocked_id,),
            )
        options = conninfo_to_dict(connection_info).get("options", "")
        database = Database(
            make_conninfo(connection_info, options=f"{options} -c role=clashlens_python_worker")
        )
        claim = SimpleNamespace(work_type="process_observation")

        def discover(player_ids: list[int]) -> None:
            with database.pool.connection() as connection, connection.transaction():
                enqueue_discovered_players(connection, database, claim, player_ids)

        def queued() -> list[int]:
            with database.pool.connection() as connection:
                return [
                    row[0]
                    for row in connection.execute(
                        "SELECT player_id FROM collector_work"
                        " WHERE kind = 'discovery_profile' AND NOT eligibility_recheck"
                        " ORDER BY player_id"
                    )
                ]

        try:
            discover([new_id, known_id])
            discover([new_id, known_id])
            assert queued() == [new_id]

            # 501 more unknown players fill the queue to its cap of 500.
            discover(others[:250])
            discover(others[250:])
            assert len(queued()) == DISCOVERY_QUEUE_CAP

            # Once a waiting check finishes, exactly one more fits.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE collector_work SET status = 'cancelled' WHERE player_id = %s",
                    (new_id,),
                )
            discover(others)
            assert len(queued()) == DISCOVERY_QUEUE_CAP + 1
            (left_out,) = set(others) - set(queued())

            # A refused player does not use up the last free place.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE collector_work SET status = 'cancelled' WHERE player_id = %s",
                    (others[0],),
                )
            discover([blocked_id, left_out])
            assert left_out in queued()
            assert blocked_id not in queued()
        finally:
            database.close()


def test_discovery_checks_added_at_the_same_moment_are_all_queued(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            first_id, second_id = [
                row[0]
                for row in connection.execute(
                    "INSERT INTO players (normalized_tag, active, eligibility_state)"
                    " VALUES ('#Q1', false, 'unknown'), ('#Q2', false, 'unknown')"
                    " RETURNING id"
                ).fetchall()
            ]
        options = conninfo_to_dict(connection_info).get("options", "")
        database = Database(
            make_conninfo(connection_info, options=f"{options} -c role=clashlens_python_worker")
        )
        claim = SimpleNamespace(work_type="process_observation")
        try:
            with database.pool.connection() as first, first.transaction():
                # The first battle log's transaction is still open.
                enqueue_discovered_players(first, database, claim, [first_id])
                with database.pool.connection() as second, second.transaction():
                    enqueue_discovered_players(second, database, claim, [second_id])
            with database.pool.connection() as connection:
                queued = sorted(
                    row[0]
                    for row in connection.execute(
                        "SELECT player_id FROM collector_work WHERE kind = 'discovery_profile'"
                    )
                )
            assert queued == sorted([first_id, second_id])
        finally:
            database.close()
