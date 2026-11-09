"""Shared setup for group comparison postgres tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from clashlens.api_db import ApiDatabase

TODAY = datetime.fromisoformat("2026-08-06T05:00:00+00:00")


def battle(lens: str, stars: int, destruction: int, trophies: int) -> dict:
    return {
        "lens": lens,
        "battle_id": str(uuid4()),
        "battle_timestamp": "2026-08-05T10:00:00+00:00",
        "opponent": {"tag": "#LQ2", "name": "Opponent"},
        "stars": stars,
        "destruction_percentage": destruction,
        "trophy_change": trophies,
    }


def seed_day(
    database: ApiDatabase,
    tag: str,
    days_ago: int,
    *,
    net: int | None,
    state: str = "Complete",
    battles: list[dict] | None = None,
    gained: int | None = None,
    lost: int | None = None,
    attacks: int | None = None,
    defenses: int | None = None,
    reasons: list[str] | None = None,
) -> None:
    start = TODAY - timedelta(days=days_ago)
    with database.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, ranked_day_end, version, state,
                coverage, battles, partial_reasons, confidence, net_trophy_change,
                attack_gain, defense_loss, attack_count, defense_count
            )
            SELECT player.id, %s, %s, COALESCE(MAX(log.version), 0) + 1, %s,
                   %s, %s, %s, 'exact', %s, %s, %s, %s, %s
            FROM players AS player
            -- seed_profile already publishes a live row for today.
            LEFT JOIN api_player_daily_logs AS log
                ON log.player_id = player.id AND log.ranked_day_start = %s
            WHERE player.normalized_tag = %s
            GROUP BY player.id
            """,
            (
                start,
                start + timedelta(days=1),
                state,
                "complete" if state == "Complete" else "partial",
                Jsonb(battles or []),
                Jsonb(reasons if reasons is not None else [] if state == "Complete" else ["missing_battle_log"]),
                net,
                gained,
                lost,
                attacks,
                defenses,
                start,
                tag,
            ),
        )


def link_player(database: ApiDatabase, account_id: int, tag: str) -> None:
    request_id = str(uuid4())
    with database.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO private_api_requests (
                request_id, caller, provider, provider_subject, account_id,
                operation, method, request_target, identity_json, state,
                response_status, response_json, completed_at
            ) VALUES (
                %s, 'typescript-website', 'google', 'group-owner-subject', %s,
                'player_links.verify', 'POST', '/v1/players/verifytoken',
                '{}'::jsonb, 'complete', 200, '{}'::jsonb, clock_timestamp()
            )
            """,
            (request_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO verified_player_links (player_id, account_id, verification_request_id)
            VALUES ((SELECT id FROM players WHERE normalized_tag = %s), %s, %s)
            """,
            (tag, account_id, request_id),
        )
