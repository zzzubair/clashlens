"""Whether new data is reaching players, for the website's delayed-updates notice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .api_db import ApiDatabase

# Normal processing takes about a second; a busy Reset can take ten minutes or more.
DELAY_SECONDS = 900


def get_update_status(database: ApiDatabase, *, now: datetime) -> dict[str, Any]:
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT (SELECT max(last_success_at) FROM collector_response_state
                    WHERE scope = 'player' AND endpoint IN ('profile', 'battle_log')),
                   (SELECT min(created_at) FROM python_processing_jobs
                    WHERE work_type IN ('process_observation', 'reconcile_ranked_day')
                      AND status IN (
                          'pending', 'waiting_retry', 'waiting_dependency', 'leased'
                      ))
            """
        ).fetchone()
    now = now.astimezone(UTC)
    limit = now - timedelta(seconds=DELAY_SECONDS)
    last_success, oldest_waiting_saved = (
        None if value is None else value.astimezone(UTC) for value in row
    )
    return {
        "kind": "update-status",
        "checked_at": now.isoformat(),
        "delay_seconds": DELAY_SECONDS,
        # No answer at all counts as delayed only once something was ever collected.
        "collection_delayed": last_success is not None and last_success < limit,
        "last_collected_at": None if last_success is None else last_success.isoformat(),
        "processing_delayed": (
            oldest_waiting_saved is not None and oldest_waiting_saved < limit
        ),
        "oldest_waiting_saved_at": (
            None if oldest_waiting_saved is None else oldest_waiting_saved.isoformat()
        ),
    }
