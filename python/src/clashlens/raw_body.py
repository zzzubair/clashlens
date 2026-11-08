"""Where one raw response's local copy stands, from the spool to the archive."""

from __future__ import annotations

from enum import StrEnum


class RawBodyStage(StrEnum):
    """Where the spool's copy of one raw response (``sha256/<hash>``) stands.

    - ``HANDED_OFF``: a sidecar in ``.handoff/`` names it, so the database has
      not committed it yet. Nothing deletes it. After a crash, restart
      recovery commits every sidecar in the order received before collection
      resumes (``collector_commits.SavedResponses.recover``).
    - ``RECORDED``: the database needs it (``RECORDED_SQL``): an upload not yet
      deleted locally, or an observation with no archived copy. Processing and
      the archive upload finish independently, in either order.
    - ``DELETABLE``: its upload is complete and every job processing it is
      complete (``DELETABLE_SQL``). Cleanup deletes it and sets the upload's
      ``local_deleted_at``.
    - ``UNREFERENCED``: nothing names it: an unchanged response the database
      compacted away, or a body a crash left before its sidecar. The
      once-a-minute sweep deletes it.

    Events: saving it with its sidecar (``Spool.publish_handoff``) gives
    ``HANDED_OFF``; the commit removes the sidecar, giving ``RECORDED`` or, for
    a compacted response, ``UNREFERENCED``; the upload and processing finishing
    give ``DELETABLE``. The stage is derived from the files and rows each time
    and never stored. A sidecar always wins, so bytes a newer sighting saved
    again stay even when the database says the older copy may go. Deletion
    runs under the spool's publication barrier (``PublicationBarrier``), so no
    save races it. If a database restore forgets a finished upload whose copy
    was deleted, the uploader reads it back from the archive
    (``uploader.Uploader._archived_copy``).
    """

    HANDED_OFF = "handed_off"
    RECORDED = "recorded"
    DELETABLE = "deletable"
    UNREFERENCED = "unreferenced"


def raw_body_stage(
    *, handed_off: bool, recorded: bool, finished: bool = False
) -> RawBodyStage:
    """``finished``: the database says its upload and processing are complete."""
    if handed_off:
        return RawBodyStage.HANDED_OFF
    if finished:
        return RawBodyStage.DELETABLE
    if recorded:
        return RawBodyStage.RECORDED
    return RawBodyStage.UNREFERENCED


def may_delete(stage: RawBodyStage) -> bool:
    return stage in {RawBodyStage.DELETABLE, RawBodyStage.UNREFERENCED}


# Hashes whose local copy the database still needs: RECORDED or DELETABLE.
# Two single-table reads, no join, so stale statistics leave the planner no
# join to get wrong. After the 7 October 2026 Reset the planner expected 1 kept
# upload, found 4,287, and rescanned every response-state row for each (286
# million rows, about 60 s). That state branch added nothing: a copy is only
# marked deleted while complete, and every path away from complete clears the
# mark, so its rows all have no mark already.
RECORDED_SQL = """
    SELECT response_hash
    FROM collector_response_uploads
    WHERE local_deleted_at IS NULL
    UNION
    SELECT response_hash
    FROM collector_observations
    WHERE archive_reference IS NULL
"""


# Whether processing not finished yet still reads an upload's (aliased
# ``upload``) bytes.
STILL_PROCESSING_SQL = """EXISTS (
    SELECT 1 FROM python_processing_jobs AS job
    JOIN collector_observations AS observation ON observation.id = job.observation_id
    WHERE job.work_type = 'process_observation' AND job.status <> 'complete'
      AND observation.response_hash = upload.response_hash)"""


# Upload rows (aliased ``upload``) whose local copy is DELETABLE.
DELETABLE_SQL = f"""
    upload.state = 'complete'
    AND upload.local_deleted_at IS NULL
    AND NOT {STILL_PROCESSING_SQL}
"""
