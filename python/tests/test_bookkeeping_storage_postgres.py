from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database, store_observation, text
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    DAY_START,
    _processor,
    _store_baseline_pair,
)

from clashlens import api_leaderboard, api_players, reconciliation_db, season_retirement
from clashlens.api_db import ApiDatabase

READ_AT = DAY_END + timedelta(minutes=10)


def _visible(api: ApiDatabase) -> tuple[object, object]:
    page = api_players.get_player_page(
        api, "#2PP", now=READ_AT, freshness_seconds=900
    )
    board = api_leaderboard.get_live_leaderboard(api, limit=50, now=READ_AT)
    assert page is not None and page["screen_ready"]["recent_days"]
    assert board is not None
    return page, board


def _log_rows(connection: psycopg.Connection) -> list[tuple]:
    return connection.execute(
        """
        SELECT log.observation_id, source.*
        FROM battle_log_observation_source_rows AS source
        JOIN battle_log_observations AS log ON log.id = source.battle_log_observation_id
        ORDER BY log.observation_id, source.source_row_index
        """
    ).fetchall()


def _ranked_day(connection: psycopg.Connection) -> tuple:
    return connection.execute(
        """
        SELECT version.id, result_hash, state, final_trophies_before_reset,
               attack_count, input_evidence
        FROM ranked_day_versions AS version
        JOIN players AS player ON player.id = version.player_id
        WHERE player.normalized_tag = '#2PP' AND ranked_day_start = %s
        ORDER BY version.version DESC LIMIT 1
        """,
        (DAY_START,),
    ).fetchone()


def test_compact_bookkeeping_keeps_results_pages_and_leaderboard_unchanged(
    database_url: str, archive_server
) -> None:
    """One row per fetch reads, reconciles and publishes like one row per battle."""
    items = json.loads(BATTLE_FIXTURE.read_bytes())["items"]
    first = items[0]
    second = dict(first, opponentPlayerTag="#9PP", stars=2,
                  destructionPercentage=80, battleTimestamp="20260804T130000.000Z")
    fetches = [[first, items[1]], [second, first, items[1]]]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = list(_store_baseline_pair(
            connection_info, archive_server, key="cut-start", boundary=DAY_START,
            trophies=6000, empty_battle_log=True,
        )[2:])
        bodies = []
        for index, fetch in enumerate(fetches):
            bodies.append(json.dumps({"items": fetch}).encode())
            jobs.append(store_observation(
                connection_info, archive_server,
                occurrence_key=f"cut-fetch-{index}", endpoint="battle_log",
                body=bodies[-1], normalized_tag="#2PP",
                observed_at=DAY_START + timedelta(hours=8 + index * 2),
            )[1])
        jobs.extend(_store_baseline_pair(
            connection_info, archive_server, key="cut-end", boundary=DAY_END,
            trophies=6066, empty_battle_log=True,
        )[2:])
        database, processor = _processor(connection_info, archive_server)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            for job in jobs:
                assert processor.process_job(job, owner="cut").outcome == "processed"
            while processor.process_once(owner="cut-follow-up") is not None:
                pass

            with database.pool.connection() as connection:
                assert connection.execute(
                    """
                    SELECT count(*), sum(cardinality(source_row_ids))
                    FROM battle_payload_row_lists
                    """
                ).fetchone() == (2, 5)
                assert connection.execute(
                    "SELECT count(*) FROM battle_payload_rows"
                ).fetchone()[0] == 0
                # Opponent #8PP is in both fetches but recorded once.
                assert connection.execute(
                    """
                    SELECT count(*), count(DISTINCT (player_id, source_kind))
                    FROM known_player_discoveries
                    """
                ).fetchone() == (2, 2)
                version = connection.execute(
                    """
                    SELECT coverage_evidence, contribution_evidence,
                           input_evidence -> 'contributions'
                    FROM ranked_day_versions WHERE id = %s
                    """,
                    (_ranked_day(connection)[0],),
                ).fetchone()
                assert version[:2] == ([], []) and len(version[2]) == 2
                rows = _log_rows(connection)
                ranked = _ranked_day(connection)
            assert ranked[2:5] == ("Complete", 6066, 2)
            visible = _visible(api)

            # Rows saved before migration 0051 listed one battle per row.
            # Both shapes must read, reconcile and publish identically.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO battle_payload_rows (
                        parsed_payload_id, reporting_player_id,
                        source_row_index, source_row_id
                    )
                    SELECT list.parsed_payload_id, list.reporting_player_id,
                           member.position - 1, member.id
                    FROM battle_payload_row_lists AS list
                    CROSS JOIN LATERAL unnest(list.source_row_ids)
                        WITH ORDINALITY AS member (id, position)
                    """
                )
                connection.execute("DELETE FROM battle_payload_row_lists")
                assert _log_rows(connection) == rows
                versions = connection.execute(
                    """
                    SELECT parser_version, processing_version,
                           domain_rule_version, analytics_rule_version
                    FROM ranked_day_versions WHERE id = %s
                    """,
                    (ranked[0],),
                ).fetchone()
                reconciliation_db.recalculate_ranked_day(
                    database, connection,
                    player_id=connection.execute(
                        "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                    ).fetchone()[0],
                    day_start=DAY_START,
                    parser_version=versions[0],
                    processing_version=versions[1],
                    domain_rule_version=versions[2],
                    analytics_rule_version=versions[3],
                )
                assert _ranked_day(connection) == ranked
            assert _visible(api) == visible

            # A repeat of an old-shape payload is not listed a second time.
            _observation, repeat = store_observation(
                connection_info, archive_server,
                occurrence_key="cut-repeat", endpoint="battle_log",
                body=bodies[-1], normalized_tag="#2PP",
                observed_at=DAY_START + timedelta(hours=11),
            )
            assert processor.process_job(repeat, owner="cut").outcome == "processed"
            while processor.process_once(owner="cut-follow-up") is not None:
                pass
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM battle_payload_row_lists"
                ).fetchone()[0] == 0
                after = _log_rows(connection)
                assert after[: len(rows)] == rows
                assert [row[4] for row in after[len(rows):]] == [0, 1, 2]
        finally:
            api.close()
            database.close()


def test_retiring_a_battle_keeps_the_rest_of_each_listed_fetch(
    database_url: str, archive_server
) -> None:
    items = json.loads(BATTLE_FIXTURE.read_bytes())["items"]
    retired, ignored = items
    live = dict(retired, opponentPlayerTag="#9PP", battleTimestamp="20260804T130000.000Z")
    fetches = [[retired], [retired, ignored], [live, retired, ignored]]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            for index, fetch in enumerate(fetches):
                _observation, job = store_observation(
                    connection_info, archive_server,
                    occurrence_key=f"retire-fetch-{index}", endpoint="battle_log",
                    body=json.dumps({"items": fetch}).encode(), normalized_tag="#2PP",
                    observed_at=DAY_START + timedelta(hours=8, minutes=index),
                )
                assert processor.process_job(job, owner="retire").outcome == "processed"
            with database.pool.connection() as connection:
                battle_id, source_id = connection.execute(
                    """
                    SELECT evidence.battle_id, evidence.source_row_id
                    FROM battle_evidence AS evidence
                    JOIN legend_battles AS battle ON battle.id = evidence.battle_id
                    JOIN players AS defender ON defender.id = battle.defender_player_id
                    WHERE defender.normalized_tag = '#8PP'
                    """
                ).fetchone()
                assert season_retirement._delete_battles(connection, [battle_id]) == 1
                positions = connection.execute(
                    """
                    SELECT log.observation_id, source.source_row_index, source.outcome
                    FROM battle_log_observation_source_rows AS source
                    JOIN battle_log_observations AS log
                      ON log.id = source.battle_log_observation_id
                    ORDER BY 1, 2
                    """
                ).fetchall()
                assert [(text(row[2]), row[1]) for row in positions] == [
                    ("ignored_non_legend", 1),
                    ("valid_legend", 0),
                    ("ignored_non_legend", 2),
                ]
                # The fetch that held only the retired battle no longer lists it.
                assert connection.execute(
                    "SELECT count(*) FROM battle_payload_row_lists"
                ).fetchone()[0] == 2
                assert connection.execute(
                    "SELECT count(*) FROM battle_source_rows WHERE id = %s", (source_id,)
                ).fetchone()[0] == 0
                connection.rollback()
                listed = connection.execute(
                    "SELECT id FROM battle_source_rows WHERE outcome = 'ignored_non_legend'"
                ).fetchone()[0]
                with pytest.raises(psycopg.errors.ForeignKeyViolation):
                    connection.execute(
                        "DELETE FROM battle_source_rows WHERE id = %s", (listed,)
                    )
        finally:
            database.close()
