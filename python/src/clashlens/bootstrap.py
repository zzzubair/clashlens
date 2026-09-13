"""Bounded fixed-population bootstrap for issue #92 (B2).

One operator-facing command admits a protected tag manifest as
inactive/unknown players and enqueues profile-only discovery work through the
existing ``clashlens_enqueue_discovery_profiles`` primitive. The whole
manifest is validated before the first write; batches replay idempotently
under a stable run-id; any colliding run fails closed. Only aggregate counts
and SHA-256 digests leave this module, never tags or player IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

from .db import CONTRACT_VERSION
from .profile import ProfileParseError, normalize_player_tag

MANIFEST_MAX_BYTES = 1 << 20
MANIFEST_MAX_LINES = 20000
BOOTSTRAP_BATCH_SIZE = 500

_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class BootstrapError(ValueError):
    """Stable fail-closed bootstrap failure; carries no tags or secrets."""


@dataclass(frozen=True)
class Manifest:
    tags: tuple[str, ...]
    raw_sha256: str
    normalized_set_sha256: str


def _require_absolute(path: str, label: str) -> str:
    if not path or not os.path.isabs(path):
        raise BootstrapError(f"{label}_path_must_be_absolute")
    return path


def _read_strict_file(path: str, label: str, *, max_bytes: int) -> bytes:
    """Read a regular file without following symlinks.

    Returns the raw bytes. Symlinks, directories, oversized files, and read
    failures all raise BootstrapError.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise BootstrapError(f"{label}_file_could_not_be_read") from error
    try:
        try:
            size = os.fstat(fd).st_size
        except OSError as error:
            raise BootstrapError(f"{label}_file_could_not_be_read") from error
        if size > max_bytes:
            raise BootstrapError(f"{label}_file_exceeds_size_limit")
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError as error:
                raise BootstrapError(f"{label}_file_could_not_be_read") from error
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(part) for part in chunks) > max_bytes:
                raise BootstrapError(f"{label}_file_exceeds_size_limit")
        return b"".join(chunks)
    finally:
        os.close(fd)


def parse_manifest(
    raw: bytes, *, expected_sha256: str, expected_count: int
) -> Manifest:
    """Fully validate a manifest before any database mutation."""
    if not _SHA256_RE.fullmatch(expected_sha256):
        raise BootstrapError("manifest_digest_is_malformed")
    if not 1 <= expected_count <= MANIFEST_MAX_LINES:
        raise BootstrapError("manifest_count_out_of_range")
    if len(raw) > MANIFEST_MAX_BYTES:
        raise BootstrapError("manifest_exceeds_size_limit")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BootstrapError("manifest_digest_mismatch")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BootstrapError("manifest_is_not_utf8") from error
    lines = text.split("\n")
    if lines and lines[-1] == "":
        # One trailing newline terminates the last tag line; anything else
        # blank (including a blank final line) is rejected below.
        lines = lines[:-1]
    if len(lines) > MANIFEST_MAX_LINES:
        raise BootstrapError("manifest_exceeds_line_limit")
    tags: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line.strip() == "":
            raise BootstrapError("manifest_has_blank_line")
        try:
            normalized = normalize_player_tag(line)
        except ProfileParseError as error:
            raise BootstrapError("manifest_has_malformed_tag") from error
        if normalized in seen:
            raise BootstrapError("manifest_has_duplicate_tag")
        seen.add(normalized)
        tags.append(normalized)
    if len(tags) != expected_count:
        raise BootstrapError("manifest_count_mismatch")
    normalized_set_sha256 = hashlib.sha256(
        "\n".join(sorted(tags)).encode("utf-8")
    ).hexdigest()
    return Manifest(
        tags=tuple(tags),
        raw_sha256=actual_sha256,
        normalized_set_sha256=normalized_set_sha256,
    )


def read_secret_file(path: str) -> str:
    """Read a single-line secret file without following symlinks."""
    _require_absolute(path, "database_url")
    raw = _read_strict_file(path, "database_url", max_bytes=4096)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BootstrapError("database_url_file_is_malformed") from error
    if not decoded or "\n" in decoded or "\r" in decoded:
        raise BootstrapError("database_url_file_is_malformed")
    return decoded


def _run_row(connection: object, run_id: str) -> dict | None:
    row = connection.execute(  # type: ignore[union-attr]
        """SELECT run_id, manifest_sha256, manifest_count,
                  normalized_set_sha256, status, batch_size,
                  players_registered, discovery_jobs_created
           FROM population_bootstrap_runs WHERE run_id = %s""",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row[0],
        "manifest_sha256": row[1],
        "manifest_count": row[2],
        "normalized_set_sha256": row[3],
        "status": row[4],
        "batch_size": row[5],
        "players_registered": row[6],
        "discovery_jobs_created": row[7],
    }


def _report(
    run_id: str,
    manifest: Manifest,
    *,
    batch_size: int,
    players_registered: int,
    discovery_jobs_created: int,
) -> dict:
    tags = list(manifest.tags)
    return {
        "status": "complete",
        "run_id": run_id,
        "manifest_sha256": manifest.raw_sha256,
        "manifest_count": len(tags),
        "normalized_set_sha256": manifest.normalized_set_sha256,
        "batch_size": batch_size,
        "batch_count": (len(tags) + batch_size - 1) // batch_size,
        "players_registered": players_registered,
        "discovery_jobs_created": discovery_jobs_created,
    }


def bootstrap_population(
    *,
    database_url: str,
    cohort_file: str,
    expected_sha256: str,
    expected_count: int,
    run_id: str,
    result_file: str,
) -> dict:
    """Validate a manifest, admit it idempotently, and write the receipt."""
    import psycopg

    if not _RUN_ID_RE.fullmatch(run_id):
        raise BootstrapError("run_id_is_malformed")
    _require_absolute(cohort_file, "cohort")
    _require_absolute(result_file, "result")
    if os.path.lexists(result_file):
        raise BootstrapError("result_file_already_exists")
    raw = _read_strict_file(cohort_file, "cohort", max_bytes=MANIFEST_MAX_BYTES)
    manifest = parse_manifest(
        raw, expected_sha256=expected_sha256, expected_count=expected_count
    )
    tags = list(manifest.tags)

    with psycopg.connect(database_url, autocommit=True) as connection:
        version = connection.execute(
            "SELECT version FROM clash_lens_contract WHERE singleton"
        ).fetchone()
        if version is None or version[0] != CONTRACT_VERSION:
            raise BootstrapError("database_contract_unsupported")

        # Serialize the fresh-run check/insert so two concurrent run IDs
        # cannot both pass the no-earlier-run invariant. The transaction
        # advisory lock mirrors the admission-evidence pattern; the loser
        # blocks here, then re-reads committed state and fails closed.
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("\
                "'population_bootstrap', 0))"
            )
            existing = _run_row(connection, run_id)
            if existing is not None:
                if (
                    existing["manifest_sha256"] != manifest.raw_sha256
                    or existing["manifest_count"] != len(tags)
                    or existing["normalized_set_sha256"]
                    != manifest.normalized_set_sha256
                    or existing["batch_size"] != BOOTSTRAP_BATCH_SIZE
                ):
                    raise BootstrapError("bootstrap_run_collision")
                if existing["status"] == "complete":
                    report = _report(
                        run_id,
                        manifest,
                        batch_size=BOOTSTRAP_BATCH_SIZE,
                        players_registered=existing["players_registered"],
                        discovery_jobs_created=existing[
                            "discovery_jobs_created"
                        ],
                    )
                    _write_result_file(result_file, report)
                    return report
                # Same run-id, still started: replay every batch below. Each
                # batch is idempotent, so a partial run simply converges.
            else:
                other_runs = connection.execute(
                    "SELECT count(*) FROM population_bootstrap_runs"
                ).fetchone()[0]
                if other_runs:
                    raise BootstrapError("bootstrap_run_collision")
                if (
                    connection.execute(
                        "SELECT count(*) FROM players WHERE active"
                    ).fetchone()[0]
                ):
                    raise BootstrapError("database_already_has_active_players")
                if (
                    connection.execute(
                        "SELECT count(*) FROM collector_work WHERE scope = 'player'"
                    ).fetchone()[0]
                ):
                    raise BootstrapError("database_already_has_player_work")
                if (
                    connection.execute(
                        "SELECT count(*) FROM collector_work WHERE kind = 'global_player_rankings'"
                    ).fetchone()[0]
                ):
                    raise BootstrapError("database_already_has_ranking_intent")
                connection.execute(
                    """INSERT INTO population_bootstrap_runs (
                           run_id, manifest_sha256, manifest_count,
                           normalized_set_sha256, status, batch_size
                       ) VALUES (%s, %s, %s, %s, 'started', %s)""",
                    (
                        run_id,
                        manifest.raw_sha256,
                        len(tags),
                        manifest.normalized_set_sha256,
                        BOOTSTRAP_BATCH_SIZE,
                    ),
                )

        for offset in range(0, len(tags), BOOTSTRAP_BATCH_SIZE):
            chunk = tags[offset : offset + BOOTSTRAP_BATCH_SIZE]
            with connection.transaction():
                connection.execute(
                    """INSERT INTO players (
                           normalized_tag, active, eligibility_state
                       ) SELECT tag, false, 'unknown'
                       FROM unnest(%s::text[]) AS tag
                       ON CONFLICT (normalized_tag) DO NOTHING""",
                    (chunk,),
                )
                player_ids = [
                    row[0]
                    for row in connection.execute(
                        """SELECT id FROM players
                           WHERE normalized_tag = ANY(%s::text[])""",
                        (chunk,),
                    ).fetchall()
                ]
                connection.execute(
                    "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                    (player_ids,),
                )

        # Recompute the durable aggregates from the cohort's actual work rows
        # after convergence. A replayed batch reuses existing work (which
        # reports zero created), so only a post-pass count is exact. These
        # are counts over the cohort set; no tags or IDs leave the database.
        players_registered = connection.execute(
            """SELECT count(*) FROM players
               WHERE normalized_tag = ANY(%s::text[])""",
            (tags,),
        ).fetchone()[0]
        discovery_jobs_created = connection.execute(
            """SELECT count(*) FROM collector_work
               WHERE kind = 'discovery_profile'
                 AND player_id IN (
                     SELECT id FROM players
                     WHERE normalized_tag = ANY(%s::text[])
                 )""",
            (tags,),
        ).fetchone()[0]

        connection.execute(
            """UPDATE population_bootstrap_runs
               SET status = 'complete', players_registered = %s,
                   discovery_jobs_created = %s,
                   completed_at = clock_timestamp()
               WHERE run_id = %s""",
            (players_registered, discovery_jobs_created, run_id),
        )

    report = _report(
        run_id,
        manifest,
        batch_size=BOOTSTRAP_BATCH_SIZE,
        players_registered=players_registered,
        discovery_jobs_created=discovery_jobs_created,
    )
    _write_result_file(result_file, report)
    return report


def _write_result_file(path: str, report: dict) -> None:
    """Write the aggregate-only receipt exclusively; never tags or IDs."""
    payload = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as error:
        raise BootstrapError("result_file_already_exists") from error
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except OSError as error:
        raise BootstrapError("result_file_could_not_be_written") from error
    finally:
        os.close(fd)
