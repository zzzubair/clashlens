from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_army_analytics_publication_postgres import _insert_confirmed_anchor
from test_army_ingestion_postgres import _live_row, _unswept_battles
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
    database_url: str, archive_server, monkeypatch
) -> None:
    """A real rebuild locks the latest result, then waits for the Reset lock.

    The publication holding that lock still saves a reference to the result.
    When the rebuild locked the result against reference checks, the two
    waited for each other.
    """
    # The rebuild keeps waiting while the publication holds the Reset.
    monkeypatch.setattr(reconciliation_db, "RESET_LOCK_WAIT", "10s")
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


def test_battle_logs_sharing_a_reset_when_its_sweep_is_saved_both_finish(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Two jobs sharing a Reset's lock when its sweep was saved each waited
    # for the other to take the full lock, and the database aborted one.
    with domain_database(database_url, include_coordinator=True) as ci:
        battles = _unswept_battles(
            ci, archive_server, {"#2PP": "u1x58", "#2PQ": "u2x58"}
        )

        def save_sweep() -> None:
            with psycopg.connect(ci, autocommit=True) as collector:
                players = collector.execute("SELECT id FROM players").fetchall()
                _sweep_with_members(collector, [row[0] for row in players])

        barrier = threading.Barrier(2, action=save_sweep, timeout=10)
        original = boundary.lock_boundary_publication_once_swept
        held = threading.local()

        def hold_until_both_share(connection, boundary_at):
            swept = original(connection, boundary_at)
            if not getattr(held, "done", False):
                held.done = True
                barrier.wait()
            return swept

        monkeypatch.setattr(
            boundary, "lock_boundary_publication_once_swept", hold_until_both_share
        )
        db = Database(ci)
        results: dict[str, str] = {}

        def save(tag: str) -> None:
            try:
                with db.pool.connection() as connection:
                    with connection.transaction():
                        army_ingestion._upsert_army_decodes(db, connection, battles[tag])
                results[tag] = "saved"
            except Exception as error:  # noqa: BLE001 - reported by the assertion
                results[tag] = repr(error)

        threads = [threading.Thread(target=save, args=(tag,)) for tag in battles]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        finally:
            db.close()

    assert results == {"#2PP": "saved", "#2PQ": "saved"}


def test_redecode_after_its_evidence_check_keeps_a_later_correction(
    database_url: str, archive_server, monkeypatch
) -> None:
    # A redecode that had already checked the battle's report replaced a
    # correction saved meanwhile with the older army.
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        jobs = []
        for occurrence, code, minutes in (
            ("before-correction", "u1x58", 1),
            ("after-correction", "u2x58", 5),
        ):
            _, job_id = store_observation(
                ci,
                archive_server,
                occurrence_key=occurrence,
                endpoint="battle_log",
                body=json.dumps({"items": [_live_row(True, "#8PP", code, ts)]}).encode(),
                observed_at=ts + timedelta(minutes=minutes),
                normalized_tag="#2PP",
            )
            jobs.append(job_id)
        db, proc = _processor(ci, archive_server)
        checked, resume = threading.Event(), threading.Event()
        results: dict[str, str] = {}

        def run(name, work) -> None:
            try:
                results[name] = work()
            except Exception as error:  # noqa: BLE001 - reported by the assertion
                results[name] = repr(error)

        def redecode() -> str:
            with db.pool.connection() as connection:
                with connection.transaction():
                    army_ingestion._upsert_army_decodes(db, connection, battle_ids)
            return "processed"

        redecoder = threading.Thread(target=run, args=("redecode", redecode))
        corrector = threading.Thread(
            target=run,
            args=(
                "correction",
                lambda: proc.process_job(jobs[1], owner="correction").outcome,
            ),
        )
        original_execute = psycopg.Connection.execute

        def pause_after_evidence_check(connection, query, params=None, **kwargs):
            cursor = original_execute(connection, query, params, **kwargs)
            if (
                threading.current_thread() is redecoder
                and "FROM battle_perspectives" in str(query)
                and not checked.is_set()
            ):
                checked.set()
                assert resume.wait(timeout=20), "correction never released the redecode"
            return cursor

        try:
            assert proc.process_job(jobs[0], owner="seed").outcome == "processed"
            with db.pool.connection() as connection:
                battle_ids = [
                    row[0]
                    for row in connection.execute("SELECT id FROM legend_battles")
                ]
                connection.execute(
                    "UPDATE battle_army_decodes SET decoder_version = 'army-decoder-v1'"
                )
            monkeypatch.setattr(
                psycopg.Connection, "execute", pause_after_evidence_check
            )
            redecoder.start()
            assert checked.wait(timeout=10), "redecode never checked the report"
            corrector.start()
            deadline = time.monotonic() + 10
            with psycopg.connect(ci, autocommit=True) as observer:
                while corrector.is_alive() and not _advisory_waiters(observer):
                    assert time.monotonic() < deadline, "correction never finished or waited"
                    time.sleep(0.02)
            resume.set()
            redecoder.join(timeout=20)
            corrector.join(timeout=20)
            with db.pool.connection() as connection:
                active = connection.execute(
                    """
                    SELECT raw_code FROM battle_army_decodes
                    WHERE is_active AND decoder_version = %s
                    """,
                    (army_ingestion.DECODER_VERSION,),
                ).fetchall()
        finally:
            resume.set()
            for thread in (redecoder, corrector):
                if thread.ident is not None:
                    thread.join(timeout=20)
            db.close()

    assert results == {"redecode": "processed", "correction": "processed"}
    assert [text(row[0]) for row in active] == ["u2x58"]
