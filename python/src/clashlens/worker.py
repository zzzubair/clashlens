from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from .archive import ArchiveReadError, S3ArchiveReader
from .battle import (
    BATTLE_LOG_ENDPOINT_VERSION,
    BATTLE_LOG_SCHEMA_VERSION,
    SUPPORTED_SOURCE_PARSER_VERSIONS,
    BattleLogParseError,
    parse_battle_log,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    LeaseLost,
)
from .profile import (
    ENDPOINT_VERSION,
    SCHEMA_VERSION,
    ProfileParseError,
    parse_profile,
)
from .rankings import (
    GLOBAL_RANKING_ENDPOINT_VERSION,
    GLOBAL_RANKING_SCHEMA_VERSION,
    RankingParseError,
    parse_global_player_rankings,
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

    def process_once(
        self, *, owner: str, lease_seconds: int = 30
    ) -> ProcessResult | None:
        claim = self.database.claim_job(owner=owner, lease_seconds=lease_seconds)
        if claim is None:
            return None
        return self._process_claim(claim, lease_seconds=lease_seconds)

    def process_job(
        self,
        job_id: int,
        *,
        owner: str,
        lease_seconds: int = 30,
    ) -> ProcessResult | None:
        claim = self.database.claim_job(
            owner=owner, lease_seconds=lease_seconds, job_id=job_id
        )
        if claim is None:
            return None
        return self._process_claim(claim, lease_seconds=lease_seconds)

    def _process_claim(self, claim: Claim, *, lease_seconds: int) -> ProcessResult:
        if claim.work_type == "reconcile_ranked_day":
            if claim.processing_version != PROCESSING_VERSION:
                return self._fail(
                    claim, "unsupported_processing_version", retryable=False
                )
            if claim.domain_rule_version != DOMAIN_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_domain_rule_version", retryable=False
                )
            try:
                self.database.renew_claim(claim, lease_seconds=lease_seconds)
                self.database.complete_reconciliation(claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "processed")
        if claim.work_type in {"build_snapshot", "build_analytics"}:
            if claim.processing_version != PROCESSING_VERSION:
                return self._fail(
                    claim, "unsupported_processing_version", retryable=False
                )
            if claim.domain_rule_version != DOMAIN_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_domain_rule_version", retryable=False
                )
            if claim.analytics_rule_version != ANALYTICS_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_analytics_rule_version", retryable=False
                )
            try:
                self.database.renew_claim(claim, lease_seconds=lease_seconds)
                if claim.work_type == "build_snapshot":
                    self.database.complete_snapshot(claim)
                else:
                    self.database.complete_analytics(claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            except (KeyError, TypeError, ValueError) as error:
                return self._fail(
                    claim,
                    "dependency_not_ready" if "dependency" in str(error) else "invalid_work_input",
                    detail=str(error),
                    retryable=False,
                )
            return ProcessResult(claim.job_id, "processed")
        if claim.work_type not in {"process_observation", "replay_observation"}:
            return self._fail(claim, "unsupported_work_type", retryable=False)
        endpoint_contracts = {
            "profile": (ENDPOINT_VERSION, SCHEMA_VERSION),
            "battle_log": (BATTLE_LOG_ENDPOINT_VERSION, BATTLE_LOG_SCHEMA_VERSION),
            "global_player_rankings": (
                GLOBAL_RANKING_ENDPOINT_VERSION,
                GLOBAL_RANKING_SCHEMA_VERSION,
            ),
        }
        if claim.endpoint not in endpoint_contracts:
            return self._fail(claim, "unsupported_endpoint", retryable=False)
        endpoint_version, schema_version = endpoint_contracts[claim.endpoint]
        checks = (
            (
                claim.endpoint_version == endpoint_version,
                "unsupported_endpoint_version",
            ),
            (claim.schema_version == schema_version, "unsupported_schema_version"),
            (
                claim.parser_version in SUPPORTED_SOURCE_PARSER_VERSIONS,
                "unsupported_parser_version",
            ),
            (
                claim.processing_version == PROCESSING_VERSION,
                "unsupported_processing_version",
            ),
            (
                claim.domain_rule_version == DOMAIN_RULE_VERSION,
                "unsupported_domain_rule_version",
            ),
        )
        for valid, category in checks:
            if not valid:
                return self._fail(claim, category, retryable=False)
        assert claim.endpoint_version is not None

        if claim.archive_reference is None or claim.response_hash is None:
            return self._fail(claim, "missing_archive_metadata", retryable=False)

        try:
            self.database.renew_claim(claim, lease_seconds=lease_seconds)
            archived = self.archive.read_verified(
                claim.archive_reference, claim.response_hash
            )
            self.database.renew_claim(claim, lease_seconds=lease_seconds)
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
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")

        if claim.http_status is None:
            return self._fail(claim, "missing_http_status", retryable=False)
        if claim.http_status < 200 or claim.http_status >= 300:
            try:
                self.database.complete_classified(claim, outcome="source_non_success")
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "classified", "non_success")

        if claim.observed_at is None:
            return self._fail(claim, "missing_observation_time", retryable=False)

        try:
            if claim.endpoint == "profile":
                if claim.normalized_tag is None or claim.endpoint_version is None:
                    return self._fail(claim, "missing_player_scope", retryable=False)
                profile = parse_profile(
                    archived.body,
                    expected_tag=claim.normalized_tag,
                    observed_at=claim.observed_at,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self.database.complete_profile(claim, profile)
                outcome = "processed"
            elif claim.endpoint == "battle_log":
                if claim.normalized_tag is None or claim.endpoint_version is None:
                    return self._fail(claim, "missing_player_scope", retryable=False)
                battle_log = parse_battle_log(
                    archived.body,
                    expected_tag=claim.normalized_tag,
                    observed_at=claim.observed_at,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self.database.complete_battle_log(claim, battle_log)
                outcome = (
                    "processed_with_gaps" if battle_log.has_row_gap else "processed"
                )
            else:
                rankings = parse_global_player_rankings(
                    archived.body,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self.database.complete_rankings(claim, rankings)
                outcome = "processed"
        except (ProfileParseError, BattleLogParseError, RankingParseError) as error:
            return self._fail(claim, error.category, detail=str(error), retryable=False)
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(claim.job_id, outcome)

    def _fail(
        self,
        claim: Claim,
        category: str,
        *,
        detail: str | None = None,
        retryable: bool,
    ) -> ProcessResult:
        try:
            state = self.database.fail_claim(
                claim,
                category=category,
                detail=detail or category,
                retryable=retryable,
            )
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(
            claim.job_id,
            "retrying" if state == "waiting_retry" else "failed",
            category,
        )

    def process_until_idle(
        self,
        *,
        owner: str,
        max_jobs: int = 100,
        lease_seconds: int = 30,
        stop_requested: Event | None = None,
    ) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for _ in range(max_jobs):
            if stop_requested is not None and stop_requested.is_set():
                break
            result = self.process_once(owner=owner, lease_seconds=lease_seconds)
            if result is None:
                break
            results.append(result)
        return results
