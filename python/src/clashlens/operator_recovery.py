"""Bounded operator visibility, explicit retry, replay and acceptance of failures."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.errors import LockNotAvailable, QueryCanceled, UniqueViolation

_INTEGRITY_FAILURES = {
    "archive_catalogue_contradiction",
    "archive_checksum_mismatch",
    "archive_reference_conflict",
    "upload_metadata_changed",
}
_RESTART_AFTER_REPAIR_FAILURES = {
    "archive_configuration_error",
    "archive_marker_mismatch",
    "archive_permission_denied",
    "archive_reference_mismatch",
    "archive_unsupported",
}

_PROCESSING_JOB = """
    SELECT job.id, job.work_type, job.endpoint, job.status, job.observation_id,
           job.replay_observation_id, job.attempt_count, job.max_attempts,
           job.outcome, job.failure_category, job.due_at, job.updated_at,
           accepted.operator_identity, accepted.reason, accepted.accepted_at,
           job.parser_version, job.processing_version, job.domain_rule_version,
           job.analytics_rule_version
    FROM python_processing_jobs AS job
    LEFT JOIN python_failed_job_acceptances AS accepted ON accepted.job_id = job.id
"""
_OPERATOR = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
# What clashlens_request_python_replay_v2 refuses one job for, by SQLSTATE.
_REQUEST_REFUSALS = {
    "22023": "replay_not_allowed",
    "P0002": "observation_not_found",
    "23505": "replay_requested_with_a_different_reason",
    "42501": "replay_role_unavailable",
}
# What stops the whole batch.
_BATCH_REFUSALS = {
    "42501": "replay_role_unavailable",
    "55P03": "replay_lock_timeout",
    "57014": "replay_statement_timeout",
}


def failed_items_command(arguments: Any, database_url: str) -> dict[str, Any]:
    """Run `failed-items`: list, or preview/apply a retry, replay or acceptance."""
    selected = (
        arguments.work_id,
        arguments.upload_hash,
        arguments.accept_job_id,
        arguments.replay_job_id,
    )
    if arguments.apply and selected == (None, None, None, None):
        raise ValueError(
            "--apply requires --work-id, --upload-hash, --accept-job-id or --replay-job-id"
        )
    if arguments.upload_hash is not None and not re.fullmatch(
        r"[0-9a-f]{64}", arguments.upload_hash
    ):
        raise ValueError("upload hash must be a lowercase SHA-256 digest")
    if (arguments.reason is None) != (selected[2:] == (None, None)):
        raise ValueError("--reason goes with --accept-job-id or --replay-job-id")
    with psycopg.connect(database_url) as connection:
        if arguments.replay_job_id is not None:
            return replay_failed_jobs(
                connection,
                job_ids=arguments.replay_job_id,
                operator=arguments.operator or "",
                reason=arguments.reason,
                apply=arguments.apply,
            )
        if arguments.accept_job_id is not None:
            return accept_failed_job(
                connection,
                job_id=arguments.accept_job_id,
                operator=arguments.operator or "",
                reason=arguments.reason,
                apply=arguments.apply,
            )
        if selected == (None, None, None, None):
            return inspect_failed_items(connection, limit=arguments.limit)
        return retry_failed_item(
            connection,
            work_id=arguments.work_id,
            upload_hash=arguments.upload_hash,
            apply=arguments.apply,
        )


def inspect_failed_items(connection: Any, *, limit: int) -> dict[str, Any]:
    """Return at most ``limit`` recent collector or upload failures."""
    if not 1 <= limit <= 100:
        raise ValueError("failed-item limit must be between 1 and 100")
    with connection.transaction():
        connection.execute("SET LOCAL statement_timeout = '30s'")
        work_rows = connection.execute(
            """
            SELECT id, kind, lane, normalized_tag, status,
                   profile_status, battle_log_status, league_history_status,
                   profile_observation_id, battle_log_observation_id,
                   league_history_observation_id, failure_category,
                   due_at, updated_at
            FROM collector_work
            WHERE status = 'failed'
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
            """,
            (limit + 1,),
        ).fetchall()
        upload_rows = connection.execute(
            """
            SELECT response_hash, spool_key, byte_size, state, upload_generation,
                   attempt_count, next_attempt_at::text, last_error_category,
                   updated_at
            FROM collector_response_uploads
            WHERE state = 'failed'
            ORDER BY updated_at DESC, response_hash
            LIMIT %s
            """,
            (limit + 1,),
        ).fetchall()
        processing_rows = connection.execute(
            f"""
            {_PROCESSING_JOB}
            WHERE job.status = 'failed'
            ORDER BY job.updated_at DESC, job.id DESC
            LIMIT %s
            """,
            (limit + 1,),
        ).fetchall()
        transport_rows = connection.execute(
            """
            SELECT id, scope, endpoint, normalized_tag, retry_state,
                   failure_category, key_label, request_started_at, failed_at
            FROM collector_transport_failures
            ORDER BY failed_at DESC, id DESC
            LIMIT %s
            """,
            (limit + 1,),
        ).fetchall()
    items = [_work_item(row) for row in work_rows]
    items.extend(_upload_item(row) for row in upload_rows)
    items.extend(_processing_item(row) for row in processing_rows)
    items.extend(_transport_item(row) for row in transport_rows)
    items.sort(key=lambda item: (item["updated_at"], item["key"]), reverse=True)
    truncated = len(items) > limit
    items = items[:limit]
    return {
        "applied": False,
        "shown_count": len(items),
        "collector_work_count": sum(
            item["item_type"] == "collector_work" for item in items
        ),
        "upload_count": sum(item["item_type"] == "archive_upload" for item in items),
        "processing_job_count": sum(
            item["item_type"] == "processing_job" for item in items
        ),
        "transport_failure_count": sum(
            item["item_type"] == "transport_failure" for item in items
        ),
        "truncated": truncated,
        "items": items,
    }


def retry_failed_item(
    connection: Any,
    *,
    work_id: int | None = None,
    upload_hash: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or retry exactly one terminal failed collector item."""
    if (work_id is None) == (upload_hash is None):
        raise ValueError("select exactly one collector work ID or upload hash")
    try:
        with connection.transaction():
            connection.execute("SET LOCAL lock_timeout = '1s'")
            connection.execute("SET LOCAL statement_timeout = '30s'")
            if work_id is not None:
                return _retry_work(connection, work_id=work_id, apply=apply)
            assert upload_hash is not None
            return _retry_upload(connection, upload_hash=upload_hash, apply=apply)
    except UniqueViolation:
        return _refused("matching_collector_work_is_already_active")
    except LockNotAvailable:
        return _refused("retry_lock_timeout")
    except QueryCanceled:
        return _refused("retry_statement_timeout")


def accept_failed_job(
    connection: Any, *, job_id: int, operator: str, reason: str, apply: bool = False
) -> dict[str, Any]:
    """Preview or record that one failed processing job is beyond repair.

    The job keeps its failed state, attempts and saved response; only the
    failed-work count stops counting it.
    """
    _check_audit(operator, reason)
    try:
        with connection.transaction():
            connection.execute("SET LOCAL lock_timeout = '1s'")
            connection.execute("SET LOCAL statement_timeout = '30s'")
            row = connection.execute(
                f"{_PROCESSING_JOB} WHERE job.id = %s FOR UPDATE OF job", (job_id,)
            ).fetchone()
            if row is None:
                return _refused("processing_job_not_found")
            item = _processing_item(row)
            if _text(row[3]) != "failed":
                return _refused(f"processing_job_is_{_text(row[3])}", item)
            if row[12] is not None:
                return _refused("processing_job_already_accepted", item)
            report = {
                "applied": apply,
                "outcome": "accepted" if apply else "preview",
                "accepted_count": int(apply),
                "refused_count": 0,
                "item": item,
            }
            if apply:
                connection.execute(
                    """
                    INSERT INTO python_failed_job_acceptances (
                        job_id, operator_identity, reason
                    ) VALUES (%s, %s, %s)
                    """,
                    (job_id, operator, reason),
                )
            return report
    except UniqueViolation:
        return _refused("processing_job_already_accepted")
    except LockNotAvailable:
        return _refused("accept_lock_timeout")
    except QueryCanceled:
        return _refused("accept_statement_timeout")


def replay_failed_jobs(
    connection: Any,
    *,
    job_ids: Sequence[int],
    operator: str,
    reason: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or queue a replay of failed processing jobs, all or none.

    Each job's saved response is replayed under the job's own parser and
    rules through clashlens_request_python_replay_v2, which records who asked
    and why, exactly as deploy/replay-request does. That function only runs
    for its replay role, so the admin login switches to it for this one
    transaction. A preview asks the function and rolls back, so it refuses
    whatever --apply would.
    """
    _check_audit(operator, reason)
    ids = list(dict.fromkeys(job_ids))
    entries: dict[int, dict[str, Any]] = {}
    try:
        with connection.transaction() as transaction:
            connection.execute("SET LOCAL lock_timeout = '1s'")
            connection.execute("SET LOCAL statement_timeout = '30s'")
            rows = connection.execute(
                f"""
                {_PROCESSING_JOB} WHERE job.id = ANY(%s)
                ORDER BY job.id FOR UPDATE OF job
                """,
                (ids,),
            ).fetchall()
            found = {int(row[0]): row for row in rows}
            for job_id in ids:
                row = found.get(job_id)
                entry = entries[job_id] = {"processing_job_id": job_id}
                if row is None:
                    entry.update(outcome="refused", reason="processing_job_not_found")
                    continue
                entry["item"] = _processing_item(row)
                if _text(row[3]) != "failed":
                    entry.update(outcome="refused", reason=f"processing_job_is_{_text(row[3])}")
                elif row[12] is not None:
                    entry.update(outcome="refused", reason="processing_job_already_accepted")
            if all("outcome" not in entry for entry in entries.values()):
                connection.execute("SET LOCAL SESSION AUTHORIZATION clashlens_replay_request")
                for job_id in ids:
                    _request_replay(connection, found[job_id], entries[job_id], operator, reason)
            if not apply or any(entry.get("outcome") == "refused" for entry in entries.values()):
                raise psycopg.Rollback(transaction)
    except psycopg.Error as error:
        refusal = _BATCH_REFUSALS.get(error.sqlstate or "")
        if refusal is None:
            raise
        return _refused(refusal)
    refused = sum(entry.get("outcome") == "refused" for entry in entries.values())
    applied = apply and not refused
    for entry in entries.values():
        entry.setdefault("outcome", "not_checked")
        if entry["outcome"] == "replay_enqueued" and not applied:
            # Rolled back: these request and job ids were never kept.
            entry["outcome"] = "would_enqueue"
            del entry["replay_request_id"], entry["replay_job_id"]
    return {
        "applied": applied,
        "outcome": "refused" if refused else "replay_enqueued" if applied else "preview",
        "replayed_count": len(ids) if applied else 0,
        "refused_count": refused,
        "items": list(entries.values()),
    }


def _request_replay(
    connection: Any, row: Any, entry: dict[str, Any], operator: str, reason: str
) -> None:
    try:
        with connection.transaction():
            request_id, job_id, status = connection.execute(
                """
                SELECT request_id, job_id, request_status
                FROM clashlens_request_python_replay_v2(%s, %s, %s, %s, %s, %s, %s)
                """,
                (row[4] if row[4] is not None else row[5], operator, reason, *row[15:19]),
            ).fetchone()
    except psycopg.Error as error:
        refusal = _REQUEST_REFUSALS.get(error.sqlstate or "")
        if refusal is None:
            raise
        entry.update(outcome="refused", reason=refusal)
        return
    entry.update(replay_request_id=int(request_id), replay_job_id=int(job_id))
    # The same request already made returns its own status; only "enqueued" is live.
    if _text(status) == "enqueued":
        entry["outcome"] = "replay_enqueued"
    else:
        entry.update(outcome="refused", reason=f"replay_request_{_text(status)}")


def _check_audit(operator: str, reason: str) -> None:
    if not _OPERATOR.fullmatch(operator):
        raise ValueError("operator must be 1-128 letters, digits or ._:@-")
    if not 8 <= len(reason) <= 500 or not reason.isprintable():
        raise ValueError("reason must be 8-500 printable characters on one line")


def _retry_work(connection: Any, *, work_id: int, apply: bool) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, kind, lane, normalized_tag, status,
               profile_status, battle_log_status, league_history_status,
               profile_observation_id, battle_log_observation_id,
               league_history_observation_id, failure_category,
               due_at, updated_at, coalescing_key
        FROM collector_work
        WHERE id = %s
        FOR UPDATE
        """,
        (work_id,),
    ).fetchone()
    if row is None:
        return _refused("collector_work_not_found")
    item = _work_item(row[:14])
    if _text(row[4]) != "failed":
        return _refused(f"collector_work_is_{_text(row[4])}", item)
    active = connection.execute(
        """
        SELECT id
        FROM collector_work
        WHERE coalescing_key = %s AND id <> %s
          AND status IN ('pending', 'waiting_retry')
        ORDER BY id
        LIMIT 1
        """,
        (row[14], work_id),
    ).fetchone()
    if active is not None:
        return _refused("matching_collector_work_is_already_active", item)
    if not apply:
        return _preview(item, new_state="waiting_retry")
    changed = connection.execute(
        """
        UPDATE collector_work
        SET status = 'waiting_retry', due_at = clock_timestamp(),
            completed_at = NULL, updated_at = clock_timestamp()
        WHERE id = %s AND status = 'failed'
        """,
        (work_id,),
    )
    if changed.rowcount != 1:
        return _refused("collector_work_changed_during_retry", item)
    return _requeued(item, new_state="waiting_retry")


def _retry_upload(connection: Any, *, upload_hash: str, apply: bool) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT response_hash, spool_key, byte_size, state, upload_generation,
               attempt_count, next_attempt_at::text, last_error_category,
               updated_at, lease_owner, lease_token,
               lease_expires_at, archive_reference, archive_instance_id,
               completed_at
        FROM collector_response_uploads
        WHERE response_hash = %s
        FOR UPDATE
        """,
        (upload_hash,),
    ).fetchone()
    if row is None:
        return _refused("archive_upload_not_found")
    item = _upload_item(row[:9])
    if _text(row[3]) != "failed":
        return _refused(f"archive_upload_is_{_text(row[3])}", item)
    if any(value is not None for value in row[9:12]):
        return _refused("archive_upload_has_an_active_lease", item)
    failure_category = _text(row[7])
    if failure_category in _INTEGRITY_FAILURES:
        return _refused("archive_integrity_repair_required", item)
    if any(value is not None for value in row[12:15]):
        return _refused("archive_upload_has_completion_metadata", item)
    if not apply:
        report = _preview(item, new_state="pending")
        return _with_upload_action(report, failure_category)
    changed = connection.execute(
        """
        UPDATE collector_response_uploads
        SET state = 'pending', next_attempt_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE response_hash = %s AND state = 'failed'
          AND lease_owner IS NULL AND lease_token IS NULL
          AND lease_expires_at IS NULL
        """,
        (upload_hash,),
    )
    if changed.rowcount != 1:
        return _refused("archive_upload_changed_during_retry", item)
    report = _requeued(item, new_state="pending")
    return _with_upload_action(report, failure_category)


def _work_item(row: Any) -> dict[str, Any]:
    return {
        "item_type": "collector_work",
        "key": str(row[0]),
        "work_id": int(row[0]),
        "kind": _text(row[1]),
        "lane": _text(row[2]),
        "normalized_tag": None if row[3] is None else _text(row[3]),
        "state": _text(row[4]),
        "endpoint_states": {
            "profile": _text(row[5]),
            "battle_log": _text(row[6]),
            "league_history": _text(row[7]),
        },
        "observation_ids": {
            "profile": row[8],
            "battle_log": row[9],
            "league_history": row[10],
        },
        "failure_category": None if row[11] is None else _text(row[11]),
        "due_at": row[12],
        "updated_at": row[13],
    }


def _upload_item(row: Any) -> dict[str, Any]:
    return {
        "item_type": "archive_upload",
        "key": _text(row[0]),
        "response_hash": _text(row[0]),
        "spool_key": _text(row[1]),
        "byte_size": int(row[2]),
        "state": _text(row[3]),
        "upload_generation": _text(row[4]),
        "attempt_count": int(row[5]),
        "next_attempt_at": _text(row[6]),
        "failure_category": None if row[7] is None else _text(row[7]),
        "updated_at": row[8],
    }


def _processing_item(row: Any) -> dict[str, Any]:
    source_observation_id = row[4] if row[4] is not None else row[5]
    item = {
        "item_type": "processing_job",
        "key": str(row[0]),
        "processing_job_id": int(row[0]),
        "work_type": _text(row[1]),
        "endpoint": None if row[2] is None else _text(row[2]),
        "state": _text(row[3]),
        "source_observation_id": source_observation_id,
        "attempt_count": int(row[6]),
        "max_attempts": int(row[7]),
        "outcome": None if row[8] is None else _text(row[8]),
        "failure_category": None if row[9] is None else _text(row[9]),
        "due_at": row[10],
        "updated_at": row[11],
        "recovery": "investigate_only",
    }
    if row[12] is not None:
        item["recovery"] = "accepted"
        item["accepted"] = {
            "operator": _text(row[12]),
            "reason": _text(row[13]),
            "accepted_at": row[14],
        }
    elif _text(row[1]) in {"process_observation", "replay_observation"} and _text(
        row[2]
    ) in {"profile", "battle_log"}:
        item["recovery"] = "replay"
    return item


def _transport_item(row: Any) -> dict[str, Any]:
    return {
        "item_type": "transport_failure",
        "key": str(row[0]),
        "transport_failure_id": int(row[0]),
        "scope": _text(row[1]),
        "endpoint": _text(row[2]),
        "normalized_tag": None if row[3] is None else _text(row[3]),
        "state": _text(row[4]),
        "failure_category": _text(row[5]),
        "key_label": _text(row[6]),
        "request_started_at": row[7],
        "updated_at": row[8],
        "recovery": "none_evidence_only",
    }


def _text(value: Any) -> str:
    return (
        value.decode("utf-8", errors="backslashreplace")
        if isinstance(value, bytes)
        else str(value or "")
    )


def _with_upload_action(
    report: dict[str, Any], failure_category: str
) -> dict[str, Any]:
    if failure_category in _RESTART_AFTER_REPAIR_FAILURES:
        report["operator_action"] = "repair_archive_configuration_and_restart_collector"
    return report


def _preview(item: dict[str, Any], *, new_state: str) -> dict[str, Any]:
    return {
        "applied": False,
        "outcome": "preview",
        "retried_count": 0,
        "refused_count": 0,
        "new_state": new_state,
        "item": item,
    }


def _requeued(item: dict[str, Any], *, new_state: str) -> dict[str, Any]:
    return {
        "applied": True,
        "outcome": "requeued",
        "retried_count": 1,
        "refused_count": 0,
        "new_state": new_state,
        "item": item,
    }


def _refused(reason: str, item: dict[str, Any] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "applied": False,
        "outcome": "refused",
        "retried_count": 0,
        "refused_count": 1,
        "reason": reason,
    }
    if item is not None:
        report["item"] = item
    return report
