from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary
from .analytics import CLASSIFICATION_CONFIDENCE, CLASSIFICATION_VERSION
from .army_decoder import DECODER_VERSION
from .catalog import CATALOG_VERSION
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    _hash_input,
    _parse_utc,
    _positive_int_input,
    _snapshot_freshness,
    _text_value,
)
from .domain import DomainRuleError


def reevaluate_boundary_publications(database) -> int:
    """Recover ready coordinator generations after worker restart/handoff."""
    with database.pool.connection() as connection:
        if (
            connection.execute(
                "SELECT to_regclass(current_schema() || '.boundary_publication_generations')"
            ).fetchone()[0]
            is None
        ):
            return 0
        with connection.transaction():
            generations = connection.execute(
                """
                SELECT id, boundary_at
                FROM boundary_publication_generations
                WHERE snapshot_state IN ('pending', 'ready')
                   OR army_state IN ('pending', 'ready')
                ORDER BY boundary_at, generation
                FOR UPDATE
                """
            ).fetchall()
            for generation_id, boundary_at in generations:
                boundary._try_enqueue_boundary_artifacts(database, 
                    connection,
                    boundary_at=boundary_at,
                    generation_id=int(generation_id),
                )
            corrections = connection.execute(
                """
                SELECT source_generation_id
                FROM boundary_publication_corrections
                WHERE state IN ('queued', 'pending_inputs')
                ORDER BY requested_at, id
                FOR UPDATE SKIP LOCKED
                """
            ).fetchall()
            for (source_generation_id,) in corrections:
                _maybe_emit_boundary_signal(database, 
                    connection, generation_id=int(source_generation_id)
                )
            return len(generations) + len(corrections)


def _maybe_emit_boundary_signal(database: Database, connection: Any, generation_id: int) -> None:
    boundary_row = connection.execute(
        "SELECT boundary_at FROM boundary_publication_generations WHERE id = %s",
        (generation_id,),
    ).fetchone()
    if boundary_row is None:
        return
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"boundary-publication:{boundary_row[0].astimezone(UTC).isoformat()}",),
    )
    row = connection.execute(
        """
        SELECT boundary_at, generation, snapshot_id,
               snapshot_input_hash, snapshot_analytics_publication_id,
               army_publication_id, supersedes_id,
               snapshot_manifest_id, army_manifest_id,
               correction_state, army_input_hash,
               snapshot_rule_version, army_rule_version,
               ordering_rule_version, freshness_rule_version
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if row is None or any(value is None for value in row[2:6]):
        return
    identities = connection.execute(
        """
        SELECT count(*)
        FROM boundary_publication_artifact_identities
        WHERE (id, artifact_kind, manifest_id, input_hash) IN (
            (%s, 'analytics', %s, %s),
            (%s, 'army', %s, %s)
        )
        """,
        (row[4], row[7], _text_value(row[3]), row[5], row[8], _text_value(row[10])),
    ).fetchone()
    if identities is None or int(identities[0]) != 2:
        return
    connection.execute(
        """
        INSERT INTO boundary_publication_events (
            boundary_at, generation, snapshot_id, snapshot_input_hash,
            snapshot_analytics_publication_id, army_publication_id,
            superseded_generation, manifest_ids, rule_versions
        ) VALUES (%s, %s, %s, %s, %s, %s,
                  (SELECT generation FROM boundary_publication_generations WHERE id = %s),
                  %s, %s)
        ON CONFLICT (boundary_at, generation) DO NOTHING
        """,
        (
            row[0],
            int(row[1]),
            int(row[2]),
            _text_value(row[3]),
            int(row[4]),
            int(row[5]),
            row[6],
            Jsonb(
                {
                    "snapshot": int(row[7]) if row[7] is not None else None,
                    "army": int(row[8]) if row[8] is not None else None,
                }
            ),
            Jsonb(
                {
                    "ordering_rule_version": _text_value(row[13]),
                    "freshness_rule_version": _text_value(row[14]),
                    "analytics_rule_version": _text_value(row[11]),
                    "army_analytics_rule_version": _text_value(row[12]),
                }
            ),
        ),
    )
    if _text_value(row[9]) in {"none", "active", "finalized"}:
        connection.execute(
            "UPDATE boundary_publication_generations SET correction_state = 'finalized', updated_at = clock_timestamp() WHERE id = %s",
            (generation_id,),
        )
        connection.execute(
            """
            UPDATE boundary_publication_corrections
            SET state = 'finalized', finalized_at = clock_timestamp()
            WHERE generation_id = %s AND state = 'active'
            """,
            (generation_id,),
        )
        queued = connection.execute(
            """
            SELECT id, affected_artifacts, pending_inputs
            FROM boundary_publication_corrections
            WHERE boundary_at = %s AND state IN ('queued', 'pending_inputs')
            ORDER BY requested_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (row[0],),
        ).fetchone()
        if queued is not None:
            source = connection.execute(
                """
                SELECT sweep_id, expected_population_count, expected_population_hash,
                       ordering_rule_version, freshness_rule_version,
                       generation + 1, membership_rule_version,
                       snapshot_rule_version, army_rule_version,
                       target_rule, target_at
                FROM boundary_publication_generations
                WHERE id = %s
                FOR UPDATE
                """,
                (generation_id,),
            ).fetchone()
            assert source is not None
            pending_inputs = queued[2] if isinstance(queued[2], list) else []
            pending_ready = True
            for pending in pending_inputs:
                pending_version = (
                    pending.get("ranked_day_version_id")
                    if isinstance(pending, dict)
                    else None
                )
                if pending_version is None:
                    continue
                pending_state = connection.execute(
                    "SELECT state FROM ranked_day_versions WHERE id = %s",
                    (pending_version,),
                ).fetchone()
                if pending_state is None or _text_value(pending_state[0]) not in {
                    "Complete",
                    "Partial",
                    "Failed",
                    "Missing",
                    "Unavailable",
                    "Inconsistent",
                    "Malformed",
                }:
                    pending_ready = False
                    break
            if not pending_ready:
                connection.execute(
                    "UPDATE boundary_publication_corrections SET state = 'pending_inputs' WHERE id = %s",
                    (queued[0],),
                )
                return
            connection.execute(
                "UPDATE boundary_publication_corrections SET state = 'activation' WHERE id = %s",
                (queued[0],),
            )
            new = connection.execute(
                """
                INSERT INTO boundary_publication_generations (
                    boundary_at, generation, sweep_id, ordering_rule_version,
                    freshness_rule_version, expected_population_count,
                    expected_population_hash, membership_rule_version,
                    snapshot_rule_version, army_rule_version,
                    target_rule, target_at, supersedes_id, source_generation_id,
                    correction_state, affected_artifacts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
                RETURNING id
                """,
                (
                    row[0],
                    int(source[5]),
                    source[0],
                    source[3],
                    source[4],
                    source[1],
                    source[2],
                    source[6],
                    source[7],
                    source[8],
                    source[9],
                    source[10],
                    generation_id,
                    generation_id,
                    queued[1],
                ),
            ).fetchone()
            assert new is not None
            new_id = int(new[0])
            connection.execute(
                "UPDATE boundary_publication_corrections SET state = 'inheritance' WHERE id = %s",
                (queued[0],),
            )
            connection.execute(
                """
                INSERT INTO boundary_publication_generation_members
                    (generation_id, player_id, ranked_day_version_id,
                     ranked_day_input_hash, status, snapshot_status, army_status)
                SELECT %s, player_id, ranked_day_version_id,
                       ranked_day_input_hash, status, snapshot_status, army_status
                FROM boundary_publication_generation_members
                WHERE generation_id = %s
                """,
                (new_id, generation_id),
            )
            for pending in queued[2] if isinstance(queued[2], list) else []:
                pending_version = pending.get("ranked_day_version_id")
                pending_snapshot_status = boundary._boundary_snapshot_status(
                    connection,
                    player_id=int(pending["player_id"]),
                    ranked_day_version_id=int(pending_version),
                    boundary_at=row[0],
                )
                pending_army_status = boundary._boundary_army_status(database, 
                    connection,
                    player_id=int(pending["player_id"]),
                    ranked_day_version_id=int(pending_version),
                    snapshot_status=pending_snapshot_status,
                )
                connection.execute(
                    """
                    UPDATE boundary_publication_generation_members
                    SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                        status = 'terminal', snapshot_status = %s,
                        army_status = %s, updated_at = clock_timestamp()
                    WHERE generation_id = %s AND player_id = %s
                    """,
                    (
                        pending_version,
                        pending.get("input_hash"),
                        pending_snapshot_status,
                        pending_army_status,
                        new_id,
                        pending.get("player_id"),
                    ),
                )
            connection.execute(
                "UPDATE boundary_publication_generations SET membership_captured_at = clock_timestamp() WHERE id = %s",
                (new_id,),
            )
            affected = [_text_value(value) for value in (queued[1] or [])]
            if "snapshot" not in affected:
                connection.execute(
                    """
                    UPDATE boundary_publication_generations AS target
                    SET snapshot_state = source.snapshot_state,
                        snapshot_id = source.snapshot_id,
                        snapshot_input_hash = source.snapshot_input_hash,
                        snapshot_manifest_id = source.snapshot_manifest_id,
                        snapshot_analytics_publication_id = source.snapshot_analytics_publication_id,
                        snapshot_coverage = source.snapshot_coverage
                    FROM boundary_publication_generations AS source
                    WHERE target.id = %s AND source.id = %s
                    """,
                    (new_id, generation_id),
                )
            if "army" not in affected:
                connection.execute(
                    """
                    UPDATE boundary_publication_generations AS target
                    SET army_state = source.army_state,
                        army_input_hash = source.army_input_hash,
                        army_manifest_id = source.army_manifest_id,
                        army_publication_id = source.army_publication_id,
                        army_coverage = source.army_coverage
                    FROM boundary_publication_generations AS source
                    WHERE target.id = %s AND source.id = %s
                    """,
                    (new_id, generation_id),
                )
            # Unaffected artifacts retain the exact immutable publication
            # identity. Only the affected artifact receives a new identity.
            connection.execute(
                "UPDATE boundary_publication_corrections SET state = 'active', generation_id = %s, started_at = clock_timestamp() WHERE id = %s",
                (new_id, queued[0]),
            )
            boundary._try_enqueue_boundary_artifacts(database, 
                connection, boundary_at=row[0], generation_id=new_id
            )


def complete_analytics(database: Database, claim: Claim) -> None:
    snapshot_id = _positive_int_input(claim.input_json, "snapshot_id")
    snapshot_version = _positive_int_input(claim.input_json, "snapshot_version")
    source_value = claim.input_json.get("source_ranked_day_version_id")
    generation_input = claim.input_json.get("generation")
    source_ranked_day_version_id = (
        _positive_int_input(claim.input_json, "source_ranked_day_version_id")
        if source_value is not None and generation_input is None
        else None
    )
    snapshot_input_hash = _hash_input(
        claim.input_json.get("snapshot_input_hash"), "snapshot_input_hash"
    )
    content_dedup = getattr(database, "_supports_content_dedup", False)
    source_rows_relation = (
        "battle_log_observation_source_rows" if content_dedup else "battle_source_rows"
    )
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("leaderboard-snapshot-v2:frozen:analytics:" + str(snapshot_id),),
            )
            snapshot = connection.execute(
                """
                SELECT id, boundary_at, version, correction_of_id, state,
                       source_ranked_day_version_id, input_hash,
                       measured_coverage, stale_entry_count,
                       fresh_entry_count, included_entry_count
                FROM leaderboard_snapshots
                WHERE id = %s AND snapshot_kind = 'frozen'
                FOR UPDATE
                """,
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise ValueError("frozen snapshot dependency is not complete")
            boundary_generation = connection.execute(
                """
                SELECT id, generation, snapshot_state
                FROM boundary_publication_generations
                WHERE snapshot_id = %s
                ORDER BY generation DESC
                LIMIT 1
                FOR UPDATE
                """,
                (snapshot_id,),
            ).fetchone()
            if boundary_generation is not None and _text_value(
                boundary_generation[2]
            ) in {"superseded", "published"}:
                database._finish_claim(
                    connection,
                    claim,
                    job,
                    state="complete",
                    outcome="stale_superseded",
                )
                return
            if int(snapshot[2]) != snapshot_version:
                raise ValueError(
                    "analytics snapshot version does not match its input"
                )
            if (
                source_ranked_day_version_id is not None
                and snapshot[5] is not None
                and int(snapshot[5]) != source_ranked_day_version_id
            ):
                raise ValueError(
                    "analytics ranked-day source does not match its snapshot"
                )
            if _text_value(snapshot[6]) != snapshot_input_hash:
                raise ValueError(
                    "analytics snapshot input hash does not match its input"
                )

            existing_summary_count = connection.execute(
                """
                SELECT count(DISTINCT s.lens), count(*),
                       count(*) FILTER (WHERE b.summary_id IS NOT NULL)
                FROM analytics_summaries AS s
                LEFT JOIN analytics_breakdowns AS b
                  ON b.summary_id = s.id
                 AND b.army_archetype = 'Unclassified'
                WHERE s.snapshot_id = %s
                """,
                (snapshot_id,),
            ).fetchone()
            assert existing_summary_count is not None
            if _text_value(snapshot[4]) == "published":
                if tuple(int(value) for value in existing_summary_count) != (
                    2,
                    2,
                    2,
                ):
                    raise ValueError(
                        "published frozen snapshot has incomplete analytics"
                    )
                if boundary_generation is not None:
                    _maybe_emit_boundary_signal(database, 
                        connection, int(boundary_generation[0])
                    )
                database._finish_claim(
                    connection, claim, job, state="complete", outcome="processed"
                )
                return
            if _text_value(snapshot[4]) != "building":
                raise ValueError("frozen snapshot is not available for analytics")

            ranked_day = None
            if source_ranked_day_version_id is not None:
                ranked_day = connection.execute(
                    "SELECT ranked_day_start, ranked_day_end FROM ranked_day_versions WHERE id = %s",
                    (source_ranked_day_version_id,),
                ).fetchone()
            if ranked_day is None and boundary_generation is not None:
                ranked_day = connection.execute(
                    "SELECT boundary_at - interval '1 day', boundary_at FROM boundary_publication_generations WHERE id = %s",
                    (boundary_generation[0],),
                ).fetchone()
            if ranked_day is None:
                raise ValueError("ranked-day version for analytics does not exist")
            period_start = ranked_day[0]
            period_end = ranked_day[1]
            input_period_start = claim.input_json.get("period_start")
            input_period_end = claim.input_json.get("period_end")
            if (
                input_period_start is not None
                and _parse_utc(input_period_start) != period_start
            ):
                raise ValueError("analytics period start does not match ranked day")
            if (
                input_period_end is not None
                and _parse_utc(input_period_end) != period_end
            ):
                raise ValueError("analytics period end does not match ranked day")
            population_filter = claim.input_json.get(
                "population_filter", {"population": "tracked_players"}
            )
            if population_filter != {"population": "tracked_players"}:
                raise ValueError(
                    "analytics population filter is not the tracked population"
                )
            entry_count = connection.execute(
                """
                SELECT count(*)
                FROM leaderboard_snapshot_entries
                WHERE snapshot_id = %s
                """,
                (snapshot_id,),
            ).fetchone()
            assert entry_count is not None
            if int(entry_count[0]) != int(snapshot[10]):
                raise ValueError("frozen snapshot entries are incomplete")

            freshness = _snapshot_freshness(
                included_count=int(snapshot[10]),
                fresh_count=int(snapshot[9]),
                stale_count=int(snapshot[8]),
            )
            prior_snapshot_id = (
                int(snapshot[3]) if snapshot[3] is not None else None
            )
            for lens, perspective in (
                ("offense", "attacker"),
                ("defense", "defender"),
            ):
                sample_rows = connection.execute(
                    """
                    SELECT *
                    FROM (
                        SELECT DISTINCT ON (b.id)
                               b.id AS battle_id, b.disagreement_state, e.stars,
                               e.army_share_code, e.id AS evidence_id, e.source_row_id,
                               e.observation_id, e.source_observed_at,
                               e.battle_timestamp
                        FROM battle_perspectives AS p
                        JOIN legend_battles AS b ON b.id = p.battle_id
                        JOIN battle_evidence AS e ON e.id = p.evidence_id
                        JOIN battle_source_rows AS source_row
                          ON source_row.id = e.source_row_id
                        JOIN leaderboard_snapshot_entries AS se
                          ON se.snapshot_id = %s
                         AND se.player_id = CASE
                             WHEN p.perspective = 'attacker'
                                 THEN b.attacker_player_id
                             ELSE b.defender_player_id
                         END
                        WHERE p.perspective = %s
                          AND e.reporting_player_id = CASE
                             WHEN p.perspective = 'attacker'
                                 THEN b.attacker_player_id
                             ELSE b.defender_player_id
                          END
                          AND source_row.outcome = 'valid_legend'
                          AND e.army_share_code IS NOT NULL
                          AND e.army_share_code <> ''
                          AND e.battle_timestamp >= %s
                          AND e.battle_timestamp < %s
                        ORDER BY b.id, p.source_observed_at DESC, e.id DESC
                    ) AS latest
                    ORDER BY latest.battle_timestamp, latest.battle_id
                    """,
                    (snapshot_id, perspective, period_start, period_end),
                ).fetchall()
                quality = connection.execute(
                    f"""
                    SELECT
                        0,
                        count(*) FILTER (
                            WHERE source_row.outcome = 'malformed_legend_row'
                               OR (
                                  source_row.outcome = 'valid_legend'
                                  AND (
                                      NOT (source_row.source_json ? 'armyShareCode')
                                      OR source_row.source_json ->> 'armyShareCode' IS NULL
                                      OR source_row.source_json ->> 'armyShareCode' = ''
                                  )
                               )
                        )
                    FROM battle_log_observations AS log
                    JOIN {source_rows_relation} AS source_row
                      ON source_row.battle_log_observation_id = log.id
                    JOIN leaderboard_snapshot_entries AS se
                      ON se.snapshot_id = %s AND se.player_id = log.player_id
                    WHERE log.observed_at >= %s
                      AND log.observed_at < %s
                      AND (
                          (
                              log.parser_version = 'supercell-source-parser-v2'
                              AND source_row.source_json ->> 'attack' = %s
                          )
                          OR (
                              log.parser_version = 'supercell-source-parser-v1'
                              AND source_row.source_json ->> 'attackOrDefense' = %s
                          )
                          OR source_row.outcome <> 'valid_legend'
                          OR (
                              source_row.outcome = 'valid_legend'
                              AND (
                                  NOT (source_row.source_json ? 'armyShareCode')
                                  OR source_row.source_json ->> 'armyShareCode' IS NULL
                                  OR source_row.source_json ->> 'armyShareCode' = ''
                              )
                          )
                      )
                    """,
                    (
                        snapshot_id,
                        period_start,
                        period_end,
                        "true" if perspective == "attacker" else "false",
                        "attack" if perspective == "attacker" else "defense",
                    ),
                ).fetchone()
                assert quality is not None
                missing_code_count = int(quality[0])
                malformed_code_count = int(quality[1])
                sample_size = len(sample_rows)
                three_star_count = sum(int(row[2]) == 3 for row in sample_rows)
                disagreement_count = sum(
                    _text_value(row[1]) == "disagreement" for row in sample_rows
                )
                sample_payload = [
                    {
                        "battle_id": int(row[0]),
                        "evidence_id": int(row[4]),
                        "source_row_id": int(row[5]),
                        "observation_id": int(row[6]),
                        "source_observed_at": row[7].astimezone(UTC).isoformat(),
                        "battle_timestamp": row[8].astimezone(UTC).isoformat(),
                        "stars": int(row[2]),
                        "army_share_code": _text_value(row[3]),
                        "disagreement": _text_value(row[1]) == "disagreement",
                    }
                    for row in sample_rows
                ]
                analytics_input = {
                    "snapshot_id": snapshot_id,
                    "snapshot_version": snapshot_version,
                    "snapshot_input_hash": snapshot_input_hash,
                    **(
                        {
                            "source_ranked_day_version_id": source_ranked_day_version_id
                        }
                        if source_ranked_day_version_id is not None
                        else {}
                    ),
                    **(
                        {"generation": int(boundary_generation[1])}
                        if boundary_generation is not None
                        else {}
                    ),
                    "lens": lens,
                    "population_filter": population_filter,
                    "period_start": period_start.astimezone(UTC).isoformat(),
                    "period_end": period_end.astimezone(UTC).isoformat(),
                    "sample": sample_payload,
                    "missing_code_count": missing_code_count,
                    "malformed_code_count": malformed_code_count,
                    "analytics_rule_version": ANALYTICS_RULE_VERSION,
                    "classification_version": CLASSIFICATION_VERSION,
                }
                analytics_input_hash = hashlib.sha256(
                    json.dumps(
                        analytics_input,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                correction_of_id = None
                if prior_snapshot_id is not None:
                    correction = connection.execute(
                        """
                        SELECT id
                        FROM analytics_summaries
                        WHERE snapshot_id = %s
                          AND lens = %s
                          AND population_filter = %s
                          AND period_start = %s
                          AND period_end = %s
                          AND analytics_rule_version = %s
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (
                            prior_snapshot_id,
                            lens,
                            Jsonb(population_filter),
                            period_start,
                            period_end,
                            ANALYTICS_RULE_VERSION,
                        ),
                    ).fetchone()
                    if correction is not None:
                        correction_of_id = int(correction[0])
                summary = connection.execute(
                    """
                    INSERT INTO analytics_summaries (
                        snapshot_id, snapshot_version,
                        source_ranked_day_version_id, correction_of_id,
                        lens, population_filter,
                        period_start, period_end, sample_size,
                        measured_coverage, freshness, classification_version,
                        classification_confidence, unclassified_count,
                        disagreement_count, missing_code_count,
                        malformed_code_count, analytics_rule_version, input_hash
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (
                        snapshot_id, lens, population_filter,
                        period_start, period_end, analytics_rule_version
                    ) DO NOTHING
                    RETURNING id
                    """,
                    (
                        snapshot_id,
                        snapshot_version,
                        source_ranked_day_version_id,
                        correction_of_id,
                        lens,
                        Jsonb(population_filter),
                        period_start,
                        period_end,
                        sample_size,
                        snapshot[7],
                        freshness,
                        CLASSIFICATION_VERSION,
                        CLASSIFICATION_CONFIDENCE,
                        sample_size,
                        disagreement_count,
                        missing_code_count,
                        malformed_code_count,
                        ANALYTICS_RULE_VERSION,
                        analytics_input_hash,
                    ),
                ).fetchone()
                if summary is None:
                    summary = connection.execute(
                        """
                        SELECT id, input_hash
                        FROM analytics_summaries
                        WHERE snapshot_id = %s
                          AND lens = %s
                          AND population_filter = %s
                          AND period_start = %s
                          AND period_end = %s
                          AND analytics_rule_version = %s
                        """,
                        (
                            snapshot_id,
                            lens,
                            Jsonb(population_filter),
                            period_start,
                            period_end,
                            ANALYTICS_RULE_VERSION,
                        ),
                    ).fetchone()
                    if (
                        summary is None
                        or _text_value(summary[1]) != analytics_input_hash
                    ):
                        raise ValueError(
                            "analytics replay has an immutable input conflict"
                        )
                summary_id = int(summary[0])
                evidence_json = {
                    "army_share_codes": [
                        _text_value(row[3]) for row in sample_rows
                    ],
                    "battle_ids": [int(row[0]) for row in sample_rows],
                    "evidence_ids": [int(row[4]) for row in sample_rows],
                    "source_row_ids": [int(row[5]) for row in sample_rows],
                }
                connection.execute(
                    """
                    INSERT INTO analytics_breakdowns (
                        summary_id, army_archetype, attack_count,
                        three_star_count, usage_rate, three_star_rate,
                        evidence_json
                    ) VALUES (%s, 'Unclassified', %s, %s, %s, %s, %s)
                    ON CONFLICT (summary_id, army_archetype) DO NOTHING
                    """,
                    (
                        summary_id,
                        sample_size,
                        three_star_count,
                        1.0 if sample_size else None,
                        three_star_count / sample_size if sample_size else None,
                        Jsonb(evidence_json),
                    ),
                )

            complete = connection.execute(
                """
                SELECT count(DISTINCT s.lens), count(*),
                       count(*) FILTER (WHERE b.summary_id IS NOT NULL)
                FROM analytics_summaries AS s
                LEFT JOIN analytics_breakdowns AS b
                  ON b.summary_id = s.id
                 AND b.army_archetype = 'Unclassified'
                WHERE s.snapshot_id = %s
                """,
                (snapshot_id,),
            ).fetchone()
            assert complete is not None
            if tuple(int(value) for value in complete) != (2, 2, 2):
                raise ValueError("analytics publication is incomplete")
            published = connection.execute(
                """
                UPDATE leaderboard_snapshots
                SET state = 'published', published_at = clock_timestamp()
                WHERE id = %s AND state = 'building'
                """,
                (snapshot_id,),
            )
            if published.rowcount != 1:
                raise ValueError("frozen snapshot publication fence was lost")
            connection.execute(
                """
                UPDATE leaderboard_snapshots
                SET state = 'superseded'
                WHERE snapshot_kind = 'frozen'
                  AND boundary_at = %s
                  AND id <> %s
                  AND state = 'published'
                """,
                (snapshot[1], snapshot_id),
            )
            if boundary_generation is not None:
                summary_ids = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT id FROM analytics_summaries WHERE snapshot_id = %s ORDER BY lens, id",
                        (snapshot_id,),
                    ).fetchall()
                ]
                analytics_identity = boundary._create_boundary_artifact_identity(
                    connection,
                    generation_id=int(boundary_generation[0]),
                    artifact_kind="analytics",
                    manifest_id=int(
                        connection.execute(
                            "SELECT snapshot_manifest_id FROM boundary_publication_generations WHERE id = %s",
                            (boundary_generation[0],),
                        ).fetchone()[0]
                    ),
                    input_hash=_text_value(snapshot[6]),
                    source_identity={
                        "snapshot_id": snapshot_id,
                        "summary_ids": summary_ids,
                    },
                )
                published_generation = connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_state = 'published',
                        snapshot_analytics_publication_id = %s,
                        updated_at = clock_timestamp()
                    WHERE id = %s AND snapshot_state = 'building'
                      AND snapshot_manifest_id = %s
                    RETURNING id
                    """,
                    (
                        analytics_identity,
                        boundary_generation[0],
                        int(claim.input_json["manifest_id"]),
                    ),
                ).fetchone()
                if published_generation is None:
                    raise ValueError(
                        "boundary publication generation fence was lost"
                    )
                _maybe_emit_boundary_signal(database, 
                    connection, int(boundary_generation[0])
                )
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def _boundary_army_manifest_needs_correction(
    database, connection: Any, *, manifest_id: int
) -> bool:
    rows = connection.execute(
        """
        SELECT player_id, ranked_day_version_id, input_identity
        FROM boundary_publication_manifest_rows
        WHERE manifest_id = %s
        ORDER BY ordinal
        """,
        (manifest_id,),
    ).fetchall()
    for player_id, version_id, identity in rows:
        expected = {
            int(value)
            for value in (
                identity.get("decode_ids", []) if isinstance(identity, dict) else []
            )
            if str(value).isdigit()
        }
        expected_battles = {
            int(value)
            for value in (
                identity.get("battle_ids", []) if isinstance(identity, dict) else []
            )
            if str(value).isdigit()
        }
        actual: set[int] = set()
        if version_id is not None:
            daily = connection.execute(
                """
                SELECT battles
                FROM api_player_daily_logs
                WHERE player_id = %s AND ranked_day_version_id = %s
                ORDER BY version DESC LIMIT 1
                """,
                (player_id, version_id),
            ).fetchone()
            for event in daily[0] if daily and isinstance(daily[0], list) else []:
                if not isinstance(event, dict) or event.get("included") is False:
                    continue
                battle_id = event.get("battle_id")
                lens = event.get("lens")
                if (
                    not str(battle_id).isdigit()
                    or int(battle_id) not in expected_battles
                    or lens not in {"offense", "defense"}
                ):
                    continue
                perspective = "attacker" if lens == "offense" else "defender"
                decode = connection.execute(
                    """
                    SELECT id
                    FROM battle_army_decodes
                    WHERE battle_id = %s AND perspective = %s
                      AND is_active AND decoder_version = %s AND catalog_version = %s
                    """,
                    (int(battle_id), perspective, DECODER_VERSION, CATALOG_VERSION),
                ).fetchone()
                if decode is not None:
                    actual.add(int(decode[0]))
        if actual != expected:
            return True
    return False


def _queue_boundary_army_correction(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    generation_id: int,
    defer_inheritance: bool = False,
) -> None:
    affected = ["army"]
    queued = connection.execute(
        """
        SELECT id
        FROM boundary_publication_corrections
        WHERE boundary_at = %s AND source_generation_id = %s
          AND state IN ('queued', 'pending_inputs')
        ORDER BY id DESC LIMIT 1
        FOR UPDATE
        """,
        (boundary_at, generation_id),
    ).fetchone()
    if queued is not None:
        connection.execute(
            """
            UPDATE boundary_publication_corrections
            SET affected_artifacts = ARRAY(
                    SELECT DISTINCT unnest(affected_artifacts || %s::text[])
                )
            WHERE id = %s
            """,
            (affected, queued[0]),
        )
        connection.execute(
            "UPDATE boundary_publication_corrections SET pending_inputs = pending_inputs || %s::jsonb WHERE id = %s",
            (Jsonb([{"kind": "decode"}]), queued[0]),
        )
        return
    current = connection.execute(
        """
        SELECT snapshot_state, army_state, army_manifest_id, generation
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if (
        current is None
        or current[2] is None
        or _text_value(current[1]) == "superseded"
    ):
        return
    if (
        _text_value(current[0]) == "published"
        and _text_value(current[1]) == "published"
    ):
        connection.execute(
            """
            UPDATE boundary_publication_generations
            SET snapshot_state = 'superseded', army_state = 'superseded',
                correction_state = 'finalized', updated_at = clock_timestamp()
            WHERE id = %s
            """,
            (generation_id,),
        )
        player_ids = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT player_id
                FROM boundary_publication_generation_members
                WHERE generation_id = %s ORDER BY player_id
                """,
                (generation_id,),
            ).fetchall()
        ]
        new_id, _new_generation = boundary._create_boundary_generation(database, 
            connection,
            boundary_at=boundary_at,
            sweep_id=int(
                connection.execute(
                    "SELECT sweep_id FROM boundary_publication_generations WHERE id = %s",
                    (generation_id,),
                ).fetchone()[0]
            ),
            player_ids=player_ids,
            generation=int(current[3]) + 1,
            supersedes_id=generation_id,
        )
        connection.execute(
            "UPDATE boundary_publication_generations SET affected_artifacts = %s WHERE id = %s",
            (affected, new_id),
        )
        if not defer_inheritance:
            connection.execute(
                """
                UPDATE boundary_publication_generations AS target
                SET snapshot_state = 'published',
                    snapshot_id = source.snapshot_id,
                    snapshot_input_hash = source.snapshot_input_hash,
                    snapshot_manifest_id = source.snapshot_manifest_id,
                    snapshot_analytics_publication_id = source.snapshot_analytics_publication_id,
                    snapshot_coverage = source.snapshot_coverage
                FROM boundary_publication_generations AS source
                WHERE target.id = %s AND source.id = %s
                """,
                (new_id, generation_id),
            )
        connection.execute(
            """
            INSERT INTO boundary_publication_corrections
                (boundary_at, source_generation_id, generation_id,
                 affected_artifacts, state, started_at)
            VALUES (%s, %s, %s, %s, 'active', clock_timestamp())
            """,
            (boundary_at, generation_id, new_id, affected),
        )
        return
    connection.execute(
        """
        INSERT INTO boundary_publication_corrections
            (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
        VALUES (%s, %s, %s, %s)
        """,
        (boundary_at, generation_id, affected, Jsonb([{"kind": "decode"}])),
    )


def _enqueue_army_analytics(
    database, connection: Any, *, ranked_day_start: datetime
) -> None:
    ranked_day_start = ranked_day_start.astimezone(UTC)
    coordinator = None
    boundary_at = ranked_day_start + timedelta(days=1)
    if getattr(database, "_supports_coordinator_contract", False):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"boundary-publication:{boundary_at.isoformat()}",),
        )
        coordinator = connection.execute(
            """
            SELECT id, generation, snapshot_state, army_state,
                   army_manifest_id
            FROM boundary_publication_generations
            WHERE boundary_at = %s
              AND snapshot_state <> 'superseded'
              AND army_state <> 'superseded'
            ORDER BY generation DESC
            LIMIT 1
            FOR UPDATE
            """,
            (boundary_at,),
        ).fetchone()
    if coordinator is not None:
        generation_id = int(coordinator[0])
        members = connection.execute(
            """
            SELECT player_id, ranked_day_version_id, snapshot_status
            FROM boundary_publication_generation_members
            WHERE generation_id = %s AND ranked_day_version_id IS NOT NULL
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchall()
        from .season_retirement import (
            SEASON_DETAIL_RETIRED,
            acquire_season_lock_shared,
            is_season_detail_retired,
        )
        seasons = [
            _text_value(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT ranked.official_season_id
                FROM boundary_publication_generation_members AS member
                JOIN ranked_day_versions AS ranked
                  ON ranked.id = member.ranked_day_version_id
                WHERE member.generation_id = %s
                """,
                (generation_id,),
            ).fetchall()
        ]
        for season_id in sorted(set(seasons)):
            acquire_season_lock_shared(connection, season_id)
            if is_season_detail_retired(connection, season_id):
                raise DomainRuleError(
                    SEASON_DETAIL_RETIRED,
                    f"season {season_id} detail is retired",
                )
        for player_id, version_id, snapshot_status in members:
            army_status = boundary._boundary_army_status(database, 
                connection,
                player_id=int(player_id),
                ranked_day_version_id=int(version_id),
                snapshot_status=_text_value(snapshot_status),
            )
            connection.execute(
                """
                UPDATE boundary_publication_generation_members
                SET army_status = %s, updated_at = clock_timestamp()
                WHERE generation_id = %s AND player_id = %s
                """,
                (army_status, generation_id, int(player_id)),
            )
        if (
            not getattr(database, "_disable_decode_corrections", False)
            and _text_value(coordinator[3]) in {"ready", "building", "published"}
            and coordinator[4] is not None
            and _boundary_army_manifest_needs_correction(database, 
                connection, manifest_id=int(coordinator[4])
            )
        ):
            _queue_boundary_army_correction(database, 
                connection,
                boundary_at=boundary_at,
                generation_id=generation_id,
                defer_inheritance=True,
            )
        boundary._try_enqueue_boundary_artifacts(database, 
            connection,
            boundary_at=boundary_at,
            generation_id=generation_id,
        )
        return
    # Decode-driven historical/test work without a reset coordinator
    # retains its existing per-day shape. Coordinated boundaries return
    # above and can only enqueue manifest-identified army work.
    completed = connection.execute(
        """
        SELECT id, official_season_id
        FROM ranked_day_versions
        WHERE ranked_day_start = %s
          AND state = 'Complete'
          AND coverage_complete
        ORDER BY id DESC
        LIMIT 1
        """,
        (ranked_day_start,),
    ).fetchone()
    if completed is None:
        return
    ranked_day_version_id = int(completed[0])
    season_id = _text_value(completed[1])
    from .season_retirement import (
        acquire_season_lock_shared,
        is_season_detail_retired,
    )

    acquire_season_lock_shared(connection, season_id)
    if is_season_detail_retired(connection, season_id):
        return
    latest_decode = connection.execute(
        """
        SELECT COALESCE(max(decode.id), 0)
        FROM legend_battles AS battle
        LEFT JOIN battle_army_decodes AS decode
          ON decode.battle_id = battle.id
         AND decode.is_active
         AND decode.decoder_version = %s
         AND decode.catalog_version = %s
        WHERE battle.ranked_day_start = %s
        """,
        (DECODER_VERSION, CATALOG_VERSION, ranked_day_start),
    ).fetchone()
    decode_generation = int(latest_decode[0]) if latest_decode else 0
    day_text = ranked_day_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    generation = f"{ranked_day_version_id}:{decode_generation}"
    connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            work_type, deduplication_key, input_json,
            processing_version, domain_rule_version,
            analytics_rule_version, due_at
        ) VALUES (
            'build_army_analytics', %s, %s, %s, %s, %s,
            clock_timestamp()
        )
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (
            f"build_army_analytics:{day_text}:{generation}:{ARMY_ANALYTICS_RULE_VERSION}:{DECODER_VERSION}:{CATALOG_VERSION}",
            Jsonb(
                {
                    "ranked_day_start": day_text,
                    "official_season_id": season_id,
                }
            ),
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ARMY_ANALYTICS_RULE_VERSION,
        ),
    )


