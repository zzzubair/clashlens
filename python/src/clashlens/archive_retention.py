"""Season-based expiry using immutable location tombstones.

A raw response becomes due 86 days after the 28-day season containing its
latest sighting ends; a body still observed in a later season keeps that
season's deadline instead. A due response is first marked 'retiring', which
blocks every new use, and its bytes are deleted only after the promised
seven-day recovery window plus a two-day restore allowance. A restore to any
promised point therefore still finds every byte its catalogue calls usable.
Run only on the collector host with its exact shared spool and a separate
operator credential. Never configure an upload-age bucket lifecycle instead.
"""
from __future__ import annotations

import re
import sys
from typing import Any

RECOVERY_WINDOW_DAYS = 7
RESTORE_ALLOWANCE_DAYS = 2
RECOVERY_HOLD = f"{RECOVERY_WINDOW_DAYS + RESTORE_ALLOWANCE_DAYS} days"

# Pending uploads and unfinished processing or replay keep a response usable.
_ACTIVE = """
    EXISTS (SELECT 1 FROM collector_response_uploads AS u
        WHERE u.response_hash = c.response_hash
          AND u.archive_reference = c.archive_reference AND u.state <> 'complete')
    OR EXISTS (SELECT 1 FROM collector_observations AS o
        JOIN python_processing_jobs AS p
          ON o.id = COALESCE(p.observation_id, p.replay_observation_id)
        WHERE o.archive_reference = c.archive_reference
          AND p.status NOT IN ('complete', 'cancelled'))
"""
_DUE = "c.availability = 'verified' AND c.retire_after <= clock_timestamp()"
_HELD = "c.availability = 'retiring' AND c.retiring_since > clock_timestamp() - %s::interval"
_RELEASED = "c.availability = 'retiring' AND c.retiring_since <= clock_timestamp() - %s::interval"


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _summary(connection: Any, instance_id: str) -> dict[str, Any]:
    """Totals across the whole catalogue, for the preview report."""
    groups = {
        "due_unprotected": (f"{_DUE} AND NOT ({_ACTIVE})", "c.retire_after", ()),
        "due_protected": (f"{_DUE} AND ({_ACTIVE})", "c.retire_after", ()),
        "held_for_recovery": (_HELD, "c.retiring_since", (RECOVERY_HOLD,)),
        "deletable_now": (_RELEASED, "c.retiring_since", (RECOVERY_HOLD,)),
    }
    summary = {}
    for name, (condition, clock, parameters) in groups.items():
        objects, size, oldest, newest = connection.execute(
            f"""
            SELECT count(*), COALESCE(sum(c.byte_size), 0), min({clock}), max({clock})
            FROM archive_catalogue AS c WHERE c.archive_instance_id = %s AND {condition}
            """, (instance_id, *parameters),
        ).fetchone()
        summary[name] = {
            "objects": objects, "bytes": int(size),
            "oldest": oldest and oldest.isoformat(), "newest": newest and newest.isoformat(),
        }
    return summary


def retire_archive_objects(
    connection: Any, spool: Any, client: Any, *, bucket: str,
    instance_id: str, max_objects: int = 100, apply: bool = False,
) -> dict[str, Any]:
    """Delete up to max_objects held responses, then mark up to max_objects due ones.

    Safe to re-run: each object is decided under its own lock and transaction,
    and a failed delete stays 'retiring' for the next run to retry.
    """
    if not instance_id or not bucket or not 1 <= max_objects <= 1000:
        raise ValueError("archive instance, bucket and a 1..1000 object batch are required")
    if not connection.autocommit:
        raise ValueError("archive retirement requires an autocommit operator connection")
    isolation = connection.execute("SHOW transaction_isolation").fetchone()[0]
    if isolation not in ("read committed", b"read committed"):
        raise ValueError("archive retirement requires READ COMMITTED isolation")
    report: dict[str, Any] = {
        "apply": apply, "recovery_hold": RECOVERY_HOLD,
        "deleted_objects": 0, "deleted_bytes": 0, "marked_objects": 0, "marked_bytes": 0,
        "protected_objects": 0, "failed_objects": 0,
    }
    if not apply:
        report["summary"] = _summary(connection, instance_id)

    def location(digest: Any, reference: Any) -> tuple[str, str]:
        digest, reference = _text(digest), _text(reference)
        prefix = f"s3://{bucket}/sha256/{digest[:2]}/{digest}"
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(
            re.escape(prefix) + r"(?:/generation/[0-9a-f]{32})?", reference
        ):
            raise ValueError("archive retirement location is outside the configured hash namespace")
        return digest, reference

    def failed(reference: str, error: Exception) -> None:
        # Exception text from storage or database clients can carry request
        # details; log only its type and the object's location.
        report["failed_objects"] += 1
        print(f"prune-archive: {type(error).__name__} for {reference}", file=sys.stderr)

    released = connection.execute(
        f"""
        SELECT c.response_hash, c.archive_reference, c.byte_size FROM archive_catalogue AS c
        WHERE c.archive_instance_id = %s AND {_RELEASED}
        ORDER BY c.retiring_since, c.archive_reference LIMIT %s
        """, (instance_id, RECOVERY_HOLD, max_objects),
    ).fetchall()
    for digest, reference, size in released:
        digest, reference = location(digest, reference)
        try:
            with spool.lock(digest, exclusive=True):
                # Recheck: the location must still be this instance's held
                # tombstone. Collection never reuses a retiring location.
                current = connection.execute(
                    f"""
                    SELECT 1 FROM archive_catalogue AS c
                    WHERE c.response_hash = %s AND c.archive_reference = %s
                      AND c.archive_instance_id = %s AND {_RELEASED}
                    """, (digest, reference, instance_id, RECOVERY_HOLD),
                ).fetchone()
                if current is None:
                    continue
                if apply:
                    client.remove_object(bucket, reference.removeprefix(f"s3://{bucket}/"))
                    connection.execute(
                        "UPDATE archive_catalogue SET availability = 'expired' WHERE archive_reference = %s AND availability = 'retiring'",
                        (reference,),
                    )
            report["deleted_objects"] += 1
            report["deleted_bytes"] += size
        except Exception as error:  # noqa: BLE001 - one object never stops the batch
            failed(reference, error)

    due = connection.execute(
        f"""
        SELECT c.response_hash, c.archive_reference, c.byte_size FROM archive_catalogue AS c
        WHERE c.archive_instance_id = %s AND {_DUE} AND NOT ({_ACTIVE})
        ORDER BY c.retire_after, c.archive_reference LIMIT %s
        """, (instance_id, max_objects),
    ).fetchall()
    for digest, reference, size in due:
        digest, reference = location(digest, reference)
        try:
            with spool.lock(digest, exclusive=True), connection.transaction():
                connection.execute("SET LOCAL lock_timeout = '1s'")
                connection.execute("SET LOCAL statement_timeout = '30s'")
                # Fence new replay references and changes to existing jobs before
                # checking activity. The trigger rejects new work after retirement.
                connection.execute(
                    "SELECT id FROM collector_observations WHERE archive_reference = %s ORDER BY id FOR UPDATE",
                    (reference,),
                ).fetchall()
                connection.execute(
                    """
                    SELECT p.id FROM python_processing_jobs AS p
                    JOIN collector_observations AS o
                      ON o.id = COALESCE(p.observation_id, p.replay_observation_id)
                    WHERE o.archive_reference = %s ORDER BY p.id FOR UPDATE OF p
                    """, (reference,),
                ).fetchall()
                current = connection.execute(
                    f"""
                    SELECT ({_ACTIVE}) FROM archive_catalogue AS c
                    WHERE c.response_hash = %s AND c.archive_reference = %s
                      AND c.archive_instance_id = %s AND {_DUE} FOR UPDATE
                    """, (digest, reference, instance_id),
                ).fetchone()
                if current is None:
                    continue
                if current[0]:
                    report["protected_objects"] += 1
                    continue
                if apply:
                    connection.execute(
                        """
                        UPDATE archive_catalogue
                        SET availability = 'retiring', retiring_since = clock_timestamp()
                        WHERE archive_reference = %s
                        """, (reference,),
                    )
            report["marked_objects"] += 1
            report["marked_bytes"] += size
        except Exception as error:  # noqa: BLE001 - one object never stops the batch
            failed(reference, error)
    return report
