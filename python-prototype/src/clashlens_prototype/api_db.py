from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .verification import KeyAction, VerificationOutcome

API_CONTRACT_VERSION = 2
VERIFICATION_RESERVATION_SECONDS = 45


@dataclass(frozen=True, slots=True)
class AccountContext:
    internal_id: int
    public_id: str
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RequestBinding:
    request_id: str
    caller: str
    provider: str
    provider_subject: str
    account_id: int | None
    operation: str
    method: str
    request_target: str
    identity: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OperationResult:
    status_code: int
    payload: dict[str, Any]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class PermitResult:
    granted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class VerificationReservation:
    fresh: bool
    result: OperationResult | None


class ApiDatabase:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        timeout_seconds: float = 5.0,
    ) -> None:
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("API database pool bounds are invalid")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("API database pool timeout is invalid")
        self.pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_seconds,
            open=True,
        )

    def close(self) -> None:
        self.pool.close()

    def is_ready(self, *, expected_contract_version: int = API_CONTRACT_VERSION) -> bool:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT version,
                       to_regclass('clash_lens_accounts') IS NOT NULL,
                       to_regclass('private_api_requests') IS NOT NULL,
                       to_regclass('shared_api_credentials') IS NOT NULL
                FROM clash_lens_contract
                WHERE singleton = true
                """
            ).fetchone()
            return bool(
                row is not None
                and int(row[0]) == expected_contract_version
                and row[1]
                and row[2]
                and row[3]
            )

    def scalar(self, query: str, params: Iterable[Any] = ()) -> Any:
        with self.pool.connection() as connection:
            row = connection.execute(query, tuple(params)).fetchone()
            return None if row is None else _text(row[0])

    def resolve_account(self, provider: str, provider_subject: str) -> AccountContext | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT a.id, a.public_id, a.normalized_username, a.display_name
                FROM account_provider_identities AS identity
                JOIN clash_lens_accounts AS a ON a.id = identity.account_id
                WHERE identity.provider = %s AND identity.provider_subject = %s
                """,
                (provider, provider_subject),
            ).fetchone()
            return None if row is None else _account_context(row)

    def register_official_credential(self, fingerprint: str) -> None:
        with self.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO shared_api_credentials (
                        credential_fingerprint, go_budget, python_budget, total_budget
                    ) VALUES (%s, 29, 1, 30)
                    ON CONFLICT (credential_fingerprint) DO NOTHING
                    """,
                    (fingerprint,),
                )
                row = connection.execute(
                    """
                    SELECT go_budget, python_budget, total_budget
                    FROM shared_api_credentials
                    WHERE credential_fingerprint = %s
                    FOR UPDATE
                    """,
                    (fingerprint,),
                ).fetchone()
                if row is None or tuple(map(int, row)) != (29, 1, 30):
                    raise RuntimeError("conflicting official credential registration")

    def acquire_official_permit(
        self,
        fingerprint: str,
        *,
        caller: str,
        request_id: str,
    ) -> PermitResult:
        del request_id  # The shared PostgreSQL gate owns the permit identity.
        if caller not in {"go", "python"}:
            raise ValueError("official traffic caller is invalid")
        with self.pool.connection() as connection:
            with connection.transaction():
                registered = connection.execute(
                    """
                    SELECT 1
                    FROM shared_api_credentials
                    WHERE credential_fingerprint = %s
                    """,
                    (fingerprint,),
                ).fetchone()
                if registered is None:
                    return PermitResult(False, "credential_unknown")
                decision = connection.execute(
                    """
                    SELECT granted, credential_state
                    FROM clashlens_acquire_shared_api_permit(%s, %s)
                    """,
                    (fingerprint, caller),
                ).fetchone()
                assert decision is not None
                if bool(decision[0]):
                    return PermitResult(True, "granted")
                state = _text(decision[1])
                if state == "quarantined":
                    return PermitResult(False, "credential_quarantined")
                if state == "cooldown":
                    return PermitResult(False, "credential_cooldown")
                if state != "active":
                    return PermitResult(False, "credential_inactive")
                counts = connection.execute(
                    """
                    SELECT count(*) FILTER (WHERE caller = %s), count(*)
                    FROM shared_api_permits
                    WHERE credential_fingerprint = %s
                      AND permitted_at > clock_timestamp() - interval '1 second'
                    """,
                    (caller, fingerprint),
                ).fetchone()
                budgets = connection.execute(
                    """
                    SELECT python_budget, go_budget, total_budget
                    FROM shared_api_credentials
                    WHERE credential_fingerprint = %s
                    """,
                    (fingerprint,),
                ).fetchone()
                assert counts is not None and budgets is not None
                caller_budget = int(budgets[0] if caller == "python" else budgets[1])
                if int(counts[0]) >= caller_budget:
                    return PermitResult(False, f"{caller}_budget_exhausted")
                if int(counts[1]) >= int(budgets[2]):
                    return PermitResult(False, "combined_budget_exhausted")
                return PermitResult(False, "credential_inactive")

    def apply_official_key_action(
        self,
        fingerprint: str,
        action: KeyAction,
        *,
        cooldown_seconds: int,
    ) -> None:
        if not 1 <= cooldown_seconds <= 300:
            raise ValueError("official credential cooldown is outside the supported range")
        if action is KeyAction.NONE:
            return
        with self.pool.connection() as connection:
            with connection.transaction():
                if action is KeyAction.COOLDOWN:
                    connection.execute(
                        """
                        UPDATE shared_api_credentials
                        SET state = CASE WHEN state = 'quarantined' THEN state ELSE 'cooldown' END,
                            cooldown_until = CASE
                                WHEN state = 'quarantined' THEN cooldown_until
                                ELSE GREATEST(
                                    COALESCE(cooldown_until, '-infinity'::timestamptz),
                                    clock_timestamp() + make_interval(secs => %s)
                                )
                            END,
                            updated_at = clock_timestamp()
                        WHERE credential_fingerprint = %s
                        """,
                        (cooldown_seconds, fingerprint),
                    )
                elif action is KeyAction.QUARANTINE:
                    connection.execute(
                        """
                        UPDATE shared_api_credentials
                        SET state = 'quarantined', cooldown_until = NULL,
                            quarantine_reason = 'verified_authentication_failure',
                            updated_at = clock_timestamp()
                        WHERE credential_fingerprint = %s
                        """,
                        (fingerprint,),
                    )

    def reserve_verification(
        self,
        binding: RequestBinding,
        *,
        normalized_tag: str,
    ) -> VerificationReservation:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(
                    connection,
                    binding,
                    recover_expired_verification=True,
                )
                if existing is not None:
                    return VerificationReservation(False, existing)
                player_id = self._ensure_player(connection, normalized_tag)
                connection.execute(
                    """
                    INSERT INTO player_link_verification_audits (
                        request_id, account_id, player_id, outcome
                    ) VALUES (%s, %s, %s, 'pending')
                    """,
                    (binding.request_id, binding.account_id, player_id),
                )
                return VerificationReservation(True, None)

    def lookup_request(self, binding: RequestBinding) -> OperationResult | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT caller, provider, provider_subject, account_id, operation,
                       method, request_target, identity_json, state,
                       response_status, response_json
                FROM private_api_requests
                WHERE request_id = %s
                """,
                (binding.request_id,),
            ).fetchone()
            if row is None:
                return None
            expected = (
                binding.caller,
                binding.provider,
                binding.provider_subject,
                binding.account_id,
                binding.operation,
                binding.method,
                binding.request_target,
                binding.identity,
            )
            actual = tuple(_text(value) for value in row[:8])
            if actual != expected:
                return OperationResult(
                    409, {"error": "request_id_conflict"}, replayed=True
                )
            if _text(row[8]) != "complete":
                return OperationResult(202, {"status": "in_progress"}, replayed=True)
            return OperationResult(int(row[9]), dict(row[10]), replayed=True)

    def complete_verification(
        self,
        binding: RequestBinding,
        *,
        normalized_tag: str,
        outcome: VerificationOutcome,
        account_id: int,
        completed_at: datetime,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                self._assert_request_binding(connection, binding)
                player = connection.execute(
                    """
                    SELECT id FROM players WHERE normalized_tag = %s FOR UPDATE
                    """,
                    (normalized_tag,),
                ).fetchone()
                if player is None:
                    raise RuntimeError("verification player reservation was lost")
                player_id = int(player[0])
                if outcome is VerificationOutcome.INVALID_TOKEN:
                    audit_outcome = "invalid_token"
                    result = OperationResult(
                        401, {"status": "invalid_token", "tag": normalized_tag}
                    )
                elif outcome is VerificationOutcome.UNAVAILABLE:
                    audit_outcome = "verification_unavailable"
                    result = OperationResult(
                        503,
                        {"status": "verification_unavailable", "tag": normalized_tag},
                    )
                else:
                    link = connection.execute(
                        """
                        SELECT account_id FROM verified_player_links
                        WHERE player_id = %s FOR UPDATE
                        """,
                        (player_id,),
                    ).fetchone()
                    if link is None:
                        connection.execute(
                            """
                            INSERT INTO verified_player_links (
                                player_id, account_id, verification_request_id,
                                verified_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                player_id,
                                account_id,
                                binding.request_id,
                                completed_at,
                                completed_at,
                            ),
                        )
                        audit_outcome = "linked"
                        result = OperationResult(
                            200, {"status": "linked", "tag": normalized_tag}
                        )
                    elif int(link[0]) == account_id:
                        connection.execute(
                            """
                            UPDATE verified_player_links
                            SET verification_request_id = %s, verified_at = %s,
                                updated_at = %s
                            WHERE player_id = %s
                            """,
                            (
                                binding.request_id,
                                completed_at,
                                completed_at,
                                player_id,
                            ),
                        )
                        audit_outcome = "already_linked"
                        result = OperationResult(
                            200, {"status": "already_linked", "tag": normalized_tag}
                        )
                    else:
                        connection.execute(
                            """
                            INSERT INTO support_player_link_transfer_candidates (
                                verification_request_id, player_id, from_account_id,
                                to_account_id, verified_at, expires_at, state
                            ) VALUES (
                                %s, %s, %s, %s, %s,
                                %s + interval '15 minutes', 'pending'
                            )
                            """,
                            (
                                binding.request_id,
                                player_id,
                                int(link[0]),
                                account_id,
                                completed_at,
                                completed_at,
                            ),
                        )
                        audit_outcome = "support_required"
                        result = OperationResult(
                            409,
                            {
                                "status": "support_required",
                                "tag": normalized_tag,
                                "verification_request_id": binding.request_id,
                            },
                        )
                updated = connection.execute(
                    """
                    UPDATE player_link_verification_audits
                    SET outcome = %s, completed_at = %s
                    WHERE request_id = %s AND outcome = 'pending'
                    """,
                    (audit_outcome, completed_at, binding.request_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("verification audit reservation was lost")
                self._complete_request(connection, binding.request_id, result)
                return result

    def complete_invalid_verification_request(
        self,
        binding: RequestBinding,
        *,
        completed_at: datetime,
    ) -> OperationResult:
        """Close a reserved request after safe body validation fails."""
        with self.pool.connection() as connection:
            with connection.transaction():
                self._assert_request_binding(connection, binding)
                result = OperationResult(422, {"error": "invalid_request"})
                updated = connection.execute(
                    """
                    UPDATE player_link_verification_audits
                    SET outcome = 'invalid_request', completed_at = %s
                    WHERE request_id = %s AND outcome = 'pending'
                    """,
                    (completed_at, binding.request_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("verification audit reservation was lost")
                self._complete_request(connection, binding.request_id, result)
                return result


    def create_account(
        self,
        binding: RequestBinding,
        *,
        username: str,
        normalized_username: str,
        display_name: str,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                public_id = uuid4()
                try:
                    with connection.transaction():
                        account = connection.execute(
                            """
                            INSERT INTO clash_lens_accounts (
                                public_id, username, normalized_username, display_name
                            ) VALUES (%s, %s, %s, %s)
                            RETURNING id
                            """,
                            (public_id, username, normalized_username, display_name),
                        ).fetchone()
                        assert account is not None
                        account_id = int(account[0])
                        connection.execute(
                            """
                            INSERT INTO account_provider_identities (
                                account_id, provider, provider_subject
                            ) VALUES (%s, %s, %s)
                            """,
                            (account_id, binding.provider, binding.provider_subject),
                        )
                except psycopg.errors.UniqueViolation as error:
                    constraint = error.diag.constraint_name or ""
                    if "normalized_username" in constraint:
                        result = OperationResult(409, {"error": "username_unavailable"})
                    else:
                        result = OperationResult(
                            409, {"error": "provider_identity_conflict"}
                        )
                    self._complete_request(connection, binding.request_id, result)
                    return result

                result = OperationResult(
                    201,
                    {
                        "username": normalized_username,
                        "display_name": display_name,
                        "preferences": {},
                        "providers": [binding.provider],
                    },
                )
                self._complete_request(connection, binding.request_id, result)
                return result

    def get_account(self, account_id: int) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT normalized_username, display_name, preferences
                FROM clash_lens_accounts
                WHERE id = %s
                """,
                (account_id,),
            ).fetchone()
            if row is None:
                return None
            providers = [
                _text(item[0])
                for item in connection.execute(
                    """
                    SELECT provider
                    FROM account_provider_identities
                    WHERE account_id = %s
                    ORDER BY provider
                    """,
                    (account_id,),
                )
            ]
            return {
                "username": _text(row[0]),
                "display_name": _text(row[1]),
                "preferences": dict(row[2]),
                "providers": providers,
            }

    def update_account(
        self,
        binding: RequestBinding,
        *,
        username: str,
        normalized_username: str,
        display_name: str,
        preferences: dict[str, Any],
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                try:
                    with connection.transaction():
                        updated = connection.execute(
                            """
                            UPDATE clash_lens_accounts
                            SET username = %s, normalized_username = %s,
                                display_name = %s, preferences = %s,
                                updated_at = clock_timestamp()
                            WHERE id = %s
                            """,
                            (
                                username,
                                normalized_username,
                                display_name,
                                Jsonb(preferences),
                                binding.account_id,
                            ),
                        )
                except psycopg.errors.UniqueViolation:
                    result = OperationResult(409, {"error": "username_unavailable"})
                    self._complete_request(connection, binding.request_id, result)
                    return result
                if updated.rowcount != 1:
                    result = OperationResult(404, {"error": "account_not_found"})
                else:
                    providers = [
                        _text(row[0])
                        for row in connection.execute(
                            """
                            SELECT provider FROM account_provider_identities
                            WHERE account_id = %s ORDER BY provider
                            """,
                            (binding.account_id,),
                        )
                    ]
                    result = OperationResult(
                        200,
                        {
                            "username": normalized_username,
                            "display_name": display_name,
                            "preferences": preferences,
                            "providers": providers,
                        },
                    )
                self._complete_request(connection, binding.request_id, result)
                return result

    def get_player_page(
        self,
        normalized_tag: str,
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT player.normalized_tag, player.active, player.eligibility_state,
                       profile.name, profile.trophies, profile.observed_at,
                       profile.source_http_status, profile.endpoint_version,
                       profile.schema_version, profile.parser_version
                FROM players AS player
                JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE player.normalized_tag = %s
                """,
                (normalized_tag,),
            ).fetchone()
            if row is None:
                return None
            observed_at = row[5].astimezone(UTC)
            age_seconds = max(0, int((now.astimezone(UTC) - observed_at).total_seconds()))
            daily_rows = connection.execute(
                """
                SELECT ranked_day_start, version, state, coverage,
                       adjustments, battles, partial_reasons
                FROM (
                    SELECT DISTINCT ON (ranked_day_start)
                           ranked_day_start, version, state, coverage,
                           adjustments, battles, partial_reasons
                    FROM api_player_daily_logs
                    WHERE player_id = (
                        SELECT id FROM players WHERE normalized_tag = %s
                    )
                    ORDER BY ranked_day_start DESC, version DESC
                ) AS current_days
                ORDER BY ranked_day_start DESC
                LIMIT 28
                """,
                (normalized_tag,),
            ).fetchall()
            return {
                "tag": _text(row[0]),
                "name": _text(row[3]),
                "trophies": int(row[4]),
                "eligibility": _text(row[2]),
                "active": bool(row[1]),
                "freshness": "fresh" if age_seconds <= freshness_seconds else "stale",
                "age_seconds": age_seconds,
                "coverage": "ranked_days" if daily_rows else "profile_only",
                "observed_at": observed_at.isoformat(),
                "source_http_status": int(row[6]),
                "endpoint_version": _text(row[7]),
                "schema_version": _text(row[8]),
                "parser_version": _text(row[9]),
                "daily_logs": [
                    {
                        "ranked_day_start": day[0].astimezone(UTC).isoformat(),
                        "version": int(day[1]),
                        "state": _text(day[2]),
                        "coverage": _text(day[3]),
                        "adjustments": list(day[4]),
                        "battles": list(day[5]),
                        "partial_reasons": list(day[6]),
                    }
                    for day in daily_rows
                ],
            }

    def get_live_leaderboard(
        self,
        *,
        limit: int,
        now: datetime,
        freshness_seconds: int,
    ) -> dict[str, Any]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT player.normalized_tag, profile.name, profile.trophies,
                       profile.observed_at, player.eligibility_state
                FROM players AS player
                JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE player.active = true
                ORDER BY profile.trophies DESC, md5(player.normalized_tag),
                         player.normalized_tag
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
            entries = []
            stale_count = 0
            for position, row in enumerate(rows, start=1):
                observed_at = row[3].astimezone(UTC)
                age_seconds = max(
                    0, int((now.astimezone(UTC) - observed_at).total_seconds())
                )
                freshness = "fresh" if age_seconds <= freshness_seconds else "stale"
                stale_count += freshness == "stale"
                entries.append(
                    {
                        "position": position,
                        "tag": _text(row[0]),
                        "name": _text(row[1]),
                        "trophies": int(row[2]),
                        "observed_at": observed_at.isoformat(),
                        "age_seconds": age_seconds,
                        "freshness": freshness,
                        "confidence": _text(row[4]),
                        "official_rank": None,
                    }
                )
            return {
                "kind": "live",
                "ordering_rule_version": "tracked-trophies-md5-v1",
                "generated_at": now.astimezone(UTC).isoformat(),
                "tracked_population": int(
                    connection.execute(
                        "SELECT count(*) FROM players WHERE active = true"
                    ).fetchone()[0]
                ),
                "coverage": {
                    "returned": len(entries),
                    "stale_entries": stale_count,
                },
                "entries": entries,
            }

    def get_frozen_leaderboard(self, *, limit: int) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            # The domain snapshot is the immutable publication source. A
            # building candidate is never current; the newest published version
            # remains visible until its complete entry set commits.
            snapshot = connection.execute(
                """
                SELECT id, boundary_at, version, ordering_rule_version,
                       freshness_rule_version, measured_coverage,
                       eligible_population_count, included_entry_count,
                       stale_entry_count, fresh_entry_count,
                       excluded_missing_count, excluded_invalid_count,
                       excluded_malformed_count, excluded_conflicting_count
                FROM leaderboard_snapshots
                WHERE snapshot_kind = 'frozen' AND state = 'published'
                ORDER BY boundary_at DESC, version DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if snapshot is not None:
                rows = connection.execute(
                    """
                    SELECT entry.position, player.normalized_tag,
                           profile.name, entry.trophies,
                           entry.profile_observed_at,
                           entry.profile_freshness,
                           entry.profile_confidence,
                           entry.official_rank
                    FROM leaderboard_snapshot_entries AS entry
                    JOIN players AS player ON player.id = entry.player_id
                    LEFT JOIN LATERAL (
                        SELECT version.name
                        FROM player_profile_versions AS version
                        WHERE version.observation_id = entry.profile_observation_id
                        ORDER BY version.id DESC
                        LIMIT 1
                    ) AS profile ON true
                    WHERE entry.snapshot_id = %s
                    ORDER BY entry.position
                    LIMIT %s
                    """,
                    (snapshot[0], limit),
                ).fetchall()
                return {
                    "kind": "frozen",
                    "snapshot_id": str(snapshot[0]),
                    "boundary_at": snapshot[1].astimezone(UTC).isoformat(),
                    "version": int(snapshot[2]),
                    "ordering_rule_version": _text(snapshot[3]),
                    "coverage": {
                        "measured": float(snapshot[5]),
                        "eligible_population": int(snapshot[6]),
                        "included_entries": int(snapshot[7]),
                        "stale_entries": int(snapshot[8]),
                        "fresh_entries": int(snapshot[9]),
                        "excluded_missing": int(snapshot[10]),
                        "excluded_invalid": int(snapshot[11]),
                        "excluded_malformed": int(snapshot[12]),
                        "excluded_conflicting": int(snapshot[13]),
                    },
                    "entries": [
                        {
                            "position": int(row[0]),
                            "tag": _text(row[1]),
                            "name": None if row[2] is None else _text(row[2]),
                            "trophies": int(row[3]),
                            "observed_at": row[4].astimezone(UTC).isoformat(),
                            "freshness": _text(row[5]),
                            "confidence": _text(row[6]),
                            "official_rank": None if row[7] is None else int(row[7]),
                        }
                        for row in rows
                    ],
                }

            # Keep the populated v1 API relation readable during migration. It
            # is used only when no immutable domain snapshot exists yet.
            snapshot = connection.execute(
                """
                SELECT id, public_id, boundary_at, version,
                       ordering_rule_version, coverage
                FROM api_frozen_leaderboards
                ORDER BY boundary_at DESC, version DESC
                LIMIT 1
                """
            ).fetchone()
            if snapshot is None:
                return None
            rows = connection.execute(
                """
                SELECT entry.position, player.normalized_tag, profile.name,
                       entry.trophies, entry.observed_at, entry.freshness,
                       entry.confidence, entry.official_rank
                FROM api_frozen_leaderboard_entries AS entry
                JOIN players AS player ON player.id = entry.player_id
                LEFT JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE entry.leaderboard_id = %s
                ORDER BY entry.position
                LIMIT %s
                """,
                (snapshot[0], limit),
            ).fetchall()
            return {
                "kind": "frozen",
                "snapshot_id": str(snapshot[1]),
                "boundary_at": snapshot[2].astimezone(UTC).isoformat(),
                "version": int(snapshot[3]),
                "ordering_rule_version": _text(snapshot[4]),
                "coverage": dict(snapshot[5]),
                "entries": [
                    {
                        "position": int(row[0]),
                        "tag": _text(row[1]),
                        "name": None if row[2] is None else _text(row[2]),
                        "trophies": int(row[3]),
                        "observed_at": row[4].astimezone(UTC).isoformat(),
                        "freshness": _text(row[5]),
                        "confidence": _text(row[6]),
                        "official_rank": None if row[7] is None else int(row[7]),
                    }
                    for row in rows
                ],
            }

    def get_basic_analytics(
        self,
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> dict[str, Any]:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT count(*), avg(profile.trophies),
                       count(*) FILTER (
                           WHERE profile.observed_at >= %s - make_interval(secs => %s)
                       )
                FROM players AS player
                JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE player.active = true
                """,
                (now, freshness_seconds),
            ).fetchone()
            sample_size = int(row[0])
            fresh = int(row[2])
            return {
                "population": "tracked_players",
                "period": {"as_of": now.astimezone(UTC).isoformat()},
                "sample_size": sample_size,
                "coverage": {
                    "profile_observations": sample_size,
                    "battle_observations": 0,
                },
                "freshness": {"fresh": fresh, "stale": sample_size - fresh},
                "classification_state": "unclassified",
                "classification_version": None,
                "analytics_rule_version": "basic-profile-v1",
                "unclassified_count": 0,
                "results": {
                    "average_trophies": None if row[1] is None else float(row[1])
                },
            }

    def submit_refresh(
        self,
        binding: RequestBinding,
        *,
        normalized_tag: str,
        cooldown_seconds: int,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                row = connection.execute(
                    """
                    SELECT job_id, outcome
                    FROM clashlens_enqueue_interactive('live_refresh', %s, %s)
                    """,
                    (normalized_tag, cooldown_seconds),
                ).fetchone()
                assert row is not None
                collector_job_id = int(row[0])
                public_id = uuid4()
                connection.execute(
                    """
                    INSERT INTO api_refresh_requests (
                        public_id, collector_job_id, normalized_tag, initial_outcome
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (collector_job_id) DO NOTHING
                    """,
                    (public_id, collector_job_id, normalized_tag, _text(row[1])),
                )
                refresh = connection.execute(
                    """
                    SELECT public_id, initial_outcome
                    FROM api_refresh_requests
                    WHERE collector_job_id = %s
                    """,
                    (collector_job_id,),
                ).fetchone()
                assert refresh is not None
                result = OperationResult(
                    202,
                    {
                        "refresh_id": str(refresh[0]),
                        "tag": normalized_tag,
                        "status": "pending",
                        "outcome": _text(refresh[1]),
                    },
                )
                self._complete_request(connection, binding.request_id, result)
                return result

    def get_refresh_status(self, refresh_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT refresh.public_id, refresh.normalized_tag,
                       job.status, refresh.initial_outcome
                FROM api_refresh_requests AS refresh
                JOIN collector_jobs AS job ON job.id = refresh.collector_job_id
                WHERE refresh.public_id = %s
                """,
                (refresh_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "refresh_id": str(row[0]),
                "tag": _text(row[1]),
                "status": _text(row[2]),
                "outcome": _text(row[3]),
            }

    def submit_export(
        self,
        binding: RequestBinding,
        *,
        export_format: str,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                public_id = uuid4()
                export_row = connection.execute(
                    """
                    INSERT INTO account_export_requests (
                        public_id, account_id, format, state
                    ) VALUES (%s, %s, %s, 'pending')
                    RETURNING id
                    """,
                    (public_id, binding.account_id, export_format),
                ).fetchone()
                assert export_row is not None
                export_request_id = int(export_row[0])
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        observation_id, work_type, deduplication_key, input_json,
                        priority, parser_version, max_attempts
                    ) VALUES (
                        NULL, 'build_export', %s, %s, 100,
                        'export-scaffold-v1', 3
                    )
                    """,
                    (
                        f"export:{public_id}",
                        Jsonb({"export_request_id": export_request_id}),
                    ),
                )
                result = OperationResult(
                    202,
                    {
                        "export_id": str(public_id),
                        "format": export_format,
                        "status": "pending",
                    },
                )
                self._complete_request(connection, binding.request_id, result)
                return result

    def get_export_status(
        self,
        account_id: int,
        export_id: str,
    ) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT public_id, format, state, result_reference, safe_failure
                FROM account_export_requests
                WHERE public_id = %s AND account_id = %s
                """,
                (export_id, account_id),
            ).fetchone()
            if row is None:
                return None
            result: dict[str, Any] = {
                "export_id": str(row[0]),
                "format": _text(row[1]),
                "status": _text(row[2]),
            }
            if row[3] is not None:
                result["result_reference"] = _text(row[3])
            if row[4] is not None:
                result["failure"] = _text(row[4])
            return result

    def add_saved_player(
        self,
        binding: RequestBinding,
        *,
        normalized_tag: str,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                player_id = self._ensure_player(connection, normalized_tag)
                connection.execute(
                    """
                    INSERT INTO account_saved_players (account_id, player_id)
                    VALUES (%s, %s)
                    ON CONFLICT (account_id, player_id) DO NOTHING
                    """,
                    (binding.account_id, player_id),
                )
                result = OperationResult(200, {"tag": normalized_tag, "saved": True})
                self._complete_request(connection, binding.request_id, result)
                return result

    def remove_saved_player(
        self,
        binding: RequestBinding,
        *,
        normalized_tag: str,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                connection.execute(
                    """
                    DELETE FROM account_saved_players AS saved
                    USING players AS player
                    WHERE saved.account_id = %s
                      AND saved.player_id = player.id
                      AND player.normalized_tag = %s
                    """,
                    (binding.account_id, normalized_tag),
                )
                result = OperationResult(200, {"tag": normalized_tag, "saved": False})
                self._complete_request(connection, binding.request_id, result)
                return result

    def list_saved_players(self, account_id: int) -> list[dict[str, Any]]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT player.normalized_tag, profile.name
                FROM account_saved_players AS saved
                JOIN players AS player ON player.id = saved.player_id
                LEFT JOIN player_profile_versions AS profile
                    ON profile.id = player.current_profile_version_id
                WHERE saved.account_id = %s
                ORDER BY player.normalized_tag
                LIMIT 500
                """,
                (account_id,),
            ).fetchall()
            return [
                {"tag": _text(row[0]), "name": None if row[1] is None else _text(row[1])}
                for row in rows
            ]

    def create_group(
        self,
        binding: RequestBinding,
        *,
        name: str,
        normalized_name: str,
        normalized_tags: list[str],
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                public_id = uuid4()
                try:
                    with connection.transaction():
                        group = connection.execute(
                            """
                            INSERT INTO account_groups (
                                public_id, account_id, name, normalized_name
                            ) VALUES (%s, %s, %s, %s)
                            RETURNING id
                            """,
                            (public_id, binding.account_id, name, normalized_name),
                        ).fetchone()
                        assert group is not None
                        self._replace_group_players(
                            connection, int(group[0]), normalized_tags
                        )
                except psycopg.errors.UniqueViolation:
                    result = OperationResult(409, {"error": "group_name_conflict"})
                    self._complete_request(connection, binding.request_id, result)
                    return result
                result = OperationResult(
                    201,
                    {
                        "group_id": str(public_id),
                        "name": name,
                        "tags": sorted(set(normalized_tags)),
                    },
                )
                self._complete_request(connection, binding.request_id, result)
                return result

    def update_group(
        self,
        binding: RequestBinding,
        *,
        group_id: str,
        name: str,
        normalized_name: str,
        normalized_tags: list[str],
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                try:
                    with connection.transaction():
                        group = connection.execute(
                            """
                            UPDATE account_groups
                            SET name = %s, normalized_name = %s,
                                updated_at = clock_timestamp()
                            WHERE public_id = %s AND account_id = %s
                            RETURNING id
                            """,
                            (name, normalized_name, group_id, binding.account_id),
                        ).fetchone()
                        if group is not None:
                            self._replace_group_players(
                                connection, int(group[0]), normalized_tags
                            )
                except psycopg.errors.UniqueViolation:
                    result = OperationResult(409, {"error": "group_name_conflict"})
                    self._complete_request(connection, binding.request_id, result)
                    return result
                if group is None:
                    result = OperationResult(404, {"error": "group_not_found"})
                else:
                    result = OperationResult(
                        200,
                        {
                            "group_id": group_id,
                            "name": name,
                            "tags": sorted(set(normalized_tags)),
                        },
                    )
                self._complete_request(connection, binding.request_id, result)
                return result

    def delete_group(
        self,
        binding: RequestBinding,
        *,
        group_id: str,
    ) -> OperationResult:
        with self.pool.connection() as connection:
            with connection.transaction():
                existing = self._reserve_request(connection, binding)
                if existing is not None:
                    return existing
                deleted = connection.execute(
                    """
                    DELETE FROM account_groups
                    WHERE public_id = %s AND account_id = %s
                    """,
                    (group_id, binding.account_id),
                )
                if deleted.rowcount == 1:
                    result = OperationResult(200, {"deleted": True, "group_id": group_id})
                else:
                    result = OperationResult(404, {"error": "group_not_found"})
                self._complete_request(connection, binding.request_id, result)
                return result

    def list_groups(self, account_id: int) -> list[dict[str, Any]]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT group_row.public_id, group_row.name, player.normalized_tag
                FROM account_groups AS group_row
                LEFT JOIN account_group_players AS member ON member.group_id = group_row.id
                LEFT JOIN players AS player ON player.id = member.player_id
                WHERE group_row.account_id = %s
                ORDER BY group_row.normalized_name, group_row.public_id, player.normalized_tag
                LIMIT 10000
                """,
                (account_id,),
            ).fetchall()
            groups: dict[str, dict[str, Any]] = {}
            for row in rows:
                public_id = str(row[0])
                group = groups.setdefault(
                    public_id,
                    {"group_id": public_id, "name": _text(row[1]), "tags": []},
                )
                if row[2] is not None:
                    group["tags"].append(_text(row[2]))
            return list(groups.values())

    def get_public_user(self, normalized_username: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            account = connection.execute(
                """
                SELECT id, normalized_username, display_name
                FROM clash_lens_accounts
                WHERE normalized_username = %s
                """,
                (normalized_username,),
            ).fetchone()
            if account is None:
                return None
            return {
                "username": _text(account[1]),
                "display_name": _text(account[2]),
                "verified_players": self._verified_players(connection, int(account[0])),
            }

    def get_multi_account_summary(self, account_id: int) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            account = connection.execute(
                """
                SELECT normalized_username, display_name
                FROM clash_lens_accounts
                WHERE id = %s
                """,
                (account_id,),
            ).fetchone()
            if account is None:
                return None
            return {
                "username": _text(account[0]),
                "display_name": _text(account[1]),
                "verified_players": self._verified_players(connection, account_id),
            }

    @staticmethod
    def _ensure_player(connection: Any, normalized_tag: str) -> int:
        row = connection.execute(
            """
            INSERT INTO players (normalized_tag, active)
            VALUES (%s, false)
            ON CONFLICT (normalized_tag) DO UPDATE
                SET normalized_tag = EXCLUDED.normalized_tag
            RETURNING id
            """,
            (normalized_tag,),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _replace_group_players(
        self,
        connection: Any,
        group_id: int,
        normalized_tags: list[str],
    ) -> None:
        connection.execute(
            "DELETE FROM account_group_players WHERE group_id = %s",
            (group_id,),
        )
        for normalized_tag in sorted(set(normalized_tags)):
            player_id = self._ensure_player(connection, normalized_tag)
            connection.execute(
                """
                INSERT INTO account_group_players (group_id, player_id)
                VALUES (%s, %s)
                """,
                (group_id, player_id),
            )

    @staticmethod
    def _verified_players(connection: Any, account_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT player.normalized_tag, profile.name
            FROM verified_player_links AS link
            JOIN players AS player ON player.id = link.player_id
            LEFT JOIN player_profile_versions AS profile
                ON profile.id = player.current_profile_version_id
            WHERE link.account_id = %s
            ORDER BY player.normalized_tag
            LIMIT 500
            """,
            (account_id,),
        ).fetchall()
        return [
            {"tag": _text(row[0]), "name": None if row[1] is None else _text(row[1])}
            for row in rows
        ]

    def _reserve_request(
        self,
        connection: Any,
        binding: RequestBinding,
        *,
        recover_expired_verification: bool = False,
    ) -> OperationResult | None:
        inserted = connection.execute(
            """
            INSERT INTO private_api_requests (
                request_id, caller, provider, provider_subject, account_id,
                operation, method, request_target, identity_json, state,
                in_progress_until
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_progress',
                clock_timestamp() + make_interval(secs => %s)
            )
            ON CONFLICT (request_id) DO NOTHING
            RETURNING request_id
            """,
            (
                binding.request_id,
                binding.caller,
                binding.provider,
                binding.provider_subject,
                binding.account_id,
                binding.operation,
                binding.method,
                binding.request_target,
                Jsonb(binding.identity),
                VERIFICATION_RESERVATION_SECONDS,
            ),
        ).fetchone()
        if inserted is not None:
            return None
        row = connection.execute(
            """
            SELECT caller, provider, provider_subject, account_id, operation,
                   method, request_target, identity_json, state,
                   response_status, response_json, in_progress_until
            FROM private_api_requests
            WHERE request_id = %s
            FOR UPDATE
            """,
            (binding.request_id,),
        ).fetchone()
        assert row is not None
        expected = (
            binding.caller,
            binding.provider,
            binding.provider_subject,
            binding.account_id,
            binding.operation,
            binding.method,
            binding.request_target,
            binding.identity,
        )
        actual = tuple(_text(value) for value in row[:8])
        if actual != expected:
            return OperationResult(409, {"error": "request_id_conflict"}, replayed=True)
        if _text(row[8]) != "complete":
            if (
                recover_expired_verification
                and _text(row[8]) == "in_progress"
                and row[11] is not None
                and row[11] <= connection.execute("SELECT clock_timestamp()").fetchone()[0]
            ):
                tag_row = connection.execute(
                    """
                    SELECT player.normalized_tag
                    FROM player_link_verification_audits AS audit
                    JOIN players AS player ON player.id = audit.player_id
                    WHERE audit.request_id = %s AND audit.outcome = 'pending'
                    FOR UPDATE OF audit
                    """,
                    (binding.request_id,),
                ).fetchone()
                if tag_row is not None:
                    result = OperationResult(
                        503,
                        {
                            "status": "verification_unavailable",
                            "tag": _text(tag_row[0]),
                        },
                    )
                    updated = connection.execute(
                        """
                        UPDATE player_link_verification_audits
                        SET outcome = 'verification_unavailable',
                            completed_at = clock_timestamp()
                        WHERE request_id = %s AND outcome = 'pending'
                        """,
                        (binding.request_id,),
                    )
                    if updated.rowcount == 1:
                        self._complete_request(connection, binding.request_id, result)
                        return result
            return OperationResult(202, {"status": "in_progress"}, replayed=True)
        return OperationResult(
            int(row[9]),
            dict(row[10]),
            replayed=True,
        )

    @staticmethod
    def _assert_request_binding(connection: Any, binding: RequestBinding) -> None:
        row = connection.execute(
            """
            SELECT caller, provider, provider_subject, account_id, operation,
                   method, request_target, identity_json, state
            FROM private_api_requests
            WHERE request_id = %s
            FOR UPDATE
            """,
            (binding.request_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("private API request reservation is missing")
        expected = (
            binding.caller,
            binding.provider,
            binding.provider_subject,
            binding.account_id,
            binding.operation,
            binding.method,
            binding.request_target,
            binding.identity,
        )
        actual = tuple(_text(value) for value in row[:8])
        if actual != expected or _text(row[8]) != "in_progress":
            raise RuntimeError("private API request binding is not active")

    @staticmethod
    def _complete_request(
        connection: Any,
        request_id: str,
        result: OperationResult,
    ) -> None:
        updated = connection.execute(
            """
            UPDATE private_api_requests
            SET state = 'complete', response_status = %s, response_json = %s,
                in_progress_until = NULL, completed_at = clock_timestamp()
            WHERE request_id = %s AND state = 'in_progress'
            """,
            (result.status_code, Jsonb(result.payload), request_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("private API request reservation was lost")


def _account_context(row: Any) -> AccountContext:
    return AccountContext(
        internal_id=int(row[0]),
        public_id=str(row[1]),
        username=_text(row[2]),
        display_name=_text(row[3]),
    )


def _text(value: Any) -> Any:
    return value.decode("utf-8") if isinstance(value, bytes) else value
