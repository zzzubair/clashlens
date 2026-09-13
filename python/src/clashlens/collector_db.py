"""Database seam for the Python collector and its durable spool handoff."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

PROCESSING_VERSION = "clashlens-domain-processing-v1"
DOMAIN_RULE_VERSION = "clashlens-domain-rules-v1"
ANALYTICS_RULE_VERSION = "legend-analytics-v1"
PROFILE_PARSER_VERSION = "supercell-profile-parser-v3"
SOURCE_PARSER_VERSION = "supercell-source-parser-v2"
REVISIT_INTERVAL = timedelta(minutes=5)
UPLOAD_RETRY_DELAY = timedelta(seconds=5)
_ENDPOINTS = {"profile", "battle_log", "global_player_rankings"}
_PLAYER_ENDPOINTS = {"profile", "battle_log"}


@dataclass(frozen=True, slots=True)
class CollectorWork:
    player_id: int | None
    normalized_tag: str
    due_at: datetime
    collector_work_id: int | None = None


@dataclass(frozen=True, slots=True)
class CollectorIntent:
    kind: str
    cycle_at: datetime
    player_id: int | None
    normalized_tag: str | None
    work_id: int | None = None
    due_at: datetime | None = None
    status: str | None = None
    sweep_id: int | None = None


@dataclass(frozen=True, slots=True)
class ResponseHandoff:
    """Metadata for one locally published response and its sidecar key."""

    occurrence_key: str
    scope: str
    identity_key: str
    endpoint: str
    player_id: int | None
    normalized_tag: str | None
    request_started_at: datetime
    response_completed_at: datetime
    http_status: int
    response_hash: str
    byte_size: int
    spool_key: str
    collector_version: str
    key_label: str
    evidence_headers: Mapping[str, Any] = field(default_factory=dict)
    collector_work_id: int | None = None


@dataclass(frozen=True, slots=True)
class ResponseResult:
    changed: bool
    observation_id: int | None
    processing_job_id: int | None
    upload_id: str | None
    parser_version: str


@dataclass(frozen=True, slots=True)
class UploadClaim:
    response_hash: str
    spool_key: str
    byte_size: int
    owner: str
    token: str
    lease_expires_at: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class CollectorPermit:
    granted: bool
    reason: str
    next_eligible_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TransportFailure:
    occurrence_key: str
    scope: str
    identity_key: str
    endpoint: str
    player_id: int | None
    normalized_tag: str | None
    request_started_at: datetime
    failed_at: datetime
    failure_category: str
    retry_state: str
    key_label: str


class CollectorDatabase:
    """Synchronous DB interface for calls made from collector threads."""

    def __init__(self, database_url: str, *, max_size: int = 32) -> None:
        if not 1 <= max_size <= 64:
            raise ValueError("collector database pool size must be between 1 and 64")
        self.database_url = database_url
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=max_size,
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    def _connection(self) -> Any:
        return self.pool.connection()

    def validate_archive_instance(self, config: Any) -> bool:
        names = (
            "instance_id",
            "endpoint",
            "region",
            "bucket",
            "marker_key",
            "marker_hash",
            "marker_payload_version",
        )
        values = tuple(
            config.get(name)
            if isinstance(config, Mapping)
            else getattr(config, name, None)
            for name in names
        )
        if not all(isinstance(value, str) and value for value in values):
            return False
        with self._connection() as connection:
            row = connection.execute(
                "SELECT endpoint, region, bucket, marker_key, marker_hash, marker_payload_version FROM archive_instances WHERE instance_id = %s",
                (values[0],),
            ).fetchone()
        return row is not None and tuple(row) == values[1:]

    def register_interactive_key(self, fingerprint: str) -> None:
        self._validate_hash(fingerprint)
        with self._connection() as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO shared_api_credentials (credential_fingerprint) VALUES (%s) ON CONFLICT (credential_fingerprint) DO NOTHING",
                    (fingerprint,),
                )
                row = connection.execute(
                    "SELECT collector_budget, python_budget, total_budget FROM shared_api_credentials WHERE credential_fingerprint = %s FOR UPDATE",
                    (fingerprint,),
                ).fetchone()
                if row is None or tuple(map(int, row)) != (29, 1, 30):
                    raise RuntimeError(
                        "conflicting interactive credential registration"
                    )

    def quarantine_interactive_key(self, fingerprint: str, *, reason: str) -> None:
        self._validate_hash(fingerprint)
        if not reason or len(reason) > 256 or "\n" in reason or "\r" in reason:
            raise ValueError("interactive quarantine reason is invalid")
        with self._connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    """
                    UPDATE shared_api_credentials
                    SET state = 'quarantined', cooldown_until = NULL,
                        quarantine_reason = %s, updated_at = clock_timestamp()
                    WHERE credential_fingerprint = %s
                    RETURNING credential_fingerprint
                    """,
                    (reason, fingerprint),
                ).fetchone()
                if row is None:
                    raise RuntimeError("interactive credential is not registered")
                connection.execute(
                    """
                    INSERT INTO shared_api_credential_events (
                        credential_fingerprint, event_type, actor, reason
                    ) VALUES (%s, 'quarantined', 'python-collector', %s)
                    """,
                    (fingerprint, reason),
                )

    def acquire_collector_permit(
        self, fingerprint: str, *, wait: bool = False, timeout_seconds: float = 1.0
    ) -> CollectorPermit:
        self._validate_hash(fingerprint)
        if timeout_seconds < 0:
            raise ValueError("permit wait timeout cannot be negative")
        with self._connection() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM shared_api_credentials WHERE credential_fingerprint = %s",
                    (fingerprint,),
                ).fetchone()
                is None
            ):
                return CollectorPermit(False, "credential_unknown")
        deadline = monotonic() + timeout_seconds
        while True:
            with self._connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        "SELECT granted, next_eligible_at, credential_state FROM clashlens_acquire_shared_api_permit(%s, 'collector')",
                        (fingerprint,),
                    ).fetchone()
                    if row is None:
                        result = CollectorPermit(False, "credential_unknown")
                    else:
                        state = str(row[2])
                        reason = (
                            "granted"
                            if row[0]
                            else {
                                "cooldown": "credential_cooldown",
                                "quarantined": "credential_quarantined",
                                "retired": "credential_inactive",
                            }.get(state, "collector_budget_exhausted")
                        )
                        result = CollectorPermit(bool(row[0]), reason, row[1])
            if (
                result.granted
                or not wait
                or result.reason
                in {
                    "credential_unknown",
                    "credential_quarantined",
                    "credential_inactive",
                }
                or monotonic() >= deadline
            ):
                return result
            delay = 0.05
            if result.next_eligible_at is not None:
                delay = max(
                    0.01, (result.next_eligible_at - datetime.now(UTC)).total_seconds()
                )
            sleep(min(delay, max(0.01, deadline - monotonic())))

    def wait_for_collector_permit(
        self, fingerprint: str, *, timeout_seconds: float = 1.0
    ) -> CollectorPermit:
        return self.acquire_collector_permit(
            fingerprint, wait=True, timeout_seconds=timeout_seconds
        )

    @staticmethod
    def _parser_for(endpoint: str) -> str:
        if endpoint == "profile":
            return PROFILE_PARSER_VERSION
        if endpoint in {"battle_log", "global_player_rankings"}:
            return SOURCE_PARSER_VERSION
        raise ValueError(f"unsupported endpoint: {endpoint}")

    @staticmethod
    def _validate_hash(response_hash: str) -> None:
        if len(response_hash) != 64 or any(
            character not in "0123456789abcdef" for character in response_hash
        ):
            raise ValueError("response hash must be a lowercase SHA-256 digest")

    @classmethod
    def _validate_handoff(cls, handoff: ResponseHandoff) -> None:
        cls._validate_hash(handoff.response_hash)
        if not 1 <= len(handoff.occurrence_key) <= 450:
            raise ValueError("occurrence key must contain 1 to 450 characters")
        if handoff.scope not in {"player", "global"}:
            raise ValueError("unsupported response scope")
        if handoff.endpoint not in _ENDPOINTS:
            raise ValueError("unsupported response endpoint")
        if handoff.byte_size < 0:
            raise ValueError("response byte size cannot be negative")
        if not handoff.spool_key or len(handoff.spool_key) > 1024:
            raise ValueError(
                "spool key is required and must be at most 1024 characters"
            )
        if not handoff.collector_version or not handoff.key_label:
            raise ValueError("collector version and key label are required")
        if handoff.collector_work_id is not None and handoff.collector_work_id < 1:
            raise ValueError("collector work identity must be positive")
        if handoff.scope == "player":
            if handoff.endpoint not in _PLAYER_ENDPOINTS:
                raise ValueError("player scope only supports player endpoints")
            if handoff.player_id is None or handoff.normalized_tag is None:
                raise ValueError("player responses require player identity")
            if handoff.identity_key != handoff.normalized_tag:
                raise ValueError("player identity key must equal normalized tag")
        elif (
            handoff.endpoint != "global_player_rankings"
            or handoff.player_id is not None
            or handoff.normalized_tag is not None
            or handoff.identity_key != "global"
        ):
            raise ValueError("global response identity is invalid")

    @staticmethod
    def _path_for(handoff: ResponseHandoff) -> tuple[str, str, str, str]:
        if handoff.endpoint == "global_player_rankings":
            return (
                "GET",
                "/v1/locations/global/rankings/players",
                "limit=200",
                "malformed",
            )
        assert handoff.normalized_tag is not None
        path = "/v1/players/%23" + handoff.normalized_tag[1:]
        if handoff.endpoint == "battle_log":
            path += "/battlelog"
        return "GET", path, "", "not_applicable"

    @staticmethod
    def _validate_work_identity(
        connection: Any, handoff: ResponseHandoff
    ) -> str | None:
        if handoff.collector_work_id is None:
            return None
        row = connection.execute(
            """
            SELECT work.kind, work.player_id, work.normalized_tag, sweep.boundary_at
            FROM collector_work AS work
            LEFT JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
            WHERE work.id = %s
            """,
            (handoff.collector_work_id,),
        ).fetchone()
        allowed_endpoints = {
            "discovery_profile": {"profile"},
            "global_player_rankings": {"global_player_rankings"},
        }.get(None if row is None else str(row[0]), _PLAYER_ENDPOINTS)
        if (
            row is None
            or handoff.endpoint not in allowed_endpoints
            or row[1] != handoff.player_id
            or row[2] != handoff.normalized_tag
            or (row[0] == "reset_baseline" and handoff.response_completed_at < row[3])
        ):
            raise ValueError("collector work identity does not match response")
        return str(row[0])

    def claim_due_players(
        self, limit: int, now: datetime | None = None
    ) -> list[CollectorWork]:
        if limit < 1:
            raise ValueError("player claim limit must be positive")
        claim_time = now or datetime.now(UTC)
        with self._connection() as connection:
            with connection.transaction():
                if not self._regular_admission_open(connection, claim_time):
                    return []
                rows = connection.execute(
                    """
                    WITH due AS (
                        SELECT id, normalized_tag, next_due_at
                        FROM players
                        WHERE active = true
                          AND next_due_at IS NOT NULL
                          AND next_due_at <= %s
                        ORDER BY next_due_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE players AS player
                    SET next_due_at = %s + interval '5 minutes'
                    FROM due
                    WHERE player.id = due.id
                    RETURNING due.id, due.normalized_tag, due.next_due_at
                    """,
                    (claim_time, limit, claim_time),
                ).fetchall()
        return [CollectorWork(int(row[0]), str(row[1]), row[2]) for row in rows]

    @staticmethod
    def _regular_admission_open(connection: Any, now: datetime) -> bool:
        utc_now = now.astimezone(UTC)
        today_boundary = utc_now.replace(hour=5, minute=0, second=0, microsecond=0)
        if utc_now < today_boundary:
            boundary = today_boundary
            completed_boundary = today_boundary - timedelta(days=1)
        else:
            boundary = today_boundary
            completed_boundary = today_boundary
        if boundary - timedelta(minutes=5) <= utc_now < boundary:
            return False
        if (
            utc_now >= today_boundary
            and connection.execute(
                "SELECT 1 FROM collector_reset_sweeps WHERE boundary_at = %s",
                (completed_boundary,),
            ).fetchone()
            is None
        ):
            return False
        return not bool(
            connection.execute(
                """
                SELECT 1
                FROM collector_work AS work
                JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
                WHERE sweep.boundary_at <= %s
                  AND work.kind = 'reset_baseline'
                  AND work.status NOT IN ('complete', 'failed', 'cancelled')
                LIMIT 1
                """,
                (completed_boundary,),
            ).fetchone()
        )

    def pending_intents(
        self,
        limit: int,
        now: datetime | None = None,
        *,
        interactive: bool | None = None,
    ) -> list[CollectorIntent]:
        if limit < 1:
            raise ValueError("intent limit must be positive")
        intent_time = now or datetime.now(UTC)
        with self._connection() as connection:
            with connection.transaction():
                rows = connection.execute(
                    """SELECT work.id, work.kind, work.due_at, work.player_id, work.normalized_tag, work.sweep_id, work.status FROM collector_work AS work WHERE work.kind IN ('initial_collection', 'live_refresh', 'reset_baseline', 'discovery_profile', 'global_player_rankings') AND work.status IN ('pending', 'waiting_retry') AND work.due_at <= %s AND (%s::boolean IS NULL OR (%s = true AND work.kind IN ('initial_collection', 'live_refresh')) OR (%s = false AND work.kind NOT IN ('initial_collection', 'live_refresh'))) ORDER BY CASE WHEN work.lane = 'reset' THEN 0 WHEN work.lane = 'interactive' THEN 1 ELSE 2 END, work.due_at, work.id LIMIT %s""",
                    (intent_time, interactive, interactive, interactive, limit),
                ).fetchall()
                intents = [
                    CollectorIntent(
                        str(row[1]),
                        row[2],
                        None if row[3] is None else int(row[3]),
                        None if row[4] is None else str(row[4]),
                        work_id=int(row[0]),
                        due_at=row[2],
                        status=str(row[6]),
                        sweep_id=None if row[5] is None else int(row[5]),
                    )
                    for row in rows
                ]
        return intents

    def health_metrics(self) -> dict[str, int | float]:
        with self._connection() as connection:
            row = connection.execute(
                """WITH active_reset AS (SELECT sweep.id FROM collector_reset_sweeps AS sweep JOIN collector_work AS work ON work.sweep_id = sweep.id WHERE work.kind = 'reset_baseline' AND work.status NOT IN ('complete', 'failed', 'cancelled') ORDER BY sweep.boundary_at DESC, sweep.id DESC LIMIT 1)
                SELECT (SELECT count(*) FROM players WHERE active = true),
                       (SELECT count(*) FROM players WHERE active = true AND next_due_at <= clock_timestamp()),
                       COALESCE((SELECT greatest(0, extract(epoch FROM clock_timestamp() - min(next_due_at))) FROM players WHERE active = true AND next_due_at <= clock_timestamp()), 0),
                       (SELECT count(*) FROM python_processing_jobs WHERE status IN ('pending', 'waiting_retry', 'waiting_dependency', 'leased')),
                       (SELECT count(*) FROM collector_response_uploads WHERE state IN ('pending', 'leased', 'failed')),
                       (SELECT count(*) FROM python_processing_jobs WHERE status = 'failed'),
                       (SELECT count(*) FROM collector_response_uploads WHERE state = 'failed'),
                       (SELECT count(*) FROM collector_work WHERE sweep_id = (SELECT id FROM active_reset) AND kind = 'reset_baseline'),
                       (SELECT count(*) FROM collector_work WHERE sweep_id = (SELECT id FROM active_reset) AND kind = 'reset_baseline' AND status IN ('complete', 'failed', 'cancelled'))"""
            ).fetchone()
        assert row is not None
        names = (
            "active_players",
            "due_queue_depth",
            "oldest_due_age_seconds",
            "pending_processing",
            "pending_uploads",
            "failed_processing",
            "failed_uploads",
            "reset_total",
            "reset_terminal",
        )
        return {
            name: float(value) if name == "oldest_due_age_seconds" else int(value)
            for name, value in zip(names, row, strict=True)
        }

    def schedule_rankings_cycle(self, now: datetime | None = None) -> bool:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        cycle_at = instant.replace(
            minute=(instant.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        coalescing_key = "global-player-rankings:" + cycle_at.isoformat().replace(
            "+00:00", "Z"
        )
        with self._connection() as connection:
            with connection.transaction():
                if (
                    connection.execute(
                        "SELECT 1 FROM collector_work WHERE coalescing_key = %s AND status = 'complete' LIMIT 1",
                        (coalescing_key,),
                    ).fetchone()
                    is not None
                ):
                    return False
                row = connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, due_at, coalescing_key,
                        profile_status, battle_log_status
                    ) VALUES (
                        'global_player_rankings', 'ordinary', 'global', %s, %s,
                        'pending', 'not_applicable'
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (cycle_at, coalescing_key),
                ).fetchone()
        return row is not None

    def complete_intent(self, job_id: int) -> bool:
        if job_id < 1:
            raise ValueError("intent job ID must be positive")
        with self._connection() as connection:
            with connection.transaction():
                work = connection.execute(
                    "SELECT kind, profile_status, battle_log_status, profile_observation_id, battle_log_observation_id FROM collector_work WHERE id = %s AND status IN ('pending', 'waiting_retry') FOR UPDATE",
                    (job_id,),
                ).fetchone()
                if work is None:
                    return False
                expected = {
                    "discovery_profile": ("observed", "not_applicable"),
                    "global_player_rankings": ("observed", "not_applicable"),
                }.get(work[0], ("observed", "observed"))
                required_ids = (
                    (work[3],)
                    if work[0] in {"discovery_profile", "global_player_rankings"}
                    else (work[3], work[4])
                )
                if (work[1], work[2]) != expected or any(
                    value is None for value in required_ids
                ):
                    return False
                row = connection.execute(
                    "UPDATE collector_work SET status = 'complete', completed_at = clock_timestamp(), failure_category = NULL, failure_detail = NULL, updated_at = clock_timestamp() WHERE id = %s RETURNING id",
                    (job_id,),
                ).fetchone()
        return row is not None

    def fail_intent(
        self,
        job_id: int,
        *,
        category: str,
        detail: str | None = None,
        retryable: bool = False,
    ) -> bool:
        if job_id < 1:
            raise ValueError("intent job ID must be positive")
        target_status = "waiting_retry" if retryable else "failed"
        with self._connection() as connection:
            with connection.transaction():
                row = connection.execute(
                    "UPDATE collector_work SET status = %s, due_at = CASE WHEN %s THEN clock_timestamp() + interval '5 seconds' ELSE due_at END, failure_category = left(%s, 128), failure_detail = left(%s, 1024), updated_at = clock_timestamp() WHERE id = %s AND status NOT IN ('complete', 'failed', 'cancelled') RETURNING id",
                    (target_status, retryable, category, detail or "", job_id),
                ).fetchone()
        return row is not None

    def begin_reset(
        self,
        boundary_at: datetime,
        *,
        local_regular_inflight: int = 0,
    ) -> int | None:
        if local_regular_inflight:
            raise RuntimeError(
                "regular work must drain before Reset membership freezes"
            )
        utc_boundary = boundary_at.astimezone(UTC)
        if (
            utc_boundary.hour,
            utc_boundary.minute,
            utc_boundary.second,
            utc_boundary.microsecond,
        ) != (5, 0, 0, 0):
            raise ValueError("Reset boundary must be 05:00 UTC")
        with self._connection() as connection:
            with connection.transaction():
                older_boundary = connection.execute(
                    """
                    SELECT sweep.boundary_at
                    FROM collector_reset_sweeps AS sweep
                    WHERE sweep.boundary_at < %s
                      AND EXISTS (
                          SELECT 1 FROM collector_work AS work
                          WHERE work.sweep_id = sweep.id
                            AND work.kind = 'reset_baseline'
                            AND work.status NOT IN ('complete', 'failed', 'cancelled')
                      )
                    ORDER BY sweep.boundary_at
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (utc_boundary,),
                ).fetchone()
                if older_boundary is not None:
                    return None
                sweep_row = connection.execute(
                    """
                    INSERT INTO collector_reset_sweeps (boundary_at)
                    VALUES (%s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (utc_boundary,),
                ).fetchone()
                first_capture = sweep_row is not None
                if sweep_row is None:
                    sweep_row = connection.execute(
                        "SELECT id FROM collector_reset_sweeps WHERE boundary_at = %s FOR UPDATE",
                        (utc_boundary,),
                    ).fetchone()
                assert sweep_row is not None
                sweep_id = int(sweep_row[0])
                if first_capture:
                    member_ids = connection.execute(
                        "SELECT COALESCE(array_agg(id ORDER BY id), '{}'::bigint[]) FROM players WHERE active = true"
                    ).fetchone()[0]
                    connection.execute(
                        """
                        UPDATE collector_reset_sweeps
                        SET member_ids = %s,
                            membership_captured_at = clock_timestamp()
                        WHERE id = %s
                        """,
                        (member_ids, sweep_id),
                    )
                else:
                    member_ids = connection.execute(
                        "SELECT member_ids FROM collector_reset_sweeps WHERE id = %s FOR UPDATE",
                        (sweep_id,),
                    ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, due_at,
                        coalescing_key, sweep_id, profile_status, battle_log_status
                    )
                    SELECT 'reset_baseline', 'reset', 'player', player.id,
                           player.normalized_tag, %s,
                           'reset:' || %s || ':' || player.id, %s, 'pending', 'pending'
                    FROM unnest(%s::bigint[]) AS member(player_id)
                    JOIN players AS player ON player.id = member.player_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM collector_work AS existing
                        WHERE existing.coalescing_key = 'reset:' || %s || ':' || player.id
                    )
                    """,
                    (utc_boundary, sweep_id, sweep_id, member_ids, sweep_id),
                )
        return sweep_id

    def reset_ready(self, sweep_id: int) -> bool:
        if sweep_id < 1:
            raise ValueError("Reset sweep ID must be positive")
        with self._connection() as connection:
            return connection.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM collector_work WHERE sweep_id = %s AND kind = 'reset_baseline' AND status NOT IN ('complete', 'failed', 'cancelled'))",
                (sweep_id,),
            ).fetchone()[0]

    @staticmethod
    def _upsert_upload(
        connection: Any, handoff: ResponseHandoff
    ) -> tuple[str | None, str | None]:
        row = connection.execute(
            """
            INSERT INTO collector_response_uploads (
                response_hash, spool_key, byte_size
            ) VALUES (%s, %s, %s)
            ON CONFLICT (response_hash) DO UPDATE
            SET local_deleted_at = NULL, updated_at = clock_timestamp()
            WHERE collector_response_uploads.spool_key = EXCLUDED.spool_key
              AND collector_response_uploads.byte_size = EXCLUDED.byte_size
            RETURNING state, archive_reference
            """,
            (handoff.response_hash, handoff.spool_key, handoff.byte_size),
        ).fetchone()
        if row is None:
            existing = connection.execute(
                """
                SELECT spool_key, byte_size
                FROM collector_response_uploads
                WHERE response_hash = %s
                """,
                (handoff.response_hash,),
            ).fetchone()
            if existing != (handoff.spool_key, handoff.byte_size):
                raise ValueError("response hash was reused for different spool bytes")
            raise RuntimeError("response upload upsert returned no row")
        if row[0] == "complete":
            return str(row[1]), handoff.response_hash
        return None, None

    @staticmethod
    def _upsert_observation(
        connection: Any,
        handoff: ResponseHandoff,
        archive_reference: str | None,
        archive_catalogue_hash: str | None,
    ) -> int:
        request_method, request_path, request_query, paging_state = (
            CollectorDatabase._path_for(handoff)
        )
        source_adapter = {
            "profile": "player-profile-v1",
            "battle_log": "battle-log-v1",
            "global_player_rankings": "global-player-rankings-v1",
        }[handoff.endpoint]
        row = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, player_id, normalized_tag, scope, endpoint,
                request_started_at, response_completed_at, http_status,
                response_hash, archive_reference, archive_catalogue_hash,
                collector_version, key_label, evidence_headers, request_method,
                request_path, request_query, paging_envelope_state,
                source_adapter_version
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (occurrence_key) DO NOTHING
            RETURNING id
            """,
            (
                handoff.occurrence_key,
                handoff.player_id,
                handoff.normalized_tag,
                handoff.scope,
                handoff.endpoint,
                handoff.request_started_at,
                handoff.response_completed_at,
                handoff.http_status,
                handoff.response_hash,
                archive_reference,
                archive_catalogue_hash,
                handoff.collector_version,
                handoff.key_label,
                Jsonb(dict(handoff.evidence_headers)),
                request_method,
                request_path,
                request_query,
                paging_state,
                source_adapter,
            ),
        ).fetchone()
        if row is not None:
            return int(row[0])
        existing = connection.execute(
            """
            SELECT id, scope, endpoint, player_id, normalized_tag, response_hash
            FROM collector_observations
            WHERE occurrence_key = %s
            """,
            (handoff.occurrence_key,),
        ).fetchone()
        expected = (
            handoff.scope,
            handoff.endpoint,
            handoff.player_id,
            handoff.normalized_tag,
            handoff.response_hash,
        )
        if existing is None or existing[1:] != expected:
            raise ValueError("response occurrence key conflicts with observation")
        return int(existing[0])

    @staticmethod
    def _upsert_processing_job(
        connection: Any,
        handoff: ResponseHandoff,
        observation_id: int,
        parser_version: str,
    ) -> int:
        deduplication_key = "process-response:" + handoff.occurrence_key
        row = connection.execute(
            """
            INSERT INTO python_processing_jobs (
                observation_id, work_type, deduplication_key, input_json,
                parser_version, processing_version, domain_rule_version,
                analytics_rule_version, due_at
            ) VALUES (
                %s, 'process_observation', %s, '{}'::jsonb, %s, %s, %s, %s, %s
            )
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (
                observation_id,
                deduplication_key,
                parser_version,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
                handoff.response_completed_at,
            ),
        ).fetchone()
        if row is not None:
            return int(row[0])
        existing = connection.execute(
            """
            SELECT id, observation_id, work_type, parser_version,
                   processing_version
            FROM python_processing_jobs
            WHERE deduplication_key = %s OR observation_id = %s
            ORDER BY id
            LIMIT 1
            """,
            (deduplication_key, observation_id),
        ).fetchone()
        expected = (
            observation_id,
            "process_observation",
            parser_version,
            PROCESSING_VERSION,
        )
        if existing is None or existing[1:] != expected:
            raise ValueError("response processing job identity conflict")
        return int(existing[0])

    @staticmethod
    def _upsert_response_state(
        connection: Any,
        handoff: ResponseHandoff,
        observation_id: int | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO collector_response_state (
                scope, identity_key, endpoint, player_id, normalized_tag,
                last_response_hash, last_occurrence_key, last_seen_at,
                last_observation_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scope, identity_key, endpoint) DO UPDATE
            SET request_count = collector_response_state.request_count + 1,
                player_id = CASE WHEN EXCLUDED.last_seen_at >=
                    collector_response_state.last_seen_at
                    THEN EXCLUDED.player_id ELSE collector_response_state.player_id END,
                normalized_tag = CASE WHEN EXCLUDED.last_seen_at >=
                    collector_response_state.last_seen_at
                    THEN EXCLUDED.normalized_tag
                    ELSE collector_response_state.normalized_tag END,
                last_response_hash = CASE WHEN EXCLUDED.last_seen_at >=
                    collector_response_state.last_seen_at
                    THEN EXCLUDED.last_response_hash
                    ELSE collector_response_state.last_response_hash END,
                last_occurrence_key = CASE WHEN EXCLUDED.last_seen_at >=
                    collector_response_state.last_seen_at
                    THEN EXCLUDED.last_occurrence_key
                    ELSE collector_response_state.last_occurrence_key END,
                last_seen_at = GREATEST(
                    EXCLUDED.last_seen_at, collector_response_state.last_seen_at
                ),
                last_observation_id = CASE WHEN EXCLUDED.last_seen_at >=
                    collector_response_state.last_seen_at
                    THEN EXCLUDED.last_observation_id
                    ELSE collector_response_state.last_observation_id END,
                updated_at = clock_timestamp()
            """,
            (
                handoff.scope,
                handoff.identity_key,
                handoff.endpoint,
                handoff.player_id,
                handoff.normalized_tag,
                handoff.response_hash,
                handoff.occurrence_key,
                handoff.response_completed_at,
                observation_id,
            ),
        )

    def record_response(self, handoff: ResponseHandoff) -> ResponseResult:
        self._validate_handoff(handoff)
        parser_version = self._parser_for(handoff.endpoint)
        state_key = f"{handoff.scope}:{handoff.identity_key}:{handoff.endpoint}"
        with self._connection() as connection:
            with connection.transaction():
                work_kind = self._validate_work_identity(connection, handoff)
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (handoff.response_hash,),
                )
                archive_reference, archive_catalogue_hash = self._upsert_upload(
                    connection, handoff
                )
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (state_key,),
                )
                state = connection.execute(
                    """
                    SELECT last_response_hash, last_seen_at, last_observation_id,
                           last_occurrence_key
                    FROM collector_response_state
                    WHERE scope = %s AND identity_key = %s AND endpoint = %s
                    FOR UPDATE
                    """,
                    (handoff.scope, handoff.identity_key, handoff.endpoint),
                ).fetchone()
                if state is not None and state[3] == handoff.occurrence_key:
                    recorded = connection.execute(
                        """
                        SELECT observation.id, job.id
                        FROM collector_observations AS observation
                        LEFT JOIN python_processing_jobs AS job
                          ON job.observation_id = observation.id
                         AND job.work_type = 'process_observation'
                        WHERE observation.occurrence_key = %s
                        """,
                        (handoff.occurrence_key,),
                    ).fetchone()
                    if recorded is None:
                        return ResponseResult(
                            False, None, None, handoff.response_hash, parser_version
                        )
                    if recorded[1] is None:
                        raise RuntimeError(
                            "response occurrence is missing its processing job"
                        )
                    return ResponseResult(
                        True,
                        int(recorded[0]),
                        int(recorded[1]),
                        handoff.response_hash,
                        parser_version,
                    )

                # Reset needs boundary-time proof even when bytes match the
                # previous poll. Ordinary unchanged responses compact to state.
                if (
                    state is not None
                    and state[0] == handoff.response_hash
                    and work_kind != "reset_baseline"
                ):
                    self._upsert_response_state(connection, handoff, state[2])
                    self._record_intent_endpoint(connection, handoff, state[2])
                    connection.execute(
                        """
                        UPDATE archive_catalogue
                        SET last_seen_before = date_trunc(
                                'hour', GREATEST(%s, clock_timestamp())
                            ) + interval '1 hour'
                        WHERE response_hash = %s AND availability = 'verified'
                          AND last_seen_before < GREATEST(%s, clock_timestamp())
                        """,
                        (
                            handoff.response_completed_at,
                            handoff.response_hash,
                            handoff.response_completed_at,
                        ),
                    )
                    return ResponseResult(
                        False, None, None, handoff.response_hash, parser_version
                    )

                observation_id = self._upsert_observation(
                    connection,
                    handoff,
                    archive_reference,
                    archive_catalogue_hash,
                )
                processing_job_id = self._upsert_processing_job(
                    connection, handoff, observation_id, parser_version
                )
                self._upsert_response_state(connection, handoff, observation_id)
                self._record_intent_endpoint(connection, handoff, observation_id)
        return ResponseResult(
            True,
            observation_id,
            processing_job_id,
            handoff.response_hash,
            parser_version,
        )

    @staticmethod
    def _record_intent_endpoint(
        connection: Any,
        handoff: ResponseHandoff,
        observation_id: int | None,
    ) -> None:
        if handoff.collector_work_id is None:
            return
        status_column = {
            "profile": "profile_status",
            "battle_log": "battle_log_status",
            "global_player_rankings": "profile_status",
        }[handoff.endpoint]
        observation_column = {
            "profile": "profile_observation_id",
            "battle_log": "battle_log_observation_id",
            "global_player_rankings": "profile_observation_id",
        }[handoff.endpoint]
        connection.execute(
            f"""
            UPDATE collector_work
            SET {status_column} = 'observed',
                {observation_column} = %s,
                updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (observation_id, handoff.collector_work_id),
        )

    def record_transport_failure(self, failure: TransportFailure) -> int:
        if failure.endpoint not in _ENDPOINTS:
            raise ValueError("unsupported transport endpoint")
        if failure.scope == "player":
            if (
                failure.endpoint not in _PLAYER_ENDPOINTS
                or failure.player_id is None
                or failure.normalized_tag is None
            ):
                raise ValueError("player transport failure identity is invalid")
        elif (
            failure.scope != "global"
            or failure.endpoint != "global_player_rankings"
            or failure.player_id is not None
            or failure.normalized_tag is not None
        ):
            raise ValueError("global transport failure identity is invalid")
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO collector_transport_failures (
                    player_id, normalized_tag, endpoint, request_started_at,
                    failed_at, failure_category, retry_state, key_label, scope,
                    evidence_key, occurrence_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (occurrence_key) DO UPDATE SET
                    failed_at = EXCLUDED.failed_at,
                    failure_category = EXCLUDED.failure_category,
                    retry_state = EXCLUDED.retry_state
                RETURNING id
                """,
                (
                    failure.player_id,
                    failure.normalized_tag,
                    failure.endpoint,
                    failure.request_started_at,
                    failure.failed_at,
                    failure.failure_category,
                    failure.retry_state,
                    failure.key_label,
                    failure.scope,
                    "python-transport:" + failure.occurrence_key,
                    failure.occurrence_key,
                ),
            ).fetchone()
            assert row is not None
        return int(row[0])

    def claim_upload(
        self,
        *,
        owner: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> UploadClaim | None:
        if not owner or lease_seconds < 1:
            raise ValueError("upload owner and positive lease are required")
        claim_time = now or datetime.now(UTC)
        token = str(uuid4())
        expires = claim_time + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE collector_response_uploads
                    SET state = 'pending', lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, updated_at = clock_timestamp()
                    WHERE state = 'leased' AND lease_expires_at <= %s
                    """,
                    (claim_time,),
                )
                row = connection.execute(
                    """
                    SELECT response_hash
                    FROM collector_response_uploads
                    WHERE state IN ('pending', 'failed')
                      AND next_attempt_at <= %s
                    ORDER BY next_attempt_at, created_at, response_hash
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                    """,
                    (claim_time,),
                ).fetchone()
                if row is None:
                    return None
                claimed = connection.execute(
                    """
                    UPDATE collector_response_uploads
                    SET state = 'leased', lease_owner = %s, lease_token = %s,
                        lease_expires_at = %s, attempt_count = attempt_count + 1,
                        updated_at = clock_timestamp()
                    WHERE response_hash = %s
                    RETURNING response_hash, spool_key, byte_size, lease_expires_at,
                              attempt_count
                    """,
                    (owner, token, expires, row[0]),
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
        )

    def _lock_upload(
        self,
        connection: Any,
        claim: UploadClaim,
        *,
        owner: str | None,
        now: datetime,
    ) -> tuple[Any, ...]:
        row = connection.execute(
            """
            SELECT response_hash, spool_key, byte_size, state, lease_owner,
                   lease_token, lease_expires_at
            FROM collector_response_uploads
            WHERE response_hash = %s
            FOR UPDATE
            """,
            (claim.response_hash,),
        ).fetchone()
        if (
            row is None
            or row[3] != "leased"
            or row[4] != (owner or claim.owner)
            or row[5] != claim.token
            or row[6] <= now
        ):
            raise RuntimeError("upload lease lost")
        return row

    def complete_upload(
        self,
        claim: UploadClaim,
        *,
        archive_reference: str,
        archive_instance_id: str,
        owner: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not archive_reference or not archive_instance_id:
            raise ValueError("archive identity is required")
        complete_time = now or datetime.now(UTC)
        with self._connection() as connection:
            with connection.transaction():
                row = self._lock_upload(
                    connection, claim, owner=owner, now=complete_time
                )
                if int(row[2]) != claim.byte_size or row[1] != claim.spool_key:
                    raise ValueError("upload claim metadata changed")
                existing = connection.execute(
                    """
                    SELECT response_hash, byte_size, archive_instance_id
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
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO archive_catalogue (
                            response_hash, archive_reference, byte_size, archive_instance_id
                        ) VALUES (%s, %s, %s, %s)
                        """,
                        (
                            claim.response_hash,
                            archive_reference,
                            claim.byte_size,
                            archive_instance_id,
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
                connection.execute(
                    """
                    UPDATE collector_response_uploads
                    SET state = 'complete', archive_reference = %s,
                        archive_instance_id = %s, completed_at = %s,
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, updated_at = clock_timestamp()
                    WHERE response_hash = %s
                    """,
                    (
                        archive_reference,
                        archive_instance_id,
                        complete_time,
                        claim.response_hash,
                    ),
                )

    def fail_upload(
        self,
        claim: UploadClaim,
        *,
        category: str,
        detail: str | None = None,
        retryable: bool = True,
        owner: str | None = None,
        now: datetime | None = None,
    ) -> None:
        fail_time = now or datetime.now(UTC)
        with self._connection() as connection:
            with connection.transaction():
                self._lock_upload(connection, claim, owner=owner, now=fail_time)
                connection.execute(
                    """
                    UPDATE collector_response_uploads
                    SET state = 'failed', next_attempt_at = CASE WHEN %s
                            THEN %s + interval '5 seconds' ELSE 'infinity'::timestamptz END,
                        last_error_category = left(%s, 128),
                        last_error_detail = left(%s, 1024),
                        lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, updated_at = clock_timestamp()
                    WHERE response_hash = %s
                    """,
                    (
                        retryable,
                        fail_time,
                        category,
                        detail or "",
                        claim.response_hash,
                    ),
                )

    def referenced_spool_hashes(self) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT response_hash
                FROM collector_response_uploads
                WHERE local_deleted_at IS NULL
                UNION
                SELECT response_hash
                FROM collector_observations
                WHERE archive_reference IS NULL
                UNION
                SELECT state.last_response_hash
                FROM collector_response_state AS state
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM collector_response_uploads AS upload
                    WHERE upload.response_hash = state.last_response_hash
                      AND upload.state = 'complete'
                      AND upload.local_deleted_at IS NOT NULL
                )
                """
            ).fetchall()
        return {str(row[0]) for row in rows}

    def deletable_hashes(self, *, limit: int = 100) -> list[str]:
        if limit < 1:
            raise ValueError("cleanup limit must be positive")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT upload.response_hash
                FROM collector_response_uploads AS upload
                WHERE upload.state = 'complete'
                  AND upload.local_deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM python_processing_jobs AS job
                      JOIN collector_observations AS observation
                        ON observation.id = job.observation_id
                      WHERE job.work_type = 'process_observation'
                        AND observation.response_hash = upload.response_hash
                        AND job.status <> 'complete'
                  )
                ORDER BY upload.completed_at, upload.response_hash
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def delete_spool_if_deletable(
        self, response_hash: str, delete: Callable[[str], bool]
    ) -> bool:
        self._validate_hash(response_hash)
        with self._connection() as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (response_hash,),
                )
                eligible = connection.execute(
                    """
                    SELECT 1
                    FROM collector_response_uploads AS upload
                    WHERE upload.response_hash = %s
                      AND upload.state = 'complete'
                      AND upload.local_deleted_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM python_processing_jobs AS job
                          JOIN collector_observations AS observation
                            ON observation.id = job.observation_id
                          WHERE job.work_type = 'process_observation'
                            AND observation.response_hash = upload.response_hash
                            AND job.status <> 'complete'
                      )
                    FOR UPDATE
                    """,
                    (response_hash,),
                ).fetchone()
                if eligible is None:
                    return False
                if not delete(response_hash):
                    return False
                connection.execute(
                    """
                    UPDATE collector_response_uploads
                    SET local_deleted_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE response_hash = %s
                    """,
                    (response_hash,),
                )
        return True
