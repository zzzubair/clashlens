from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens.collector_db import (
    CollectorDatabase,
    CollectorWork,
    ResponseHandoff,
    TransportFailure,
)

NOW = datetime(2030, 9, 13, 4, 0, tzinfo=UTC)


def _hash(byte: str) -> str:
    import hashlib

    return hashlib.sha256(byte.encode("ascii")).hexdigest()


def _player(connection_info: str, tag: str = "#2PP") -> int:
    with psycopg.connect(connection_info) as connection:
        return int(
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, next_due_at)
                VALUES (%s, true, %s)
                RETURNING id
                """,
                (tag, NOW - timedelta(seconds=1)),
            ).fetchone()[0]
        )


def _handoff(
    *,
    occurrence_key: str,
    response_hash: str,
    player_id: int,
    tag: str = "#2PP",
    endpoint: str = "profile",
    completed_at: datetime = NOW,
    collector_work_id: int | None = None,
) -> ResponseHandoff:
    return ResponseHandoff(
        occurrence_key=occurrence_key,
        scope="player",
        identity_key=tag,
        endpoint=endpoint,
        player_id=player_id,
        normalized_tag=tag,
        request_started_at=completed_at - timedelta(seconds=1),
        response_completed_at=completed_at,
        http_status=200,
        response_hash=response_hash,
        byte_size=1,
        spool_key=f"sha256/{response_hash[:2]}/{response_hash}",
        collector_version="python-collector-test",
        key_label="regular-a",
        evidence_headers={"content-type": "application/json"},
        collector_work_id=collector_work_id,
    )


def test_due_players_are_claimed_without_collector_work_rows(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)

        work = database.claim_due_players(limit=10, now=NOW)

        assert work == [CollectorWork(player_id, "#2PP", NOW - timedelta(seconds=1))]
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_work WHERE player_id = %s",
                    (player_id,),
                ).fetchone()[0]
                == 0
            )
            next_due_at = connection.execute(
                "SELECT next_due_at FROM players WHERE id = %s", (player_id,)
            ).fetchone()[0]
        assert next_due_at == NOW + timedelta(minutes=5)


def test_response_state_compacts_unchanged_and_enqueues_changed_response(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        first_hash = _hash("a")
        second_hash = _hash("b")

        first = database.record_response(
            _handoff(
                occurrence_key="response-1",
                response_hash=first_hash,
                player_id=player_id,
            )
        )
        unchanged = database.record_response(
            _handoff(
                occurrence_key="response-2",
                response_hash=first_hash,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        changed = database.record_response(
            _handoff(
                occurrence_key="response-3",
                response_hash=second_hash,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=10),
            )
        )

        assert first.changed is True
        assert first.observation_id is not None
        assert first.processing_job_id is not None
        assert unchanged.changed is False
        assert unchanged.observation_id is None
        assert changed.changed is True
        assert changed.observation_id is not None
        assert changed.processing_job_id is not None
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_observations"
                ).fetchone()[0]
                == 2
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'process_observation'"
                ).fetchone()[0]
                == 2
            )
            state = connection.execute(
                """
                SELECT last_response_hash, last_seen_at
                FROM collector_response_state
                WHERE scope = 'player' AND identity_key = '#2PP'
                  AND endpoint = 'profile'
                """
            ).fetchone()
        assert state == (second_hash, NOW + timedelta(minutes=10))


def test_changed_response_upsert_is_idempotent_by_occurrence_key(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        handoff = _handoff(
            occurrence_key="retryable-response",
            response_hash=_hash("a"),
            player_id=player_id,
        )

        first = database.record_response(handoff)
        retry = database.record_response(handoff)

        assert first.changed is True
        assert retry.changed is True
        assert retry.observation_id == first.observation_id
        assert retry.processing_job_id == first.processing_job_id
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_observations"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM python_processing_jobs WHERE work_type = 'process_observation'"
                ).fetchone()[0]
                == 1
            )


def test_older_response_does_not_regress_compact_state(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        latest_hash = _hash("latest")
        database.record_response(
            _handoff(
                occurrence_key="latest-response",
                response_hash=latest_hash,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=10),
            )
        )
        older = database.record_response(
            _handoff(
                occurrence_key="older-response",
                response_hash=_hash("older"),
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )

        assert older.changed is True
        with psycopg.connect(connection_info) as connection:
            state = connection.execute(
                """
                SELECT last_response_hash, last_seen_at, last_occurrence_key
                FROM collector_response_state
                WHERE scope = 'player' AND identity_key = '#2PP'
                  AND endpoint = 'profile'
                """
            ).fetchone()
        assert state == (latest_hash, NOW + timedelta(minutes=10), "latest-response")


def test_upload_claim_is_fenced_and_cleanup_waits_for_processing(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("a")
        result = database.record_response(
            _handoff(
                occurrence_key="upload-response",
                response_hash=response_hash,
                player_id=player_id,
            )
        )
        claim = database.claim_upload(owner="uploader-a", lease_seconds=60, now=NOW)
        assert claim is not None
        assert claim.response_hash == response_hash
        assert (
            database.claim_upload(owner="uploader-b", lease_seconds=60, now=NOW) is None
        )
        with pytest.raises(RuntimeError, match="upload lease lost"):
            database.complete_upload(
                claim,
                archive_reference="s3://evidence/a",
                archive_instance_id="fixture-instance",
                owner="uploader-b",
            )

        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO archive_instances (
                    instance_id, endpoint, region, bucket, marker_key,
                    marker_hash, marker_payload_version
                ) VALUES ('fixture-instance', 'archive.test:443', 'us-east-1',
                          'evidence', 'clashlens/archive-instance.json',
                          repeat('f', 64), 'v1')
                ON CONFLICT (instance_id) DO NOTHING
                """
            )
        database.complete_upload(
            claim,
            archive_reference="s3://evidence/a",
            archive_instance_id="fixture-instance",
        )
        assert database.deletable_hashes(limit=10) == []
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW,),
            )
        assert database.deletable_hashes(limit=10) == [response_hash]
        assert (
            database.delete_spool_if_deletable(response_hash, lambda _: False)
            is False
        )
        assert database.deletable_hashes(limit=10) == [response_hash]
        assert database.delete_spool_if_deletable(response_hash, lambda _: True) is True
        assert database.deletable_hashes(limit=10) == []
        republished = database.record_response(
            _handoff(
                occurrence_key="upload-response-again",
                response_hash=response_hash,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        assert republished.changed is False
        assert database.deletable_hashes(limit=10) == [response_hash]
        assert result.upload_id is not None


def test_retryable_upload_failure_returns_to_pending_with_a_fence(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        database.record_response(
            _handoff(
                occurrence_key="failed-upload-response",
                response_hash=_hash("a"),
                player_id=player_id,
            )
        )
        claim = database.claim_upload(owner="uploader", lease_seconds=60, now=NOW)
        assert claim is not None
        database.fail_upload(claim, category="archive_unavailable", detail="offline")
        retry = database.claim_upload(
            owner="uploader-retry", lease_seconds=60, now=NOW + timedelta(minutes=1)
        )
        assert retry is not None
        assert retry.response_hash == claim.response_hash


def test_transport_failure_is_durable_without_collector_work(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        failure_id = database.record_transport_failure(
            TransportFailure(
                occurrence_key="transport-1",
                scope="player",
                identity_key="#2PP",
                endpoint="profile",
                player_id=player_id,
                normalized_tag="#2PP",
                request_started_at=NOW,
                failed_at=NOW + timedelta(seconds=2),
                failure_category="timeout",
                retry_state="next_pass",
                key_label="regular-a",
            )
        )
        assert failure_id > 0
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT scope, occurrence_key FROM collector_transport_failures WHERE id = %s",
                (failure_id,),
            ).fetchone() == ("player", "transport-1")


def test_reset_reprocesses_same_hash_as_a_new_boundary_occurrence(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("same-at-reset")
        database.record_response(
            _handoff(
                occurrence_key="before-reset",
                response_hash=response_hash,
                player_id=player_id,
            )
        )
        boundary = NOW + timedelta(days=1, hours=1)
        sweep_id = database.begin_reset(boundary)
        with psycopg.connect(connection_info) as connection:
            work_id, _tag = connection.execute(
                """
                SELECT id, normalized_tag FROM collector_work
                WHERE sweep_id = %s AND kind = 'reset_baseline'
                """,
                (sweep_id,),
            ).fetchone()
        result = database.record_response(
            _handoff(
                occurrence_key="at-reset",
                response_hash=response_hash,
                player_id=player_id,
                completed_at=boundary,
                collector_work_id=work_id,
            )
        )

        assert result.changed is True
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_observations"
                ).fetchone()[0]
                == 2
            )


def test_archive_instance_validation_and_interactive_permit_budget(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        fingerprint = _hash("interactive-key")
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO archive_instances (
                    instance_id, endpoint, region, bucket, marker_key,
                    marker_hash, marker_payload_version
                ) VALUES ('fixture-instance', 'archive.test:443', 'us-east-1',
                          'evidence', 'clashlens/archive-instance.json',
                          repeat('f', 64), 'v1')
                """
            )
        database = CollectorDatabase(connection_info)
        assert database.validate_archive_instance(
            {
                "instance_id": "fixture-instance",
                "endpoint": "archive.test:443",
                "region": "us-east-1",
                "bucket": "evidence",
                "marker_key": "clashlens/archive-instance.json",
                "marker_hash": "f" * 64,
                "marker_payload_version": "v1",
            }
        )
        database.register_interactive_key(fingerprint)
        assert database.acquire_collector_permit(fingerprint).granted is True
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO shared_api_permits (credential_fingerprint, caller)
                SELECT %s, 'collector' FROM generate_series(1, 28)
                """,
                (fingerprint,),
            )
        assert database.acquire_collector_permit(fingerprint).granted is False


def test_health_metrics_report_due_work(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _player(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE players SET next_due_at = clock_timestamp() - interval '1 second'"
            )
        database = CollectorDatabase(connection_info)

        metrics = database.health_metrics()

        assert metrics["active_players"] == 1
        assert metrics["due_queue_depth"] == 1
        assert metrics["oldest_due_age_seconds"] >= 1
        assert metrics["pending_processing"] == 0
        assert metrics["pending_uploads"] == 0


def test_hash_reuse_attaches_existing_archive_without_second_upload(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        hash_a, hash_b = _hash("a"), _hash("b")
        first = database.record_response(
            _handoff(occurrence_key="a-1", response_hash=hash_a, player_id=player_id)
        )
        claim = database.claim_upload(owner="uploader", now=NOW)
        assert claim is not None
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO archive_instances (
                    instance_id, endpoint, region, bucket, marker_key,
                    marker_hash, marker_payload_version
                ) VALUES ('fixture-instance', 'archive.test:443', 'us-east-1',
                          'evidence', 'clashlens/archive-instance.json', repeat('f', 64), 'v1')
                """
            )
        database.complete_upload(
            claim,
            archive_reference="s3://evidence/a",
            archive_instance_id="fixture-instance",
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW,),
            )
        database.delete_spool_if_deletable(hash_a, lambda _: True)
        database.record_response(
            _handoff(
                occurrence_key="b-1",
                response_hash=hash_b,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        claim = database.claim_upload(owner="uploader", now=NOW + timedelta(minutes=5))
        assert claim is not None
        database.complete_upload(
            claim,
            archive_reference="s3://evidence/b",
            archive_instance_id="fixture-instance",
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW + timedelta(minutes=5),),
            )
        database.delete_spool_if_deletable(hash_b, lambda _: True)
        reused = database.record_response(
            _handoff(
                occurrence_key="a-2",
                response_hash=hash_a,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=10),
            )
        )
        assert reused.changed is True
        assert (
            database.claim_upload(
                owner="second-uploader", now=NOW + timedelta(minutes=10)
            )
            is None
        )
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT archive_reference, archive_catalogue_hash FROM collector_observations WHERE id = %s",
                (reused.observation_id,),
            ).fetchone() == ("s3://evidence/a", hash_a)
            assert (
                connection.execute(
                    "SELECT count(*) FROM archive_catalogue WHERE response_hash = %s",
                    (hash_a,),
                ).fetchone()[0]
                == 1
            )
        assert first.observation_id is not None


def test_reset_membership_freezes_on_first_insert_and_boundary_is_exact(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_player = _player(connection_info)
        database = CollectorDatabase(connection_info)
        boundary = NOW + timedelta(days=1, hours=1)
        sweep_id = database.begin_reset(boundary)
        _player(connection_info, "#3PP")
        assert database.begin_reset(boundary) == sweep_id
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT unnest(member_ids) FROM collector_reset_sweeps WHERE id = %s",
                (sweep_id,),
            ).fetchall() == [(first_player,)]
        with pytest.raises(ValueError, match="05:00 UTC"):
            database.begin_reset(boundary + timedelta(seconds=1))


def test_unfinished_older_reset_blocks_the_next_boundary(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _player(connection_info)
        database = CollectorDatabase(connection_info)
        first_boundary = NOW + timedelta(days=1, hours=1)
        database.begin_reset(first_boundary)

        assert database.begin_reset(first_boundary + timedelta(days=1)) is None
        assert (
            database.claim_due_players(
                limit=1, now=first_boundary + timedelta(days=2) - timedelta(hours=1)
            )
            == []
        )
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_reset_sweeps"
                ).fetchone()[0]
                == 1
            )


def test_complete_intent_requires_active_work_and_exact_observed_endpoints(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _player(connection_info)
        database = CollectorDatabase(connection_info)
        boundary = NOW + timedelta(days=1, hours=1)
        sweep_id = database.begin_reset(boundary)
        with psycopg.connect(connection_info) as connection:
            work_id, _tag = connection.execute(
                """
                SELECT id, normalized_tag FROM collector_work
                WHERE sweep_id = %s AND kind = 'reset_baseline'
                """,
                (sweep_id,),
            ).fetchone()
        assert database.complete_intent(work_id) is False
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                UPDATE collector_work
                SET profile_status = 'observed', battle_log_status = 'observed'
                WHERE id = %s
                """,
                (work_id,),
            )
        assert database.complete_intent(work_id) is False
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET status = 'failed' WHERE id = %s", (work_id,)
            )
        assert database.complete_intent(work_id) is False
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM collector_work WHERE id = %s", (work_id,)
                ).fetchone()[0]
                != "complete"
            )


def test_rankings_cycle_does_not_duplicate_completed_job(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = CollectorDatabase(connection_info)
        assert database.schedule_rankings_cycle(NOW) is True
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = clock_timestamp() WHERE kind = 'global_player_rankings'"
            )
        assert database.schedule_rankings_cycle(NOW) is False
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_work WHERE kind = 'global_player_rankings'"
                ).fetchone()[0]
                == 1
            )


def test_health_reset_progress_ignores_terminal_historical_sweep(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _player(connection_info)
        database = CollectorDatabase(connection_info)
        boundary = NOW + timedelta(days=1, hours=1)
        sweep_id = database.begin_reset(boundary)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = clock_timestamp() WHERE sweep_id = %s",
                (sweep_id,),
            )
        metrics = database.health_metrics()
        assert metrics["reset_total"] == 0
        assert metrics["reset_terminal"] == 0


def test_compact_work_replaces_legacy_collector_and_reset_tables(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            names = connection.execute(
                """
                SELECT name, to_regclass(current_schema() || '.' || name)
                FROM unnest(ARRAY[
                    'collector_jobs', 'collector_attempts', 'collector_endpoint_results',
                    'collector_reset_sweep_members', 'collector_reset_baseline_sweeps',
                    'collector_boundary_admission', 'global_rankings_intents',
                    'discovery_profile_intents', 'collector_spool_handoffs',
                    'collector_work'
                ]) AS names(name)
                """
            ).fetchall()
        assert dict(names) == {
            "collector_jobs": None,
            "collector_attempts": None,
            "collector_endpoint_results": None,
            "collector_reset_sweep_members": None,
            "collector_reset_baseline_sweeps": None,
            "collector_boundary_admission": None,
            "global_rankings_intents": None,
            "discovery_profile_intents": None,
            "collector_spool_handoffs": None,
            "collector_work": "collector_work",
        }


def test_refresh_work_coalesces_cools_down_and_reports_status(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            first = connection.execute(
                "SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#2PP', 30, false)"
            ).fetchone()
            second = connection.execute(
                "SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#2PP', 30, false)"
            ).fetchone()
            assert first[1:] == ("created", False)
            assert second[0] == first[0]
            assert second[1:] == ("coalesced", True)
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = clock_timestamp() WHERE id = %s",
                (first[0],),
            )
            cooldown = connection.execute(
                "SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#2PP', 30, false)"
            ).fetchone()
            status = connection.execute(
                "SELECT status FROM collector_work WHERE id = %s", (first[0],)
            ).fetchone()[0]
        assert cooldown[0] == first[0]
        assert cooldown[1:] == ("cooldown_hit", True)
        assert status == "complete"


def test_refresh_default_cooldown_is_thirty_seconds(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            first = connection.execute(
                "SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#2PP')"
            ).fetchone()
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = clock_timestamp() - interval '31 seconds' WHERE id = %s",
                (first[0],),
            )
            after_cooldown = connection.execute(
                "SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#2PP')"
            ).fetchone()

        assert after_cooldown[1:] == ("created", False)
        assert after_cooldown[0] != first[0]


def test_refresh_enqueue_is_not_publicly_executable(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            privileges = connection.execute(
                """
                SELECT
                    has_function_privilege(
                        'clashlens_collector',
                        'clashlens_enqueue_interactive(text,text,integer,boolean)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        'clashlens_python_api',
                        'clashlens_enqueue_interactive(text,text,integer,boolean)',
                        'EXECUTE'
                    ),
                    has_function_privilege(
                        'clashlens_python_worker',
                        'clashlens_enqueue_interactive(text,text,integer,boolean)',
                        'EXECUTE'
                    )
                """
            ).fetchone()

        assert privileges == (True, True, False)


def test_reset_compact_work_freezes_membership_and_pairs_endpoints(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_player = _player(connection_info)
        database = CollectorDatabase(connection_info)
        boundary = NOW + timedelta(days=1, hours=1)
        sweep_id = database.begin_reset(boundary)
        _player(connection_info, "#3PP")
        assert database.begin_reset(boundary) == sweep_id
        with psycopg.connect(connection_info) as connection:
            member_ids = connection.execute(
                "SELECT member_ids FROM collector_reset_sweeps WHERE id = %s",
                (sweep_id,),
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT id, player_id, profile_status, battle_log_status FROM collector_work WHERE sweep_id = %s",
                (sweep_id,),
            ).fetchall()
        assert member_ids == [first_player]
        work_id = rows[0][0]
        assert rows == [(work_id, first_player, "pending", "pending")]
        assert database.record_response(
            _handoff(
                occurrence_key="reset-profile",
                response_hash=_hash("reset-profile"),
                player_id=first_player,
                completed_at=boundary,
                collector_work_id=work_id,
            )
        ).changed
        assert database.record_response(
            _handoff(
                occurrence_key="reset-battle",
                response_hash=_hash("reset-battle"),
                player_id=first_player,
                endpoint="battle_log",
                completed_at=boundary,
                collector_work_id=work_id,
            )
        ).changed
        with psycopg.connect(connection_info) as connection:
            profile_status, battle_status = connection.execute(
                "SELECT profile_status, battle_log_status FROM collector_work WHERE id = %s",
                (work_id,),
            ).fetchone()
        assert (profile_status, battle_status) == ("observed", "observed")
        assert database.complete_intent(work_id) is True


def test_rankings_and_discovery_use_unleased_compact_work(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        assert database.schedule_rankings_cycle(NOW) is True
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                ([player_id],),
            )
            lease_columns = connection.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'collector_work'
                  AND column_name IN ('lease_owner', 'lease_token', 'lease_expires_at')
                """
            ).fetchall()
        assert lease_columns == []
        intents = database.pending_intents(limit=10, now=NOW + timedelta(minutes=1))
        assert {intent.kind for intent in intents} == {
            "global_player_rankings",
            "discovery_profile",
        }
        assert database.schedule_rankings_cycle(NOW) is False
        discovery_id = next(
            intent.work_id for intent in intents if intent.kind == "discovery_profile"
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_work SET profile_status = 'observed', status = 'complete', completed_at = clock_timestamp() WHERE id = %s",
                (discovery_id,),
            )
            assert (
                connection.execute(
                    "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                    ([player_id],),
                ).fetchone()[0]
                == 0
            )
        assert [
            intent.kind
            for intent in database.pending_intents(
                limit=10, now=NOW + timedelta(minutes=1)
            )
        ] == ["global_player_rankings"]
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_work WHERE kind = 'global_player_rankings'"
                ).fetchone()[0]
                == 1
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM collector_work WHERE kind = 'discovery_profile'"
                ).fetchone()[0]
                == 1
            )


def test_response_cannot_mark_another_players_work_observed(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_player = _player(connection_info)
        second_player = _player(connection_info, "#3PP")
        database = CollectorDatabase(connection_info)
        sweep_id = database.begin_reset(NOW + timedelta(days=1, hours=1))
        assert sweep_id is not None
        with psycopg.connect(connection_info) as connection:
            first_work_id = connection.execute(
                "SELECT id FROM collector_work WHERE sweep_id = %s AND player_id = %s",
                (sweep_id, first_player),
            ).fetchone()[0]

        with pytest.raises(ValueError, match="collector work identity"):
            database.record_response(
                _handoff(
                    occurrence_key="wrong-player",
                    response_hash=_hash("wrong-player"),
                    player_id=second_player,
                    tag="#3PP",
                    collector_work_id=first_work_id,
                )
            )
