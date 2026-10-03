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
from clashlens.worker import ProcessResult, process_until_stopped


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
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _queue_builds(connection_info, 20)
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
            tags = [f"#2{first}{second}" for first in "PYLQ" for second in "GRJCU"]
            job_ids = _queue_profiles(connection_info, archive_server, tags)
            statuses = _wait_for_statuses(connection_info, job_ids)
            assert statuses.count("complete") == len(job_ids), statuses
            assert not release_builds.is_set()
            assert builds_started == 1
        finally:
            release_builds.set()
            stop.set()
            thread.join(30)
            database.close()
    assert not thread.is_alive()


def test_a_lane_that_waits_out_the_pool_recovers_while_a_build_keeps_running(
    database_url: str, archive_server, monkeypatch
) -> None:
    build_running = Event()
    release_build = Event()
    holder: dict[str, Database] = {}

    def held_build(_database: object, _claim: object) -> None:
        # The build holds one of the pool's two connections while it runs.
        with holder["database"].pool.connection():
            build_running.set()
            assert release_build.wait(30), "test release gate was not opened"

    monkeypatch.setattr(army_ingestion, "complete_army_analytics", held_build)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _queue_builds(connection_info, 1)
        database, processor = _processor(
            connection_info,
            archive_server,
            database_factory=lambda info: Database(info, max_size=2),
        )
        holder["database"] = database
        database.pool.timeout = 0.3  # production waits 30 seconds
        failures: list[BaseException] = []
        results: list[ProcessResult] = []
        stop = Event()

        def run() -> None:
            try:
                process_until_stopped(
                    processor,
                    concurrency=2,  # lane 1 takes responses, lane 2 the build
                    owner="pool-busy-postgres",
                    lease_seconds=60,
                    stop_requested=stop,
                    idle_seconds=0.05,
                    claims_ready=lambda: True,
                    maintain=lambda _turns: None,
                    on_result=results.append,
                )
            except BaseException as error:  # noqa: BLE001 - reported below
                failures.append(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        held_connection = None
        try:
            assert build_running.wait(10)
            held_connection = database.pool.getconn(timeout=5)
            job_ids = _queue_profiles(
                connection_info, archive_server, ["#2PGR", "#2PGJ"]
            )
            deadline = time.monotonic() + 10
            while (
                database.pool.get_stats().get("requests_errors", 0) < 2
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert database.pool.get_stats().get("requests_errors", 0) >= 2
            assert thread.is_alive() and not failures

            database.pool.putconn(held_connection)
            held_connection = None
            statuses = _wait_for_statuses(connection_info, job_ids)
            assert statuses.count("complete") == len(job_ids), statuses
            assert not release_build.is_set()

            release_build.set()
            deadline = time.monotonic() + 10
            while len(results) < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert sorted(result.outcome for result in results) == ["processed"] * 3
        finally:
            if held_connection is not None:
                database.pool.putconn(held_connection)
            release_build.set()
            stop.set()
            thread.join(30)
            database.close()
    assert not thread.is_alive()
    assert not failures
