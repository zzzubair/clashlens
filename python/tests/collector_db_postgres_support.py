"""Shared setup for collector db postgres tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from clashlens.collector_db import (
    ResponseHandoff,
)

# A historical Friday keeps the following reset outside the Monday season boundary.
NOW = datetime(2020, 9, 11, 4, 0, tzinfo=UTC)


def _hash(byte: str) -> str:
    import hashlib

    return hashlib.sha256(byte.encode("ascii")).hexdigest()


def _player(connection_info: str, tag: str = "#2PP", *, now: datetime) -> int:
    with psycopg.connect(connection_info) as connection:
        return int(
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, next_due_at)
                VALUES (%s, true, %s)
                RETURNING id
                """,
                (tag, now - timedelta(seconds=1)),
            ).fetchone()[0]
        )


def _handoff(
    *,
    occurrence_key: str,
    response_hash: str,
    player_id: int,
    tag: str = "#2PP",
    endpoint: str = "profile",
    completed_at: datetime = NOW,
    collector_work_id: int | None = None,
    content_fingerprint: str | None = None,
    http_status: int = 200,
) -> ResponseHandoff:
    return ResponseHandoff(
        occurrence_key=occurrence_key,
        scope="player",
        identity_key=tag,
        endpoint=endpoint,
        player_id=player_id,
        normalized_tag=tag,
        request_started_at=completed_at - timedelta(seconds=1),
        response_completed_at=completed_at,
        http_status=http_status,
        response_hash=response_hash,
        content_fingerprint=content_fingerprint or response_hash,
        byte_size=1,
        spool_key=f"sha256/{response_hash[:2]}/{response_hash}",
        collector_version="python-collector-test",
        key_label="regular-a",
        evidence_headers={"content-type": "application/json"},
        collector_work_id=collector_work_id,
    )
