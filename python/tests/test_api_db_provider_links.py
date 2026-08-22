from __future__ import annotations

import threading
from uuid import uuid4

import psycopg
from test_api_migration import migrated_production_database

from clashlens.api_db import ApiDatabase, OperationResult, RequestBinding


def binding(
    *,
    request_id: str | None = None,
    subject: str = "google-subject-one",
    provider: str = "google",
    operation: str = "providers.link",
    account_id: int | None = None,
) -> RequestBinding:
    return RequestBinding(
        request_id=request_id or str(uuid4()),
        caller="typescript-website",
        provider=provider,
        provider_subject=subject,
        account_id=account_id,
        operation=operation,
        method="POST" if "link" in operation else "DELETE",
        request_target=f"/v1/account/providers/{provider}",
        identity={"provider": provider},
    )


def _create_account(
    database: ApiDatabase,
    *,
    provider: str,
    subject: str,
    username: str,
) -> int:
    result = database.create_account(
        binding(provider=provider, subject=subject, operation="account.create"),
        username=username.title(),
        normalized_username=username,
        display_name=username.title(),
    )
    assert result.status_code == 201
    context = database.resolve_account(provider, subject)
    assert context is not None
    return context.internal_id


def test_link_adds_second_provider_and_resolves_through_both(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = _create_account(
                database, provider="google", subject="g-sub-1", username="playerone"
            )

            linked = database.link_provider(
                binding(subject="d-sub-1", provider="discord"),
                account_id=account_id,
                provider="discord",
                provider_subject="d-sub-1",
            )
            assert linked.status_code == 200
            assert linked.payload == {"providers": ["discord", "google"]}

            assert database.resolve_account("discord", "d-sub-1") is not None
            google_account = database.resolve_account("google", "g-sub-1")
            assert google_account is not None
            assert google_account.internal_id == account_id

            replay = database.link_provider(
                binding(request_id=str(uuid4()), subject="d-sub-1", provider="discord"),
                account_id=account_id,
                provider="discord",
                provider_subject="d-sub-1",
            )
            assert replay.status_code == 200
            assert replay.payload == {"providers": ["discord", "google"]}
        finally:
            database.close()


def test_link_refuses_identity_owned_by_another_account(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            first = _create_account(
                database, provider="google", subject="g-sub-1", username="playerone"
            )
            second = _create_account(
                database, provider="discord", subject="d-sub-2", username="playertwo"
            )

            conflict = database.link_provider(
                binding(subject="d-sub-2", provider="discord"),
                account_id=first,
                provider="discord",
                provider_subject="d-sub-2",
            )
            assert conflict.status_code == 409
            assert conflict.payload == {"error": "provider_identity_conflict"}

            # Neither account changed hands or grew identities.
            assert database.resolve_account("discord", "d-sub-2") is not None
            assert database.resolve_account("discord", "d-sub-2").internal_id == second
        finally:
            database.close()


def test_concurrent_links_of_one_subject_return_a_safe_conflict(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_ids = [
                _create_account(
                    database,
                    provider="google",
                    subject=f"g-sub-{index}",
                    username=f"player{index}",
                )
                for index in (1, 2)
            ]
            results: list[OperationResult | None] = [None, None]
            start = threading.Barrier(2)

            def link(index: int) -> None:
                pool = ApiDatabase(connection_info)
                try:
                    start.wait()
                    results[index] = pool.link_provider(
                        binding(subject="d-sub-race", provider="discord"),
                        account_id=account_ids[index],
                        provider="discord",
                        provider_subject="d-sub-race",
                    )
                finally:
                    pool.close()

            threads = [threading.Thread(target=link, args=(index,)) for index in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert all(isinstance(result, OperationResult) for result in results)
            operation_results = [
                result for result in results if isinstance(result, OperationResult)
            ]
            assert sorted(result.status_code for result in operation_results) == [200, 409]
            assert next(
                result for result in operation_results if result.status_code == 409
            ).payload == {"error": "provider_identity_conflict"}
        finally:
            database.close()


def test_link_refuses_second_subject_for_held_provider(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = _create_account(
                database, provider="google", subject="g-sub-1", username="playerone"
            )
            conflict = database.link_provider(
                binding(subject="g-sub-other", provider="google"),
                account_id=account_id,
                provider="google",
                provider_subject="g-sub-other",
            )
            assert conflict.status_code == 409
            assert conflict.payload == {"error": "provider_identity_conflict"}
        finally:
            database.close()


def test_unlink_removes_only_the_reauthenticated_identity_and_keeps_the_account(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = _create_account(
                database, provider="google", subject="g-sub-1", username="playerone"
            )
            assert (
                database.link_provider(
                    binding(subject="d-sub-1", provider="discord"),
                    account_id=account_id,
                    provider="discord",
                    provider_subject="d-sub-1",
                ).status_code
                == 200
            )

            removed = database.unlink_provider(
                binding(subject="d-sub-1", provider="discord", operation="providers.unlink"),
                account_id=account_id,
                provider="discord",
                provider_subject="d-sub-1",
            )
            assert removed.status_code == 200
            assert removed.payload == {"providers": ["google"]}
            assert database.resolve_account("discord", "d-sub-1") is None
            assert database.resolve_account("google", "g-sub-1") is not None

            # Unlinking an identity that is no longer linked fails safely.
            missing = database.unlink_provider(
                binding(
                    request_id=str(uuid4()),
                    subject="d-sub-1",
                    provider="discord",
                    operation="providers.unlink",
                ),
                account_id=account_id,
                provider="discord",
                provider_subject="d-sub-1",
            )
            assert missing.status_code == 404
            assert missing.payload == {"error": "provider_not_linked"}
        finally:
            database.close()


def test_unlink_refuses_the_final_linked_identity(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = _create_account(
                database, provider="google", subject="g-sub-1", username="playerone"
            )
            refused = database.unlink_provider(
                binding(subject="g-sub-1", provider="google", operation="providers.unlink"),
                account_id=account_id,
                provider="google",
                provider_subject="g-sub-1",
            )
            assert refused.status_code == 409
            assert refused.payload == {"error": "final_provider"}
            assert database.resolve_account("google", "g-sub-1") is not None
        finally:
            database.close()


def test_concurrent_unlinks_of_both_providers_cannot_empty_the_account(
    database_url: str,
) -> None:
    """Two simultaneous unlinks race on separate connections; the per-account
    row lock serializes them, so exactly one removal can ever succeed."""
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = _create_account(
                database, provider="google", subject="g-sub-race", username="racer"
            )
            assert (
                database.link_provider(
                    binding(subject="d-sub-race", provider="discord"),
                    account_id=account_id,
                    provider="discord",
                    provider_subject="d-sub-race",
                ).status_code
                == 200
            )

            results: dict[str, OperationResult] = {}
            start = threading.Barrier(2)

            def unlink(provider: str, subject: str) -> None:
                pool = ApiDatabase(connection_info)
                try:
                    start.wait()
                    results[provider] = pool.unlink_provider(
                        binding(
                            subject=subject,
                            provider=provider,
                            operation="providers.unlink",
                        ),
                        account_id=account_id,
                        provider=provider,
                        provider_subject=subject,
                    )
                finally:
                    pool.close()

            threads = [
                threading.Thread(target=unlink, args=("google", "g-sub-race")),
                threading.Thread(target=unlink, args=("discord", "d-sub-race")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            google_result = results["google"]
            discord_result = results["discord"]
            statuses = sorted((google_result.status_code, discord_result.status_code))
            # One unlink wins; the loser must see the post-removal provider set.
            assert statuses == [200, 409]
            winner_payload = (
                google_result.payload if google_result.status_code == 200 else discord_result.payload
            )
            assert len(winner_payload["providers"]) == 1  # type: ignore[index]

            with psycopg.connect(connection_info) as connection:
                remaining = connection.execute(
                    """
                    SELECT count(*) FROM account_provider_identities
                    WHERE account_id = %s
                    """,
                    (account_id,),
                ).fetchone()
            assert remaining is not None and int(remaining[0]) == 1
        finally:
            database.close()


def test_concurrent_recovery_collision_retains_refusal_audit(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_ids = [
                _create_account(
                    database,
                    provider="google",
                    subject=f"recovery-google-{index}",
                    username=f"recover{index}",
                )
                for index in (1, 2)
            ]
            public_ids = [_public_id(connection_info, account_id) for account_id in account_ids]
            with psycopg.connect(connection_info) as connection:
                for index, account_id in enumerate(account_ids, start=1):
                    tag = f"#P{index}P"
                    player_id = connection.execute(
                        "INSERT INTO players (normalized_tag, active) VALUES (%s, false) RETURNING id",
                        (tag,),
                    ).fetchone()[0]
                    request_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO private_api_requests (
                            request_id, caller, provider, provider_subject, account_id,
                            operation, method, request_target, identity_json, state,
                            response_status, response_json, completed_at
                        ) VALUES (
                            %s, 'typescript-website', 'google', %s, %s,
                            'player_links.verify', 'POST', %s,
                            '{}'::jsonb, 'complete', 200, '{}'::jsonb, clock_timestamp()
                        )
                        """,
                        (request_id, f"recovery-google-{index}", account_id, f"/v1/players/{tag}/verifytoken"),
                    )
                    connection.execute(
                        """
                        INSERT INTO verified_player_links (
                            player_id, account_id, verification_request_id
                        ) VALUES (%s, %s, %s)
                        """,
                        (player_id, account_id, request_id),
                    )

            results: list[tuple[str, str] | None] = [None, None]
            start = threading.Barrier(2)

            def recover(index: int) -> None:
                pool = ApiDatabase(connection_info)
                try:
                    start.wait()
                    results[index] = pool.support_attach_discord_identity(
                        account_public_id=public_ids[index],
                        normalized_player_tag=f"#P{index + 1}P",
                        discord_subject="1234567890123456789",
                        operator_identity=f"operator-{index}",
                        reason="verified support recovery race",
                    )
                finally:
                    pool.close()

            threads = [threading.Thread(target=recover, args=(index,)) for index in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert all(isinstance(result, tuple) for result in results)
            statuses = sorted(result[0] for result in results if isinstance(result, tuple))
            assert statuses == ["attached", "refused_collision"]
            with psycopg.connect(connection_info) as connection:
                refused = connection.execute(
                    """
                    SELECT count(*) FROM provider_identity_audits
                    WHERE action = 'support_recovery' AND result = 'refused_collision'
                    """
                ).fetchone()
            assert refused is not None and int(refused[0]) == 1
        finally:
            database.close()


def test_provider_events_leave_audits_without_duplicating_subjects(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = _create_account(
                database, provider="google", subject="g-sub-1", username="playerone"
            )
            database.link_provider(
                binding(subject="d-sub-1", provider="discord"),
                account_id=account_id,
                provider="discord",
                provider_subject="d-sub-1",
            )
            database.unlink_provider(
                binding(subject="d-sub-1", provider="discord", operation="providers.unlink"),
                account_id=account_id,
                provider="discord",
                provider_subject="d-sub-1",
            )
            status, detail = database.support_attach_discord_identity(
                account_public_id=_public_id(connection_info, account_id),
                normalized_player_tag="#PLAYERTAG",
                discord_subject="d-sub-support",
                operator_identity="maintainer",
                reason="maintainer-assisted recovery after token proof",
            )
            # The player is not verified on the account in this scenario.
            assert status == "player_not_verified_on_account"
            del detail

            with psycopg.connect(connection_info) as connection:
                rows = connection.execute(
                    """
                    SELECT provider, action, result, reason
                    FROM provider_identity_audits
                    ORDER BY id
                    """
                ).fetchall()
                columns = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT column_name FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'provider_identity_audits'
                        """
                    )
                }
            assert [(row[0], row[1], row[2]) for row in rows] == [
                ("discord", "link", "succeeded"),
                ("discord", "unlink", "succeeded"),
                # The mismatched player/account recovery attempt is audited
                # as a failed recovery after target-account resolution.
                ("discord", "support_recovery", "failed"),
            ]
            assert all(row[3] for row in rows)
            assert "provider_subject" not in columns
        finally:
            database.close()


def _public_id(connection_info: str, account_id: int) -> str:
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            "SELECT public_id FROM clash_lens_accounts WHERE id = %s", (account_id,)
        ).fetchone()
        assert row is not None
        return str(row[0])
