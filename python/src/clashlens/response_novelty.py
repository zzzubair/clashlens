"""Unchanged-response confirmation and deduplication: whether a collected
response is new evidence or confirms the one already saved.

Each player (or the global ranking) and endpoint has one
``collector_response_state`` row naming the last saved observation and the
fingerprint of the fields Clash Lens uses (response_fields.py). Each response
locks that row and is classified once, by `response_novelty`:

NEW: no row yet, the row's observation was pruned (a work row answered by
no observation could never finish), or the used fields changed. It becomes a new observation, processing job and upload, and the
row points at it. NEEDS_OWN_PROOF: the used fields match, but the response
must stand as its own evidence: a Reset pair or settlement check answer, a
Monday's first profile after the league display window (league_week), a
discovery profile whose league the last one left unrecognized, or a Refresh
of an untracked ineligible player. It is saved as NEW is. UNCHANGED: the
used fields match and nothing needs its own proof. It confirms the saved
observation (``CollectorDatabase._record_unchanged``): the row counts the
request and its occurrence, the kept body's sighting and archive retention
move later, and any work row is answered by the saved observation. No new
observation, job or upload is made. When confirming would wait on a row
another transaction holds, or the archived copy is being retired, it is saved
as NEW instead (from the spool), or, on the spool-skipping path
(``record_unchanged_response``), nothing is recorded and the caller saves it.

A later changed response is NEW again. A response whose occurrence key the
row already applied was saved before a crash or retry; ``_record_response``
returns that earlier result before classifying. Nothing here is stored
besides the row, so a restart classifies the same way. The collector also
remembers the fingerprint it last committed per endpoint
(``Collector._committed``) only to decide whether to try skipping the spool.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, NamedTuple

from . import league_week


class ResponseNovelty(Enum):
    NEW = "new"
    NEEDS_OWN_PROOF = "needs_own_proof"
    UNCHANGED = "unchanged"


class SavedResponse(NamedTuple):
    """One locked ``collector_response_state`` row."""

    response_hash: str
    seen_at: datetime
    observation_id: int | None
    occurrence_key: str
    # Rows upgraded before this column existed hold the old byte hash.
    content_fingerprint: str | None
    applied_occurrence_key: str | None


def lock_saved_response(connection: Any, handoff: Any) -> SavedResponse | None:
    """Lock and read the state row of a ``collector_db.ResponseHandoff``."""
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{handoff.scope}:{handoff.identity_key}:{handoff.endpoint}",),
    )
    row = connection.execute(
        """
        SELECT last_response_hash, last_seen_at, last_observation_id,
               last_occurrence_key, last_content_fingerprint,
               last_applied_occurrence_key
        FROM collector_response_state
        WHERE scope = %s AND identity_key = %s AND endpoint = %s
        FOR UPDATE
        """,
        (handoff.scope, handoff.identity_key, handoff.endpoint),
    ).fetchone()
    return None if row is None else SavedResponse(*row)


def response_novelty(
    connection: Any,
    handoff: Any,
    saved: SavedResponse | None,
    work_kind: str | None,
) -> ResponseNovelty:
    if (
        saved is None
        or saved.observation_id is None
        # Matching the response hash also counts, for upgraded rows.
        or saved.content_fingerprint
        not in (handoff.content_fingerprint, handoff.response_hash)
    ):
        return ResponseNovelty.NEW
    if work_kind in {
        "reset_baseline",
        "reset_settlement",
    } or league_week.saves_unchanged_answer(connection, handoff, saved.observation_id):
        return ResponseNovelty.NEEDS_OWN_PROOF
    # Discovery reprocesses an unrecognized league; a Refresh re-decides an
    # untracked player's (league_week).
    if (
        work_kind in {"discovery_profile", "live_refresh"}
        and handoff.endpoint == "profile"
        and connection.execute(
            "SELECT CASE WHEN %s THEN clashlens_profile_observation_unrecognized(%s) ELSE (SELECT NOT active AND eligibility_state = 'ineligible' FROM players WHERE id = %s) END",
            (work_kind == "discovery_profile", saved.observation_id, handoff.player_id),
        ).fetchone()[0]
    ):
        return ResponseNovelty.NEEDS_OWN_PROOF
    return ResponseNovelty.UNCHANGED
