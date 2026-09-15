from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens.collector_db import CollectorDatabase, ResponseHandoff
from clashlens.collector_uploads import (
    UploadLeaseLost,
    claim_upload,
    complete_upload,
    fail_upload,
    renew_upload,
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


def _archive_instance(connection_info: str) -> None:
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


def _handoff(
    *,
    occurrence_key: str,
    response_hash: str,
    player_id: int,
    tag: str = "#2PP",
    endpoint: str = "profile",
    completed_at: datetime = NOW,
    collector_work_id: int | None = None,
    content_fingerprint: str | None = None,
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
        content_fingerprint=content_fingerprint or response_hash,
        byte_size=1,
        spool_key=f"sha256/{response_hash[:2]}/{response_hash}",
        collector_version="python-collector-test",
        key_label="regular-a",
        evidence_headers={"content-type": "application/json"},
        collector_work_id=collector_work_id,
    )


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
        claim = claim_upload(database, owner="uploader-a", lease_seconds=60, now=NOW)
        assert claim is not None
        assert claim.response_hash == response_hash
        assert (
            claim_upload(database, owner="uploader-b", lease_seconds=60, now=NOW)
            is None
        )
        with pytest.raises(RuntimeError, match="upload lease lost"):
            complete_upload(
                database,
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
        complete_upload(
            database,
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
            database.delete_spool_if_deletable(response_hash, lambda _: False) is False
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
        assert republished.observation_id is None
        assert republished.processing_job_id is None
        assert database.deletable_hashes(limit=10) == []
        assert response_hash not in database.referenced_spool_hashes()
        assert result.upload_id is not None


def test_cleanup_prioritizes_oldest_last_use_over_a_reused_upload(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        first_player = _player(connection_info)
        second_player = _player(connection_info, "#8VV")
        cold_player = _player(connection_info, "#9YY")
        database = CollectorDatabase(connection_info)
        _archive_instance(connection_info)
        hot_hash = _hash("shared-history")
        cold_hash = _hash("one-use-profile")

        database.record_response(
            _handoff(
                occurrence_key="shared-history-first",
                response_hash=hot_hash,
                player_id=first_player,
                endpoint="league_history",
            )
        )
        hot_claim = claim_upload(database, owner="hot-uploader", now=NOW)
        assert hot_claim is not None
        complete_upload(
            database,
            hot_claim,
            archive_reference="s3://evidence/shared-history",
            archive_instance_id="fixture-instance",
            now=NOW,
        )
        database.record_response(
            _handoff(
                occurrence_key="cold-profile",
                response_hash=cold_hash,
                player_id=cold_player,
                tag="#9YY",
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        cold_claim = claim_upload(
            database, owner="cold-uploader", now=NOW + timedelta(minutes=5)
        )
        assert cold_claim is not None
        complete_upload(
            database,
            cold_claim,
            archive_reference="s3://evidence/one-use-profile",
            archive_instance_id="fixture-instance",
            now=NOW + timedelta(minutes=5),
        )
        database.record_response(
            _handoff(
                occurrence_key="shared-history-reused",
                response_hash=hot_hash,
                player_id=second_player,
                tag="#8VV",
                endpoint="league_history",
                completed_at=NOW + timedelta(minutes=10),
            )
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW + timedelta(minutes=10),),
            )

        assert database.deletable_hashes(limit=1) == [cold_hash]


def test_upload_renewal_requires_the_same_live_claim(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        database.record_response(
            _handoff(
                occurrence_key="renew-upload-response",
                response_hash=_hash("renew-upload"),
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader", lease_seconds=60, now=NOW)
        assert claim is not None

        renewed_until = renew_upload(
            database, claim, lease_seconds=60, now=NOW + timedelta(seconds=20)
        )

        assert renewed_until == NOW + timedelta(seconds=80)
        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            renew_upload(
                database,
                replace(claim, token="00000000-0000-0000-0000-000000000000"),
                lease_seconds=60,
                now=NOW + timedelta(seconds=40),
            )
        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            renew_upload(
                database,
                claim,
                lease_seconds=60,
                now=NOW + timedelta(seconds=81),
            )


def test_complete_upload_reconciles_only_the_exact_settled_claim(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("complete-retry")
        database.record_response(
            _handoff(
                occurrence_key="complete-retry-response",
                response_hash=response_hash,
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader", now=NOW)
        assert claim is not None
        _archive_instance(connection_info)
        reference = f"s3://evidence/sha256/{response_hash[:2]}/{response_hash}"

        complete_upload(
            database,
            claim,
            archive_reference=reference,
            archive_instance_id="fixture-instance",
            now=NOW + timedelta(seconds=1),
        )
        # This is the call made after a commit acknowledgement is lost.
        complete_upload(
            database,
            claim,
            archive_reference=reference,
            archive_instance_id="fixture-instance",
            now=NOW + timedelta(seconds=2),
        )

        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            complete_upload(
                database,
                claim,
                archive_reference=reference + "-different",
                archive_instance_id="fixture-instance",
                now=NOW + timedelta(seconds=2),
            )
        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            complete_upload(
                database,
                replace(claim, attempt_count=claim.attempt_count + 1),
                archive_reference=reference,
                archive_instance_id="fixture-instance",
                now=NOW + timedelta(seconds=2),
            )
        with psycopg.connect(connection_info) as connection:
            settled_token, catalogue_rows = connection.execute(
                """
                SELECT upload.settled_lease_token,
                       (SELECT count(*) FROM archive_catalogue
                        WHERE archive_reference = %s)
                FROM collector_response_uploads AS upload
                WHERE upload.response_hash = %s
                """,
                (reference, response_hash),
            ).fetchone()
        assert str(settled_token) == claim.token
        assert catalogue_rows == 1


def test_fail_upload_reconciles_exact_retry_but_fences_an_old_owner(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        database.record_response(
            _handoff(
                occurrence_key="fail-retry-response",
                response_hash=_hash("fail-retry"),
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader-a", now=NOW)
        assert claim is not None

        fail_upload(
            database,
            claim,
            category="archive_unavailable",
            detail="offline",
            retryable=True,
            now=NOW + timedelta(seconds=1),
        )
        fail_upload(
            database,
            claim,
            category="archive_unavailable",
            detail="offline",
            retryable=True,
            now=NOW + timedelta(seconds=2),
        )
        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            fail_upload(
                database,
                claim,
                category="archive_unavailable",
                detail="different result",
                retryable=True,
                now=NOW + timedelta(seconds=2),
            )

        newer = claim_upload(
            database, owner="uploader-b", now=NOW + timedelta(seconds=7)
        )
        assert newer is not None
        assert newer.attempt_count == claim.attempt_count + 1
        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            fail_upload(
                database,
                claim,
                category="archive_unavailable",
                detail="offline",
                retryable=True,
                now=NOW + timedelta(seconds=8),
            )


def test_upload_retire_after_follows_the_response_season(
    database_url: str,
) -> None:
    # A season-N response uploaded days later still retires with season N:
    # retire_after derives from response_completed_at, not upload completion.
    response_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    uploaded_later = response_at + timedelta(days=40)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("season-dated")
        database.record_response(
            _handoff(
                occurrence_key="season-dated-response",
                response_hash=response_hash,
                player_id=player_id,
                completed_at=response_at,
            )
        )
        claim = claim_upload(database, owner="uploader", lease_seconds=60)
        assert claim is not None
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
        complete_upload(
            database,
            claim,
            archive_reference="s3://evidence/season-dated",
            archive_instance_id="fixture-instance",
            now=uploaded_later,
        )
        with psycopg.connect(connection_info) as connection:
            row = connection.execute(
                """
                SELECT retire_after,
                       clashlens_season_retire_after(%s),
                       clashlens_season_retire_after(%s)
                FROM archive_catalogue WHERE response_hash = %s
                """,
                (response_at, uploaded_later, response_hash),
            ).fetchone()
        assert row is not None
        assert row[0] == row[1]
        assert row[0] != row[2]


def test_pending_upload_uses_later_ignored_hash_sighting_for_retention(
    database_url: str,
) -> None:
    response_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    seen_next_season = response_at + timedelta(days=29)
    upload_at = seen_next_season + timedelta(days=30)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("pending-next-season")
        fingerprint = _hash("pending-used-fields")
        database.record_response(
            _handoff(
                occurrence_key="pending-first",
                response_hash=response_hash,
                content_fingerprint=fingerprint,
                player_id=player_id,
                completed_at=response_at,
            )
        )
        displaced = database.record_response(
            _handoff(
                occurrence_key="pending-ignored-hash",
                response_hash=_hash("pending-ignored-hash"),
                content_fingerprint=fingerprint,
                player_id=player_id,
                completed_at=seen_next_season,
            )
        )
        assert displaced.changed is False
        late = database.record_response(
            _handoff(
                occurrence_key="pending-late-replay",
                response_hash=response_hash,
                content_fingerprint=fingerprint,
                player_id=player_id,
                completed_at=response_at + timedelta(days=1),
            )
        )
        assert late.changed is False
        with psycopg.connect(connection_info) as connection:
            assert (
                connection.execute(
                    "SELECT latest_sighting_at FROM collector_response_uploads WHERE response_hash = %s",
                    (response_hash,),
                ).fetchone()[0]
                == seen_next_season
            )

        claim = claim_upload(database, owner="uploader", now=upload_at)
        assert claim is not None
        _archive_instance(connection_info)
        complete_upload(
            database,
            claim,
            archive_reference="s3://evidence/pending-next-season",
            archive_instance_id="fixture-instance",
            now=upload_at,
        )
        with psycopg.connect(connection_info) as connection:
            row = connection.execute(
                """
                SELECT retire_after,
                       clashlens_season_retire_after(%s),
                       clashlens_season_retire_after(%s)
                FROM archive_catalogue WHERE response_hash = %s
                """,
                (seen_next_season, response_at, response_hash),
            ).fetchone()
        assert row is not None
        assert row[0] == row[1]
        assert row[0] != row[2]


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
        claim = claim_upload(database, owner="uploader", lease_seconds=60, now=NOW)
        assert claim is not None
        fail_upload(database, claim, category="archive_unavailable", detail="offline")
        retry = claim_upload(
            database,
            owner="uploader-retry",
            lease_seconds=60,
            now=NOW + timedelta(minutes=1),
        )
        assert retry is not None
        assert retry.response_hash == claim.response_hash


def test_retired_archive_location_reuploads_under_a_generation(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        hash_a, hash_b = _hash("retired-a"), _hash("retired-b")
        database.record_response(
            _handoff(
                occurrence_key="retired-a-1",
                response_hash=hash_a,
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader", now=NOW)
        assert claim is not None
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
        complete_upload(
            database,
            claim,
            archive_reference="s3://evidence/old-a",
            archive_instance_id="fixture-instance",
            now=NOW,
        )
        # Retirement tombstones the old location; later re-observation must
        # not write to it again. B's upload completes first so the recycled
        # row is the only pending upload when A is observed again.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE archive_catalogue SET availability = 'expired' WHERE archive_reference = 's3://evidence/old-a'"
            )
        database.record_response(
            _handoff(
                occurrence_key="retired-b",
                response_hash=hash_b,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        claim_b = claim_upload(
            database, owner="uploader", now=NOW + timedelta(minutes=5)
        )
        assert claim_b is not None
        assert claim_b.response_hash == hash_b
        complete_upload(
            database,
            claim_b,
            archive_reference="s3://evidence/b-done",
            archive_instance_id="fixture-instance",
            now=NOW + timedelta(minutes=5),
        )
        reobserved = database.record_response(
            _handoff(
                occurrence_key="retired-a-2",
                response_hash=hash_a,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=10),
            )
        )
        assert reobserved.changed is True

        recycled = claim_upload(
            database, owner="uploader", now=NOW + timedelta(minutes=10)
        )
        assert recycled is not None
        assert recycled.response_hash == hash_a
        assert len(recycled.generation) == 32
        generation_reference = (
            f"s3://evidence/sha256/{hash_a[:2]}/{hash_a}"
            f"/generation/{recycled.generation}"
        )
        complete_upload(
            database,
            recycled,
            archive_reference=generation_reference,
            archive_instance_id="fixture-instance",
            now=NOW + timedelta(minutes=10),
        )
        with psycopg.connect(connection_info) as connection:
            rows = connection.execute(
                """
                SELECT archive_reference, availability
                FROM archive_catalogue
                WHERE response_hash = %s
                ORDER BY first_verified_at
                """,
                (hash_a,),
            ).fetchall()
        assert rows == [
            ("s3://evidence/old-a", "expired"),
            (generation_reference, "verified"),
        ]


@pytest.mark.parametrize("availability", ["retiring", "expired"])
def test_identical_response_reuploads_when_its_location_is_tombstoned(
    database_url: str, availability: str
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("identical-retired")
        database.record_response(
            _handoff(
                occurrence_key="identical-first",
                response_hash=response_hash,
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader", now=NOW)
        assert claim is not None
        _archive_instance(connection_info)
        old_reference = f"s3://evidence/sha256/{response_hash[:2]}/{response_hash}"
        complete_upload(
            database,
            claim,
            archive_reference=old_reference,
            archive_instance_id="fixture-instance",
            now=NOW,
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE archive_catalogue SET availability = %s WHERE archive_reference = %s",
                (availability, old_reference),
            )

        repeated = database.record_response(
            _handoff(
                occurrence_key=f"identical-after-{availability}",
                response_hash=response_hash,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        assert repeated.changed is True
        assert repeated.observation_id is not None
        assert repeated.processing_job_id is not None
        recycled = claim_upload(
            database, owner="uploader", now=NOW + timedelta(minutes=5)
        )
        assert recycled is not None
        assert recycled.response_hash == response_hash
        assert len(recycled.generation) == 32
        assert recycled.generation not in old_reference
        with pytest.raises(UploadLeaseLost, match="upload lease lost"):
            complete_upload(
                database,
                claim,
                archive_reference=old_reference,
                archive_instance_id="fixture-instance",
                now=NOW + timedelta(minutes=5),
            )


def test_ignored_raw_change_follows_the_retained_observation_archive(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        hash_a, hash_b = _hash("retained-a"), _hash("ignored-b")
        fingerprint = _hash("used-fields")
        first = database.record_response(
            _handoff(
                occurrence_key="retained-a",
                response_hash=hash_a,
                content_fingerprint=fingerprint,
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader", now=NOW)
        assert claim is not None
        _archive_instance(connection_info)
        reference = f"s3://evidence/sha256/{hash_a[:2]}/{hash_a}"
        complete_upload(
            database,
            claim,
            archive_reference=reference,
            archive_instance_id="fixture-instance",
            now=NOW,
        )
        ignored = database.record_response(
            _handoff(
                occurrence_key="ignored-b-available",
                response_hash=hash_b,
                content_fingerprint=fingerprint,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=5),
            )
        )
        assert ignored.changed is False
        assert (
            claim_upload(database, owner="uploader", now=NOW + timedelta(minutes=5))
            is None
        )
        with psycopg.connect(connection_info) as connection:
            state = connection.execute(
                "SELECT last_response_hash, last_observation_id FROM collector_response_state"
            ).fetchone()
            assert state == (hash_b, first.observation_id)
            connection.execute(
                "UPDATE archive_catalogue SET availability = 'expired' WHERE archive_reference = %s",
                (reference,),
            )

        preserved = database.record_response(
            _handoff(
                occurrence_key="ignored-b-after-expiry",
                response_hash=hash_b,
                content_fingerprint=fingerprint,
                player_id=player_id,
                completed_at=NOW + timedelta(minutes=10),
            )
        )
        assert preserved.changed is True
        replacement = claim_upload(
            database, owner="uploader", now=NOW + timedelta(minutes=10)
        )
        assert replacement is not None
        assert replacement.response_hash == hash_b
        assert replacement.generation == ""


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
        claim = claim_upload(database, owner="uploader", now=NOW)
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
        complete_upload(
            database,
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
        claim = claim_upload(database, owner="uploader", now=NOW + timedelta(minutes=5))
        assert claim is not None
        complete_upload(
            database,
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
            claim_upload(
                database, owner="second-uploader", now=NOW + timedelta(minutes=10)
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
