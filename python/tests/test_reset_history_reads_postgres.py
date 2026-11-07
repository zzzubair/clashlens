"""A Reset's board work must not read each member's whole profile history.

On the copy of 14 October 2026 a swept player had about 126 saved profiles
and 190 sightings of them. Each member result asked whether the player had
an accepted reading before the Reset by sorting all of them: about 118 pages
a member read cold, 38.6 s for 1,500 members. Taking a board's members read
each swept player's history too, under the Reset's full publication lock.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database
from test_reset_scenarios_postgres import (
    SEASON,
    _even,
    _ranked,
    _story,
    _tag,
    attacks,
    day,
)

from clashlens import boundary, boundary_members, snapshots

SWEPT, HISTORY = 30, 400
HISTORY_TABLES = ("player_profile_versions", "player_profile_effects")


def _seed_history(connection_info: str) -> None:
    """HISTORY older accepted profiles for each player, each sighted once,
    from July 2026, long before the story's Seasons."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")

        def others(table: str, *own: str) -> str:
            return ", ".join(
                row[0] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = %s AND NOT column_name = ANY(%s)"
                    " AND is_generated = 'NEVER' AND identity_generation IS NULL",
                    (table, ["id", *own]),
                ).fetchall()
            )

        versions = others("player_profile_versions", "observation_id", "observed_at")
        effects = others(
            "player_profile_effects", "observation_id", "profile_version_id", "observed_at")
        connection.execute(
            f"""
            WITH copy AS (
                INSERT INTO player_profile_versions (observation_id, observed_at, {versions})
                SELECT -(version.id * {HISTORY} + n),
                       timestamptz '2026-07-01 00:00+00' + n * interval '1 hour', {versions}
                FROM player_profile_versions AS version, generate_series(1, {HISTORY}) AS n
                RETURNING id, observation_id, observed_at
            )
            INSERT INTO player_profile_effects (
                observation_id, profile_version_id, observed_at, {effects})
            SELECT copy.observation_id, copy.id, copy.observed_at + interval '1 minute',
                   {", ".join(f"effect.{name}" for name in effects.split(", "))}
            FROM copy, LATERAL (SELECT * FROM player_profile_effects LIMIT 1) AS effect
            """
        )
        connection.execute(f"ANALYZE {', '.join(HISTORY_TABLES)}")


def test_reset_board_reads_no_members_whole_profile_history(
    database_url: str, archive_server, monkeypatch
) -> None:
    sunday, monday = day(7), day(8)
    tags = [f"#P{_tag(n)[1:]}" for n in range(1, SWEPT + 1)]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *tags)
        for tag in tags:
            story.players[tag].battles = _even(sunday) + attacks(
                sunday + timedelta(hours=6), 40, 40)
        story.reset(sunday, dict.fromkeys(tags, (5300, SEASON)))
        story.reset(monday, dict.fromkeys(tags, (5380, SEASON)), run=False)
        _seed_history(connection_info)
        story.run_worker()
        unfinished = story.unfinished_jobs()
        members = story.board_members(monday)
        board = story.served_board(monday)
        # The board's two history checks, as the Reset ran them, explained.
        plans: list[dict] = []
        with psycopg.connect(connection_info) as connection:
            explaining = SimpleNamespace(execute=lambda sql, params: (plans.append(
                connection.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}", params)
                .fetchone()[0][0]["Plan"]), connection.execute(sql, params))[1])
            swept, (player_id, version_id) = connection.execute(
                """
                SELECT sweep.member_ids, (
                    SELECT ARRAY[member.player_id, member.ranked_day_version_id]
                    FROM boundary_publication_generation_members AS member
                    WHERE member.generation_id = generation.id
                    ORDER BY member.player_id LIMIT 1)
                FROM boundary_publication_generations AS generation
                JOIN collector_reset_sweeps AS sweep ON sweep.id = generation.sweep_id
                WHERE generation.boundary_at = %s
                ORDER BY generation.generation DESC LIMIT 1
                """,
                (monday,),
            ).fetchone()
            taken = boundary_members.board_members(explaining, monday, swept)
            taking = _history_rows(plans)
            plans.clear()
            status = boundary._boundary_snapshot_status(
                explaining, player_id=player_id, ranked_day_version_id=version_id,
                boundary_at=monday)
            checking = _history_rows(plans)
    assert unfinished == []
    assert members == dict.fromkeys(tags, "complete")
    assert board is not None and board["total_entries"] == SWEPT
    assert _ranked(board) == dict.fromkeys(tags, 5380)
    # Every member was swept, so taking the members reads no one's history.
    assert taken == sorted(swept)
    assert taking == 0, taking
    # A member's result stops at their newest reading before the Reset, not
    # all HISTORY of them.
    assert status == "complete"
    assert checking < 20, checking


def test_board_build_reads_one_ranked_day_however_many_newer_days_exist(
    database_url: str, archive_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correction rebuilds a board weeks after its Reset. Finding the
    board's Legend day joined its input list to every saved day newest first:
    in production on 7 November 2026 the 6 October board walked the millions
    of days saved since against its 13,452 rows, hit the 15-minute limit on
    every try, and held back every queued board behind it."""
    # With enough members, PostgreSQL bets that walking days newest first
    # finds one of theirs soon, as it did in production.
    members, newer = 100, 6_000
    sunday, monday = day(7), day(8)
    tags = [f"#P{_tag(n)[1:]}" for n in range(1, members + 1)]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *tags)
        for tag in tags:
            story.players[tag].battles = _even(sunday) + attacks(
                sunday + timedelta(hours=6), 40, 40)
        story.reset(sunday, dict.fromkeys(tags, (5300, SEASON)))
        story.reset(monday, dict.fromkeys(tags, (5380, SEASON)), run=False)
        _seed_newer_days(connection_info, newer)
        # Every SELECT the board build runs, explained as it runs.
        plans: list[dict] = []
        building = [False]
        execute = psycopg.Connection.execute

        def explained(connection, query, params=None, **kwargs):
            if building[0] and str(query).lstrip().startswith("SELECT"):
                plans.append(execute(
                    connection, f"EXPLAIN (ANALYZE, FORMAT JSON) {query}", params)
                    .fetchone()[0][0]["Plan"])
            return execute(connection, query, params, **kwargs)

        complete = snapshots.complete_snapshot

        def build(database, claim):
            # Production's statistics are current; a story's tables are new.
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute("ANALYZE")
            building[0] = True
            try:
                return complete(database, claim)
            finally:
                building[0] = False

        monkeypatch.setattr(psycopg.Connection, "execute", explained)
        monkeypatch.setattr(snapshots, "complete_snapshot", build)
        story.run_worker()
        unfinished = story.unfinished_jobs()
        board = story.served_board(monday)
    assert unfinished == []
    assert plans, "no board was built"
    assert _ranked(board) == dict.fromkeys(tags, 5380)
    # The build reads its own members' days, not the newer ones.
    read = _history_rows(plans, ("ranked_day_versions",))
    assert read < newer // 10, read


def _seed_newer_days(connection_info: str, count: int) -> None:
    """``count`` saved days with ids above every day the story saves, as a
    board rebuilt weeks after its Reset sees: copies of the story's first
    day, dated on the days before July 2026, long before its Seasons."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        columns = ", ".join(
            row[0] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'ranked_day_versions' AND NOT column_name = ANY(%s)",
                (["id", "ranked_day_start", "ranked_day_end", "replaces_version_id"],),
            ).fetchall()
        )
        connection.execute(
            f"""
            INSERT INTO ranked_day_versions
                (id, ranked_day_start, ranked_day_end, {columns})
            OVERRIDING SYSTEM VALUE
            SELECT 1000000 + row_number() OVER (), start, start + interval '1 day',
                   {columns}
            FROM (
                SELECT timestamptz '2026-07-01 05:00+00' - n * interval '1 day' AS start,
                       day.*
                FROM (SELECT * FROM ranked_day_versions ORDER BY id LIMIT 1) AS day,
                     generate_series(1, %s) AS n
            ) AS copy
            """,
            (count,),
        )
        connection.execute("ANALYZE ranked_day_versions")


def _history_rows(plans: list[dict], tables: tuple[str, ...] = HISTORY_TABLES) -> int:
    """Rows the plans read from ``tables``: by default saved profiles and
    their sightings."""
    def nodes(plan: dict) -> list[dict]:
        return [plan, *(node for child in plan.get("Plans", []) for node in nodes(child))]

    return sum(
        int(node["Actual Rows"] * node["Actual Loops"])
        for plan in plans for node in nodes(plan)
        if node.get("Relation Name") in tables
    )
