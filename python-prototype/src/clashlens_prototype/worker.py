from __future__ import annotations

from dataclasses import dataclass

from .archive import ArchiveReadError, S3ArchiveReader
from .db import PROCESSING_VERSION, Claim, Database, LeaseLost
from .profile import (
    ENDPOINT_VERSION,
    PARSER_VERSION,
    SCHEMA_VERSION,
    ProfileParseError,
    parse_profile,
)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    job_id: int
    outcome: str
    category: str | None = None


class ObservationProcessor:
    def __init__(self, database: Database, archive: S3ArchiveReader) -> None:
        self.database = database
        self.archive = archive

    def process_once(self, *, owner: str, lease_seconds: int = 30) -> ProcessResult | None:
        claim = self.database.claim_job(owner=owner, lease_seconds=lease_seconds)
        if claim is None:
            return None
        return self._process_claim(claim)

    def process_job(
        self,
        job_id: int,
        *,
        owner: str,
        lease_seconds: int = 30,
    ) -> ProcessResult | None:
        claim = self.database.claim_job(owner=owner, lease_seconds=lease_seconds, job_id=job_id)
        if claim is None:
            return None
        return self._process_claim(claim)

    def _process_claim(self, claim: Claim) -> ProcessResult:
        if claim.http_status < 200 or claim.http_status >= 300:
            try:
                self.database.complete_classified(claim, outcome="non_success")
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "classified", "non_success")
        if claim.endpoint != "profile":
            category = "unsupported_endpoint"
            try:
                self.database.fail_claim(
                    claim,
                    category=category,
                    detail="prototype accepts profile observations only",
                    retryable=False,
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "failed", category)

        supported_versions = (
            (claim.endpoint_version, ENDPOINT_VERSION, "unsupported_endpoint_version"),
            (claim.schema_version, SCHEMA_VERSION, "unsupported_schema_version"),
            (claim.parser_version, PARSER_VERSION, "unsupported_parser_version"),
            (claim.processing_version, PROCESSING_VERSION, "unsupported_processing_version"),
        )
        for actual, expected, category in supported_versions:
            if actual == expected:
                continue
            try:
                self.database.fail_claim(
                    claim,
                    category=category,
                    detail=f"prototype requires {expected}; received {actual}",
                    retryable=False,
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "failed", category)

        try:
            archived = self.archive.read_verified(claim.archive_reference, claim.response_hash)
        except ArchiveReadError as error:
            try:
                state = self.database.fail_claim(
                    claim,
                    category=error.category,
                    detail=str(error),
                    retryable=error.retryable,
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(
                claim.job_id,
                "retrying" if state == "waiting_retry" else "failed",
                error.category,
            )

        try:
            profile = parse_profile(
                archived.body,
                expected_tag=claim.normalized_tag,
                observed_at=claim.observed_at,
                endpoint_version=claim.endpoint_version,
            )
        except ProfileParseError as error:
            try:
                self.database.fail_claim(
                    claim,
                    category=error.category,
                    detail=str(error),
                    retryable=False,
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "failed", error.category)

        try:
            self.database.complete_profile(claim, profile)
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(claim.job_id, "processed")

    def process_until_idle(
        self,
        *,
        owner: str,
        max_jobs: int = 100,
        lease_seconds: int = 30,
    ) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for _ in range(max_jobs):
            result = self.process_once(owner=owner, lease_seconds=lease_seconds)
            if result is None:
                break
            results.append(result)
        return results
