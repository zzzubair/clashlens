"""The bot's reads and its one write, run as the API's database role."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from domain_test_support import as_api_role, domain_database
from test_api_db_public_ops import NOW, seed_profile

from bot.commands import Commands
from bot.replies import Site
from bot.store import Store
from clashlens import api_accounts, api_leaderboard
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


def _set_default(database: ApiDatabase, account, tag: str) -> bool:
    """Choose the default player as the website does, through the private API's
    own save; True when it was this account's verified player."""
    result = api_accounts.set_default_player(
        database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="discord",
            provider_subject=DISCORD_ID,
            account_id=account.internal_id,
            operation="account.update",
            method="POST",
            request_target="/v1/account/default-player",
            identity={"default_player": tag},
        ),
        normalized_tag=tag,
    )
    return result.status_code == 200


def test_bot_lists_every_verified_player_past_500(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            letters = "0289PYLQGRJCUV"
            tags = [
                "#" + "".join(letters[index // 14**place % 14] for place in range(3))
                for index in range(501)
            ]
            with owner.pool.connection() as connection:
                for tag in tags:
                    connection.execute(
                        "INSERT INTO players (normalized_tag, active) VALUES (%s, false)",
                        (tag,),
                    )
            for tag in tags:
                _verify(owner, mine, tag)
            store = Store(bot)
            account = store.account(DISCORD_ID)
            assert account is not None
            cards = store.players(account, NOW)
            assert sorted(card["tag"] for card in cards) == sorted(tags)
        finally:
            bot.close()
            owner.close()


def test_bot_finds_discord_accounts_and_uses_a_default_only_while_verified(
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

            assert store.default_tag(account) is None
            assert _set_default(bot, account, "#9RR") is False
            assert store.default_tag(account) is None
            assert _set_default(bot, account, "#8QQ") is True
            assert store.default_tag(account) == "#8QQ"
            assert _set_default(bot, account, "#2PP") is True
            assert store.default_tag(account) == "#2PP"

            # Unverifying the player forgets the default.
            with owner.pool.connection() as connection:
                connection.execute(
                    "DELETE FROM verified_player_links WHERE account_id = %s"
                    " AND player_id = (SELECT id FROM players WHERE normalized_tag = '#2PP')",
                    (mine,),
                )
            assert store.default_tag(account) is None

            # Verifying the default again on the same account keeps it.
            assert _set_default(bot, account, "#8QQ") is True
            _verify(owner, mine, "#8QQ")
            assert store.default_tag(account) == "#8QQ"
            # A default moved to another account and back is forgotten.
            _verify(owner, theirs, "#8QQ")
            _verify(owner, mine, "#8QQ")
            assert store.default_tag(account) is None
            # So is one unverified and verified again.
            assert _set_default(bot, account, "#8QQ") is True
            with owner.pool.connection() as connection:
                connection.execute(
                    "DELETE FROM verified_player_links"
                    " WHERE player_id = (SELECT id FROM players WHERE normalized_tag = '#8QQ')"
                )
            _verify(owner, mine, "#8QQ")
            assert store.default_tag(account) is None
            # The API role may run the player page and live board reads too.
            page = store.player_page("#8QQ", NOW)
            (card,) = [card for card in store.players(account, NOW) if card["tag"] == "#8QQ"]
            assert (page["tag"], page["state"], page["reason"], page["rank"]) == (
                "#8QQ", card["state"], card["reason"], card["rank"]
            )
        finally:
            bot.close()
            owner.close()


def test_bot_shows_a_player_who_has_not_played_yet_at_5000(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            # Legend I at 5,000 with no Legend battle, in a profile naming
            # Season 0 that the game sends before sign-up and is rejected.
            seed_profile(owner, "#2PP", 5000)
            with owner.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE player_profile_versions
                    SET source_contract_state = 'conflict',
                        current_league_season_id = '0',
                        eligibility_reason = 'confirmed_legend_i'
                    WHERE normalized_tag = '#2PP'
                    """
                )
                connection.execute(
                    "UPDATE players SET current_profile_version_id = NULL"
                    " WHERE normalized_tag = '#2PP'"
                )
                # Today saved with every battle so far recorded: none yet.
                connection.execute(
                    """
                    UPDATE api_player_daily_logs
                    SET coverage = 'complete', partial_reasons = '[]'::jsonb,
                        ranked_day_end = ranked_day_start + interval '1 day',
                        attack_count = 0, attack_three_star_count = 0, attack_gain = 0,
                        defense_count = 0, defense_three_star_count = 0, defense_loss = 0
                    WHERE player_id = (SELECT id FROM players WHERE normalized_tag = '#2PP')
                    """
                )
            account_id = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, account_id, "#2PP")
            store = Store(bot)
            account = store.account(DISCORD_ID)
            assert account is not None

            (card,) = store.players(account, NOW)
            assert (card["reason"], card["trophies"]) == (None, 5000)
            commands = Commands(store, Site("https://clashlens.test"), now=lambda: NOW)
            reply = commands.stats(account)
            assert reply.title is not None and reply.title.startswith("Player #2PP")
            assert "Being checked" not in reply.body
        finally:
            bot.close()
            owner.close()


def test_a_default_saved_during_a_transfer_is_not_kept(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            seed_profile(owner, "#8QQ", 5100)
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            theirs = _account(owner, "google", "google-subject", "other")
            _verify(owner, mine, "#8QQ")
            store = Store(bot)
            account = store.account(DISCORD_ID)
            assert account is not None
            with ThreadPoolExecutor(max_workers=1) as pool:
                with owner.pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            "UPDATE verified_player_links SET account_id = %s"
                            " WHERE player_id = (SELECT id FROM players"
                            " WHERE normalized_tag = '#8QQ')",
                            (theirs,),
                        )
                        saving = pool.submit(_set_default, bot, account, "#8QQ")
                        # The save waits for the transfer instead of reading the old owner.
                        with pytest.raises(TimeoutError):
                            saving.result(timeout=1)
                assert saving.result(timeout=10) is False
            _verify(owner, mine, "#8QQ")
            assert store.default_tag(account) is None
        finally:
            bot.close()
            owner.close()


def test_bot_finds_a_player_who_has_not_played_yet_by_name(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            # Drifter is Legend I at 5,000 with no Legend battle, known only
            # from the rejected Season 0 profile the game sends before sign-up.
            seed_profile(owner, "#8QQ", 5000)
            with owner.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE player_profile_versions
                    SET name = 'Drifter', source_contract_state = 'conflict',
                        current_league_season_id = '0',
                        eligibility_reason = 'confirmed_legend_i'
                    WHERE normalized_tag = '#8QQ'
                    """
                )
                connection.execute(
                    "UPDATE players SET current_profile_version_id = NULL"
                    " WHERE normalized_tag = '#8QQ'"
                )
                api_leaderboard.save_live_unplayed(connection, NOW)
            _account(owner, "discord", DISCORD_ID, "drift")
            store = Store(bot)
            account = store.account(DISCORD_ID)
            assert account is not None
            commands = Commands(store, Site("https://clashlens.test"), now=lambda: NOW)

            found = commands.stats(account, "Drifter")
            assert found.title is not None and found.title.startswith("Drifter #8QQ")
            assert "Being checked" not in found.body
            assert [choice.value for choice in commands.player_choices(DISCORD_ID, "drift")] == [
                "#8QQ"
            ]
        finally:
            bot.close()
            owner.close()


def test_bot_names_and_dates_a_player_by_the_profile_their_page_shows(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            # An accepted 5,000 profile named Drifter II, then a newer Season 0
            # one at 5,000 named Drifter, which the player's page shows.
            seed_profile(owner, "#8QQ", 5000, observed_at=NOW - timedelta(hours=1))
            with owner.pool.connection() as connection:
                connection.execute(
                    "UPDATE player_profile_versions SET name = 'Drifter II'"
                    " WHERE normalized_tag = '#8QQ'"
                )
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag, endpoint_version,
                        schema_version, parser_version, observed_at, source_http_status,
                        name, trophies, league_tier_id, league_tier_name,
                        eligibility_state, current_league_season_id, profile_json,
                        source_contract_state, eligibility_reason
                    )
                    SELECT player_id, observation_id, normalized_tag, endpoint_version,
                           schema_version, 'profile-parser-v2', %s, source_http_status,
                           'Drifter', trophies, league_tier_id, league_tier_name,
                           eligibility_state, '0', profile_json,
                           'conflict', 'confirmed_legend_i'
                    FROM player_profile_versions WHERE normalized_tag = '#8QQ'
                    """,
                    (NOW,),
                )
                api_leaderboard.save_live_unplayed(connection, NOW)
            account_id = _account(owner, "discord", DISCORD_ID, "drift")
            store = Store(bot)
            account = store.account(DISCORD_ID)
            assert account is not None
            commands = Commands(store, Site("https://clashlens.test"), now=lambda: NOW)

            found = commands.stats(account, "Drifter")
            assert found.title is not None and found.title.startswith("Drifter #8QQ")
            assert [choice.label for choice in commands.player_choices(DISCORD_ID, "drift")] == [
                "Drifter #8QQ · 5,000"
            ]
            # The player menu dates the player by the newer profile, read now,
            # not the older one.
            seed_profile(owner, "#2PP", 5100)
            _verify(owner, account_id, "#8QQ")
            _verify(owner, account_id, "#2PP")
            menu = commands.stats(account)
            assert menu.choices and menu.updated == NOW
        finally:
            bot.close()
            owner.close()


def test_lookup_reads_run_as_the_api_role(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        bot = ApiDatabase(as_api_role(connection_info))
        try:
            for tag, trophies in (("#2PP", 5100), ("#8QQ", 5300), ("#9RR", 5200)):
                seed_profile(owner, tag, trophies)
            _account(owner, "discord", DISCORD_ID, "drift")
            store = Store(bot)
            assert store.account(DISCORD_ID) is not None
            assert [item["tag"] for item in store.search("Player #8", NOW)] == ["#8QQ"]
        finally:
            bot.close()
            owner.close()
