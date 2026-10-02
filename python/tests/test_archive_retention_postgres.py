"""Raw-response expiry: 86 days after the latest sighting, then a recovery hold, then delete."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from clashlens.archive_retention import retire_archive_objects
from clashlens.spool import Spool

FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
MIGRATIONS = Path(__file__).parents[2] / "deploy" / "migrations"
OPTIONS = {"bucket": "evidence", "instance_id": "fixture-instance"}


class DeleteClient:
    def __init__(self, keys=(), failing=()):
        self.keys = set(keys)
        self.failing = set(failing)
        self.calls = []

    def remove_object(self, bucket, key):
        assert bucket == "evidence"
        self.calls.append(key)
        if key in self.failing:
            self.failing.discard(key)
            raise TimeoutError("unknown delete result")
        self.keys.discard(key)


def _seed(connection, count: int, *, deadline: str = "-1 minute") -> list[str]:
    connection.execute(
        """
        INSERT INTO archive_instances (
            instance_id, endpoint, region, bucket, marker_key,
            marker_hash, marker_payload_version
        ) VALUES ('fixture-instance', 'archive.test:443', 'us-east-1',
                  'evidence', 'clashlens/archive-instance.json', repeat('f', 64), 'v1')
        ON CONFLICT (instance_id) DO NOTHING
        """
    )
    keys = []
    for _ in range(count):
        digest = hashlib.sha256(uuid4().bytes).hexdigest()
        key = f"sha256/{digest[:2]}/{digest}"
        connection.execute(
            """
            INSERT INTO archive_catalogue (
                response_hash, archive_reference, byte_size, archive_instance_id, retire_after
            ) VALUES (%s, %s, 1000, 'fixture-instance', clock_timestamp() + %s::interval)
            """,
            (digest, "s3://evidence/" + key, deadline),
        )
        keys.append(key)
    return keys


def _availability(connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT availability FROM archive_catalogue ORDER BY availability"
        ).fetchall()
    ]


def _hold_elapsed(connection, age: str) -> None:
    connection.execute(
        "UPDATE archive_catalogue SET retiring_since = clock_timestamp() - %s::interval WHERE availability = 'retiring'",
        (age,),
    )


def test_response_is_due_86_days_after_its_latest_sighting(database_url, tmp_path):
    from domain_test_support import domain_database

    with domain_database(database_url) as dsn, psycopg.connect(dsn, autocommit=True) as connection:
        _seed(connection, 1)
        spool = Spool(tmp_path / "spool", max_body_bytes=1 << 20)
        client = DeleteClient()
        try:
            for last_seen, marked in (
                ("85 days", 0),
                ("85 days 23 hours 59 minutes", 0),
                ("86 days 1 second", 1),
            ):
                connection.execute(
                    "UPDATE archive_catalogue SET retire_after = clashlens_season_retire_after(clock_timestamp() - %s::interval)",
                    (last_seen,),
                )
                report = retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)
                assert report["marked_objects"] == marked, last_seen
        finally:
            spool.close()


def test_existing_deadlines_are_recalculated_from_retained_sightings(database_url):
    schema = f"retention_upgrade_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    dsn = make_conninfo(database_url, options=f"-c search_path={schema}")
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name.startswith("0046_"):
                    break
                connection.execute(path.read_text(encoding="utf-8"))
            keys = _seed(connection, 3)
            now = connection.execute("SELECT clock_timestamp()").fetchone()[0].replace(microsecond=0)
            # Old code marked responses 56 days after their season ended, so a
            # 60-day-old one may already be marked; another was marked long ago.
            sightings = [now - timedelta(days=60), now - timedelta(days=60), now - timedelta(days=200)]
            for key, sighting, availability in zip(keys, sightings, ("verified", "retiring", "retiring")):
                digest = key.rsplit("/", 1)[1]
                connection.execute(
                    "INSERT INTO collector_response_uploads (response_hash, spool_key, byte_size, latest_sighting_at) VALUES (%s, %s, 1000, %s)",
                    (digest, key, sighting),
                )
                connection.execute(
                    """
                    UPDATE archive_catalogue
                    SET retire_after = clashlens_season_retire_after(%s), availability = %s
                    WHERE response_hash = %s
                    """,
                    (sighting, availability, digest),
                )
            connection.execute((MIGRATIONS / "0046_raw_recovery_hold.sql").read_text(encoding="utf-8"))
            rows = [
                connection.execute(
                    "SELECT retire_after, retiring_since FROM archive_catalogue WHERE archive_reference = %s",
                    ("s3://evidence/" + key,),
                ).fetchone()
                for key in keys
            ]
            assert [retire_after - sighting for (retire_after, _), sighting in zip(rows, sightings)] == [
                timedelta(days=86)
            ] * 3
            assert rows[0][1] is None
            # A marked response still waits its full 86 days before the hold starts.
            assert rows[1][1] == rows[1][0]
            assert rows[2][1] >= now
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_bytes_survive_the_recovery_hold_and_reruns_are_safe(database_url, tmp_path):
    from domain_test_support import domain_database

    with domain_database(database_url) as dsn, psycopg.connect(dsn, autocommit=True) as connection:
        keys = _seed(connection, 3)
        _seed(connection, 1, deadline="1 day")
        spool = Spool(tmp_path / "spool", max_body_bytes=1 << 20)
        client = DeleteClient(keys, failing=keys[:1])
        try:
            preview = retire_archive_objects(connection, spool, client, **OPTIONS)
            assert (preview["marked_objects"], preview["marked_bytes"], preview["deleted_objects"]) == (3, 3000, 0)
            assert _availability(connection) == ["verified"] * 4

            assert retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)["marked_objects"] == 3
            assert _availability(connection) == ["retiring"] * 3 + ["verified"]
            # A restore to any point in the last seven days, plus two days to
            # perform it, must still find the bytes.
            _hold_elapsed(connection, "8 days 23 hours 59 minutes")
            report = retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)
            assert report["deleted_objects"] == report["marked_objects"] == 0
            assert client.calls == [] and client.keys == set(keys)

            _hold_elapsed(connection, "9 days 1 minute")
            preview = retire_archive_objects(connection, spool, client, **OPTIONS)
            assert (preview["deleted_objects"], preview["deleted_bytes"]) == (3, 3000)
            assert client.calls == []

            # One unknown DELETE outcome does not stop the batch; it stays
            # marked, the run reports a failure, and the next run retries it.
            report = retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)
            assert (report["deleted_objects"], report["deleted_bytes"], report["failed_objects"]) == (2, 2000, 1)
            assert _availability(connection) == ["expired", "expired", "retiring", "verified"]
            report = retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)
            assert (report["deleted_objects"], report["failed_objects"]) == (1, 0)
            assert client.keys == set()
            report = retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)
            assert report["deleted_objects"] == report["marked_objects"] == report["failed_objects"] == 0
            assert len(client.calls) == 4
            assert _availability(connection) == ["expired"] * 3 + ["verified"]
        finally:
            spool.close()


def test_retirement_fences_replay_and_keeps_active_work(
    database_url: str, archive_server, tmp_path: Path
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from time import monotonic, sleep

    from domain_test_support import domain_database, store_observation
    from test_domain_processing_postgres import _processor
    from test_parsed_content_dedup_postgres import _replay_job

    with domain_database(database_url, include_coordinator=True) as dsn:
        database, processor = _processor(dsn, archive_server)
        spool = Spool(tmp_path / "retention-spool", max_body_bytes=1 << 20)
        client = DeleteClient()
        try:
            observation, job = store_observation(
                dsn, archive_server, occurrence_key="expiry-first", endpoint="profile",
                body=FIXTURE.read_bytes(), normalized_tag="#2PP", observed_at=datetime.now(UTC),
            )
            assert processor.process_job(job, owner="expiry").outcome == "processed"
            with psycopg.connect(dsn, autocommit=True) as connection:
                reference = connection.execute(
                    "SELECT archive_reference FROM collector_observations WHERE id = %s",
                    (observation,),
                ).fetchone()[0]
                reference = reference.decode() if isinstance(reference, bytes) else reference
                client.keys.add(reference.removeprefix("s3://evidence/"))

                def racing_replay():
                    with psycopg.connect(dsn, autocommit=True, application_name="expiry-racing-replay") as replay:
                        replay.execute("SET statement_timeout = '5s'")
                        _replay_job(replay, observation, "supercell-source-parser-v2")

                # Make a replay wait behind retirement. Its availability check
                # must run after the observation lock, not before the FK waits.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    with connection.transaction():
                        connection.execute(
                            "SELECT id FROM collector_observations WHERE id = %s FOR UPDATE",
                            (observation,),
                        )
                        future = executor.submit(racing_replay)
                        deadline = monotonic() + 3
                        while not connection.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE application_name = 'expiry-racing-replay' AND wait_event_type = 'Lock')"
                        ).fetchone()[0]:
                            assert monotonic() < deadline, "replay did not reach the observation fence"
                            connection.execute("SELECT pg_stat_clear_snapshot()")
                            sleep(0.01)
                        connection.execute("UPDATE archive_catalogue SET availability = 'retiring'")
                    with pytest.raises(psycopg.errors.RaiseException, match="expired"):
                        future.result(timeout=5)
                connection.execute("UPDATE archive_catalogue SET availability = 'verified'")
                connection.execute("UPDATE archive_catalogue SET retire_after = clock_timestamp() - interval '1 minute'")
                # A duplicate sighting extends retention without another raw object.
                _, pending = store_observation(
                    dsn, archive_server, occurrence_key="expiry-duplicate", endpoint="profile",
                    body=FIXTURE.read_bytes(), normalized_tag="#2PP", observed_at=datetime.now(UTC),
                )
                assert retire_archive_objects(connection, spool, client, **OPTIONS)["marked_objects"] == 0
                connection.execute("UPDATE archive_catalogue SET retire_after = clock_timestamp() - interval '1 minute'")
                # Unfinished processing keeps the response usable.
                report = retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)
                assert report["marked_objects"] == 0
                assert processor.process_job(pending, owner="expiry-duplicate").outcome == "processed"
                assert retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)["marked_objects"] == 1
                with pytest.raises(psycopg.errors.RaiseException, match="expired"):
                    _replay_job(connection, observation, "supercell-source-parser-v2")
                # Recollection gets a separate location. Deleting the old key can
                # never affect this new generation.
                renewed = reference + "/generation/" + "a" * 32
                client.keys.add(renewed.removeprefix("s3://evidence/"))
                connection.execute(
                    """
                    INSERT INTO archive_catalogue(response_hash, archive_reference, byte_size, archive_instance_id)
                    SELECT response_hash, %s, byte_size, archive_instance_id
                    FROM archive_catalogue WHERE archive_reference = %s
                    """, (renewed, reference),
                )
                _hold_elapsed(connection, "10 days")
                assert retire_archive_objects(connection, spool, client, apply=True, **OPTIONS)["deleted_objects"] == 1
                assert client.keys == {renewed.removeprefix("s3://evidence/")}
                assert connection.execute("SELECT count(*) FROM player_profile_versions").fetchone()[0] == 1
        finally:
            spool.close()
            database.close()
