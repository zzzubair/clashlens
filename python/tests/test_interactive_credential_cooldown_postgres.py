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
