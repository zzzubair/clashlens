from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from domain_test_support import domain_database
from psycopg.types.json import Jsonb
from test_crews_postgres import Site

from clashlens import api_crew_boards
from clashlens.domain import ranked_day_for

# The same saved days and hand-worked totals the website's Season summary
# test reads (website/tests/unit/crew-boards-parity.test.ts).
PARITY = json.loads(
    (Path(__file__).parents[2] / "testdata" / "crew-boards-parity.json").read_text()
)
NOW = datetime.fromisoformat(PARITY["now"])
TODAY = NOW.replace(hour=5)
DAY = timedelta(days=1)


@pytest.fixture
def site(database_url: str):
    with domain_database(database_url) as connection_info:
        built = Site(connection_info)
        try:
            yield built
        finally:
            built.close()


def battle(lens: str, battle_id: str, at: datetime, stars: int, trophies: int) -> dict:
    return {
        "lens": lens,
        "battle_id": battle_id,
        "battle_timestamp": at.isoformat(),
        "opponent": {"tag": "#LQ2", "name": "Rival"},
        "stars": stars,
        "destruction_percentage": 100 if stars == 3 else 60,
        "trophy_change": trophies,
    }


def seed_day(
    site: Site,
    tag: str,
    start: datetime,
    battles: list[dict[str, Any]] = (),
    *,
    reasons: list[str] = (),
) -> None:
    """Publish a newer saved log of one Legend day."""
    day = ranked_day_for(start)
    attacks = sum(item["lens"] == "offense" for item in battles)
    site.sql(
        """
        INSERT INTO api_player_daily_logs (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, version, state, coverage, confidence, battles,
            partial_reasons, attack_count, defense_count
        )
        SELECT player.id, %s, %s, %s, %s, COALESCE(MAX(log.version), 0) + 1,
               'Complete', 'complete', 'exact', %s, %s, %s, %s
        FROM players AS player
        LEFT JOIN api_player_daily_logs AS log
            ON log.player_id = player.id AND log.ranked_day_start = %s
        WHERE player.normalized_tag = %s
        GROUP BY player.id
        """,
        (
            day.start, day.end, day.official_season_id, day.day_number,
            Jsonb(list(battles)), Jsonb(list(reasons)), attacks,
            len(battles) - attacks, day.start, tag,
        ),
    )


def seed_reset_board(
    site: Site, reset: datetime, trophies: dict[str, int], state: str = "published"
) -> int:
    """Save a Reset board with these accounts in this order."""
    board = site.sql(
        """
        INSERT INTO leaderboard_snapshots (
            snapshot_kind, boundary_at, version, ordering_rule_version,
            freshness_rule_version, state, measured_coverage, stale_entry_count
        ) VALUES (
            'frozen', %s,
            (SELECT COALESCE(MAX(version), 0) + 1 FROM leaderboard_snapshots
             WHERE boundary_at = %s),
            'ordering-v1', 'freshness-v1', %s, 1.0, 0
        ) RETURNING id
        """,
        (reset, reset, state),
    )[0][0]
    for position, (tag, total) in enumerate(trophies.items(), start=1):
        site.sql(
            """
            INSERT INTO leaderboard_snapshot_entries (
                snapshot_id, position, player_id, trophies, trophy_observation_id,
                trophy_observed_at, observation_age_seconds, freshness,
                confidence, tie_hash
            )
            SELECT %s, %s, player_id, %s, observation_id, %s, 0, 'fresh',
                   'confirmed', repeat('c', 64)
            FROM player_profile_versions WHERE normalized_tag = %s
            ORDER BY id DESC LIMIT 1
            """,
            (board, position, total, reset, tag),
        )
    return board


def seed_parity(site: Site) -> None:
    for player in PARITY["players"]:
        for saved in player["days"]:
            seed_day(
                site,
                player["tag"],
                datetime.fromisoformat(saved["start"]),
                saved["battles"],
                reasons=saved["partial_reasons"],
            )


def crew_of_four(site: Site) -> str:
    """Akira holds #2PP and #9QQ, Bea holds #8PY and #PPP, which has left
    Legend League. #9QQ has not played a Legend day this Season."""
    site.account("akira")
    site.account("bea")
    site.link("akira", "#2PP")
    site.link("akira", "#9QQ")
    site.link("bea", "#8PY")
    site.link("bea", "#PPP", "not_in_legend")
    crew_id = site.make("akira", ["#2PP", "#9QQ"])
    code = site.invite("akira", crew_id)
    assert site.accept("bea", code, ["#8PY"]).status_code == 200
    # Joined while it was still in Legend League.
    site.sql(
        """
        INSERT INTO crew_players (crew_id, account_id, player_id)
        SELECT crew.id, %s, player.id FROM crews AS crew, players AS player
        WHERE crew.public_id = %s AND player.normalized_tag = '#PPP'
        """,
        (site.ids["bea"], crew_id),
    )
    seed_day(site, "#9QQ", TODAY, reasons=["not_enrolled"])
    return crew_id


def boards(site: Site, crew_id: str, period: str, now: datetime = NOW, who="akira"):
    return api_crew_boards.get_crew_boards(
        site.api, site.ids[who], crew_id, period=period, now=now
    )


def rows(board: dict[str, Any], *fields: str) -> list[tuple[Any, ...]]:
    return [(row["tag"], *(row[field] for field in fields)) for row in board["rows"]]


def missing(board: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted((row["tag"], row["reason"]) for row in board["missing"])


def test_averages_match_the_player_page_on_hand_worked_days(site: Site) -> None:
    crew_id = crew_of_four(site)
    seed_parity(site)
    for period in ("season", "week"):
        result = boards(site, crew_id, period)
        expected = {
            player["tag"]: player["expected"][period] for player in PARITY["players"]
        }
        for board, side in (
            ("attackers", "attack"),
            ("best_defenders", "defense"),
            ("worst_defenders", "defense"),
        ):
            assert {
                row["tag"]: {key: row[key] for key in ("total", "days", "battles")}
                for row in result["boards"][board]["rows"]
            } == {tag: values[side] for tag, values in expected.items()}, (period, board)
        # 105 over 2 days beats 240 over 5; losing 10 over 2 days is the best
        # defense and 92 over 5 the worst.
        assert rows(result["boards"]["attackers"], "you") == [
            ("#8PY", False), ("#2PP", True),
        ]
        assert rows(result["boards"]["best_defenders"]) == [("#8PY",), ("#2PP",)]
        assert rows(result["boards"]["worst_defenders"]) == [("#2PP",), ("#8PY",)]
        assert missing(result["boards"]["attackers"]) == [
            ("#9QQ", "no_battles_this_season"),
            ("#PPP", "not_in_legend"),
        ]
    season = boards(site, crew_id, "season")
    assert (season["season_id"], season["day_number"], len(season["window_days"])) == (
        "1783918800", 25, 24,
    )
    assert boards(site, crew_id, "week")["window_days"] == [
        (TODAY - DAY * days).isoformat() for days in range(7, 0, -1)
    ]


def test_today_and_streaks(site: Site) -> None:
    crew_id = crew_of_four(site)
    seed_parity(site)
    today = boards(site, crew_id, "today")
    assert today["window_days"] == [TODAY.isoformat()]
    assert rows(today["boards"]["attackers"], "total", "days", "battles") == [
        ("#2PP", 40, 1, 1),
    ]
    assert rows(today["boards"]["worst_defenders"], "total") == [("#2PP", 16)]
    assert missing(today["boards"]["attackers"]) == [
        ("#8PY", "no_attacks_today"),
        ("#9QQ", "no_battles_this_season"),
        ("#PPP", "not_in_legend"),
    ]
    assert missing(today["boards"]["best_defenders"])[0] == ("#8PY", "no_defenses_today")
    # #2PP's attacks in order are 3, 2, 3, 3, 1, 3, 3 stars, then today's 3:
    # a run of three still going. The 2026-07-12 attack was last Season's.
    # The last seven days stop at today's Reset: 3, 1, 3, 3.
    streaks = {
        period: rows(boards(site, crew_id, period)["boards"]["streaks"], "best", "going", "attacks")
        for period in ("season", "week", "today")
    }
    assert streaks == {
        "season": [("#2PP", 3, True, 8), ("#8PY", 2, False, 3)],
        "week": [("#2PP", 2, True, 4), ("#8PY", 2, False, 3)],
        "today": [("#2PP", 1, True, 1)],
    }


def test_live_and_top_players(site: Site) -> None:
    crew_id = crew_of_four(site)
    site.sql(
        """
        UPDATE player_profile_versions SET trophies = 5300
        WHERE normalized_tag = '#2PP'
        """
    )
    # #LQ2 is on the board but not in the crew.
    site.account("cyrus")
    site.link("cyrus", "#LQ2")
    # Today's Reset board, before #2PP's automatic defense loss, in its own
    # order for the tie. Only the board now published counts, and only
    # today's Reset.
    seed_reset_board(site, TODAY, {"#2PP": 5100, "#8PY": 5000}, "superseded")
    seed_reset_board(site, TODAY, {"#8PY": 5400, "#2PP": 5400, "#LQ2": 5600})
    seed_reset_board(site, TODAY - DAY, {"#9QQ": 5200})
    result = boards(site, crew_id, "season", who="bea")
    live = result["boards"]["live"]
    assert rows(live, "trophies", "you") == [
        ("#2PP", 5300, False), ("#8PY", 5000, True), ("#9QQ", 5000, False),
    ]
    assert missing(live) == [("#PPP", "not_in_legend")]
    top = result["boards"]["top"]
    assert rows(top, "trophies", "you") == [("#8PY", 5400, True), ("#2PP", 5400, False)]
    assert missing(top) == [
        ("#9QQ", "no_battles_this_season"), ("#PPP", "not_in_legend"),
    ]


def test_top_players_reads_one_view_and_skips_withdrawn_boards(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    crew_id = crew_of_four(site)
    site.link("bea", "#LQ2")
    board = seed_reset_board(site, TODAY, {"#LQ2": 5600, "#2PP": 5400})
    # #LQ2 joins while the boards are loading, after the crew list is read.
    read_players = api_crew_boards.api_crews._players

    def join_midway(*args: Any) -> Any:
        places = read_players(*args)
        site.sql(
            """
            INSERT INTO crew_players (crew_id, account_id, player_id)
            SELECT crew.id, %s, player.id FROM crews AS crew, players AS player
            WHERE crew.public_id = %s AND player.normalized_tag = '#LQ2'
            """,
            (site.ids["bea"], crew_id),
        )
        return places

    monkeypatch.setattr(api_crew_boards.api_crews, "_players", join_midway)
    result = boards(site, crew_id, "season")
    monkeypatch.undo()
    assert rows(result["boards"]["top"], "trophies") == [("#2PP", 5400)]
    top = boards(site, crew_id, "season")["boards"]["top"]
    assert rows(top, "trophies") == [("#LQ2", 5600), ("#2PP", 5400)]
    # A recalculation withdraws the board before its replacement is saved.
    site.sql(
        """
        INSERT INTO boundary_publication_generations (
            boundary_at, target_at, generation, ordering_rule_version,
            freshness_rule_version, expected_population_count,
            expected_population_hash, snapshot_state, snapshot_id
        ) VALUES (%s, %s, 1, 'test', 'test', 1, %s, 'superseded', %s)
        """,
        (TODAY, TODAY, "0" * 64, board),
    )
    top = boards(site, crew_id, "season")["boards"]["top"]
    assert top["rows"] == []
    assert ("#2PP", "no_reset_reading") in missing(top)


def test_a_new_season_starts_every_board_fresh(site: Site) -> None:
    crew_id = crew_of_four(site)
    seed_parity(site)
    day_one = datetime(2026, 8, 10, 12, tzinfo=UTC)
    last_day = day_one.replace(hour=5) - DAY
    seed_day(
        site, "#8PY", last_day,
        [battle("offense", "old", last_day + timedelta(hours=3), 3, 40)],
    )
    seed_reset_board(site, day_one.replace(hour=5), {"#8PY": 5440})
    seed_day(
        site, "#8PY", day_one.replace(hour=5),
        [battle("offense", "new", day_one - timedelta(hours=1), 3, 40)],
    )
    # #2PP has only defended so far this Season.
    seed_day(
        site, "#2PP", day_one.replace(hour=5),
        [battle("defense", "held", day_one - timedelta(hours=2), 1, -10)],
    )
    # Day 1: no finished day yet, and nobody has a reading for the new
    # Season on the Live Leaderboard.
    result = boards(site, crew_id, "season", day_one)
    assert (result["day_number"], result["window_days"]) == (1, [])
    for name in ("live", "top", "attackers", "best_defenders", "worst_defenders"):
        assert result["boards"][name]["rows"] == [], name
    assert missing(result["boards"]["live"]) == [
        ("#2PP", "not_on_live_board"),
        ("#8PY", "not_on_live_board"),
        ("#9QQ", "no_battles_this_season"),
        ("#PPP", "not_in_legend"),
    ]
    assert missing(result["boards"]["attackers"])[:2] == [
        ("#2PP", "no_days_in_period"), ("#8PY", "no_days_in_period"),
    ]
    assert missing(result["boards"]["top"])[:2] == [
        ("#2PP", "no_reset_reading"), ("#8PY", "no_reset_reading"),
    ]
    # Today counts for the Season's streaks, but the last seven days have
    # none finished yet.
    streaks = boards(site, crew_id, "season", day_one)["boards"]["streaks"]
    assert rows(streaks, "best", "attacks") == [("#8PY", 1, 1)]
    assert missing(streaks)[0] == ("#2PP", "no_attacks_in_period")
    streaks = boards(site, crew_id, "week", day_one)["boards"]["streaks"]
    assert streaks["rows"] == []
    assert missing(streaks)[:2] == [
        ("#2PP", "no_attacks_in_period"), ("#8PY", "no_attacks_in_period"),
    ]

    # Day 2: the week so far is Day 1 alone, and last Season's days never
    # count, so the week and the Season agree.
    day_two = day_one + DAY
    for period in ("season", "week"):
        result = boards(site, crew_id, period, day_two)
        assert result["window_days"] == [day_one.replace(hour=5).isoformat()]
        assert rows(result["boards"]["attackers"], "total", "days") == [
            ("#8PY", 40, 1), ("#2PP", 0, 1),
        ]
        assert rows(result["boards"]["streaks"], "best", "attacks") == [("#8PY", 1, 1)]


def test_a_full_crew_season_board_reads_quickly(site: Site) -> None:
    """100 accounts on Day 28, each with 28 saved days of 8 attacks and 7
    defenses: about 3 KB of battles per day, as production stored on 8 Oct.
    Measured on 10 Oct at about 0.55 s, mostly decoding and checking 42,000
    battles; the owner accepted that for the largest crews late in a Season."""
    now = datetime(2026, 8, 9, 20, tzinfo=UTC)
    season_start = ranked_day_for(now).season_start
    site.account("akira")
    tags = [f"#P{index:02d}".translate(str.maketrans("0123456789", "QRUVY289LC"))
            for index in range(100)]
    for tag in tags:
        site.link("akira", tag)
    crew_id = site.make("akira", tags, size=100)
    with site.seed.pool.connection() as connection:
        players = dict(
            connection.execute(
                "SELECT normalized_tag, id FROM players WHERE normalized_tag = ANY(%s)",
                (tags,),
            ).fetchall()
        )
        logs = []
        for tag in tags:
            for number in range(28):
                start = season_start + number * DAY
                day_battles = [
                    battle(
                        "offense" if slot < 8 else "defense",
                        f"{tag}-{number}-{slot}",
                        start + timedelta(minutes=30 + 70 * slot),
                        slot % 4,
                        (16 + slot) * (1 if slot < 8 else -1),
                    )
                    | {"army_share_code": "u1x15-2x3s1x9-3x2"}
                    for slot in range(15)
                ]
                logs.append(
                    (players[tag], start, start + DAY, Jsonb(day_battles))
                )
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, ranked_day_end, version, state,
                    coverage, battles
                ) VALUES (%s, %s, %s, 2, 'Complete', 'complete', %s)
                """,
                logs,
            )
        connection.execute("ANALYZE api_player_daily_logs")
    timings = []
    for _ in range(3):
        started = time.perf_counter()
        result = boards(site, crew_id, "season", now)
        timings.append(time.perf_counter() - started)
    assert len(result["boards"]["attackers"]["rows"]) == 100
    assert result["boards"]["attackers"]["rows"][0]["days"] == 27
    assert min(timings) < 1, timings
