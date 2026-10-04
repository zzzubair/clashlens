from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_api_db_organization import account_binding, create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_migration import migrated_production_database

from clashlens import api_accounts, api_analytics, api_groups, api_players
from clashlens.api_db import ApiDatabase, _screen_daily_log_with_events
from clashlens.domain import ranked_day_for

TODAY = datetime.fromisoformat("2026-08-06T05:00:00+00:00")


@pytest.mark.parametrize(
    "reasons,slots,disputed,gain_offset,count_offset,expected",
    [
        (["missing_start_battle_log_baseline"], 1, False, 0, 0, None),
        (["battle_log_overlap_gap"], 1, False, 0, 0, None),
        (["perspective_disagreement"], 1, False, 0, 0, None),
        ([], 1, True, 0, 0, None),
        ([], 1, False, 1, 0, None),
        ([], 1, False, 0, 1, None),
        (
            [
                "missing_start_baseline",
                "missing_end_battle_log_baseline",
                "automatic_defense_basis_unavailable",
            ],
            1,
            False,
            0,
            0,
            40,
        ),
        (["missing_start_battle_log_baseline"], 8, False, 0, 0, 0),
        (["missing_start_battle_log_baseline"], 8, True, 0, 0, None),
        (["missing_end_battle_log_baseline"], 0, False, 0, 0, 0),
    ],
)
def test_today_withholds_unproven_net_but_keeps_recorded_evidence(
    reasons, slots, disputed, gain_offset, count_offset, expected
) -> None:
    attacks = [battle("offense", 3, 100, 40) for _ in range(slots)]
    defenses = [battle("defense", 3, 100, -40) for _ in range(8)] if slots == 8 else []
    for event in attacks + defenses:
        event["battle_timestamp"] = (TODAY + timedelta(hours=1)).isoformat()
    if disputed:
        attacks[0]["disagreement"] = True
    day = {
        "ranked_day_start": TODAY.isoformat(),
        "ranked_day_end": (TODAY + timedelta(days=1)).isoformat(),
        "state": "Live",
        "coverage": "partial",
        "confidence": "partial",
        "partial_reasons": reasons,
        "net_trophy_change": None,
        "attack_count": slots + count_offset,
        "defense_count": len(defenses),
        "attack_gain": slots * 40 + gain_offset,
        "defense_loss": len(defenses) * 40,
        "battles": attacks + defenses,
    }
    row = (
        1,
        TODAY,
        "Live",
        "partial",
        "partial",
        reasons,
        None,
        day["attack_count"],
        day["defense_count"],
        day["battles"],
        day["attack_gain"],
        day["defense_loss"],
    )
    today = api_groups._window(1, {(1, TODAY): row}, [], TODAY, set())["today"]
    assert today == {
        "net": expected,
        "gained": day["attack_gain"],
        "lost": day["defense_loss"],
        "attacks": day["attack_count"],
        "defenses": day["defense_count"],
    }
    player = _screen_daily_log_with_events(day, "high", NOW)
    assert player["battles_complete"] is (expected is not None)


def test_member_list_and_add_wait_for_current_season(
    database_url: str,
) -> None:
    now = datetime(2026, 10, 5, 4, 59, 59, tzinfo=UTC)
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as info:
        database = ApiDatabase(info)
        try:
            seed_profile(
                database,
                "#2PP",
                6000,
                observed_at=datetime(2026, 9, 28, 4, 58, tzinfo=UTC),
            )
            owner = create_owner(database)
            original = create_group(database, owner, ["#2PP"])
            for index, (at, trophies, pending) in enumerate(
                [
                    (now, 6000, False),
                    (datetime(2026, 10, 5, 5, tzinfo=UTC), None, True),
                    (datetime(2026, 10, 5, 5, 1, tzinfo=UTC), None, True),
                    (datetime(2026, 10, 6, 12, tzinfo=UTC), None, True),
                    (datetime(2026, 9, 28, 5, 1, tzinfo=UTC), 6000, False),
                ]
            ):
                now = at
                members = api_accounts.list_groups(database, owner, now=now)
                member = next(
                    g for g in members if g["group_id"] == original.payload["group_id"]
                )["players"][0]
                assert (
                    member["trophies"],
                    member["season_reset_pending"],
                ) == (trophies, pending)
                assert (member["name"], member["state"]) == ("Player #2PP", "tracking")
                group = create_group(database, owner, [], name=f"Add {index}")
                group_id = group.payload["group_id"]
                binding = account_binding(
                    owner,
                    "groups.add_player",
                    f"/v1/account/groups/{group_id}/players",
                    {"group_id": group_id, "tag": "#2PP"},
                )
                added = api_accounts.add_group_player(
                    database,
                    binding,
                    group_id=group_id,
                    normalized_tag="#2PP",
                    now=now,
                )
                assert added.status_code == 200
                assert (
                    added.payload["trophies"],
                    added.payload["season_reset_pending"],
                ) == (trophies, pending)
                if index == 0:
                    first_binding, first_group_id, first_added = (
                        binding,
                        group_id,
                        added,
                    )
                elif index == 1:
                    replay = api_accounts.add_group_player(
                        database,
                        first_binding,
                        group_id=first_group_id,
                        normalized_tag="#2PP",
                        now=now,
                    )
                    assert replay.replayed and replay.payload == first_added.payload
                    # The saved operation stays intact; the notice must not
                    # repeat its pre-Reset trophy number as a current value.
                    assert replay.payload["trophies"] == 6000
                    assert next(g for g in members if g["group_id"] == first_group_id)[
                        "tags"
                    ] == ["#2PP"]
                comparison = api_groups.get_group_comparison(
                    database, owner, group_id, days=3, now=now, freshness_seconds=900
                )
                assert (
                    comparison["players"][0]["trophies"],
                    comparison["players"][0]["season_reset_pending"],
                ) == (trophies, pending)
                page = api_players.get_player_page(
                    database, "#2PP", now=now, freshness_seconds=900
                )
                assert page["season_reset_pending"] is pending
            now = datetime(2026, 10, 5, 5, 2, tzinfo=UTC)
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE player_profile_versions SET current_league_season_id = %s, trophies = 5000 WHERE normalized_tag = '#2PP'",
                    (ranked_day_for(now).official_season_id,),
                )
            member = api_accounts.list_groups(database, owner, now=now)[0]["players"][0]
            assert (member["trophies"], member["season_reset_pending"]) == (5000, False)
        finally:
            database.close()


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
            # Cleanup runs in batches: this row in the cleaned season is not
            # deleted yet, so it is still a recorded, counted day.
            seed_day(database, "#8PY", 6, net=5)
            # Today is still live: no net change is published until it ends.
            seed_day(
                database,
                "#2PP",
                0,
                net=None,
                state="Live",
                gained=80,
                lost=16,
                attacks=2,
                defenses=1,
                battles=[battle("offense", 3, 100, 40), battle("offense", 3, 100, 40),
                         battle("defense", 1, 45, -16)],
                reasons=["missing_end_battle_log_baseline"],
            )
            seed_day(database, "#8PY", 0, net=None, state="Live",
                     gained=40, lost=0, attacks=1, defenses=0,
                     battles=[battle("offense", 3, 100, 40)],
                     reasons=["battle_log_overlap_gap"])
            # The account's own player, outside the group, with a big day.
            seed_profile(database, "#YQ", 5400)
            seed_day(database, "#YQ", 1, net=100)
            # The season holding the two oldest days had its detail cleaned up.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO season_detail_retirements (
                        official_season_id, status, season_start, season_end
                    ) VALUES ('old-season', 'finalized', %s, %s)
                    """,
                    (TODAY - timedelta(days=9), TODAY - timedelta(days=5)),
                )
            account_id = create_owner(database)
            link_player(database, account_id, "#YQ")
            group = create_group(database, account_id, ["#2PP", "#8PY", "#9PY", "#QQQ"])
            assert group.status_code == 201

            result = compare(database, account_id, group.payload["group_id"])
            assert result is not None
            # The Season holding 2026-08-06, which opened at 2026-07-13 05:00 UTC.
            assert result["season"] == "1783918800"
            players = {player["tag"]: player for player in result["players"]}
            leader = players["#2PP"]
            # Cleaned-up days read as history no longer kept, missing days stay
            # empty, the last ended day may still change and a partial day is
            # shown but left out of the counted total.
            assert [(day["state"], day["net"]) for day in leader["day_results"]] == [
                ("retired", None),
                ("retired", None),
                ("missing", None),
                ("missing", None),
                ("complete", 20),
                ("partial", 10),
                ("correcting", 40),
            ]
            assert (
                leader["counted_days"],
                leader["counted_attacks"],
                leader["net"],
                leader["net_per_day"],
            ) == (2, 2, 60, 30)
            assert leader["today"] == {
                "net": 64,
                "gained": 80,
                "lost": 16,
                "attacks": 2,
                "defenses": 1,
            }
            assert players["#8PY"]["today"] == {
                "net": None, "gained": 40, "lost": 0, "attacks": 1, "defenses": 0,
            }
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
            # Each pair is compared only on days both have counted, and the
            # account's own player outside the group is not part of the group.
            assert leader["vs_group"] == 50
            assert players["#8PY"]["vs_group"] == -50
            assert players["#8PY"]["day_results"][1] == {
                "start": (TODAY - timedelta(days=6)).isoformat(),
                "state": "complete",
                "net": 5,
            }
            assert players["#8PY"]["counted_days"] == 2
            own = players["#YQ"]
            assert (own["you"], own["in_group"], own["vs_group"]) == (True, False, 85)
            # No result is not a zero result.
            assert players["#9PY"]["net"] is None
            assert players["#9PY"]["vs_group"] is None
            assert players["#9PY"]["attack"]["count"] == 0
            # An unknown tag started the same check a player page starts.
            assert players["#QQQ"]["status"] == "checking"
            assert players["#QQQ"]["trophies"] is None
            assert [player["tag"] for player in result["players"] if player["you"]] == [
                "#YQ"
            ]

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
            assert (
                compare(database, other_id.internal_id, group.payload["group_id"])
                is None
            )

            tags = sorted({f"#P{a}{b}" for a in "0289PYLQ" for b in "GRJ"})[:21]
            large = create_group(database, account_id, tags, name="Clan")
            with pytest.raises(api_groups.GroupTooLarge):
                compare(database, account_id, large.payload["group_id"])
        finally:
            database.close()


def test_profiles_from_before_a_season_reset_show_no_current_trophies(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            # Both profiles name the Season that ends at the August 10 Reset.
            seed_profile(database, "#2PP", 6400)
            seed_profile(database, "#8PY", 5100)
            account_id = create_owner(database)
            group = create_group(database, account_id, ["#2PP", "#8PY"])

            def read(now: datetime):
                result = api_groups.get_group_comparison(
                    database, account_id, group.payload["group_id"],
                    days=3, now=now, freshness_seconds=900,
                )
                found = api_players.search_known_players(
                    database, "Player", now=now, freshness_seconds=900
                )
                average = api_analytics.get_basic_analytics(
                    database, now=now, freshness_seconds=900
                )
                return (
                    [(p["tag"], p["trophies"], p["season_reset_pending"])
                     for p in result["players"]],
                    [(r["tag"], r["trophies"], r["season_reset_pending"])
                     for r in found],
                    (average["sample_size"], average["results"]["average_trophies"]),
                )

            groups, found, average = read(NOW)
            assert groups == [("#2PP", 6400, False), ("#8PY", 5100, False)]
            assert found == [("#2PP", 6400, False), ("#8PY", 5100, False)]
            assert average == (2, 5750)

            # Day 2 of the next Season: only #8PY has reported its Season reset.
            now = datetime.fromisoformat("2026-08-11T12:00:00+00:00")
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE player_profile_versions
                    SET current_league_season_id = %s
                    WHERE normalized_tag = '#8PY'
                    """,
                    (ranked_day_for(now).official_season_id,),
                )
            groups, found, average = read(now)
            assert groups == [("#2PP", None, True), ("#8PY", 5100, False)]
            assert found == [("#8PY", 5100, False), ("#2PP", None, True)]
            assert average == (1, 5100)
        finally:
            database.close()
