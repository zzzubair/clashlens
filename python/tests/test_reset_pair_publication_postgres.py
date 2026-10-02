from __future__ import annotations

import threading
import time
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_reconciliation_postgres import (
    DAY_END,
    DAY_START,
    _battle_log,
    _processor,
    _store_baseline_pair,
)
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

from clashlens import reconciliation_db
from clashlens.profile import PROFILE_PARSER_VERSION
from clashlens.worker import ObservationProcessor


def test_reset_pair_with_production_parser_versions_publishes_army_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Production processes profiles with parser v3 and battle logs with v2,
    # usually at the same moment. Each Reset check used to look for both
    # results under its own job's parser version, and before the other job
    # committed, so no Reset pair was ever complete, ended days stayed Live
    # and no army analytics job was ever queued.
    monkeypatch.setattr(
        ObservationProcessor, "_process_claim", ObservationProcessor._process_claim_once
    )
    with domain_database(database_url, include_coordinator=True) as connection_info:
        pairs = [
            _store_baseline_pair(
                connection_info,
                archive_server,
                key=key,
                boundary=boundary,
                trophies=trophies,
                empty_battle_log=empty,
                profile_parser_version=PROFILE_PARSER_VERSION,
            )
            for key, boundary, trophies, empty in (
                ("start", DAY_START, 6000, True),
                ("end", DAY_END, 6040, False),
            )
        ]
        # The Reset battle log repeats an already decoded battle, as most do,
        # so neither Reset job takes the pair's lock before its own result.
        _middle_observation, middle_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="middle-battle",
            endpoint="battle_log",
            body=_battle_log(),
            observed_at=DAY_START + timedelta(hours=7),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in (*pairs[0][2:], middle_job):
                result = processor.process_job(job_id, owner=f"source-{job_id}")
                assert result is not None and result.outcome == "processed"
            outcomes: dict[int, str] = {}

            def process(job_id: int) -> None:
                db, own_processor = _processor(connection_info, archive_server)
                try:
                    result = own_processor.process_job(job_id, owner=f"pair-{job_id}")
                    outcomes[job_id] = result.outcome if result else "unclaimed"
                finally:
                    db.close()

            with psycopg.connect(connection_info) as holder:
                lock_key = "reset-baseline:" + str(
                    holder.execute(
                        "SELECT id FROM collector_work WHERE profile_observation_id = %s",
                        (pairs[1][0],),
                    ).fetchone()[0]
                )
                holder.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
                )
                # Start the battle log first: a profile job ahead of it can
                # hold a player row the battle log then waits on.
                threads = []
                for expected, job_id in enumerate(reversed(pairs[1][2:]), start=1):
                    threads.append(threading.Thread(target=process, args=(job_id,)))
                    threads[-1].start()
                    waiting = 0
                    for _ in range(200):
                        waiting = holder.execute(
                            """
                            SELECT count(*) FROM pg_locks
                            WHERE locktype = 'advisory' AND NOT granted
                              AND objid = (hashtextextended(%s, 0) & 4294967295)::oid
                            """,
                            (lock_key,),
                        ).fetchone()[0]
                        if waiting == expected:
                            break
                        time.sleep(0.05)
                    assert waiting == expected, "both Reset jobs must overlap"
            for thread in threads:
                thread.join(timeout=60)
            assert outcomes == dict.fromkeys(pairs[1][2:], "processed")

            boundary = DAY_END.strftime("%Y-%m-%dT%H:%M:%SZ")

            def next_job(work_type: str, boundary: str | None = None) -> int | None:
                with database.pool.connection() as connection:
                    row = connection.execute(
                        """
                        SELECT id FROM python_processing_jobs
                        WHERE work_type = %s AND status = 'pending'
                          AND (%s::text IS NULL OR input_json->>'boundary_at' = %s)
                        ORDER BY id LIMIT 1
                        """,
                        (work_type, boundary, boundary),
                    ).fetchone()
                return None if row is None else int(row[0])

            while (reconcile := next_job("reconcile_ranked_day")) is not None:
                processor.process_job(reconcile, owner="reconcile")
            snapshot = next_job("build_snapshot", boundary)
            assert snapshot is not None, "the ended day never reached publication"
            _process_snapshot_and_analytics(
                connection_info, database, processor, snapshot, owner_prefix="snapshot"
            )
            army_job = next_job("build_army_analytics", boundary)
            assert army_job is not None, "no army analytics job was queued"
            army = processor.process_job(army_job, owner="army")
            assert army is not None and army.outcome == "processed"
            with database.pool.connection() as connection:
                completed_day = connection.execute(
                    "SELECT 1 FROM army_analytics_completed_days"
                    " WHERE ranked_day_start = %s",
                    (DAY_START,),
                ).fetchone()
            assert completed_day is not None
        finally:
            database.close()


def test_republication_finishes_days_left_by_partial_reset_pairs(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Production state on 2026-10-02: both Reset results were processed but
    # the pair's latest check said partial, so the day stayed Live.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        pairs = [
            _store_baseline_pair(
                connection_info,
                archive_server,
                key=key,
                boundary=boundary,
                trophies=trophies,
                empty_battle_log=True,
                profile_parser_version=PROFILE_PARSER_VERSION,
            )
            for key, boundary, trophies in (
                ("start", DAY_START, 6000),
                ("end", DAY_END, 6040),
            )
        ]
        database, processor = _processor(connection_info, archive_server)
        try:
            for job_id in pairs[0][2:]:
                assert (
                    processor.process_job(job_id, owner="start").outcome == "processed"
                )
            with monkeypatch.context() as patch:
                patch.setattr(
                    reconciliation_db.reset_baselines,
                    "_refresh_reset_baseline_evidence",
                    lambda *args, **kwargs: None,
                )
                for job_id in pairs[1][2:]:
                    assert (
                        processor.process_job(job_id, owner="end").outcome
                        == "processed"
                    )
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO reset_baseline_evidence (
                        sweep_id, player_id, boundary_at, collector_work_id,
                        profile_observation_id, battle_log_observation_id,
                        state, failure_reasons, evidence_key
                    )
                    SELECT sweep_id, player_id, %s, id, profile_observation_id,
                           battle_log_observation_id, 'partial',
                           '["unprocessed_profile"]', repeat('e', 64)
                    FROM collector_work WHERE profile_observation_id = %s
                    """,
                    (DAY_END, pairs[1][0]),
                )
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'cancelled'"
                    " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'"
                )
                connection.commit()

            jobs = reconciliation_db.enqueue_current_season_republication(
                database, max_jobs=10
            )
            assert len(jobs) == 1
            assert processor.process_job(jobs[0], owner="repair").outcome == "processed"
            assert (
                reconciliation_db.enqueue_current_season_republication(
                    database, max_jobs=10
                )
                == []
            )
            with database.pool.connection() as connection:
                day_state = connection.execute(
                    """
                    SELECT state FROM ranked_day_versions
                    WHERE ranked_day_start = %s ORDER BY id DESC LIMIT 1
                    """,
                    (DAY_START,),
                ).fetchone()[0]
            assert text(day_state) != "Live"
        finally:
            database.close()
