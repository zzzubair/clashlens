from __future__ import annotations

import json
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation
from test_no_opponent_rows_postgres import NO_OPPONENT_ROW
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    DAY_START,
    _processor,
)
from test_reset_scenarios_postgres import SEASON, _story, attacks, day, defenses

from clashlens import ranked_day_inputs
from clashlens.domain import ranked_day_for

REAL = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
# Any other row without an opponent may hide a battle.
BROKEN = NO_OPPONENT_ROW | {"destructionPercentage": 12}


def _store(connection_info, archive_server, key, rows, minutes):
    return store_observation(
        connection_info,
        archive_server,
        occurrence_key=key,
        endpoint="battle_log",
        body=json.dumps({"items": rows}).encode(),
        observed_at=DAY_START + timedelta(minutes=minutes),
        normalized_tag="#2PP",
    )


def _process(connection_info, archive_server, job, *, dedup=True):
    database, processor = _processor(connection_info, archive_server)
    database._supports_content_dedup = dedup
    try:
        assert processor.process_job(job, owner="test").outcome in {
            "processed", "processed_with_gaps"
        }
    finally:
        database.close()


def _raw(database, connection, player_id, *, shared_read):
    return connection.execute(
        ranked_day_inputs._coverage_sql(database, shared_read=shared_read),
        (player_id, None, DAY_START, None, DAY_END),
    ).fetchall()


def _quality(database, connection, player_id):
    return {
        log.observation_id: (
            log.has_row_gap, log.malformed_row_count,
            log.unclassified_row_count, log.valid,
        )
        for log in ranked_day_inputs.load_coverage(
            database, connection, player_id,
            ranked_day_for(DAY_START), None, None,
        )
    }


def test_one_read_of_each_log_keeps_coverage_in_every_storage_layout(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        direct, direct_job = _store(
            connection_info, archive_server, "direct", [REAL, BROKEN], 60
        )
        # The oldest path, before saved content was shared, relies on the
        # one-report-per-row rule 0012 dropped.
        with psycopg.connect(connection_info) as connection:
            connection.execute("ALTER TABLE battle_evidence ADD UNIQUE (source_row_id)")
        _process(connection_info, archive_server, direct_job, dedup=False)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "ALTER TABLE battle_evidence DROP CONSTRAINT battle_evidence_source_row_id_key"
            )
        members, members_job = _store(
            connection_info, archive_server, "members", [REAL, BROKEN, REAL], 180
        )
        _process(connection_info, archive_server, members_job)
        listed, listed_job = _store(
            connection_info, archive_server, "listed", [REAL, REAL, NO_OPPONENT_ROW], 240
        )
        _process(connection_info, archive_server, listed_job)
        empty, empty_job = _store(connection_info, archive_server, "empty", [], 300)
        _process(connection_info, archive_server, empty_job)

        database, _ = _processor(connection_info, archive_server)
        try:
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT player_id FROM battle_log_observations LIMIT 1"
                ).fetchone()[0]
                # A log saved per row, before one list held the whole log.
                connection.execute(
                    """
                    WITH moved AS (
                        DELETE FROM battle_payload_row_lists AS list
                        USING battle_log_observations AS log
                        WHERE log.observation_id = %s
                          AND list.parsed_payload_id = log.parsed_payload_id
                          AND list.reporting_player_id = log.player_id
                        RETURNING list.*
                    )
                    INSERT INTO battle_payload_rows (
                        parsed_payload_id, reporting_player_id,
                        source_row_index, source_row_id
                    )
                    SELECT moved.parsed_payload_id, moved.reporting_player_id,
                           (member.position - 1)::integer, member.source_row_id
                    FROM moved CROSS JOIN LATERAL unnest(moved.source_row_ids)
                        WITH ORDINALITY AS member (source_row_id, position)
                    """,
                    (members,),
                )
                # Older parsers saved rows they could not classify.
                connection.execute(
                    """
                    UPDATE battle_source_rows SET failure_category = 'unclassified_row'
                    WHERE id = (
                        SELECT source_row_id FROM battle_log_observation_source_rows AS row
                        JOIN battle_log_observations AS log
                          ON log.id = row.battle_log_observation_id
                        WHERE log.observation_id = %s AND row.outcome = 'malformed_legend_row'
                    )
                    """,
                    (direct,),
                )
                layouts = connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM battle_source_rows
                          WHERE battle_log_observation_id IS NOT NULL),
                        (SELECT count(*) FROM battle_payload_rows),
                        (SELECT count(*) FROM battle_payload_row_lists)
                    """
                ).fetchone()
                assert all(layouts), layouts

                before = _raw(database, connection, player_id, shared_read=False)
                assert _raw(database, connection, player_id, shared_read=True) == before
                assert len(before) == 4
                # Each repeated row stays listed once per position.
                assert [len(row[5]) for row in before] == [2, 3, 3, 0]

                expected = {
                    # Broken row, also the unclassified one.
                    direct: (True, 1, 1, False),
                    members: (True, 1, 0, False),
                    listed: (False, 0, 0, True),
                    empty: (False, 0, 0, True),
                }
                assert _quality(database, connection, player_id) == expected

                # Several reports of one rejected row still count it once.
                connection.execute(
                    """
                    INSERT INTO battle_evidence (
                        battle_id, source_row_id, observation_id, reporting_player_id,
                        perspective, battle_timestamp, stars, destruction_percentage,
                        army_share_code, reporter_trophies, opponent_trophies,
                        attacker_gain, defender_loss, trophy_rule_version,
                        source_observed_at, parser_version
                    )
                    SELECT e.battle_id, row.source_row_id, observed.id,
                           e.reporting_player_id, e.perspective, e.battle_timestamp,
                           e.stars, e.destruction_percentage, e.army_share_code,
                           e.reporter_trophies, e.opponent_trophies, e.attacker_gain,
                           e.defender_loss, e.trophy_rule_version,
                           log.observed_at, e.parser_version
                    FROM battle_log_observation_source_rows AS row
                    JOIN battle_log_observations AS log
                      ON log.id = row.battle_log_observation_id
                    CROSS JOIN (SELECT * FROM battle_evidence ORDER BY id LIMIT 1) AS e
                    CROSS JOIN (
                        SELECT id FROM collector_observations ORDER BY id LIMIT 2
                    ) AS observed
                    WHERE log.observation_id = %s
                      AND row.outcome = 'malformed_legend_row'
                    """,
                    (members,),
                )
                assert _quality(database, connection, player_id) == expected
                assert _raw(database, connection, player_id, shared_read=True) == _raw(
                    database, connection, player_id, shared_read=False
                )
        finally:
            database.close()


def _scans(plan: dict) -> set[tuple[str, str]]:
    found, pending = set(), [plan]
    while pending:
        node = pending.pop()
        if "Relation Name" in node:
            found.add((node["Node Type"], node["Relation Name"]))
        pending.extend(node.get("Plans", []))
    return found


def test_day_coverage_never_reads_the_unused_occurrence_table(
    database_url: str, archive_server, monkeypatch
) -> None:
    tag = "#2PP"
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, tag)
        story.players[tag].battles = (
            attacks(day(2), 40, 40) + defenses(day(2), 30, 30)
            + attacks(day(3), 40) + defenses(day(3), 30)
        )
        story.reset(day(2), {tag: (5300, SEASON)})
        story.reset(day(3), {tag: (5320, SEASON)})
        story.reads(day(3) + timedelta(hours=20), {tag: 5330})
        story.reset(day(4), {tag: (5330, SEASON)})
        saved = story.saved_day(tag, day(3))
        unfinished = story.unfinished_jobs()
        members = story.board_members(day(4))
        board = story.served_board()
        # Production's 3.7 million battle reports, and the unused table as the
        # 14 October 2026 vacuum recorded it.
        with psycopg.connect(connection_info) as connection:
            restored = connection.execute(
                """
                SELECT pg_restore_relation_stats(
                    'schemaname', current_schema()::text, 'relname', relname,
                    'relpages', relpages, 'reltuples', reltuples)
                FROM (VALUES ('battle_evidence', 1, 3700000::real),
                             ('battle_log_observation_rows', 0, 0::real))
                    AS stats (relname, relpages, reltuples)
                """
            ).fetchall()
        database, _ = _processor(connection_info, archive_server)
        try:
            with database.pool.connection() as connection:
                player_id, start, end = connection.execute(
                    """
                    SELECT player_id, min(observation_id), max(observation_id)
                    FROM battle_log_observations
                    WHERE observed_at >= %s
                    GROUP BY player_id
                    """,
                    (day(3),),
                ).fetchone()
                coverage = ranked_day_inputs.load_coverage(
                    database, connection, player_id, ranked_day_for(day(3)), start, end
                )
                plan = connection.execute(
                    "EXPLAIN (FORMAT JSON) "
                    + ranked_day_inputs._coverage_sql(database, shared_read=True),
                    (player_id, start, day(3), end, day(4)),
                ).fetchone()[0][0]["Plan"]
        finally:
            database.close()

    assert unfinished == []
    assert restored == [(True,), (True,)]
    assert saved == {
        "state": "Complete", "start": 5320, "end": 5120, "next_start": 5120,
        "automatic_loss": 210, "raise": 0, "attacks": 1, "defenses": 1,
        "residual": 0,
    }
    assert members == {tag: "complete"}
    assert board is not None and board["boundary_at"] == day(4).isoformat()
    assert [(entry["tag"], entry["trophies"]) for entry in board["entries"]] == [
        (tag, 5330)
    ]
    assert [
        (log.row_count, len(log.battle_identities), log.has_row_gap, log.valid)
        for log in coverage
    ] == [(4, 4, False, True), (6, 6, False, True), (6, 6, False, True)]
    scans = _scans(plan)
    assert "battle_log_observation_rows" not in {relation for _, relation in scans}
    assert ("Seq Scan", "battle_evidence") not in scans
