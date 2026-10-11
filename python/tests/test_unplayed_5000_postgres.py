"""A Legend I player at 5,000 who has not played a Legend battle this Season
is tracked and ranked at 5,000, whatever Season their profile names.

On 11 October 2026, 1,374 tracked players' Reset profiles said Legend I,
5,000 trophies and Season ID 0, and none had a Legend battle; each was
rejected and left off both boards. These tests run saved profile and battle
log responses through the worker's Reset checks, Legend day results and the
frozen Daily board, then read the live board.
"""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
from domain_test_support import domain_database, repair_season, store_observation
from test_first_battle_log_postgres import (
    DAY_1,
    DAY_2,
    LOSS,
    WIN,
    _log,
    _new_season_profile,
)
from test_reconciliation_postgres import _processor, _profile
from test_snapshot_publication_postgres import _process_snapshot_and_analytics

from clashlens import (
    api_player_lookup,
    boundary_manifest,
    domain_repair,
    reset_baselines,
)
from clashlens.api_db import ApiDatabase
from clashlens.api_leaderboard import _read_live_board
from clashlens.api_players import get_player_page
from clashlens.db import Database
from clashlens.domain import ranked_day_for

UNPLAYED, STARTER, ORDINARY = "#2PP", "#8QQ", "#9YY"
DAY_0 = DAY_1 - timedelta(days=1)  # The previous Season's last day.
DAY_3, DAY_4 = DAY_1 + timedelta(days=2), DAY_1 + timedelta(days=3)
# Reset sweeps read profiles from 05:03:40.
READ_AFTER = timedelta(minutes=4)


def _season_zero(tag: str) -> bytes:
    payload = json.loads(_new_season_profile(5000, tag))
    payload["currentLeagueSeasonId"] = 0
    return json.dumps(payload).encode()


def _sweep(connection_info, archive_server, boundary, readings) -> list[int]:
    """Save one Reset sweep: each tag's profile and battle log at the Reset."""
    jobs, ids = [], {}
    for tag, (profile, log) in readings.items():
        for endpoint, body in (("profile", profile), ("battle_log", log)):
            ids[tag, endpoint], job = store_observation(
                connection_info, archive_server,
                occurrence_key=f"{tag}-{boundary.isoformat()}-{endpoint}",
                endpoint=endpoint, body=body, observed_at=boundary + READ_AFTER,
                normalized_tag=tag,
            )
            jobs.append(job)
    with psycopg.connect(connection_info) as connection:
        players = [
            connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
            ).fetchone()[0]
            for tag in readings
        ]
        sweep = connection.execute(
            """
            INSERT INTO collector_reset_sweeps (boundary_at, member_ids, membership_captured_at)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (boundary, players, boundary),
        ).fetchone()[0]
        for tag, player in zip(readings, players, strict=True):
            connection.execute(
                """
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                    coalescing_key, status, profile_status, battle_log_status,
                    profile_observation_id, battle_log_observation_id, completed_at
                ) VALUES ('reset_baseline', 'reset', 'player', %s, %s, %s, %s,
                          %s, 'complete', 'observed', 'observed', %s, %s, %s)
                """,
                (player, tag, sweep, boundary, f"reset-{tag}-{boundary}",
                 ids[tag, "profile"], ids[tag, "battle_log"], boundary),
            )
    return jobs


def _run(connection_info, archive_server, jobs) -> None:
    """Process the saved responses, then every queued day and board."""
    database, processor = _processor(connection_info, archive_server)
    try:
        for job in jobs:
            assert processor.process_job(job, owner=f"job-{job}") is not None
        while True:
            with database.pool.connection() as connection:
                pending = connection.execute(
                    """
                    SELECT id, work_type FROM python_processing_jobs
                    WHERE status = 'pending'
                      AND work_type IN ('reconcile_ranked_day', 'build_snapshot')
                    ORDER BY work_type = 'build_snapshot', id LIMIT 1
                    """
                ).fetchone()
            if pending is None:
                return
            if pending[1] == "build_snapshot":
                _process_snapshot_and_analytics(
                    connection_info, database, processor, int(pending[0]),
                    owner_prefix=f"board-{pending[0]}",
                )
            else:
                processor.process_job(int(pending[0]), owner="day")
    finally:
        database.close()


def _days(connection) -> dict:
    return {
        (row[0], row[1]): row[2:]
        for row in connection.execute(
            """
            SELECT DISTINCT ON (player.normalized_tag, day.ranked_day_start)
                   player.normalized_tag, day.ranked_day_start, day.attack_count,
                   day.defense_count, day.start_trophies, day.next_start_trophies,
                   day.automatic_defense_loss
            FROM ranked_day_versions AS day
            JOIN players AS player ON player.id = day.player_id
            ORDER BY player.normalized_tag, day.ranked_day_start, day.version DESC
            """
        )
    }


def _boards(connection) -> dict:
    boards: dict = {}
    for boundary, tag, trophies in connection.execute(
        """
        SELECT snapshot.boundary_at, player.normalized_tag, entry.trophies
        FROM (
            SELECT DISTINCT ON (boundary_at) id, boundary_at
            FROM leaderboard_snapshots WHERE snapshot_kind = 'frozen'
            ORDER BY boundary_at, version DESC, id DESC
        ) AS snapshot
        JOIN leaderboard_snapshot_entries AS entry ON entry.snapshot_id = snapshot.id
        JOIN players AS player ON player.id = entry.player_id
        ORDER BY snapshot.boundary_at, entry.position
        """
    ):
        boards.setdefault(boundary, []).append((tag, trophies))
    return boards


def test_unplayed_players_rank_at_5000_until_their_first_battle(
    database_url: str, archive_server
) -> None:
    # The starter signs up and wins one attack at 10:00 on Day 2; the
    # ordinary player attacks and defends once on Day 1, at 5,000 on Day 0.
    starter_battle = (DAY_2 + timedelta(hours=5), True)
    ordinary_battles = (
        (DAY_1 + timedelta(hours=2), True), (DAY_1 + timedelta(hours=3), False),
    )
    ordinary_end = 5000 + WIN - LOSS
    resets = {
        DAY_0: {
            UNPLAYED: (_season_zero(UNPLAYED), _log()),
            STARTER: (_season_zero(STARTER), _log()),
            ORDINARY: (_profile(5000, ORDINARY), _log()),
        },
        DAY_1: {
            UNPLAYED: (_season_zero(UNPLAYED), _log()),
            STARTER: (_season_zero(STARTER), _log()),
            ORDINARY: (_new_season_profile(5000, ORDINARY), _log()),
        },
        DAY_2: {
            UNPLAYED: (_season_zero(UNPLAYED), _log()),
            STARTER: (_season_zero(STARTER), _log()),
            ORDINARY: (_new_season_profile(ordinary_end, ORDINARY), _log(*ordinary_battles)),
        },
        DAY_3: {
            UNPLAYED: (_season_zero(UNPLAYED), _log()),
            STARTER: (_new_season_profile(5000 + WIN, STARTER), _log(starter_battle)),
            ORDINARY: (_new_season_profile(ordinary_end, ORDINARY), _log(*ordinary_battles)),
        },
        DAY_4: {
            UNPLAYED: (_season_zero(UNPLAYED), _log()),
            STARTER: (_new_season_profile(5000 + WIN, STARTER), _log(starter_battle)),
            ORDINARY: (_new_season_profile(ordinary_end, ORDINARY), _log(*ordinary_battles)),
        },
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        for boundary, readings in resets.items():
            jobs = _sweep(connection_info, archive_server, boundary, readings)
            if boundary == DAY_3:
                # The starter signed up at 05:19 on Day 2, before its battle.
                jobs.append(store_observation(
                    connection_info, archive_server, occurrence_key="sign-up",
                    endpoint="profile", body=_new_season_profile(5000, STARTER),
                    observed_at=DAY_2 + timedelta(minutes=19), normalized_tag=STARTER,
                )[1])
            _run(connection_info, archive_server, jobs)
        with psycopg.connect(connection_info) as connection:
            days, boards = _days(connection), _boards(connection)
            rejected = connection.execute(
                """
                SELECT player.normalized_tag, evidence.boundary_at, evidence.failure_reasons
                FROM reset_baseline_evidence AS evidence
                JOIN players AS player ON player.id = evidence.player_id
                WHERE failure_reasons ? 'profile_invalid'
                """
            ).fetchall()
            live = _read_live_board(connection, DAY_4 + timedelta(hours=1))
        api = ApiDatabase(connection_info)
        try:
            pages = {
                tag: get_player_page(
                    api, tag, now=DAY_4 + timedelta(hours=1), freshness_seconds=900
                )
                for tag in (UNPLAYED, STARTER)
            }
        finally:
            api.close()

    # No Reset reading was rejected.
    assert rejected == []
    # (a) Never played: every Legend day of the Season is 0 attacks and 0
    # defenses from 5,000 to 5,000, and no automatic defense loss applies.
    for day in (DAY_1, DAY_2, DAY_3):
        assert days[UNPLAYED, day] == (0, 0, 5000, 5000, None)
    # (b) Started on Day 2: Day 1 is the same; from Day 2 the day is the
    # ordinary calculation from 5,000.
    assert days[STARTER, DAY_1] == (0, 0, 5000, 5000, None)
    assert days[STARTER, DAY_2] == (1, 0, 5000, 5000 + WIN, None)
    assert days[STARTER, DAY_3] == (0, 0, 5000 + WIN, 5000 + WIN, None)
    # (c) The ordinary player's days are their own battles.
    assert days[ORDINARY, DAY_1] == (1, 1, 5000, ordinary_end, 0)
    assert days[ORDINARY, DAY_2][2:4] == (ordinary_end, ordinary_end)
    # Each Daily board ranks the unplayed at 5,000, the starter from its
    # battles on, and the ordinary player at their own total.
    # Neither 5,000 player has attacked, so the tag hash orders them.
    assert boards[DAY_2] == [(ORDINARY, ordinary_end), (STARTER, 5000), (UNPLAYED, 5000)]
    assert boards[DAY_3] == [(STARTER, 5000 + WIN), (ORDINARY, ordinary_end), (UNPLAYED, 5000)]
    assert boards[DAY_4] == [(STARTER, 5000 + WIN), (ORDINARY, ordinary_end), (UNPLAYED, 5000)]
    # The previous Season's last board, its final ranking, ranks them at
    # 5,000 too. No one attacked, so the tag hash orders all three.
    assert boards[DAY_1] == [(STARTER, 5000), (UNPLAYED, 5000), (ORDINARY, 5000)]
    assert [(row[0], row[2]) for row in live.rows] == [
        (STARTER, 5000 + WIN), (ORDINARY, ordinary_end), (UNPLAYED, 5000),
    ]
    # Their pages show them at 5,000 with each day before their first battle
    # complete: 0 attacks and 0 defenses from 5,000, a change of 0.
    assert (pages[UNPLAYED]["trophies"], pages[UNPLAYED]["season_reset_pending"]) == (
        5000, False,
    )
    shown = {
        (tag, day["ranked_day_start"]): (
            day["attack_count"], day["defense_count"], day["start_trophies"],
            day["net_trophy_change"], day["state"], day["partial_reasons"],
        )
        for tag, page in pages.items() for day in page["screen_ready"]["days"]
    }
    for tag, day in ((UNPLAYED, DAY_1), (UNPLAYED, DAY_2), (UNPLAYED, DAY_3), (STARTER, DAY_1)):
        assert shown[tag, day.isoformat()] == (0, 0, 5000, 0, "Complete", [])


def test_season_zero_after_a_higher_season_end_starts_at_5000(
    database_url: str, archive_server
) -> None:
    # Legend I at 6,400 on the previous Season's last day, then Season 0 at
    # 5,000 with no battle from the opening Reset on.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        for boundary, profile in (
            (DAY_0, _profile(6400, UNPLAYED)),
            (DAY_1, _season_zero(UNPLAYED)),
            (DAY_2, _season_zero(UNPLAYED)),
            (DAY_3, _season_zero(UNPLAYED)),
        ):
            _run(connection_info, archive_server, _sweep(
                connection_info, archive_server, boundary, {UNPLAYED: (profile, _log())},
            ))
        with psycopg.connect(connection_info) as connection:
            days = _days(connection)

    for day in (DAY_1, DAY_2):
        assert days[UNPLAYED, day][:4] == (0, 0, 5000, 5000)


def test_the_season_repair_brings_days_and_boards_saved_before_the_rule(
    database_url: str, archive_server, monkeypatch
) -> None:
    season = ranked_day_for(DAY_1).official_season_id
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # Saved before the rule: the readings were rejected and the boards
        # left the player out.
        monkeypatch.setattr(reset_baselines, "_unplayed", lambda *args: False)
        monkeypatch.setattr(boundary_manifest, "unplayed_profiles", lambda *args: {})
        for boundary in (DAY_1, DAY_2, DAY_3):
            _run(connection_info, archive_server, _sweep(
                connection_info, archive_server, boundary, {
                    UNPLAYED: (_season_zero(UNPLAYED), _log()),
                    ORDINARY: (_new_season_profile(5000, ORDINARY), _log()),
                },
            ))
        with psycopg.connect(connection_info) as connection:
            assert _days(connection)[UNPLAYED, DAY_1][2:4] == (None, None)
            assert _boards(connection)[DAY_3] == [(ORDINARY, 5000)]
        monkeypatch.undo()

        _, queued = repair_season(connection_info, season)
        _run(connection_info, archive_server, [])
        database = Database(connection_info)
        try:
            boards = domain_repair.season_repair(database, season, "queue", max_jobs=100)
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            days = _days(connection)
            corrections = connection.execute(
                "SELECT boundary_at FROM boundary_publication_corrections ORDER BY 1"
            ).fetchall()

    assert queued["phase"] == "days"
    for day in (DAY_1, DAY_2):
        assert days[UNPLAYED, day] == (0, 0, 5000, 5000, None)
    # Both boards that left the player out are queued for a rebuild.
    assert boards["phase"] == "boards"
    assert corrections == [(DAY_2,), (DAY_3,)]


def test_a_newer_season_zero_profile_replaces_an_accepted_one_at_5000(
    database_url: str, archive_server
) -> None:
    # Accepted at 5,000 naming the Season at the Day 1 Reset, then renamed
    # in a profile naming Season 0, still at 5,000 and with no battle.
    renamed = json.loads(_season_zero(UNPLAYED))
    renamed["name"] = "Renamed"
    renamed = json.dumps(renamed).encode()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        for boundary, profile in (
            (DAY_1, _new_season_profile(5000, UNPLAYED)),
            (DAY_2, renamed),
            (DAY_3, renamed),
        ):
            _run(connection_info, archive_server, _sweep(
                connection_info, archive_server, boundary, {UNPLAYED: (profile, _log())},
            ))
        now = DAY_3 + timedelta(hours=1)
        with psycopg.connect(connection_info) as connection:
            lookup = api_player_lookup._lookup(connection, UNPLAYED, now)
            live = _read_live_board(connection, now)
            board = connection.execute(
                """
                SELECT entry.trophies, entry.trophy_observed_at
                FROM leaderboard_snapshots AS snapshot
                JOIN leaderboard_snapshot_entries AS entry
                  ON entry.snapshot_id = snapshot.id
                WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.boundary_at = %s
                ORDER BY snapshot.version DESC, snapshot.id DESC LIMIT 1
                """,
                (DAY_3,),
            ).fetchone()
        api = ApiDatabase(connection_info)
        try:
            page = get_player_page(api, UNPLAYED, now=now, freshness_seconds=900)
        finally:
            api.close()

    assert "reason" not in lookup
    assert (page["name"], page["trophies"]) == ("Renamed", 5000)
    assert [(row[0], row[1], row[2]) for row in live.rows] == [
        (UNPLAYED, "Renamed", 5000),
    ]
    # The Day 3 board ranks them from the renamed profile read after the
    # Day 2 Reset.
    assert board == (5000, DAY_2 + READ_AFTER)


def test_every_day_before_a_player_first_seen_unplayed_is_a_0_0_day_at_5000(
    database_url: str, archive_server
) -> None:
    # First seen at the Day 8 Reset at 5,000 with no battle: one already
    # naming the Season, one not signed up who signs up by the Day 10 Reset,
    # and one never signed up.
    signed, joins, never = "#2PP", "#8QQ", "#9YY"
    day_8, day_10 = (DAY_1 + timedelta(days=n) for n in (7, 9))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        for boundary, joined in ((day_8, _season_zero(joins)),
                                 (day_10, _new_season_profile(5000, joins))):
            _run(connection_info, archive_server, _sweep(
                connection_info, archive_server, boundary, {
                    signed: (_new_season_profile(5000, signed), _log()),
                    joins: (joined, _log()),
                    never: (_season_zero(never), _log()),
                },
            ))
        with psycopg.connect(connection_info) as connection:
            days = _days(connection)
            not_enrolled = connection.execute(
                "SELECT count(*) FROM ranked_day_versions"
                " WHERE failure_reasons ? 'not_enrolled'"
            ).fetchone()[0]

    for tag in (signed, joins, never):
        for day in range(9):
            assert days[tag, DAY_1 + timedelta(days=day)][:4] == (0, 0, 5000, 5000)
    assert not_enrolled == 0


def test_days_before_first_seen_are_recalculated_once_the_first_battle_log_lands(
    database_url: str, archive_server
) -> None:
    # Each Day 8 profile and its days are processed before the first battle
    # log, which then proves those days had no battle: read at the same
    # Reset, or first read a day later.
    day_8 = DAY_1 + timedelta(days=7)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        profile_job, log_job = _sweep(connection_info, archive_server, day_8, {
            UNPLAYED: (_season_zero(UNPLAYED), _log()),
        })
        late_profile, late_log = (
            store_observation(
                connection_info, archive_server, occurrence_key=f"late-{endpoint}",
                endpoint=endpoint, body=body, observed_at=at, normalized_tag=STARTER,
            )[1]
            for endpoint, body, at in (
                ("profile", _season_zero(STARTER), day_8 + READ_AFTER),
                ("battle_log", _log(), day_8 + timedelta(days=1) + READ_AFTER),
            )
        )
        _run(connection_info, archive_server, [profile_job, late_profile])
        _run(connection_info, archive_server, [log_job, late_log])
        with psycopg.connect(connection_info) as connection:
            days = {
                (row[0], row[1]): row[2:]
                for row in connection.execute(
                    """
                    SELECT DISTINCT ON (player.normalized_tag, day.ranked_day_start)
                           player.normalized_tag, day.ranked_day_start,
                           day.attack_count, day.defense_count, day.start_trophies,
                           day.next_start_trophies, day.net_trophy_change
                    FROM ranked_day_versions AS day
                    JOIN players AS player ON player.id = day.player_id
                    ORDER BY player.normalized_tag, day.ranked_day_start,
                             day.version DESC
                    """
                )
            }

    for tag in (UNPLAYED, STARTER):
        for day in range(7):
            assert days[tag, DAY_1 + timedelta(days=day)] == (0, 0, 5000, 5000, 0)


def test_a_malformed_season_at_5000_stays_rejected(
    database_url: str, archive_server
) -> None:
    # Legend I at 5,000 with no battle, but the Season ID reads "00".
    malformed = json.loads(_new_season_profile(5000, UNPLAYED))
    malformed["currentLeagueSeasonId"] = "00"
    malformed = json.dumps(malformed).encode()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        for boundary in (DAY_2, DAY_3):
            _run(connection_info, archive_server, _sweep(
                connection_info, archive_server, boundary, {
                    UNPLAYED: (malformed, _log()),
                    ORDINARY: (_new_season_profile(5000, ORDINARY), _log()),
                },
            ))
        now = DAY_3 + timedelta(hours=1)
        with psycopg.connect(connection_info) as connection:
            rejected = connection.execute(
                """
                SELECT count(*) FROM reset_baseline_evidence AS evidence
                JOIN players AS player ON player.id = evidence.player_id
                WHERE player.normalized_tag = %s
                  AND evidence.failure_reasons ? 'profile_invalid'
                """,
                (UNPLAYED,),
            ).fetchone()[0]
            live = _read_live_board(connection, now)
            boards = _boards(connection)
        api = ApiDatabase(connection_info)
        try:
            page = get_player_page(api, UNPLAYED, now=now, freshness_seconds=900)
        finally:
            api.close()

    assert rejected == 2
    assert [row[0] for row in live.rows] == [ORDINARY]
    assert boards[DAY_3] == [(ORDINARY, 5000)]
    assert page is None
