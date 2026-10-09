from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database

from clashlens import api_leaderboard
from clashlens.api_db import ApiDatabase

BOARDS = 28
PLAYERS = 400
FIRST_END = datetime(2026, 9, 2, 5, tzinfo=UTC)


def _seed_published_boards(connection_info: str) -> None:
    """Publish BOARDS frozen boards, each listing PLAYERS ranked days."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute(
            """
            INSERT INTO players (normalized_tag)
            SELECT '#LOC' || player FROM generate_series(1, %(players)s) AS player
            """,
            {"players": PLAYERS},
        )
        # Ranked-day ids run backwards from board dates, so walking the
        # ranked-day index from the newest id meets other boards' rows first.
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end, official_season_id,
                season_day_number, season_anchor_rule_version,
                reconciliation_rule_version, input_hash, result_hash, version,
                state, confidence
            )
            SELECT player.id, %(first)s + (board - 2) * interval '1 day',
                   %(first)s + (board - 1) * interval '1 day', '2026-09', board,
                   'test', 'test', repeat('a', 64), repeat('b', 64), 1,
                   'Complete', 'confirmed'
            FROM generate_series(%(boards)s, 1, -1) AS board
            CROSS JOIN players AS player
            ORDER BY board DESC, player.id
            """,
            {"first": FIRST_END, "boards": BOARDS},
        )
        for board in range(1, BOARDS + 1):
            boundary_at = FIRST_END + timedelta(days=board - 1)
            snapshot_id = connection.execute(
                """
                INSERT INTO leaderboard_snapshots (
                    snapshot_kind, boundary_at, version, ordering_rule_version,
                    freshness_rule_version, state, measured_coverage,
                    stale_entry_count, published_at
                )
                VALUES ('frozen', %s, 1, 'test', 'test', 'published', 1, 0, %s)
                RETURNING id
                """,
                (boundary_at, boundary_at),
            ).fetchone()[0]
            generation_id = connection.execute(
                """
                INSERT INTO boundary_publication_generations (
                    boundary_at, generation, ordering_rule_version,
                    freshness_rule_version, expected_population_count,
                    expected_population_hash, target_at, snapshot_id,
                    snapshot_state
                )
                VALUES (%s, 1, 'test', 'test', %s, repeat('c', 64), %s, %s,
                        'published')
                RETURNING id
                """,
                (boundary_at, PLAYERS, boundary_at, snapshot_id),
            ).fetchone()[0]
            manifest_id = connection.execute(
                """
                INSERT INTO boundary_publication_manifests (
                    generation_id, artifact_kind, rule_versions, digest
                )
                VALUES (%s, 'snapshot', '{}', repeat('d', 64))
                RETURNING id
                """,
                (generation_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE boundary_publication_generations"
                " SET snapshot_manifest_id = %s WHERE id = %s",
                (manifest_id, generation_id),
            )
            connection.execute(
                """
                INSERT INTO boundary_publication_manifest_rows (
                    manifest_id, ordinal, player_id, ranked_day_version_id,
                    classification, input_identity
                )
                SELECT %s, row_number() OVER (ORDER BY ranked.player_id),
                       ranked.player_id, ranked.id, 'Complete', '{}'
                FROM ranked_day_versions AS ranked
                WHERE ranked.season_day_number = %s
                """,
                (manifest_id, board),
            )
        connection.execute("ANALYZE")


def _rows_read(plan: dict, relation: str) -> int:
    read = 0
    if plan.get("Relation Name") == relation:
        per_loop = plan["Actual Rows"] + plan.get("Rows Removed by Filter", 0)
        read += round(per_loop * plan["Actual Loops"])
    for child in plan.get("Plans", []):
        read += _rows_read(child, relation)
    return read


def test_daily_board_lookup_reads_one_input_row_per_board(
    database_url: str, monkeypatch
) -> None:
    # 7 October 2026: production held about 5.4 million board input rows and
    # finding each board's Legend day walked through all of them, so the
    # daily board hit the statement time limit and showed as unavailable.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _seed_published_boards(connection_info)
        locators: list[tuple[str, tuple]] = []
        execute = psycopg.Connection.execute

        def record(connection, query, params=None, **kwargs):
            if "WITH publications AS" in str(query):
                locators.append((str(query), params))
            return execute(connection, query, params, **kwargs)

        monkeypatch.setattr(psycopg.Connection, "execute", record)
        api = ApiDatabase(connection_info)
        try:
            frozen = api_leaderboard.get_frozen_leaderboard(
                api, limit=10, official_season_id="2026-09", season_day_number=1
            )
        finally:
            api.close()
        monkeypatch.undo()
        assert len(locators) == 1
        query, params = locators[0]
        with psycopg.connect(connection_info) as connection:
            plan = connection.execute(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + query, params
            ).fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)

    assert frozen is not None
    assert frozen["boundary_at"] == FIRST_END.isoformat()
    assert frozen["season_day_number"] == 1
    # Each published board's Legend day lookup should read one input row and
    # one ranked day.
    for relation in ("boundary_publication_manifest_rows", "ranked_day_versions"):
        assert _rows_read(plan[0]["Plan"], relation) <= BOARDS, (relation, plan)
