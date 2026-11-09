"""Shared setup for api db public ops tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from clashlens.api_db import ApiDatabase
from clashlens.api_requests import RequestBinding
from clashlens.domain import ranked_day_for

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def anonymous_binding(operation: str, target: str, tag: str) -> RequestBinding:
    return RequestBinding(
        request_id=str(uuid4()),
        caller="typescript-website",
        provider="",
        provider_subject="",
        account_id=None,
        operation=operation,
        method="POST",
        request_target=target,
        identity={"tag": tag},
    )


def seed_profile(
    database: ApiDatabase, tag: str, trophies: int, *, observed_at: datetime = NOW
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            """
            INSERT INTO players (normalized_tag, active, eligibility_state)
            VALUES (%s, true, 'eligible')
            RETURNING id
            """,
            (tag,),
        ).fetchone()[0]
        work_id = connection.execute(
            """
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                league_history_status
            ) VALUES (
                'initial_collection', 'interactive', 'player', %s, %s, %s,
                %s, 'pending', 'pending', 'pending', 'pending'
            ) RETURNING id
            """,
            (player_id, tag, observed_at, f"seed:{tag}"),
        ).fetchone()[0]
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, scope, player_id, normalized_tag, endpoint,
                request_started_at, response_completed_at, http_status,
                response_hash, collector_version, key_label, evidence_headers,
                request_method, request_path, request_query,
                paging_envelope_state, source_adapter_version
            ) VALUES (
                %s, 'player', %s, %s, 'profile', %s, %s, 200,
                %s, 'test', 'normal-test', '{}'::jsonb,
                'GET', %s, '', 'not_applicable', 'supercell-profile-parser-v3'
            ) RETURNING id
            """,
            (
                f"seed:{tag}:profile",
                player_id,
                tag,
                observed_at,
                observed_at,
                "a" * 64,
                f"/v1/players/%23{tag.removeprefix('#')}",
            ),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE collector_work
            SET status = 'complete', profile_status = 'observed',
                battle_log_status = 'observed', league_history_status = 'observed',
                profile_observation_id = %s,
                completed_at = %s, updated_at = %s
            WHERE id = %s
            """,
            (observation_id, observed_at, observed_at, work_id),
        )
        profile_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, current_league_season_id, profile_json
            ) VALUES (
                %s, %s, %s, 'profile-v1', 'profile-schema-v1',
                'profile-parser-v1', %s, 200, %s, %s, 105000036,
                'Legend I', 'eligible', %s, '{}'::jsonb
            ) RETURNING id
            """,
            (
                player_id,
                observation_id,
                tag,
                observed_at,
                f"Player {tag}",
                trophies,
                ranked_day_for(observed_at).official_season_id,
            ),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE players
            SET current_profile_version_id = %s, current_observed_at = %s
            WHERE id = %s
            """,
            (profile_id, observed_at, player_id),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                adjustments, battles, partial_reasons
            ) VALUES (
                %s, '2026-08-06T05:00:00Z', 1, 'Live', 'partial',
                '[]'::jsonb, '[]'::jsonb, '["active_day"]'::jsonb
            )
            """,
            (player_id,),
        )
        # The weekly league record, where the schema has it, has decided the
        # profile's week.
        if connection.execute("SELECT to_regclass('league_week_dirty')").fetchone()[0]:
            connection.execute("DELETE FROM league_week_dirty WHERE player_id = %s", (player_id,))
        connection.commit()


def seed_league_history(
    database: ApiDatabase,
    tag: str,
    season_id: str,
    *,
    observed_at: datetime = NOW,
    tier_id: int = 105000036,
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
        ).fetchone()[0]
        response_hash = uuid4().hex + uuid4().hex
        observation_id = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, scope, player_id, normalized_tag, endpoint,
                request_started_at, response_completed_at, http_status,
                response_hash, collector_version, key_label, evidence_headers,
                request_method, request_path, request_query,
                paging_envelope_state, source_adapter_version
            ) VALUES (
                %s, 'player', %s, %s, 'league_history', %s, %s, 200,
                %s, 'test', 'normal-test', '{}'::jsonb,
                'GET', %s, '', 'not_applicable', 'league-history-v1'
            ) RETURNING id
            """,
            (
                f"seed:{tag}:league-history:{season_id}",
                player_id,
                tag,
                observed_at,
                observed_at,
                response_hash,
                f"/v1/players/%23{tag.removeprefix('#')}/leaguehistory",
            ),
        ).fetchone()[0]
        payload_id = connection.execute(
            """
            INSERT INTO parsed_source_payloads (
                endpoint, response_hash, parser_version, schema_version,
                parse_outcome, parsed_json
            ) VALUES (
                'league_history', %s, 'supercell-league-history-parser-v1',
                'league-history-schema-v1', 'valid', '{}'::jsonb
            ) RETURNING id
            """,
            (response_hash,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO player_league_history_entries (
                player_id, league_season_id, observed_at, observation_id,
                parsed_payload_id, league_trophies, league_tier_id, placement,
                source_json
            ) VALUES (%s, %s, %s, %s, %s, 5812, %s, 12, '{}'::jsonb)
            """,
            (
                player_id,
                season_id,
                observed_at,
                observation_id,
                payload_id,
                tier_id,
            ),
        )
        connection.commit()
