"""The named states of a Reset's publication: its Daily board and army
results (``ArtifactState``), each member (``MemberStatus``), each correction
(``CorrectionState``), each frozen input list (``ManifestStage``) and each
army decode (``DecodeVerdict``).

The database holds every one of these states, with CHECK constraints listing
the same values and triggers that keep published rows immutable; these types
name them for the Python code that moves them, and a test keeps both lists
equal. Each Reset's publication lock (``boundary.lock_boundary_publication``)
serialises the moves, so a crash or retry leaves the last committed state and
the next publication re-check carries on from it.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from .army_decoder import DecodedArmy, DecodeFailure
from .db import _text_value


class ArtifactState(StrEnum):
    """Where one generation's board (``snapshot_state``) or army results
    (``army_state``) stand.

    PENDING: members are still waiting for their day results.
    READY: every member is settled; the inputs freeze into a manifest and the
    build is queued (``boundary._try_enqueue_boundary_artifacts``).
    BUILDING: a board build saved the board; its statistics are still being
    built (``snapshots``). Army builds go from READY straight to PUBLISHED.
    PUBLISHED: served. SUPERSEDED: a newer generation of the same Reset
    replaced it; what it published stays saved. FAILED: allowed by the
    database, never written by the current code.

    A correction makes a new generation, PENDING again for each affected
    artifact and a copy of the old state for the others. A failed build job
    is queued again with growing waits (``board_deadline.retry_failed_build``)
    and moves nothing.
    """

    PENDING = "pending"
    READY = "ready"
    BUILDING = "building"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    FAILED = "failed"


# The moves the code makes; a supersession may come from any state.
ARTIFACT_MOVES: dict[ArtifactState, frozenset[ArtifactState]] = {
    ArtifactState.PENDING: frozenset({ArtifactState.READY, ArtifactState.SUPERSEDED}),
    ArtifactState.READY: frozenset({
        ArtifactState.BUILDING, ArtifactState.PUBLISHED, ArtifactState.SUPERSEDED}),
    ArtifactState.BUILDING: frozenset({ArtifactState.PUBLISHED, ArtifactState.SUPERSEDED}),
    ArtifactState.PUBLISHED: frozenset({ArtifactState.SUPERSEDED}),
    ArtifactState.SUPERSEDED: frozenset(),
    ArtifactState.FAILED: frozenset(),
}


class MemberStatus(StrEnum):
    """A board member's ``snapshot_status`` or ``army_status``.

    PENDING until the member's day result is saved, then the result's state
    (COMPLETE, PARTIAL, INCONSISTENT, MALFORMED), MISSING with no usable day,
    FAILED when the day's calculation failed for good after the Reset, or
    UNAVAILABLE when a board deadline left the member out
    (``board_deadline``). A settled member changes only in a new generation.
    """

    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    INCONSISTENT = "inconsistent"
    MALFORMED = "malformed"


# Every member status, as an SQL list.
MEMBER_STATUSES_SQL = ",".join(f"'{status}'" for status in MemberStatus)


class CorrectionState(StrEnum):
    """Where one correction to a published Reset stands.

    QUEUED: a changed input was saved for the Reset; later changes join it.
    PENDING_INPUTS: a day it carries is not saved yet; it is tried again on
    the next publication re-check. ACTIVATION: its replacement generation is
    being created; INHERITANCE: the members and unaffected artifacts are being
    copied into it. These two happen inside one transaction. ACTIVE: its
    generation is building. FINALIZED: that generation published. TERMINAL:
    allowed by the database, never written by the current code.

    A waiting correction starts only once the Reset's previous generation has
    published, no repair holds the Reset, and the pace allows it
    (``past_reset_pacing.CorrectionPace``); one at a time per Reset.
    """

    QUEUED = "queued"
    PENDING_INPUTS = "pending_inputs"
    ACTIVATION = "activation"
    INHERITANCE = "inheritance"
    ACTIVE = "active"
    FINALIZED = "finalized"
    TERMINAL = "terminal"


# A day result a correction may carry into a board.
SETTLED_DAY_STATES = frozenset({
    "Complete", "Partial", "Failed", "Missing", "Unavailable", "Inconsistent", "Malformed",
})


def move_correction(connection: Any, correction_id: int, state: CorrectionState) -> None:
    connection.execute(
        "UPDATE boundary_publication_corrections SET state = %s WHERE id = %s",
        (state.value, correction_id),
    )


def pending_inputs_settled(connection: Any, pending_inputs: Iterable[Any]) -> bool:
    """Whether every day a correction carries is saved as a settled result."""
    for pending in pending_inputs:
        version = pending.get("ranked_day_version_id") if isinstance(pending, dict) else None
        if version is None:
            continue
        state = connection.execute(
            "SELECT state FROM ranked_day_versions WHERE id = %s", (version,)
        ).fetchone()
        if state is None or _text_value(state[0]) not in SETTLED_DAY_STATES:
            return False
    return True


class ManifestStage(StrEnum):
    """What one freeze of a generation's inputs did.

    REUSED: the generation already has a manifest of this kind; it is
    returned as it was, since its rows and digest are immutable. BACKING_OFF:
    an earlier freeze of it timed out, and the next waits a minute, then
    twice as long after each further timeout, at most an hour. FROZEN: the
    rows were assembled, saved and sealed in one transaction, and the
    generation names the manifest; its build reads exactly those rows.
    TIMED_OUT: the freeze ran past the query limit; it rolled back, and the
    timeout is saved as a failed build for the alert check and the backoff.
    """

    REUSED = "reused"
    BACKING_OFF = "backing_off"
    FROZEN = "frozen"
    TIMED_OUT = "timed_out"


class DecodeStatus(StrEnum):
    """A saved army decode's ``status``: DECODED, PARTIAL (saved before
    unnamed ids were read back with the rest) or FAILED."""

    DECODED = "decoded"
    PARTIAL = "partial"
    FAILED = "failed"


class DecodeVerdict(StrEnum):
    """What a fresh decode of one battle side means for its saved decode.

    MISSING: no active decode was made from this evidence and code, so one
    is saved. CURRENT: the active decode already records this result
    (a PARTIAL row counts for any successful decode), so nothing is saved.
    OUTDATED: the same evidence now decodes differently, so a new decode
    replaces it. A new or replaced decode marks its Reset's army results for
    a correction; their old facts stop being current when the new ones are
    saved.
    """

    MISSING = "missing"
    CURRENT = "current"
    OUTDATED = "outdated"


def decode_verdict(
    active: Any, evidence_id: int, raw_code: Any, decoded: DecodedArmy | DecodeFailure,
) -> DecodeVerdict:
    """The verdict for the side's newest active decode row (id, evidence_id,
    raw_code, identity_hash, status, failure_category) against a fresh decode.

    A row saved under any unit list counts: saved armies hold ids, not names,
    so a unit list change saves nothing again.
    """
    if (
        active is None
        or int(active[1]) != int(evidence_id)
        or (active[2] is None) != (raw_code is None)
        or raw_code is not None and _text_value(active[2]) != _text_value(raw_code)
    ):
        return DecodeVerdict.MISSING
    if isinstance(decoded, DecodedArmy):
        status = DecodeStatus(_text_value(active[4]))
        current = status is DecodeStatus.PARTIAL or (
            status is DecodeStatus.DECODED
            and _text_value(active[3]) == decoded.identity_hash)
    else:
        current = _text_value(active[5]) == decoded.category
    return DecodeVerdict.CURRENT if current else DecodeVerdict.OUTDATED
