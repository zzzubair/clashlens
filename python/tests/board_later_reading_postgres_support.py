"""Shared setup for board later reading postgres tests."""

from __future__ import annotations

from datetime import datetime

import psycopg


def _hold(connection_info: str, tags: tuple[str, ...], since: datetime) -> None:
    """Leave the players' responses from ``since`` for the worker's next
    round: it saves the others first."""
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            UPDATE python_processing_jobs AS job SET due_at = now() + interval '1 day'
            FROM collector_observations AS observation
            WHERE observation.id = job.observation_id
              AND observation.normalized_tag = ANY(%s)
              AND observation.response_completed_at >= %s
            """,
            (list(tags), since),
        )
