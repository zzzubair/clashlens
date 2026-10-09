from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from time import monotonic

import psycopg
import pytest
from test_api_migration import migrated_production_database

from clashlens.cli import main
from clashlens.collector_db import (
    CollectorDatabase,
    ResponseHandoff,
    TransportFailure,
)
from clashlens.collector_metrics import health_metrics
from clashlens.operator_recovery import inspect_failed_items, retry_failed_item


def _seed_failures(connection_info: str) -> tuple[int, str]:
    response_hash = "a" * 64
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            """
            INSERT INTO players (normalized_tag, active)
            VALUES ('#2PP', true)
            RETURNING id
            """
        ).fetchone()[0]
        work_id = connection.execute(
            """
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                league_history_status, failure_category, failure_detail
            ) VALUES (
                'initial_collection', 'interactive', 'player', %s, '#2PP',
                clock_timestamp() - interval '1 hour', 'initial:#2PP',
                'failed', 'pending', 'observed', 'pending',
                'provider_failure', 'safe operator detail'
            )
            RETURNING id
            """,
            (player_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO collector_response_uploads (
                response_hash, spool_key, byte_size, state, attempt_count,
                next_attempt_at, last_error_category, last_error_detail
            ) VALUES (
                %s, 'sha256/aa/evidence', 321, 'failed', 4,
                'infinity'::timestamptz, 'archive_configuration_error',
                'credential was rejected'
            )
            """,
            (response_hash,),
        )
        connection.commit()
    return int(work_id), response_hash


def _payload(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _seed_processing_and_transport_failures(
    connection_info: str,
) -> tuple[int, int]:
    now = datetime.now(UTC)
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
    database = CollectorDatabase(connection_info)
    try:
        response_hash = "b" * 64
        result = database.record_response(
            ResponseHandoff(
                occurrence_key="processing-visibility",
                scope="player",
                identity_key="#2PP",
                endpoint="profile",
                player_id=int(player_id),
                normalized_tag="#2PP",
                request_started_at=now - timedelta(seconds=1),
                response_completed_at=now,
                http_status=200,
                response_hash=response_hash,
                content_fingerprint=response_hash,
                byte_size=2,
                spool_key=f"sha256/bb/{response_hash}",
                collector_version="operator-test",
                key_label="regular-a",
            )
        )
        assert result.processing_job_id is not None
        transport_id = database.record_transport_failure(
            TransportFailure(
                occurrence_key="transport-visibility",
                scope="player",
                identity_key="#2PP",
                endpoint="battle_log",
                player_id=int(player_id),
                normalized_tag="#2PP",
                request_started_at=now,
                failed_at=now + timedelta(seconds=1),
                failure_category="timeout",
                retry_state="next_pass",
                key_label="regular-b",
            )
        )
    finally:
        database.close()
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            UPDATE python_processing_jobs
            SET status = 'failed', attempt_count = max_attempts,
                outcome = 'durable_failure', failure_category = 'parser_failure',
                failure_detail = 'unsafe detail must stay private',
                completed_at = clock_timestamp(), updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (result.processing_job_id,),
        )
        connection.commit()
    return int(result.processing_job_id), transport_id


def test_failed_item_output_survives_sql_ascii_bytes() -> None:
    class Result:
        def __init__(self, rows: list[tuple[object, ...]]) -> None:
            self.rows = rows

        def fetchall(self) -> list[tuple[object, ...]]:
            return self.rows

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, statement: str, _parameters=()):
            if "FROM collector_work" in statement:
                return Result(
                    [
                        (
                            1,
                            b"initial_collection",
                            b"interactive",
                            b"#\xff",
                            b"failed",
                            b"pending",
                            b"observed",
                            b"pending",
                            None,
                            None,
                            None,
                            b"provider_\xff",
                            "due",
                            "updated",
                        )
                    ]
                )
            return Result([])

    report = inspect_failed_items(Connection(), limit=1)

    assert report["items"][0]["normalized_tag"] == "#\\xff"
    assert json.loads(json.dumps(report))["shown_count"] == 1


def test_failed_items_lists_a_bounded_mixed_queue(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        _seed_failures(connection_info)
        processing_job_id, transport_id = _seed_processing_and_transport_failures(
            connection_info
        )

        with psycopg.connect(connection_info) as connection:
            direct = inspect_failed_items(connection, limit=10)
        assert direct["shown_count"] == 4
        assert {item["item_type"] for item in direct["items"]} == {
            "collector_work",
            "archive_upload",
            "processing_job",
            "transport_failure",
        }
        processing = next(
            item for item in direct["items"] if item["item_type"] == "processing_job"
        )
        transport = next(
            item for item in direct["items"] if item["item_type"] == "transport_failure"
        )
        assert processing["processing_job_id"] == processing_job_id
        assert processing["recovery"] == "replay"
        assert processing["source_observation_id"] is not None
        assert transport["transport_failure_id"] == transport_id
        assert transport["recovery"] == "none_evidence_only"
        assert all("failure_detail" not in item for item in direct["items"])

        assert (
            main(["failed-items", "--database-url", connection_info, "--limit", "1"])
            == 0
        )
        report = _payload(capsys)

        assert report["shown_count"] == 1
        assert (
            sum(
                report[name]
                for name in (
                    "collector_work_count",
                    "upload_count",
                    "processing_job_count",
                    "transport_failure_count",
                )
            )
            == 1
        )
        assert report["truncated"] is True
        assert "failure_detail" not in report["items"][0]


def test_work_retry_previews_then_preserves_failure_and_endpoint_state(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        work_id, _response_hash = _seed_failures(connection_info)
        command = [
            "failed-items",
            "--database-url",
            connection_info,
            "--work-id",
            str(work_id),
        ]

        assert main(command) == 0
        assert _payload(capsys)["outcome"] == "preview"
        with psycopg.connect(connection_info) as connection:
            before = connection.execute(
                """
                SELECT status, profile_status, battle_log_status,
                       league_history_status, failure_category, failure_detail
                FROM collector_work WHERE id = %s
                """,
                (work_id,),
            ).fetchone()
        assert before == (
            "failed",
            "pending",
            "observed",
            "pending",
            "provider_failure",
            "safe operator detail",
        )

        assert main([*command, "--apply"]) == 0
        report = _payload(capsys)
        assert (report["outcome"], report["retried_count"]) == ("requeued", 1)
        with psycopg.connect(connection_info) as connection:
            after = connection.execute(
                """
                SELECT status, profile_status, battle_log_status,
                       league_history_status, failure_category, failure_detail,
                       due_at <= clock_timestamp()
                FROM collector_work WHERE id = %s
                """,
                (work_id,),
            ).fetchone()
        assert after == (
            "waiting_retry",
            "pending",
            "observed",
            "pending",
            "provider_failure",
            "safe operator detail",
            True,
        )


def test_work_retry_refuses_a_matching_active_item(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        work_id, _response_hash = _seed_failures(connection_info)
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT player_id FROM collector_work WHERE id = %s", (work_id,)
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, due_at,
                    coalescing_key, status, profile_status, battle_log_status,
                    league_history_status
                ) VALUES (
                    'initial_collection', 'interactive', 'player', %s, '#2PP',
                    clock_timestamp(), 'initial:#2PP', 'pending',
                    'pending', 'pending', 'pending'
                )
                """,
                (player_id,),
            )
            connection.commit()

        code = main(
            [
                "failed-items",
                "--database-url",
                connection_info,
                "--work-id",
                str(work_id),
                "--apply",
            ]
        )
        report = _payload(capsys)

        assert code == 1
        assert report["reason"] == "matching_collector_work_is_already_active"


def test_upload_retry_allows_repaired_config_but_blocks_integrity_failure(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        _work_id, response_hash = _seed_failures(connection_info)
        command = [
            "failed-items",
            "--database-url",
            connection_info,
            "--upload-hash",
            response_hash,
        ]

        assert main(command) == 0
        preview = _payload(capsys)
        assert preview["outcome"] == "preview"
        assert (
            preview["operator_action"]
            == "repair_archive_configuration_and_restart_collector"
        )
        assert main([*command, "--apply"]) == 0
        applied = _payload(capsys)
        assert applied["outcome"] == "requeued"
        assert (
            applied["operator_action"]
            == "repair_archive_configuration_and_restart_collector"
        )
        with psycopg.connect(connection_info) as connection:
            row = connection.execute(
                """
                SELECT state, attempt_count, last_error_category,
                       last_error_detail, spool_key, byte_size
                FROM collector_response_uploads WHERE response_hash = %s
                """,
                (response_hash,),
            ).fetchone()
            assert row == (
                "pending",
                4,
                "archive_configuration_error",
                "credential was rejected",
                "sha256/aa/evidence",
                321,
            )
            connection.execute(
                """
                UPDATE collector_response_uploads
                SET state = 'failed',
                    last_error_category = 'archive_checksum_mismatch'
                WHERE response_hash = %s
                """,
                (response_hash,),
            )
            connection.commit()

        assert main([*command, "--apply"]) == 1
        report = _payload(capsys)
        assert report["reason"] == "archive_integrity_repair_required"


def test_worker_role_cannot_run_collector_retry(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        work_id, _response_hash = _seed_failures(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET ROLE clashlens_python_worker")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                retry_failed_item(connection, work_id=work_id, apply=True)


def test_retry_refuses_a_busy_row_within_its_lock_timeout(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        work_id, _response_hash = _seed_failures(connection_info)
        with (
            psycopg.connect(connection_info) as blocker,
            psycopg.connect(connection_info) as operator,
        ):
            blocker.execute(
                "SELECT id FROM collector_work WHERE id = %s FOR UPDATE", (work_id,)
            )
            started = monotonic()
            report = retry_failed_item(operator, work_id=work_id, apply=True)

            assert monotonic() - started < 3
            assert report["reason"] == "retry_lock_timeout"


def test_an_accepted_failed_job_keeps_its_evidence_and_a_new_failure_still_counts(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute("INSERT INTO players (normalized_tag) VALUES ('#2PP')")
        job_id, _transport_id = _seed_processing_and_transport_failures(connection_info)

        def outstanding() -> dict[str, int | float]:
            # Read as the collector does in production.
            with psycopg.connect(connection_info) as connection:
                connection.execute("SET ROLE clashlens_collector")
                return health_metrics(connection)

        def job_state() -> tuple[object, ...]:
            with psycopg.connect(connection_info) as connection:
                return connection.execute(
                    """
                    SELECT status, attempt_count, outcome, failure_category,
                           failure_detail, observation_id IS NOT NULL
                    FROM python_processing_jobs WHERE id = %s
                    """,
                    (job_id,),
                ).fetchone()

        before = job_state()
        assert outstanding()["failed_processing"] == 1
        command = [
            "failed-items",
            "--database-url",
            connection_info,
            "--accept-job-id",
            str(job_id),
            "--operator",
            "ops:zubair",
        ]
        # A reason is required.
        assert main(command) == 1
        capsys.readouterr()
        reason = ["--reason", "profile does not match profile-schema-v1"]

        assert main([*command, *reason]) == 0
        assert _payload(capsys)["outcome"] == "preview"
        assert outstanding()["failed_processing"] == 1

        assert main([*command, *reason, "--apply"]) == 0
        assert _payload(capsys)["outcome"] == "accepted"
        assert outstanding()["failed_processing"] == 0
        # The job and its evidence stay exactly as they were.
        assert job_state() == before
        with psycopg.connect(connection_info) as connection:
            record = connection.execute(
                """
                SELECT operator_identity, reason, accepted_at <= clock_timestamp()
                FROM python_failed_job_acceptances WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
            listed = inspect_failed_items(connection, limit=10)["items"]
        assert record == (
            "ops:zubair",
            "profile does not match profile-schema-v1",
            True,
        )
        jobs = [item for item in listed if item["item_type"] == "processing_job"]
        assert [(item["processing_job_id"], item["recovery"]) for item in jobs] == [
            (job_id, "accepted")
        ]
        assert jobs[0]["accepted"]["operator"] == "ops:zubair"

        # Accepting twice keeps the first record.
        assert main([*command, "--reason", "a different reason", "--apply"]) == 1
        assert _payload(capsys)["reason"] == "processing_job_already_accepted"

        # A new failure after the old one was accepted still raises the alarm.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, status, updated_at
                )
                SELECT 'reconcile_ranked_day', 'new-failure',
                       jsonb_build_object('player_id', id,
                           'ranked_day_start', '2026-10-08T05:00:00Z'),
                       'failed', clock_timestamp()
                FROM players WHERE normalized_tag = '#2PP'
                """
            )
        metrics = outstanding()
        assert metrics["failed_processing"] == 1
        assert metrics["newest_failed_processing_age_seconds"] < 600


def test_failed_jobs_replay_all_or_none_through_the_replay_function(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute("INSERT INTO players (normalized_tag) VALUES ('#2PP')")
        job_id, _transport_id = _seed_processing_and_transport_failures(connection_info)

        def replays() -> list[tuple[object, ...]]:
            with psycopg.connect(connection_info) as connection:
                return connection.execute(
                    """
                    SELECT request.operator_identity, request.reason, job.id,
                           job.work_type, job.status,
                           job.replay_observation_id = failed.observation_id,
                           job.parser_version = failed.parser_version
                    FROM python_replay_requests AS request
                    JOIN python_processing_jobs AS job ON job.id = request.job_id
                    JOIN python_processing_jobs AS failed ON failed.id = %s
                    """,
                    (job_id,),
                ).fetchall()

        command = ["failed-items", "--database-url", connection_info, "--operator", "ops:zubair"]
        replay = [*command, "--replay-job-id", str(job_id)]
        reason = ["--reason", "replay the failed profile response"]
        # A reason is required.
        assert main(replay) == 1
        capsys.readouterr()

        # A preview asks the replay function, then keeps nothing.
        assert main([*replay, *reason]) == 0
        preview = _payload(capsys)
        assert (preview["outcome"], preview["applied"]) == ("preview", False)
        assert [item["outcome"] for item in preview["items"]] == ["would_enqueue"]
        assert "replay_job_id" not in preview["items"][0]
        assert replays() == []

        # A job the function refuses: battle parser v3 reads battle logs only.
        with psycopg.connect(connection_info) as connection:
            parser = connection.execute(
                "SELECT parser_version FROM python_processing_jobs WHERE id = %s", (job_id,)
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE python_processing_jobs
                SET parser_version = 'supercell-battle-parser-v3' WHERE id = %s
                """,
                (job_id,),
            )
        assert main([*replay, *reason, "--apply"]) == 1
        refused = _payload(capsys)
        assert (refused["applied"], refused["refused_count"]) == (False, 1)
        assert refused["items"][0]["reason"] == "replay_not_allowed"
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET parser_version = %s WHERE id = %s",
                (parser, job_id),
            )

        # One unknown job refuses the whole batch.
        assert main([*replay, "--replay-job-id", "999999", *reason, "--apply"]) == 1
        batch = _payload(capsys)
        assert [(item["processing_job_id"], item["outcome"]) for item in batch["items"]] == [
            (job_id, "not_checked"),
            (999999, "refused"),
        ]
        assert replays() == []

        assert main([*replay, *reason, "--apply"]) == 0
        applied = _payload(capsys)
        assert (applied["outcome"], applied["replayed_count"]) == ("replay_enqueued", 1)
        replay_job_id = applied["items"][0]["replay_job_id"]
        assert replays() == [
            (
                "ops:zubair",
                "replay the failed profile response",
                replay_job_id,
                "replay_observation",
                "pending",
                True,
                True,
            )
        ]

        # The same request again names the same queued job; another reason is refused.
        assert main([*replay, *reason, "--apply"]) == 0
        assert _payload(capsys)["items"][0]["replay_job_id"] == replay_job_id
        assert main([*replay, "--reason", "a different reason", "--apply"]) == 1
        assert _payload(capsys)["items"][0]["reason"] == (
            "replay_requested_with_a_different_reason"
        )
        # Only failed jobs replay.
        assert main([*command, "--replay-job-id", str(replay_job_id), *reason]) == 1
        assert _payload(capsys)["items"][0]["reason"] == "processing_job_is_pending"
        assert len(replays()) == 1
