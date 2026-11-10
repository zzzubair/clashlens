"""Shared setup for army season summaries postgres tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

SEASON = "1785714000"


DAY0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)


def _fact_hash(lens: str, index: int) -> str:
    return hashlib.sha256(f"{lens}:{index}".encode()).hexdigest()


def _seed(
    connection,
    *,
    player_tag="#2PP",
    offense: list[dict] | None = None,
    defense: list[dict] | None = None,
    completed_days: int = 28,
    with_day28_log: bool = True,
):
    """Seed facts directly; parents are bypassed test-only via replica role."""
    connection.execute("SET LOCAL session_replication_role = replica")
    player_id = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state)
        VALUES (%s, true, 'eligible')
        RETURNING id
        """,
        (player_tag,),
    ).fetchone()[0]
    if with_day28_log:
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, 1, 'Complete', 'complete', %s, %s, 28)
            """,
            (player_id, DAY0 + timedelta(days=27), DAY0 + timedelta(days=28), SEASON),
        )
    for index, (lens, specs) in enumerate(
        (("offense", offense or []), ("defense", defense or []))
    ):
        for position, spec in enumerate(specs):
            connection.execute(
                """
                INSERT INTO army_analytics_battle_facts (
                    battle_id, evidence_id, source_ranked_day_version_id,
                    ranked_day_start, official_season_id, season_day_number,
                    lens, population_player_id, stars, destruction_percentage,
                    army_state, home_troops, spells, siege, cc_troops, heroes,
                    unresolved_components, perspective_disagreement,
                    battle_time_trophies, input_hash, version
                ) VALUES (
                    %s, %s, 4242, %s, %s, 1, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s, 1
                )
                """,
                (
                    7000 + index * 10 + position,
                    8000 + index * 10 + position,
                    DAY0,
                    SEASON,
                    lens,
                    player_id,
                    spec["stars"],
                    spec.get("destruction", 100),
                    spec.get("army_state", "decoded"),
                    json.dumps(spec.get("home_troops", [])),
                    json.dumps(spec.get("spells", [])),
                    json.dumps(spec.get("siege", [])),
                    json.dumps(spec.get("cc_troops", [])),
                    json.dumps(spec.get("heroes", [])),
                    json.dumps(spec.get("unresolved", [])),
                    spec.get("disagreement", False),
                    spec.get("trophies", 6000),
                    _fact_hash(lens, index * 10 + position),
                ),
            )
    for day in range(1, completed_days + 1):
        connection.execute(
            """
            INSERT INTO army_analytics_completed_days (
                ranked_day_start, official_season_id, season_day_number,
                fact_input_hash
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                DAY0 + timedelta(days=day - 1),
                SEASON,
                day,
                hashlib.sha256(f"day:{day}".encode()).hexdigest(),
            ),
        )
    return player_id
