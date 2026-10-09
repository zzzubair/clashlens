from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database

from clashlens import api_dashboard
from clashlens.api_db import ApiDatabase


def battle(
    lens: str, battle_id: str, at: str, opponent: str, stars: int, trophies: int
) -> dict[str, Any]:
    return {
        "lens": lens,
        "battle_id": battle_id,
        "battle_timestamp": at,
        "opponent": {"tag": opponent, "name": f"Player {opponent}"},
        "destruction_percentage": 100 if stars == 3 else 70,
        "stars": stars,
        "trophy_change": trophies,
    }


def publish_today(
    database: ApiDatabase,
    tag: str,
    *,
    attacks: int,
    defenses: int,
    tripled: int,
    defense_loss: int,
    battles: list[dict[str, Any]],
) -> None:
    with database.pool.connection() as connection:
        connection.execute(
            """
            UPDATE api_player_daily_logs
            SET coverage = 'complete', attack_count = %s, defense_count = %s,
                defense_three_star_count = %s, defense_loss = %s, battles = %s,
                published_at = '2026-08-06T11:30:00Z'
            WHERE player_id = (SELECT id FROM players WHERE normalized_tag = %s)
              AND ranked_day_start = '2026-08-06T05:00:00Z'
            """,
            (attacks, defenses, tripled, defense_loss, Jsonb(battles), tag),
        )
        connection.commit()


def test_dashboard_today_ranks_bases_and_automatic_defense(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 5000)
            seed_profile(database, "#8PY", 5100)
            seed_profile(database, "#9Q2", 4900)
            # You: 2 attacks and 2 defenses today, 6 of each still open.
            publish_today(
                database,
                "#2PP",
                attacks=2,
                defenses=2,
                tripled=0,
                defense_loss=30,
                battles=[
                    battle("offense", "a1", "2026-08-06T10:00:00Z", "#8PY", 3, 40),
                    battle("offense", "a2", "2026-08-06T11:00:00Z", "#9Q2", 2, 30),
                ],
            )
            # #8PY used every attack, and held 2 of its 3 defenses, one of them yours.
            publish_today(
                database,
                "#8PY",
                attacks=8,
                defenses=3,
                tripled=1,
                defense_loss=53,
                battles=[
                    battle("defense", "d1", "2026-08-06T06:00:00Z", "#2PL", 1, -5),
                    battle("defense", "a1", "2026-08-06T10:00:00Z", "#2PP", 3, -40),
                    battle("defense", "d3", "2026-08-06T11:15:00Z", "#2PC", 2, -8),
                ],
            )
            with database.pool.connection() as connection:
                # Yesterday's 8 defenses lost 200, so the loss for each open
                # defense averages both days: (200 + 30) // (8 + 2).
                connection.execute(
                    """
                    INSERT INTO api_player_daily_logs (
                        player_id, ranked_day_start, ranked_day_end, version, state,
                        coverage, attack_count, defense_count, defense_loss,
                        adjustments, battles, partial_reasons
                    ) VALUES (
                        (SELECT id FROM players WHERE normalized_tag = '#2PP'),
                        '2026-08-05T05:00:00Z', '2026-08-06T05:00:00Z', 1,
                        'Complete', 'complete', 8, 8, 200,
                        '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
                    )
                    """
                )
                connection.commit()

            board = api_dashboard.DashboardBoard()
            today = api_dashboard.get_player_today(database, board, "#2PP", now=NOW)

            assert today is not None
            # Best: nobody's worst end (5,100 - 5 x 40, 4,900 - 8 x 40) beats
            # your best 5,000 + 6 x 40. Worst: both others can still reach
            # your worst 5,000 - 6 x 40.
            assert today["rank_range"] == {"best": 1, "worst": 3}
            assert api_dashboard.get_player_today(database, board, "#8PY", now=NOW)[
                "rank_range"
            ] == {"best": 1, "worst": 3}
            assert today["legends_held"] == {"held": 4, "defenses": 5}
            assert today["open_defenses"] == 6
            assert today["automatic_defense_each"] == 23
            assert [
                (row["tag"], row["hit"]["stars"], row["defenses"])
                for row in today["opponents"]
            ] == [
                (
                    "#8PY",
                    3,
                    [
                        {"stars": 1, "yours": False},
                        {"stars": 3, "yours": True},
                        {"stars": 2, "yours": False},
                    ],
                ),
                # #9Q2's log has no battles yet; your attack is still shown.
                ("#9Q2", 2, [{"stars": 2, "yours": True}]),
            ]
            assert today["opponents"][0]["observed_at"] == "2026-08-06T11:30:00+00:00"

            assert api_dashboard.get_player_today(database, board, "#QQQ", now=NOW) is None
        finally:
            database.close()


def test_dashboard_today_leaves_the_loss_unknown_without_yesterday(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 5000)
            publish_today(
                database, "#2PP", attacks=0, defenses=3, tripled=1, defense_loss=50,
                battles=[],
            )

            today = api_dashboard.get_player_today(
                database, api_dashboard.DashboardBoard(), "#2PP", now=NOW
            )

            assert today is not None
            assert today["open_defenses"] == 5
            assert today["automatic_defense_each"] is None
            assert today["opponents"] == []
        finally:
            database.close()
