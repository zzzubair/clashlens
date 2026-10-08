"""A correction's manifest stores only the rows that differ from its Reset's
full manifest, and still rebuilds exactly the rows, Season inputs and digest
the full manifest would have stored."""

from __future__ import annotations

import json
from typing import Any

import psycopg
import pytest
from domain_test_support import domain_database, text
from test_boundary_manifest_postgres import PLAYERS, per_player_inputs, seed_population

from clashlens import boundary
from clashlens.boundary_manifest import (
    _apply_list_changes,
    _list_changes,
    manifest_contents,
    manifest_digest,
)
from clashlens.db import Database


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return {**left, "generation": 0} == {**right, "generation": 0}


def _correction(
    connection: Any, source_id: int, generation: int, *, first_player: int = 1
) -> int:
    """The next generation of ``source_id``'s Reset, members copied, as a
    correction makes it, or with members from ``first_player`` on only."""
    boundary_at, sweep_id = connection.execute(
        "SELECT boundary_at, sweep_id FROM boundary_publication_generations WHERE id = %s",
        (source_id,),
    ).fetchone()
    generation_id, _ = boundary._create_boundary_generation(
        None,
        connection,
        boundary_at=boundary_at,
        sweep_id=sweep_id,
        player_ids=list(range(first_player, PLAYERS + 1)),
        generation=generation,
        supersedes_id=source_id if first_player == 1 else None,
    )
    if first_player != 1:
        connection.execute(
            """
            UPDATE boundary_publication_generation_members AS member
            SET ranked_day_version_id = source.ranked_day_version_id,
                ranked_day_input_hash = source.ranked_day_input_hash,
                status = source.status, snapshot_status = source.snapshot_status,
                army_status = source.army_status
            FROM boundary_publication_generation_members AS source
            WHERE member.generation_id = %s AND source.generation_id = %s
              AND source.player_id = member.player_id
            """,
            (generation_id, source_id),
        )
    return generation_id


def _fail_members(connection: Any, generation_id: int, condition: str) -> None:
    connection.execute(
        f"""
        UPDATE boundary_publication_generation_members
        SET snapshot_status = 'failed', army_status = 'failed'
        WHERE generation_id = %s AND {condition}
        """,
        (generation_id,),
    )


def _freeze(database: Database, connection: Any, generation_id: int, generation: int):
    """Freeze both manifests and check each rebuilds the per-player freeze's
    rows and Season inputs under the digest it stored."""
    frozen = {}
    for artifact_kind in ("snapshot", "army"):
        expected, season_inputs = per_player_inputs(
            connection, generation_id=generation_id, artifact_kind=artifact_kind
        )
        manifest_id, digest = boundary._freeze_boundary_manifest(
            database, connection, generation_id=generation_id, artifact_kind=artifact_kind
        )
        stored_digest, contents = manifest_contents(connection, manifest_id)
        assert stored_digest == digest == manifest_digest(contents)
        assert contents["generation"] == generation
        assert contents["rule_versions"].get("season_inputs") == season_inputs
        assert [_canonical(row) for row in contents["rows"]] == [
            _canonical(row) for row in expected
        ]
        assert [
            (row[0], row[1], row[2], text(row[3]), text(row[4]), text(row[5]))
            for row in connection.execute(
                """
                SELECT ordinal, player_id, ranked_day_version_id, input_hash,
                       classification, unavailable_reason
                FROM boundary_publication_manifest_entries(%s) ORDER BY ordinal
                """,
                (manifest_id,),
            ).fetchall()
        ] == [
            (
                ordinal,
                row["player_id"],
                row["ranked_day_version_id"],
                row["input_hash"],
                row["classification"],
                "reset_baseline_failed" if row["classification"] == "Unavailable" else None,
            )
            for ordinal, row in enumerate(expected, start=1)
        ]
        base_id, stored_rule_versions = connection.execute(
            "SELECT base_manifest_id, rule_versions FROM boundary_publication_manifests WHERE id = %s",
            (manifest_id,),
        ).fetchone()
        own = dict(
            connection.execute(
                """
                SELECT player_id, identity_manifest_id
                FROM boundary_publication_manifest_rows WHERE manifest_id = %s
                """,
                (manifest_id,),
            ).fetchall()
        )
        frozen[artifact_kind] = (manifest_id, base_id, own, expected, stored_rule_versions)
    return frozen


def test_corrections_store_only_changed_rows_and_rebuild_the_full_manifest(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_id = seed_population(connection_info, PLAYERS)
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                first = _freeze(database, connection, first_id, 1)
                second_id = _correction(connection, first_id, 2)
                _fail_members(connection, second_id, "player_id %% 40 = 1")
                second = _freeze(database, connection, second_id, 2)
                third_id = _correction(connection, second_id, 3)
                _fail_members(connection, third_id, "player_id %% 40 = 2")
                # The Season's army inputs drop some decodes and gain others.
                connection.execute(
                    """
                    UPDATE battle_army_decodes SET is_active = NOT is_active
                    WHERE decoder_version = 'army-decoder-v2'
                      AND battle_id IN (
                          SELECT id FROM legend_battles WHERE attacker_player_id % 40 = 2
                      )
                    """
                )
                third = _freeze(database, connection, third_id, 3)
            for kind in ("snapshot", "army"):
                full_id, full_base, full_own, base_rows, _ = first[kind]
                assert full_base is None and len(full_own) == PLAYERS
                assert set(full_own.values()) == {None}
                second_manifest, second_base, second_own, second_rows, _ = second[kind]
                changed = {
                    row["player_id"]
                    for row, base in zip(second_rows, base_rows, strict=True)
                    if not _same(row, base)
                }
                assert second_base == full_id and changed
                assert second_own == dict.fromkeys(changed)
                _, third_base, third_own, third_rows, rule_versions = third[kind]
                assert third_base == full_id
                # A row the second manifest already stored is named, not copied.
                assert third_own == {
                    row["player_id"]: (
                        second_manifest if _same(row, previous) else None
                    )
                    for row, previous, base in zip(
                        third_rows, second_rows, base_rows, strict=True
                    )
                    if not _same(row, base)
                }
                assert any(third_own.values()) and None in third_own.values()
                if kind == "army":
                    changes = rule_versions["season_input_changes"]
                    assert "season_inputs" not in rule_versions
                    assert changes["decode_ids"]["removed"]
                    assert changes["decode_ids"]["added"]
        finally:
            database.close()


@pytest.mark.parametrize("change", ["membership", "most_rows"])
def test_a_changed_membership_or_most_rows_changing_freezes_a_full_manifest(
    database_url: str, change: str
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_id = seed_population(connection_info, PLAYERS)
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                _freeze(database, connection, first_id, 1)
                second_id = _correction(
                    connection, first_id, 2, first_player=2 if change == "membership" else 1
                )
                if change == "most_rows":
                    _fail_members(connection, second_id, "player_id %% 4 <> 0")
                second = _freeze(database, connection, second_id, 2)
            for kind in ("snapshot", "army"):
                _, base_id, own, rows, rule_versions = second[kind]
                assert base_id is None and len(own) == len(rows)
                assert "season_input_changes" not in rule_versions
        finally:
            database.close()


def test_reuse_reaches_only_a_full_manifest_of_the_same_reset(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_id = seed_population(connection_info, PLAYERS)
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                _freeze(database, connection, first_id, 1)
                second_id = _correction(connection, first_id, 2)
                _fail_members(connection, second_id, "player_id %% 40 = 1")
                second = _freeze(database, connection, second_id, 2)
                third_id = _correction(connection, second_id, 3)
                delta_id, _, own, _, _ = second["snapshot"]
                player_id = next(iter(own))
                connection.commit()
                with pytest.raises(psycopg.errors.RaiseException, match="full manifest"):
                    with connection.transaction():
                        connection.execute(
                            """
                            INSERT INTO boundary_publication_manifests
                                (generation_id, artifact_kind, rule_versions, digest,
                                 base_manifest_id)
                            VALUES (%s, 'snapshot', '{}', repeat('a', 64), %s)
                            """,
                            (third_id, delta_id),
                        )
                with pytest.raises(psycopg.errors.RaiseException, match="stored in full"):
                    with connection.transaction():
                        manifest_id = connection.execute(
                            """
                            INSERT INTO boundary_publication_manifests
                                (generation_id, artifact_kind, rule_versions, digest)
                            VALUES (%s, 'snapshot', '{}', repeat('b', 64))
                            RETURNING id
                            """,
                            (third_id,),
                        ).fetchone()[0]
                        connection.execute(
                            """
                            INSERT INTO boundary_publication_manifest_rows
                                (manifest_id, ordinal, player_id, classification,
                                 identity_manifest_id)
                            VALUES (%s, 1, %s, 'Failed', %s)
                            """,
                            (manifest_id, player_id, manifest_id),
                        )
        finally:
            database.close()


@pytest.mark.parametrize(
    ("base", "values"),
    [
        ([1, 2, 3, 4, 5], [1, 3, 4, 5, 6, 7]),
        ([9, 3, 7, 1], [3, 8, 7, 2, 1]),
        ([], [4, 2]),
        ([5, 6], []),
    ],
)
def test_list_changes_rebuild_the_list_in_its_order(base, values) -> None:
    change = _list_changes(base, values)
    assert change is not None and _apply_list_changes(base, change) == values


@pytest.mark.parametrize(
    ("base", "values"), [([1, 2, 1], [1, 2]), ([1, 2, 3], [3, 2, 1])]
)
def test_list_changes_refuse_lists_they_cannot_rebuild(base, values) -> None:
    assert _list_changes(base, values) is None
