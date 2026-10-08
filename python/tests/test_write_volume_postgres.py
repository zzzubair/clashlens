"""Database change-log (WAL) bytes written per repeat sighting and per job.

The backup change log grows with every row the database rewrites. Measured on
8 Oct 2026, repeat sightings moving retention deadlines and job lease renewals
were two of its largest avoidable sources. Run with ``-s`` to print bytes per
operation; ``cold`` starts each operation just after a checkpoint, so its first
change to each page copies the whole page, as most production writes do.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from test_collector_uploads_postgres import (
    NOW,
    _archive_instance,
    _handoff,
    _hash,
    _player,
    upload_database,
)

from clashlens.collector_db import CollectorDatabase
from clashlens.collector_uploads import claim_upload, complete_upload
from clashlens.db import RESPONSE_WORK_TYPES, Database, LeaseLost
from clashlens.job_outcomes import complete_terminal

DAY = timedelta(days=1)


def _wal(connection: psycopg.Connection, cold: bool) -> int:
    if cold:
        connection.execute("CHECKPOINT")
    return int(connection.execute("SELECT pg_current_wal_insert_lsn() - '0/0'::pg_lsn").fetchone()[0])


def _versions(connection: psycopg.Connection, response_hash: str) -> tuple:
    """Row versions of the body's upload and archive rows: a rewrite changes them."""
    return connection.execute(
        """SELECT upload.xmin::text, catalogue.xmin::text, upload.latest_sighting_at,
                  catalogue.retire_after
           FROM collector_response_uploads AS upload
           JOIN archive_catalogue AS catalogue USING (response_hash)
           WHERE response_hash = %s""",
        (response_hash,),
    ).fetchone()


def _archived_body(connection_info: str, name: str):
    database = CollectorDatabase(connection_info)
    first = _handoff(
        occurrence_key=f"{name}-0", response_hash=_hash(name),
        player_id=_player(connection_info), completed_at=NOW,
    )
    assert database.record_response(first).changed
    claim = claim_upload(database, owner="uploader", now=NOW)
    assert claim is not None
    _archive_instance(connection_info)
    complete_upload(
        database, claim, archive_reference=f"s3://evidence/{name}",
        archive_instance_id="fixture-instance", now=NOW,
    )
    return database, first


def _sighting(database: CollectorDatabase, first, index: int, at) -> None:
    assert database.record_unchanged_response(replace(
        first, occurrence_key=f"{first.occurrence_key}-{index}",
        request_started_at=at - timedelta(seconds=1), response_completed_at=at,
    ))


def test_same_day_repeat_sightings_rewrite_no_retention_rows(database_url: str) -> None:
    with upload_database(database_url) as connection_info, psycopg.connect(
        connection_info, autocommit=True
    ) as connection:
        database, first = _archived_body(connection_info, "same-day")
        try:
            settled = _versions(connection, first.response_hash)
            # Kept to the end of the sighting's UTC day plus 86 days, never less.
            assert settled[3] == NOW.replace(hour=0) + 87 * DAY
            for index in range(1, 12):
                _sighting(database, first, index, NOW + index * timedelta(minutes=15))
            assert _versions(connection, first.response_hash) == settled
            # The next UTC day's first sighting moves both, by exactly one day.
            next_day = NOW.replace(hour=0) + DAY + timedelta(minutes=5)
            _sighting(database, first, 12, next_day)
            moved = _versions(connection, first.response_hash)
            assert moved[0] != settled[0] and moved[1] != settled[1]
            assert (moved[2], moved[3]) == (next_day, settled[3] + DAY)
            # A late sighting from an earlier day never shortens either.
            _sighting(database, first, 13, NOW + timedelta(hours=1))
            assert _versions(connection, first.response_hash) == moved
        finally:
            database.close()


def test_renewing_a_fresh_lease_checks_ownership_without_rewriting_the_job(
    database_url: str,
) -> None:
    with upload_database(database_url) as connection_info, psycopg.connect(
        connection_info, autocommit=True
    ) as connection:
        collector, _first = _archived_body(connection_info, "fresh-lease")
        jobs = Database(connection_info)
        try:
            claim = jobs.claim_jobs(owner="lane", lease_seconds=60, work_types=RESPONSE_WORK_TYPES)[0]
            row = "SELECT xmin::text, lease_expires_at FROM python_processing_jobs WHERE id = %s"
            claimed = connection.execute(row, (claim.job_id,)).fetchone()
            jobs.renew_claim(claim, lease_seconds=60)
            assert connection.execute(row, (claim.job_id,)).fetchone() == claimed
            # Under half the lease left: the renewal writes a full new lease.
            connection.execute(
                "UPDATE python_processing_jobs SET lease_expires_at = clock_timestamp() + interval '20 seconds' WHERE id = %s",
                (claim.job_id,),
            )
            jobs.renew_claim(claim, lease_seconds=60)
            remaining = connection.execute(
                "SELECT lease_expires_at - clock_timestamp() FROM python_processing_jobs WHERE id = %s",
                (claim.job_id,),
            ).fetchone()[0]
            assert timedelta(seconds=55) < remaining <= timedelta(seconds=60)
            # A longer requested lease is always written.
            jobs.renew_claim(claim, lease_seconds=300)
            assert connection.execute(
                "SELECT lease_expires_at - clock_timestamp() > interval '200 seconds' FROM python_processing_jobs WHERE id = %s",
                (claim.job_id,),
            ).fetchone()[0]
            # A worker that no longer owns the job is still rejected.
            connection.execute(
                "UPDATE python_processing_jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
                (claim.job_id,),
            )
            with pytest.raises(LeaseLost):
                jobs.renew_claim(claim, lease_seconds=60)
        finally:
            jobs.close()
            collector.close()


@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_write_volume_per_sighting_and_job(database_url: str, cold: bool) -> None:
    sightings, job_count = 96, 40
    with upload_database(database_url) as connection_info, psycopg.connect(
        connection_info, autocommit=True
    ) as connection:
        database, first = _archived_body(connection_info, f"volume-{cold}")
        jobs = Database(connection_info)
        try:
            # One unchanged body polled every 15 minutes for a day: production
            # repeat sightings more than 10 minutes apart rewrote both rows.
            sighting_bytes = 0
            for index in range(1, sightings + 1):
                before = _wal(connection, cold)
                _sighting(database, first, index, NOW + index * timedelta(minutes=15))
                sighting_bytes += _wal(connection, False) - before
            for index in range(1, job_count):
                assert database.record_response(_handoff(
                    occurrence_key=f"volume-job-{index}", response_hash=_hash(f"volume-job-{cold}-{index}"),
                    player_id=first.player_id, completed_at=NOW + DAY + index * timedelta(seconds=1),
                )).changed
            steps = {"claim": 0, "renew": 0, "complete": 0}
            for _ in range(job_count):
                before = _wal(connection, cold)
                claim = jobs.claim_jobs(owner="volume", lease_seconds=60, work_types=RESPONSE_WORK_TYPES)[0]
                steps["claim"] += _wal(connection, False) - before
                # The local-spool worker renews once, right after reading the body.
                before = _wal(connection, cold)
                jobs.renew_claim(claim, lease_seconds=60)
                steps["renew"] += _wal(connection, False) - before
                before = _wal(connection, cold)
                complete_terminal(jobs, claim, outcome="processed")
                steps["complete"] += _wal(connection, False) - before
            assert connection.execute(
                "SELECT count(*) FROM python_processing_jobs WHERE status = 'complete'"
            ).fetchone()[0] == job_count
            mode = "cold" if cold else "warm"
            print(f"\n{mode}: {sighting_bytes / sightings:.0f} WAL bytes per repeat sighting"
                  f" ({sightings} sightings, 15 minutes apart)")
            for step, total in steps.items():
                print(f"{mode}: {total / job_count:.0f} WAL bytes per job {step} ({job_count} jobs)")
        finally:
            jobs.close()
            database.close()
