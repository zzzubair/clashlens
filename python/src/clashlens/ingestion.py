from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import (
    job_outcomes,
    queue_refresh,
    reset_baselines,
    season_anchors,
)
from .db import Claim, Database, _text_value, enqueue_discovered_players
from .domain import ranked_day_for
from .profile import ParsedProfile
from .rankings import ParsedOfficialRankings
from .response_fields import content_fingerprint
from .season_anchors import AnchorOutcome

# The game applies the automatic defense loss about 7 to 13 minutes after the
# Reset, and on 6 October 2026 a reading at 05:09:56 first showed the ended
# day's attack gains; see ``supersede_profile``.
RESET_SETTLING_WINDOW = timedelta(minutes=30)


def supersede_profile(database: Database, claim: Claim) -> bool:
    """Skip a profile when a later one from the same Legend day was applied.

    Only the newest profile sets the leaderboard. Each Legend day's last
    profile is never skipped: end-of-day snapshots read the latest profile at
    or before the Reset, so only a newer profile from the same day covers one.
    Nor is one read in the first RESET_SETTLING_WINDOW after a Reset, while
    the game finishes crediting the ended day and charges its automatic
    defense loss: such a reading can settle the ended day's end (see
    ``ranked_day_inputs.load_readings``). Every other profile can still
    be skipped, so a post-Reset backlog costs at most that window's profiles.
    """
    if claim.normalized_tag is None or claim.observed_at is None:
        return False
    day = ranked_day_for(claim.observed_at)
    if claim.observed_at < day.start + RESET_SETTLING_WINDOW:
        return False
    day_end = day.end

    def covered(connection: Any) -> bool:
        return connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM players AS player
                JOIN player_profile_versions AS version ON version.player_id = player.id
                JOIN player_profile_effects AS effect
                  ON effect.profile_version_id = version.id
                WHERE player.normalized_tag = %s
                  AND version.source_contract_state = 'accepted'
                  AND effect.observed_at > %s AND effect.observed_at < %s
            )
            """,
            (claim.normalized_tag, claim.observed_at, day_end),
        ).fetchone()[0]

    return job_outcomes.complete_superseded(database, claim, covered)


def complete_profile(database: Database, claim: Claim, profile: ParsedProfile) -> None:
    projection = _profile_semantic_projection(profile)
    # The clan tag links the clan in the game. It stays out of the projection,
    # so it alone never makes a new profile version.
    clan = profile.profile_json.get("clan")
    clan_tag = clan.get("tag") if isinstance(clan, dict) else None
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
    fingerprint = content_fingerprint(
        "profile", json.dumps(profile.profile_json).encode(),
        http_status=http_status, response_hash=response_hash,
    )
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
                representation="player_profile_versions",
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
                        Jsonb({"clan": {"name": projection["clan_name"], "tag": clan_tag}}),
                        parsed_payload_id, Jsonb(projection),
                    ),
                ).fetchone()
                assert profile_version is not None
            profile_version_id = int(profile_version[0])
            if created_profile:
                anchor_outcome = season_anchors.record(
                    connection, profile_version_id, profile
                )
            else:
                retained_anchor = connection.execute(
                    "SELECT outcome FROM season_anchor_evidence WHERE profile_version_id = %s",
                    (profile_version_id,),
                ).fetchone()
                anchor_outcome = (
                    AnchorOutcome(_text_value(retained_anchor[0]))
                    if retained_anchor is not None
                    else (
                        AnchorOutcome.CONFLICT
                        if profile.source_contract_state == "conflict"
                        else AnchorOutcome.NOT_APPLICABLE
                    )
                )
            processing_id = job_outcomes._record_processing_outcome(database, 
                connection,
                claim,
                outcome="processed",
                failure_category=(
                    "season_anchor_conflict"
                    if anchor_outcome is AnchorOutcome.CONFLICT
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
                "SELECT id FROM players WHERE id = %s FOR NO KEY UPDATE", (player[0],)
            )
            if profile.eligibility_state in {"eligible", "ineligible"}:
                # The list keeps no negative count, so a lower-league profile
                # showing one is listed without it.
                connection.execute(
                    "SELECT clashlens_note_promotion_candidate(%s, %s, %s, %s)",
                    (profile.normalized_tag, profile.league_tier_id,
                     profile.trophies if profile.trophies >= 0 else None, profile.observed_at),
                )
            queue_refresh.queue_for_reading(
                connection, player[0], f"reading:{observation_id}", profile.observed_at
            )
            connection.execute(
                """
                WITH candidate AS (
                    SELECT effect.profile_version_id, effect.observed_at,
                           %s::text AS content_fingerprint,
                           version.source_contract_state = 'accepted'
                           AND (
                               p.current_observed_at IS NULL
                               OR GREATEST(p.current_observed_at, p.current_profile_confirmed_at) < effect.observed_at
                               OR (GREATEST(p.current_observed_at, p.current_profile_confirmed_at) = effect.observed_at
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
                SET current_profile_version_id = candidate.profile_version_id,
                    current_observed_at = candidate.observed_at,
                    current_profile_fingerprint = candidate.content_fingerprint,
                    updated_at = CASE
                        WHEN p.current_profile_version_id IS DISTINCT FROM candidate.profile_version_id
                        THEN clock_timestamp()
                        ELSE p.updated_at
                    END
                FROM candidate
                WHERE p.id = %s AND candidate.update_profile
                """,
                (fingerprint, player[0], observation_id, player[0]),
            )
            # Tracking follows the weekly league record, decided from this
            # answer within seconds (``league_days.track``).
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
            anchor_outcome = season_anchors.record(
                connection, profile_version_id, profile
            )
            queue_refresh.queue_for_reading(
                connection, player[0], f"reading:{observation_id}", profile.observed_at
            )
            connection.execute(
                """
                WITH candidate AS (
                    SELECT v.id AS profile_version_id, v.observed_at,
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
                SET current_profile_version_id = CASE
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
                WHERE p.id = %s AND candidate.update_profile
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
                    if anchor_outcome is AnchorOutcome.CONFLICT
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
            enqueue_discovered_players(
                connection, database, claim, player_ids.values()
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
            enqueue_discovered_players(
                connection, database, claim, player_ids.values()
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
