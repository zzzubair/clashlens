from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, timedelta
from itertools import islice, product
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from api_db_public_ops_support import seed_league_history, seed_profile
from api_migration_support import migrated_production_database
from fastapi.testclient import TestClient
from private_api_support import (
    DISCORD_CURRENT,
    NOW,
    NOW_SECONDS,
    TS_CURRENT,
    TS_PREVIOUS,
    json_body,
    signed_headers,
)
from pydantic import ValidationError

from clashlens import api_accounts, api_analytics, api_verification
from clashlens.api import GroupBody, create_app
from clashlens.api_db import ApiDatabase
from clashlens.api_groups import MAX_COMPARED_MEMBERS
from clashlens.api_requests import RequestBinding
from clashlens.army_analytics import ArmyAnalyticsUnavailable, CurrentSeasonEmpty
from clashlens.verification import OfficialVerificationResponse


def test_live_leaderboard_freshness_uses_ten_minutes(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            with database.pool.connection() as connection:
                observed_at = connection.execute(
                    "SELECT current_observed_at FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0].astimezone(UTC)
            current = observed_at
            app = create_app(
                database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: current,
            )
            target = "/v1/leaderboards/live?limit=1"
            with TestClient(app) as client:
                for seconds in (599, 600, 600.5, 720, 900):
                    current = observed_at + timedelta(seconds=seconds)
                    response = client.get(target, headers=signed_headers(target))
                    assert response.status_code == 200
                    board = response.json()
                    stale = seconds > 600
                    [entry] = board["entries"]
                    assert entry["observed_at"] == observed_at.isoformat()
                    assert entry["age_seconds"] == int(seconds)
                    assert entry["freshness"] == ("stale" if stale else "fresh")
                    assert board["source_observations"]["stale_count"] == int(stale)
                    assert board["provenance"]["freshness"] == entry["freshness"]
        finally:
            database.close()


@dataclass
class FakeOfficialVerifier:
    calls: int = 0

    def verify(
        self, normalized_tag: str, player_token: str
    ) -> OfficialVerificationResponse:
        assert normalized_tag == "#2PP"
        assert player_token
        self.calls += 1
        return OfficialVerificationResponse(200, b'{"status":"ok"}')


def test_player_season_routes_publish_only_public_history_fields(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 6000)
            # League history names the Season starting 1781499600 by its end.
            seed_league_history(database, "#2PP", "1783918800")
            app = create_app(
                database=database,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
            )
            with TestClient(app) as client:
                list_target = "/v1/players/%232PP/seasons"
                list_response = client.get(
                    list_target, headers=signed_headers(list_target)
                )
                detail_target = "/v1/players/%232PP/seasons/1781499600"
                detail_response = client.get(
                    detail_target, headers=signed_headers(detail_target)
                )
        finally:
            database.close()

    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert list_response.json()["seasons"][0]["source"] == "official_league_history"
    detail = detail_response.json()
    for bounds in (detail, list_response.json()["seasons"][0]):
        assert bounds["season_start"] == "2026-06-15T05:00:00+00:00"
        assert bounds["season_end"] == "2026-07-13T05:00:00+00:00"
        assert bounds["last_day_start"] == "2026-07-12T05:00:00+00:00"
        assert bounds["day_count"] == 28
    assert detail["official_history"] == {
        "source": "official_league_history",
        "observed_at": "2026-08-06T12:00:00+00:00",
        "eod_trophies": 5812,
        "final_placement": 12,
    }
    serialized = json.dumps([list_response.json(), detail])
    assert "account_id" not in serialized
    assert "provider_subject" not in serialized
    assert "source_json" not in serialized


def test_caller_operation_matrix_google_beta_and_complete_private_operations(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 6000)
        verifier = FakeOfficialVerifier()
        fingerprint = hashlib.sha256(b"safe-synthetic-interactive-key").hexdigest()
        api_verification.register_official_credential(database, fingerprint)
        app = create_app(
            database=database,
            keys={
                ("typescript-website", "current"): TS_CURRENT,
                ("typescript-website", "previous"): TS_PREVIOUS,
                ("discord-bot", "current"): DISCORD_CURRENT,
            },
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            verification_client=verifier,
            official_credential_fingerprint=fingerprint,
        )

        with TestClient(app) as client:
            player_target = "/v1/players/%232PP"
            player = client.get(
                player_target,
                headers=signed_headers(player_target, key_id="previous"),
            )
            discord_player = client.get(
                player_target,
                headers=signed_headers(player_target, caller="discord-bot"),
            )
            search_target = "/v1/players/search?q=Player"
            search = client.get(
                search_target,
                headers=signed_headers(search_target),
            )
            discord_search = client.get(
                search_target,
                headers=signed_headers(search_target, caller="discord-bot"),
            )
            leaderboard_target = "/v1/leaderboards/live?limit=100"
            leaderboard = client.get(
                leaderboard_target,
                headers=signed_headers(leaderboard_target),
            )
            public_user_target = "/v1/users/missinguser"
            public_user = client.get(
                public_user_target,
                headers=signed_headers(public_user_target),
            )
            anonymous_account = client.get(
                "/v1/account",
                headers=signed_headers("/v1/account"),
            )
            discord_identity = client.get(
                player_target,
                headers=signed_headers(
                    player_target,
                    caller="discord-bot",
                    provider="discord",
                    subject="123",
                ),
            )

            assert player.status_code == 200
            assert discord_player.status_code == 403
            assert discord_player.json() == {"error": "caller_operation_not_authorized"}
            assert search.status_code == 200
            assert search.json() == {
                "query": "Player",
                "known_only": True,
                "users": [],
                "results": [
                    {
                        "tag": "#2PP",
                        "name": "Player #2PP",
                        "clan": None,
                        # The August 2026 profile names an earlier Season.
                        "trophies": None,
                        "season_reset_pending": True,
                        "freshness": "stale",
                        "age_seconds": 20_982_400,
                        "observed_at": "2026-08-06T12:00:00+00:00",
                        "public_confidence": "high",
                    }
                ],
            }
            assert discord_search.status_code == 403
            assert discord_search.json() == {"error": "caller_operation_not_authorized"}
            assert leaderboard.status_code == 200
            assert public_user.status_code == 404
            assert anonymous_account.status_code == 403
            assert discord_identity.status_code == 403
            assert discord_identity.json() == {
                "error": "caller_operation_not_authorized"
            }

            create_target = "/v1/account"
            create_data = json_body(
                {"username": "ApiOwner", "display_name": "API Owner"}
            )
            discord_data = json_body(
                {"username": "DiscordOwner", "display_name": "Discord Owner"}
            )
            discord_create = client.post(
                create_target,
                content=discord_data,
                headers=signed_headers(
                    create_target,
                    method="POST",
                    body=discord_data,
                    provider="discord",
                    subject="discord-api-owner",
                ),
            )
            created = client.post(
                create_target,
                content=create_data,
                headers=signed_headers(
                    create_target,
                    method="POST",
                    body=create_data,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            # Phase 1 login providers are exactly Google and Discord; either
            # identity may create an independent Clash Lens account.
            assert discord_create.status_code == 201
            assert discord_create.json() == {
                "username": "discordowner",
                "display_name": "Discord Owner",
                "preferences": {},
                "providers": ["discord"],
            }
            assert created.status_code == 201
            assert created.json()["username"] == "apiowner"

            account = client.get(
                create_target,
                headers=signed_headers(
                    create_target,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            assert account.status_code == 200

            group_data = json_body({"name": "Main", "tags": ["#2PP"]})
            group = client.post(
                "/v1/account/groups",
                content=group_data,
                headers=signed_headers(
                    "/v1/account/groups",
                    method="POST",
                    body=group_data,
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            summary = client.get(
                "/v1/account/summary",
                headers=signed_headers(
                    "/v1/account/summary",
                    provider="google",
                    subject="google-api-owner",
                ),
            )
            assert group.status_code == 201
            assert summary.status_code == 200

            token_request_id = str(uuid4())
            verify_target = "/v1/players/%232PP/verifytoken"
            first_token_body = json_body({"token": "one-time-secret"})
            verified = client.post(
                verify_target,
                content=first_token_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=first_token_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=token_request_id,
                ),
            )
            replay_body = b"not-json-different-new-secret"
            replay = client.post(
                verify_target,
                content=replay_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=replay_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=token_request_id,
                ),
            )
            assert verified.status_code == 200
            assert verified.json() == {"status": "linked", "tag": "#2PP"}
            assert replay.status_code == 200
            assert replay.json() == verified.json()
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            changed_target = "/v1/players/%238PY/verifytoken"
            changed_binding = client.post(
                changed_target,
                content=b"not-json-changed-binding-secret",
                headers=signed_headers(
                    changed_target,
                    method="POST",
                    body=b"not-json-changed-binding-secret",
                    provider="google",
                    subject="google-api-owner",
                    request_id=token_request_id,
                ),
            )
            assert changed_binding.status_code == 409
            assert changed_binding.json() == {"error": "request_id_conflict"}
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            invalid_body = json_body({"token": "must-not-leak", "extra": True})
            invalid_request_id = str(uuid4())
            invalid = client.post(
                verify_target,
                content=invalid_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=invalid_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=invalid_request_id,
                ),
            )
            assert invalid.status_code == 422
            assert invalid.json() == {"error": "invalid_request"}
            assert "must-not-leak" not in invalid.text
            invalid_retry_body = json_body({"token": "must-not-call-source"})
            invalid_retry = client.post(
                verify_target,
                content=invalid_retry_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=invalid_retry_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=invalid_request_id,
                ),
            )
            assert invalid_retry.status_code == 422
            assert invalid_retry.json() == {"error": "invalid_request"}
            assert "must-not-call-source" not in invalid_retry.text
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            owner_context = api_accounts.resolve_account(
                database, "google", "google-api-owner"
            )
            assert owner_context is not None
            crashed_request_id = str(uuid4())
            crashed_binding = RequestBinding(
                request_id=crashed_request_id,
                caller="typescript-website",
                provider="google",
                provider_subject="google-api-owner",
                account_id=owner_context.internal_id,
                operation="player_links.verify",
                method="POST",
                request_target=verify_target,
                identity={"tag": "#2PP"},
            )
            assert api_verification.reserve_verification(
                database, crashed_binding, normalized_tag="#2PP"
            ).fresh
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE private_api_requests
                    SET in_progress_until = clock_timestamp() - interval '1 second'
                    WHERE request_id = %s
                    """,
                    (crashed_request_id,),
                )
                connection.commit()
            crashed_body = json_body({"token": "crashed-source-secret"})
            crashed_reuse = client.post(
                verify_target,
                content=crashed_body,
                headers=signed_headers(
                    verify_target,
                    method="POST",
                    body=crashed_body,
                    provider="google",
                    subject="google-api-owner",
                    request_id=crashed_request_id,
                ),
            )
            assert crashed_reuse.status_code == 503
            assert crashed_reuse.json() == {
                "status": "verification_unavailable",
                "tag": "#2PP",
            }
            assert "crashed-source-secret" not in crashed_reuse.text
            assert verifier.calls == 1
            assert database.scalar("SELECT count(*) FROM shared_api_permits") == 1

            captured = database.scalar(
                """
                SELECT string_agg(surface, ' ') FROM (
                    SELECT identity_json::text AS surface FROM private_api_requests
                    UNION ALL
                    SELECT response_json::text FROM private_api_requests
                    UNION ALL
                    SELECT outcome FROM player_link_verification_audits
                    UNION ALL
                    SELECT state FROM support_player_link_transfer_candidates
                    UNION ALL
                    SELECT operator_identity FROM support_player_link_transfer_audits
                    UNION ALL
                    SELECT reason FROM support_player_link_transfer_audits
                    UNION ALL
                    SELECT input_json::text FROM python_processing_jobs
                ) AS captured_surfaces
                """
            )
            assert "one-time-secret" not in captured
            assert "different-new-secret" not in captured
            assert "must-not-leak" not in captured
            assert "must-not-call-source" not in captured
            assert "changed-binding-secret" not in captured
            assert "crashed-source-secret" not in captured
            assert "body_hash" not in captured
            assert hashlib.sha256(first_token_body).hexdigest() not in captured
            assert hashlib.sha256(replay_body).hexdigest() not in captured
            assert hashlib.sha256(invalid_body).hexdigest() not in captured


def test_linked_elsewhere_requires_one_bounded_support_candidate(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 6000)
        verifier = FakeOfficialVerifier()
        fingerprint = hashlib.sha256(b"safe-support-candidate-key").hexdigest()
        api_verification.register_official_credential(database, fingerprint)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            verification_client=verifier,
            official_credential_fingerprint=fingerprint,
        )
        try:
            with TestClient(app) as client:
                create_target = "/v1/account"
                first_body = json_body(
                    {"username": "FirstOwner", "display_name": "First Owner"}
                )
                second_body = json_body(
                    {"username": "SecondOwner", "display_name": "Second Owner"}
                )
                for body, subject in (
                    (first_body, "google-first-owner"),
                    (second_body, "google-second-owner"),
                ):
                    response = client.post(
                        create_target,
                        content=body,
                        headers=signed_headers(
                            create_target,
                            method="POST",
                            body=body,
                            provider="google",
                            subject=subject,
                        ),
                    )
                    assert response.status_code == 201
                first = api_accounts.resolve_account(
                    database, "google", "google-first-owner"
                )
                second = api_accounts.resolve_account(
                    database, "google", "google-second-owner"
                )
                assert first is not None and second is not None
                verify_target = "/v1/players/%232PP/verifytoken"
                first_token = json_body({"token": "first-owner-secret"})
                first_verified = client.post(
                    verify_target,
                    content=first_token,
                    headers=signed_headers(
                        verify_target,
                        method="POST",
                        body=first_token,
                        provider="google",
                        subject="google-first-owner",
                    ),
                )
                assert first_verified.json() == {"status": "linked", "tag": "#2PP"}
                with database.pool.connection() as connection:
                    connection.execute(
                        "DELETE FROM shared_api_permits WHERE credential_fingerprint = %s",
                        (fingerprint,),
                    )
                    connection.commit()
                second_token = json_body({"token": "second-owner-secret"})
                second_verified = client.post(
                    verify_target,
                    content=second_token,
                    headers=signed_headers(
                        verify_target,
                        method="POST",
                        body=second_token,
                        provider="google",
                        subject="google-second-owner",
                    ),
                )

                assert second_verified.status_code == 409
                support_required = second_verified.json()
                assert set(support_required) == {
                    "status",
                    "tag",
                    "verification_request_id",
                }
                assert support_required["status"] == "support_required"
                assert support_required["tag"] == "#2PP"
                assert (
                    str(UUID(support_required["verification_request_id"]))
                    == support_required["verification_request_id"]
                )
                assert (
                    database.scalar("SELECT account_id FROM verified_player_links")
                    == first.internal_id
                )
                with database.pool.connection() as connection:
                    candidate = connection.execute(
                        """
                        SELECT state, from_account_id, to_account_id,
                               expires_at - verified_at = interval '15 minutes'
                        FROM support_player_link_transfer_candidates
                        WHERE verification_request_id = %s
                        """,
                        (support_required["verification_request_id"],),
                    ).fetchone()
                    candidate_count_row = connection.execute(
                        "SELECT count(*) FROM support_player_link_transfer_candidates"
                    ).fetchone()
                assert candidate is not None and candidate_count_row is not None
                candidate_state = (
                    candidate[0].decode("utf-8")
                    if isinstance(candidate[0], bytes)
                    else candidate[0]
                )
                assert candidate_state == "pending"
                assert candidate[1:] == (first.internal_id, second.internal_id, True)
                assert candidate_count_row[0] == 1
                assert verifier.calls == 2
        finally:
            database.close()


def test_signed_army_analytics_http_contract_preserves_auth_and_error_details(
    database_url: str,
    monkeypatch,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        payload = {
            "kind": "army-analytics",
            "total_attacks": 1,
            "usable_army_sample": 1,
            "army_states": {"fully_decoded": 1},
            "army_states_sum_confirmed": True,
            "unknown_affected_attacks": 0,
            "unknown_component_occurrences": 0,
            "perspective_disagreement_count": 0,
            "missing_trophy_membership_evidence": 0,
            "cohort_evidence": {
                "cohort_players": 1,
                "stale_or_uncertain_cohort_members": 0,
                "streak_excluded_players": 0,
                "shielded_player_days": 0,
            },
            "collection_coverage": {"state": "complete", "completed_days": 1},
            "freshness": {"state": "frozen"},
            "reproducibility": {
                "official_season_id": "2026-08",
                "legend_days": [1, 1],
                "snapshot_versions": [1],
            },
            "versions": {
                "decoder": "army-decoder-v2",
                "catalog": "unit-catalog-v1",
                "analytics": "army-analytics-v2",
            },
            "publication_identity": "army-publication-test-v1",
            "selection": {
                "lens": "offense",
                "season": "2026-08",
                "start_day": 1,
                "end_day": 1,
                "population": "top-100",
                "category": "troops",
                "sort": "usage-rate",
            },
            "rows": [],
        }
        requested = []
        monkeypatch.setattr(
            api_analytics,
            "get_army_analytics",
            lambda _database, _selection, now=None, examples=None: (
                requested.append(examples) or payload
            ),
        )
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        target = "/v1/analytics/armies?season=2026-08"
        with TestClient(app) as client:
            unauthorized = client.get(target)
            assert unauthorized.status_code == 401
            success = client.get(target, headers=signed_headers(target))
            assert success.status_code == 200
            assert success.json() == payload
            # Opening a variant asks for its examples: Revenge Thrower is
            # variant 3 of type 3, and 0 is a type's Other.
            examples = f"{target}&category=army-types&type_id=3&variant_id=3"
            assert client.get(examples, headers=signed_headers(examples)).status_code == 200
            assert requested == [None, (3, 3)]
            for invalid_target in (
                "/v1/analytics/armies",
                "/v1/analytics/armies?season=2026-08&lens=invalid",
                f"{target}&category=army-types&type_id=3&variant_id=999",
                f"{target}&category=army-types&type_id=999&variant_id=0",
                f"{target}&category=troops&type_id=3&variant_id=3",
            ):
                invalid = client.get(
                    invalid_target, headers=signed_headers(invalid_target)
                )
                assert invalid.status_code == 422

            monkeypatch.setattr(
                api_analytics,
                "get_army_analytics",
                lambda _database, _selection, now=None, examples=None: (_ for _ in ()).throw(
                    ArmyAnalyticsUnavailable([2, 4])
                ),
            )
            unavailable = client.get(target, headers=signed_headers(target))
            assert unavailable.status_code == 404
            assert unavailable.json()["affected_days"] == [2, 4]

            monkeypatch.setattr(
                api_analytics,
                "get_army_analytics",
                lambda _database, _selection, now=None, examples=None: (_ for _ in ()).throw(
                    CurrentSeasonEmpty("2026-07")
                ),
            )
            empty = client.get(
                "/v1/analytics/armies?season=current",
                headers=signed_headers("/v1/analytics/armies?season=current"),
            )
            assert empty.status_code == 404
            assert empty.json() == {
                "error": "no_completed_legend_days",
                "previous_season_id": "2026-07",
                "calendar": {"as_of": NOW.isoformat(), "day_start": "2027-04-06T05:00:00+00:00",
                             "day_end": "2027-04-07T05:00:00+00:00", "season_id": "1805691600",
                             "season_start": "2027-03-22T05:00:00+00:00",
                             "season_end": "2027-04-19T05:00:00+00:00", "day_number": 16, "day_count": 28},
            }

            def cancelled(setting: str):
                def read(_database, _selection, now=None, examples=None):
                    with database.pool.connection() as connection:
                        with connection.transaction():
                            connection.execute(f"SET LOCAL {setting} = '50ms'")
                            connection.execute("SELECT pg_sleep(1)")

                return read

            for setting in ("statement_timeout", "transaction_timeout"):
                monkeypatch.setattr(
                    api_analytics, "get_army_analytics", cancelled(setting)
                )
                timed_out = client.get(target, headers=signed_headers(target))
                assert timed_out.status_code == 503, setting
                assert timed_out.json() == {"error": "timeout"}, setting


def test_public_player_read_p95_is_below_200_milliseconds(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        seed_profile(database, "#2PP", 6000)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        target = "/v1/players/%232PP"
        durations = []
        with TestClient(app) as client:
            for _index in range(100):
                started = perf_counter()
                response = client.get(target, headers=signed_headers(target))
                durations.append((perf_counter() - started) * 1000)
                assert response.status_code == 200
        p95 = sorted(durations)[94]
        assert p95 < 200, f"saved player read p95 was {p95:.2f} ms"


def test_leaderboard_rejects_misaligned_selectors_and_missing_pages(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        try:
            with TestClient(app) as client:
                for target in (
                    "/v1/leaderboards/live?limit=100&offset=1",
                    "/v1/leaderboards/frozen?official_season_id=2026-08",
                ):
                    assert (
                        client.get(target, headers=signed_headers(target)).status_code
                        == 422
                    )
                missing = "/v1/leaderboards/live?limit=100&offset=100"
                assert (
                    client.get(missing, headers=signed_headers(missing)).status_code
                    == 404
                )
        finally:
            database.close()


def test_saving_a_group_refuses_more_players_than_a_comparison_shows() -> None:
    GroupBody(name="Main", tags=["#2PP"] * MAX_COMPARED_MEMBERS)
    with pytest.raises(ValidationError):
        GroupBody(name="Main", tags=["#2PP"] * (MAX_COMPARED_MEMBERS + 1))


@pytest.mark.parametrize(("method", "target"), [
    ("GET", "/v1/battles/1/army?perspective=attacker"),
    ("GET", "/v1/analytics/basic"),
    ("POST", "/v1/account/exports"),
    ("GET", "/v1/account/exports/00000000-0000-4000-8000-000000000001"),
])
def test_removed_routes_are_not_available(database_url: str, method: str, target: str) -> None:
    app = create_app(
        database=ApiDatabase(database_url),
        keys={("typescript-website", "current"): TS_CURRENT},
        clock=lambda: NOW_SECONDS,
    )
    with TestClient(app) as client:
        response = client.request(
            method, target,
            headers=signed_headers(target, method=method),
        )
    assert response.status_code == 404


def test_account_metadata_matches_database_and_http_acceptance(database_url: str) -> None:
    from api_db_organization_support import create_owner

    contract = json.loads((Path(__file__).parents[2] / "testdata/w6-domain-contract.json").read_text())
    with migrated_production_database(database_url, include_compact_collector=True) as info:
        database = ApiDatabase(info)
        try:
            create_owner(database)
            app = create_app(database, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: NOW)
            with TestClient(app) as client:
                def call(target, value=None, method="GET"):
                    body = b"" if value is None else json_body(value)
                    return client.request(method, target, content=body, headers=signed_headers(
                        target, method=method, body=body, provider="google", subject="group-owner-subject"))

                listing = call("/v1/account/groups").json()
                assert listing["groups"] == []
                assert listing["limits"] == contract["groups"]
                assert listing["calendar"]["as_of"] == NOW.isoformat()
                assert listing["calendar"]["day_start"] == "2027-04-06T05:00:00+00:00"
                tags = ["#" + "".join(chars) for chars in islice(product("289LYQ", repeat=3), 21)]
                too_many = call("/v1/account/groups", {"name": "Too many", "tags": tags}, "POST")
                assert too_many.status_code == 422
                for index in range(listing["limits"]["max_groups"]):
                    created = call("/v1/account/groups", {"name": f"Group {index}", "tags": tags[:20]}, "POST")
                    assert created.status_code == 201, created.text
                    group_id = created.json()["group_id"]
                listing = call("/v1/account/groups").json()
                assert len(listing["groups"]) == listing["limits"]["max_groups"] == 10
                assert all(len(group["tags"]) == listing["limits"]["max_players"] == 20 for group in listing["groups"])
                assert call("/v1/account/groups", {"name": "Eleventh", "tags": []}, "POST").json() == {
                    "error": "group_limit_reached"}
                for days in listing["limits"]["comparison_days"]:
                    compared = call(f"/v1/account/groups/{group_id}/comparison?days={days}")
                    assert compared.status_code == 200, compared.text
                    assert compared.json()["limits"] == listing["limits"]
                    assert compared.json()["calendar"] == listing["calendar"]
                    assert len(compared.json()["day_starts"]) == days
                assert call(f"/v1/account/groups/{group_id}/comparison?days=4").status_code == 422
                overlay = call("/v1/account/obs-overlay").json()
                settings = overlay["settings_contract"]
                assert settings == contract["overlay"]
                assert settings["defaults"] == overlay["settings"]
                assert settings["choices"]["bg"] == ["card", "none", "tint"]
                assert settings["numeric_max"] == 4000
                linked = call(f"/v1/overlays/{overlay['link']}").json()
                assert linked["settings_contract"] == settings
                board = client.get("/v1/leaderboards/live", headers=signed_headers("/v1/leaderboards/live")).json()
                assert board["entries"] == []
                assert board["focus_neighbors"] == 5
                assert board["calendar"] == listing["calendar"]
        finally:
            database.close()


def test_refresh_display_states_are_served_without_changing_raw_status(database_url: str) -> None:
    contract = json.loads((Path(__file__).parents[2] / "testdata/w6-domain-contract.json").read_text())
    with migrated_production_database(database_url, include_compact_collector=True) as info:
        database = ApiDatabase(info)
        try:
            seed_profile(database, "#2PP", 5300)
            app = create_app(database, keys={("typescript-website", "current"): TS_CURRENT},
                             clock=lambda: NOW_SECONDS, now=lambda: NOW)
            with TestClient(app) as client:
                target = "/v1/players/%232PP/refresh"
                request_id = str(uuid4())
                headers = signed_headers(target, method="POST", request_id=request_id)
                submitted = client.post(target, headers=headers)
                assert submitted.status_code == 202, submitted.text
                assert submitted.json()["display_state"] == "queued"
                refresh = submitted.json()
                assert client.post(target, headers=headers).json() == refresh
                with database.pool.connection() as connection:
                    saved = connection.execute("SELECT response_json FROM private_api_requests WHERE request_id = %s", (request_id,)).fetchone()[0]
                    assert "display_state" not in saved
                target = f"/v1/refreshes/{refresh['refresh_id']}"
                # Compact collector work is unleased; its schema admits these five raw states.
                for raw in ("pending", "waiting_retry", "complete", "failed", "cancelled"):
                    with database.pool.connection() as connection:
                        connection.execute("UPDATE collector_work SET status = %s, completed_at = CASE WHEN %s = 'complete' THEN clock_timestamp() ELSE NULL END", (raw, raw))
                    response = client.get(target, headers=signed_headers(target))
                    assert response.status_code == 200, response.text
                    assert response.json() == {**refresh, "status": raw, "display_state": contract["refresh"][raw]}
                player = "/v1/players/%232PP"
                assert client.get(player, headers=signed_headers(player)).json()["tag"] == refresh["tag"]
                missing = f"/v1/refreshes/{uuid4()}"
                assert client.get(missing, headers=signed_headers(missing)).status_code == 404
                malformed = "/v1/refreshes/not-a-uuid"
                assert client.get(malformed, headers=signed_headers(malformed)).status_code == 422
        finally:
            database.close()

    for raw, display in contract["refresh"].items():
        assert api_accounts.refresh_display_state(raw) == display
    with pytest.raises(KeyError):
        api_accounts.refresh_display_state("invented")
