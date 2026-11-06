"""Finding the saved bytes of a new response so the worker can process it."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from . import collector_uploads
from .archive import ArchiveReadError, ArchiveReadResult
from .collector_uploads import ArchivedCopy
from .db import Claim
from .spool import SpoolError


class Evidence(StrEnum):
    """Where a claimed new response's bytes stand before processing.

    Two states hand the bytes on. ``LOCAL``: the spool's copy is there and
    its hash matches. ``RECOVERED``: the spool's copy was gone, as after a
    lost disk or a database restored to before spool cleanup ran, and the
    archive's copy was read back, its hash checked and saved locally again.

    The rest raise ``ArchiveReadError`` for the job, whose queue state
    (``job_outcomes.fail_claim``) then decides what a failure means:

    - ``NO_LOCAL_READER``: this worker has no spool reader; fails for good.
    - ``LOCAL_READ_FAILED``: the disk read failed; waits as a dependency,
      spending no attempt.
    - ``NOT_ARCHIVED``: no archive location can hold a copy.
    - ``ARCHIVE_PENDING``: a copy the catalogue records, or one an upload may
      still write, could not be read yet, or the read failed another way;
      the archive's own error goes to the job.
    - ``ARCHIVE_UNAVAILABLE``: the upload's location was empty and the
      archive's identity marker could not be checked.
    - ``ARCHIVE_MISMATCH``: the location was empty and the marker names
      another archive.
    - ``NEVER_ARCHIVED``: the location was empty, no upload is in flight and
      the archive is the right one.

    So "missing" is never permanent at once: ``NOT_ARCHIVED`` and
    ``NEVER_ARCHIVED`` are retried within the job's attempts, and only its
    last records the response as lost proof. Each retry, including after a
    restart, starts again from the spool.
    """

    LOCAL = "local"
    RECOVERED = "recovered"
    NO_LOCAL_READER = "no_local_reader"
    LOCAL_READ_FAILED = "local_read_failed"
    NOT_ARCHIVED = "not_archived"
    ARCHIVE_PENDING = "archive_pending"
    ARCHIVE_UNAVAILABLE = "archive_unavailable"
    ARCHIVE_MISMATCH = "archive_mismatch"
    NEVER_ARCHIVED = "never_archived"


# The job failure each state that raises its own error records.
FAILURES: dict[Evidence, tuple[str, str, bool]] = {
    Evidence.NO_LOCAL_READER: (
        "spool_missing", "new observation has no local spool reader", False),
    Evidence.LOCAL_READ_FAILED: ("spool_io_failed", "local evidence read failed", True),
    Evidence.NOT_ARCHIVED: (
        "spool_missing", "new observation is missing from the local spool and the archive",
        True),
    Evidence.ARCHIVE_UNAVAILABLE: (
        "archive_unavailable", "archive marker could not be checked", True),
    Evidence.ARCHIVE_MISMATCH: (
        "archive_marker_mismatch", "archive marker does not match its configured hash", True),
    Evidence.NEVER_ARCHIVED: (
        "spool_missing",
        "new observation is missing from the local spool and was never archived", True),
}


def _failure(state: Evidence) -> ArchiveReadError:
    category, message, retryable = FAILURES[state]
    return ArchiveReadError(category, message, retryable=retryable)


def unreadable_copy(
    error: ArchiveReadError, copy: ArchivedCopy, marker_health: Callable[[], str]
) -> Evidence:
    """Why an archived copy could not be read; checks the marker only when
    nothing else explains it."""
    if error.category != "archive_missing" or copy.recorded or copy.uploading:
        return Evidence.ARCHIVE_PENDING
    marker = marker_health()
    if marker == "degraded":
        return Evidence.ARCHIVE_UNAVAILABLE
    if marker == "terminal":
        return Evidence.ARCHIVE_MISMATCH
    return Evidence.NEVER_ARCHIVED


def read_saved_evidence(
    database: Any, archive: Any, claim: Claim, renew_lease: Callable[[], None]
) -> ArchiveReadResult:
    """The bytes of ``claim``'s response, ``LOCAL`` or ``RECOVERED``."""
    verify = getattr(getattr(archive, "spool", None), "verify", None)
    if not callable(verify):
        raise _failure(Evidence.NO_LOCAL_READER)
    try:
        body = verify(claim.response_hash)
    except (OSError, SpoolError) as error:
        # A failed disk read is not a missing response: wait and retry it
        # without spending an attempt. Database failures stay separate.
        raise _failure(Evidence.LOCAL_READ_FAILED) from error
    if body is not None:
        return ArchiveReadResult(
            body=body,
            reference=claim.archive_reference or "",
            sha256=claim.response_hash or "",
        )
    assert claim.response_hash is not None
    bucket = getattr(getattr(archive, "archive", None), "bucket", None)
    copy = (
        None
        if bucket is None
        else collector_uploads.archived_copy(database, claim.response_hash, bucket=bucket)
    )
    if copy is None:
        raise _failure(Evidence.NOT_ARCHIVED)
    renew_lease()
    try:
        return archive.read_verified(copy.reference, claim.response_hash, heartbeat=renew_lease)
    except ArchiveReadError as error:
        state = unreadable_copy(error, copy, archive.check_marker_health)
        if state is Evidence.ARCHIVE_PENDING:
            raise
        raise _failure(state) from error
