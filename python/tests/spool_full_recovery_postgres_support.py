"""Shared setup for spool full recovery postgres tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import PROFILE_FIXTURE

from clashlens.collector_db import CollectorDatabase, ResponseHandoff
from clashlens.spool import Spool

NOW = datetime.now(UTC).replace(microsecond=0)


TAGS = ("#2PP", "#2PY")


def _profile(tag: str) -> bytes:
    payload = json.loads(PROFILE_FIXTURE.read_bytes())
    payload["tag"] = tag
    return json.dumps(payload).encode()


def _save(
    connection_info: str, database: CollectorDatabase, spool: Spool, tag: str
) -> str:
    """Save one profile the way the collector does: spool file, then database."""
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "INSERT INTO players (normalized_tag, active, next_due_at)"
            " VALUES (%s, true, %s) RETURNING id",
            (tag, NOW),
        ).fetchone()[0]
    body = _profile(tag)
    digest = hashlib.sha256(body).hexdigest()
    with spool.reservation() as reservation:
        reservation.publish(body, digest)
    database.record_response(
        ResponseHandoff(
            occurrence_key=f"full-spool-{tag}",
            scope="player",
            identity_key=tag,
            endpoint="profile",
            player_id=player_id,
            normalized_tag=tag,
            request_started_at=NOW - timedelta(seconds=1),
            response_completed_at=NOW,
            http_status=200,
            response_hash=digest,
            content_fingerprint=digest,
            byte_size=len(body),
            spool_key=f"sha256/{digest[:2]}/{digest}",
            collector_version="python-collector-test",
            key_label="regular-a",
            evidence_headers={"content-type": "application/json"},
        )
    )
    return digest
