"""The bot's reads and its one write, run as the API's database role."""

from __future__ import annotations

from uuid import uuid4

from domain_test_support import as_api_role, domain_database
from test_api_db_public_ops import NOW, seed_profile

from bot.store import Store
from clashlens import api_accounts
from clashlens.api_db import ApiDatabase, RequestBinding

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


def test_bot_finds_discord_accounts_and_keeps_a_main_only_while_verified(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            for tag in ("#2PP", "#8QQ", "#9RR"):
                seed_profile(owner, tag, 5100)
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            theirs = _account(owner, "google", "google-subject", "other")
            _verify(owner, mine, "#2PP")
            _verify(owner, mine, "#8QQ")
            _verify(owner, theirs, "#9RR")
            store = Store(bot)

            # A Google-only account is not found by a Discord ID.
            assert store.account("google-subject") is None
            account = store.account(DISCORD_ID)
            assert account is not None and account.internal_id == mine
            cards = store.players(account, NOW)
            assert [card["tag"] for card in cards] == ["#2PP", "#8QQ"]
            assert all(card["observed_at"] is not None for card in cards)

            assert store.main_tag(account) is None
            assert store.set_main(account, "#9RR") is False
            assert store.main_tag(account) is None
            assert store.set_main(account, "#8QQ") is True
            assert store.main_tag(account) == "#8QQ"
            assert store.set_main(account, "#2PP") is True
            assert store.main_tag(account) == "#2PP"

            # Unverifying the player forgets the main without touching the bot's row.
            with owner.pool.connection() as connection:
                connection.execute(
                    "DELETE FROM verified_player_links WHERE account_id = %s"
                    " AND player_id = (SELECT id FROM players WHERE normalized_tag = '#2PP')",
                    (mine,),
                )
            assert store.main_tag(account) is None

            # Verifying the main again on the same account keeps it.
            assert store.set_main(account, "#8QQ") is True
            _verify(owner, mine, "#8QQ")
            assert store.main_tag(account) == "#8QQ"
            # A main moved to another account and back is forgotten.
            _verify(owner, theirs, "#8QQ")
            assert store.main_tag(account) is None
            _verify(owner, mine, "#8QQ")
            assert store.main_tag(account) is None
            # The API role may run the player page and live board reads too.
            page = store.player_page("#8QQ", NOW)
            (card,) = [card for card in store.players(account, NOW) if card["tag"] == "#8QQ"]
            assert (page["tag"], page["state"], page["reason"]) == (
                "#8QQ", card["state"], card["reason"]
            )
            store.live_rank("#8QQ", NOW)
        finally:
            bot.close()
            owner.close()
