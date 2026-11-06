"""Where a claimed response's bytes come from, and what each miss records."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clashlens import collector_uploads
from clashlens.archive import ArchiveReadError, ArchiveReadResult
from clashlens.collector_uploads import ArchivedCopy
from clashlens.evidence_recovery import (
    FAILURES,
    Evidence,
    read_saved_evidence,
    unreadable_copy,
)
from clashlens.spool import SpoolError

HASH = "0" * 64
CLAIM = SimpleNamespace(response_hash=HASH, archive_reference="s3://evidence/old")
BODY = b'{"tag":"#2PP"}'


class Spool:
    def __init__(self, body: bytes | None = None, error: Exception | None = None) -> None:
        self.body, self.error = body, error

    def verify(self, expected_hash: str) -> bytes | None:
        assert expected_hash == HASH
        if self.error is not None:
            raise self.error
        return self.body


class Archive:
    def __init__(
        self, spool: Spool | None, read_error: ArchiveReadError | None = None,
        marker: str = "ready",
    ) -> None:
        if spool is not None:
            self.spool = spool
        self.archive = SimpleNamespace(bucket="evidence")
        self.read_error, self.marker = read_error, marker
        self.reads: list[str] = []
        self.marker_checks = 0

    def read_verified(self, reference: str, expected_hash: str, *, heartbeat) -> ArchiveReadResult:
        heartbeat()
        self.reads.append(reference)
        if self.read_error is not None:
            raise self.read_error
        return ArchiveReadResult(BODY, reference, expected_hash)

    def check_marker_health(self) -> str:
        self.marker_checks += 1
        return self.marker


def _read(archive: Archive, copy: ArchivedCopy | None, monkeypatch) -> tuple[bytes, int]:
    renewals = []
    monkeypatch.setattr(collector_uploads, "archived_copy",
                        lambda _database, response_hash, *, bucket: copy)
    result = read_saved_evidence(object(), archive, CLAIM, lambda: renewals.append(1))
    return result.body, len(renewals)


def test_a_local_copy_whose_hash_matches_is_read_without_the_archive(monkeypatch) -> None:
    archive = Archive(Spool(BODY))
    assert _read(archive, None, monkeypatch) == (BODY, 0)
    assert archive.reads == []


def test_a_lost_local_copy_is_recovered_from_the_archive(monkeypatch) -> None:
    archive = Archive(Spool(None))
    copy = ArchivedCopy("s3://evidence/copy", recorded=True, uploading=False)
    # The lease is renewed before the read and on each of its retries.
    assert _read(archive, copy, monkeypatch) == (BODY, 2)
    assert archive.reads == ["s3://evidence/copy"]


MISSING = ArchiveReadError("archive_missing", "no object", retryable=True)


@pytest.mark.parametrize(
    ("spool", "copy", "read_error", "marker", "state"),
    [
        (None, None, None, "ready", Evidence.NO_LOCAL_READER),
        (Spool(error=OSError("disk")), None, None, "ready", Evidence.LOCAL_READ_FAILED),
        (Spool(error=SpoolError("bad")), None, None, "ready", Evidence.LOCAL_READ_FAILED),
        (Spool(None), None, None, "ready", Evidence.NOT_ARCHIVED),
        (Spool(None), ArchivedCopy("s3://e/c", False, False), MISSING, "degraded",
         Evidence.ARCHIVE_UNAVAILABLE),
        (Spool(None), ArchivedCopy("s3://e/c", False, False), MISSING, "terminal",
         Evidence.ARCHIVE_MISMATCH),
        (Spool(None), ArchivedCopy("s3://e/c", False, False), MISSING, "ready",
         Evidence.NEVER_ARCHIVED),
    ],
)
def test_each_missing_state_records_its_own_failure(
    spool, copy, read_error, marker, state, monkeypatch
) -> None:
    archive = Archive(spool, read_error, marker)
    with pytest.raises(ArchiveReadError) as raised:
        _read(archive, copy, monkeypatch)
    category, message, retryable = FAILURES[state]
    assert (raised.value.category, str(raised.value), raised.value.retryable) == (
        category, f"{category}: {message}", retryable)
    # Only a lost local copy with no reader fails for good at once; every
    # other miss is retried within the job's attempts or waited out.
    assert retryable is (state is not Evidence.NO_LOCAL_READER)


@pytest.mark.parametrize(
    ("copy", "error"),
    [
        # Recorded in the catalogue but not found yet.
        (ArchivedCopy("s3://e/c", True, False), MISSING),
        # An upload holds a lease, or its last write may yet land.
        (ArchivedCopy("s3://e/c", False, True), MISSING),
        # The archive could not be read at all.
        (ArchivedCopy("s3://e/c", False, False),
         ArchiveReadError("archive_unavailable", "S3 read failed", retryable=True)),
    ],
)
def test_a_copy_that_may_still_arrive_keeps_the_archives_own_error(
    copy, error, monkeypatch
) -> None:
    archive = Archive(Spool(None), error)
    with pytest.raises(ArchiveReadError) as raised:
        _read(archive, copy, monkeypatch)
    assert raised.value is error
    assert unreadable_copy(error, copy, archive.check_marker_health) is Evidence.ARCHIVE_PENDING
    assert archive.marker_checks == 0


def test_a_retry_after_the_local_copy_returns_reads_it_locally(monkeypatch) -> None:
    spool = Spool(None)
    archive = Archive(spool, MISSING)
    copy = ArchivedCopy("s3://e/c", False, False)
    with pytest.raises(ArchiveReadError, match="never archived"):
        _read(archive, copy, monkeypatch)
    spool.body = BODY  # a restored disk, before the job's next attempt
    assert _read(archive, copy, monkeypatch) == (BODY, 0)
