"""Shared setup for discord bot postgres tests."""

from __future__ import annotations

from uuid import uuid4

from clashlens import api_accounts
from clashlens.api_db import ApiDatabase
from clashlens.api_requests import RequestBinding

DISCORD_ID = "100000000000000001"


def _account(database: ApiDatabase, provider: str, subject: str, username: str) -> int:
    created = api_accounts.create_account(
        database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider=provider,
            provider_subject=subject,
            account_id=None,
            operation="account.create",
            method="POST",
            request_target="/v1/account",
            identity={"username": username},
        ),
        username=username,
        normalized_username=username,
        display_name=username.title(),
    )
    assert created.status_code == 201
    account = api_accounts.resolve_account(database, provider, subject)
    assert account is not None
    return account.internal_id


def _verify(database: ApiDatabase, account_id: int, tag: str) -> None:
    """Verify `tag` to the account, moving it there as a transfer does."""
    request_id = str(uuid4())
    with database.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO private_api_requests (
                request_id, caller, provider, provider_subject, account_id,
                operation, method, request_target, identity_json, state,
                response_status, response_json, completed_at
            ) VALUES (
                %s, 'typescript-website', 'discord', 'fixture', %s,
                'player_links.verify', 'POST', '/v1/players/verifytoken',
                '{}'::jsonb, 'complete', 200, '{}'::jsonb, clock_timestamp()
            )
            """,
            (request_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO verified_player_links (player_id, account_id, verification_request_id)
            SELECT id, %s, %s FROM players WHERE normalized_tag = %s
            ON CONFLICT (player_id) DO UPDATE
                SET account_id = EXCLUDED.account_id,
                    verification_request_id = EXCLUDED.verification_request_id
            """,
            (account_id, request_id, tag),
        )
