"""Bounded operator visibility, explicit retry, replay and acceptance of failures."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.errors import LockNotAvailable, QueryCanceled, UniqueViolation
from psycopg.rows import dict_row

from .job_contracts import (
    ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    PYTHON_BACKFILL_PRIORITY,
)
from .profile import RENAMED_LEGEND_TIERS, RENAMED_TIER_PARSER_VERSION

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
    SELECT job.id, job.work_type, job.endpoint, job.status AS state, job.observation_id,
           job.replay_observation_id, job.attempt_count, job.max_attempts,
           job.outcome, job.failure_category, job.due_at, job.updated_at,
           accepted.operator_identity, accepted.reason, accepted.accepted_at,
           job.parser_version, job.processing_version, job.domain_rule_version,
           job.analytics_rule_version
    FROM python_processing_jobs AS job
    LEFT JOIN python_failed_job_acceptances AS accepted ON accepted.job_id = job.id
"""


class Recovery(StrEnum):
    """The way back for one failed processing job or transport failure.

    A processing job that failed for good is ``REPLAY`` when its saved
    response is a profile or battle log the worker can read again under the
    job's own parser and rules, else ``INVESTIGATE_ONLY``. Either becomes
    ``ACCEPTED`` once an operator records it as beyond repair, which is
    final: the job keeps its failed state, attempts and saved response, and
    the failed-work count stops counting it. A replay queues a new replay
    job, and records who asked and why, but leaves the failed job as it was.
    A transport failure is ``EVIDENCE_ONLY``: the request got no answer, so
    there is nothing to retry. (Failed collector work and archive uploads
    have one way back, a retry, so their items carry no such field.)

    The database rows are the state, read by ``processing_recovery``;
    ``_processing_refusal`` is the one guard on accepting or replaying.
    Each action is previewed first and applied in one transaction that
    re-reads its rows under lock; a lock wait over 1 second, a statement
    over 30 seconds or a row that changed meanwhile refuses it with a
    reason and changes nothing, so it can simply be run again.
    """

    REPLAY = "replay"
    INVESTIGATE_ONLY = "investigate_only"
    ACCEPTED = "accepted"
    EVIDENCE_ONLY = "none_evidence_only"


def processing_recovery(
    work_type: str, endpoint: str | None, accepted: bool
) -> Recovery:
    if accepted:
        return Recovery.ACCEPTED
    if work_type in {"process_observation", "replay_observation"} and endpoint in {
        "profile",
        "battle_log",
    }:
        return Recovery.REPLAY
    return Recovery.INVESTIGATE_ONLY


def _processing_refusal(row: Any) -> str | None:
    """Why a processing job row may not be accepted or replayed, if it may not."""
    if _text(row["state"]) != "failed":
        return f"processing_job_is_{_text(row['state'])}"
    if row["operator_identity"] is not None:
        return "processing_job_already_accepted"
    return None


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
        arguments.reread_reset,
    )
    if arguments.apply and selected == (None,) * 5:
        raise ValueError(
            "--apply requires --work-id, --upload-hash, --accept-job-id,"
            " --replay-job-id or --reread-reset"
        )
    if arguments.upload_hash is not None and not re.fullmatch(
        r"[0-9a-f]{64}", arguments.upload_hash
    ):
        raise ValueError("upload hash must be a lowercase SHA-256 digest")
    if (arguments.reason is None) != (selected[2:] == (None,) * 3):
        raise ValueError(
            "--reason goes with --accept-job-id, --replay-job-id or --reread-reset"
        )
    with psycopg.connect(database_url) as connection:
        if arguments.reread_reset is not None:
            return reread_reset_profiles(
                connection,
                resets=arguments.reread_reset,
                operator=arguments.operator or "",
                reason=arguments.reason,
                apply=arguments.apply,
            )
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
        if selected == (None,) * 5:
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
        work_rows = (
            connection.cursor(row_factory=dict_row)
            .execute(
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
            )
            .fetchall()
        )
        upload_rows = (
            connection.cursor(row_factory=dict_row)
            .execute(
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
            )
            .fetchall()
        )
        processing_rows = (
            connection.cursor(row_factory=dict_row)
            .execute(
                f"""
            {_PROCESSING_JOB}
            WHERE job.status = 'failed'
            ORDER BY job.updated_at DESC, job.id DESC
            LIMIT %s
            """,
                (limit + 1,),
            )
            .fetchall()
        )
        transport_rows = (
            connection.cursor(row_factory=dict_row)
            .execute(
                """
            SELECT id, scope, endpoint, normalized_tag, retry_state,
                   failure_category, key_label, request_started_at, failed_at
            FROM collector_transport_failures
            ORDER BY failed_at DESC, id DESC
            LIMIT %s
            """,
                (limit + 1,),
            )
            .fetchall()
        )
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
            row = (
                connection.cursor(row_factory=dict_row)
                .execute(
                    f"{_PROCESSING_JOB} WHERE job.id = %s FOR UPDATE OF job", (job_id,)
                )
                .fetchone()
            )
            if row is None:
                return _refused("processing_job_not_found")
            item = _processing_item(row)
            refusal = _processing_refusal(row)
            if refusal is not None:
                return _refused(refusal, item)
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
            rows = (
                connection.cursor(row_factory=dict_row)
                .execute(
                    f"""
                {_PROCESSING_JOB} WHERE job.id = ANY(%s)
                ORDER BY job.id FOR UPDATE OF job
                """,
                    (ids,),
                )
                .fetchall()
            )
            found = {int(row["id"]): row for row in rows}
            for job_id in ids:
                row = found.get(job_id)
                entry = entries[job_id] = {"processing_job_id": job_id}
                if row is None:
                    entry.update(outcome="refused", reason="processing_job_not_found")
                    continue
                entry["item"] = _processing_item(row)
                refusal = _processing_refusal(row)
                if refusal is not None:
                    entry.update(outcome="refused", reason=refusal)
            if all("outcome" not in entry for entry in entries.values()):
                connection.execute(
                    "SET LOCAL SESSION AUTHORIZATION clashlens_replay_request"
                )
                for job_id in ids:
                    _request_replay(
                        connection, found[job_id], entries[job_id], operator, reason
                    )
            if not apply or any(
                entry.get("outcome") == "refused" for entry in entries.values()
            ):
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
        "outcome": "refused"
        if refused
        else "replay_enqueued"
        if applied
        else "preview",
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
                (
                    row["observation_id"]
                    if row["observation_id"] is not None
                    else row["replay_observation_id"],
                    operator,
                    reason,
                    *(
                        row["parser_version"],
                        row["processing_version"],
                        row["domain_rule_version"],
                        row["analytics_rule_version"],
                    ),
                ),
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


# Each Reset pair's saved profile whose result, its newest processing
# outcome's, was kept uncertain only because it named a renamed Legend tier,
# not yet asked to be re-read.
_RENAMED_TIER_READINGS = """
    SELECT DISTINCT work.profile_observation_id
    FROM collector_reset_sweeps AS sweep
    JOIN collector_work AS work
      ON work.sweep_id = sweep.id AND work.kind = 'reset_baseline'
    CROSS JOIN LATERAL (
        SELECT outcome.parser_version
        FROM observation_processing_outcomes AS outcome
        WHERE outcome.observation_id = work.profile_observation_id
          AND outcome.processing_version = %(processing)s
        ORDER BY outcome.id DESC
        LIMIT 1
    ) AS outcome
    JOIN player_profile_effects AS effect
      ON effect.observation_id = work.profile_observation_id
     AND effect.parser_version = outcome.parser_version
    JOIN player_profile_versions AS profile ON profile.id = effect.profile_version_id
    WHERE sweep.boundary_at = %(reset)s
      AND profile.eligibility_reason = 'known_tier_name_conflict'
      AND (profile.league_tier_id, profile.league_tier_name) IN (
          SELECT * FROM unnest(%(tier_ids)s::bigint[], %(tier_names)s::text[]))
      AND NOT EXISTS (
          SELECT FROM python_replay_requests AS request
          WHERE request.observation_id = work.profile_observation_id
            AND request.target_parser_version = %(new)s)
    ORDER BY 1
"""
_REREAD_BATCH = 500


def reread_reset_profiles(
    connection: Any,
    *,
    resets: Sequence[datetime],
    operator: str,
    reason: str,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or queue a re-read of each Reset pair's saved profile that
    profile parser v3 kept uncertain because it named a renamed Legend tier.

    From 16 November 2026, 12:31 UTC, until the fix deployed on 18 November
    at 10:03 UTC, v3 kept every profile naming "Legend League I", II or III
    uncertain, so the 17 and 18 November Reset pairs failed and the Legend
    days they ended, with their Daily boards, stayed unproven. Each such
    reading of the given Resets is replayed under profile parser v4, the
    fixed reader, through clashlens_request_python_replay_v2, which records
    who asked and why, as ``replay_failed_jobs`` does. Its replay saves the
    reading's new result beside the old one and checks the Reset pair again,
    which recalculates the day the Reset ended, and a day it changes then
    corrects its board as any changed day does. The jobs run at backfill
    priority, so they wait while live work is behind. Readings are asked
    for 500 a transaction; a reading already asked for is not listed again,
    so a run that stopped can simply be run again. A preview asks the replay
    function and rolls back, so it refuses whatever ``apply`` would.
    """
    _check_audit(operator, reason)
    tiers = sorted(RENAMED_LEGEND_TIERS.items())
    report: dict[str, Any] = {
        "applied": apply,
        "outcome": "replay_enqueued" if apply else "preview",
        "replayed_count": 0,
        "refused_count": 0,
        "resets": [],
    }
    for reset in dict.fromkeys(resets):
        with connection.transaction():
            connection.execute("SET LOCAL statement_timeout = '30s'")
            readings = [
                int(row[0])
                for row in connection.execute(
                    _RENAMED_TIER_READINGS,
                    {
                        "reset": reset,
                        "processing": PROCESSING_VERSION,
                        "new": RENAMED_TIER_PARSER_VERSION,
                        "tier_ids": [tier_id for tier_id, _name in tiers],
                        "tier_names": [name for _tier_id, name in tiers],
                    },
                ).fetchall()
            ]
        refused: Counter[str] = Counter()
        refused_ids: list[int] = []
        job_ids: list[int] = []
        entry = {"reset": reset.isoformat(), "readings": len(readings)}
        report["resets"].append(entry)
        for start in range(0, len(readings), _REREAD_BATCH):
            try:
                with connection.transaction() as transaction:
                    connection.execute("SET LOCAL lock_timeout = '1s'")
                    connection.execute("SET LOCAL statement_timeout = '30s'")
                    connection.execute(
                        "SET LOCAL SESSION AUTHORIZATION clashlens_replay_request"
                    )
                    queued: list[int] = []
                    for observation_id in readings[start : start + _REREAD_BATCH]:
                        result: dict[str, Any] = {}
                        _request_replay(
                            connection,
                            {
                                "observation_id": observation_id,
                                "parser_version": RENAMED_TIER_PARSER_VERSION,
                                "processing_version": PROCESSING_VERSION,
                                "domain_rule_version": DOMAIN_RULE_VERSION,
                                "analytics_rule_version": ANALYTICS_RULE_VERSION,
                            },
                            result,
                            operator,
                            reason,
                        )
                        if result["outcome"] == "replay_enqueued":
                            queued.append(result["replay_job_id"])
                        else:
                            refused[result["reason"]] += 1
                            refused_ids.append(observation_id)
                    connection.execute("RESET SESSION AUTHORIZATION")
                    connection.execute(
                        "UPDATE python_processing_jobs SET priority = %s"
                        " WHERE id = ANY(%s)",
                        (PYTHON_BACKFILL_PRIORITY, queued),
                    )
                    if not apply:
                        raise psycopg.Rollback(transaction)
                job_ids.extend(queued)
            except psycopg.Error as error:
                refusal = _BATCH_REFUSALS.get(error.sqlstate or "")
                if refusal is None:
                    raise
                report.update(outcome="refused", reason=refusal)
                break
        entry["replay_enqueued" if apply else "would_enqueue"] = len(job_ids)
        if apply and job_ids:
            entry["replay_job_ids"] = [min(job_ids), max(job_ids)]
        entry["refused"] = dict(sorted(refused.items()))
        entry["refused_observation_ids"] = refused_ids[:20]
        report["replayed_count"] += len(job_ids) if apply else 0
        report["refused_count"] += sum(refused.values())
        if report["outcome"] == "refused":
            break
    return report


def _check_audit(operator: str, reason: str) -> None:
    if not _OPERATOR.fullmatch(operator):
        raise ValueError("operator must be 1-128 letters, digits or ._:@-")
    if not 8 <= len(reason) <= 500 or not reason.isprintable():
        raise ValueError("reason must be 8-500 printable characters on one line")


def _retry_work(connection: Any, *, work_id: int, apply: bool) -> dict[str, Any]:
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
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
        )
        .fetchone()
    )
    if row is None:
        return _refused("collector_work_not_found")
    item = _work_item(row)
    if _text(row["status"]) != "failed":
        return _refused(f"collector_work_is_{_text(row['status'])}", item)
    active = connection.execute(
        """
        SELECT id
        FROM collector_work
        WHERE coalescing_key = %s AND id <> %s
          AND status IN ('pending', 'waiting_retry')
        ORDER BY id
        LIMIT 1
        """,
        (row["coalescing_key"], work_id),
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
    row = (
        connection.cursor(row_factory=dict_row)
        .execute(
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
        )
        .fetchone()
    )
    if row is None:
        return _refused("archive_upload_not_found")
    item = _upload_item(row)
    if _text(row["state"]) != "failed":
        return _refused(f"archive_upload_is_{_text(row['state'])}", item)
    if any(
        value is not None
        for value in (row["lease_owner"], row["lease_token"], row["lease_expires_at"])
    ):
        return _refused("archive_upload_has_an_active_lease", item)
    failure_category = _text(row["last_error_category"])
    if failure_category in _INTEGRITY_FAILURES:
        return _refused("archive_integrity_repair_required", item)
    if any(
        value is not None
        for value in (
            row["archive_reference"],
            row["archive_instance_id"],
            row["completed_at"],
        )
    ):
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
        "key": str(row["id"]),
        "work_id": int(row["id"]),
        "kind": _text(row["kind"]),
        "lane": _text(row["lane"]),
        "normalized_tag": None
        if row["normalized_tag"] is None
        else _text(row["normalized_tag"]),
        "state": _text(row["status"]),
        "endpoint_states": {
            "profile": _text(row["profile_status"]),
            "battle_log": _text(row["battle_log_status"]),
            "league_history": _text(row["league_history_status"]),
        },
        "observation_ids": {
            "profile": row["profile_observation_id"],
            "battle_log": row["battle_log_observation_id"],
            "league_history": row["league_history_observation_id"],
        },
        "failure_category": None
        if row["failure_category"] is None
        else _text(row["failure_category"]),
        "due_at": row["due_at"],
        "updated_at": row["updated_at"],
    }


def _upload_item(row: Any) -> dict[str, Any]:
    return {
        "item_type": "archive_upload",
        "key": _text(row["response_hash"]),
        "response_hash": _text(row["response_hash"]),
        "spool_key": _text(row["spool_key"]),
        "byte_size": int(row["byte_size"]),
        "state": _text(row["state"]),
        "upload_generation": _text(row["upload_generation"]),
        "attempt_count": int(row["attempt_count"]),
        "next_attempt_at": _text(row["next_attempt_at"]),
        "failure_category": None
        if row["last_error_category"] is None
        else _text(row["last_error_category"]),
        "updated_at": row["updated_at"],
    }


def _processing_item(row: Any) -> dict[str, Any]:
    source_observation_id = (
        row["observation_id"]
        if row["observation_id"] is not None
        else row["replay_observation_id"]
    )
    item = {
        "item_type": "processing_job",
        "key": str(row["id"]),
        "processing_job_id": int(row["id"]),
        "work_type": _text(row["work_type"]),
        "endpoint": None if row["endpoint"] is None else _text(row["endpoint"]),
        "state": _text(row["state"]),
        "source_observation_id": source_observation_id,
        "attempt_count": int(row["attempt_count"]),
        "max_attempts": int(row["max_attempts"]),
        "outcome": None if row["outcome"] is None else _text(row["outcome"]),
        "failure_category": None
        if row["failure_category"] is None
        else _text(row["failure_category"]),
        "due_at": row["due_at"],
        "updated_at": row["updated_at"],
        "recovery": processing_recovery(
            _text(row["work_type"]),
            None if row["endpoint"] is None else _text(row["endpoint"]),
            row["operator_identity"] is not None,
        ).value,
    }
    if row["operator_identity"] is not None:
        item["accepted"] = {
            "operator": _text(row["operator_identity"]),
            "reason": _text(row["reason"]),
            "accepted_at": row["accepted_at"],
        }
    return item


def _transport_item(row: Any) -> dict[str, Any]:
    return {
        "item_type": "transport_failure",
        "key": str(row["id"]),
        "transport_failure_id": int(row["id"]),
        "scope": _text(row["scope"]),
        "endpoint": _text(row["endpoint"]),
        "normalized_tag": None
        if row["normalized_tag"] is None
        else _text(row["normalized_tag"]),
        "state": _text(row["retry_state"]),
        "failure_category": _text(row["failure_category"]),
        "key_label": _text(row["key_label"]),
        "request_started_at": row["request_started_at"],
        "updated_at": row["failed_at"],
        "recovery": Recovery.EVIDENCE_ONLY.value,
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
