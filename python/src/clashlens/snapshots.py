from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from .analytics import (
    FRESHNESS_RULE_VERSION,
    PROFILE_FRESHNESS_SECONDS,
    SNAPSHOT_ORDERING_RULE_VERSION,
    deterministic_tag_hash,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    _text_value,
)


def complete_snapshot(database: Database, claim: Claim) -> None:
    generation_input = claim.input_json.get("generation")
    generation_number = (
        int(generation_input) if generation_input is not None else None
    )
    ranked_day_version_id = (
        int(claim.input_json["ranked_day_version_id"])
        if generation_number is None
        else None
    )
    content_dedup = getattr(database, "_supports_content_dedup", False)
    official_entries_relation = (
        "official_top200_version_entries"
        if content_dedup
        else "official_top200_entries"
    )
    profile_effect_join = (
        "LEFT JOIN player_profile_effects AS effect\n"
        "                              ON effect.profile_version_id = v.id"
        if content_dedup
        else ""
    )
    profile_observation = "effect.observation_id" if content_dedup else "v.observation_id"
    profile_observed = "effect.observed_at" if content_dedup else "v.observed_at"
    profile_effect_id = "effect.id" if content_dedup else "v.id"
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            boundary_text = claim.input_json.get("boundary_at")
            if boundary_text is None:
                raise ValueError("snapshot boundary is required")
            boundary_at = datetime.fromisoformat(str(boundary_text)).astimezone(UTC)
            generation_row = None
            manifest_digest_value = None
            if generation_number is not None:
                generation_row = connection.execute(
                    """
                    SELECT id, snapshot_state, expected_population_count,
                           expected_population_hash, snapshot_manifest_id,
                           target_at, target_rule, snapshot_rule_version
                    FROM boundary_publication_generations
                    WHERE boundary_at = %s AND generation = %s
                    FOR UPDATE
                    """,
                    (boundary_at, generation_number),
                ).fetchone()
                if generation_row is None:
                    raise ValueError(
                        "boundary publication generation does not exist"
                    )
                if _text_value(generation_row[1]) in {"superseded", "published"}:
                    database._finish_claim(
                        connection,
                        claim,
                        job,
                        state="complete",
                        outcome="stale_superseded",
                    )
                    return
                if _text_value(generation_row[1]) != "ready":
                    raise ValueError("boundary snapshot is not ready")
                target_at = generation_row[5]
                if _text_value(generation_row[6]) != "boundary-delay-v1":
                    raise ValueError("unsupported boundary target rule")
                if _text_value(generation_row[7]) != ANALYTICS_RULE_VERSION:
                    raise ValueError("unsupported boundary snapshot rule")
                now_row = connection.execute("SELECT clock_timestamp()").fetchone()
                assert now_row is not None
                if now_row[0] < target_at:
                    raise ValueError(
                        "snapshot publication target dependency is not ready"
                    )
                manifest_id = (
                    int(generation_row[4])
                    if generation_row[4] is not None
                    else None
                )
                if manifest_id is None:
                    raise ValueError("boundary snapshot manifest is not frozen")
                if int(claim.input_json.get("manifest_id", 0)) != manifest_id:
                    raise ValueError(
                        "boundary snapshot manifest identity does not match"
                    )
                manifest_digest = connection.execute(
                    "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                    (manifest_id,),
                ).fetchone()
                if manifest_digest is None or _text_value(
                    manifest_digest[0]
                ) != claim.input_json.get("manifest_digest"):
                    raise ValueError(
                        "boundary snapshot manifest digest does not match"
                    )
                manifest_digest_value = _text_value(manifest_digest[0])
                official_identity = connection.execute(
                    """
                    SELECT DISTINCT input_identity->>'official_top200_version_id'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s
                    """,
                    (manifest_id,),
                ).fetchall()
                official_version_input = next(
                    (
                        int(row[0])
                        for row in official_identity
                        if row[0] is not None
                    ),
                    None,
                )
                source_row = connection.execute(
                    """
                    SELECT ranked.id, ranked.ranked_day_start,
                           ranked.ranked_day_end, ranked.input_hash, ranked.version
                    FROM boundary_publication_manifest_rows AS manifest
                    JOIN ranked_day_versions AS ranked
                      ON ranked.id = manifest.ranked_day_version_id
                    WHERE manifest.manifest_id = %s
                    ORDER BY ranked.id DESC
                    LIMIT 1
                    """,
                    (manifest_id,),
                ).fetchone()
                if source_row is None:
                    ranked_day_version_id = None
                    ranked_day = (
                        boundary_at - timedelta(days=1),
                        boundary_at,
                        "",
                        0,
                    )
                else:
                    ranked_day_version_id = int(source_row[0])
                    ranked_day = source_row[1:]
                if ranked_day[1] != boundary_at:
                    raise ValueError("snapshot boundary does not match generation")
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_state = 'building', updated_at = clock_timestamp()
                    WHERE id = %s AND snapshot_state = 'ready'
                    """,
                    (generation_row[0],),
                )
            else:
                official_version_input = None
                assert ranked_day_version_id is not None
                ranked_day = connection.execute(
                    """
                    SELECT ranked_day_start, ranked_day_end, input_hash, version
                    FROM ranked_day_versions WHERE id = %s
                    """,
                    (ranked_day_version_id,),
                ).fetchone()
                if ranked_day is None:
                    raise ValueError(
                        "ranked-day version for snapshot does not exist"
                    )
                if boundary_at != ranked_day[1]:
                    raise ValueError(
                        "snapshot boundary does not match ranked-day boundary"
                    )

            profile_version_ids: dict[int, int] = {}
            if generation_row is None:
                # Direct snapshots use the newest accepted historical
                # profile. Coordinated snapshots never execute this query:
                # their manifest is the complete profile input.
                profile_rows = connection.execute(
                    f"""
                    WITH accepted_profiles AS (
                        SELECT DISTINCT ON (v.player_id)
                               p.id, p.normalized_tag, v.trophies,
                               {profile_observation}, {profile_observed},
                               v.eligibility_state
                        FROM player_profile_versions AS v
                        JOIN players AS p ON p.id = v.player_id
                        {profile_effect_join}
                        WHERE {profile_observed} <= %s
                          AND v.source_contract_state = 'accepted'
                        ORDER BY v.player_id, {profile_observed} DESC,
                                 {profile_effect_id} DESC
                    )
                    SELECT id, normalized_tag, trophies, observation_id,
                           observed_at, eligibility_state
                    FROM accepted_profiles
                    WHERE eligibility_state = 'eligible'
                    ORDER BY id
                    """,
                    (boundary_at,),
                ).fetchall()
            else:
                manifest_profiles = connection.execute(
                    """
                    SELECT player_id, input_identity->'profile_snapshot',
                           input_identity->>'profile_version_id',
                           input_identity->>'snapshot_quality'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s
                    """,
                    (generation_row[4],),
                ).fetchall()
                profile_version_ids = {
                    int(row[0]): int(row[2])
                    for row in manifest_profiles
                    if row[2] is not None
                }
                profile_rows = [
                    (
                        int(row[0]),
                        _text_value(row[1]["tag"]),
                        int(row[1]["trophies"]),
                        int(row[1]["observation_id"]),
                        datetime.fromisoformat(
                            str(row[1]["observed_at"])
                        ).astimezone(UTC),
                        _text_value(row[1]["eligibility_state"]),
                    )
                    for row in manifest_profiles
                    if isinstance(row[1], dict)
                    and _text_value(row[1].get("eligibility_state")) == "eligible"
                    and _text_value(row[3]) == "eligible"
                ]

            official_rows = connection.execute(
                f"""
                WITH complete_versions AS (
                    SELECT v.id, v.observed_at
                    FROM official_top200_versions AS v
                    JOIN official_top200_attempts AS a ON a.id = v.attempt_id
                    JOIN {official_entries_relation} AS e ON e.version_id = v.id
                    WHERE a.outcome = 'official_observed'
                      AND v.observed_at <= %s
                      AND (%s::bigint IS NULL OR v.id = %s)
                    GROUP BY v.id, v.observed_at
                    HAVING count(*) = 200
                       AND count(DISTINCT e.rank) = 200
                       AND min(e.rank) = 1
                       AND max(e.rank) = 200
                ), latest_complete AS (
                    SELECT id
                    FROM complete_versions
                    ORDER BY observed_at DESC, id DESC
                    LIMIT 1
                )
                SELECT e.player_id, e.rank, v.id, v.observed_at
                FROM {official_entries_relation} AS e
                JOIN official_top200_versions AS v ON v.id = e.version_id
                JOIN latest_complete AS latest ON latest.id = v.id
                """,
                (boundary_at, official_version_input, official_version_input),
            ).fetchall()
            official_by_player = {
                int(row[0]): (int(row[1]), int(row[2]), row[3])
                for row in official_rows
            }
            if generation_row is not None:
                manifest_officials = connection.execute(
                    """
                    SELECT player_id, input_identity->>'official_rank',
                           input_identity->>'official_rank_observed_at',
                           input_identity->>'official_top200_version_id'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s
                    """,
                    (generation_row[4],),
                ).fetchall()
                official_by_player = {
                    int(row[0]): (
                        int(row[1]),
                        int(row[3]),
                        datetime.fromisoformat(str(row[2])).astimezone(UTC),
                    )
                    for row in manifest_officials
                    if row[1] is not None
                    and row[2] is not None
                    and row[3] is not None
                }

            entries: list[dict[str, Any]] = []
            for row in profile_rows:
                age_seconds = int((boundary_at - row[4]).total_seconds())
                if age_seconds < 0:
                    raise ValueError("snapshot selected future profile evidence")
                freshness = (
                    "fresh" if age_seconds <= PROFILE_FRESHNESS_SECONDS else "stale"
                )
                entries.append(
                    {
                        "player_id": int(row[0]),
                        "tag": _text_value(row[1]),
                        "trophies": int(row[2]),
                        "observation_id": int(row[3]),
                        "profile_version_id": profile_version_ids.get(int(row[0])),
                        "observed_at": row[4],
                        "age_seconds": age_seconds,
                        "freshness": freshness,
                        "confidence": "confirmed",
                        "tie_hash": deterministic_tag_hash(_text_value(row[1])),
                        "official": official_by_player.get(int(row[0])),
                    }
                )
            entries.sort(
                key=lambda item: (
                    -int(item["trophies"]),
                    str(item["tie_hash"]),
                    str(item["tag"]),
                )
            )

            quality_row = connection.execute(
                f"""
                WITH known_players AS (
                    SELECT id
                    FROM players
                    WHERE active = true
                    UNION
                    SELECT DISTINCT v.player_id
                    FROM player_profile_versions AS v
                    {profile_effect_join}
                    WHERE {profile_observed} <= %s
                    UNION
                    SELECT DISTINCT o.player_id
                    FROM collector_observations AS o
                    WHERE o.endpoint = 'profile'
                      AND o.player_id IS NOT NULL
                      AND o.response_completed_at <= %s
                ), latest_accepted AS (
                    SELECT DISTINCT ON (v.player_id)
                           v.player_id, v.trophies, {profile_observed},
                           v.eligibility_state
                    FROM player_profile_versions AS v
                    {profile_effect_join}
                    WHERE {profile_observed} <= %s
                      AND v.source_contract_state = 'accepted'
                    ORDER BY v.player_id, {profile_observed} DESC,
                             {profile_effect_id} DESC
                ), latest_any AS (
                    SELECT DISTINCT ON (v.player_id)
                           v.player_id, v.eligibility_state,
                           v.source_contract_state
                    FROM player_profile_versions AS v
                    {profile_effect_join}
                    WHERE {profile_observed} <= %s
                    ORDER BY v.player_id, {profile_observed} DESC,
                             {profile_effect_id} DESC
                ), latest_profile_job AS (
                    SELECT DISTINCT ON (o.player_id)
                           o.player_id, j.failure_category, j.outcome
                    FROM collector_observations AS o
                    JOIN python_processing_jobs_worker AS j
                      ON j.observation_id = o.id
                    WHERE o.endpoint = 'profile'
                      AND o.player_id IS NOT NULL
                      AND o.response_completed_at <= %s
                    ORDER BY o.player_id, o.response_completed_at DESC, o.id DESC
                ), classified AS (
                    SELECT k.id,
                           accepted.trophies,
                           accepted.observed_at,
                           accepted.eligibility_state AS accepted_state,
                           any_profile.source_contract_state AS any_source_state,
                           job.failure_category
                    FROM known_players AS k
                    LEFT JOIN latest_accepted AS accepted
                      ON accepted.player_id = k.id
                    LEFT JOIN latest_any AS any_profile
                      ON any_profile.player_id = k.id
                    LEFT JOIN latest_profile_job AS job
                      ON job.player_id = k.id
                )
                SELECT
                    count(*) FILTER (
                        WHERE accepted_state = 'eligible'
                    ),
                    count(*) FILTER (
                        WHERE accepted_state = 'eligible'
                          AND trophies IS NOT NULL
                    ),
                    count(*) FILTER (
                        WHERE accepted_state = 'eligible'
                          AND %s - observed_at > make_interval(secs => %s)
                    ),
                    count(*) FILTER (
                        WHERE accepted_state = 'eligible'
                          AND %s - observed_at <= make_interval(secs => %s)
                    ),
                    count(*) FILTER (
                        WHERE accepted_state IS NULL
                          AND any_source_state IS NULL
                          AND failure_category IS NULL
                    ),
                    count(*) FILTER (
                        WHERE accepted_state = 'uncertain'
                    ),
                    count(*) FILTER (
                        WHERE accepted_state IS NULL
                          AND any_source_state IS NULL
                          AND failure_category IN (
                              'malformed_json',
                              'unsupported_profile_schema',
                              'source_identity_mismatch',
                              'invalid_player_tag'
                          )
                    ),
                    count(*) FILTER (
                        WHERE accepted_state IS NULL
                          AND any_source_state = 'conflict'
                    )
                FROM classified
                WHERE (%s::bigint IS NULL OR id IN (
                    SELECT player_id
                    FROM boundary_publication_generation_members
                    WHERE generation_id = %s
                ))
                """,
                (
                    boundary_at,
                    boundary_at,
                    boundary_at,
                    boundary_at,
                    boundary_at,
                    boundary_at,
                    PROFILE_FRESHNESS_SECONDS,
                    boundary_at,
                    PROFILE_FRESHNESS_SECONDS,
                    generation_row[0] if generation_row is not None else None,
                    generation_row[0] if generation_row is not None else None,
                ),
            ).fetchone()
            assert quality_row is not None
            if generation_row is not None:
                manifest_quality = connection.execute(
                    """
                    SELECT player_id, classification,
                           input_identity->>'snapshot_quality'
                    FROM boundary_publication_manifest_rows
                    WHERE manifest_id = %s
                    """,
                    (generation_row[4],),
                ).fetchall()
                included_players = {int(entry["player_id"]) for entry in entries}
                by_classification: dict[str, int] = {}
                by_quality: dict[str, int] = {}
                excluded_quality: dict[str, int] = {}
                for player_id, classification, snapshot_quality in manifest_quality:
                    name = _text_value(classification)
                    quality_name = _text_value(snapshot_quality)
                    by_classification[name] = by_classification.get(name, 0) + 1
                    by_quality[quality_name] = by_quality.get(quality_name, 0) + 1
                    if int(player_id) not in included_players:
                        excluded_quality[quality_name] = (
                            excluded_quality.get(quality_name, 0) + 1
                        )
                if sum(by_classification.values()) != int(generation_row[2]):
                    raise ValueError(
                        "snapshot manifest coverage does not match expected population"
                    )
                if len(entries) + sum(excluded_quality.values()) != int(
                    generation_row[2]
                ):
                    raise ValueError("snapshot output coverage is not reconciled")
                quality = {
                    "expected_population_count": int(generation_row[2]),
                    "classification_counts": by_classification,
                    "excluded_classification_counts": excluded_quality,
                    "eligible_population_count": by_quality.get("eligible", 0),
                    "included_entry_count": len(entries),
                    "stale_entry_count": sum(
                        entry["freshness"] == "stale" for entry in entries
                    ),
                    "fresh_entry_count": sum(
                        entry["freshness"] == "fresh" for entry in entries
                    ),
                    "excluded_missing_count": excluded_quality.get("missing", 0),
                    "excluded_unavailable_count": excluded_quality.get(
                        "unavailable", 0
                    ),
                    "excluded_invalid_count": excluded_quality.get("invalid", 0),
                    "excluded_malformed_count": excluded_quality.get("malformed", 0),
                    "excluded_conflicting_count": excluded_quality.get(
                        "conflicting", 0
                    ),
                    "excluded_partial_count": excluded_quality.get("partial", 0),
                    "excluded_inconsistent_count": excluded_quality.get(
                        "inconsistent", 0
                    ),
                }
            else:
                quality = {
                    "eligible_population_count": int(quality_row[0]),
                    "included_entry_count": int(quality_row[1]),
                    "stale_entry_count": int(quality_row[2]),
                    "fresh_entry_count": int(quality_row[3]),
                    "excluded_missing_count": int(quality_row[4]),
                    "excluded_unavailable_count": 0,
                    "excluded_invalid_count": int(quality_row[5]),
                    "excluded_malformed_count": int(quality_row[6]),
                    "excluded_conflicting_count": int(quality_row[7]),
                    "excluded_partial_count": 0,
                    "excluded_inconsistent_count": 0,
                }
            if quality["included_entry_count"] != len(entries):
                raise ValueError("snapshot quality count does not match entries")
            coverage = (
                quality["included_entry_count"]
                / quality["eligible_population_count"]
                if quality["eligible_population_count"]
                else 0.0
            )
            hash_entries = [
                {
                    "player_id": entry["player_id"],
                    "tag": entry["tag"],
                    "trophies": entry["trophies"],
                    "profile_observation_id": entry["observation_id"],
                    "profile_observed_at": entry["observed_at"]
                    .astimezone(UTC)
                    .isoformat(),
                    "profile_age_seconds": entry["age_seconds"],
                    "profile_freshness": entry["freshness"],
                    "profile_confidence": entry["confidence"],
                    "tie_hash": entry["tie_hash"],
                    "official_rank": (
                        entry["official"][0]
                        if entry["official"] is not None
                        else None
                    ),
                    "official_rank_version_id": (
                        entry["official"][1]
                        if entry["official"] is not None
                        else None
                    ),
                    "official_rank_observed_at": (
                        entry["official"][2].astimezone(UTC).isoformat()
                        if entry["official"] is not None
                        else None
                    ),
                }
                for entry in entries
            ]
            if generation_row is not None:
                hash_payload = {
                    "manifest_digest": manifest_digest_value,
                    "rule_versions": {
                        "ordering_rule_version": SNAPSHOT_ORDERING_RULE_VERSION,
                        "freshness_rule_version": FRESHNESS_RULE_VERSION,
                        "analytics_rule_version": _text_value(generation_row[7]),
                    },
                    "entries": hash_entries,
                    "quality": quality,
                }
            else:
                hash_payload = {
                    "boundary_at": boundary_at.astimezone(UTC).isoformat(),
                    "source_ranked_day_version_id": ranked_day_version_id,
                    "source_ranked_day_version": int(ranked_day[3]),
                    "source_ranked_day_input_hash": _text_value(ranked_day[2]),
                    "ordering_rule_version": SNAPSHOT_ORDERING_RULE_VERSION,
                    "freshness_rule_version": FRESHNESS_RULE_VERSION,
                    "entries": hash_entries,
                    "quality": quality,
                }
            input_hash = hashlib.sha256(
                json.dumps(
                    hash_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            frozen_snapshot_id, frozen_snapshot_version = (
                _publish_snapshot_kind(database, 
                    connection,
                    snapshot_kind="frozen",
                    boundary_at=boundary_at,
                    ranked_day_version_id=(
                        None
                        if generation_row is not None
                        else ranked_day_version_id
                    ),
                    entries=entries,
                    coverage=coverage,
                    quality=quality,
                    input_hash=input_hash,
                    publish=False,
                )
            )
            _publish_snapshot_kind(database, 
                connection,
                snapshot_kind="live",
                boundary_at=boundary_at,
                ranked_day_version_id=(
                    None if generation_row is not None else ranked_day_version_id
                ),
                entries=entries,
                coverage=coverage,
                quality=quality,
                input_hash=input_hash,
                publish=True,
            )
            if generation_row is not None:
                connection.execute(
                    """
                    UPDATE boundary_publication_generations
                    SET snapshot_id = %s, snapshot_input_hash = %s,
                        snapshot_coverage = %s,
                        updated_at = clock_timestamp()
                    WHERE id = %s AND snapshot_state = 'building'
                    """,
                    (
                        frozen_snapshot_id,
                        input_hash,
                        Jsonb(quality),
                        generation_row[0],
                    ),
                )
            _enqueue_snapshot_analytics(
                connection,
                snapshot_id=frozen_snapshot_id,
                snapshot_version=frozen_snapshot_version,
                snapshot_input_hash=input_hash,
                ranked_day_version_id=(
                    None if generation_row is not None else ranked_day_version_id
                ),
                generation_number=generation_number,
                manifest_id=(
                    int(generation_row[4]) if generation_row is not None else None
                ),
                manifest_digest=(
                    _text_value(
                        connection.execute(
                            "SELECT digest FROM boundary_publication_manifests WHERE id = %s",
                            (generation_row[4],),
                        ).fetchone()[0]
                    )
                    if generation_row is not None
                    else None
                ),
                boundary_at=(boundary_at if generation_row is not None else None),
                period_start=ranked_day[0],
                period_end=ranked_day[1],
            )
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def _publish_snapshot_kind(
    database: Database,
    connection: Any,
    *,
    snapshot_kind: str,
    boundary_at: datetime,
    ranked_day_version_id: int | None,
    entries: list[dict[str, Any]],
    coverage: float,
    quality: dict[str, int],
    input_hash: str,
    publish: bool,
) -> tuple[int, int]:
    """Assemble one immutable snapshot version.

    Frozen snapshots stop at ``building``. The analytics transaction is the
    only writer that changes a frozen snapshot to ``published``. Live
    snapshots keep their independent publication path.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (
            "leaderboard-snapshot-v2:"
            + snapshot_kind
            + ":"
            + boundary_at.isoformat(),
        ),
    )
    existing = connection.execute(
        """
        SELECT id, version, state
        FROM leaderboard_snapshots
        WHERE snapshot_kind = %s
          AND boundary_at = %s
          AND input_hash = %s
        ORDER BY id DESC
        LIMIT 1
        FOR UPDATE
        """,
        (snapshot_kind, boundary_at, input_hash),
    ).fetchone()
    if existing is not None and _text_value(existing[2]) != "building":
        return int(existing[0]), int(existing[1])

    prior = connection.execute(
        """
        SELECT id, version
        FROM leaderboard_snapshots
        WHERE snapshot_kind = %s
          AND boundary_at = %s
          AND state = 'published'
        ORDER BY version DESC, id DESC
        LIMIT 1
        """,
        (snapshot_kind, boundary_at),
    ).fetchone()
    if existing is not None:
        snapshot_id = int(existing[0])
        snapshot_version = int(existing[1])
    else:
        next_version_row = connection.execute(
            """
            SELECT COALESCE(max(version), 0) + 1
            FROM leaderboard_snapshots
            WHERE snapshot_kind = %s AND boundary_at = %s
            """,
            (snapshot_kind, boundary_at),
        ).fetchone()
        assert next_version_row is not None
        snapshot_version = int(next_version_row[0])
        if getattr(database, "_supports_coordinator_contract", False):
            snapshot = connection.execute(
                """
                INSERT INTO leaderboard_snapshots (
                    snapshot_kind, boundary_at, version, correction_of_id,
                    ordering_rule_version, freshness_rule_version, state,
                    source_ranked_day_version_id, measured_coverage,
                    stale_entry_count, input_hash,
                    eligible_population_count, included_entry_count,
                    fresh_entry_count, excluded_missing_count,
                    excluded_unavailable_count, excluded_invalid_count,
                    excluded_malformed_count, excluded_partial_count,
                    excluded_inconsistent_count, excluded_conflicting_count
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'building', %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    snapshot_kind,
                    boundary_at,
                    snapshot_version,
                    prior[0] if prior is not None else None,
                    SNAPSHOT_ORDERING_RULE_VERSION,
                    FRESHNESS_RULE_VERSION,
                    ranked_day_version_id,
                    coverage,
                    quality["stale_entry_count"],
                    input_hash,
                    quality["eligible_population_count"],
                    quality["included_entry_count"],
                    quality["fresh_entry_count"],
                    quality["excluded_missing_count"],
                    quality["excluded_unavailable_count"],
                    quality["excluded_invalid_count"],
                    quality["excluded_malformed_count"],
                    quality["excluded_partial_count"],
                    quality["excluded_inconsistent_count"],
                    quality["excluded_conflicting_count"],
                ),
            ).fetchone()
        else:
            snapshot = connection.execute(
                """
                INSERT INTO leaderboard_snapshots (
                    snapshot_kind, boundary_at, version, correction_of_id,
                    ordering_rule_version, freshness_rule_version, state,
                    source_ranked_day_version_id, measured_coverage,
                    stale_entry_count, input_hash,
                    eligible_population_count, included_entry_count,
                    fresh_entry_count, excluded_missing_count,
                    excluded_invalid_count, excluded_malformed_count,
                    excluded_conflicting_count
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'building', %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    snapshot_kind,
                    boundary_at,
                    snapshot_version,
                    prior[0] if prior is not None else None,
                    SNAPSHOT_ORDERING_RULE_VERSION,
                    FRESHNESS_RULE_VERSION,
                    ranked_day_version_id,
                    coverage,
                    quality["stale_entry_count"],
                    input_hash,
                    quality["eligible_population_count"],
                    quality["included_entry_count"],
                    quality["fresh_entry_count"],
                    quality["excluded_missing_count"],
                    quality["excluded_invalid_count"],
                    quality["excluded_malformed_count"],
                    quality["excluded_conflicting_count"],
                ),
            ).fetchone()
        assert snapshot is not None
        snapshot_id = int(snapshot[0])
    entry_rows = [
        {
            "position": position,
            "player_id": int(entry["player_id"]),
            "profile_version_id": entry.get("profile_version_id"),
            "trophies": int(entry["trophies"]),
            "observation_id": int(entry["observation_id"]),
            "observed_at": entry["observed_at"].astimezone(UTC).isoformat(),
            "age_seconds": int(entry["age_seconds"]),
            "freshness": entry["freshness"],
            "confidence": entry["confidence"],
            "tie_hash": entry["tie_hash"],
            "official_rank": (
                entry["official"][0] if entry["official"] is not None else None
            ),
            "official_rank_version_id": (
                entry["official"][1] if entry["official"] is not None else None
            ),
            "official_rank_observed_at": (
                entry["official"][2].astimezone(UTC).isoformat()
                if entry["official"] is not None
                else None
            ),
        }
        for position, entry in enumerate(entries, start=1)
    ]
    if getattr(database, "_supports_coordinator_contract", False):
        connection.execute(
            """
            INSERT INTO leaderboard_snapshot_entries (
                snapshot_id, position, player_id, profile_version_id, trophies,
                trophy_observation_id, trophy_observed_at,
                observation_age_seconds, freshness, confidence, tie_hash,
                profile_observation_id, profile_observed_at,
                profile_age_seconds, profile_freshness, profile_confidence,
                official_rank, official_rank_version_id,
                official_rank_observed_at
            )
            SELECT %s, row.position, row.player_id, row.profile_version_id,
                   row.trophies, row.observation_id, row.observed_at,
                   row.age_seconds, row.freshness, row.confidence, row.tie_hash,
                   row.observation_id, row.observed_at, row.age_seconds,
                   row.freshness, row.confidence, row.official_rank,
                   row.official_rank_version_id, row.official_rank_observed_at
            FROM jsonb_to_recordset(%s::jsonb) AS row(
                position integer, player_id bigint, profile_version_id bigint,
                trophies integer, observation_id bigint, observed_at timestamptz,
                age_seconds integer, freshness text, confidence text,
                tie_hash text, official_rank integer,
                official_rank_version_id bigint,
                official_rank_observed_at timestamptz
            )
            ON CONFLICT (snapshot_id, position) DO NOTHING
            """,
            (snapshot_id, Jsonb(entry_rows)),
        )
    else:
        connection.execute(
            """
            INSERT INTO leaderboard_snapshot_entries (
                snapshot_id, position, player_id, trophies,
                trophy_observation_id, trophy_observed_at,
                observation_age_seconds, freshness, confidence, tie_hash,
                profile_observation_id, profile_observed_at,
                profile_age_seconds, profile_freshness, profile_confidence,
                official_rank, official_rank_version_id,
                official_rank_observed_at
            )
            SELECT %s, row.position, row.player_id, row.trophies,
                   row.observation_id, row.observed_at, row.age_seconds,
                   row.freshness, row.confidence, row.tie_hash,
                   row.observation_id, row.observed_at, row.age_seconds,
                   row.freshness, row.confidence, row.official_rank,
                   row.official_rank_version_id, row.official_rank_observed_at
            FROM jsonb_to_recordset(%s::jsonb) AS row(
                position integer, player_id bigint, trophies integer,
                observation_id bigint, observed_at timestamptz,
                age_seconds integer, freshness text, confidence text,
                tie_hash text, official_rank integer,
                official_rank_version_id bigint,
                official_rank_observed_at timestamptz
            )
            ON CONFLICT (snapshot_id, position) DO NOTHING
            """,
            (snapshot_id, Jsonb(entry_rows)),
        )
    if publish:
        connection.execute(
            """
            UPDATE leaderboard_snapshots
            SET state = 'published', published_at = clock_timestamp()
            WHERE id = %s AND state = 'building'
            """,
            (snapshot_id,),
        )
        if prior is not None and int(prior[0]) != snapshot_id:
            connection.execute(
                """
                UPDATE leaderboard_snapshots
                SET state = 'superseded'
                WHERE id = %s AND state = 'published'
                """,
                (prior[0],),
            )
    return snapshot_id, snapshot_version


def _enqueue_snapshot_analytics(
    connection: Any,
    *,
    snapshot_id: int,
    snapshot_version: int,
    snapshot_input_hash: str,
    ranked_day_version_id: int | None,
    period_start: datetime,
    period_end: datetime,
    generation_number: int | None = None,
    manifest_id: int | None = None,
    manifest_digest: str | None = None,
    boundary_at: datetime | None = None,
) -> None:
    deduplication_key = (
        f"build_analytics:snapshot:{int(snapshot_id)}:v{int(snapshot_version)}:"
        f"input:{snapshot_input_hash}"
    )
    connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version
        ) VALUES (NULL, 'build_analytics', %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (
            deduplication_key,
            Jsonb(
                {
                    "snapshot_id": int(snapshot_id),
                    "snapshot_version": int(snapshot_version),
                    "snapshot_input_hash": snapshot_input_hash,
                    **(
                        {"source_ranked_day_version_id": int(ranked_day_version_id)}
                        if ranked_day_version_id is not None
                        else {}
                    ),
                    **(
                        {
                            "generation": int(generation_number),
                            "manifest_id": int(manifest_id),
                            "manifest_digest": manifest_digest,
                            "boundary_at": boundary_at.astimezone(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                        }
                        if generation_number is not None
                        and manifest_id is not None
                        and manifest_digest is not None
                        and boundary_at is not None
                        else {}
                    ),
                    "period_start": period_start.astimezone(UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "period_end": period_end.astimezone(UTC).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "population_filter": {"population": "tracked_players"},
                }
            ),
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
        ),
    )


