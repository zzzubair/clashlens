from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from threading import Event

import psycopg
from domain_test_support import domain_database, store_observation, text
from test_domain_processing_postgres import PROFILE_FIXTURE, _processor

from clashlens import army_ingestion
from clashlens.db import (
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
)
from clashlens.worker import process_until_stopped


def test_real_responses_finish_while_builds_hold_derived_lanes(
    database_url: str, archive_server, monkeypatch
) -> None:
    release_builds = Event()
    lock = threading.Lock()
    builds_started = 0

    def held_build(_database: object, _claim: object) -> None:
        nonlocal builds_started
        with lock:
            builds_started += 1
        assert release_builds.wait(30), "test release gate was not opened"

    monkeypatch.setattr(army_ingestion, "complete_army_analytics", held_build)
    observed_at = datetime.now(UTC) - timedelta(minutes=5)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            for generation in range(1, 21):
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
        database, processor = _processor(
            connection_info,
            archive_server,
            database_factory=lambda info: Database(info, max_size=12),
        )
        stop = Event()
        thread = threading.Thread(
            target=process_until_stopped,
            args=(processor,),
            kwargs={
                "concurrency": 12,
                "owner": "reserved-postgres",
                "lease_seconds": 60,
                "stop_requested": stop,
                "idle_seconds": 0.05,
                "claims_ready": lambda: True,
                "maintain": lambda _turns: None,
                "on_result": lambda _result: None,
            },
            daemon=True,
        )
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while builds_started == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)  # every lane has looked for work at least once
            job_ids = []
            tags = [f"#2{first}{second}" for first in "PYLQ" for second in "GRJCU"]
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
            assert statuses.count("complete") == len(job_ids), statuses
            assert not release_builds.is_set()
            assert builds_started == 1
        finally:
            release_builds.set()
            stop.set()
            thread.join(30)
            database.close()
    assert not thread.is_alive()
