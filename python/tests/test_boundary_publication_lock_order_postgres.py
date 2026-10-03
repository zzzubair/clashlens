from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_army_analytics_publication_postgres import _insert_confirmed_anchor
from test_boundary_publication_postgres import (
    BOUNDARY,
    DAY_START,
    _player_and_version,
    _sweep_with_members,
)
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    _processor,
    _store_baseline_pair,
)

from clashlens import (
    army_ingestion,
    boundary,
    boundary_publication,
    reconciliation_db,
    reset_baselines,
    snapshots,
)
from clashlens.db import Database


def _advisory_waiters(connection) -> int:
    return int(
        connection.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        ).fetchone()[0]
    )


def _wait_for_waiters(connection, count: int, errors: list[BaseException]) -> None:
    deadline = time.monotonic() + 30
    while _advisory_waiters(connection) < count and not errors:
        assert time.monotonic() < deadline, "work never queued on the Reset lock"
        time.sleep(0.02)


def _run_rebuild_while_build_runs(
    database: Database,
    connection_info: str,
    rebuild: Callable[[psycopg.Connection], None],
    build: Callable[[], None] | None = None,
    *,
    publish: Callable[[psycopg.Connection], None] | None = None,
) -> None:
    """Queue a day-result rebuild, then a build, on a busy Reset lock.

    Something else holds the Reset lock, the rebuild queues on it, then the
    build runs; the rebuild gets the lock first. Before the lock order was
    fixed the build had already locked the Reset's generation row, so the
    rebuild waited for that row while the build waited for the lock.
    ``publish`` runs in the lock holder's transaction once the rebuild queues.
    """
    errors: list[BaseException] = []

    def run(work: Callable[[], None]) -> threading.Thread:
        def target() -> None:
            try:
                work()
            except BaseException as error:  # noqa: BLE001 - asserted below
                errors.append(error)

        thread = threading.Thread(target=target)
        thread.start()
        return thread

    def rebuild_transaction() -> None:
        with psycopg.connect(connection_info) as connection:
            rebuild(connection)
            connection.commit()

    with (
        psycopg.connect(connection_info) as holder,
        psycopg.connect(connection_info, autocommit=True) as observer,
    ):
        boundary.lock_boundary_publication(holder, BOUNDARY)
        threads = [run(rebuild_transaction)]
        _wait_for_waiters(observer, 1, errors)
        if build is not None:
            threads.append(run(build))
            _wait_for_waiters(observer, 2, errors)
        if publish is not None:
            publish(holder)
        holder.commit()
        for thread in threads:
            thread.join(60)
    assert errors == []


def _record_correction(database: Database, player_id: int, version_id: int):
    def rebuild(connection: psycopg.Connection) -> None:
        boundary._record_boundary_generation(
            database,
            connection,
            boundary_at=BOUNDARY,
            player_id=player_id,
            ranked_day_version_id=version_id,
            ranked_day_input_hash="b" * 64,
        )

    return rebuild


def _job_state(database: Database, job_id: int) -> tuple[str, str]:
    with database.pool.connection() as connection:
        row = connection.execute(
            "SELECT status, outcome FROM python_processing_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    return text(row[0]), text(row[1])


@pytest.mark.parametrize("rebuild_kind", ["day_result", "decode_correction"])
def test_snapshot_analytics_build_does_not_deadlock_with_rebuilds(
    database_url: str, rebuild_kind: str
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id, version_id = _player_and_version(
                    connection, "#LOCK1", 1, "a" * 64
                )
                _sweep_with_members(connection, [player_id])
                boundary._record_boundary_generation(
                    database,
                    connection,
                    boundary_at=BOUNDARY,
                    player_id=player_id,
                    ranked_day_version_id=version_id,
                    ranked_day_input_hash="a" * 64,
                )
                corrected = int(
                    connection.execute(
                        """
                        INSERT INTO ranked_day_versions (
                            player_id, ranked_day_start, ranked_day_end,
                            official_season_id, season_day_number,
                            season_anchor_rule_version,
                            reconciliation_rule_version, result_hash, version,
                            state, confidence, input_hash, evidence_complete,
                            coverage_complete
                        ) VALUES (
                            %s, %s, %s, 'test-season', 1, 'test-anchor',
                            'test-rules', %s, 2, 'Complete', 'exact', %s,
                            true, true
                        ) RETURNING id
                        """,
                        (player_id, DAY_START, BOUNDARY, "b" * 64, "b" * 64),
                    ).fetchone()[0]
                )
                snapshot_job = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE work_type = 'build_snapshot'"
                ).fetchone()[0]
                connection.commit()
            claim = database.claim_job(owner="lock-order", job_id=snapshot_job)
            assert claim is not None
            snapshots.complete_snapshot(database, claim)
            with database.pool.connection() as connection:
                analytics_job = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_analytics'"
                    ).fetchone()[0]
                )
            claim = database.claim_job(owner="lock-order", job_id=analytics_job)
            assert claim is not None

            if rebuild_kind == "day_result":
                rebuild = _record_correction(database, player_id, corrected)
            else:

                def rebuild(connection: psycopg.Connection) -> None:
                    # The check a battle log runs when it saves new decodes.
                    boundary_publication._enqueue_army_analytics(
                        database,
                        connection,
                        ranked_day_start=DAY_START,
                        player_ids=[player_id],
                    )

            _run_rebuild_while_build_runs(
                database,
                connection_info,
                rebuild,
                lambda: boundary_publication.complete_analytics(database, claim),
            )
            assert _job_state(database, analytics_job)[0] == "complete"
        finally:
            database.close()


def test_army_build_does_not_deadlock_with_day_result_rebuild(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                players = [
                    int(
                        connection.execute(
                            "INSERT INTO players (normalized_tag, active) VALUES (%s, true) RETURNING id",
                            (tag,),
                        ).fetchone()[0]
                    )
                    for tag in ("#LOCK2", "#LOCK3")
                ]
                sweep_id = _sweep_with_members(connection, players)
                for player_id in players:
                    reset_baselines._record_boundary_baseline(
                        database,
                        connection,
                        boundary_at=BOUNDARY,
                        reset_sweep_id=sweep_id,
                        player_id=player_id,
                        state="failed",
                    )
                connection.commit()
            store_observation(
                connection_info,
                archive_server,
                occurrence_key="lock-order-anchor-source",
                endpoint="profile",
                body=b"{}",
                observed_at=DAY_START,
                normalized_tag="#2PP",
            )
            _insert_confirmed_anchor(database, "1783918800", "1781499600")
            with database.pool.connection() as connection:
                army_job = int(
                    connection.execute(
                        "SELECT id FROM python_processing_jobs WHERE work_type = 'build_army_analytics'"
                    ).fetchone()[0]
                )
                version_id = int(
                    connection.execute(
                        """
                        INSERT INTO ranked_day_versions (
                            player_id, ranked_day_start, ranked_day_end,
                            official_season_id, season_day_number,
                            season_anchor_rule_version,
                            reconciliation_rule_version, result_hash, version,
                            state, confidence, input_hash, evidence_complete,
                            coverage_complete
                        ) VALUES (
                            %s, %s, %s, 'test-season', 1, 'test-anchor',
                            'test-rules', %s, 1, 'Complete', 'exact', %s,
                            true, true
                        ) RETURNING id
                        """,
                        (players[0], DAY_START, BOUNDARY, "b" * 64, "b" * 64),
                    ).fetchone()[0]
                )
                connection.commit()
            claim = database.claim_job(owner="lock-order", job_id=army_job)
            assert claim is not None

            _run_rebuild_while_build_runs(
                database,
                connection_info,
                _record_correction(database, players[0], version_id),
                lambda: army_ingestion.complete_army_analytics(database, claim),
            )
            assert _job_state(database, army_job) == ("complete", "processed")
        finally:
            database.close()


def test_publication_can_point_at_a_result_while_a_rebuild_replaces_it(
    database_url: str, archive_server
) -> None:
    """A real rebuild locks the latest result, then waits for the Reset lock.

    The publication holding that lock still saves a reference to the result.
    When the rebuild locked the result against reference checks, the two
    waited for each other.
    """
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = list(_store_baseline_pair(
            connection_info, archive_server, key="rebuild-start",
            boundary=DAY_START, trophies=6000, empty_battle_log=True,
        )[2:])
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="rebuild-fetch",
            endpoint="battle_log", body=BATTLE_FIXTURE.read_bytes(),
            normalized_tag="#2PP", observed_at=DAY_START + timedelta(hours=8),
        )[1])
        jobs.extend(_store_baseline_pair(
            connection_info, archive_server, key="rebuild-end",
            boundary=BOUNDARY, trophies=6066, empty_battle_log=True,
        )[2:])
        database, processor = _processor(connection_info, archive_server)
        try:
            for job in jobs:
                processor.process_job(job, owner="rebuild")
            while processor.process_once(owner="rebuild-follow-up") is not None:
                pass
            with database.pool.connection() as connection:
                latest, player_id, versions = connection.execute(
                    """
                    SELECT version.id, version.player_id,
                           ARRAY[parser_version, processing_version,
                                 domain_rule_version, analytics_rule_version]
                    FROM ranked_day_versions AS version
                    JOIN players AS player ON player.id = version.player_id
                    WHERE player.normalized_tag = '#2PP'
                      AND version.ranked_day_start = %s
                    ORDER BY version.version DESC LIMIT 1
                    """,
                    (DAY_START,),
                ).fetchone()
                # New evidence changes the day's result, so the rebuild saves
                # a new one and records it on the Reset.
                connection.execute(
                    "UPDATE ranked_day_versions SET result_hash = %s WHERE id = %s",
                    ("c" * 64, latest),
                )
                connection.commit()

            def rebuild(connection: psycopg.Connection) -> None:
                reconciliation_db.recalculate_ranked_day(
                    database, connection, player_id=player_id, day_start=DAY_START,
                    parser_version=versions[0], processing_version=versions[1],
                    domain_rule_version=versions[2],
                    analytics_rule_version=versions[3],
                )

            def publish(connection: psycopg.Connection) -> None:
                # Already replaced, so the rebuild records its result on the
                # Reset's real generation as usual.
                generation = connection.execute(
                    """
                    INSERT INTO boundary_publication_generations (
                        boundary_at, target_at, generation, ordering_rule_version,
                        freshness_rule_version, expected_population_count,
                        expected_population_hash, snapshot_state, army_state
                    ) SELECT %s, %s, coalesce(max(generation), 0) + 100, 'test',
                             'test', 1, %s, 'superseded', 'superseded'
                    FROM boundary_publication_generations
                    RETURNING id
                    """,
                    (BOUNDARY, BOUNDARY, "0" * 64),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO boundary_publication_generation_members (
                        generation_id, player_id, ranked_day_version_id
                    ) VALUES (%s, %s, %s)
                    """,
                    (generation, player_id, latest),
                )

            _run_rebuild_while_build_runs(
                database, connection_info, rebuild, publish=publish
            )
            with database.pool.connection() as connection:
                assert connection.execute(
                    """
                    SELECT replaces_version_id FROM ranked_day_versions
                    WHERE player_id = %s AND ranked_day_start = %s
                    ORDER BY version DESC LIMIT 1
                    """,
                    (player_id, DAY_START),
                ).fetchone()[0] == latest
        finally:
            database.close()
