from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database
from test_collector import _collector
from test_collector_uploads_postgres import _archive_instance
from test_domain_processing_postgres import PROFILE_FIXTURE
from test_worker_lifecycle import _worker_namespace

from clashlens import cli
from clashlens import spool as spool_module
from clashlens.archive import S3ArchiveReader, SpoolFirstReader
from clashlens.collector import _CLEANUP_LOOKUP_SIZE, Collector
from clashlens.collector_db import CollectorDatabase, ResponseHandoff
from clashlens.collector_uploads import (
    UNRESOLVED_WRITE_ATTEMPTS,
    archived_copy,
    claim_upload,
    complete_upload,
    fail_upload,
    release_expired_uploads,
    unresolved_write_detail,
)
from clashlens.spool import Spool, SpoolError

NOW = datetime.now(UTC).replace(microsecond=0)
TAGS = ("#2PP", "#2PY")


def _profile(tag: str) -> bytes:
    payload = json.loads(PROFILE_FIXTURE.read_bytes())
    payload["tag"] = tag
    return json.dumps(payload).encode()


def _save(
    connection_info: str, database: CollectorDatabase, spool: Spool, tag: str
) -> str:
    """Save one profile the way the collector does: spool file, then database."""
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "INSERT INTO players (normalized_tag, active, next_due_at)"
            " VALUES (%s, true, %s) RETURNING id",
            (tag, NOW),
        ).fetchone()[0]
    body = _profile(tag)
    digest = hashlib.sha256(body).hexdigest()
    with spool.reservation() as reservation:
        reservation.publish(body, digest)
    database.record_response(
        ResponseHandoff(
            occurrence_key=f"full-spool-{tag}",
            scope="player",
            identity_key=tag,
            endpoint="profile",
            player_id=player_id,
            normalized_tag=tag,
            request_started_at=NOW - timedelta(seconds=1),
            response_completed_at=NOW,
            http_status=200,
            response_hash=digest,
            content_fingerprint=digest,
            byte_size=len(body),
            spool_key=f"sha256/{digest[:2]}/{digest}",
            collector_version="python-collector-test",
            key_label="regular-a",
            evidence_headers={"content-type": "application/json"},
        )
    )
    return digest


def test_full_spool_drains_once_the_archive_returns(
    database_url: str, tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "spool"
    limits = {"max_body_bytes": 64 << 10, "max_objects": len(TAGS)}
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(root, **limits)
        digests = [_save(connection_info, database, spool, tag) for tag in TAGS]

        # Full: the collector may not save another response, and nothing can be
        # deleted while the archive has no copy and the worker has not read it.
        assert spool.readiness() == (False, "degraded_capacity")
        with pytest.raises(SpoolError, match="degraded_capacity"):
            spool.reserve()
        collector = SimpleNamespace(
            database=database, spool=spool, loop_passes={}, database_waits={}
        )
        assert Collector.cleanup_uploaded(collector) == (0, 0)

        # The archive comes back and takes a copy of both responses.
        for index in range(len(TAGS)):
            claim = claim_upload(database, owner="uploader")
            assert claim is not None
            complete_upload(
                database,
                claim,
                archive_reference=f"s3://evidence/{index}",
                archive_instance_id="fixture-instance",
            )
        assert Collector.cleanup_uploaded(collector) == (0, 0)

        # The real worker entry point still reads the saved responses.
        reader = SpoolFirstReader(
            SimpleNamespace(
                max_body_bytes=limits["max_body_bytes"],
                check_marker_health=lambda: "healthy",
                set_pool_acquire_observer=lambda _observer: None,
            ),
            spool_root=str(root),
            validate_database=False,
            **{key: value for key, value in limits.items() if key != "max_body_bytes"},
        )
        monkeypatch.setattr(cli, "_archive", lambda _arguments, **_kwargs: reader)
        assert (
            cli._run_worker(
                _worker_namespace(database_url=connection_info, max_jobs=10)
            )
            == 0
        )
        results = json.loads(capsys.readouterr().out.splitlines()[-1])["results"]
        assert len(results) == len(TAGS)
        assert {result["outcome"] for result in results} <= {
            "processed",
            "processed_with_gaps",
        }

        # Cleanup can now free the space and collection can resume.
        assert Collector.cleanup_uploaded(collector) == (len(TAGS), len(TAGS))
        assert all(spool.verify(digest) is None for digest in digests)
        assert spool.readiness() == (True, "ready")
        with spool.reserve():
            pass


def test_failed_spool_read_waits_instead_of_failing_the_job(
    database_url: str, tmp_path, monkeypatch, capsys
) -> None:
    root = tmp_path / "spool"
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        _save(connection_info, database, Spool(root, max_body_bytes=64 << 10), TAGS[0])
        reader = SpoolFirstReader(
            SimpleNamespace(
                max_body_bytes=64 << 10,
                check_marker_health=lambda: "healthy",
                set_pool_acquire_observer=lambda _observer: None,
            ),
            spool_root=str(root),
            validate_database=False,
        )

        def unreadable(_digest: str, expected_size: int | None = None) -> bytes:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(reader.spool, "verify", unreadable)
        monkeypatch.setattr(cli, "_archive", lambda _arguments, **_kwargs: reader)
        assert (
            cli._run_worker(_worker_namespace(database_url=connection_info, max_jobs=1))
            == 0
        )
        capsys.readouterr()
        with psycopg.connect(connection_info) as connection:
            job = connection.execute(
                "SELECT state, failure_category, attempt_count"
                " FROM python_processing_jobs_worker"
            ).fetchone()
        # The response is still on disk; the job waits to be read again rather
        # than being failed as missing evidence or spending an attempt.
        assert job[:2] == ("waiting_dependency", "spool_io_failed")


def test_cleanup_deletes_the_same_responses_with_few_lookups(
    database_url: str, tmp_path, monkeypatch
) -> None:
    # Each lookup once read the whole upload table: on production 2026-10-04
    # that was 42% of all database reads, 16 responses at a time.
    # Flushing to disk changes no outcome here and is most of the time taken.
    monkeypatch.setattr(spool_module.os, "fsync", lambda _fd: None)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(tmp_path / "spool", max_body_bytes=64 << 10)
        digests = {
            _save(connection_info, database, spool, f"#{index:04d}")
            for index in range(_CLEANUP_LOOKUP_SIZE + 4)
        }
        while (claim := claim_upload(database, owner="uploader")) is not None:
            complete_upload(
                database,
                claim,
                archive_reference=f"s3://evidence/{claim.response_hash}",
                archive_instance_id="fixture-instance",
            )
        with psycopg.connect(connection_info) as connection:
            # The worker has read every response and its finished jobs are gone.
            connection.execute("DELETE FROM python_processing_jobs")
        assert set(database.deletable_hashes(limit=2000)) == digests

        lookups = []
        deletable_hashes = database.deletable_hashes

        def counted(*, limit: int) -> list[str]:
            lookups.append(limit)
            return deletable_hashes(limit=limit)

        database.deletable_hashes = counted  # type: ignore[method-assign]
        collector = _collector(spool, database, None)  # type: ignore[arg-type]

        async def clean() -> None:
            stop = asyncio.Event()
            task = asyncio.create_task(collector._upload_loop(stop, 0.01))
            for _attempt in range(1000):
                if all(spool.verify(digest) is None for digest in digests):
                    break
                await asyncio.sleep(0.01)
            # A short turn means little is left, so the next lookup waits.
            await asyncio.sleep(2)
            stop.set()
            await asyncio.wait_for(task, timeout=5)

        asyncio.run(clean())
        assert all(spool.verify(digest) is None for digest in digests)
        # A full lookup, a short one, then one more as the collector stops.
        assert len(lookups) == 3
        assert deletable_hashes(limit=2000) == []


def test_cleanup_lookup_reads_kept_uploads_from_the_index_in_order(
    database_url: str,
) -> None:
    # Production kept 1.67 million upload rows on 2026-10-04, almost all
    # already deleted locally; every lookup read and sorted all of them.
    with domain_database(database_url) as connection_info:
        _archive_instance(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO collector_response_uploads (
                    response_hash, spool_key, byte_size, state, archive_reference,
                    archive_instance_id, completed_at, local_deleted_at,
                    latest_sighting_at
                )
                SELECT encode(sha256(i::text::bytea), 'hex'), 'sha256/fixture', 1,
                       'complete', 's3://evidence/' || i, 'fixture-instance', now(),
                       CASE WHEN i % 500 = 0 THEN NULL ELSE now() END,
                       now() - i * interval '1 second'
                FROM generate_series(1, 100000) AS i
                """
            )
            connection.execute("ANALYZE collector_response_uploads")
        database = CollectorDatabase(connection_info)
        plans = []

        @contextmanager
        def explained():
            with database.pool.connection() as connection:
                query = SimpleNamespace(
                    execute=lambda sql, params: (
                        plans.append(
                            connection.execute(
                                f"EXPLAIN (FORMAT JSON) {sql}", params
                            ).fetchone()[0][0]["Plan"]
                        ),
                        connection.execute(sql, params),
                    )[1]
                )
                yield query

        database._connection = explained  # type: ignore[method-assign]
        try:
            found = database.deletable_hashes(limit=_CLEANUP_LOOKUP_SIZE)
        finally:
            database.close()

    def nodes(plan: dict) -> list[dict]:
        return [
            plan,
            *(node for child in plan.get("Plans", []) for node in nodes(child)),
        ]

    # The 200 kept rows come back oldest sighting first.
    assert found == [
        hashlib.sha256(str(i).encode()).hexdigest() for i in range(100000, 0, -500)
    ]
    uploads = [
        node
        for node in nodes(plans[0])
        if node.get("Relation Name") == "collector_response_uploads"
    ]
    assert [node.get("Index Name") for node in uploads] == [
        "collector_response_uploads_cleanup_order"
    ]
    assert all(node["Node Type"] != "Sort" for node in nodes(plans[0]))


def _lost_copy_reader(root, endpoint: str) -> SpoolFirstReader:
    return SpoolFirstReader(
        S3ArchiveReader(
            endpoint=endpoint,
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
            max_retries=0,
        ),
        spool_root=str(root),
        max_body_bytes=64 << 10,
        validate_database=False,
    )


@pytest.mark.parametrize(
    ("history", "archived_at", "outcome"),
    [
        # The upload finished and was recorded before the saved copy was lost.
        ("recorded", "original", ("complete", None)),
        # The database was restored to before the upload finished: it still
        # lists the upload as pending, though the bytes reached the archive.
        ("restored", "original", ("complete", None)),
        # The bytes never reached the archive: real missing proof.
        ("restored", None, ("failed", "spool_missing")),
        # The first location was retired, and the bytes were uploaded again
        # under a new one this database knows.
        ("retired", "generation", ("complete", None)),
        # A retired location is never read back, even if it still exists.
        ("retired", "original", ("failed", "spool_missing")),
        # An upload still in flight may yet deliver the bytes. Like an archive
        # outage, this waits without spending an attempt; once that upload
        # gives up on the missing copy, the next try fails as missing proof.
        ("uploading", None, ("waiting_dependency", "archive_missing")),
        # An upload whose write could not be confirmed may still land, even
        # after a later attempt found no copy.
        ("write_unresolved", None, ("waiting_dependency", "archive_missing")),
        ("missing_after_unresolved", None, ("waiting_dependency", "archive_missing")),
        # Only for so many attempts after that write, however many attempts
        # failed before it.
        ("unresolved_exhausted", None, ("failed", "spool_missing")),
        ("written_after_many_failures", None, ("waiting_dependency", "archive_missing")),
        # The archive cannot be reached: try again later.
        ("unreachable", "original", ("waiting_dependency", "archive_unavailable")),
    ],
)
def test_a_lost_saved_copy_is_read_back_from_the_archive(
    database_url: str,
    tmp_path,
    monkeypatch,
    capsys,
    archive_server,
    history: str,
    archived_at: str | None,
    outcome: tuple[str, str | None],
) -> None:
    root = tmp_path / "spool"
    generation = "b" * 32
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(root, max_body_bytes=64 << 10)
        digest = _save(connection_info, database, spool, TAGS[0])
        body = spool.verify(digest)
        original = f"sha256/{digest[:2]}/{digest}"
        if history == "recorded":
            claim = claim_upload(database, owner="uploader")
            assert claim is not None
            complete_upload(
                database,
                claim,
                archive_reference=f"s3://evidence/{original}",
                archive_instance_id="fixture-instance",
            )
        if history == "uploading":
            assert claim_upload(database, owner="uploader") is not None
        if history in {
            "write_unresolved",
            "missing_after_unresolved",
            "unresolved_exhausted",
            "written_after_many_failures",
        }:
            claim = claim_upload(database, owner="uploader")
            assert claim is not None
            fail_upload(
                database,
                claim,
                category="archive_unavailable",
                detail="archive write outcome could not be verified",
            )
        if history == "missing_after_unresolved":
            claim = claim_upload(
                database, owner="uploader", now=datetime.now(UTC) + timedelta(minutes=1)
            )
            assert claim is not None
            fail_upload(database, claim, category="archive_missing")
        if history == "unresolved_exhausted":
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE collector_response_uploads SET attempt_count = %s,"
                    " last_error_category = 'archive_missing',"
                    " last_error_detail = %s",
                    (
                        1 + UNRESOLVED_WRITE_ATTEMPTS,
                        unresolved_write_detail(1, "no such object"),
                    ),
                )
        if history == "written_after_many_failures":
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE collector_response_uploads SET attempt_count = %s",
                    (10 * UNRESOLVED_WRITE_ATTEMPTS,),
                )
        if history == "retired":
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "INSERT INTO archive_catalogue (response_hash, archive_reference,"
                    " byte_size, archive_instance_id, availability)"
                    " VALUES (%s, %s, %s, 'fixture-instance', 'expired')",
                    (digest, f"s3://evidence/{original}", len(body)),
                )
                connection.execute(
                    "UPDATE collector_response_uploads SET upload_generation = %s"
                    " WHERE response_hash = %s",
                    (generation, digest),
                )
        objects = archive_server[3].objects
        objects.clear()
        if archived_at == "original":
            objects[original] = body
        elif archived_at == "generation":
            objects[f"{original}/generation/{generation}"] = body
        spool.delete_if_unreferenced(digest)
        assert spool.verify(digest) is None
        endpoint = "127.0.0.1:9" if history == "unreachable" else archive_server[0]
        reader = _lost_copy_reader(root, endpoint)
        monkeypatch.setattr(cli, "_archive", lambda _arguments, **_kwargs: reader)

        assert (
            cli._run_worker(_worker_namespace(database_url=connection_info, max_jobs=1))
            == 0
        )
        capsys.readouterr()
        with psycopg.connect(connection_info) as connection:
            job = connection.execute(
                "SELECT state, failure_category FROM python_processing_jobs_worker"
            ).fetchone()

        assert job == outcome
        # Bytes read back are saved locally again; nothing else is.
        assert (spool.verify(digest) == body) is (outcome[0] == "complete")
        if history == "retired" and archived_at == "original":
            assert archive_server[3].get_count == 0


def test_a_write_that_may_yet_land_is_checked_again_before_missing_proof(
    database_url: str, tmp_path
) -> None:
    later = datetime.now(UTC) + timedelta(minutes=1)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(tmp_path / "spool", max_body_bytes=64 << 10)
        _save(connection_info, database, spool, TAGS[0])
        at = later
        claim = claim_upload(database, owner="uploader", now=at)
        # Thirty attempts that only found the archive marker unreadable, as
        # the uploads process records them.
        while claim is not None and claim.attempt_count <= UNRESOLVED_WRITE_ATTEMPTS:
            detail = "archive marker could not be checked"
            if claim.unresolved_write is not None:
                detail = unresolved_write_detail(claim.unresolved_write, detail)
            fail_upload(database, claim, category="archive_unavailable", detail=detail)
            at += timedelta(minutes=2)
            claim = claim_upload(database, owner="uploader", now=at)
        # The next attempt writes, and the write times out.
        assert claim is not None
        write = claim.attempt_count
        fail_upload(
            database,
            claim,
            category="archive_unavailable",
            detail="archive write outcome could not be verified",
        )
        at += timedelta(minutes=2)
        claim = claim_upload(database, owner="uploader", now=at)
        # Attempts finding no archived copy do not settle it until that write
        # has had its full allowance of later attempts.
        while claim is not None and claim.attempt_count <= write + UNRESOLVED_WRITE_ATTEMPTS:
            assert claim.unresolved_write == write
            fail_upload(
                database,
                claim,
                category="archive_missing",
                detail=unresolved_write_detail(write, "no such object"),
            )
            at += timedelta(minutes=2)
            claim = claim_upload(database, owner="uploader", now=at)
        assert claim is not None and claim.unresolved_write is None
        # An attempt whose lease ran out may have written too.
        at += timedelta(minutes=2)
        assert release_expired_uploads(database, now=at) == 1
        expired = claim.attempt_count
        claim = claim_upload(database, owner="uploader", now=at)
        assert claim is not None and claim.unresolved_write == expired


class _FinishesAfterFirstRead:
    """A database whose upload finishes between archived_copy's two reads."""

    def __init__(self, database: CollectorDatabase, finish) -> None:
        self._database = database
        self._finish = finish
        self.pool = self

    @contextmanager
    def connection(self):
        finish = self._finish

        class Connection:
            def __init__(self, connection) -> None:
                self._connection = connection

            def execute(self, *args, **kwargs):
                nonlocal finish
                result = self._connection.execute(*args, **kwargs)
                if finish is not None:
                    finish, done = None, finish
                    done()
                return result

        with self._database.pool.connection() as connection:
            yield Connection(connection)


def test_an_upload_finishing_during_the_lookup_still_counts_as_archived(
    database_url: str, tmp_path
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        spool = Spool(tmp_path / "spool", max_body_bytes=64 << 10)
        digest = _save(connection_info, database, spool, TAGS[0])
        claim = claim_upload(database, owner="uploader")
        assert claim is not None
        reference = f"s3://evidence/sha256/{digest[:2]}/{digest}"

        copy = archived_copy(
            _FinishesAfterFirstRead(
                database,
                lambda: complete_upload(
                    database,
                    claim,
                    archive_reference=reference,
                    archive_instance_id="fixture-instance",
                ),
            ),
            digest,
            bucket="evidence",
        )

    assert copy is not None
    assert (copy.reference, copy.recorded) == (reference, True)
