from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from . import api_db
from .api_db import (
    AccountContext,
    ApiDatabase,
    OperationResult,
    RequestBinding,
    _account_context,
    _text,
)


def resolve_account(
    database, provider: str, provider_subject: str
) -> AccountContext | None:
    with database.pool.connection() as connection:
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


def create_account(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    username: str,
    normalized_username: str,
    display_name: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
                api_db._complete_request(connection, binding.request_id, result)
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
            api_db._complete_request(connection, binding.request_id, result)
            return result


def get_account(database: ApiDatabase, account_id: int) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
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
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    username: str,
    normalized_username: str,
    display_name: str,
    preferences: dict[str, Any],
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
                api_db._complete_request(connection, binding.request_id, result)
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
            api_db._complete_request(connection, binding.request_id, result)
            return result


def link_provider(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    account_id: int,
    provider: str,
    provider_subject: str,
) -> OperationResult:
    """Idempotently attach one provider identity to the signed-in account.

    A collision never merges or moves identities: a subject owned by any
    other account, or an account that already holds this provider with a
    different subject, is refused with a safe conflict.

    The account row lock serializes every provider mutation for one
    account, so concurrent links and unlinks observe a stable provider
    set and can never remove the final identity together.
    """
    with database.pool.connection() as connection:
        with connection.transaction():
            locked_account = connection.execute(
                """
                SELECT id FROM clash_lens_accounts WHERE id = %s FOR UPDATE
                """,
                (account_id,),
            ).fetchone()
            if locked_account is None:
                result = OperationResult(404, {"error": "account_not_found"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            owner = connection.execute(
                """
                SELECT account_id FROM account_provider_identities
                WHERE provider = %s AND provider_subject = %s
                FOR UPDATE
                """,
                (provider, provider_subject),
            ).fetchone()
            current = connection.execute(
                """
                SELECT provider_subject FROM account_provider_identities
                WHERE account_id = %s AND provider = %s
                FOR UPDATE
                """,
                (account_id, provider),
            ).fetchone()
            collision = (owner is not None and int(owner[0]) != account_id) or (
                current is not None and _text(current[0]) != provider_subject
            )
            if collision:
                result = OperationResult(
                    409, {"error": "provider_identity_conflict"}
                )
                api_db._complete_request(connection, binding.request_id, result)
                return result
            if current is None:
                inserted = connection.execute(
                    """
                    INSERT INTO account_provider_identities (
                        account_id, provider, provider_subject
                    ) VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (account_id, provider, provider_subject),
                )
                if inserted.rowcount != 1:
                    # The unique constraints arbitrate absent-key races.
                    result = OperationResult(
                        409, {"error": "provider_identity_conflict"}
                    )
                    api_db._complete_request(connection, binding.request_id, result)
                    return result
                _audit_provider_event(
                    connection,
                    account_id=account_id,
                    provider=provider,
                    action="link",
                    result="succeeded",
                    operator_identity=None,
                    reason="linked from the authenticated account",
                )
            providers = _account_providers(connection, account_id)
            result = OperationResult(
                200,
                {"providers": providers},
            )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def unlink_provider(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    account_id: int,
    provider: str,
    provider_subject: str,
) -> OperationResult:
    """Remove one freshly reauthenticated provider identity.

    The final linked identity cannot be removed, and unlinking never
    deletes the account or any private data.

    The account row lock serializes concurrent unlinks: the second unlink
    re-reads the remaining providers only after the first commits, so two
    simultaneous unlinks can never remove both identities.
    """
    with database.pool.connection() as connection:
        with connection.transaction():
            locked_account = connection.execute(
                """
                SELECT id FROM clash_lens_accounts WHERE id = %s FOR UPDATE
                """,
                (account_id,),
            ).fetchone()
            if locked_account is None:
                result = OperationResult(404, {"error": "account_not_found"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            owned = connection.execute(
                """
                SELECT 1 FROM account_provider_identities
                WHERE account_id = %s AND provider = %s
                  AND provider_subject = %s
                FOR UPDATE
                """,
                (account_id, provider, provider_subject),
            ).fetchone()
            if owned is None:
                result = OperationResult(404, {"error": "provider_not_linked"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            remaining = _account_providers(connection, account_id)
            if len(remaining) <= 1:
                result = OperationResult(409, {"error": "final_provider"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            connection.execute(
                """
                DELETE FROM account_provider_identities
                WHERE account_id = %s AND provider = %s
                  AND provider_subject = %s
                """,
                (account_id, provider, provider_subject),
            )
            _audit_provider_event(
                connection,
                account_id=account_id,
                provider=provider,
                action="unlink",
                result="succeeded",
                operator_identity=None,
                reason="unlinked after fresh provider authentication",
            )
            providers = _account_providers(connection, account_id)
            result = OperationResult(200, {"providers": providers})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def _account_providers(connection: Any, account_id: int) -> list[str]:
    return [
        _text(row[0])
        for row in connection.execute(
            """
            SELECT provider FROM account_provider_identities
            WHERE account_id = %s ORDER BY provider
            """,
            (account_id,),
        )
    ]


def _audit_provider_event(
    connection: Any,
    *,
    account_id: int,
    provider: str,
    action: str,
    result: str,
    operator_identity: str | None,
    reason: str,
) -> None:
    connection.execute(
        """
        INSERT INTO provider_identity_audits (
            account_id, provider, action, result, operator_identity, reason
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (account_id, provider, action, result, operator_identity, reason),
    )


def support_attach_discord_identity(
    database: ApiDatabase,
    *,
    account_public_id: str,
    normalized_player_tag: str,
    discord_subject: str,
    operator_identity: str,
    reason: str,
) -> tuple[str, str]:
    """Attach a free Discord identity after maintainer-assisted recovery.

    The requester must already have proven control of a player whose
    verified link points at the target account. The Discord subject must
    not belong to any Clash Lens account and the target must not hold
    Discord yet. Returns a (status, detail) pair; nothing personal beyond
    the audited event is written.

    Every outcome after target-account resolution is audited: a
    player/account mismatch as a failed recovery and either Discord
    collision as refused_collision. Self-service refusals keep their
    records in private_api_requests only.
    """
    with database.pool.connection() as connection:
        with connection.transaction():
            account = connection.execute(
                """
                SELECT id FROM clash_lens_accounts WHERE public_id = %s
                FOR UPDATE
                """,
                (UUID(account_public_id),),
            ).fetchone()
            if account is None:
                return "account_not_found", "target account does not exist"
            account_id = int(account[0])
            link = connection.execute(
                """
                SELECT link.account_id
                FROM verified_player_links AS link
                JOIN players AS player ON player.id = link.player_id
                WHERE player.normalized_tag = %s
                FOR UPDATE OF link
                """,
                (normalized_player_tag,),
            ).fetchone()
            if link is None or int(link[0]) != account_id:
                _audit_provider_event(
                    connection,
                    account_id=account_id,
                    provider="discord",
                    action="support_recovery",
                    result="failed",
                    operator_identity=operator_identity,
                    reason=reason,
                )
                return (
                    "player_not_verified_on_account",
                    "the player is not verified on the target account",
                )
            owner = connection.execute(
                """
                SELECT account_id FROM account_provider_identities
                WHERE provider = 'discord' AND provider_subject = %s
                FOR UPDATE
                """,
                (discord_subject,),
            ).fetchone()
            if owner is not None:
                _audit_provider_event(
                    connection,
                    account_id=account_id,
                    provider="discord",
                    action="support_recovery",
                    result="refused_collision",
                    operator_identity=operator_identity,
                    reason=reason,
                )
                return (
                    "refused_collision",
                    "the Discord identity belongs to an account",
                )
            existing = connection.execute(
                """
                SELECT 1 FROM account_provider_identities
                WHERE account_id = %s AND provider = 'discord'
                """,
                (account_id,),
            ).fetchone()
            if existing is not None:
                _audit_provider_event(
                    connection,
                    account_id=account_id,
                    provider="discord",
                    action="support_recovery",
                    result="refused_collision",
                    operator_identity=operator_identity,
                    reason=reason,
                )
                return (
                    "refused_collision",
                    "the target account already has Discord linked",
                )
            inserted = connection.execute(
                """
                INSERT INTO account_provider_identities (
                    account_id, provider, provider_subject
                ) VALUES (%s, 'discord', %s)
                ON CONFLICT DO NOTHING
                """,
                (account_id, discord_subject),
            )
            if inserted.rowcount != 1:
                # Keep a concurrent uniqueness refusal in this transaction.
                _audit_provider_event(
                    connection,
                    account_id=account_id,
                    provider="discord",
                    action="support_recovery",
                    result="refused_collision",
                    operator_identity=operator_identity,
                    reason=reason,
                )
                return (
                    "refused_collision",
                    "the Discord identity belongs to an account",
                )
            _audit_provider_event(
                connection,
                account_id=account_id,
                provider="discord",
                action="support_recovery",
                result="succeeded",
                operator_identity=operator_identity,
                reason=reason,
            )
            return "attached", "Discord identity attached to the account"


def submit_refresh(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    normalized_tag: str,
    cooldown_seconds: int,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            row = connection.execute(
                """
                SELECT *
                FROM clashlens_enqueue_interactive('live_refresh', %s, %s)
                """,
                (normalized_tag, cooldown_seconds),
            ).fetchone()
            assert row is not None
            collector_work_id = int(row[0])
            public_id = uuid4()
            connection.execute(
                """
                INSERT INTO api_refresh_requests (
                    public_id, collector_work_id, normalized_tag, initial_outcome
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (collector_work_id) DO NOTHING
                """,
                (public_id, collector_work_id, normalized_tag, _text(row[1])),
            )
            refresh = connection.execute(
                """
                SELECT public_id, initial_outcome
                FROM api_refresh_requests
                WHERE collector_work_id = %s
                """,
                (collector_work_id,),
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
            api_db._complete_request(connection, binding.request_id, result)
            return result


def get_refresh_status(database: ApiDatabase, refresh_id: str) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT refresh.public_id, refresh.normalized_tag,
                   work.status, refresh.initial_outcome
            FROM api_refresh_requests AS refresh
            JOIN collector_work AS work ON work.id = refresh.collector_work_id
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
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    export_format: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
            api_db._complete_request(connection, binding.request_id, result)
            return result


def get_export_status(
    database: ApiDatabase,
    account_id: int,
    export_id: str,
) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
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
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    normalized_tag: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            player_id = api_db._ensure_player(connection, normalized_tag)
            connection.execute(
                """
                INSERT INTO account_saved_players (account_id, player_id)
                VALUES (%s, %s)
                ON CONFLICT (account_id, player_id) DO NOTHING
                """,
                (binding.account_id, player_id),
            )
            result = OperationResult(200, {"tag": normalized_tag, "saved": True})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def remove_saved_player(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    normalized_tag: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
            api_db._complete_request(connection, binding.request_id, result)
            return result


def list_saved_players(database: ApiDatabase, account_id: int) -> list[dict[str, Any]]:
    with database.pool.connection() as connection:
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
            {
                "tag": _text(row[0]),
                "name": None if row[1] is None else _text(row[1]),
            }
            for row in rows
        ]


def create_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    name: str,
    normalized_name: str,
    normalized_tags: list[str],
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
                    _replace_group_players(database, 
                        connection, int(group[0]), normalized_tags
                    )
            except psycopg.errors.UniqueViolation:
                result = OperationResult(409, {"error": "group_name_conflict"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            result = OperationResult(
                201,
                {
                    "group_id": str(public_id),
                    "name": name,
                    "tags": sorted(set(normalized_tags)),
                },
            )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def update_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
    name: str,
    normalized_name: str,
    normalized_tags: list[str],
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
                        _replace_group_players(database, 
                            connection, int(group[0]), normalized_tags
                        )
            except psycopg.errors.UniqueViolation:
                result = OperationResult(409, {"error": "group_name_conflict"})
                api_db._complete_request(connection, binding.request_id, result)
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
            api_db._complete_request(connection, binding.request_id, result)
            return result


def delete_group(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    group_id: str,
) -> OperationResult:
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(database, connection, binding)
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
                result = OperationResult(
                    200, {"deleted": True, "group_id": group_id}
                )
            else:
                result = OperationResult(404, {"error": "group_not_found"})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def list_groups(database: ApiDatabase, account_id: int) -> list[dict[str, Any]]:
    with database.pool.connection() as connection:
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


def get_public_user(database: ApiDatabase, normalized_username: str) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
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
            "verified_players": _verified_players(connection, int(account[0])),
        }


def get_multi_account_summary(database: ApiDatabase, account_id: int) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
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
            "verified_players": _verified_players(connection, account_id),
        }


def _replace_group_players(
    database: ApiDatabase,
    connection: Any,
    group_id: int,
    normalized_tags: list[str],
) -> None:
    connection.execute(
        "DELETE FROM account_group_players WHERE group_id = %s",
        (group_id,),
    )
    for normalized_tag in sorted(set(normalized_tags)):
        player_id = api_db._ensure_player(connection, normalized_tag)
        connection.execute(
            """
            INSERT INTO account_group_players (group_id, player_id)
            VALUES (%s, %s)
            """,
            (group_id, player_id),
        )


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


