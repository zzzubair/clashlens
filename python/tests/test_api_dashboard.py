from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database
from test_api_security import KEY, FakeDatabase, _signed_headers
from test_api_security import NOW as SIGNED_AT

from clashlens import api_dashboard
from clashlens.api import create_app
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
    attack_gain: int | None = None,
    start_trophies: int | None = None,
    input_evidence: dict[str, int] | None = None,
) -> None:
    with database.pool.connection() as connection:
        version_id = None
        if start_trophies is not None:
            version_id = connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end, official_season_id,
                    season_day_number, season_anchor_rule_version,
                    reconciliation_rule_version, result_hash, version, state,
                    confidence, start_trophies, input_evidence
                ) VALUES (
                    (SELECT id FROM players WHERE normalized_tag = %s),
                    '2026-08-06T05:00:00Z', '2026-08-07T05:00:00Z', 'test-season',
                    2, 'test-anchor', 'test-rules', %s, 1, 'Live', 'exact', %s, %s
                ) RETURNING id
                """,
                (tag, "a" * 64, start_trophies, Jsonb(input_evidence or {})),
            ).fetchone()[0]
        connection.execute(
            """
            UPDATE api_player_daily_logs
            SET coverage = 'complete', attack_count = %s, attack_gain = %s,
                defense_count = %s, defense_three_star_count = %s,
                defense_loss = %s, battles = %s, ranked_day_version_id = %s,
                published_at = '2026-08-06T11:30:00Z'
            WHERE player_id = (SELECT id FROM players WHERE normalized_tag = %s)
              AND ranked_day_start = '2026-08-06T05:00:00Z'
            """,
            (
                attacks, attack_gain, defenses, tripled, defense_loss,
                Jsonb(battles), version_id, tag,
            ),
        )
        connection.commit()


def publish_yesterday(
    database: ApiDatabase,
    tag: str,
    *,
    state: str,
    defenses: int,
    defense_loss: int,
    partial_reasons: list[str],
) -> None:
    with database.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, ranked_day_end, version, state,
                coverage, attack_count, defense_count, defense_loss,
                adjustments, battles, partial_reasons
            ) VALUES (
                (SELECT id FROM players WHERE normalized_tag = %s),
                '2026-08-05T05:00:00Z', '2026-08-06T05:00:00Z', 1,
                %s, 'complete', 8, %s, %s, '[]'::jsonb, '[]'::jsonb, %s
            )
            """,
            (tag, state, defenses, defense_loss, Jsonb(partial_reasons)),
        )
        connection.commit()


@pytest.mark.parametrize("enabled", [False, True])
def test_dashboard_today_read_refuses_while_the_dashboard_is_off(
    monkeypatch, enabled: bool
) -> None:
    monkeypatch.setattr(
        api_dashboard, "get_player_today", lambda _db, _board, tag, now: {"tag": tag}
    )
    app = create_app(
        FakeDatabase(),
        keys={("typescript-website", "current"): KEY},
        clock=lambda: SIGNED_AT,
        dashboard_enabled=enabled,
    )
    target = "/v1/players/%232PP/today"
    with TestClient(app) as client:
        response = client.get(target, headers=_signed_headers(target))
    if enabled:
        assert (response.status_code, response.json()) == (200, {"tag": "#2PP"})
    else:
        assert (response.status_code, response.json()["error"]) == (
            404,
            "dashboard_disabled",
        )


def test_dashboard_today_ranks_bases_and_automatic_defense(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 5000)
            seed_profile(database, "#8PY", 5100)
            seed_profile(database, "#9Q2", 4900)
            # You: 2 attacks and 2 defenses today, 6 of each still open, on
            # 5,000 after the Reset's 4,960, +70 and -30.
            publish_today(
                database,
                "#2PP",
                attacks=2,
                defenses=2,
                tripled=0,
                defense_loss=30,
                attack_gain=70,
                start_trophies=4960,
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
                attack_gain=0,
                start_trophies=5153,
                battles=[
                    battle("defense", "d1", "2026-08-06T06:00:00Z", "#2PL", 1, -5),
                    battle("defense", "a1", "2026-08-06T10:00:00Z", "#2PP", 3, -40),
                    battle("defense", "d3", "2026-08-06T11:15:00Z", "#2PC", 2, -8),
                ],
            )
            # Yesterday's 8 defenses lost 200, so the loss for each open
            # defense averages both days: (200 + 30) // (8 + 2).
            publish_yesterday(
                database, "#2PP", state="Complete", defenses=8, defense_loss=200,
                partial_reasons=[],
            )

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


def test_dashboard_range_counts_battles_with_their_trophies(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            # Your profile still shows the Reset's 5,000, but the log already
            # counts a +40 attack.
            seed_profile(database, "#2PP", 5000)
            publish_today(
                database, "#2PP", attacks=1, defenses=0, tripled=0, defense_loss=0,
                battles=[], attack_gain=40, start_trophies=5000,
            )
            # #8PY finished its day on 5,300.
            seed_profile(database, "#8PY", 5300)
            publish_today(
                database, "#8PY", attacks=8, defenses=8, tripled=0, defense_loss=0,
                battles=[], attack_gain=0, start_trophies=5300,
            )

            today = api_dashboard.get_player_today(
                database, api_dashboard.DashboardBoard(), "#2PP", now=NOW
            )

            # Your best is 5,040 + 7 x 40 = 5,320, so you can still pass #8PY.
            assert today is not None
            assert today["rank_range"] == {"best": 1, "worst": 2}
        finally:
            database.close()


def test_dashboard_range_counts_no_opponent_slots_as_used(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            # Seven +40 attacks and one "no opponent, no battle" attack slot,
            # and seven zero-loss defenses and one such defense slot: your day
            # is over on 5,280.
            seed_profile(database, "#2PP", 5280)
            publish_today(
                database, "#2PP", attacks=7, defenses=7, tripled=0, defense_loss=0,
                battles=[], attack_gain=280, start_trophies=5000,
                input_evidence={
                    "zero_result_attack_slots": 1,
                    "zero_result_defense_slots": 1,
                },
            )
            seed_profile(database, "#8PY", 5300)
            publish_today(
                database, "#8PY", attacks=8, defenses=8, tripled=0, defense_loss=0,
                battles=[], attack_gain=0, start_trophies=5300,
            )

            today = api_dashboard.get_player_today(
                database, api_dashboard.DashboardBoard(), "#2PP", now=NOW
            )

            assert today is not None
            assert today["rank_range"] == {"best": 2, "worst": 2}
        finally:
            database.close()


def test_dashboard_today_skips_a_yesterday_the_worker_rejects(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 5000)
            publish_today(
                database, "#2PP", attacks=0, defenses=3, tripled=0, defense_loss=30,
                battles=[],
            )
            # A 9th defense yesterday: the game went past its own cap.
            publish_yesterday(
                database, "#2PP", state="Partial", defenses=9, defense_loss=90,
                partial_reasons=["defense_count_exceeds_eight"],
            )

            today = api_dashboard.get_player_today(
                database, api_dashboard.DashboardBoard(), "#2PP", now=NOW
            )

            assert today is not None
            assert today["open_defenses"] == 5
            assert today["automatic_defense_each"] is None
        finally:
            database.close()
