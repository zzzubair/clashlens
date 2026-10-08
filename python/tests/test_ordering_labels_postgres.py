from __future__ import annotations

import json
from datetime import timedelta

import pytest
from domain_test_support import domain_database, seed_attacks, text
from test_boundary_publication_postgres import (
    BOUNDARY,
    _player_and_version,
    _sweep_with_members,
)

from clashlens import army_ingestion, boundary, boundary_publication, snapshots
from clashlens.analytics import SNAPSHOT_ORDERING_RULE_VERSION, deterministic_tag_hash
from clashlens.db import Database
from clashlens.domain import RANKED_DAY_DURATION, ranked_day_for

OLDER = "tracked-player-order-v1"
SEASON = ranked_day_for(BOUNDARY - RANKED_DAY_DURATION).official_season_id
# Both tag hashes, the older rule's SHA-256 and the current MD5, put #2PP
# ahead; #28's higher Season average attack destruction puts it ahead.
BY_TAG, BY_ATTACKS = ["#2PP", "#28"], ["#28", "#2PP"]


def _run(database: Database, connection, work_type: str, complete) -> None:
    for (job_id,) in connection.execute(
        "SELECT id FROM python_processing_jobs"
        " WHERE work_type = %s AND status = 'pending' ORDER BY id",
        (work_type,),
    ).fetchall():
        connection.commit()
        claim = database.claim_job(owner=f"labels-{work_type}", job_id=int(job_id))
        assert claim is not None
        complete(database, claim)


def _publish(database: Database, connection) -> None:
    _run(database, connection, "build_snapshot", snapshots.complete_snapshot)
    _run(database, connection, "build_analytics", boundary_publication.complete_analytics)
    _run(database, connection, "build_army_analytics", army_ingestion.complete_army_analytics)


def _order(connection, generation: int) -> list[str]:
    return [
        text(row[0])
        for row in connection.execute(
            """
            SELECT player.normalized_tag
            FROM boundary_publication_generations AS g
            JOIN leaderboard_snapshot_entries AS entry ON entry.snapshot_id = g.snapshot_id
            JOIN players AS player ON player.id = entry.player_id
            WHERE g.boundary_at = %s AND g.generation = %s
            ORDER BY entry.position
            """,
            (BOUNDARY, generation),
        ).fetchall()
    ]


def _labels(connection, generation: int) -> list[str | None]:
    """The ordering label saved on the generation, its two frozen input
    lists, its frozen and live boards and its publication event."""
    row = connection.execute(
        """
        SELECT g.ordering_rule_version,
               snapshot_manifest.rule_versions->>'ordering_rule_version',
               army_manifest.rule_versions->>'ordering_rule_version',
               board.ordering_rule_version,
               (SELECT live.ordering_rule_version FROM leaderboard_snapshots AS live
                WHERE live.snapshot_kind = 'live' AND live.boundary_at = g.boundary_at
                  AND live.input_hash = board.input_hash),
               event.rule_versions->>'ordering_rule_version'
        FROM boundary_publication_generations AS g
        LEFT JOIN boundary_publication_manifests AS snapshot_manifest
          ON snapshot_manifest.id = g.snapshot_manifest_id
        LEFT JOIN boundary_publication_manifests AS army_manifest
          ON army_manifest.id = g.army_manifest_id
        LEFT JOIN leaderboard_snapshots AS board ON board.id = g.snapshot_id
        LEFT JOIN boundary_publication_events AS event
          ON event.boundary_at = g.boundary_at AND event.generation = g.generation
        WHERE g.boundary_at = %s AND g.generation = %s
        """,
        (BOUNDARY, generation),
    ).fetchone()
    assert row is not None
    return [text(value) for value in row]


@pytest.mark.parametrize(
    "replacement",
    ["army", "army-deferred", "army-queued", "reordered", "army-then-board", "army-rebuilt"],
)
def test_a_replacement_saves_one_ordering_label_everywhere(
    database_url: str, monkeypatch, replacement: str
) -> None:
    """A board built under the older ordering rule keeps that label on a
    replacement that only rebuilds army records, inheriting the board now,
    later or from a queued correction. One rebuilt because of its older
    order, an army-only replacement whose board a revised day result
    rebuilds, or one the board rebuild check finds before it inherits the
    older board, freezes its inputs under the current rule."""
    monkeypatch.setattr(boundary, "SNAPSHOT_ORDERING_RULE_VERSION", OLDER)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                assert sorted(BY_TAG, key=deterministic_tag_hash) == BY_TAG
                players = {
                    tag: _player_and_version(connection, tag, 1, "a" * 64) for tag in BY_TAG
                }
                _sweep_with_members(connection, [ids[0] for ids in players.values()])
                for destruction, (tag, (player_id, _)) in zip(
                    (50, 60), players.items(), strict=True
                ):
                    seed_attacks(connection, player_id, [(BOUNDARY - timedelta(days=2), destruction)])
                    # Tied at the Reset, read without the responses.
                    connection.execute("SET LOCAL session_replication_role = replica")
                    connection.execute(
                        """
                        INSERT INTO player_profile_versions (
                            player_id, observation_id, normalized_tag, endpoint_version,
                            schema_version, parser_version, observed_at,
                            source_http_status, name, trophies, league_tier_id,
                            league_tier_name, eligibility_state, profile_json,
                            source_contract_state, current_league_season_id
                        ) VALUES (%s, %s, %s, 'v1', 'v1', 'parser', %s, 200, %s, 5300,
                                  105000034, 'Legend League', 'eligible', %s,
                                  'accepted', %s)
                        """,
                        (
                            player_id, 900000 + player_id, tag, BOUNDARY - timedelta(hours=1),
                            tag, json.dumps({"tag": tag, "trophies": 5300}), SEASON,
                        ),
                    )
                connection.commit()
                player_id, version_id = players["#2PP"]
                for member, version in players.values():
                    boundary._record_boundary_generation(
                        database,
                        connection,
                        boundary_at=BOUNDARY,
                        player_id=member,
                        ranked_day_version_id=version,
                        ranked_day_input_hash="a" * 64,
                    )
                monkeypatch.undo()
                # Its input list was frozen before Season attacks were.
                connection.execute("SET LOCAL session_replication_role = replica")
                connection.execute(
                    "UPDATE boundary_publication_manifest_rows"
                    " SET input_identity = input_identity - 'season_attacks'"
                )
                _publish(database, connection)
                assert _labels(connection, 1) == [OLDER] * 6
                assert _order(connection, 1) == BY_TAG
                source = int(
                    connection.execute(
                        "SELECT id FROM boundary_publication_generations WHERE generation = 1"
                    ).fetchone()[0]
                )
                if replacement in {"reordered", "army-queued"}:
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_corrections
                            (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
                        VALUES (%s, %s, %s, '[]'::jsonb)
                        """,
                        (BOUNDARY, source, ["army"] if replacement == "army-queued" else ["snapshot", "army"]),
                    )
                else:
                    boundary_publication._queue_boundary_army_correction(
                        database,
                        connection,
                        boundary_at=BOUNDARY,
                        generation_id=source,
                        defer_inheritance=replacement != "army",
                    )
                if replacement == "army-rebuilt":
                    connection.commit()
                    # Queued behind the replacement, and once queued, kept.
                    assert [
                        boundary.queue_board_rebuilds(database, SEASON, queue=True)["boards"]
                        for _ in range(2)
                    ] == [
                        [
                            {
                                "boundary_at": BOUNDARY.isoformat(),
                                "generation": 2,
                                "profile_not_found": 0,
                                "late_battles": 0,
                                "reordered": True,
                                "correction": correction,
                            }
                        ]
                        for correction in ("queued", "already_queued")
                    ]
                if replacement == "army-then-board":
                    # A revised day result reaches the replacement before it
                    # inherits the board.
                    revised = connection.execute(
                        """
                        INSERT INTO ranked_day_versions (
                            player_id, ranked_day_start, ranked_day_end, official_season_id,
                            season_day_number, season_anchor_rule_version,
                            reconciliation_rule_version, result_hash, version, state,
                            confidence, input_hash, evidence_complete, coverage_complete
                        ) SELECT player_id, ranked_day_start, ranked_day_end, official_season_id,
                                 season_day_number, season_anchor_rule_version,
                                 reconciliation_rule_version, %s, 2, state, confidence, %s,
                                 true, true
                        FROM ranked_day_versions WHERE id = %s
                        RETURNING id
                        """,
                        ("d" * 64, "d" * 64, version_id),
                    ).fetchone()[0]
                    boundary._record_boundary_generation(
                        database,
                        connection,
                        boundary_at=BOUNDARY,
                        player_id=player_id,
                        ranked_day_version_id=int(revised),
                        ranked_day_input_hash="d" * 64,
                    )
                connection.commit()
                boundary_publication.reevaluate_boundary_publications(database)
                _publish(database, connection)
                if replacement in {"army-then-board", "army-rebuilt"}:
                    # The replacement gave way to one under the current rule.
                    _publish(database, connection)
                    assert _labels(connection, 2)[0] == OLDER
                    assert _labels(connection, 3) == [SNAPSHOT_ORDERING_RULE_VERSION] * 6
                    assert _order(connection, 3) == BY_ATTACKS
                    assert connection.execute(
                        """
                        SELECT bool_and(entry.input_identity ? 'season_attacks')
                        FROM boundary_publication_generations AS g,
                             boundary_publication_manifest_entries(g.snapshot_manifest_id) AS entry
                        WHERE g.generation = 3
                        """
                    ).fetchone()[0]
                    return
                reordered = replacement == "reordered"
                assert _labels(connection, 2) == [
                    SNAPSHOT_ORDERING_RULE_VERSION if reordered else OLDER
                ] * 6
                assert _order(connection, 2) == (BY_ATTACKS if reordered else BY_TAG)
        finally:
            database.close()
