"""Shared setup for worker reserved capacity postgres tests."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import (
    PROFILE_FIXTURE,
    store_observation,
    text,
)

from clashlens.job_contracts import (
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)


def _queue_builds(connection_info: str, count: int) -> None:
    with psycopg.connect(connection_info) as connection:
        for generation in range(1, count + 1):
            connection.execute(
                """
                INSERT INTO python_processing_jobs (
                    work_type, deduplication_key, input_json, priority,
                    processing_version, domain_rule_version,
                    analytics_rule_version, parser_version
                ) VALUES ('build_army_analytics', %s, %s::jsonb, 100,
                          %s, %s, %s, 'supercell-source-parser-v1')
                """,
                (
                    f"held-build:{generation}",
                    json.dumps(
                        {
                            "generation": generation,
                            "manifest_id": generation,
                            "manifest_digest": "a" * 64,
                        }
                    ),
                    PROCESSING_VERSION,
                    DOMAIN_RULE_VERSION,
                    ARMY_ANALYTICS_RULE_VERSION,
                ),
            )


def _queue_profiles(connection_info: str, archive_server, tags: list[str]) -> list[int]:
    observed_at = datetime.now(UTC) - timedelta(minutes=5)
    job_ids = []
    for index, tag in enumerate(tags):
        payload = json.loads(PROFILE_FIXTURE.read_bytes())
        payload["tag"] = tag
        job_ids.append(
            store_observation(
                connection_info,
                archive_server,
                occurrence_key=f"reserved-profile-{index}",
                endpoint="profile",
                body=json.dumps(payload).encode(),
                observed_at=observed_at,
                normalized_tag=payload["tag"],
            )[1]
        )
    return job_ids


def _wait_for_statuses(connection_info: str, job_ids: list[int]) -> list[str]:
    statuses: list[str] = []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with psycopg.connect(connection_info) as connection:
            statuses = [
                text(row[0])
                for row in connection.execute(
                    "SELECT status FROM python_processing_jobs WHERE id = ANY(%s)",
                    (job_ids,),
                )
            ]
        if statuses.count("complete") == len(job_ids):
            break
        time.sleep(0.1)
    return statuses
