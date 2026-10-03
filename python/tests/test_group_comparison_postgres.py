from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_api_db_organization import account_binding, create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database

from clashlens import api_accounts, api_groups
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
    net: int,
    state: str = "Complete",
    battles: list[dict] | None = None,
) -> None:
    start = TODAY - timedelta(days=days_ago)
    with database.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, ranked_day_end, version, state,
                coverage, battles, partial_reasons, confidence, net_trophy_change
            ) VALUES (
                (SELECT id FROM players WHERE normalized_tag = %s), %s, %s, 1, %s,
                %s, %s, %s, 'exact', %s
            )
            """,
            (
                tag,
                start,
                start + timedelta(days=1),
                state,
                "complete" if state == "Complete" else "partial",
                Jsonb(battles or []),
                Jsonb([] if state == "Complete" else ["missing_battle_log"]),
                net,
            ),
        )


def create_group(
    database: ApiDatabase, account_id: int, tags: list[str], *, name: str = "Rivals"
):
    return api_accounts.create_group(
        database,
        account_binding(
            account_id,
            "groups.create",
            "/v1/account/groups",
            {"name": name, "tags": tags},
        ),
        name=name,
        normalized_name=name.lower(),
        normalized_tags=tags,
    )


def compare(database: ApiDatabase, account_id: int, group_id: str, days: int = 7):
    return api_groups.get_group_comparison(
        database, account_id, group_id, days=days, now=NOW, freshness_seconds=900
    )


def test_group_comparison_counts_samples_and_keeps_missing_days_empty(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            seed_profile(database, "#2PP", 5300)
            seed_profile(database, "#8PY", 5200)
            seed_profile(database, "#9PY", 5100)
            seed_day(
                database,
                "#2PP",
                1,
                net=40,
                battles=[
                    battle("offense", 3, 100, 40),
                    battle("offense", 2, 80, 16),
                    battle("defense", 1, 45, -16),
                ],
            )
            seed_day(database, "#2PP", 2, net=10, state="Partial")
            seed_day(database, "#2PP", 3, net=20)
            seed_day(database, "#8PY", 1, net=-10)
            account_id = create_owner(database)
            group = create_group(database, account_id, ["#2PP", "#8PY", "#9PY", "#QQQ"])
            assert group.status_code == 201

            result = compare(database, account_id, group.payload["group_id"])
            assert result is not None
            players = {player["tag"]: player for player in result["players"]}
            leader = players["#2PP"]
            # Missing days stay empty, the last ended day may still change and
            # a partial day is shown but left out of the counted total.
            assert [(day["state"], day["net"]) for day in leader["day_results"]] == [
                ("missing", None),
                ("missing", None),
                ("missing", None),
                ("missing", None),
                ("complete", 20),
                ("partial", 10),
                ("correcting", 40),
            ]
            assert (leader["counted_days"], leader["net"], leader["net_per_day"]) == (
                2,
                60,
                30,
            )
            assert leader["attack"] == {
                "count": 2,
                "stars": 5,
                "destruction": 180,
                "three_stars": 1,
                "trophies": 56,
            }
            assert leader["defense"] == {
                "count": 1,
                "stars": 1,
                "destruction": 45,
                "trophies": 16,
                "star_counts": {"0": 0, "1": 1, "2": 0, "3": 0},
            }
            assert leader["trophies"] == 5300 and leader["freshness"] == "fresh"
            assert leader["vs_group_per_day"] == 40
            assert players["#8PY"]["vs_group_per_day"] == -40
            # No result is not a zero result.
            assert players["#9PY"]["net"] is None
            assert players["#9PY"]["vs_group_per_day"] is None
            assert players["#9PY"]["attack"]["count"] == 0
            # An unknown tag started the same check a player page starts.
            assert players["#QQQ"]["status"] == "checking"
            assert players["#QQQ"]["trophies"] is None
            assert all(not player["you"] for player in result["players"])

            three = compare(database, account_id, group.payload["group_id"], 3)
            assert three is not None and len(three["day_starts"]) == 3

            # Only the owning account can read it.
            other = api_accounts.create_account(
                database,
                account_binding(
                    None,  # type: ignore[arg-type]
                    "account.create",
                    "/v1/account",
                    {"username": "otherowner"},
                    subject="other-subject",
                ),
                username="otherowner",
                normalized_username="otherowner",
                display_name="Other",
            )
            assert other.status_code == 201
            other_id = api_accounts.resolve_account(database, "google", "other-subject")
            assert compare(database, other_id.internal_id, group.payload["group_id"]) is None

            tags = sorted({f"#P{a}{b}" for a in "0289PYLQ" for b in "GRJ"})[:21]
            large = create_group(database, account_id, tags, name="Clan")
            with pytest.raises(api_groups.GroupTooLarge):
                compare(database, account_id, large.payload["group_id"])
        finally:
            database.close()
