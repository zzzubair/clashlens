"""Six-month, last-seen expiry using immutable location tombstones.

Run only on the collector host with its exact shared spool and a separate
operator credential. Never configure an upload-age bucket lifecycle instead.
"""
from __future__ import annotations

import re
from typing import Any


def retire_archive_objects(
    connection: Any, spool: Any, client: Any, *, bucket: str,
    instance_id: str, max_objects: int = 100, apply: bool = False,
) -> dict[str, int | bool]:
    if not instance_id or not bucket or not 1 <= max_objects <= 1000:
        raise ValueError("archive instance, bucket and a 1..1000 object batch are required")
    if not connection.autocommit:
        raise ValueError("archive retirement requires an autocommit operator connection")
    isolation = connection.execute("SHOW transaction_isolation").fetchone()[0]
    if isolation not in ("read committed", b"read committed"):
        raise ValueError("archive retirement requires READ COMMITTED isolation")
    candidates = connection.execute(
        """
        SELECT response_hash, archive_reference FROM archive_catalogue
        WHERE archive_instance_id = %s AND (
            availability = 'retiring' OR
            (availability = 'verified' AND last_seen_before < clock_timestamp() - interval '6 months')
        ) ORDER BY last_seen_before, archive_reference LIMIT %s
        """, (instance_id, max_objects),
    ).fetchall()
    retired = protected = eligible = 0
    for digest, reference in candidates:
        digest = digest.decode() if isinstance(digest, bytes) else digest
        reference = reference.decode() if isinstance(reference, bytes) else reference
        prefix = f"s3://{bucket}/sha256/{digest[:2]}/{digest}"
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(
            re.escape(prefix) + r"(?:/generation/[0-9a-f]{32})?", reference
        ):
            raise ValueError("archive retirement location is outside the configured hash namespace")
        with spool.lock(digest, exclusive=True):
            with connection.transaction():
                connection.execute("SET LOCAL lock_timeout = '1s'")
                connection.execute("SET LOCAL statement_timeout = '30s'")
                # Fence new replay references and changes to existing jobs before
                # checking activity. The trigger rejects new work after retirement.
                connection.execute(
                    "SELECT id FROM collector_observations WHERE archive_reference = %s ORDER BY id FOR UPDATE",
                    (reference,),
                ).fetchall()
                jobs = connection.execute(
                    """
                    SELECT p.status FROM python_processing_jobs AS p
                    JOIN collector_observations AS o
                      ON o.id = COALESCE(p.observation_id, p.replay_observation_id)
                    WHERE o.archive_reference = %s ORDER BY p.id FOR UPDATE OF p
                    """, (reference,),
                ).fetchall()
                pending = connection.execute(
                    """
                    SELECT EXISTS (SELECT 1 FROM collector_endpoint_results
                        WHERE archive_reference = %s AND outcome = 'pending_remote_verification')
                    """, (reference,),
                ).fetchone()[0]
                if pending or any(row[0] not in ("complete", b"complete", "cancelled", b"cancelled") for row in jobs):
                    protected += 1
                    continue
                current = connection.execute(
                    """
                    SELECT availability FROM archive_catalogue
                    WHERE response_hash = %s AND archive_reference = %s
                      AND archive_instance_id = %s AND (
                        availability = 'retiring' OR
                        (availability = 'verified' AND last_seen_before < clock_timestamp() - interval '6 months')
                      ) FOR UPDATE
                    """, (digest, reference, instance_id),
                ).fetchone()
                if current is None:
                    continue
                eligible += 1
                if not apply:
                    continue
                connection.execute(
                    "UPDATE archive_catalogue SET availability = 'retiring' WHERE archive_reference = %s",
                    (reference,),
                )
            # Commit the tombstone BEFORE requesting deletion. A crash or unknown
            # DELETE leaves it retiring. Collection uses a different generation;
            # retrying this deletion can therefore never remove the new bytes.
            if apply:
                client.remove_object(bucket, reference.removeprefix(f"s3://{bucket}/"))
                connection.execute(
                    "UPDATE archive_catalogue SET availability = 'expired' WHERE archive_reference = %s AND availability = 'retiring'",
                    (reference,),
                )
                retired += 1
    return {"apply": apply, "eligible_objects": eligible, "protected_objects": protected, "retired_objects": retired}
