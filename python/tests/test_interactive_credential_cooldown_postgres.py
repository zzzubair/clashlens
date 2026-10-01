from __future__ import annotations

import hashlib
from uuid import uuid4

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens import api_verification
from clashlens.api_db import ApiDatabase
from clashlens.collector_db import CollectorDatabase


def test_collector_429_cooldown_blocks_api_verification_and_is_audited(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        fingerprint = hashlib.sha256(b"shared-interactive-fixture").hexdigest()
        collector = CollectorDatabase(connection_info)
        api = ApiDatabase(connection_info)
        try:
            collector.register_interactive_key(fingerprint)

            assert collector.cooldown_interactive_key(fingerprint, 120) is True
            collector.register_interactive_key(fingerprint, starts_per_second=29)
            api_verification.register_official_credential(api, fingerprint)
            permit = api_verification.acquire_official_permit(
                api, fingerprint, request_id=str(uuid4())
            )

            assert permit.granted is False
            assert permit.reason == "credential_cooldown"
            with psycopg.connect(connection_info) as connection:
                credential = connection.execute(
                    """
                    SELECT state,
                           extract(epoch FROM cooldown_until - clock_timestamp())
                    FROM shared_api_credentials
                    WHERE credential_fingerprint = %s
                    """,
                    (fingerprint,),
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT event_type, actor, reason,
                           cooldown_until IS NOT NULL
                    FROM shared_api_credential_events
                    WHERE credential_fingerprint = %s
                    ORDER BY id DESC LIMIT 1
                    """,
                    (fingerprint,),
                ).fetchone()
            assert credential is not None
            assert credential[0] == "cooldown"
            assert 110 <= float(credential[1]) <= 120
            assert event == (
                "cooldown",
                "python-collector",
                "provider_http_429",
                True,
            )
        finally:
            api.close()
            collector.close()


@pytest.mark.parametrize("seconds", [0, 301])
def test_collector_cooldown_rejects_unbounded_duration(seconds: int) -> None:
    collector = object.__new__(CollectorDatabase)

    with pytest.raises(ValueError, match="cooldown"):
        collector.cooldown_interactive_key("a" * 64, seconds)


@pytest.mark.parametrize("rate", [0, 30, 31])
def test_collector_registration_rejects_unsafe_rates(rate: int) -> None:
    collector = object.__new__(CollectorDatabase)

    with pytest.raises(ValueError, match="between 1 and 29"):
        collector.register_interactive_key("a" * 64, starts_per_second=rate)


def test_lowering_shared_rate_preserves_outstanding_permissions(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        fingerprint = hashlib.sha256(b"shared-rate-change-fixture").hexdigest()
        collector = CollectorDatabase(connection_info)
        api = ApiDatabase(connection_info)
        try:
            collector.register_interactive_key(fingerprint, starts_per_second=29)
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    INSERT INTO shared_api_permits (credential_fingerprint, caller)
                    SELECT %s, 'collector' FROM generate_series(1, 25)
                    """,
                    (fingerprint,),
                )
            collector.register_interactive_key(fingerprint, starts_per_second=25)
            api_verification.register_official_credential(api, fingerprint)

            assert collector.acquire_collector_permit(fingerprint).granted is False
            verification = api_verification.acquire_official_permit(
                api, fingerprint, request_id=str(uuid4())
            )
            assert verification.granted is False
            assert verification.reason == "combined_budget_exhausted"
            assert api.scalar("SELECT count(*) FROM shared_api_permits") == 25

            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE shared_api_permits SET permitted_at = clock_timestamp() - interval '1 second'"
                )
            assert collector.acquire_collector_permit(fingerprint).granted is True
            assert api_verification.acquire_official_permit(
                api, fingerprint, request_id=str(uuid4())
            ).granted is True
        finally:
            api.close()
            collector.close()
