"""Shared setup for reset settlement state postgres tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import psycopg
from domain_test_support import (
    store_observation,
)
from reconciliation_postgres_support import (
    DAY_END,
    _processor,
    _profile,
)

from clashlens import (
    league_decider,
    reset_baselines,
)

TAG = "#2PP"


# The fixture profiles report the Season that ends at the August 10 Reset.
BOUNDARIES = {
    "ordinary": DAY_END,
    "monday": datetime(2026, 8, 3, 5, tzinfo=UTC),
    "season": datetime(2026, 8, 10, 5, tzinfo=UTC),
    "season_day_2": datetime(2026, 8, 11, 5, tzinfo=UTC),
}


def _reset_work(connection_info, archive_server, boundary, *, profile=None,
                log=None, profile_status=200, status="complete",
                profile_at=None, log_at=None) -> list[int]:
    """Save a Reset sweep's responses for one player; return their jobs."""
    ids, jobs = {}, []
    for endpoint, body, http_status, observed_at in (
        ("profile", profile, profile_status, profile_at or boundary),
        ("battle_log", log, 200, log_at or boundary),
    ):
        if body is not None:
            ids[endpoint], job = store_observation(
                connection_info, archive_server,
                occurrence_key=f"{boundary.isoformat()}-{endpoint}",
                endpoint=endpoint, body=body, observed_at=observed_at,
                normalized_tag=TAG, http_status=http_status,
            )
            jobs.append(job)
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO players (normalized_tag) VALUES (%s) ON CONFLICT DO NOTHING",
            (TAG,),
        )
        connection.execute(
            """
            WITH player AS (SELECT id FROM players WHERE normalized_tag = %(tag)s),
            sweep AS (
                INSERT INTO collector_reset_sweeps (
                    boundary_at, member_ids, membership_captured_at
                ) SELECT %(at)s, ARRAY[player.id], %(at)s FROM player RETURNING id
            )
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                profile_observation_id, battle_log_observation_id, completed_at
            ) SELECT 'reset_baseline', 'reset', 'player', player.id, %(tag)s,
                     sweep.id, %(at)s, 'reset-' || %(at)s::text, %(status)s,
                     %(profile_status)s, %(log_status)s, %(profile)s, %(log)s,
                     CASE WHEN %(status)s = 'complete' THEN %(at)s END
              FROM player, sweep
            """,
            {
                "tag": TAG, "at": boundary, "status": status,
                "profile": ids.get("profile"), "log": ids.get("battle_log"),
                "profile_status": "observed" if profile else "failed",
                "log_status": "observed" if log else "failed",
            },
        )
    return jobs


def _process(connection_info, archive_server, jobs) -> None:
    database, processor = _processor(connection_info, archive_server)
    try:
        for job_id in jobs:
            assert processor.process_job(job_id, owner=f"job-{job_id}") is not None
        if not jobs:
            reset_baselines.settle_failed_reset_work(database)
        while True:
            with database.pool.connection() as connection:
                pending = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                    " AND work_type = 'reconcile_ranked_day' ORDER BY id LIMIT 1"
                ).fetchone()
            if pending is None:
                # The worker's league week thread decides the weeks the
                # work marked, queueing the days a decision changes.
                if league_decider.decide_due(database):
                    continue
                return
            processor.process_job(int(pending[0]), owner="reconcile")
    finally:
        database.close()


def _season_profile(trophies: int, season_id: int) -> bytes:
    payload = json.loads(_profile(trophies))
    payload["currentLeagueSeasonId"] = season_id
    payload["previousLeagueSeasonId"] = season_id - 28 * 24 * 60 * 60
    return json.dumps(payload).encode()


OLD_SEASON, NEW_SEASON = 1783918800, 1786338000  # Seasons around August 10.
