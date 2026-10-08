"""Leased archive-upload state for the Python collector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from .archive import immutable_reference


@dataclass(frozen=True, slots=True)
class UploadClaim:
    response_hash: str
    spool_key: str
    byte_size: int
    owner: str
    token: str
    lease_expires_at: datetime
    attempt_count: int
    # Non-empty when retired bytes were seen again: the upload must use the
    # hash's generation suffix so the tombstoned location stays untouched.
    generation: str = ""
    # The previous attempt's write may yet land; see WRITE_UNRESOLVED_SQL.
    write_unresolved: bool = False


class UploadLeaseLost(RuntimeError):
    """The upload claim is stale, expired, or owned by another uploader."""


@dataclass(frozen=True, slots=True)
class ArchivedCopy:
    reference: str
    # True when the catalogue records a verified copy there. False for the
    # upload's own location, which only bytes found there confirm.
    recorded: bool
    # An upload of these bytes holds a lease right now, or its last write may
    # yet land.
    uploading: bool


# Uploads retry at least 5 seconds apart, so a write that may yet land is
# waited for through at least two and a half minutes of attempts.
UNRESOLVED_WRITE_ATTEMPTS = 30


# The last attempt's lease ran out, or it failed in a way that may pass, so a
# write an earlier attempt made may still land: until the upload has made
# UNRESOLVED_WRITE_ATTEMPTS attempts, a missing copy is not yet proof the
# archive lacks these bytes.
WRITE_UNRESOLVED_SQL = f"""
    (((upload.state = 'failed' AND upload.last_error_retryable IS TRUE)
      OR (upload.state = 'pending' AND upload.attempt_count > 0))
     AND upload.attempt_count < {UNRESOLVED_WRITE_ATTEMPTS})
"""


# collector_response_uploads_claim_order matches this filter and order, so
# claims can stop at the first due row they can lock without sorting the backlog.
NEXT_DUE_UPLOAD_SQL = f"""
    SELECT upload.response_hash, {WRITE_UNRESOLVED_SQL}
    FROM collector_response_uploads AS upload
    WHERE state IN ('pending', 'failed')
      AND next_attempt_at <= %s
    ORDER BY next_attempt_at, created_at, response_hash
    FOR UPDATE SKIP LOCKED
    LIMIT 1
"""


# collector_response_uploads_lease_expiry (migration 0081) holds only leased
# rows, so this reads those, not the whole table.
RELEASE_EXPIRED_UPLOADS_SQL = """
    UPDATE collector_response_uploads
    SET state = 'pending', lease_owner = NULL, lease_token = NULL,
        lease_expires_at = NULL, updated_at = clock_timestamp()
    WHERE response_hash IN (
        SELECT response_hash
        FROM collector_response_uploads
        WHERE state = 'leased'
          AND lease_expires_at <= COALESCE(%s::timestamptz, clock_timestamp())
        ORDER BY lease_expires_at
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
"""


def release_expired_uploads(
    database: Any, *, limit: int = 1000, now: datetime | None = None
) -> int:
    """Return up to ``limit`` uploads whose lease ran out to pending."""
    if limit < 1:
        raise ValueError("release limit must be positive")
    with database.pool.connection() as connection:
        released = connection.execute(RELEASE_EXPIRED_UPLOADS_SQL, (now, limit))
    return released.rowcount


def claim_upload(
    database: Any,
    *,
    owner: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> UploadClaim | None:
    """Lease the next due upload."""
    if not owner or lease_seconds < 1:
        raise ValueError("upload owner and positive lease are required")
    token = str(uuid4())
    with database.pool.connection() as connection:
        with connection.transaction():
            claim_time = (
                now or connection.execute("SELECT clock_timestamp()").fetchone()[0]
            )
            expires = claim_time + timedelta(seconds=lease_seconds)
            row = connection.execute(NEXT_DUE_UPLOAD_SQL, (claim_time,)).fetchone()
            if row is None:
                return None
            claimed = connection.execute(
                """
                UPDATE collector_response_uploads
                SET state = 'leased', lease_owner = %s, lease_token = %s,
                    lease_expires_at = %s, attempt_count = attempt_count + 1,
                    settled_lease_token = NULL, last_error_retryable = NULL,
                    upload_generation = CASE
                        WHEN upload_generation = '' AND EXISTS (
                            SELECT 1 FROM archive_catalogue AS catalogue
                            WHERE catalogue.response_hash = %s
                              AND catalogue.availability <> 'verified'
                              AND position('/generation/' IN catalogue.archive_reference) = 0
                        ) THEN %s
                        ELSE upload_generation
                    END,
                    updated_at = clock_timestamp()
                WHERE response_hash = %s
                RETURNING response_hash, spool_key, byte_size, lease_expires_at,
                          attempt_count, upload_generation
                """,
                (owner, token, expires, row[0], uuid4().hex, row[0]),
            ).fetchone()
            assert claimed is not None
    return UploadClaim(
        str(claimed[0]),
        str(claimed[1]),
        int(claimed[2]),
        owner,
        token,
        claimed[3],
        int(claimed[4]),
        str(claimed[5]),
        bool(row[1]),
    )


def renew_upload(
    database: Any,
    claim: UploadClaim,
    *,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> datetime:
    if lease_seconds < 1:
        raise ValueError("positive upload lease is required")
    with database.pool.connection() as connection:
        renewed = connection.execute(
            """
            WITH tick AS (
                SELECT COALESCE(%s::timestamptz, clock_timestamp()) AS at
            )
            UPDATE collector_response_uploads AS upload
            SET lease_expires_at = tick.at + (%s * interval '1 second'),
                updated_at = clock_timestamp()
            FROM tick
            WHERE upload.response_hash = %s
              AND upload.state = 'leased'
              AND upload.lease_owner = %s
              AND upload.lease_token = %s
              AND upload.lease_expires_at > tick.at
            RETURNING upload.lease_expires_at
            """,
            (
                now,
                lease_seconds,
                claim.response_hash,
                claim.owner,
                claim.token,
            ),
        ).fetchone()
    if renewed is None:
        raise UploadLeaseLost("upload lease lost")
    return renewed[0]


def _claim_matches(row: tuple[Any, ...], claim: UploadClaim) -> bool:
    return (
        str(row[0]) == claim.response_hash
        and str(row[1]) == claim.spool_key
        and int(row[2]) == claim.byte_size
        and int(row[7]) == claim.attempt_count
        and str(row[8]) == claim.generation
    )


def _settled_by_claim(row: tuple[Any, ...], claim: UploadClaim) -> bool:
    try:
        token = UUID(claim.token)
    except ValueError:
        return False
    return row[10] is not None and row[10] == token


def _lock_upload(connection: Any, response_hash: str) -> tuple[Any, ...] | None:
    return connection.execute(
        """
        SELECT response_hash, spool_key, byte_size, state, lease_owner,
               lease_token, lease_expires_at, attempt_count,
               upload_generation,
               latest_sighting_at,
               settled_lease_token,
               archive_reference, archive_instance_id, completed_at,
               last_error_category, last_error_detail, last_error_retryable
        FROM collector_response_uploads
        WHERE response_hash = %s
        FOR UPDATE
        """,
        (response_hash,),
    ).fetchone()


def _require_live(
    row: tuple[Any, ...] | None,
    claim: UploadClaim,
    *,
    owner: str,
    now: datetime,
) -> tuple[Any, ...]:
    if (
        row is None
        or not _claim_matches(row, claim)
        or row[3] != "leased"
        or row[4] != owner
        or row[5] != claim.token
        or row[6] <= now
    ):
        raise UploadLeaseLost("upload lease lost")
    return row


def complete_upload(
    database: Any,
    claim: UploadClaim,
    *,
    archive_reference: str,
    archive_instance_id: str,
    owner: str | None = None,
    now: datetime | None = None,
) -> None:
    if not archive_reference or not archive_instance_id:
        raise ValueError("archive identity is required")
    with database.pool.connection() as connection:
        with connection.transaction():
            complete_time = (
                now or connection.execute("SELECT clock_timestamp()").fetchone()[0]
            )
            row = _lock_upload(connection, claim.response_hash)
            if (
                row is not None
                and _claim_matches(row, claim)
                and row[3] == "complete"
                and _settled_by_claim(row, claim)
                and row[11] == archive_reference
                and row[12] == archive_instance_id
                and row[13] is not None
            ):
                return
            row = _require_live(
                row, claim, owner=owner or claim.owner, now=complete_time
            )
            existing = connection.execute(
                """
                SELECT response_hash, byte_size, archive_instance_id, availability
                FROM archive_catalogue
                WHERE archive_reference = %s
                FOR UPDATE
                """,
                (archive_reference,),
            ).fetchone()
            if existing is not None and (
                existing[0] != claim.response_hash
                or int(existing[1]) != claim.byte_size
                or existing[2] != archive_instance_id
            ):
                raise ValueError(
                    "archive reference is already bound to different bytes"
                )
            if existing is not None and existing[3] != "verified":
                connection.execute(
                    """
                    UPDATE collector_response_uploads
                    SET state = 'pending', upload_generation = %s,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE response_hash = %s
                    """,
                    (uuid4().hex, claim.response_hash),
                )
                return
            if existing is not None:
                connection.execute(
                    """
                    UPDATE archive_catalogue
                    SET retire_after = clashlens_season_retire_after(%s)
                    WHERE archive_reference = %s
                      AND retire_after < clashlens_season_retire_after(%s)
                    """,
                    (row[9], archive_reference, row[9]),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO archive_catalogue (
                        response_hash, archive_reference, byte_size,
                        archive_instance_id, first_verified_at, retire_after
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        clashlens_season_retire_after(%s)
                    )
                    """,
                    (
                        claim.response_hash,
                        archive_reference,
                        claim.byte_size,
                        archive_instance_id,
                        complete_time,
                        row[9],
                    ),
                )
            connection.execute(
                """
                UPDATE collector_observations
                SET archive_reference = %s,
                    archive_catalogue_hash = response_hash
                WHERE response_hash = %s AND archive_reference IS NULL
                """,
                (archive_reference, claim.response_hash),
            )
            completed = connection.execute(
                """
                UPDATE collector_response_uploads
                SET state = 'complete', archive_reference = %s,
                    archive_instance_id = %s, completed_at = %s,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, settled_lease_token = %s::uuid,
                    updated_at = clock_timestamp()
                WHERE response_hash = %s AND state = 'leased'
                  AND lease_owner = %s AND lease_token = %s
                  AND lease_expires_at >
                      COALESCE(%s::timestamptz, clock_timestamp())
                """,
                (
                    archive_reference,
                    archive_instance_id,
                    complete_time,
                    claim.token,
                    claim.response_hash,
                    owner or claim.owner,
                    claim.token,
                    now,
                ),
            )
            if completed.rowcount != 1:
                raise UploadLeaseLost("upload lease lost")


def fail_upload(
    database: Any,
    claim: UploadClaim,
    *,
    category: str,
    detail: str | None = None,
    retryable: bool = True,
    owner: str | None = None,
    now: datetime | None = None,
) -> None:
    stored_category = category[:128]
    stored_detail = (detail or "")[:1024]
    with database.pool.connection() as connection:
        with connection.transaction():
            fail_time = (
                now or connection.execute("SELECT clock_timestamp()").fetchone()[0]
            )
            row = _lock_upload(connection, claim.response_hash)
            if (
                row is not None
                and _claim_matches(row, claim)
                and row[3] == "failed"
                and _settled_by_claim(row, claim)
                and row[14] == stored_category
                and row[15] == stored_detail
                and row[16] == retryable
            ):
                return
            _require_live(row, claim, owner=owner or claim.owner, now=fail_time)
            failed = connection.execute(
                """
                UPDATE collector_response_uploads
                SET state = 'failed', next_attempt_at = CASE WHEN %s
                        THEN %s + interval '5 seconds' ELSE 'infinity'::timestamptz END,
                    last_error_category = %s, last_error_detail = %s,
                    last_error_retryable = %s,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, settled_lease_token = %s::uuid,
                    updated_at = clock_timestamp()
                WHERE response_hash = %s AND state = 'leased'
                  AND lease_owner = %s AND lease_token = %s
                  AND lease_expires_at >
                      COALESCE(%s::timestamptz, clock_timestamp())
                """,
                (
                    retryable,
                    fail_time,
                    stored_category,
                    stored_detail,
                    retryable,
                    claim.token,
                    claim.response_hash,
                    owner or claim.owner,
                    claim.token,
                    now,
                ),
            )
            if failed.rowcount != 1:
                raise UploadLeaseLost("upload lease lost")


def archived_copy(
    database: Any, response_hash: str, *, bucket: str
) -> ArchivedCopy | None:
    """Where the archive holds, or would hold, bytes whose saved copy is gone.

    A recorded verified copy comes first. Without one, a database restored to
    before an upload finished may not know an upload that did happen; its bytes
    are then at that upload's own location, unless the catalogue marks that
    location retired. Bytes uploaded again after a retirement went to a new
    location this database may never have recorded, so none is guessed. None
    means the archive cannot hold a copy.
    """
    with database.pool.connection() as connection:
        verified, generation, state, unresolved = connection.execute(
            f"""
            SELECT verified.archive_reference, upload.upload_generation,
                   upload.state, {WRITE_UNRESOLVED_SQL}
            FROM (SELECT 1) AS one
            LEFT JOIN LATERAL (
                SELECT archive_reference
                FROM archive_catalogue
                WHERE response_hash = %(hash)s AND availability = 'verified'
                ORDER BY first_verified_at DESC
                LIMIT 1
            ) AS verified ON true
            LEFT JOIN collector_response_uploads AS upload
              ON upload.response_hash = %(hash)s
            """,
            {"hash": response_hash},
        ).fetchone()
        uploading = state == "leased" or bool(unresolved)
        if verified is not None:
            return ArchivedCopy(str(verified), True, uploading)
        if state is None:
            return None
        reference = immutable_reference(bucket, response_hash, generation or None)
        catalogued = connection.execute(
            "SELECT availability FROM archive_catalogue WHERE archive_reference = %s",
            (reference,),
        ).fetchone()
    if catalogued is None:
        return ArchivedCopy(reference, False, uploading)
    # An upload that finished after the first read recorded this copy.
    if catalogued[0] == "verified":
        return ArchivedCopy(reference, True, uploading)
    return None
