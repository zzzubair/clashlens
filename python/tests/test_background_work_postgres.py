"""Background work never holds up live tracking.

On 9 Oct 2026 six army re-decodes ran at once, each holding about 103 battle
locks for 5 to 14 seconds; live battle logs that needed those battles gave
way, and live responses fell to about 8,000 players over 2 minutes late.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_army_ingestion_postgres import _live_row, _processor

from clashlens import army_ingestion
from clashlens.background_pacing import BACKGROUND_JOB_LIMIT
from clashlens.db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
)

DAY = datetime(2026, 10, 8, 5, tzinfo=UTC)
OPPONENTS = [f"#8{first}{second}" for first in "PYLQGRJCUV" for second in "PYL"]


def _queue_redecodes(connection_info: str, batches: list[list[int]]) -> list[int]:
    """Re-decode jobs exactly as migration 0089 queues them."""
    with psycopg.connect(connection_info) as connection:
        return [
            connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, priority,
                    processing_version, domain_rule_version, analytics_rule_version
                ) VALUES ('redecode_army', %s, %s::jsonb, 25, %s, %s, %s)
                RETURNING id
                """,
                (f"redecode-test:{index}", json.dumps({"battle_ids": battle_ids}),
                 PROCESSING_VERSION, DOMAIN_RULE_VERSION, ARMY_ANALYTICS_RULE_VERSION),
            ).fetchone()[0]
            for index, battle_ids in enumerate(batches)
        ]


def _queue_result(
    connection_info: str, key: str, *, priority: int = 100,
    due_at: datetime | None = None, processing_version: str = PROCESSING_VERSION,
) -> int:
    """A daily-result job: live at priority 100, background at 25."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            INSERT INTO python_processing_jobs (
                work_type, deduplication_key, input_json, priority, due_at,
                parser_version, processing_version, domain_rule_version,
                analytics_rule_version
            ) VALUES ('reconcile_ranked_day', %s,
                      '{"player_id": 1, "ranked_day_start": "2026-10-08T05:00:00Z"}', %s,
                      COALESCE(%s, clock_timestamp()), %s, %s, %s, %s)
            RETURNING id
            """,
            (key, priority, due_at, DEFAULT_PARSER_VERSION, processing_version,
             DOMAIN_RULE_VERSION, ANALYTICS_RULE_VERSION),
        ).fetchone()[0]


def test_at_most_two_background_jobs_run_across_worker_processes(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        _queue_redecodes(connection_info, [[index] for index in range(1, 6)])
        first, second = Database(connection_info), Database(connection_info)
        try:
            # One claim of up to 8 jobs takes one background job, never a batch
            # of them; on 9 Oct 2026 568 jobs were leased at once.
            claimed = [
                first.claim_jobs(owner="process-1", limit=8),
                second.claim_jobs(owner="process-2", limit=8),
            ]
            assert [[claim.work_type for claim in claims] for claims in claimed] == [
                ["redecode_army"], ["redecode_army"]
            ]
            assert BACKGROUND_JOB_LIMIT == 2
            assert first.claim_jobs(owner="process-1", limit=8) == []
            # Live work is never held behind them.
            live = [_queue_result(connection_info, f"live:{index}") for index in range(2)]
            claims = second.claim_jobs(owner="process-2", limit=8)
            assert sorted(claim.job_id for claim in claims) == live
            # A finished background job frees its turn, and live work still goes first.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'complete',"
                    " lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL"
                    " WHERE id = %s",
                    (claimed[0][0].job_id,),
                )
            later = _queue_result(connection_info, "live:later")
            claims = first.claim_jobs(owner="process-1", limit=8)
            assert [(claim.job_id, claim.work_type) for claim in claims] == [
                (later, "reconcile_ranked_day"), (claims[1].job_id, "redecode_army")
            ]
            assert first.claim_jobs(owner="process-1", limit=8) == []
            # Still running past their leases, their transactions holding the
            # rows, they keep both turns.
            running = [claimed[1][0].job_id, claims[1].job_id]
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET lease_expires_at ="
                    " clock_timestamp() - interval '1 minute' WHERE id = ANY(%s)",
                    (running,),
                )
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "SELECT FROM python_processing_jobs WHERE id = ANY(%s) FOR UPDATE",
                    (running,),
                )
                assert first.claim_jobs(owner="process-1", limit=8) == []
        finally:
            first.close()
            second.close()


def test_background_work_waits_while_live_work_is_two_minutes_late(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        _queue_redecodes(connection_info, [[1], [2]])
        now = datetime.now(UTC)
        live = _queue_result(connection_info, "live", due_at=now - timedelta(minutes=3))
        database = Database(connection_info)
        try:
            # This lane cannot take the late result, but starts no background job.
            assert database.claim_jobs(owner="lane", work_types=["redecode_army"]) == []
            # Season repair waiting an hour is background too and pauses nothing.
            _queue_result(connection_info, "season-repair", priority=25,
                          due_at=now - timedelta(hours=1))
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET due_at = %s WHERE id = %s",
                    (now - timedelta(minutes=1), live),
                )
            assert [claim.work_type for claim in database.claim_jobs(
                owner="lane", work_types=["redecode_army"]
            )] == ["redecode_army"]
            # Live work this worker cannot take, such as a newer version's, never
            # pauses background work for good.
            _queue_result(connection_info, "newer", processing_version="future",
                          due_at=now - timedelta(hours=1))
            assert [claim.work_type for claim in database.claim_jobs(
                owner="lane", work_types=["redecode_army"]
            )] == ["redecode_army"]
        finally:
            database.close()


def test_background_work_waits_while_late_live_work_is_leased_or_waiting(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        _queue_redecodes(connection_info, [[1], [2]])
        now = datetime.now(UTC)
        live = _queue_result(connection_info, "live", due_at=now - timedelta(minutes=3))
        database = Database(connection_info)
        try:
            # A live lane takes the late result, hits a busy battle lock and
            # keeps its lease with the attempt refunded, as on 9 Oct 2026.
            [claim] = database.claim_jobs(owner="live", work_types=["reconcile_ranked_day"])
            assert claim.job_id == live
            database.refund_claim_attempt(claim)
            assert database.claim_jobs(owner="lane", work_types=["redecode_army"]) == []
            # Still running past its lease on its last attempt, it pauses them too.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET attempt_count = max_attempts,"
                    " lease_expires_at = clock_timestamp() - interval '1 minute'"
                    " WHERE id = %s",
                    (live,),
                )
            assert database.claim_jobs(owner="lane", work_types=["redecode_army"]) == []
            # Waiting for its saved response on its last try, it can still
            # resume, so it pauses background work too.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'waiting_dependency',"
                    " attempt_count = max_attempts, lease_owner = NULL,"
                    " lease_token = NULL, lease_expires_at = NULL WHERE id = %s",
                    (live,),
                )
            assert database.claim_jobs(owner="lane", work_types=["redecode_army"]) == []
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'complete' WHERE id = %s",
                    (live,),
                )
            assert [claim.work_type for claim in database.claim_jobs(
                owner="lane", work_types=["redecode_army"]
            )] == ["redecode_army"]
        finally:
            database.close()


def _seed_redecode(
    connection_info: str, archive_server, database, processor, *, readable: bool = False
) -> list[int]:
    """30 saved battles whose armies failed to read, or (readable) were read
    under the old unit list, and one re-decode job for them all."""
    _, job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key="seed",
        endpoint="battle_log",
        body=json.dumps({"items": [
            _live_row(True, tag, f"u{index + 1}x0", DAY + timedelta(minutes=index))
            for index, tag in enumerate(OPPONENTS)
        ]}).encode(),
        observed_at=DAY + timedelta(hours=1),
        normalized_tag="#2PP",
    )
    assert processor.process_job(job_id, owner="seed").outcome == "processed"
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end,
                official_season_id, season_day_number,
                season_anchor_rule_version, reconciliation_rule_version,
                result_hash, version, state, confidence, coverage_complete
            )
            SELECT id, %s, %s, 'test-season', 4, 'test-anchor', 'test-reconciliation',
                   repeat('a', 64), 1, 'Live', 'exact', false
            FROM players WHERE normalized_tag = '#2PP'
            """,
            (DAY, DAY + timedelta(days=1)),
        )
        if readable:
            connection.execute(
                "UPDATE battle_army_decodes SET catalog_version = 'unit-catalog-v2'"
            )
        else:
            # Saved while the unit list was unavailable, as earlier code did; a
            # re-decode reads these armies again.
            connection.execute(
                """
                UPDATE battle_army_decodes
                SET status = 'failed', failure_category = 'catalog_version_unavailable',
                    failure_detail = 'pinned unit catalog is unavailable or has the wrong hash',
                    exact_army_id = NULL, identity_hash = NULL
                """
            )
        battle_ids = [row[0] for row in connection.execute(
            "SELECT id FROM legend_battles ORDER BY id"
        ).fetchall()]
    assert len(battle_ids) == len(OPPONENTS)
    return battle_ids


def _upgraded(database: Database) -> int:
    with database.pool.connection() as connection:
        return connection.execute(
            "SELECT count(DISTINCT battle_id) FROM battle_army_decodes"
            " WHERE is_active AND status = 'decoded'"
        ).fetchone()[0]


def _battle_locks(connection: psycopg.Connection, pid: int, battle_ids: list[int]) -> int:
    return connection.execute(
        """
        SELECT count(*) FROM pg_locks AS lock
        JOIN unnest(%s::bigint[]) AS battle (id)
          ON lock.classid::bigint
                 = (hashtextextended('army-redecode-battle:' || battle.id, 0) >> 32) & 4294967295
         AND lock.objid::bigint
                 = hashtextextended('army-redecode-battle:' || battle.id, 0) & 4294967295
        WHERE lock.locktype = 'advisory' AND lock.objsubid = 1
          AND lock.granted AND lock.pid = %s
        """,
        (battle_ids, pid),
    ).fetchone()[0]


def test_a_live_battle_log_never_waits_for_a_whole_redecode(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            battle_ids = _seed_redecode(connection_info, archive_server, database, processor)
            [redecode] = _queue_redecodes(connection_info, [battle_ids])
            # A later battle log of the same player repeats its newest battle.
            _, live = store_observation(
                connection_info,
                archive_server,
                occurrence_key="live",
                endpoint="battle_log",
                body=json.dumps({"items": [_live_row(
                    True, OPPONENTS[-1], f"u{len(OPPONENTS)}x0",
                    DAY + timedelta(minutes=len(OPPONENTS) - 1),
                )]}).encode(),
                observed_at=DAY + timedelta(hours=2),
                normalized_tag="#2PP",
            )
            # Hold the re-decode inside its first write, with its locks taken.
            holding, release = threading.Event(), threading.Event()
            holder: list[int] = []
            upsert = army_ingestion._upsert_army_decodes

            def held_upsert(db, connection, ids, **kwargs):
                upsert(db, connection, ids, **kwargs)
                if threading.current_thread().name == "redecode" and not holding.is_set():
                    holder.append(connection.info.backend_pid)
                    holding.set()
                    assert release.wait(20)

            monkeypatch.setattr(army_ingestion, "_upsert_army_decodes", held_upsert)
            results: dict[str, str] = {}

            def run(name: str, job_id: int) -> threading.Thread:
                thread = threading.Thread(name=name, target=lambda: results.update(
                    {name: processor.process_job(job_id, owner=name).outcome}))
                thread.start()
                return thread

            background = run("redecode", redecode)
            try:
                assert holding.wait(10)
                with psycopg.connect(connection_info, autocommit=True) as monitor:
                    held = _battle_locks(monitor, holder[0], battle_ids)
                # The live battle log needs a battle the re-decode has not reached.
                run("live", live).join(10)
                assert results.get("live") == "processed"
            finally:
                release.set()
                background.join(20)
            assert results["redecode"] == "processed"
            assert held == army_ingestion.REDECODE_GROUP
            assert _upgraded(database) == len(battle_ids)
        finally:
            database.close()


def test_a_redecode_gives_way_to_a_live_job_holding_its_battle(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            battle_ids = _seed_redecode(connection_info, archive_server, database, processor)
            [redecode] = _queue_redecodes(connection_info, [battle_ids])
            results: list[str] = []
            thread = threading.Thread(target=lambda: results.append(
                processor.process_job(redecode, owner="redecode").outcome))
            with psycopg.connect(connection_info) as live, psycopg.connect(
                connection_info
            ) as other_live:
                # A live battle log is saving the sixth battle.
                live.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"army-redecode-battle:{battle_ids[5]}",),
                )
                thread.start()
                time.sleep(0.5)
                # The re-decode keeps none of the five battles before it while it
                # waits, so another live battle log saves the first at once.
                other_live.execute("SET lock_timeout = '200ms'")
                other_live.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"army-redecode-battle:{battle_ids[0]}",),
                )
                other_live.commit()
                live.commit()
            thread.join(20)
            assert results == ["processed"]
            assert _upgraded(database) == len(battle_ids)
            with database.pool.connection() as connection:
                assert text(connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE id = %s", (redecode,)
                ).fetchone()[0]) == "complete"
        finally:
            database.close()


def _live_job_beside_held_battles(
    connection_info: str, archive_server, processor, battle_ids: list[int]
) -> list[str]:
    """Run a live battle log repeating the newest battle while another
    connection holds every battle's lock; return its outcome if it finished
    within 10 seconds."""
    _, live = store_observation(
        connection_info,
        archive_server,
        occurrence_key="live",
        endpoint="battle_log",
        body=json.dumps({"items": [_live_row(
            True, OPPONENTS[-1], f"u{len(OPPONENTS)}x0",
            DAY + timedelta(minutes=len(OPPONENTS) - 1),
        )]}).encode(),
        observed_at=DAY + timedelta(hours=2),
        normalized_tag="#2PP",
    )
    results: list[str] = []
    thread = threading.Thread(target=lambda: results.append(
        processor.process_job(live, owner="live").outcome))
    with psycopg.connect(connection_info) as other_live:
        # Other live battle logs are saving these battles.
        other_live.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'army-redecode-battle:' || id, 0)) FROM unnest(%s::bigint[]) AS id",
            (battle_ids,),
        )
        thread.start()
        thread.join(10)
        finished = list(results)
        other_live.commit()
    thread.join(20)
    return finished


def _saved_armies(database: Database) -> list[tuple[str, str]]:
    with database.pool.connection() as connection:
        return [(text(row[0]), text(row[1])) for row in connection.execute(
            "SELECT status, catalog_version FROM battle_army_decodes ORDER BY id"
        ).fetchall()]


def test_a_live_battle_log_reuses_an_older_catalog_decode(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            battle_ids = _seed_redecode(
                connection_info, archive_server, database, processor, readable=True
            )
            before = _saved_armies(database)
            assert _live_job_beside_held_battles(
                connection_info, archive_server, processor, battle_ids
            ) == ["processed"]
            # Nothing was read again: the armies saved under the old list stand.
            assert _saved_armies(database) == before
            assert {row[1] for row in before} == {"unit-catalog-v2"}
        finally:
            database.close()


def test_a_live_battle_log_skips_a_busy_battle_it_would_save_again(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            battle_ids = _seed_redecode(connection_info, archive_server, database, processor)
            # The repeated battle's failed army now reads, so the live job
            # would save it again, but another job holds that battle.
            assert _live_job_beside_held_battles(
                connection_info, archive_server, processor, battle_ids
            ) == ["processed"]
            assert _upgraded(database) == 0
            assert len(_saved_armies(database)) == len(battle_ids)
        finally:
            database.close()
