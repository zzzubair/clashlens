"""Bounded, operator-invoked compaction of completed collection bookkeeping.

This does not delete raw objects, battles, profiles, publications, or corrections.
The database's restrictive domain foreign keys are a final safety barrier.
"""

from __future__ import annotations

from typing import Any

# Keep source transitions and both ends of unchanged log runs. Removing an
# interior repeat cannot change the overlap/quality result of the log chain.
_ELIGIBLE = """
SELECT job.id FROM collector_jobs AS job
WHERE job.status = 'complete'
  AND job.work_type IN ('regular_poll', 'initial_collection', 'global_player_rankings')
  AND job.parent_attempt_id IS NULL
  AND job.updated_at < clock_timestamp() - make_interval(hours => %s)
  AND (%s::bigint[] IS NULL OR job.id = ANY(%s::bigint[]))
  AND NOT EXISTS (
      SELECT 1 FROM collector_attempts AS attempt
      JOIN collector_jobs AS child ON child.parent_attempt_id = attempt.id
      WHERE attempt.job_id = job.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM collector_transport_failures WHERE collection_job_id = job.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM reset_baseline_evidence WHERE collection_job_id = job.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM collector_observations AS o
      WHERE o.collection_job_id = job.id AND (
          NOT EXISTS (
              SELECT 1 FROM observation_processing_outcomes AS outcome
              WHERE outcome.observation_id = o.id AND outcome.outcome = 'processed'
          )
          OR EXISTS (
              SELECT 1 FROM python_processing_jobs AS p
              WHERE (p.observation_id = o.id OR p.replay_observation_id = o.id)
                AND (p.status <> 'complete' OR
                     p.updated_at >= clock_timestamp() - make_interval(hours => %s))
          )
          OR EXISTS (SELECT 1 FROM player_profile_versions WHERE observation_id = o.id)
          OR EXISTS (SELECT 1 FROM battle_evidence WHERE observation_id = o.id)
          OR EXISTS (
              SELECT 1 FROM leaderboard_snapshot_entries
              WHERE trophy_observation_id = o.id OR profile_observation_id = o.id
          )
          OR EXISTS (
              SELECT 1 FROM reset_baseline_evidence
              WHERE profile_observation_id = o.id OR battle_log_observation_id = o.id
          )
          OR EXISTS (
              SELECT 1 FROM player_profile_effects AS effect
              WHERE effect.observation_id = o.id AND NOT EXISTS (
                  SELECT 1 FROM player_profile_effects AS newer
                  WHERE newer.profile_version_id = effect.profile_version_id
                    AND (newer.observed_at, newer.id) > (effect.observed_at, effect.id)
              )
          )
          OR EXISTS (
              SELECT 1 FROM official_top200_versions AS version
              WHERE version.observation_id = o.id AND (
                  EXISTS (SELECT 1 FROM leaderboard_snapshot_entries
                          WHERE official_rank_version_id = version.id)
                  OR NOT EXISTS (
                      SELECT 1 FROM official_top200_versions AS newer
                      WHERE newer.parser_version = version.parser_version
                        AND (newer.observed_at, newer.id) > (version.observed_at, version.id)
                  )
              )
          )
          OR EXISTS (
              SELECT 1 FROM battle_log_observations AS log
              WHERE log.observation_id = o.id AND (
                  log.parsed_payload_id IS NULL OR log.has_row_gap
                  OR log.parsed_payload_id IS DISTINCT FROM (
                      SELECT previous.parsed_payload_id FROM battle_log_observations AS previous
                      WHERE previous.player_id = log.player_id
                        AND previous.parser_version = log.parser_version
                        AND (previous.observed_at, previous.id) < (log.observed_at, log.id)
                      ORDER BY previous.observed_at DESC, previous.id DESC LIMIT 1
                  )
                  OR log.parsed_payload_id IS DISTINCT FROM (
                      SELECT following.parsed_payload_id FROM battle_log_observations AS following
                      WHERE following.player_id = log.player_id
                        AND following.parser_version = log.parser_version
                        AND (following.observed_at, following.id) > (log.observed_at, log.id)
                      ORDER BY following.observed_at, following.id LIMIT 1
                  )
              )
          )
      )
  )
ORDER BY job.updated_at, job.id
LIMIT %s
FOR UPDATE OF job SKIP LOCKED
"""


def prune_completed_history(
    connection: Any,
    *,
    retention_hours: int = 48,
    max_jobs: int = 1000,
    apply: bool = False,
) -> dict[str, int | bool]:
    if not 48 <= retention_hours <= 24 * 28:
        raise ValueError("retention_hours must be between 48 and 672")
    if not 1 <= max_jobs <= 1000:
        raise ValueError("max_jobs must be between 1 and 1000")
    with connection.transaction():
        isolation = connection.execute("SHOW transaction_isolation").fetchone()[0]
        if isinstance(isolation, bytes):
            isolation = isolation.decode()
        if isolation != "read committed":
            raise ValueError("history cleanup requires READ COMMITTED isolation")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        candidates = [
            row[0]
            for row in connection.execute(
                _ELIGIBLE,
                (retention_hours, None, None, retention_hours, max_jobs),
            ).fetchall()
        ]
        if candidates:
            # Block new replay references, then fence existing processing jobs.
            # Recheck eligibility in a fresh READ COMMITTED statement after locks.
            connection.execute(
                """
                SELECT id FROM collector_observations
                WHERE collection_job_id = ANY(%s::bigint[]) ORDER BY id FOR UPDATE
                """,
                (candidates,),
            ).fetchall()
            connection.execute(
                """
                SELECT p.id FROM python_processing_jobs AS p
                JOIN collector_observations AS o
                  ON o.id = COALESCE(p.observation_id, p.replay_observation_id)
                WHERE o.collection_job_id = ANY(%s::bigint[])
                ORDER BY p.id FOR UPDATE OF p
                """,
                (candidates,),
            ).fetchall()
            candidates = [
                row[0]
                for row in connection.execute(
                    _ELIGIBLE,
                    (retention_hours, candidates, candidates, retention_hours, max_jobs),
                ).fetchall()
            ]
        deleted = 0
        if apply and candidates:
            deleted = connection.execute(
                "DELETE FROM collector_jobs WHERE id = ANY(%s::bigint[])",
                (candidates,),
            ).rowcount
    garbage = _prune_unused_content(connection, retention_hours, max_jobs, apply)
    return {
        **garbage,
        "apply": apply,
        "retention_hours": retention_hours,
        "eligible_collection_jobs": len(candidates),
        "deleted_collection_jobs": deleted,
    }


def _prune_unused_content(connection: Any, hours: int, limit: int, apply: bool) -> dict[str, int]:
    # Only these operational tables are eligible; account/export jobs and domain
    # histories are deliberately outside this cleanup surface.
    predicates = {
        "python_processing_jobs": """
            target.status = 'complete'
            AND target.work_type <> 'build_export'
            AND target.updated_at < clock_timestamp() - make_interval(hours => %s)
            AND NOT EXISTS (SELECT 1 FROM boundary_publication_legacy_job_migrations
                            WHERE job_id = target.id)
        """,
        "official_top200_entries": """
            target.version_id IS NULL AND target.parsed_payload_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM parsed_source_payloads AS payload
                        WHERE payload.id = target.parsed_payload_id
                          AND payload.created_at < clock_timestamp() - make_interval(hours => %s)
                          AND NOT EXISTS (SELECT 1 FROM collector_observations AS o
                              WHERE o.response_hash = payload.response_hash))
            AND NOT EXISTS (SELECT 1 FROM official_top200_attempt_entries WHERE source_row_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM official_top200_version_entries WHERE source_row_id = target.id)
        """,
        "parsed_source_payloads": """
            target.created_at < clock_timestamp() - make_interval(hours => %s)
            AND NOT EXISTS (SELECT 1 FROM collector_observations WHERE response_hash = target.response_hash)
            AND NOT EXISTS (SELECT 1 FROM observation_processing_outcomes WHERE parsed_payload_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM player_profile_versions WHERE parsed_payload_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM player_profile_effects WHERE parsed_payload_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM battle_log_observations WHERE parsed_payload_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM battle_source_rows WHERE parsed_payload_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM official_top200_entries WHERE parsed_payload_id = target.id)
        """,
        "source_response_parses": """
            target.created_at < clock_timestamp() - make_interval(hours => %s)
            AND NOT EXISTS (SELECT 1 FROM processed_observation_versions WHERE parse_id = target.id)
            AND NOT EXISTS (SELECT 1 FROM collector_observations WHERE response_hash = target.response_hash)
        """,
    }
    counts = {}
    for table, predicate in predicates.items():
        with connection.transaction():
            connection.execute("SET LOCAL lock_timeout = '1s'")
            connection.execute("SET LOCAL statement_timeout = '30s'")
            rows = connection.execute(
                f"SELECT target.id FROM {table} AS target WHERE {predicate} "
                "ORDER BY target.id LIMIT %s FOR UPDATE OF target SKIP LOCKED",
                (hours, limit),
            ).fetchall()
            counts[f"eligible_{table}"] = len(rows)
            counts[f"deleted_{table}"] = 0
            if apply and rows:
                counts[f"deleted_{table}"] = connection.execute(
                    f"DELETE FROM {table} WHERE id = ANY(%s::bigint[])",
                    ([row[0] for row in rows],),
                ).rowcount
    return counts
