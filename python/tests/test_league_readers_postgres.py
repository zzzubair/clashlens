"""End-to-end: every reader asks the weekly league record which days are
Legend days (stage 3).

Saved responses for a 12 October 2026-like Monday go through the real worker
into PostgreSQL, the worker's maintenance timer decides each player's weeks,
and the data service's own login reads them back the way the website, the
Discord bot and Season summaries do. See docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from test_league_week_postgres import _decide
from test_reset_scenarios_postgres import SEASON, _story, attacks, day, defenses

from bot import replies
from clashlens import api_players, season_summaries
from clashlens.api_db import ApiDatabase

STAYED, DROPPED, UNKNOWN, UNDECIDED = "#2PP", "#8PP", "#9PP", "#PPP"
SEASON_ID = str(int(SEASON.timestamp()))
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _page(connection_info: str, tag: str) -> dict:
    api = ApiDatabase(as_api_role(connection_info))
    try:
        return api_players.get_player_page(api, tag, now=NOW, freshness_seconds=900)
    finally:
        api.close()


def _days(page: dict) -> dict[datetime, list[str]]:
    """Each Legend day the page shows from Sunday 11 October, with its
    reasons."""
    return {
        start: shown["uncertainty_reasons"]
        for shown in page["screen_ready"]["days"]
        if (start := datetime.fromisoformat(shown["ranked_day_start"])) >= day(7)
    }


def _season_flags(connection_info: str, tag: str) -> dict[int, list[str]]:
    """Each day of the player's Season summary, by day number, with its
    flags, summarized again as the worker does after saving a day."""
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (tag,)).fetchone()[0]
        season_summaries.materialize_player_season(connection, player_id, SEASON_ID)
    api = ApiDatabase(as_api_role(connection_info))
    try:
        summary = api_players.get_player_season_summary(api, tag, SEASON_ID)
    finally:
        api.close()
    return {entry["season_day_number"]: entry["flags"] for entry in summary["daily_entries"]}


def test_readers_show_legend_i_days_hide_lower_weeks_and_flag_unconfirmed_ones(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Four players play Sunday 11 October (the Season's first week) as
    Legend I players. STAYED, UNKNOWN and UNDECIDED play Monday too. DROPPED
    is read in Legend II naming Season 0 at 05:20 and 14:00 on Monday, which
    proves its second week below Legend I. The Tuesday Reset ends Monday. UNKNOWN's second week is then set
    unknown, as conflicting evidence leaves it, and UNDECIDED's is not
    decided yet.

    The player page, the Discord bot's day summary and the Season summary
    keep both of STAYED's days as they were saved; leave out DROPPED's
    Monday, which is still saved; and label UNKNOWN's and UNDECIDED's Monday
    "league not confirmed". The page says DROPPED is below Legend I this
    week."""
    sunday, monday, tuesday = day(7), day(8), day(9)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       STAYED, DROPPED, UNKNOWN, UNDECIDED)
        for tag in (STAYED, UNKNOWN, UNDECIDED):
            story.players[tag].battles = (
                attacks(sunday, 40, 40) + defenses(sunday, 40)
                + attacks(monday, 40) + defenses(monday, 30))
        story.players[DROPPED].battles = attacks(sunday, 40) + defenses(sunday, 40, 40)
        story.reset(sunday, {tag: (5100, SEASON) for tag in story.players})
        story.reset(monday, {STAYED: (5000, SEASON), DROPPED: (4920, SEASON),
                             UNKNOWN: (5000, SEASON), UNDECIDED: (5000, SEASON)})
        story.players[DROPPED].dropped = True
        story.reads(monday + timedelta(minutes=20), {DROPPED: 4920})
        story.reads(monday + timedelta(hours=7), {STAYED: 5010, UNKNOWN: 5010, UNDECIDED: 5010})
        story.reads(monday + timedelta(hours=9), {DROPPED: 4920})
        story.reset(tuesday, {STAYED: (5010, SEASON), DROPPED: (4920, None),
                              UNKNOWN: (5010, SEASON), UNDECIDED: (5010, SEASON)})
        story.run_worker()
        _decide(connection_info, monkeypatch)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                UPDATE player_league_weeks AS week
                SET league = 'unknown', entry = NULL, strength = 0,
                    unknown_reason = 'no_evidence', proof_kind = NULL, proof_at = NULL,
                    proof_source_id = NULL, proof_second_id = NULL
                FROM players AS player
                WHERE player.id = week.player_id AND player.normalized_tag = %s
                  AND week.week_start = %s
                """,
                (UNKNOWN, monday),
            )
            connection.execute(
                """
                DELETE FROM player_league_weeks AS week USING players AS player
                WHERE player.id = week.player_id AND player.normalized_tag = %s
                  AND week.week_start = %s
                """,
                (UNDECIDED, monday),
            )
            weeks = dict(connection.execute(
                """
                SELECT player.normalized_tag, week.league FROM player_league_weeks AS week
                JOIN players AS player ON player.id = week.player_id
                WHERE week.week_start = %s AND player.normalized_tag = ANY(%s)
                """,
                (monday, list(story.players)),
            ).fetchall())
            saved = {tag for (tag,) in connection.execute(
                """
                SELECT player.normalized_tag FROM api_player_daily_logs AS saved
                JOIN players AS player ON player.id = saved.player_id
                WHERE saved.ranked_day_start = %s
                """,
                (monday,),
            ).fetchall()}
        pages = {tag: _page(connection_info, tag) for tag in story.players}
        seasons = {tag: _season_flags(connection_info, tag) for tag in story.players}

    assert weeks == {STAYED: "legend_i", DROPPED: "below_legend_i", UNKNOWN: "unknown"}
    assert saved == {STAYED, DROPPED, UNKNOWN, UNDECIDED}
    not_confirmed = "league_not_confirmed"
    shown = {tag: _days(page) for tag, page in pages.items()}
    assert set(shown[STAYED]) == {sunday, monday}
    assert not_confirmed not in shown[STAYED][sunday] + shown[STAYED][monday]
    assert set(shown[DROPPED]) == {sunday}
    for tag in (UNKNOWN, UNDECIDED):
        assert set(shown[tag]) == {sunday, monday}
        assert not_confirmed not in shown[tag][sunday]
        assert not_confirmed in shown[tag][monday]
    assert pages[STAYED]["eligibility"] == "eligible"
    assert pages[DROPPED]["eligibility"] == "ineligible"
    assert pages[UNKNOWN]["eligibility"] == "uncertain"

    summaries = {tag: replies.summary_day(page, tuesday, None, False)
                 for tag, page in pages.items()}
    assert summaries[DROPPED] is None
    assert summaries[STAYED]["status"] != "League not confirmed"
    assert summaries[UNKNOWN]["status"] == "League not confirmed"
    assert summaries[UNDECIDED]["status"] == "League not confirmed"

    assert set(seasons[STAYED]) == {6, 7, 8}
    assert not_confirmed not in seasons[STAYED][8]
    assert set(seasons[DROPPED]) == {6, 7}
    assert not_confirmed in seasons[UNKNOWN][8]
    assert not_confirmed not in seasons[UNKNOWN][7]


def test_legend_days_are_the_saved_days_of_legend_i_weeks_unchanged(database_url: str) -> None:
    """Every column of a saved day in a Legend I week reads through
    legend_days exactly as saved; a day in a week below Legend I is left
    out; a day in an unknown or undecided week gains league_not_confirmed
    once, even when already saved with it. The data service can no longer
    read saved days around the rule."""
    week = datetime(2026, 10, 12, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info, psycopg.connect(
        connection_info
    ) as connection:
        players = [connection.execute(
            "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (tag,)
        ).fetchone()[0] for tag in ("#2PP", "#8PP", "#9PP", "#PPP", "#YPP")]
        legend_i, below, unknown, undecided, saved_flag = players
        for player in players:
            connection.execute(
                """
                INSERT INTO api_player_daily_logs (
                    player_id, ranked_day_start, version, state, coverage,
                    partial_reasons, attack_count, attack_gain)
                VALUES (%s, %s, 1, 'Complete', 'complete', %s, 8, 240)
                """,
                (player, week + timedelta(days=2),
                 '["league_not_confirmed"]' if player == saved_flag else '[]'),
            )
        for player, league in ((legend_i, "legend_i"), (below, "below_legend_i"),
                               (unknown, "unknown"), (saved_flag, "unknown")):
            connection.execute(
                """
                INSERT INTO player_league_weeks (
                    player_id, week_start, league, entry, strength, unknown_reason,
                    proof_kind, proof_at, proof_source_id, rule_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                """,
                (player, week, league,
                 "stayed" if league == "legend_i" else None,
                 0 if league == "unknown" else 4,
                 "no_evidence" if league == "unknown" else None,
                 None if league == "unknown" else "battle",
                 None if league == "unknown" else week,
                 None if league == "unknown" else 1),
            )
        saved = connection.execute(
            "SELECT * FROM api_player_daily_logs WHERE player_id = %s", (legend_i,)
        ).fetchall()
        through_view = connection.execute(
            """
            SELECT id, player_id, ranked_day_start, version, state, coverage,
                   adjustments, battles, partial_reasons, published_at,
                   ranked_day_end, official_season_id, season_day_number,
                   confidence, attack_count, attack_three_star_count, attack_gain,
                   defense_count, defense_three_star_count, defense_loss,
                   net_trophy_change, ranked_day_version_id
            FROM legend_days WHERE player_id = %s
            """,
            (legend_i,),
        ).fetchall()
        shown = dict(connection.execute(
            "SELECT player_id, partial_reasons FROM legend_days WHERE player_id = ANY(%s)",
            (players,),
        ).fetchall())
        leagues = dict(connection.execute(
            "SELECT player_id, league FROM legend_days WHERE player_id = ANY(%s)", (players,)
        ).fetchall())
        with psycopg.connect(as_api_role(connection_info)) as api, pytest.raises(
            psycopg.errors.InsufficientPrivilege
        ):
            api.execute("SELECT 1 FROM api_player_daily_logs LIMIT 1")

    assert through_view == saved
    assert shown == {
        legend_i: [],
        unknown: ["league_not_confirmed"],
        undecided: ["league_not_confirmed"],
        saved_flag: ["league_not_confirmed"],
    }
    assert leagues == {legend_i: "legend_i", unknown: "unknown", undecided: "unknown",
                       saved_flag: "unknown"}
