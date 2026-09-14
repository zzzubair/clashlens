from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from . import job_outcomes, reset_baselines
from .db import Claim, Database, _text_value
from .domain import SEASON_ANCHOR_RULE_VERSION, DomainRuleError, validate_season_anchor
from .profile import PROFILE_PARSER_VERSION, ParsedProfile
from .rankings import ParsedOfficialRankings


def complete_profile(database: Database, claim: Claim, profile: ParsedProfile) -> None:
    compact = getattr(database, "_supports_compact_battles", False)
    projection = _profile_semantic_projection(profile)
    if not getattr(database, "_supports_content_dedup", False):
        return _complete_profile_legacy(database, claim, profile)
    (
        observation_id,
        http_status,
        response_hash,
        _observed_at,
        endpoint,
        schema_version,
    ) = job_outcomes._observation_source(claim)
    with database._timed_connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            parsed_payload_id = job_outcomes._record_parsed_payload(
                connection,
                endpoint=endpoint,
                response_hash=response_hash,
                parser_version=profile.parser_version,
                schema_version=schema_version,
                parse_outcome="valid",
                parsed_json=profile.profile_json,
                representation="player_profile_versions" if compact else None,
            )
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                VALUES (%s, %s, %s)
                ON CONFLICT (normalized_tag) DO NOTHING
                """,
                (
                    profile.normalized_tag,
                    profile.eligibility_state == "eligible",
                    profile.eligibility_state,
                ),
            )
            player = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s",
                (profile.normalized_tag,),
            ).fetchone()
            assert player is not None
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (
                    f"profile-semantic:{player[0]}:{json.dumps(projection, sort_keys=True, separators=(",", ":"))}",
                ),
            )
            profile_version = connection.execute(
                """
                SELECT version.id
                FROM player_profile_versions AS version
                JOIN players AS current_player ON current_player.id = version.player_id
                WHERE version.player_id = %s AND version.semantic_projection = %s
                ORDER BY (version.id = current_player.current_profile_version_id) DESC,
                         version.id DESC
                LIMIT 1
                """,
                (player[0], Jsonb(projection)),
            ).fetchone()
            created_profile = profile_version is None
            if created_profile:
                profile_version = connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag, endpoint_version,
                        schema_version, parser_version, observed_at, source_http_status,
                        name, trophies, league_tier_id, league_tier_name,
                        eligibility_state, current_league_season_id,
                        previous_league_season_id, eligibility_reason,
                        source_contract_state, season_anchor_state, profile_json,
                        parsed_payload_id, semantic_projection
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        player[0], observation_id, profile.normalized_tag,
                        profile.endpoint_version, profile.schema_version,
                        profile.parser_version, profile.observed_at, http_status,
                        profile.name, profile.trophies, profile.league_tier_id,
                        profile.league_tier_name, profile.eligibility_state,
                        profile.current_league_season_id,
                        profile.previous_league_season_id,
                        profile.eligibility_reason, profile.source_contract_state,
                        profile.season_anchor_state,
                        Jsonb({"clan": {"name": projection["clan_name"]}} if compact else profile.profile_json),
                        parsed_payload_id, Jsonb(projection),
                    ),
                ).fetchone()
                assert profile_version is not None
            profile_version_id = int(profile_version[0])
            if created_profile:
                anchor_outcome = _record_season_anchor(
                    connection, profile_version_id, profile
                )
            else:
                retained_anchor = connection.execute(
                    "SELECT outcome FROM season_anchor_evidence WHERE profile_version_id = %s",
                    (profile_version_id,),
                ).fetchone()
                anchor_outcome = (
                    _text_value(retained_anchor[0])
                    if retained_anchor is not None
                    else (
                        "conflict"
                        if profile.source_contract_state == "conflict"
                        else "not_applicable"
                    )
                )
            processing_id = job_outcomes._record_processing_outcome(database, 
                connection,
                claim,
                outcome="processed",
                failure_category=(
                    "season_anchor_conflict"
                    if anchor_outcome == "conflict"
                    else None
                ),
                parsed_payload_id=parsed_payload_id,
            )
            connection.execute(
                """
                INSERT INTO player_profile_effects (
                    profile_version_id, observation_id, effect_kind,
                    parsed_payload_id, attempt_id, processing_outcome_id,
                    observed_at, source_http_status, endpoint_version,
                    schema_version, parser_version
                ) VALUES (%s, %s, 'current_profile', %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (observation_id, parser_version, effect_kind) DO NOTHING
                """,
                (
                    profile_version_id, observation_id, parsed_payload_id,
                    claim.attempt_id, processing_id, profile.observed_at,
                    http_status, profile.endpoint_version, profile.schema_version,
                    profile.parser_version,
                ),
            )
            connection.execute(
                """
                WITH candidate AS (
                    SELECT effect.profile_version_id, effect.observed_at,
                           version.eligibility_state,
                           version.eligibility_state IN ('eligible', 'ineligible')
                           AND NOT EXISTS (
                               SELECT 1
                               FROM player_profile_effects AS newer_effect
                               JOIN player_profile_versions AS newer
                                 ON newer.id = newer_effect.profile_version_id
                               WHERE newer.player_id = version.player_id
                                 AND newer.eligibility_state IN ('eligible', 'ineligible')
                                 AND (newer_effect.observed_at, newer_effect.id)
                                     > (effect.observed_at, effect.id)
                           )
                           AND (
                               p.eligibility_state IS DISTINCT FROM version.eligibility_state
                               OR p.active IS DISTINCT FROM (version.eligibility_state = 'eligible')
                               OR (version.eligibility_state = 'eligible' AND p.next_due_at IS NULL)
                               OR (version.eligibility_state = 'ineligible' AND p.next_due_at IS NOT NULL)
                           ) AS update_eligibility,
                           version.source_contract_state = 'accepted'
                           AND (
                               p.current_observed_at IS NULL
                               OR p.current_observed_at < effect.observed_at
                               OR (p.current_observed_at = effect.observed_at
                                   AND effect.id > COALESCE((
                                       SELECT max(current_effect.id)
                                       FROM player_profile_effects AS current_effect
                                       WHERE current_effect.profile_version_id = p.current_profile_version_id
                                         AND current_effect.effect_kind = 'current_profile'
                                         AND current_effect.observed_at = p.current_observed_at
                                   ), 0))
                           ) AS update_profile
                    FROM player_profile_effects AS effect
                    JOIN player_profile_versions AS version
                      ON version.id = effect.profile_version_id
                    JOIN players AS p ON p.id = version.player_id
                    WHERE p.id = %s AND effect.observation_id = %s
                )
                UPDATE players AS p
                SET active = CASE
                        WHEN candidate.update_eligibility AND candidate.eligibility_state = 'eligible' THEN true
                        WHEN candidate.update_eligibility AND candidate.eligibility_state = 'ineligible' THEN false
                        ELSE p.active END,
                    next_due_at = CASE
                        WHEN candidate.update_eligibility AND candidate.eligibility_state = 'eligible' THEN COALESCE(p.next_due_at, clock_timestamp())
                        WHEN candidate.update_eligibility AND candidate.eligibility_state = 'ineligible' THEN NULL
                        ELSE p.next_due_at END,
                    eligibility_state = CASE WHEN candidate.update_eligibility THEN candidate.eligibility_state ELSE p.eligibility_state END,
                    current_profile_version_id = CASE WHEN candidate.update_profile THEN candidate.profile_version_id ELSE p.current_profile_version_id END,
                    current_observed_at = CASE WHEN candidate.update_profile THEN candidate.observed_at ELSE p.current_observed_at END,
                    updated_at = CASE
                        WHEN candidate.update_eligibility
                          OR (candidate.update_profile
                              AND p.current_profile_version_id IS DISTINCT FROM candidate.profile_version_id)
                        THEN clock_timestamp()
                        ELSE p.updated_at
                    END
                FROM candidate
                WHERE p.id = %s AND (candidate.update_eligibility OR candidate.update_profile)
                """,
                (player[0], observation_id, player[0]),
            )
            # A newly discovered player starts inactive while its first profile
            # is still unknown. Cancel ordinary discovery only after a trusted
            # profile has classified the player as ineligible; explicit Refresh
            # and frozen Reset work must continue, and their evidence remains
            # referenced independently.
            connection.execute(
                "SELECT clashlens_cancel_inactive_discovery_work(%s)",
                (player[0],),
            )
            reset_baselines._refresh_reset_baseline_evidence(database, connection, claim)
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def _complete_profile_legacy(database: Database, claim: Claim, profile: ParsedProfile) -> None:
    (
        observation_id,
        http_status,
        response_hash,
        _observed_at,
        endpoint,
        schema_version,
    ) = job_outcomes._observation_source(claim)
    with database._timed_connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            player = connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                VALUES (%s, %s, %s)
                ON CONFLICT (normalized_tag) DO UPDATE
                    SET updated_at = clock_timestamp()
                RETURNING id
                """,
                (
                    profile.normalized_tag,
                    profile.eligibility_state == "eligible",
                    profile.eligibility_state,
                ),
            ).fetchone()
            assert player is not None
            profile_version = connection.execute(
                """
                INSERT INTO player_profile_versions (
                    player_id, observation_id, normalized_tag, endpoint_version,
                    schema_version, parser_version, observed_at, source_http_status,
                    name, trophies, league_tier_id, league_tier_name,
                    eligibility_state, current_league_season_id,
                    previous_league_season_id, eligibility_reason,
                    source_contract_state, season_anchor_state, profile_json
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                    name = EXCLUDED.name,
                    trophies = EXCLUDED.trophies,
                    league_tier_id = EXCLUDED.league_tier_id,
                    league_tier_name = EXCLUDED.league_tier_name,
                    eligibility_state = EXCLUDED.eligibility_state,
                    current_league_season_id = EXCLUDED.current_league_season_id,
                    previous_league_season_id = EXCLUDED.previous_league_season_id,
                    eligibility_reason = EXCLUDED.eligibility_reason,
                    source_contract_state = EXCLUDED.source_contract_state,
                    season_anchor_state = EXCLUDED.season_anchor_state,
                    profile_json = EXCLUDED.profile_json
                RETURNING id
                """,
                (
                    player[0],
                    observation_id,
                    profile.normalized_tag,
                    profile.endpoint_version,
                    profile.schema_version,
                    profile.parser_version,
                    profile.observed_at,
                    http_status,
                    profile.name,
                    profile.trophies,
                    profile.league_tier_id,
                    profile.league_tier_name,
                    profile.eligibility_state,
                    profile.current_league_season_id,
                    profile.previous_league_season_id,
                    profile.eligibility_reason,
                    profile.source_contract_state,
                    profile.season_anchor_state,
                    Jsonb(profile.profile_json),
                ),
            ).fetchone()
            assert profile_version is not None
            profile_version_id = int(profile_version[0])
            connection.execute(
                """
                INSERT INTO player_profile_effects (profile_version_id, observation_id, effect_kind)
                VALUES (%s, %s, 'current_profile')
                ON CONFLICT (observation_id, effect_kind) DO NOTHING
                """,
                (profile_version_id, observation_id),
            )
            anchor_outcome = _record_season_anchor(
                connection, profile_version_id, profile
            )
            connection.execute(
                """
                WITH candidate AS (
                    SELECT v.id AS profile_version_id, v.observed_at,
                           v.eligibility_state,
                           v.eligibility_state IN ('eligible', 'ineligible')
                           AND NOT EXISTS (
                               SELECT 1
                               FROM player_profile_versions AS newer
                               WHERE newer.player_id = v.player_id
                                 AND newer.eligibility_state
                                     IN ('eligible', 'ineligible')
                                 AND (newer.observed_at, newer.id)
                                     > (v.observed_at, v.id)
                           ) AS update_eligibility,
                           v.source_contract_state = 'accepted'
                           AND (
                               p.current_observed_at IS NULL
                               OR p.current_observed_at < v.observed_at
                               OR (
                                   p.current_observed_at = v.observed_at
                                   AND (
                                       p.current_profile_version_id IS NULL
                                       OR p.current_profile_version_id < v.id
                                   )
                               )
                           ) AS update_profile
                    FROM player_profile_versions AS v
                    JOIN players AS p ON p.id = v.player_id
                    WHERE p.id = %s AND v.id = %s
                )
                UPDATE players AS p
                SET active = CASE
                        WHEN candidate.update_eligibility
                             AND candidate.eligibility_state = 'eligible' THEN true
                        WHEN candidate.update_eligibility
                             AND candidate.eligibility_state = 'ineligible' THEN false
                        ELSE p.active
                    END,
                    next_due_at = CASE
                        WHEN candidate.update_eligibility
                             AND candidate.eligibility_state = 'eligible'
                            THEN COALESCE(p.next_due_at, clock_timestamp())
                        WHEN candidate.update_eligibility
                             AND candidate.eligibility_state = 'ineligible' THEN NULL
                        ELSE p.next_due_at
                    END,
                    eligibility_state = CASE
                        WHEN candidate.update_eligibility
                            THEN candidate.eligibility_state
                        ELSE p.eligibility_state
                    END,
                    current_profile_version_id = CASE
                        WHEN candidate.update_profile
                            THEN candidate.profile_version_id
                        ELSE p.current_profile_version_id
                    END,
                    current_observed_at = CASE
                        WHEN candidate.update_profile THEN candidate.observed_at
                        ELSE p.current_observed_at
                    END,
                    updated_at = clock_timestamp()
                FROM candidate
                WHERE p.id = %s
                  AND (candidate.update_eligibility OR candidate.update_profile)
                """,
                (player[0], profile_version_id, player[0]),
            )
            job_outcomes._record_parsed_payload(
                connection,
                endpoint=endpoint,
                response_hash=response_hash,
                parser_version=profile.parser_version,
                schema_version=schema_version,
                parse_outcome="valid",
                parsed_json=profile.profile_json,
            )
            job_outcomes._record_processing_outcome(database, 
                connection,
                claim,
                outcome="processed",
                failure_category=(
                    "season_anchor_conflict"
                    if anchor_outcome == "conflict"
                    else None
                ),
            )
            reset_baselines._refresh_reset_baseline_evidence(database, connection, claim)
            database._finish_claim(
                connection, claim, job, state="complete", outcome="processed"
            )


def complete_rankings(
    database: Database,
    claim: Claim,
    rankings: ParsedOfficialRankings,
) -> None:
    if not getattr(database, "_supports_content_dedup", False):
        return _complete_rankings_legacy(database, claim, rankings)
    (
        observation_id,
        _http_status,
        response_hash,
        observed_at,
        endpoint,
        schema_version,
    ) = job_outcomes._observation_source(claim)
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            parsed_payload_id = job_outcomes._record_parsed_payload(
                connection,
                endpoint=endpoint,
                response_hash=response_hash,
                parser_version=rankings.parser_version,
                schema_version=schema_version,
                parse_outcome=(
                    "valid"
                    if rankings.outcome == "official_observed"
                    else "valid_with_gaps"
                ),
                parsed_json={
                    "items": [entry.source_json for entry in rankings.entries],
                    "paging": {"cursors": {}},
                },
            )
            attempt = connection.execute(
                """
                INSERT INTO official_top200_attempts (
                    observation_id, parser_version, outcome, failure_reasons,
                    observed_at, season_provenance, official_season_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    failure_reasons = EXCLUDED.failure_reasons
                RETURNING id
                """,
                (
                    observation_id,
                    rankings.parser_version,
                    rankings.outcome,
                    Jsonb(list(rankings.failure_reasons)),
                    observed_at,
                    rankings.season_provenance,
                    rankings.official_season_id,
                ),
            ).fetchone()
            assert attempt is not None
            player_ids: dict[str, int] = {}
            if rankings.entries:
                normalized_tags = [
                    entry.normalized_tag for entry in rankings.entries
                ]
                connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active, eligibility_state)
                    SELECT DISTINCT normalized_tag, false, 'unknown'
                    FROM unnest(%s::text[]) AS input(normalized_tag)
                    ON CONFLICT (normalized_tag) DO NOTHING
                    """,
                    (normalized_tags,),
                )
                rows = connection.execute(
                    """
                    SELECT id, normalized_tag
                    FROM players
                    WHERE normalized_tag = ANY(%s::text[])
                    """,
                    (normalized_tags,),
                ).fetchall()
                player_ids = {
                    _text_value(tag): int(player_id) for player_id, tag in rows
                }
                connection.execute(
                    """
                    INSERT INTO known_player_discoveries (
                        player_id, observation_id, source_row_index,
                        source_kind, discovered_at
                    )
                    SELECT players.id, %s, input.source_row_index,
                           'official_ranking', %s
                    FROM players
                    JOIN (
                        SELECT normalized_tag, min(source_row_index) AS source_row_index
                        FROM unnest(%s::text[], %s::integer[])
                            AS item(normalized_tag, source_row_index)
                        GROUP BY normalized_tag
                    ) AS input USING (normalized_tag)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        observation_id,
                        observed_at,
                        normalized_tags,
                        [entry.source_row_index for entry in rankings.entries],
                    ),
                )
            if (
                database.player_discovery_enabled
                and claim.work_type == "process_observation"
                and player_ids
            ):
                discovery_ids = sorted(set(player_ids.values()))
                for offset in range(0, len(discovery_ids), 500):
                    connection.execute(
                        "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                        (discovery_ids[offset : offset + 500],),
                    )
            official_entries = [
                entry for entry in rankings.entries if 1 <= entry.rank <= 200
            ]
            canonical_entries = connection.execute(
                """
                INSERT INTO official_top200_entries (
                    version_id, parsed_payload_id, source_row_index,
                    rank, player_id, normalized_tag, source_json
                )
                SELECT NULL, %s, entry.source_row_index, entry.rank,
                       entry.player_id, entry.normalized_tag, entry.source_json
                FROM jsonb_to_recordset(%s::jsonb) AS entry(
                    rank integer, source_row_index integer, player_id bigint,
                    normalized_tag text, source_json jsonb
                )
                ON CONFLICT (parsed_payload_id, source_row_index) DO NOTHING
                RETURNING id, source_row_index
                """,
                (
                    parsed_payload_id,
                    Jsonb(
                        [
                            {
                                "rank": entry.rank,
                                "source_row_index": entry.source_row_index,
                                "player_id": player_ids[entry.normalized_tag],
                                "normalized_tag": entry.normalized_tag,
                                "source_json": entry.source_json,
                            }
                            for entry in official_entries
                        ]
                    ),
                ),
            ).fetchall()
            if not canonical_entries:
                canonical_entries = connection.execute(
                    """
                    SELECT id, source_row_index
                    FROM official_top200_entries
                    WHERE parsed_payload_id = %s
                    ORDER BY source_row_index
                    """,
                    (parsed_payload_id,),
                ).fetchall()
            connection.execute(
                """
                INSERT INTO official_top200_attempt_entries (
                    attempt_id, source_row_id, rank, player_id,
                    normalized_tag, source_row_index
                )
                SELECT %s, source.id, entry.rank, entry.player_id,
                       entry.normalized_tag, entry.source_row_index
                FROM jsonb_to_recordset(%s::jsonb) AS entry(
                    rank integer, source_row_index integer, player_id bigint,
                    normalized_tag text
                )
                JOIN official_top200_entries AS source
                  ON source.parsed_payload_id = %s
                 AND source.source_row_index = entry.source_row_index
                ON CONFLICT (attempt_id, source_row_index) DO NOTHING
                """,
                (
                    attempt[0],
                    Jsonb(
                        [
                            {
                                "rank": entry.rank,
                                "source_row_index": entry.source_row_index,
                                "player_id": player_ids[entry.normalized_tag],
                                "normalized_tag": entry.normalized_tag,
                            }
                            for entry in official_entries
                        ]
                    ),
                    parsed_payload_id,
                ),
            )
            if rankings.outcome == "official_observed":

                version = connection.execute(
                    """
                    INSERT INTO official_top200_versions (
                        attempt_id, observation_id, observed_at, parser_version
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (attempt_id) DO UPDATE
                        SET attempt_id = EXCLUDED.attempt_id
                    RETURNING id
                    """,
                    (
                        attempt[0],
                        observation_id,
                        observed_at,
                        rankings.parser_version,
                    ),
                ).fetchone()
                assert version is not None
                connection.execute(
                    """
                    INSERT INTO official_top200_version_entries (
                        version_id, source_row_id, rank, player_id,
                        normalized_tag, source_row_index
                    )
                    SELECT %s, source.id, entry.rank, entry.player_id,
                           entry.normalized_tag, entry.source_row_index
                    FROM jsonb_to_recordset(%s::jsonb) AS entry(
                        rank integer, source_row_index integer, player_id bigint,
                        normalized_tag text
                    )
                    JOIN official_top200_entries AS source
                      ON source.parsed_payload_id = %s
                     AND source.source_row_index = entry.source_row_index
                    ON CONFLICT (version_id, rank) DO NOTHING
                    """,
                    (
                        version[0],
                        Jsonb(
                            [
                                {
                                    "rank": entry.rank,
                                    "source_row_index": entry.source_row_index,
                                    "player_id": player_ids[entry.normalized_tag],
                                    "normalized_tag": entry.normalized_tag,
                                }
                                for entry in official_entries
                            ]
                        ),
                        parsed_payload_id,
                    ),
                )
            job_outcomes._record_processing_outcome(database, 
                connection,
                claim,
                outcome="processed",
                failure_category=(
                    None
                    if rankings.outcome == "official_observed"
                    else rankings.outcome
                ),
                parsed_payload_id=parsed_payload_id,
            )
            database._finish_claim(
                connection,
                claim,
                job,
                state="complete",
                outcome=rankings.outcome,
            )


def _complete_rankings_legacy(
    database: Database,
    claim: Claim,
    rankings: ParsedOfficialRankings,
) -> None:
    (
        observation_id,
        _http_status,
        response_hash,
        observed_at,
        endpoint,
        schema_version,
    ) = job_outcomes._observation_source(claim)
    with database.pool.connection() as connection:
        with connection.transaction():
            job = database._lock_live_claim(connection, claim)
            attempt = connection.execute(
                """
                INSERT INTO official_top200_attempts (
                    observation_id, parser_version, outcome, failure_reasons,
                    observed_at, season_provenance, official_season_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (observation_id, parser_version) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    failure_reasons = EXCLUDED.failure_reasons
                RETURNING id
                """,
                (
                    observation_id,
                    rankings.parser_version,
                    rankings.outcome,
                    Jsonb(list(rankings.failure_reasons)),
                    observed_at,
                    rankings.season_provenance,
                    rankings.official_season_id,
                ),
            ).fetchone()
            assert attempt is not None
            player_ids: dict[str, int] = {}
            if rankings.entries:
                rows = connection.execute(
                    """
                    WITH input AS (
                        SELECT normalized_tag, min(source_row_index) AS source_row_index
                        FROM unnest(%s::text[], %s::integer[])
                            AS item(normalized_tag, source_row_index)
                        GROUP BY normalized_tag
                    ), players_upserted AS (
                        INSERT INTO players (normalized_tag, active, eligibility_state)
                        SELECT normalized_tag, false, 'unknown' FROM input
                        ON CONFLICT (normalized_tag) DO UPDATE
                            SET updated_at = clock_timestamp()
                        RETURNING id, normalized_tag
                    ), discoveries AS (
                        INSERT INTO known_player_discoveries (
                            player_id, observation_id, source_row_index,
                            source_kind, discovered_at
                        )
                        SELECT player.id, %s, input.source_row_index,
                               'official_ranking', %s
                        FROM players_upserted AS player
                        JOIN input USING (normalized_tag)
                        ON CONFLICT DO NOTHING
                    )
                    SELECT normalized_tag, id FROM players_upserted
                    """,
                    (
                        [entry.normalized_tag for entry in rankings.entries],
                        [entry.source_row_index for entry in rankings.entries],
                        observation_id,
                        observed_at,
                    ),
                ).fetchall()
                player_ids = {
                    _text_value(tag): int(player_id) for tag, player_id in rows
                }
            if (
                database.player_discovery_enabled
                and claim.work_type == "process_observation"
                and player_ids
            ):
                discovery_ids = sorted(set(player_ids.values()))
                for offset in range(0, len(discovery_ids), 500):
                    connection.execute(
                        "SELECT clashlens_enqueue_discovery_profiles(%s::bigint[])",
                        (discovery_ids[offset : offset + 500],),
                    )
            if rankings.outcome == "official_observed":
                version = connection.execute(
                    """
                    INSERT INTO official_top200_versions (
                        attempt_id, observation_id, observed_at, parser_version
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (attempt_id) DO UPDATE
                        SET attempt_id = EXCLUDED.attempt_id
                    RETURNING id
                    """,
                    (
                        attempt[0],
                        observation_id,
                        observed_at,
                        rankings.parser_version,
                    ),
                ).fetchone()
                assert version is not None
                connection.execute(
                    """
                    INSERT INTO official_top200_entries (
                        version_id, rank, player_id, normalized_tag, source_json
                    )
                    SELECT %s, entry.rank, entry.player_id,
                           entry.normalized_tag, entry.source_json
                    FROM jsonb_to_recordset(%s::jsonb) AS entry(
                        rank integer, player_id bigint,
                        normalized_tag text, source_json jsonb
                    )
                    ON CONFLICT (version_id, rank) DO UPDATE SET
                        player_id = EXCLUDED.player_id,
                        normalized_tag = EXCLUDED.normalized_tag,
                        source_json = EXCLUDED.source_json
                    """,
                    (
                        version[0],
                        Jsonb(
                            [
                                {
                                    "rank": entry.rank,
                                    "player_id": player_ids[entry.normalized_tag],
                                    "normalized_tag": entry.normalized_tag,
                                    "source_json": entry.source_json,
                                }
                                for entry in rankings.entries
                            ]
                        ),
                    ),
                )
            job_outcomes._record_parsed_payload(
                connection,
                endpoint=endpoint,
                response_hash=response_hash,
                parser_version=rankings.parser_version,
                schema_version=schema_version,
                parse_outcome="valid",
                parsed_json={
                    "items": [entry.source_json for entry in rankings.entries],
                    "paging": {"cursors": {}},
                },
            )
            job_outcomes._record_processing_outcome(database, 
                connection,
                claim,
                outcome="processed",
                failure_category=(
                    None
                    if rankings.outcome == "official_observed"
                    else rankings.outcome
                ),
            )
            database._finish_claim(
                connection,
                claim,
                job,
                state="complete",
                outcome=rankings.outcome,
            )


def get_player(database: Database, normalized_tag: str) -> dict[str, Any] | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT
                p.normalized_tag, p.active, p.eligibility_state,
                v.name, v.trophies, v.league_tier_id, v.league_tier_name,
                COALESCE(p.current_observed_at, v.observed_at),
                v.endpoint_version, v.schema_version,
                v.parser_version, v.source_http_status
            FROM players AS p
            JOIN player_profile_versions AS v ON v.id = p.current_profile_version_id
            WHERE p.normalized_tag = %s
            """,
            (normalized_tag,),
        ).fetchone()
        if row is None:
            return None
        return {
            "normalized_tag": _text_value(row[0]),
            "active": row[1],
            "eligibility_state": _text_value(row[2]),
            "name": _text_value(row[3]),
            "trophies": row[4],
            "league_tier_id": row[5],
            "league_tier_name": _text_value(row[6]),
            "observed_at": row[7],
            "endpoint_version": _text_value(row[8]),
            "schema_version": _text_value(row[9]),
            "parser_version": _text_value(row[10]),
            "source_http_status": row[11],
        }


def _record_season_anchor(
    connection: Any,
    profile_version_id: int,
    profile: ParsedProfile,
) -> str:
    outcome = "not_applicable"
    failure_reason: str | None = None
    anchor = None
    if profile.eligibility_state == "eligible":
        try:
            if profile.parser_version == PROFILE_PARSER_VERSION:
                if profile.season_anchor_current_id is None:
                    raise DomainRuleError(
                        "invalid_season_anchor", "profile current season is missing"
                    )
                anchor = validate_season_anchor(
                    profile.season_anchor_current_id,
                    profile.season_anchor_previous_id or "",
                )
            else:
                if (
                    profile.current_league_season_id is None
                    or profile.previous_league_season_id is None
                ):
                    raise DomainRuleError(
                        "invalid_season_anchor", "profile season values are missing"
                    )
                anchor = validate_season_anchor(
                    profile.current_league_season_id,
                    profile.previous_league_season_id,
                )
            outcome = "accepted"
        except DomainRuleError as error:
            outcome = "conflict"
            failure_reason = error.category

    if anchor is not None:

        def read_current(*, for_update: bool) -> Any:
            lock_clause = "FOR UPDATE OF a" if for_update else ""
            return connection.execute(
                f"""
                SELECT a.id, a.current_league_season_id,
                       a.previous_league_season_id, a.current_start,
                       v.observed_at
                FROM legend_season_anchors AS a
                JOIN player_profile_versions AS v
                  ON v.id = a.source_profile_version_id
                WHERE a.state = 'confirmed'
                  AND a.anchor_rule_version = %s
                {lock_clause}
                """,
                (SEASON_ANCHOR_RULE_VERSION,),
            ).fetchone()

        def matches_or_is_not_newer(current: Any) -> bool:
            return current is not None and (
                (
                    _text_value(current[1]) == anchor.current_id
                    and _text_value(current[2]) == anchor.previous_id
                )
                or profile.observed_at <= current[4]
            )

        current = read_current(for_update=False)
        if matches_or_is_not_newer(current):
            pass
        else:
            # A possible transition must re-read under the row lock. The
            # unlocked read is only an optimization for the common no-op
            # path and is never used to advance confirmed state.
            current = read_current(for_update=True)

        inserted_initial_anchor = False
        if current is None:
            inserted = connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    anchor.current_id,
                    anchor.previous_id,
                    anchor.current_start,
                    anchor.previous_start,
                    SEASON_ANCHOR_RULE_VERSION,
                    profile_version_id,
                ),
            ).fetchone()
            inserted_initial_anchor = inserted is not None
            if not inserted_initial_anchor:
                current = read_current(for_update=True)
                assert current is not None
        if inserted_initial_anchor or matches_or_is_not_newer(current):
            pass
        elif anchor.current_start > current[3]:
            connection.execute(
                "UPDATE legend_season_anchors SET state = 'superseded' WHERE id = %s",
                (current[0],),
            )
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, %s, %s, 'confirmed')
                ON CONFLICT (current_league_season_id, anchor_rule_version)
                DO UPDATE SET
                    source_profile_version_id = EXCLUDED.source_profile_version_id,
                    state = 'confirmed',
                    confirmed_at = clock_timestamp()
                """,
                (
                    anchor.current_id,
                    anchor.previous_id,
                    anchor.current_start,
                    anchor.previous_start,
                    SEASON_ANCHOR_RULE_VERSION,
                    profile_version_id,
                ),
            )
        else:
            outcome = "conflict"
            failure_reason = "season_anchor_disagreement"

    connection.execute(
        """
        INSERT INTO season_anchor_evidence (
            profile_version_id, current_league_season_id,
            previous_league_season_id, current_start, previous_start,
            anchor_rule_version, outcome, failure_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (profile_version_id) DO UPDATE SET
            outcome = EXCLUDED.outcome,
            failure_reason = EXCLUDED.failure_reason
        """,
        (
            profile_version_id,
            profile.current_league_season_id,
            profile.previous_league_season_id,
            anchor.current_start if anchor is not None else None,
            anchor.previous_start if anchor is not None else None,
            SEASON_ANCHOR_RULE_VERSION,
            outcome,
            failure_reason,
        ),
    )
    if outcome == "conflict":
        connection.execute(
            """
            UPDATE player_profile_versions
            SET source_contract_state = 'conflict', season_anchor_state = 'conflict'
            WHERE id = %s
            """,
            (profile_version_id,),
        )
    return outcome


def _profile_semantic_projection(profile: ParsedProfile) -> dict[str, Any]:
    clan = profile.profile_json.get("clan")
    clan_name = clan.get("name") if isinstance(clan, dict) else None
    return {
        "normalized_tag": profile.normalized_tag,
        "name": profile.name,
        "trophies": profile.trophies,
        "league_tier_id": profile.league_tier_id,
        "league_tier_name": profile.league_tier_name,
        "eligibility_state": profile.eligibility_state,
        "eligibility_reason": profile.eligibility_reason,
        "source_contract_state": profile.source_contract_state,
        "current_league_season_id": profile.current_league_season_id,
        "previous_league_season_id": profile.previous_league_season_id,
        "season_anchor_state": profile.season_anchor_state,
        "clan_name": clan_name,
    }



