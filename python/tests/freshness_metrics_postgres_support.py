"""Shared setup for freshness metrics postgres tests."""

from __future__ import annotations


def seed_check(connection, tag, endpoint, at, *, not_found=None):
    connection.execute(
        """
        INSERT INTO collector_response_state (
            scope, identity_key, endpoint, player_id, normalized_tag,
            last_response_hash, last_content_fingerprint,
            last_occurrence_key, last_applied_occurrence_key,
            last_seen_at, last_success_at, last_not_found_at
        ) SELECT 'player', normalized_tag, %s, id, normalized_tag,
                 repeat('a', 64), repeat('a', 64), normalized_tag || %s, 'seed',
                 COALESCE(%s, clock_timestamp()), %s, %s
          FROM players WHERE normalized_tag = %s
        """,
        (endpoint, endpoint, at, at, not_found, tag),
    )
