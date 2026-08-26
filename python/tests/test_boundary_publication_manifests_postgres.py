from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, text

from clashlens.db import Database

BOUNDARY = datetime(2026, 8, 5, 5, tzinfo=UTC)


def _seed_player(connection, tag: str, version: int) -> tuple[int, int]:
    player = int(
        connection.execute(
            "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
            (tag,),
        ).fetchone()[0]
    )
    ranked = int(
        connection.execute(
            """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version, reconciliation_rule_version,
            result_hash, version, state, confidence, input_hash,
            evidence_complete, coverage_complete
        ) VALUES (%s, %s, %s, 'season', 1, 'anchor', 'rules', %s, %s,
                  'Complete', 'exact', %s, true, true)
        RETURNING id
        """,
            (player, BOUNDARY.replace(day=4), BOUNDARY, "a" * 64, version, "a" * 64),
        ).fetchone()[0]
    )
    return player, ranked


def test_published_snapshot_coverage_columns_are_immutable(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            snapshot_id = connection.execute(
                """
                INSERT INTO leaderboard_snapshots (
                    snapshot_kind, boundary_at, version, ordering_rule_version,
                    freshness_rule_version, state, measured_coverage,
                    stale_entry_count, eligible_population_count,
                    included_entry_count, fresh_entry_count
                ) VALUES ('frozen', %s, 1, 'order', 'freshness', 'building',
                          1, 0, 1, 1, 1)
                RETURNING id
                """,
                (BOUNDARY,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE leaderboard_snapshots
                SET state = 'published', published_at = clock_timestamp()
                WHERE id = %s
                """,
                (snapshot_id,),
            )
            with pytest.raises(Exception, match="immutable"):
                connection.execute(
                    "UPDATE leaderboard_snapshots SET excluded_partial_count = 1 WHERE id = %s",
                    (snapshot_id,),
                )


def test_manifest_is_sorted_frozen_and_reused_after_member_change(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                first = _seed_player(connection, "#M1", 1)
                second = _seed_player(connection, "#M2", 1)
                sweep = int(
                    connection.execute(
                        "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s) RETURNING id",
                        (BOUNDARY,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO collector_reset_sweep_members (sweep_id, player_id) VALUES (%s, %s), (%s, %s)",
                    (sweep, first[0], sweep, second[0]),
                )
                connection.execute(
                    """
                    INSERT INTO collector_boundary_admission (
                        boundary_at, reset_sweep_id, regular_drain_complete,
                        reset_drain_complete, safe_handoff, state
                    ) VALUES (%s, %s, true, true, true, 'safe_handoff')
                    """,
                    (BOUNDARY, sweep),
                )
                for player, ranked in (first, second):
                    database._record_boundary_generation(
                        connection,
                        boundary_at=BOUNDARY,
                        player_id=player,
                        ranked_day_version_id=ranked,
                        ranked_day_input_hash="a" * 64,
                    )
                generation = connection.execute(
                    "SELECT id FROM boundary_publication_generations"
                ).fetchone()[0]
                connection.execute("SAVEPOINT capture_timestamp_guard")
                with pytest.raises(Exception, match="immutable"):
                    connection.execute(
                        "UPDATE boundary_publication_generations SET membership_captured_at = NULL WHERE id = %s",
                        (generation,),
                    )
                connection.execute("ROLLBACK TO SAVEPOINT capture_timestamp_guard")
                connection.execute(
                    "UPDATE collector_reset_sweeps SET membership_captured_at = clock_timestamp() WHERE boundary_at = %s",
                    (BOUNDARY,),
                )
                connection.execute("SAVEPOINT sweep_capture_timestamp_guard")
                with pytest.raises(Exception, match="immutable"):
                    connection.execute(
                        "UPDATE collector_reset_sweeps SET membership_captured_at = NULL WHERE boundary_at = %s",
                        (BOUNDARY,),
                    )
                connection.execute(
                    "ROLLBACK TO SAVEPOINT sweep_capture_timestamp_guard"
                )
                manifests = connection.execute(
                    """
                    SELECT id, artifact_kind, digest
                    FROM boundary_publication_manifests
                    WHERE generation_id = %s ORDER BY artifact_kind
                    """,
                    (generation,),
                ).fetchall()
                assert [text(row[1]) for row in manifests] == ["army", "snapshot"]
                before = [
                    tuple(row)
                    for row in connection.execute(
                        """
                        SELECT player_id, ordinal
                        FROM boundary_publication_manifest_rows
                        WHERE manifest_id = %s ORDER BY ordinal
                        """,
                        (manifests[1][0],),
                    ).fetchall()
                ]
                assert before == sorted(before, key=lambda row: row[0])
                connection.execute("SAVEPOINT manifest_insert_guard")
                with pytest.raises(Exception, match="immutable"):
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_manifest_rows
                            (manifest_id, ordinal, player_id, classification, input_identity)
                        VALUES (%s, 99, %s, 'Complete', '{}')
                        """,
                        (manifests[1][0], first[0]),
                    )
                connection.execute("ROLLBACK TO SAVEPOINT manifest_insert_guard")
                digest = text(manifests[1][2])
                connection.execute("SAVEPOINT source_identity_guard")
                with pytest.raises(Exception, match="source identity"):
                    connection.execute(
                        "UPDATE boundary_publication_generation_members SET ranked_day_input_hash = %s WHERE generation_id = %s AND player_id = %s",
                        ("b" * 64, generation, first[0]),
                    )
                connection.execute("ROLLBACK TO SAVEPOINT source_identity_guard")
                connection.execute("SAVEPOINT source_identity_null_guard")
                with pytest.raises(Exception, match="source identity"):
                    connection.execute(
                        "UPDATE boundary_publication_generation_members SET ranked_day_version_id = NULL WHERE generation_id = %s AND player_id = %s",
                        (generation, first[0]),
                    )
                connection.execute("ROLLBACK TO SAVEPOINT source_identity_null_guard")
                next_boundary = BOUNDARY + timedelta(days=1)
                next_sweep = int(
                    connection.execute(
                        "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s) RETURNING id",
                        (next_boundary,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO collector_reset_sweep_members (sweep_id, player_id) VALUES (%s, %s)",
                    (next_sweep, first[0]),
                )
                next_generation, _ = database._create_boundary_generation(
                    connection,
                    boundary_at=next_boundary,
                    sweep_id=next_sweep,
                    player_ids=[first[0]],
                    generation=1,
                    supersedes_id=None,
                )
                next_manifest = database._freeze_boundary_manifest(
                    connection,
                    generation_id=next_generation,
                    artifact_kind="snapshot",
                )
                unsealed_manifest = connection.execute(
                    """
                    INSERT INTO boundary_publication_manifests
                        (generation_id, artifact_kind, rule_versions, digest)
                    VALUES (%s, 'army', '{}', repeat('c', 64))
                    RETURNING id
                    """,
                    (next_generation,),
                ).fetchone()[0]
                connection.execute("SAVEPOINT manifest_row_relocation_guard")
                with pytest.raises(Exception, match="manifest rows"):
                    connection.execute(
                        """
                        UPDATE boundary_publication_manifest_rows
                        SET manifest_id = %s
                        WHERE manifest_id = %s AND ordinal = 1
                        """,
                        (unsealed_manifest, next_manifest[0]),
                    )
                connection.execute(
                    "ROLLBACK TO SAVEPOINT manifest_row_relocation_guard"
                )
                connection.execute("SAVEPOINT source_identity_null_to_value_guard")
                with pytest.raises(Exception, match="source identity"):
                    connection.execute(
                        """
                        UPDATE boundary_publication_generation_members
                        SET ranked_day_version_id = %s, ranked_day_input_hash = %s
                        WHERE generation_id = %s AND player_id = %s
                        """,
                        (first[1], "a" * 64, next_generation, first[0]),
                    )
                connection.execute(
                    "ROLLBACK TO SAVEPOINT source_identity_null_to_value_guard"
                )
                database._try_enqueue_boundary_artifacts(
                    connection, boundary_at=BOUNDARY, generation_id=int(generation)
                )
                assert (
                    text(
                        connection.execute(
                            "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                            (manifests[1][0],),
                        ).fetchone()[0]
                    )
                    == digest
                )
                with pytest.raises(Exception, match="immutable"):
                    connection.execute(
                        "UPDATE boundary_publication_manifests SET digest = %s WHERE id = %s",
                        ("c" * 64, manifests[1][0]),
                    )
        finally:
            database.close()
