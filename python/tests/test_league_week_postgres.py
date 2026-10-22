"""End-to-end: the weekly league record decided from saved game data.

The collector's saved responses for a 12 October 2026-like Monday go
through the real worker into PostgreSQL; then the worker's league week thread
decides each player's weeks. Stage 1 records the weeks and changes no day or
board, so these tests check the records. See docs/testing.md.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database, text
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_reset_scenarios_postgres import SEASON, _story, attacks, day, defenses

from clashlens import league_week, queue_refresh, worker
from clashlens.collector_db import CollectorDatabase
from clashlens.db import Database
from clashlens.stage_metrics import StageMetrics

W1, W2 = SEASON, SEASON + timedelta(days=7)


def test_every_evidence_table_marks_the_weeks_it_changes(database_url: str) -> None:
    """Evidence nobody marks is never decided: each table in
    ``EVIDENCE_TABLES`` needs a trigger writing ``league_week_dirty``, and
    no other table has one."""
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        tables = {
            text(table) for (table,) in connection.execute(
                """
                SELECT DISTINCT trigger.tgrelid::regclass::text
                FROM pg_trigger AS trigger
                JOIN pg_proc AS function ON function.oid = trigger.tgfoid
                WHERE NOT trigger.tgisinternal
                  AND function.prosrc LIKE '%league_week_dirty%'
                """
            ).fetchall()
        }
    assert tables == set(league_week.EVIDENCE_TABLES)


def test_a_league_history_row_marks_the_week_it_speaks_for(database_url: str) -> None:
    """A Legend I row labelled with the Monday Reset ending its Season marks
    the Season's last week; a Legend II row labelled with the Monday Reset
    starting its week marks that week (migration 0127). Until 22 October
    2026 a Legend II row marked the week before its own."""
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        player = connection.execute(
            "INSERT INTO players (normalized_tag) VALUES ('#2PP') RETURNING id").fetchone()[0]
        observation = connection.execute(
            """
            INSERT INTO collector_observations (
                occurrence_key, scope, player_id, normalized_tag, endpoint,
                request_started_at, response_completed_at, http_status, response_hash,
                collector_version, key_label, evidence_headers, request_method,
                request_path, request_query, paging_envelope_state, source_adapter_version
            ) VALUES ('history', 'player', %s, '#2PP', 'league_history', %s, %s, 200,
                      repeat('a', 64), 'test', 'normal-test', '{}'::jsonb, 'GET',
                      '/v1/players/%%232PP/leaguehistory', '', 'not_applicable',
                      'league-history-v1')
            RETURNING id
            """,
            (player, W2, W2),
        ).fetchone()[0]
        payload = connection.execute(
            """
            INSERT INTO parsed_source_payloads (
                endpoint, response_hash, parser_version, schema_version,
                parse_outcome, parsed_json
            ) VALUES ('league_history', repeat('a', 64), 'supercell-league-history-parser-v1',
                      'league-history-schema-v1', 'valid', '{}'::jsonb)
            RETURNING id
            """
        ).fetchone()[0]
        marked = {}
        for tier in (105000036, 105000035):
            connection.execute("DELETE FROM league_week_dirty")
            connection.execute(
                """
                INSERT INTO player_league_history_entries (
                    player_id, league_season_id, observed_at, observation_id,
                    parsed_payload_id, league_tier_id, source_json
                ) VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb)
                ON CONFLICT (player_id, league_season_id) DO UPDATE
                SET league_tier_id = EXCLUDED.league_tier_id
                """,
                (player, str(int(W2.timestamp())), W2, observation, payload, tier),
            )
            marked[tier] = [row[0] for row in connection.execute(
                "SELECT week_start FROM league_week_dirty").fetchall()]

    assert marked == {105000036: [W1], 105000035: [W2]}


def test_a_battle_moved_to_another_day_marks_both_weeks(database_url: str) -> None:
    """A repair moving a battle to another day (as migration 0057 did) marks
    the week it left and the week it joined, for both players; a second
    report of the battle that moves nothing marks nothing; deleting it marks
    its week."""
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        first, second = (connection.execute(
            "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (tag,)
        ).fetchone()[0] for tag in ("#2PP", "#8PP"))
        sunday, monday = W2 - timedelta(days=1), W2

        def marks() -> set[tuple[int, datetime]]:
            return {(player, week) for player, week in connection.execute(
                "DELETE FROM league_week_dirty RETURNING player_id, week_start").fetchall()}

        battle = connection.execute(
            """
            INSERT INTO legend_battles (ranked_day_start, attacker_player_id, defender_player_id)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (sunday, first, second),
        ).fetchone()[0]
        inserted = marks()
        connection.execute(
            "UPDATE legend_battles SET disagreement_state = 'agreed' WHERE id = %s", (battle,))
        agreed = marks()
        connection.execute(
            "UPDATE legend_battles SET ranked_day_start = %s WHERE id = %s", (monday, battle))
        moved = marks()
        connection.execute("DELETE FROM legend_battles WHERE id = %s", (battle,))
        deleted = marks()

    assert inserted == {(first, W1), (second, W1)}
    assert agreed == set()
    assert moved == {(first, W1), (second, W1), (first, W2), (second, W2)}
    assert deleted == {(first, W2), (second, W2)}


STAYED, DROPPED, FIRST_SEEN, FAILED, GLITCH = "#2PP", "#8PP", "#9PP", "#PPP", "#YPP"
IDLE, PROMOTED, STALE = "#LPP", "#QPP", "#GPP"


def _decide(connection_info: str, monkeypatch) -> None:
    """The worker's league week thread, started by its maintenance timer and
    run as the worker's database login until no week is left to decide, on
    Wednesday 14 October 2026. It takes one mark a batch and one batch a
    call, so it must keep deciding on its own after the timer's one run."""
    monkeypatch.setattr(league_week, "_now", lambda: datetime(2026, 10, 14, 12, tzinfo=UTC))
    monkeypatch.setattr(league_week, "_retry_at", float("-inf"))
    monkeypatch.setattr(league_week, "IDLE_SECONDS", 0.05)
    monkeypatch.setattr(league_week, "BATCH", 1)
    monkeypatch.setattr(league_week, "TICK_SECONDS", 0.0)
    # The twice-daily re-check follows the real clock; it is not under test.
    monkeypatch.setattr(queue_refresh, "due_recheck_slot", lambda _connection, _now: None)
    options = conninfo_to_dict(connection_info).get("options", "")
    database = Database(make_conninfo(
        connection_info, options=f"{options} -c role=clashlens_python_worker"))
    maintenance = worker.TimedMaintenance(database, StageMetrics())
    try:
        maintenance.run_due()
        for _ in range(600):
            with psycopg.connect(connection_info) as connection:
                if not connection.execute("SELECT count(*) FROM league_week_dirty").fetchone()[0]:
                    return
            time.sleep(0.05)
        raise AssertionError("the league week thread left weeks undecided")
    finally:
        maintenance.close()
        database.close()


def _records(connection_info: str, tags: list[str]) -> dict[tuple[str, datetime], tuple]:
    """Each player's week: league, entry, strength, proof kind, when the
    proof was read or fought, when a pair's second answer was read, the
    evidence against, and the decision number."""
    with psycopg.connect(connection_info) as connection:
        return {
            (text(tag), week): (text(league), text(entry), strength, text(kind), proof_at,
                                second_at, text(against), number)
            for tag, week, league, entry, strength, kind, proof_at, second_at, against, number
            in connection.execute(
                """
                SELECT player.normalized_tag, week.week_start, week.league, week.entry,
                       week.strength, week.proof_kind, week.proof_at,
                       second.response_completed_at, week.against_kind, week.decision_number
                FROM player_league_weeks AS week
                JOIN players AS player ON player.id = week.player_id
                LEFT JOIN collector_observations AS second ON second.id = week.proof_second_id
                WHERE player.normalized_tag = ANY(%s)
                """,
                (tags,),
            ).fetchall()
        }


def test_the_12_october_2026_monday_in_the_weekly_league_record(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Five players around the 12 October 2026 Monday Reset, the Season's
    second week. STAYED, at 4,960 below 5,000, and GLITCH play Legend
    battles both weeks; GLITCH is read once in Legend II naming Season 0
    hours after the Reset, as 86 players were. DROPPED played Sunday, is
    still shown in Legend I by the Reset reading, then in Legend II naming
    Season 0 by the settlement check and once more after the display window.
    FAILED's Monday Reset request failed; its battles show later. FIRST_SEEN
    was promoted at the Reset and first read on Tuesday. IDLE and PROMOTED
    never battle: both are read twice in Legend II naming Season 0 on Sunday
    and once more at 05:16 on Monday, as #V02JGRJC was; PROMOTED was promoted
    at the Reset and is read in Legend I naming Season 0 at 06:30. STALE
    played Sunday, then is read in Legend I naming Season 0 at the Reset and
    at 09:20, then in Legend II at 21:04 and 05:00 Tuesday, as #GQ9QQ0VLJ was.

    Every Legend I week is Legend I and every week below stays below until
    evidence shows a change (the owner's rule of 14 October 2026): IDLE's
    lone Monday answer keeps it below; DROPPED is proven below only once its
    second Legend II answer is saved, and PROMOTED Legend I once its Legend I
    answer is. STALE's Legend I answers before its Legend II ones are the
    game's display catching up, so its week is below. Deciding again with
    nothing new changes nothing."""
    sunday, monday, tuesday = day(7), day(8), day(9)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       STAYED, DROPPED, FIRST_SEEN, FAILED, GLITCH, IDLE, PROMOTED, STALE)
        story.decides = False
        for tag in (STAYED, FAILED, GLITCH):
            story.players[tag].battles = attacks(sunday, 40, 40) + defenses(sunday, 40) + attacks(
                monday, 40) + defenses(monday, 30)
        story.players[DROPPED].battles = attacks(sunday, 40) + defenses(sunday, 40, 40)
        story.players[STALE].battles = attacks(sunday, 40) + defenses(sunday, 40)
        story.players[FIRST_SEEN].battles = attacks(monday + timedelta(hours=3), 40, 40)
        story.reset(sunday, {STAYED: (4920, SEASON), DROPPED: (4960, SEASON),
                             FAILED: (5100, SEASON), GLITCH: (5100, SEASON),
                             STALE: (5100, SEASON)})
        for tag in (IDLE, PROMOTED):
            story.players[tag].dropped = True
            for hours in (2, 3, 24 + 0.27):
                story.read(tag, sunday + timedelta(hours=hours), 4100)
        story.reset(monday, {STAYED: (5000, SEASON), DROPPED: (4920, SEASON),
                             FAILED: (None, SEASON), GLITCH: (5140, SEASON),
                             STALE: (5100, None)})
        story.players[DROPPED].dropped = True
        story.reads(monday + timedelta(minutes=20), {DROPPED: 4920})
        story.players[GLITCH].dropped = True
        story.read(GLITCH, monday + timedelta(hours=6), 5150)
        story.players[GLITCH].dropped = False
        story.reads(monday + timedelta(hours=7), {STAYED: 5010, FAILED: 5150, GLITCH: 5150})
        story.read(STALE, monday + timedelta(hours=4, minutes=20), 5100, None)
        story.players[STALE].dropped = True
        story.read(STALE, monday + timedelta(hours=16, minutes=4), 5100)
        story.read(STALE, tuesday, 5100)
        story.reads(tuesday + timedelta(hours=1, minutes=25), {FIRST_SEEN: 5080})
        story.run_worker()
        # The deploy's seed marked GLITCH's Season too (migration 0115).
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO league_week_dirty (player_id, week_start, source)"
                " SELECT id, %s, 'seed' FROM players WHERE normalized_tag = %s", (W1, GLITCH))
        _decide(connection_info, monkeypatch)
        before_pair = _records(connection_info, list(story.players))
        story.reads(monday + timedelta(hours=9), {DROPPED: 4920})
        story.players[PROMOTED].dropped = False
        story.read(PROMOTED, monday + timedelta(hours=1, minutes=30), 5000, None)
        story.run_worker()
        _decide(connection_info, monkeypatch)
        decided = _records(connection_info, list(story.players))
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO league_week_dirty (player_id, week_start, source)"
                " SELECT player_id, week_start, 'seed' FROM player_league_weeks")
        _decide(connection_info, monkeypatch)
        again = _records(connection_info, list(story.players))
        with psycopg.connect(connection_info) as connection:
            queued = {text(tag) for (tag,) in connection.execute(
                "SELECT DISTINCT player.normalized_tag FROM python_processing_jobs AS job"
                " JOIN players AS player"
                "   ON job.deduplication_key LIKE 'reconcile:league-week:' || player.id || ':%'"
            ).fetchall()} & set(story.players)

    started = ("legend_i", "season_start", 4, "battle", sunday, None, None, 1)
    battle = ("legend_i", "stayed", 4, "battle")
    sunday_pair = ("below_legend_i", None, 2, "unconfirmed_pair", sunday + timedelta(hours=2),
                   sunday + timedelta(hours=3), None, 1)
    still_below = ("below_legend_i", None, 1, "previous_week", W1, None, None, 1)
    assert decided == {
        (STAYED, W1): started,
        (STAYED, W2): (*battle, monday, None, None, 1),
        (GLITCH, W1): started,
        (GLITCH, W2): (*battle, monday, None, None, 1),
        (FAILED, W1): started,
        (FAILED, W2): (*battle, monday, None, None, 1),
        (DROPPED, W1): started,
        (DROPPED, W2): ("below_legend_i", None, 2, "unconfirmed_pair",
                        monday + timedelta(minutes=20), monday + timedelta(hours=9), None, 2),
        (FIRST_SEEN, W2): ("legend_i", "first_seen", 4, "battle", monday, None, None, 1),
        (IDLE, W1): sunday_pair,
        (IDLE, W2): still_below,
        (PROMOTED, W1): sunday_pair,
        (PROMOTED, W2): ("legend_i", "promoted", 2, "unconfirmed_answer",
                         monday + timedelta(hours=1, minutes=30), None, None, 2),
        (STALE, W1): started,
        (STALE, W2): ("below_legend_i", None, 2, "unconfirmed_pair",
                      monday + timedelta(hours=16, minutes=4), tuesday, None, 1),
    }
    # DROPPED's Reset reading still showed Legend I, inside the display
    # window; one Legend II answer alone proves no drop, so Legend I carries.
    assert before_pair[DROPPED, W2] == (
        "legend_i", "stayed", 1, "previous_week", W1, None, None, 1)
    assert before_pair[PROMOTED, W2] == still_below
    assert again == decided
    # Decisions queue the days they change. IDLE's weeks were never Legend
    # I, so it has none; the Season repair recalculates the seeded GLITCH's.
    assert queued == {STAYED, FAILED, DROPPED, FIRST_SEEN, PROMOTED, STALE}


def test_a_player_whose_decision_fails_is_set_aside_and_counted_as_failed_work(
    database_url: str, monkeypatch
) -> None:
    """One player whose decision fails must not stop the rest, which used to
    retry the same batch every minute with only a log line to show for it.
    BROKEN is set aside: their marks stay, marked failed, and the failed-work
    count the alert reads counts them, while FINE is decided. New evidence
    about BROKEN tries again. A decider that stops deciding counts too, once
    a mark has waited 30 minutes: no new week then has a league."""
    broken_tag, fine_tag = "#2PP", "#8PP"
    original = league_week.load_season
    broken: list[int] = []

    def load(connection, player_id, season_start, now):
        if player_id in broken:
            raise ValueError("unreadable evidence")
        return original(connection, player_id, season_start, now)

    monkeypatch.setattr(league_week, "load_season", load)
    monkeypatch.setattr(league_week, "_now", lambda: datetime(2026, 10, 14, 12, tzinfo=UTC))
    monkeypatch.setattr(league_week, "_retry_at", float("-inf"))
    with domain_database(database_url) as connection_info:
        options = conninfo_to_dict(connection_info).get("options", "")
        as_role = {role: make_conninfo(connection_info, options=f"{options} -c role={role}")
                   for role in ("clashlens_python_worker", "clashlens_collector")}

        def mark(*player_ids: int) -> None:
            with psycopg.connect(connection_info) as connection:
                for player_id in player_ids:
                    connection.execute(
                        "INSERT INTO league_week_dirty (player_id, week_start, source)"
                        " VALUES (%s, %s, 'profile')", (player_id, W2))

        def decide() -> tuple[dict[str, bool], int]:
            database = Database(as_role["clashlens_python_worker"])
            try:
                while league_week.decide_due(database):
                    pass
            finally:
                database.close()
            collector = CollectorDatabase(as_role["clashlens_collector"])
            try:
                failed = collector.health_metrics()["failed_processing"]
            finally:
                collector.close()
            with psycopg.connect(connection_info) as connection:
                return dict(connection.execute(
                    "SELECT player.normalized_tag, mark.failed_at IS NOT NULL"
                    " FROM league_week_dirty AS mark"
                    " JOIN players AS player ON player.id = mark.player_id").fetchall()), failed

        with psycopg.connect(connection_info) as connection:
            broken.append(connection.execute(
                "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (broken_tag,)
            ).fetchone()[0])
            fine = connection.execute(
                "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (fine_tag,)
            ).fetchone()[0]
        mark(broken[0], fine)
        set_aside = decide()
        mark(broken.pop())
        tried_again = decide()
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO league_week_dirty (player_id, week_start, source, marked_at)"
                " VALUES (%s, %s, 'profile', clock_timestamp() - interval '31 minutes')",
                (fine, W2))
        collector = CollectorDatabase(as_role["clashlens_collector"])
        try:
            stalled = collector.health_metrics()["failed_processing"]
        finally:
            collector.close()

    assert set_aside == ({broken_tag: True}, 1)
    assert tried_again == ({}, 0)
    assert stalled == 1


BUSY = [f"#{first}QQ" for first in "289PYLGRJC"]
LOOKED_UP = "#UQQ"


def test_players_whose_lock_is_busy_hold_up_no_one_else(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Ten players read in Legend II twice on Monday leave Legend I that
    week, and deciding so locks their week, which a day calculation of each
    is holding, as a Reset's board freezing holds the Reset's lock. A player
    looked up after them, with a Legend battle and a Legend I profile, must
    be decided and tracked within seconds all the same, even with batches
    of 4 marks and the ten tried again each second, so more of them are
    due again than a batch holds. Until 20 October 2026 the ten were first
    tried first in every batch, a quarter second each, then picked into
    each batch before the looked-up player, so it waited as long as the
    lock was held. Once it is free the ten are decided too."""
    sunday, monday = day(7), day(8)
    monkeypatch.setattr(league_week, "_busy", {}, raising=False)
    monkeypatch.setattr(league_week, "BATCH", 4)
    monkeypatch.setattr(league_week, "BUSY_SECONDS", 1.0)
    # The twice-daily re-check follows the real clock; it is not under test.
    monkeypatch.setattr(queue_refresh, "due_recheck_slot", lambda _connection, _now: None)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *BUSY, LOOKED_UP)
        for tag in BUSY:
            story.players[tag].battles = attacks(sunday, 40) + defenses(sunday, 40)
        story.reads(sunday + timedelta(hours=2), dict.fromkeys(BUSY, 5000))
        for tag in BUSY:
            story.players[tag].dropped = True
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(BUSY, 4960))
        story.run_worker()
        story.decides = False
        story.reads(monday + timedelta(hours=9), dict.fromkeys(BUSY, 4960))
        story.run_worker()
        story.players[LOOKED_UP].battles = attacks(monday + timedelta(hours=3), 40)
        story.reads(monday + timedelta(hours=10), {LOOKED_UP: 5040})
        story.run_worker()

        def waiting() -> set[str]:
            with psycopg.connect(connection_info) as connection:
                return {text(tag) for (tag,) in connection.execute(
                    "SELECT DISTINCT player.normalized_tag FROM league_week_dirty AS mark"
                    " JOIN players AS player ON player.id = mark.player_id"
                    " WHERE player.normalized_tag = ANY(%s)", ([*BUSY, LOOKED_UP],)).fetchall()}

        def weeks() -> dict[str, tuple[str, bool]]:
            with psycopg.connect(connection_info) as connection:
                return {text(tag): (text(league), active) for tag, league, active in
                        connection.execute(
                            "SELECT player.normalized_tag, week.league, player.active"
                            " FROM player_league_weeks AS week"
                            " JOIN players AS player ON player.id = week.player_id"
                            " WHERE week.week_start = %s AND player.normalized_tag = ANY(%s)",
                            (W2, [*BUSY, LOOKED_UP])).fetchall()}

        before, marked = weeks(), waiting()
        options = conninfo_to_dict(connection_info).get("options", "")
        database = Database(make_conninfo(
            connection_info, options=f"{options} -c role=clashlens_python_worker"))
        maintenance = worker.TimedMaintenance(database, StageMetrics())
        try:
            with psycopg.connect(connection_info) as day_calculation:
                day_calculation.execute(
                    "SELECT 1 FROM player_league_weeks AS week JOIN players AS player"
                    " ON player.id = week.player_id"
                    " WHERE player.normalized_tag = ANY(%s) AND week.week_start = %s"
                    " FOR KEY SHARE OF week", (BUSY, W2))
                started = time.monotonic()
                maintenance.run_due()
                looked_up_after = None
                while time.monotonic() < started + 15:
                    if LOOKED_UP not in waiting():
                        looked_up_after = time.monotonic() - started
                        break
                    time.sleep(0.1)
                held, during = waiting(), weeks()
                day_calculation.rollback()
            for _ in range(300):
                if not waiting():
                    break
                time.sleep(0.1)
            after, left = weeks(), waiting()
        finally:
            maintenance.close()
            database.close()

    assert before == dict.fromkeys(BUSY, ("legend_i", True))
    assert marked == {*BUSY, LOOKED_UP}
    assert looked_up_after is not None and looked_up_after < 10
    assert held == set(BUSY)
    assert during == {**before, LOOKED_UP: ("legend_i", True)}
    # Below Legend I only by answers naming Season 0, so still tracked.
    assert after == {**dict.fromkeys(BUSY, ("below_legend_i", True)),
                     LOOKED_UP: ("legend_i", True)}
    assert left == set()
