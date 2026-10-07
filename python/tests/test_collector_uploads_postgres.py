from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database
from psycopg import sql

from clashlens.collector_db import (
    REFERENCED_SPOOL_HASHES_SQL,
    CollectorDatabase,
    ResponseHandoff,
)
from clashlens.collector_uploads import (
    NEXT_DUE_UPLOAD_SQL,
    UploadLeaseLost,
    claim_upload,
    complete_upload,
    fail_upload,
    renew_upload,
)

NOW = datetime(2020, 9, 13, 4, 0, tzinfo=UTC)


@contextmanager
def upload_database(database_url: str) -> Iterator[str]:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # Upload admission normally uses the database clock. Seed its due time
        # on the same controlled timeline as claim/renew/complete below, even
        # when the real clock has passed every date in this test file.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                sql.SQL(
                    "ALTER TABLE collector_response_uploads "
                    "ALTER COLUMN next_attempt_at SET DEFAULT {}"
                ).format(sql.Literal(NOW))
            )
        yield connection_info


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


def test_cleanup_batch_marks_only_deleted_eligible_copies(database_url: str) -> None:
    with upload_database(database_url) as connection_info:
        _archive_instance(connection_info)
        database = CollectorDatabase(connection_info)
        hashes = [_hash(letter) for letter in "abc"]
        observations = {}
        for index, response_hash in enumerate(hashes):
            tag = f"#2P{'PYL'[index]}"
            observations[response_hash] = database.record_response(
                _handoff(
                    occurrence_key=f"batch-{index}",
                    response_hash=response_hash,
                    player_id=_player(connection_info, tag),
                    tag=tag,
                )
            ).observation_id
            claim = claim_upload(database, owner="uploader", now=NOW)
            assert claim is not None
            complete_upload(
                database,
                claim,
                archive_reference=f"s3://evidence/{index}",
                archive_instance_id="fixture-instance",
                now=NOW,
            )
        deletable, protected, processing = hashes
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s"
                " WHERE observation_id = ANY(%s)",
                (NOW, [observations[deletable], observations[protected]]),
            )
        attempted: list[str] = []

        def delete(candidate: str) -> bool:
            attempted.append(candidate)
            return candidate != protected

        assert database.delete_spool_if_deletable(hashes, delete) == 1
        assert sorted(attempted) == sorted([deletable, protected])
        assert database.deletable_hashes(limit=10) == [protected]
        assert deletable not in database.referenced_spool_hashes()
        assert processing in database.referenced_spool_hashes()


def test_upload_claim_is_fenced_and_cleanup_waits_for_processing(
    database_url: str,
) -> None:
    with upload_database(database_url) as connection_info:
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
                now=NOW,
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
            now=NOW,
        )
        assert database.deletable_hashes(limit=10) == []
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW,),
            )
        assert database.deletable_hashes(limit=10) == [response_hash]
        assert database.delete_spool_if_deletable([response_hash], lambda _: False) == 0
        assert database.deletable_hashes(limit=10) == [response_hash]
        assert database.delete_spool_if_deletable([response_hash], lambda _: True) == 1
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
    with upload_database(database_url) as connection_info:
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
    with upload_database(database_url) as connection_info:
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
    with upload_database(database_url) as connection_info:
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
    with upload_database(database_url) as connection_info:
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


def test_late_upload_counts_86_days_from_its_latest_sighting(
    database_url: str,
) -> None:
    response_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    uploaded_later = response_at + timedelta(days=40)
    with upload_database(database_url) as connection_info:
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
        claim = claim_upload(
            database, owner="uploader", lease_seconds=60, now=uploaded_later
        )
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
                SELECT retire_after, first_verified_at
                FROM archive_catalogue WHERE response_hash = %s
                """,
                (response_hash,),
            ).fetchone()
        assert row == (response_at + timedelta(days=86), uploaded_later)


def test_pending_upload_uses_later_ignored_hash_sighting_for_retention(
    database_url: str,
) -> None:
    response_at = NOW
    seen_next_season = response_at + timedelta(days=29)
    upload_at = response_at + timedelta(days=2)
    with upload_database(database_url) as connection_info:
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
                (seen_next_season, upload_at, response_hash),
            ).fetchone()
        assert row is not None
        assert row[0] == row[1] == seen_next_season + timedelta(days=86)
        assert row[0] != row[2]


def test_retryable_upload_failure_returns_to_pending_with_a_fence(
    database_url: str,
) -> None:
    with upload_database(database_url) as connection_info:
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
        fail_upload(
            database, claim, category="archive_unavailable", detail="offline", now=NOW
        )
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
    with upload_database(database_url) as connection_info:
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

        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_response_uploads SET next_attempt_at = %s WHERE state = 'pending'",
                (NOW,),
            )
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
    with upload_database(database_url) as connection_info:
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
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_response_uploads SET next_attempt_at = %s WHERE state = 'pending'",
                (NOW,),
            )
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


@pytest.mark.parametrize(
    ("at_claim", "at_completion"),
    [
        ("verified", "verified"),
        ("retiring", "retiring"),
        ("expired", "expired"),
        ("verified", "retiring"),
    ],
)
def test_upload_never_reuses_a_tombstoned_legacy_location(
    database_url: str, at_claim: str, at_completion: str
) -> None:
    # A location catalogued before upload rows existed has no upload row, so
    # its bytes seen again would upload to the same key without a generation.
    with upload_database(database_url) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        response_hash = _hash("legacy-location")
        reference = f"s3://evidence/sha256/{response_hash[:2]}/{response_hash}"
        _archive_instance(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO archive_catalogue (
                    response_hash, archive_reference, byte_size, archive_instance_id,
                    first_verified_at, retire_after, availability
                ) VALUES (%s, %s, 1, 'fixture-instance', %s, %s, %s)
                """,
                (response_hash, reference, NOW - timedelta(days=200), NOW - timedelta(days=1), at_claim),
            )
        observed = database.record_response(
            _handoff(
                occurrence_key="legacy-location-seen-again",
                response_hash=response_hash,
                player_id=player_id,
            )
        )
        claim = claim_upload(database, owner="uploader", lease_seconds=120, now=NOW)
        assert claim is not None
        # The write destination is chosen before any bytes are written.
        assert (claim.generation == "") is (at_claim == "verified")
        destination = reference + (f"/generation/{claim.generation}" if claim.generation else "")
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE archive_catalogue SET availability = %s WHERE archive_reference = %s",
                (at_completion, reference),
            )
        complete_upload(
            database,
            claim,
            archive_reference=destination,
            archive_instance_id="fixture-instance",
            now=NOW + timedelta(minutes=1),
        )
        with psycopg.connect(connection_info) as connection:
            bound = connection.execute(
                "SELECT archive_reference FROM collector_observations WHERE id = %s",
                (observed.observation_id,),
            ).fetchone()[0]
            retire_after = connection.execute(
                "SELECT retire_after FROM archive_catalogue WHERE archive_reference = %s",
                (bound or reference,),
            ).fetchone()[0]
        if destination != reference:
            assert bound == destination
            assert retire_after == NOW + timedelta(days=86)
            return
        if at_completion == "verified":
            assert bound == destination
            assert retire_after == NOW + timedelta(days=86)
            return
        # Marked between claim and completion: never attached, uploaded again.
        assert bound is None
        assert retire_after == NOW - timedelta(days=1)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE collector_response_uploads SET next_attempt_at = %s WHERE response_hash = %s",
                (NOW + timedelta(minutes=1), response_hash),
            )
        retry = claim_upload(database, owner="uploader", now=NOW + timedelta(minutes=1))
        assert retry is not None and retry.response_hash == response_hash
        assert len(retry.generation) == 32


def test_ignored_raw_change_follows_the_retained_observation_archive(
    database_url: str,
) -> None:
    with upload_database(database_url) as connection_info:
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
    with upload_database(database_url) as connection_info:
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
            now=NOW,
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW,),
            )
        database.delete_spool_if_deletable([hash_a], lambda _: True)
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
            now=NOW + timedelta(minutes=5),
        )
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (NOW + timedelta(minutes=5),),
            )
        database.delete_spool_if_deletable([hash_b], lambda _: True)
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


def test_claim_reads_only_the_next_due_row_in_a_production_sized_backlog(
    database_url: str,
) -> None:
    # Production on 2026-10-01 held about 456,000 complete and 57,600 due
    # rows; the old claim read every due row's table page, then sorted them.
    with upload_database(database_url) as connection_info:
        _archive_instance(connection_info)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO collector_response_uploads (
                    response_hash, spool_key, byte_size, state,
                    archive_reference, archive_instance_id, completed_at,
                    next_attempt_at, created_at
                )
                SELECT encode(sha256(n::text::bytea), 'hex'), 'sha256/' || n, 1,
                       kind.state,
                       CASE WHEN kind.state = 'complete' THEN 's3://evidence/' || n END,
                       CASE WHEN kind.state = 'complete' THEN 'fixture-instance' END,
                       CASE WHEN kind.state = 'complete' THEN %(now)s::timestamptz END,
                       CASE WHEN kind.state = 'failed'
                            THEN %(now)s::timestamptz + interval '1 hour'
                            ELSE %(now)s::timestamptz - n * interval '10 milliseconds'
                       END,
                       %(now)s::timestamptz - interval '1 day'
                FROM generate_series(1, 200000) AS n
                CROSS JOIN LATERAL (
                    SELECT CASE n %% 8 WHEN 0 THEN 'pending'
                                       WHEN 4 THEN 'failed'
                                       ELSE 'complete' END AS state
                ) AS kind
                """,
                {"now": NOW},
            )
            connection.execute("ANALYZE collector_response_uploads")
        with psycopg.connect(connection_info) as connection:
            with connection.transaction(force_rollback=True):
                plan = connection.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + NEXT_DUE_UPLOAD_SQL,
                    (NOW,),
                ).fetchone()[0][0]["Plan"]
        pages = plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]
        assert pages < 50, plan

        database = CollectorDatabase(connection_info)
        first = claim_upload(database, owner="uploader-a", now=NOW)
        second = claim_upload(database, owner="uploader-b", now=NOW)
        assert first is not None and second is not None
        assert first.spool_key == "sha256/200000"
        assert second.spool_key == "sha256/199992"


def _rescans_a_table(plan: dict) -> bool:
    children = plan.get("Plans", [])
    if plan["Node Type"] == "Nested Loop" and _scans_a_table(children[1]):
        return True
    return any(_rescans_a_table(child) for child in children)


def _scans_a_table(plan: dict) -> bool:
    return plan["Node Type"] == "Seq Scan" or any(
        _scans_a_table(child) for child in plan.get("Plans", [])
    )


def test_spool_reference_read_ignores_stale_statistics(database_url: str) -> None:
    # 7 October 2026: statistics taken before the Reset said no upload kept its
    # spool copy, the Reset then left 4,287 kept, and the planner rescanned all
    # 66,859 response-state rows for each one. Spool cleanup took 60 s instead
    # of 0.7 s, and each restarted collector spent its life waiting on it.
    with upload_database(database_url) as connection_info:
        _archive_instance(connection_info)
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                "ALTER TABLE collector_response_uploads SET (autovacuum_enabled = off)"
            )
            connection.execute(
                """
                WITH player AS (
                    INSERT INTO players (normalized_tag, active, next_due_at)
                    SELECT '#P' || n, true, %(now)s
                    FROM generate_series(1, 3000) AS n
                    RETURNING id, normalized_tag
                ), state AS (
                    INSERT INTO collector_response_state (
                        scope, identity_key, endpoint, player_id, normalized_tag,
                        last_response_hash, last_content_fingerprint,
                        last_occurrence_key, last_applied_occurrence_key,
                        last_seen_at
                    )
                    SELECT 'player', normalized_tag, 'profile', id, normalized_tag,
                           encode(sha256(normalized_tag::bytea), 'hex'),
                           encode(sha256(normalized_tag::bytea), 'hex'),
                           'stale-' || id, 'stale-' || id, %(now)s
                    FROM player
                    RETURNING last_response_hash
                )
                INSERT INTO collector_response_uploads (
                    response_hash, spool_key, byte_size, state,
                    archive_reference, archive_instance_id, completed_at,
                    local_deleted_at
                )
                SELECT last_response_hash, 'sha256/' || last_response_hash, 1,
                       'complete', 's3://evidence/' || last_response_hash,
                       'fixture-instance', %(now)s, %(now)s
                FROM state
                """,
                {"now": NOW},
            )
            connection.execute(
                "ANALYZE collector_response_uploads, collector_response_state"
            )
            kept = {
                row[0]
                for row in connection.execute(
                    """
                    UPDATE collector_response_uploads SET local_deleted_at = NULL
                    WHERE response_hash IN (
                        SELECT response_hash FROM collector_response_uploads
                        ORDER BY response_hash LIMIT 1000
                    )
                    RETURNING response_hash
                    """
                ).fetchall()
            }
            # Hash and merge joins cost about the same as that nested loop, so
            # which one stale statistics pick is luck. Forbid them: the read
            # must still have no plan that rescans a table per row.
            connection.execute("SET enable_hashjoin = off")
            connection.execute("SET enable_mergejoin = off")
            plan = connection.execute(
                "EXPLAIN (FORMAT JSON) " + REFERENCED_SPOOL_HASHES_SQL
            ).fetchone()[0][0]["Plan"]
        database = CollectorDatabase(connection_info)
        try:
            referenced = database.referenced_spool_hashes()
        finally:
            database.close()

    assert not _rescans_a_table(plan), plan
    assert referenced == kept
