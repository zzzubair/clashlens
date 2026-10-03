from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_reconciliation_postgres import (
    DAY_END,
    DAY_START,
    _battle_log,
    _processor,
    _store_baseline_pair,
)
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

from clashlens import army_ingestion, boundary, boundary_publication
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


def _sweep_with_members(connection, player_ids: list[int], boundary: datetime) -> int:
    sweep = int(
        connection.execute(
            """
            INSERT INTO collector_reset_sweeps
                (boundary_at, member_ids, membership_captured_at)
            VALUES (%s, %s, clock_timestamp())
            RETURNING id
            """,
            (boundary, player_ids),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
            coalescing_key, profile_status, battle_log_status
        )
        SELECT 'reset_baseline', 'reset', 'player', player.id,
               player.normalized_tag, %s, %s,
               'reset:' || %s || ':' || player.id, 'pending', 'pending'
        FROM players AS player
        WHERE player.id = ANY(%s::bigint[])
        ON CONFLICT DO NOTHING
        """,
        (sweep, boundary, sweep, player_ids),
    )
    return sweep


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
                _sweep_with_members(connection, [first[0], second[0]], BOUNDARY)
                for player, ranked in (first, second):
                    boundary._record_boundary_generation(database, 
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
                assert connection.execute(
                    "SELECT membership_captured_at FROM collector_reset_sweeps WHERE boundary_at = %s",
                    (BOUNDARY,),
                ).fetchone()[0] is not None
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
                next_sweep = _sweep_with_members(
                    connection, [first[0]], next_boundary
                )
                next_generation, _ = boundary._create_boundary_generation(database, 
                    connection,
                    boundary_at=next_boundary,
                    sweep_id=next_sweep,
                    player_ids=[first[0]],
                    generation=1,
                    supersedes_id=None,
                )
                next_manifest = boundary._freeze_boundary_manifest(database, 
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
                boundary._try_enqueue_boundary_artifacts(database, 
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


def _freeze_army_inputs(connection_info: str, archive_server, processor):
    """Process one Legend day up to its Reset's frozen, unbuilt army build.

    Returns a pending-jobs lookup for that Reset and the army job and input.
    """
    pairs = [
        _store_baseline_pair(
            connection_info,
            archive_server,
            key=key,
            boundary=day,
            trophies=trophies,
            empty_battle_log=empty,
        )
        for key, day, trophies, empty in (
            ("start", DAY_START, 6000, True),
            ("end", DAY_END, 6040, False),
        )
    ]
    middle = store_observation(
        connection_info,
        archive_server,
        occurrence_key="middle",
        endpoint="battle_log",
        body=_battle_log(),
        observed_at=DAY_START + timedelta(hours=7),
        normalized_tag="#2PP",
    )[1]
    boundary_at = DAY_END.strftime("%Y-%m-%dT%H:%M:%SZ")
    for job_id in (*pairs[0][2:], middle, *pairs[1][2:]):
        assert processor.process_job(job_id, owner="source").outcome == "processed"

    def jobs(work_type: str) -> list[tuple]:
        with processor.database.pool.connection() as connection:
            return connection.execute(
                "SELECT id, input_json FROM python_processing_jobs"
                " WHERE work_type = %s AND status = 'pending'"
                " AND coalesce(input_json->>'boundary_at', %s) = %s"
                " ORDER BY id",
                (work_type, boundary_at, boundary_at),
            ).fetchall()

    for job_id, _input in jobs("reconcile_ranked_day"):
        assert processor.process_job(job_id, owner="day").outcome == "processed"
    [(army_job, army_input)] = jobs("build_army_analytics")
    [(snapshot_job, _input)] = jobs("build_snapshot")
    _process_snapshot_and_analytics(
        connection_info,
        processor.database,
        processor,
        snapshot_job,
        owner_prefix="snapshot",
    )
    return jobs, army_job, army_input


def _process_changed_army(
    connection_info: str, archive_server, processor, *, key: str, code: str
) -> None:
    """Save and process a battle log giving the frozen battle a new army."""
    body = json.loads(_battle_log())
    body["items"][0]["armyShareCode"] = code
    changed = store_observation(
        connection_info,
        archive_server,
        occurrence_key=key,
        endpoint="battle_log",
        body=json.dumps(body).encode(),
        observed_at=DAY_END + timedelta(minutes=10),
        normalized_tag="#2PP",
    )[1]
    assert processor.process_job(changed, owner=key).outcome == "processed"


def test_army_correction_during_publication_keeps_each_frozen_battle(
    database_url: str, archive_server
) -> None:
    # A changed army code for an already-known battle arrives after the
    # army inputs are frozen and before the army build finishes.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            jobs, army_job, army_input = _freeze_army_inputs(
                connection_info, archive_server, processor
            )
            _process_changed_army(
                connection_info,
                archive_server,
                processor,
                key="changed-after-freeze",
                code="u3x0-2x1",
            )

            def current_fact() -> tuple:
                with database.pool.connection() as connection:
                    return connection.execute(
                        "SELECT battle_id, evidence_id, decode_id"
                        " FROM army_analytics_battle_facts WHERE is_current"
                    ).fetchall()

            with database.pool.connection() as connection:
                [frozen] = connection.execute(
                    "SELECT input_identity FROM boundary_publication_manifest_rows"
                    " WHERE manifest_id = %s",
                    (army_input["manifest_id"],),
                ).fetchall()
                frozen = frozen[0]
                assert connection.execute(
                    "SELECT evidence_id FROM battle_perspectives"
                ).fetchone()[0] not in frozen["evidence_ids"]
                # A frozen battle whose evidence is not in the manifest fails
                # the build instead of disappearing from a completed day.
                with pytest.raises(ValueError, match="frozen army evidence missing"):
                    army_ingestion._build_army_facts(
                        database,
                        connection,
                        DAY_START.isoformat(),
                        member_ids=[frozen["player_id"]],
                        ranked_version_ids=[frozen["ranked_day_version_id"]],
                        decode_ids=frozen["decode_ids"],
                        evidence_ids=[],
                        daily_log_ids=[frozen["daily_log_id"]],
                        battle_ids=frozen["battle_ids"],
                    )
                connection.rollback()

            # The original publication finishes with its frozen battle and
            # decode, then the queued correction publishes the changed decode.
            assert processor.process_job(army_job, owner="army").outcome == "processed"
            assert current_fact() == [
                (
                    frozen["battle_ids"][0],
                    frozen["evidence_ids"][0],
                    frozen["decode_ids"][0],
                )
            ]
            [(correction_job, correction_input)] = jobs("build_army_analytics")
            assert correction_input["generation"] == army_input["generation"] + 1
            assert (
                processor.process_job(correction_job, owner="correction").outcome
                == "processed"
            )
            [(battle_id, evidence_id, decode_id)] = current_fact()
            assert battle_id == frozen["battle_ids"][0]
            assert evidence_id not in frozen["evidence_ids"]
            assert decode_id not in frozen["decode_ids"]
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_completed_days"
                    " WHERE ranked_day_start = %s",
                    (DAY_START,),
                ).fetchone()[0] == 1
        finally:
            database.close()


def test_army_build_leaves_the_generation_unlocked_until_it_publishes(
    database_url: str, archive_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The snapshot and analytics builds lock the generation row. A day's army
    # build used to hold that lock for its whole run, so a Reset publication's
    # two builds ran one after the other.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        pairs = [
            _store_baseline_pair(
                connection_info,
                archive_server,
                key=key,
                boundary=day,
                trophies=trophies,
                empty_battle_log=empty,
            )
            for key, day, trophies, empty in (
                ("start", DAY_START, 6000, True),
                ("end", DAY_END, 6040, False),
            )
        ]
        middle = store_observation(
            connection_info,
            archive_server,
            occurrence_key="middle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )[1]
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in (*pairs[0][2:], middle, *pairs[1][2:]):
                assert processor.process_job(job_id, owner="source").outcome == "processed"
            with database.pool.connection() as connection:
                for (job_id,) in connection.execute(
                    "SELECT id FROM python_processing_jobs"
                    " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'"
                ).fetchall():
                    assert processor.process_job(job_id, owner="day").outcome == "processed"
                [(army_job,)] = connection.execute(
                    "SELECT id FROM python_processing_jobs"
                    " WHERE work_type = 'build_army_analytics' AND status = 'pending'"
                    " AND input_json->>'boundary_at' = %s",
                    (DAY_END.strftime("%Y-%m-%dT%H:%M:%SZ"),),
                ).fetchall()

            real_build = army_ingestion._build_army_facts
            locked_during_build = []

            def probe_then_build(*args, **kwargs):
                with psycopg.connect(connection_info) as other:
                    try:
                        other.execute(
                            "SELECT id FROM boundary_publication_generations"
                            " WHERE boundary_at = %s FOR UPDATE NOWAIT",
                            (DAY_END,),
                        ).fetchall()
                        locked_during_build.append(False)
                    except psycopg.errors.LockNotAvailable:
                        locked_during_build.append(True)
                return real_build(*args, **kwargs)

            monkeypatch.setattr(army_ingestion, "_build_army_facts", probe_then_build)
            # One manifest row per page also reads past the last full page.
            monkeypatch.setattr(army_ingestion, "ARMY_FACT_PLAYER_BATCH", 1)
            assert processor.process_job(army_job, owner="army").outcome == "processed"
            assert locked_during_build == [False]
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT army_state FROM boundary_publication_generations"
                    " WHERE boundary_at = %s",
                    (DAY_END,),
                ).fetchone()[0] == "published"
                facts = connection.execute(
                    "SELECT battle_id, lens, input_hash FROM army_analytics_battle_facts"
                    " WHERE ranked_day_start = %s AND is_current ORDER BY battle_id, lens",
                    (DAY_START,),
                ).fetchall()
                assert len(facts) == 1
                # The day's hash keeps its earlier definition, so a rebuilt
                # day with unchanged facts keeps its published identity.
                assert connection.execute(
                    "SELECT fact_input_hash FROM army_analytics_completed_days"
                    " WHERE ranked_day_start = %s",
                    (DAY_START,),
                ).fetchone()[0] == hashlib.sha256(
                    json.dumps(
                        [[int(row[0]), text(row[1]), text(row[2])] for row in facts],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                # The fact keeps no copy of its army; reads get the decode's.
                armies = "home_troops, spells, siege, cc_troops, heroes, unresolved_components"
                assert connection.execute(
                    f"SELECT {armies} FROM army_analytics_battle_facts"
                ).fetchone() == (None,) * 6
                read = connection.execute(
                    f"SELECT decode_id, {armies} FROM army_analytics_battle_facts_with_armies"
                ).fetchone()
                assert read[0] is not None
                assert read[1:] == tuple(
                    value if value is not None else []
                    for value in connection.execute(
                        f"SELECT {armies} FROM battle_army_decodes WHERE id = %s",
                        (read[0],),
                    ).fetchone()
                )
        finally:
            database.close()


def test_changed_armies_check_only_their_players_frozen_army_inputs(
    database_url: str, archive_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every battle log that re-decodes a frozen Reset's battle checks, under
    # that Reset's lock, whether its army build needs a correction. Reading
    # the whole frozen list (13,000 players, 110 MB on production) per job
    # ran the worker out of memory and made all its lanes take turns.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _jobs, _army_job, army_input = _freeze_army_inputs(
                connection_info, archive_server, processor
            )
            check = boundary_publication._boundary_army_manifest_needs_correction
            checked: list[list[int] | None] = []

            def spy(database, connection, *, manifest_id, player_ids):
                checked.append(player_ids)
                return check(
                    database,
                    connection,
                    manifest_id=manifest_id,
                    player_ids=player_ids,
                )

            monkeypatch.setattr(
                boundary_publication, "_boundary_army_manifest_needs_correction", spy
            )
            for key, code in (("first", "u3x0-2x1"), ("second", "u4x0-1x1")):
                _process_changed_army(
                    connection_info, archive_server, processor, key=key, code=code
                )
            with database.pool.connection() as connection:
                battle_players = sorted(
                    int(value)
                    for value in connection.execute(
                        "SELECT attacker_player_id, defender_player_id"
                        " FROM legend_battles"
                    ).fetchone()
                )
                # Each job read only its battle's two players.
                assert checked == [battle_players, battle_players]
                # One correction carries one army marker, however many jobs
                # found the change.
                assert connection.execute(
                    "SELECT pending_inputs FROM boundary_publication_corrections"
                    " WHERE state IN ('queued', 'pending_inputs')"
                ).fetchall() == [([{"kind": "decode"}],)]
                # A player outside the changed battle reads nothing, though
                # the Reset's frozen army is now out of date.
                assert check(
                    database,
                    connection,
                    manifest_id=army_input["manifest_id"],
                    player_ids=battle_players,
                )
                assert not check(
                    database,
                    connection,
                    manifest_id=army_input["manifest_id"],
                    player_ids=[max(battle_players) + 1000],
                )
        finally:
            database.close()


def _merge_into_day_before(connection, old: int) -> int:
    """Do to battle ``old`` what 0057 did to a merged battle; return its new row."""
    target = connection.execute(
        """
        INSERT INTO legend_battles
            (ranked_day_start, attacker_player_id, defender_player_id)
        SELECT ranked_day_start - interval '1 day',
               attacker_player_id, defender_player_id
        FROM legend_battles WHERE id = %s
        RETURNING id
        """,
        (old,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO battle_day_repairs (
            from_battle_id, to_battle_id, perspective, evidence_id,
            attacker_player_id, defender_player_id, from_day, to_day
        )
        SELECT battle.id, %s, side.perspective, side.evidence_id,
               battle.attacker_player_id, battle.defender_player_id,
               battle.ranked_day_start,
               battle.ranked_day_start - interval '1 day'
        FROM legend_battles AS battle
        JOIN battle_perspectives AS side ON side.battle_id = battle.id
        WHERE battle.id = %s
        """,
        (target, old),
    )
    for table in (
        "battle_evidence",
        "battle_perspectives",
        "battle_army_decodes",
        "army_analytics_battle_facts",
    ):
        connection.execute(
            f"UPDATE {table} SET battle_id = %s WHERE battle_id = %s",
            (target, old),
        )
    connection.execute("DELETE FROM legend_battles WHERE id = %s", (old,))
    return target


def test_frozen_army_build_follows_a_battle_merged_after_the_freeze(
    database_url: str, archive_server
) -> None:
    # Migration 0057 moved a frozen report into the day before's battle row
    # and deleted the old row; the Reset's army build then failed with
    # "frozen army evidence missing" on every retry.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _jobs, army_job, army_input = _freeze_army_inputs(
                connection_info, archive_server, processor
            )
            with database.pool.connection() as connection:
                [(frozen,)] = connection.execute(
                    "SELECT input_identity FROM boundary_publication_manifest_rows"
                    " WHERE manifest_id = %s",
                    (army_input["manifest_id"],),
                ).fetchall()
                [old] = frozen["battle_ids"]
                target = _merge_into_day_before(connection, old)
                # The moved decode is still the frozen one: no correction.
                assert (
                    not boundary_publication._boundary_army_manifest_needs_correction(
                        database,
                        connection,
                        manifest_id=army_input["manifest_id"],
                        player_ids=[frozen["player_id"]],
                    )
                )
            assert processor.process_job(army_job, owner="army").outcome == "processed"
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT battle_id, evidence_id, decode_id"
                    " FROM army_analytics_battle_facts WHERE is_current"
                ).fetchall() == [
                    (target, frozen["evidence_ids"][0], frozen["decode_ids"][0])
                ]
        finally:
            database.close()


def test_army_frozen_after_a_merge_reads_the_moved_report(
    database_url: str, archive_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Oct 1 Reset's army inputs were frozen after 0057 had moved a report
    # but before the day was rebuilt: they named the old battle and, its row
    # gone, no report for it. Job 2074885 then failed with "frozen army
    # evidence missing for battle 48183 offense".
    freeze = boundary._freeze_boundary_manifest
    moves: list[tuple[int, int]] = []

    def merge_then_freeze(database, connection, *, generation_id, artifact_kind):
        if artifact_kind == "army" and not moves:
            [(old,)] = connection.execute("SELECT id FROM legend_battles").fetchall()
            moves.append((old, _merge_into_day_before(connection, old)))
        return freeze(
            database,
            connection,
            generation_id=generation_id,
            artifact_kind=artifact_kind,
        )

    monkeypatch.setattr(boundary, "_freeze_boundary_manifest", merge_then_freeze)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _jobs, army_job, army_input = _freeze_army_inputs(
                connection_info, archive_server, processor
            )
            [(old, target)] = moves
            with database.pool.connection() as connection:
                [(frozen,)] = connection.execute(
                    "SELECT input_identity FROM boundary_publication_manifest_rows"
                    " WHERE manifest_id = %s",
                    (army_input["manifest_id"],),
                ).fetchall()
                assert (frozen["battle_ids"], frozen["evidence_ids"]) == ([old], [])
                [(moved_report,)] = connection.execute(
                    "SELECT evidence_id FROM battle_day_repairs"
                    " WHERE perspective = 'attacker'"
                ).fetchall()
            assert processor.process_job(army_job, owner="army").outcome == "processed"
            with database.pool.connection() as connection:
                # Its decode was frozen as missing, as it was then.
                assert connection.execute(
                    "SELECT battle_id, evidence_id, decode_id"
                    " FROM army_analytics_battle_facts WHERE is_current"
                ).fetchall() == [(target, moved_report, None)]
                # A side with no recorded move still fails clearly.
                connection.execute("DELETE FROM battle_day_repairs")
                with pytest.raises(
                    ValueError,
                    match=f"frozen army evidence missing for battle {old} ",
                ):
                    army_ingestion._build_army_fact_batch(
                        connection,
                        DAY_START,
                        connection.execute(
                            "SELECT ranked_day_version_id, player_id, battles,"
                            " official_season_id, season_day_number, NULL"
                            " FROM api_player_daily_logs WHERE id = %s",
                            (frozen["daily_log_id"],),
                        ).fetchall(),
                        battle_ids=[old],
                        decode_ids=[],
                        evidence_ids=[],
                        active_keys=set(),
                    )
        finally:
            database.close()
