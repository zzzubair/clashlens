from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .analytics import FRESHNESS_RULE_VERSION, SNAPSHOT_ORDERING_RULE_VERSION
from .army_decoder import DECODER_VERSION
from .catalog import CATALOG_VERSION
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
    _text_value,
)


def _create_boundary_generation(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    sweep_id: int,
    player_ids: list[int],
    generation: int,
    supersedes_id: int | None,
    pending_inputs: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    population_hash = _boundary_population_hash(player_ids)
    target_at = boundary_at + timedelta(
        minutes=10 if boundary_at.weekday() == 0 else 5
    )
    row = connection.execute(
        """
        INSERT INTO boundary_publication_generations (
            boundary_at, generation, sweep_id, ordering_rule_version,
            freshness_rule_version, expected_population_count,
            expected_population_hash, membership_rule_version,
            snapshot_rule_version, army_rule_version, target_rule, target_at,
            supersedes_id, source_generation_id,
            correction_state, affected_artifacts
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, generation
        """,
        (
            boundary_at,
            generation,
            sweep_id,
            SNAPSHOT_ORDERING_RULE_VERSION,
            FRESHNESS_RULE_VERSION,
            len(player_ids),
            population_hash,
            "active-members-v1",
            ANALYTICS_RULE_VERSION,
            ARMY_ANALYTICS_RULE_VERSION,
            "boundary-delay-v1",
            target_at,
            supersedes_id,
            supersedes_id,
            "active" if supersedes_id is not None else "none",
            ["snapshot", "army"] if supersedes_id is not None else [],
        ),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT id, generation, expected_population_count,
                   expected_population_hash, sweep_id
            FROM boundary_publication_generations
            WHERE boundary_at = %s AND generation = %s
            FOR UPDATE
            """,
            (boundary_at, generation),
        ).fetchone()
    assert row is not None
    generation_id = int(row[0])
    if len(row) > 2 and (
        int(row[2]) != len(player_ids)
        or _text_value(row[3]) != population_hash
        or int(row[4]) != sweep_id
    ):
        raise ValueError(
            "boundary generation inputs conflict with captured membership"
        )
    if supersedes_id is None:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members
                (generation_id, player_id)
            SELECT %s, unnest(%s::bigint[])
            ON CONFLICT DO NOTHING
            """,
            (generation_id, player_ids),
        )
    else:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            )
            SELECT %s, player_id, ranked_day_version_id,
                   ranked_day_input_hash, status, snapshot_status, army_status
            FROM boundary_publication_generation_members
            WHERE generation_id = %s
            ON CONFLICT DO NOTHING
            """,
            (generation_id, supersedes_id),
        )
    for pending in pending_inputs or []:
        pending_version = pending.get("ranked_day_version_id")
        pending_snapshot_status = _boundary_snapshot_status(
            connection,
            player_id=int(pending["player_id"]),
            ranked_day_version_id=int(pending_version),
            boundary_at=boundary_at,
        )
        pending_army_status = _boundary_army_status(database, 
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
                generation_id,
                pending.get("player_id"),
            ),
        )
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET membership_captured_at = COALESCE(membership_captured_at, clock_timestamp())
        WHERE id = %s
        """,
        (generation_id,),
    )
    return generation_id, int(row[1])


def _freeze_boundary_manifest(
    database: Database,
    connection: Any,
    *,
    generation_id: int,
    artifact_kind: str,
) -> tuple[int, str] | None:
    """Freeze one sorted coordinator input before creating its job.

    The row and digest are persisted together. Once inserted, the manifest
    trigger makes both the population and its selected inputs immutable;
    retrying callers therefore reuse the same identity instead of
    reconstructing inputs from live eligibility tables.
    """
    existing = connection.execute(
        """
        SELECT id, digest
        FROM boundary_publication_manifests
        WHERE generation_id = %s AND artifact_kind = %s
        FOR UPDATE
        """,
        (generation_id, artifact_kind),
    ).fetchone()
    if existing is not None:
        return int(existing[0]), _text_value(existing[1])
    generation = connection.execute(
        """
        SELECT boundary_at, generation, ordering_rule_version, freshness_rule_version,
               snapshot_rule_version, army_rule_version
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if generation is None:
        return None
    official_version_id = None
    if artifact_kind == "snapshot":
        official_version = connection.execute(
            """
            SELECT v.id
            FROM official_top200_versions AS v
            JOIN official_top200_attempts AS a ON a.id = v.attempt_id
            JOIN official_top200_version_entries AS e ON e.version_id = v.id
            WHERE a.outcome = 'official_observed' AND v.observed_at <= %s
            GROUP BY v.id, v.observed_at
            HAVING count(*) = 200 AND count(DISTINCT e.rank) = 200
               AND min(e.rank) = 1 AND max(e.rank) = 200
            ORDER BY v.observed_at DESC, v.id DESC
            LIMIT 1
            """,
            (generation[0],),
        ).fetchone()
        official_version_id = int(official_version[0]) if official_version else None
    members = connection.execute(
        """
        SELECT player_id, ranked_day_version_id, ranked_day_input_hash,
               snapshot_status, army_status
        FROM boundary_publication_generation_members
        WHERE generation_id = %s
        ORDER BY player_id, ranked_day_version_id NULLS FIRST, ranked_day_input_hash NULLS FIRST
        """,
        (generation_id,),
    ).fetchall()
    manifest_rows: list[dict[str, Any]] = []
    for ordinal, row in enumerate(members, start=1):
        player_id = int(row[0])
        version_id = int(row[1]) if row[1] is not None else None
        input_hash = _text_value(row[2]) if row[2] is not None else None
        status = _text_value(row[3] if artifact_kind == "snapshot" else row[4])
        classification = {
            "complete": "Complete",
            "partial": "Partial",
            "failed": "Failed",
            "missing": "Missing",
            "unavailable": "Unavailable",
            "inconsistent": "Inconsistent",
            "malformed": "Malformed",
        }.get(status, "Pending")
        if (
            status in {"complete", "partial", "inconsistent", "malformed"}
            and version_id is not None
        ):
            if artifact_kind == "snapshot":
                classification = {
                    "complete": "Complete",
                    "partial": "Partial",
                    "missing": "Missing",
                    "unavailable": "Unavailable",
                    "inconsistent": "Inconsistent",
                    "malformed": "Malformed",
                }.get(
                    _boundary_snapshot_status(
                        connection,
                        player_id=player_id,
                        ranked_day_version_id=version_id,
                        boundary_at=generation[0],
                    ),
                    "Missing",
                )
            elif status == "partial":
                classification = "Partial"
            else:
                state_row = connection.execute(
                    "SELECT state FROM ranked_day_versions WHERE id = %s",
                    (version_id,),
                ).fetchone()
                state = (
                    _text_value(state_row[0])
                    if state_row is not None
                    else "Malformed"
                )
                classification = (
                    state
                    if state in {"Complete", "Partial", "Malformed", "Inconsistent"}
                    else "Partial"
                )
        identity = {
            "artifact_kind": artifact_kind,
            "generation": int(generation[1]),
            "player_id": player_id,
            "ranked_day_version_id": version_id,
            "input_hash": input_hash,
            "classification": classification,
        }
        if artifact_kind == "snapshot":
            identity["official_top200_version_id"] = official_version_id
            official_entry = None
            if official_version_id is not None:
                official_entry = connection.execute(
                    """
                    SELECT entry.rank, version.observed_at
                    FROM official_top200_version_entries AS entry
                    JOIN official_top200_versions AS version
                      ON version.id = entry.version_id
                    WHERE entry.version_id = %s AND entry.player_id = %s
                    """,
                    (official_version_id, player_id),
                ).fetchone()
            identity["official_rank"] = (
                int(official_entry[0]) if official_entry else None
            )
            identity["official_rank_observed_at"] = (
                official_entry[1].astimezone(UTC).isoformat()
                if official_entry
                else None
            )
            profile = connection.execute(
                """
                SELECT profile.id,
                       COALESCE(effect.observation_id, profile.observation_id),
                       COALESCE(effect.observed_at, profile.observed_at),
                       profile.profile_json, profile.normalized_tag,
                       profile.name, profile.trophies, profile.eligibility_state
                FROM player_profile_versions AS profile
                LEFT JOIN player_profile_effects AS effect
                  ON effect.profile_version_id = profile.id
                WHERE profile.player_id = %s
                  AND COALESCE(effect.observed_at, profile.observed_at) <= %s
                  AND profile.source_contract_state = 'accepted'
                ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                         COALESCE(effect.id, profile.id) DESC
                LIMIT 1
                """,
                (player_id, generation[0]),
            ).fetchone()
            if profile is not None:
                identity["profile_version_id"] = int(profile[0])
                identity["profile_input_hash"] = hashlib.sha256(
                    json.dumps(
                        profile[3], sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                identity["profile_snapshot"] = {
                    "observation_id": int(profile[1]),
                    "tag": _text_value(profile[4]),
                    "name": profile[5],
                    "trophies": int(profile[6]),
                    "observed_at": profile[2].astimezone(UTC).isoformat(),
                    "eligibility_state": _text_value(profile[7]),
                    "profile_json": profile[3],
                }
                identity["snapshot_quality"] = (
                    "eligible"
                    if _text_value(profile[7]) == "eligible"
                    else "invalid"
                )
            else:
                identity["profile_version_id"] = None
                identity["profile_input_hash"] = None
                latest = connection.execute(
                    """
                    SELECT (
                        SELECT profile.source_contract_state
                        FROM player_profile_versions AS profile
                        LEFT JOIN player_profile_effects AS effect
                          ON effect.profile_version_id = profile.id
                        WHERE profile.player_id = %s
                          AND COALESCE(effect.observed_at, profile.observed_at) <= %s
                        ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                                 COALESCE(effect.id, profile.id) DESC
                        LIMIT 1
                    ), (
                        SELECT job.failure_category
                        FROM collector_observations AS observation
                        JOIN python_processing_jobs_worker AS job
                          ON job.observation_id = observation.id
                        WHERE observation.player_id = %s
                          AND observation.endpoint = 'profile'
                          AND observation.response_completed_at <= %s
                        ORDER BY observation.response_completed_at DESC,
                                 observation.id DESC
                        LIMIT 1
                    )
                    """,
                    (player_id, generation[0], player_id, generation[0]),
                ).fetchone()
                source_state = _text_value(latest[0]) if latest and latest[0] else None
                failure = _text_value(latest[1]) if latest and latest[1] else None
                if failure in {
                    "malformed_json",
                    "unsupported_profile_schema",
                    "source_identity_mismatch",
                    "invalid_player_tag",
                }:
                    snapshot_quality = "malformed"
                elif source_state == "conflict":
                    snapshot_quality = "conflicting"
                else:
                    snapshot_quality = {
                        "Unavailable": "unavailable",
                        "Failed": "unavailable",
                        "Partial": "partial",
                        "Inconsistent": "inconsistent",
                        "Malformed": "malformed",
                    }.get(classification, "missing")
                identity["snapshot_quality"] = snapshot_quality
        if artifact_kind == "army" and version_id is not None:
            ranked_identity = connection.execute(
                """
                SELECT input_evidence, coverage_evidence, start_baseline_id,
                       end_baseline_id
                FROM ranked_day_versions WHERE id = %s
                """,
                (version_id,),
            ).fetchone()
            if ranked_identity is not None:
                identity.update(
                    {
                        "input_evidence": ranked_identity[0],
                        "coverage_evidence": ranked_identity[1],
                        "start_baseline_id": ranked_identity[2],
                        "end_baseline_id": ranked_identity[3],
                    }
                )
            daily_log = connection.execute(
                "SELECT id, battles FROM api_player_daily_logs WHERE ranked_day_version_id = %s ORDER BY id DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            identity["daily_log_id"] = int(daily_log[0]) if daily_log else None
            battle_ids: list[int] = []
            decode_ids: list[int] = []
            if daily_log is not None and isinstance(daily_log[1], list):
                battle_ids = [
                    int(event["battle_id"])
                    for event in daily_log[1]
                    if isinstance(event, dict)
                    and str(event.get("battle_id", "")).isdigit()
                ]
                if battle_ids:
                    decode_ids = [
                        int(row[0])
                        for row in connection.execute(
                            """
                            SELECT id FROM battle_army_decodes
                            WHERE battle_id = ANY(%s::bigint[]) AND is_active
                              AND decoder_version = %s AND catalog_version = %s
                            ORDER BY id
                            """,
                            (battle_ids, DECODER_VERSION, CATALOG_VERSION),
                        ).fetchall()
                    ]
            evidence_ids: list[int] = []
            if daily_log is not None and isinstance(daily_log[1], list):
                for event in daily_log[1]:
                    if (
                        not isinstance(event, dict)
                        or not str(event.get("battle_id", "")).isdigit()
                    ):
                        continue
                    perspective = (
                        "attacker" if event.get("lens") == "offense" else "defender"
                    )
                    evidence_row = connection.execute(
                        """
                        SELECT perspective.evidence_id
                        FROM battle_perspectives AS perspective
                        WHERE perspective.battle_id = %s
                          AND perspective.perspective = %s
                        """,
                        (int(event["battle_id"]), perspective),
                    ).fetchone()
                    if evidence_row is not None:
                        evidence_ids.append(int(evidence_row[0]))
            identity["battle_ids"] = sorted(set(battle_ids))
            identity["decode_ids"] = decode_ids
            identity["evidence_ids"] = sorted(set(evidence_ids))
        manifest_rows.append(identity)
    season_inputs = None
    if artifact_kind == "army":
        season_row = connection.execute(
            """
            SELECT official_season_id
            FROM ranked_day_versions
            WHERE id = ANY(%s::bigint[])
            ORDER BY id
            LIMIT 1
            """,
            ([row[1] for row in members if row[1] is not None],),
        ).fetchone()
        if season_row is not None:
            season_versions = connection.execute(
                """
                SELECT id
                FROM ranked_day_versions
                WHERE official_season_id = %s
                  AND state = 'Complete' AND coverage_complete
                ORDER BY id
                """,
                (season_row[0],),
            ).fetchall()
            season_version_ids = [int(row[0]) for row in season_versions]
            season_logs = connection.execute(
                """
                SELECT DISTINCT ON (ranked_day_version_id) id, battles
                FROM api_player_daily_logs
                WHERE ranked_day_version_id = ANY(%s::bigint[])
                  AND state = 'Complete' AND coverage = 'complete'
                ORDER BY ranked_day_version_id, version DESC, id DESC
                """,
                (season_version_ids,),
            ).fetchall()
            season_daily_log_ids = [int(row[0]) for row in season_logs]
            season_battle_ids = sorted(
                {
                    int(event["battle_id"])
                    for row in season_logs
                    for event in (row[1] if isinstance(row[1], list) else [])
                    if isinstance(event, dict)
                    and str(event.get("battle_id", "")).isdigit()
                }
            )
            season_decode_ids = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT id
                    FROM battle_army_decodes
                    WHERE battle_id = ANY(%s::bigint[])
                      AND decoder_version = %s AND catalog_version = %s
                      AND is_active
                    ORDER BY id
                    """,
                    (season_battle_ids, DECODER_VERSION, CATALOG_VERSION),
                ).fetchall()
            ]
            season_evidence_ids = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT evidence_id
                    FROM battle_perspectives
                    WHERE battle_id = ANY(%s::bigint[])
                    ORDER BY evidence_id
                    """,
                    (season_battle_ids,),
                ).fetchall()
            ]
            season_inputs = {
                "ranked_version_ids": season_version_ids,
                "daily_log_ids": season_daily_log_ids,
                "battle_ids": season_battle_ids,
                "decode_ids": season_decode_ids,
                "evidence_ids": season_evidence_ids,
            }
    rule_versions = {
        "ordering_rule_version": _text_value(generation[2]),
        "freshness_rule_version": _text_value(generation[3]),
        "analytics_rule_version": (
            _text_value(generation[5])
            if artifact_kind == "army"
            else _text_value(generation[4])
        ),
        **({"season_inputs": season_inputs} if season_inputs is not None else {}),
    }
    digest = hashlib.sha256(
        json.dumps(
            {
                "generation": int(generation[1]),
                "artifact_kind": artifact_kind,
                "rule_versions": rule_versions,
                "rows": manifest_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = connection.execute(
        """
        INSERT INTO boundary_publication_manifests
            (generation_id, artifact_kind, rule_versions, digest)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (generation_id, artifact_kind, Jsonb(rule_versions), digest),
    ).fetchone()
    assert manifest is not None
    manifest_id = int(manifest[0])
    for ordinal, identity in enumerate(manifest_rows, start=1):
        connection.execute(
            """
            INSERT INTO boundary_publication_manifest_rows
                (manifest_id, ordinal, player_id, ranked_day_version_id,
                 input_hash, classification, unavailable_reason, input_identity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                manifest_id,
                ordinal,
                identity["player_id"],
                identity["ranked_day_version_id"],
                identity["input_hash"],
                identity["classification"],
                "reset_baseline_failed"
                if identity["classification"] == "Unavailable"
                else None,
                Jsonb(identity),
            ),
        )
    connection.execute(
        """
        UPDATE boundary_publication_manifests
        SET rows_sealed = true, frozen_at = clock_timestamp(),
            enqueued_at = clock_timestamp()
        WHERE id = %s AND NOT rows_sealed
        """,
        (manifest_id,),
    )
    column = (
        "snapshot_manifest_id"
        if artifact_kind == "snapshot"
        else "army_manifest_id"
    )
    connection.execute(
        f"UPDATE boundary_publication_generations SET {column} = %s, updated_at = clock_timestamp() WHERE id = %s",
        (manifest_id, generation_id),
    )
    return manifest_id, digest


def _inherit_deferred_army_successor_snapshot(
    database, connection: Any, *, generation_id: int
) -> bool:
    target = connection.execute(
        """
        SELECT source_generation_id, snapshot_state, snapshot_manifest_id,
               affected_artifacts
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if target is None or _text_value(target[1]) != "pending":
        return False
    if [_text_value(value) for value in (target[3] or [])] != ["army"]:
        return False
    if target[0] is None or target[2] is not None:
        return False
    if (
        connection.execute(
            """
        SELECT 1
        FROM boundary_publication_corrections
        WHERE source_generation_id = %s
          AND state IN ('queued', 'pending_inputs', 'active')
        LIMIT 1
        """,
            (generation_id,),
        ).fetchone()
        is not None
    ):
        return False
    source = connection.execute(
        """
        SELECT snapshot_state, snapshot_id, snapshot_input_hash,
               snapshot_manifest_id, snapshot_analytics_publication_id,
               snapshot_coverage
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (target[0],),
    ).fetchone()
    if (
        source is None
        or _text_value(source[0]) not in {"published", "superseded"}
        or any(value is None for value in source[1:5])
    ):
        return False
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET snapshot_state = %s,
            snapshot_id = %s,
            snapshot_input_hash = %s,
            snapshot_manifest_id = %s,
            snapshot_analytics_publication_id = %s,
            snapshot_coverage = %s,
            updated_at = clock_timestamp()
        WHERE id = %s
          AND snapshot_state = 'pending'
          AND snapshot_manifest_id IS NULL
          AND affected_artifacts = ARRAY['army']::text[]
        """,
        ("published", *source[1:5], Jsonb(source[5]), generation_id),
    )
    return True


def _try_enqueue_boundary_artifacts(
    database, connection: Any, *, boundary_at: datetime, generation_id: int
) -> None:
    _inherit_deferred_army_successor_snapshot(database, 
        connection, generation_id=generation_id
    )
    generation = connection.execute(
        """
        SELECT id, generation, sweep_id, snapshot_state, army_state,
               expected_population_count, affected_artifacts, target_at, target_rule
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if generation is None:
        return
    generation_number = int(generation[1])
    sweep_id = int(generation[2]) if generation[2] is not None else None
    if sweep_id is None:
        return

    classifications = connection.execute(
        """
        SELECT count(*) AS member_count,
               count(*) FILTER (WHERE snapshot_status = 'pending') AS snapshot_pending,
               count(*) FILTER (WHERE army_status = 'pending') AS army_pending,
               count(*) FILTER (WHERE snapshot_status NOT IN
                   ('complete','partial','failed','missing','unavailable','inconsistent','malformed','pending')) AS bad_snapshot,
               count(*) FILTER (WHERE army_status NOT IN
                   ('complete','partial','failed','missing','unavailable','inconsistent','malformed','pending')) AS bad_army
        FROM boundary_publication_generation_members
        WHERE generation_id = %s
        """,
        (generation_id,),
    ).fetchone()
    assert classifications is not None
    member_count, snapshot_pending, army_pending, bad_snapshot, bad_army = (
        int(value) for value in classifications
    )
    if member_count != int(generation[5]) or bad_snapshot or bad_army:
        return
    boundary_text = boundary_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _text_value(generation[8]) != "boundary-delay-v1":
        return
    target = generation[7]
    now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    if now < target:
        return
    affected_artifacts = [_text_value(value) for value in (generation[6] or [])]
    if snapshot_pending == 0 and _text_value(generation[3]) == "pending":
        connection.execute(
            "UPDATE boundary_publication_generations SET snapshot_state = 'ready', updated_at = clock_timestamp() WHERE id = %s AND snapshot_state = 'pending'",
            (generation_id,),
        )
    if army_pending == 0 and _text_value(generation[4]) == "pending":
        connection.execute(
            "UPDATE boundary_publication_generations SET army_state = 'ready', updated_at = clock_timestamp() WHERE id = %s AND army_state = 'pending'",
            (generation_id,),
        )
    snapshot = connection.execute(
        "SELECT snapshot_state, snapshot_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
        (generation_id,),
    ).fetchone()
    if (
        snapshot_pending == 0
        and snapshot is not None
        and _text_value(snapshot[0]) == "ready"
        and (not affected_artifacts or "snapshot" in affected_artifacts)
    ):
        manifest = _freeze_boundary_manifest(database, 
            connection, generation_id=generation_id, artifact_kind="snapshot"
        )
        assert manifest is not None
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (NULL, 'build_snapshot', %s, %s, 'pending', %s, %s, %s, %s, %s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                f"build_snapshot:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}",
                Jsonb(
                    {
                        "boundary_at": boundary_text,
                        "generation": generation_number,
                        "manifest_id": manifest[0],
                        "manifest_digest": manifest[1],
                    }
                ),
                target,
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        )
    army = connection.execute(
        "SELECT army_state, army_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
        (generation_id,),
    ).fetchone()
    if (
        army_pending == 0
        and army is not None
        and _text_value(army[0]) == "ready"
        and (not affected_artifacts or "army" in affected_artifacts)
    ):
        manifest = _freeze_boundary_manifest(database, 
            connection, generation_id=generation_id, artifact_kind="army"
        )
        assert manifest is not None
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (NULL, 'build_army_analytics', %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                f"build_army_analytics:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}",
                Jsonb(
                    {
                        "boundary_at": boundary_text,
                        "generation": generation_number,
                        "manifest_id": manifest[0],
                        "manifest_digest": manifest[1],
                    }
                ),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ARMY_ANALYTICS_RULE_VERSION,
            ),
        )


def _boundary_snapshot_status(
    connection: Any,
    *,
    player_id: int,
    ranked_day_version_id: int,
    boundary_at: datetime | None = None,
) -> str:
    ranked = connection.execute(
        "SELECT state FROM ranked_day_versions WHERE id = %s AND player_id = %s",
        (ranked_day_version_id, player_id),
    ).fetchone()
    status = {
        "Complete": "complete",
        "Partial": "partial",
        "Inconsistent": "inconsistent",
        "Malformed": "malformed",
    }.get(_text_value(ranked[0]) if ranked else "", "pending")
    if status != "complete":
        return status
    profile = connection.execute(
        """
        SELECT 1
        FROM player_profile_versions AS profile
        LEFT JOIN player_profile_effects AS effect
          ON effect.profile_version_id = profile.id
        WHERE profile.player_id = %s
          AND profile.source_contract_state = 'accepted'
          AND (%s::timestamptz IS NULL OR COALESCE(effect.observed_at, profile.observed_at) <= %s)
        ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                 COALESCE(effect.id, profile.id) DESC
        LIMIT 1
        """,
        (player_id, boundary_at, boundary_at),
    ).fetchone()
    return "complete" if profile is not None else "missing"


def _boundary_army_status(
    database: Database,
    connection: Any,
    *,
    player_id: int,
    ranked_day_version_id: int,
    snapshot_status: str,
) -> str:
    if snapshot_status in {"pending", "unavailable", "missing", "failed"}:
        return (
            "unavailable"
            if snapshot_status in {"unavailable", "missing", "failed"}
            else "pending"
        )
    daily_log = connection.execute(
        """
        SELECT battles, state, coverage
        FROM api_player_daily_logs
        WHERE player_id = %s AND ranked_day_version_id = %s
        ORDER BY version DESC LIMIT 1
        """,
        (player_id, ranked_day_version_id),
    ).fetchone()
    # Coordinated publication requires daily-log evidence before army
    # readiness; direct legacy reconciliation retains its old fallback.
    if daily_log is None:
        return (
            "pending"
            if getattr(database, "_supports_coordinator_contract", False)
            else snapshot_status
        )
    daily_state = _text_value(daily_log[1])
    daily_coverage = _text_value(daily_log[2])
    if daily_state == "Partial":
        return "partial"
    if daily_state != "Complete" or daily_coverage != "complete":
        return "pending"
    battle_ids = [
        int(event["battle_id"])
        for event in (daily_log[0] if isinstance(daily_log[0], list) else [])
        if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
    ]
    if not battle_ids:
        return "complete"
    decoded = connection.execute(
        """
        SELECT count(DISTINCT battle_id)
        FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[]) AND is_active
          AND decoder_version = %s AND catalog_version = %s
        """,
        (battle_ids, DECODER_VERSION, CATALOG_VERSION),
    ).fetchone()
    return (
        snapshot_status
        if decoded is not None and int(decoded[0]) == len(set(battle_ids))
        else "pending"
    )


def _record_boundary_generation(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    player_id: int,
    ranked_day_version_id: int,
    ranked_day_input_hash: str,
) -> bool:
    """Record one member result and enqueue each ready artifact once.

    The collector's sweep membership is the frozen population authority.
    A generation is reused until either artifact has frozen; after that a
    changed member starts one superseding generation and later corrections
    coalesce into it.
    """
    boundary_at = boundary_at.astimezone(UTC)
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"boundary-publication:{boundary_at.isoformat()}",),
    )
    sweep = connection.execute(
        "SELECT id, member_ids FROM collector_reset_sweeps WHERE boundary_at = %s",
        (boundary_at,),
    ).fetchone()
    if sweep is None:
        return False
    sweep_id = int(sweep[0])
    member_ids = [int(value) for value in (sweep[1] or [])]
    if player_id not in member_ids:
        return True
    current = connection.execute(
        """
        SELECT id, generation, snapshot_state, army_state,
               correction_state, snapshot_manifest_id, army_manifest_id,
               affected_artifacts
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
    if current is None:
        generation_id, generation = _create_boundary_generation(database, 
            connection,
            boundary_at=boundary_at,
            sweep_id=sweep_id,
            player_ids=member_ids,
            generation=1,
            supersedes_id=None,
        )
    else:
        generation_id, generation = int(current[0]), int(current[1])
        prior_member = connection.execute(
            """
            SELECT ranked_day_version_id, ranked_day_input_hash
            FROM boundary_publication_generation_members
            WHERE generation_id = %s AND player_id = %s
            FOR UPDATE
            """,
            (generation_id, player_id),
        ).fetchone()
        # A generation's expected membership is immutable. A player that
        # appears in the sweep after generation capture is not inserted,
        # including after a frozen publication.
        if prior_member is None:
            return True
        changed = (
            prior_member[0] is None
            or int(prior_member[0]) != int(ranked_day_version_id)
            or _text_value(prior_member[1]) != ranked_day_input_hash
        )
        frozen_artifacts = [
            kind
            for kind, manifest_id in (
                ("snapshot", current[5]),
                ("army", current[6]),
            )
            if manifest_id is not None
        ]
        correction_active = _text_value(current[4]) == "active"
        if correction_active and current[7] and not (changed and frozen_artifacts):
            affected = {_text_value(value) for value in current[7]}
            frozen_artifacts = [
                kind for kind in frozen_artifacts if kind in affected
            ]
        elif changed and frozen_artifacts:
            # A ranked-day source feeds both artifacts. Once either
            # manifest has frozen, reprocess both instead of inheriting an
            # army identity built from the previous source version.
            frozen_artifacts = ["snapshot", "army"]
        fully_published = (
            _text_value(current[2]) == "published"
            and _text_value(current[3]) == "published"
        )
        active_target_correction = connection.execute(
            """
            SELECT id
            FROM boundary_publication_corrections
            WHERE generation_id = %s AND state = 'active'
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if (
            changed
            and active_target_correction is not None
            and not frozen_artifacts
        ):
            snapshot_status = _boundary_snapshot_status(
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_day_version_id,
                boundary_at=boundary_at,
            )
            army_status = _boundary_army_status(database, 
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_day_version_id,
                snapshot_status=snapshot_status,
            )
            connection.execute(
                """
                UPDATE boundary_publication_generation_members
                SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                    status = %s, snapshot_status = %s, army_status = %s,
                    updated_at = clock_timestamp()
                WHERE generation_id = %s AND player_id = %s
                """,
                (
                    ranked_day_version_id,
                    ranked_day_input_hash,
                    "terminal" if snapshot_status != "pending" else "pending",
                    snapshot_status,
                    army_status,
                    generation_id,
                    player_id,
                ),
            )
            connection.execute(
                """
                UPDATE boundary_publication_generations
                SET affected_artifacts = ARRAY(
                        SELECT DISTINCT unnest(affected_artifacts || ARRAY['snapshot']::text[])
                    ),
                    updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (generation_id,),
            )
            connection.execute(
                """
                UPDATE boundary_publication_corrections
                SET affected_artifacts = ARRAY(
                        SELECT DISTINCT unnest(affected_artifacts || ARRAY['snapshot']::text[])
                    )
                WHERE id = %s
                """,
                (active_target_correction[0],),
            )
            _try_enqueue_boundary_artifacts(database, 
                connection, boundary_at=boundary_at, generation_id=generation_id
            )
            return True
        if (
            changed
            and frozen_artifacts
            and (correction_active or not fully_published)
        ):
            # Keep the changed input only in the correction queue. The
            # captured generation member remains immutable; the queued
            # successor applies this input before it seals inheritance.
            pending_input = {
                "player_id": player_id,
                "ranked_day_version_id": ranked_day_version_id,
                "input_hash": ranked_day_input_hash,
            }
            queued = connection.execute(
                """
                SELECT id
                FROM boundary_publication_corrections
                WHERE boundary_at = %s AND source_generation_id = %s
                  AND state IN ('queued', 'pending_inputs')
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (boundary_at, generation_id),
            ).fetchone()
            correction_artifacts = frozen_artifacts
            if queued is None:
                connection.execute(
                    """
                    INSERT INTO boundary_publication_corrections
                        (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        boundary_at,
                        generation_id,
                        correction_artifacts,
                        Jsonb([pending_input]),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE boundary_publication_corrections
                    SET affected_artifacts = ARRAY(
                            SELECT DISTINCT unnest(affected_artifacts || %s::text[])
                        ),
                        pending_inputs = pending_inputs || %s::jsonb
                    WHERE id = %s
                    """,
                    (correction_artifacts, Jsonb([pending_input]), queued[0]),
                )
            return True
        if (
            changed
            and not correction_active
            and frozen_artifacts
            and fully_published
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
            frozen_members = connection.execute(
                """
                SELECT player_id
                FROM boundary_publication_generation_members
                WHERE generation_id = %s
                ORDER BY player_id
                """,
                (generation_id,),
            ).fetchall()
            generation_id, generation = _create_boundary_generation(database, 
                connection,
                boundary_at=boundary_at,
                sweep_id=sweep_id,
                player_ids=[int(row[0]) for row in frozen_members],
                generation=generation + 1,
                supersedes_id=int(current[0]),
                pending_inputs=[
                    {
                        "player_id": player_id,
                        "ranked_day_version_id": ranked_day_version_id,
                        "input_hash": ranked_day_input_hash,
                    }
                ],
            )
            affected = frozen_artifacts or ["snapshot", "army"]
            connection.execute(
                "UPDATE boundary_publication_generations SET affected_artifacts = %s WHERE id = %s",
                (affected, generation_id),
            )
            connection.execute(
                """
                INSERT INTO boundary_publication_corrections
                    (boundary_at, source_generation_id, generation_id,
                     affected_artifacts, state, started_at)
                VALUES (%s, %s, %s, %s, 'active', clock_timestamp())
                """,
                (boundary_at, current[0], generation_id, affected),
            )
    snapshot_status = _boundary_snapshot_status(
        connection,
        player_id=player_id,
        ranked_day_version_id=ranked_day_version_id,
        boundary_at=boundary_at,
    )
    army_status = _boundary_army_status(database, 
        connection,
        player_id=player_id,
        ranked_day_version_id=ranked_day_version_id,
        snapshot_status=snapshot_status,
    )
    member_status = "terminal" if snapshot_status != "pending" else "pending"
    updated_member = connection.execute(
        """
        UPDATE boundary_publication_generation_members
        SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
            status = %s, snapshot_status = %s, army_status = %s,
            updated_at = clock_timestamp()
        WHERE generation_id = %s AND player_id = %s
        """,
        (
            ranked_day_version_id,
            ranked_day_input_hash,
            member_status,
            snapshot_status,
            army_status,
            generation_id,
            player_id,
        ),
    )
    if updated_member.rowcount == 0:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                generation_id,
                player_id,
                ranked_day_version_id,
                ranked_day_input_hash,
                member_status,
                snapshot_status,
                army_status,
            ),
        )
    _try_enqueue_boundary_artifacts(database, 
        connection, boundary_at=boundary_at, generation_id=generation_id
    )
    return True


def _boundary_population_hash(player_ids: list[int]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(player_ids), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _create_boundary_artifact_identity(
    connection: Any,
    *,
    generation_id: int,
    artifact_kind: str,
    manifest_id: int,
    input_hash: str,
    source_identity: dict[str, Any],
) -> int:
    row = connection.execute(
        """
        INSERT INTO boundary_publication_artifact_identities
            (generation_id, artifact_kind, manifest_id, input_hash, source_identity)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (generation_id, artifact_kind, manifest_id) DO NOTHING
        RETURNING id
        """,
        (
            generation_id,
            artifact_kind,
            manifest_id,
            input_hash,
            Jsonb(source_identity),
        ),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT id, input_hash, source_identity
            FROM boundary_publication_artifact_identities
            WHERE generation_id = %s AND artifact_kind = %s AND manifest_id = %s
            FOR UPDATE
            """,
            (generation_id, artifact_kind, manifest_id),
        ).fetchone()
        if (
            row is None
            or _text_value(row[1]) != input_hash
            or dict(row[2]) != source_identity
        ):
            raise ValueError("boundary publication artifact identity conflict")
    return int(row[0])


