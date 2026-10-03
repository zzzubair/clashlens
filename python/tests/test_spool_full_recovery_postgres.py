from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database
from test_collector_uploads_postgres import _archive_instance
from test_domain_processing_postgres import PROFILE_FIXTURE
from test_worker_lifecycle import _worker_namespace

from clashlens import cli
from clashlens.archive import SpoolFirstReader
from clashlens.collector import Collector
from clashlens.collector_db import CollectorDatabase, ResponseHandoff
from clashlens.collector_uploads import claim_upload, complete_upload
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
        collector = SimpleNamespace(database=database, spool=spool)
        assert Collector.cleanup_uploaded(collector) == 0

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
        assert Collector.cleanup_uploaded(collector) == 0

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
        assert Collector.cleanup_uploaded(collector) == len(TAGS)
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
