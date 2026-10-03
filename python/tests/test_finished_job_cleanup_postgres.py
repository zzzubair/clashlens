"""Scheduled cleanup removes only finished jobs 48 hours old, as a role that can do nothing else."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_domain_processing_postgres import _processor

from clashlens.collector_db import CollectorDatabase, ResponseHandoff, ResponseResult
from clashlens.history import prune_completed_history
from clashlens.profile import PROFILE_PARSER_VERSION
from clashlens.response_fields import content_fingerprint

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
OBSERVED = datetime(2026, 9, 1, 6, tzinfo=UTC)


def _as_cleanup_role(dsn: str) -> str:
    options = conninfo_to_dict(dsn).get("options", "")
    return make_conninfo(dsn, options=f"{options} -c role=clashlens_history_retention".strip())


def _jobs(info: str, count: int, archive_server) -> list[int]:
    return [
        store_observation(
            info,
            archive_server,
            occurrence_key=f"cleanup-{index}",
            endpoint="profile",
            body=PROFILE_FIXTURE.read_bytes(),
            observed_at=OBSERVED + timedelta(minutes=index),
            normalized_tag="#2PP",
        )[1]
        for index in range(count)
    ]


def test_only_finished_jobs_older_than_48_hours_are_removed(database_url, archive_server):
    with domain_database(database_url) as info:
        old, recent, failed, pending, leased, anchor = _jobs(info, 6, archive_server)
        database, processor = _processor(info, archive_server)
        try:
            for job in (old, recent, anchor):
                assert processor.process_job(job, owner="cleanup-test").outcome == "processed"
        finally:
            database.close()
        with psycopg.connect(info) as connection:
            connection.execute(
                """
                UPDATE python_processing_jobs SET status = CASE id
                    WHEN %(failed)s THEN 'failed' WHEN %(leased)s THEN 'leased' ELSE status END,
                    lease_owner = CASE WHEN id = %(leased)s THEN 'cleanup-test' END,
                    lease_token = CASE WHEN id = %(leased)s THEN gen_random_uuid() END,
                    lease_expires_at = CASE WHEN id = %(leased)s
                        THEN clock_timestamp() + interval '1 hour' END,
                    updated_at = clock_timestamp() - CASE id
                        WHEN %(recent)s THEN interval '47 hours' ELSE interval '49 hours' END
                """,
                {"failed": failed, "leased": leased, "recent": recent},
            )
            # Legacy publication anchors stay for the boundary history they explain.
            connection.execute(
                """
                INSERT INTO boundary_publication_legacy_job_migrations (
                    job_id, work_type, previous_state, reason
                ) VALUES (%s, 'process_observation', 'complete', 'cleanup test')
                """,
                (anchor,),
            )
            outcomes = connection.execute(
                "SELECT count(*) FROM observation_processing_outcomes"
            ).fetchone()[0]
            assert connection.execute(
                "SELECT count(*) FROM python_processing_attempts WHERE job_id = %s", (old,)
            ).fetchone()[0] == 1

        with psycopg.connect(_as_cleanup_role(info)) as connection:
            preview = prune_completed_history(connection, jobs_only=True)
            assert preview == {
                "apply": False,
                "retention_hours": 48,
                "eligible_python_processing_jobs": 1,
                "deleted_python_processing_jobs": 0,
            }
            applied = prune_completed_history(connection, jobs_only=True, apply=True)
            assert applied["deleted_python_processing_jobs"] == 1
            assert prune_completed_history(connection, jobs_only=True, apply=True)[
                "eligible_python_processing_jobs"
            ] == 0

        with psycopg.connect(info) as connection:
            kept = {
                row[0]
                for row in connection.execute("SELECT id FROM python_processing_jobs")
            }
            assert kept == {recent, failed, pending, leased, anchor}
            assert connection.execute(
                "SELECT count(*) FROM python_processing_attempts WHERE job_id = %s", (old,)
            ).fetchone()[0] == 0
            # What the job produced stays; only its link to the deleted attempt clears.
            assert connection.execute(
                "SELECT count(*) FROM observation_processing_outcomes"
            ).fetchone()[0] == outcomes
            assert connection.execute(
                "SELECT count(*) FROM player_profile_versions"
            ).fetchone()[0] > 0


def test_cleanup_role_can_only_delete_finished_jobs(database_url, archive_server):
    with domain_database(database_url) as info:
        _jobs(info, 1, archive_server)
        for statement in (
            "SELECT id FROM python_processing_jobs",
            "DELETE FROM python_processing_jobs",
            "DELETE FROM python_processing_attempts",
            "SELECT id FROM players",
        ):
            with psycopg.connect(_as_cleanup_role(info)) as connection:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement)
        with psycopg.connect(_as_cleanup_role(info)) as connection:
            with pytest.raises(psycopg.errors.RaiseException):
                connection.execute("SELECT * FROM clashlens_prune_finished_jobs(47, 1000, true)")
        with psycopg.connect(_as_cleanup_role(info)) as connection:
            # The full operator cleanup needs the operator role.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                prune_completed_history(connection, apply=True)
        with psycopg.connect(info) as connection:
            assert connection.execute(
                "SELECT count(*) FROM python_processing_jobs"
            ).fetchone()[0] == 1


def test_cleanup_ignores_temporary_tables_the_cleanup_role_creates(database_url, archive_server):
    with domain_database(database_url) as info:
        _jobs(info, 1, archive_server)
        with psycopg.connect(_as_cleanup_role(info)) as connection:
            connection.execute(
                """
                CREATE TEMPORARY TABLE python_processing_jobs (
                    id bigint, status text, work_type text, updated_at timestamptz
                )
                """
            )
            connection.execute(
                """
                INSERT INTO python_processing_jobs VALUES (
                    1, 'complete', 'process_observation', clock_timestamp() - interval '49 hours'
                )
                """
            )
            preview = prune_completed_history(connection, jobs_only=True)
            assert preview["eligible_python_processing_jobs"] == 0


def test_collector_restart_accepts_a_saved_response_whose_job_was_removed(
    database_url, archive_server
):
    body = PROFILE_FIXTURE.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    with domain_database(database_url, include_coordinator=True) as info:
        observation, job = store_observation(
            info, archive_server, occurrence_key="restart", endpoint="profile", body=body,
            observed_at=OBSERVED, normalized_tag="#2PP", parser_version=PROFILE_PARSER_VERSION,
        )
        database, processor = _processor(info, archive_server)
        collector = CollectorDatabase(info)
        try:
            assert processor.process_job(job, owner="cleanup-test").outcome == "processed"
            with psycopg.connect(info) as connection:
                player_id = connection.execute(
                    "SELECT player_id FROM collector_observations WHERE id = %s", (observation,)
                ).fetchone()[0]
            handoff = ResponseHandoff(
                occurrence_key="restart", scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=OBSERVED - timedelta(seconds=1),
                response_completed_at=OBSERVED, http_status=200, response_hash=digest,
                content_fingerprint=content_fingerprint(
                    "profile", body, http_status=200, response_hash=digest
                ),
                byte_size=len(body), spool_key=f"sha256/{digest[:2]}/{digest}",
                collector_version="cleanup-test", key_label="regular-a",
                evidence_headers={"content-type": "application/json"},
            )
            assert collector.record_response(handoff).processing_job_id == job
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs "
                    "SET updated_at = clock_timestamp() - interval '49 hours'"
                )
            with psycopg.connect(_as_cleanup_role(info)) as connection:
                applied = prune_completed_history(connection, jobs_only=True, apply=True)
                assert applied["deleted_python_processing_jobs"] == 1

            # A restart replays the saved response; it must count as already recorded.
            recorded = ResponseResult(True, observation, None, digest, PROFILE_PARSER_VERSION)
            assert collector.record_recovered_response(handoff, serialized=True) == recorded
            assert collector.record_response(handoff) == recorded
            with psycopg.connect(info) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM python_processing_jobs"
                ).fetchone()[0] == 0
        finally:
            collector.close()
            database.close()
