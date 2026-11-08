"""Which Clash Lens account a Google or Discord sign-in connection belongs to.

Account setup: a provider login whose connection no account holds is
"setup required" (``api_accounts.resolve_account`` answers None and the API
refuses with ``account_not_found``); ``api_accounts.create_account`` makes the
account and attaches that connection in one transaction, after which the
login resolves to the account. A valid provider login is never itself an
account, and nothing deletes an account.

Afterwards each connection moves between the ``ProviderMembership`` states
below, always under the account row lock.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from . import api_db, api_discord_alerts
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text
from .login_sessions import _record_identity_removal, _record_logout


class ProviderMembership(StrEnum):
    """One sign-in connection (provider and subject) seen from one account.

    States:
    - ``FREE``: no account holds the connection and the account has no
      connection from this provider.
    - ``ATTACHED``: this account holds exactly this connection.
    - ``OWNED_ELSEWHERE``: another account holds the connection.
    - ``PROVIDER_TAKEN``: this account already holds a different connection
      from the same provider.

    Events: linking (or support recovery) moves ``FREE`` to ``ATTACHED``;
    unlinking moves ``ATTACHED`` to ``FREE`` unless it is the account's last
    connection (refused as ``final_provider``). Every other state refuses the
    change and changes nothing: identities are never merged or moved.

    Owner: ``read_membership`` derives the state from
    ``account_provider_identities`` while the caller holds the account row
    lock, so concurrent links and unlinks on one account take turns. The
    unique constraints decide a race on a connection no row held yet, and the
    loser is refused as a conflict. Each change commits with its saved
    response, so a retried request replays it and a restart changes nothing.
    """

    FREE = "free"
    ATTACHED = "attached"
    OWNED_ELSEWHERE = "owned_elsewhere"
    PROVIDER_TAKEN = "provider_taken"


def read_membership(
    connection: Any,
    account_id: int,
    provider: str,
    provider_subject: str,
    *,
    other_accounts: bool = True,
) -> ProviderMembership:
    """Lock and classify the connection. Without ``other_accounts`` the
    connection's owner is not read or locked, so a connection another
    account holds reads as ``FREE``; unlinking only asks for ``ATTACHED``."""
    owner = (
        connection.execute(
            """
            SELECT account_id FROM account_provider_identities
            WHERE provider = %s AND provider_subject = %s
            FOR UPDATE
            """,
            (provider, provider_subject),
        ).fetchone()
        if other_accounts
        else None
    )
    if owner is not None:
        # Decided before locking this account's own connection, so two
        # recoveries naming each other's connections cannot deadlock. An
        # account holds one connection per provider, so its own is this one.
        if int(owner[0]) == account_id:
            return ProviderMembership.ATTACHED
        return ProviderMembership.OWNED_ELSEWHERE
    current = connection.execute(
        """
        SELECT provider_subject FROM account_provider_identities
        WHERE account_id = %s AND provider = %s
        FOR UPDATE
        """,
        (account_id, provider),
    ).fetchone()
    if current is None:
        return ProviderMembership.FREE
    if _text(current[0]) != provider_subject:
        return ProviderMembership.PROVIDER_TAKEN
    return ProviderMembership.ATTACHED


def link_provider(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    account_id: int,
    provider: str,
    provider_subject: str,
) -> OperationResult:
    """Idempotently attach one provider identity to the signed-in account:
    ``FREE`` becomes ``ATTACHED``, ``ATTACHED`` stays, and any other state is
    refused with a safe conflict."""
    with database.pool.connection() as connection:
        with connection.transaction():
            if not _lock_account(connection, account_id):
                result = OperationResult(404, {"error": "account_not_found"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            membership = read_membership(connection, account_id, provider, provider_subject)
            if membership in (
                ProviderMembership.OWNED_ELSEWHERE,
                ProviderMembership.PROVIDER_TAKEN,
            ):
                result = OperationResult(409, {"error": "provider_identity_conflict"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            if membership is ProviderMembership.FREE:
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
                    result = OperationResult(409, {"error": "provider_identity_conflict"})
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
            if provider == "discord":
                # Connecting Discord again adds the bot, which then confirms
                # each attack alert switch again.
                api_discord_alerts.ask_to_confirm_again(connection, account_id)
            result = OperationResult(200, {"providers": _account_providers(connection, account_id)})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def unlink_provider(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    account_id: int,
    provider: str,
    provider_subject: str,
    ended_session: str | None = None,
) -> OperationResult:
    """Remove one freshly reauthenticated provider identity: ``ATTACHED``
    becomes ``FREE`` unless it is the last one.

    Unlinking never deletes the account or any private data. Every login
    issued through the removed identity up to now ends, on every browser.
    When `ended_session` is given, that login is also recorded as logged out
    in the same transaction.
    """
    with database.pool.connection() as connection:
        with connection.transaction():
            if not _lock_account(connection, account_id):
                result = OperationResult(404, {"error": "account_not_found"})
                api_db._complete_request(connection, binding.request_id, result)
                return result
            existing = api_db._reserve_request(database, connection, binding)
            if existing is not None:
                return existing
            membership = read_membership(
                connection, account_id, provider, provider_subject, other_accounts=False
            )
            if membership is not ProviderMembership.ATTACHED:
                result = OperationResult(404, {"error": "provider_not_linked"})
            elif len(_account_providers(connection, account_id)) <= 1:
                result = OperationResult(409, {"error": "final_provider"})
            else:
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
                _record_identity_removal(connection, provider, provider_subject)
                if ended_session is not None:
                    _record_logout(connection, ended_session)
                result = OperationResult(
                    200, {"providers": _account_providers(connection, account_id)}
                )
            api_db._complete_request(connection, binding.request_id, result)
            return result


def _lock_account(connection: Any, account_id: int) -> bool:
    return (
        connection.execute(
            "SELECT id FROM clash_lens_accounts WHERE id = %s FOR UPDATE", (account_id,)
        ).fetchone()
        is not None
    )


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
    verified link points at the target account. Only a ``FREE`` Discord
    connection is attached. Returns a (status, detail) pair; nothing personal
    beyond the audited event is written.

    Every outcome after target-account resolution is audited: a
    player/account mismatch as a failed recovery and every other state as
    refused_collision. Self-service refusals keep their records in
    private_api_requests only.
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

            def audit(result: str) -> None:
                _audit_provider_event(
                    connection,
                    account_id=account_id,
                    provider="discord",
                    action="support_recovery",
                    result=result,
                    operator_identity=operator_identity,
                    reason=reason,
                )

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
                audit("failed")
                return (
                    "player_not_verified_on_account",
                    "the player is not verified on the target account",
                )
            membership = read_membership(connection, account_id, "discord", discord_subject)
            if membership is ProviderMembership.PROVIDER_TAKEN:
                audit("refused_collision")
                return "refused_collision", "the target account already has Discord linked"
            if membership is not ProviderMembership.FREE:
                audit("refused_collision")
                return "refused_collision", "the Discord identity belongs to an account"
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
                audit("refused_collision")
                return "refused_collision", "the Discord identity belongs to an account"
            audit("succeeded")
            return "attached", "Discord identity attached to the account"
