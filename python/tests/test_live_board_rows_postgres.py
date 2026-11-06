"""The Live Leaderboard's rows from saved profiles, league weeks, checks, saved
5,000 profiles and attack tallies.

On 31 October 2026 at 07:31 the Live board's query passed the API's 10
second limit and the page showed "Leaderboard unavailable". Its rewrite
reads the players once and joins the saved 5,000 profiles instead of
looking one up per player; these players keep every rule it applies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database

from clashlens.api_leaderboard import _read_live_board, live_positions
from clashlens.domain import ranked_day_for

# Saturday of the Season's second week, which began Monday 12 October.
NOW = datetime(2026, 10, 17, 7, 31, tzinfo=UTC)
SEASON, OLD_SEASON = (ranked_day_for(at).official_season_id
                      for at in (NOW, NOW - timedelta(days=28)))
SEASON_START, WEEK_START = datetime(2026, 10, 5, 5, tzinfo=UTC), datetime(
    2026, 10, 12, 5, tzinfo=UTC)
READ = NOW - timedelta(hours=1)
LAST_WEEK = WEEK_START - timedelta(days=2)


def _player(connection, tag, trophies, *, season=SEASON, read=READ, state="accepted",
            active=True, confirmed=None) -> tuple[int, int]:
    """A player and their current profile: (player id, profile id)."""
    player = connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state, current_observed_at,
                             current_profile_confirmed_at)
        VALUES (%s, %s, 'eligible', %s, %s) RETURNING id
        """,
        (tag, active, read, confirmed),
    ).fetchone()[0]
    profile = _profile(connection, player, tag, trophies, season, read, state)
    connection.execute("UPDATE players SET current_profile_version_id = %s WHERE id = %s",
                       (profile, player))
    return player, profile


def _profile(connection, player, tag, trophies, season, read, state="accepted") -> int:
    return connection.execute(
        """
        INSERT INTO player_profile_versions (
            player_id, observation_id, normalized_tag, endpoint_version, schema_version,
            parser_version, observed_at, source_http_status, name, trophies,
            league_tier_id, league_tier_name, eligibility_state, current_league_season_id,
            eligibility_reason, source_contract_state, profile_json)
        VALUES (%s, -%s * 10 - (SELECT count(*) FROM player_profile_versions
                                WHERE player_id = %s),
                %s, 'v1', 'v1', 'p1', %s, 200, %s, %s, 105000034, 'Legend League',
                'eligible', %s, 'confirmed_legend_i', %s,
                jsonb_build_object('clan', jsonb_build_object('name', %s::text)))
        RETURNING id
        """,
        (player, player, player, tag, read, f"Name {tag}", trophies, season, state,
         f"Clan {tag}"),
    ).fetchone()[0]


def _check(connection, player, tag, *, success, not_found=None, observation=None) -> None:
    connection.execute(
        """
        INSERT INTO collector_response_state (
            scope, identity_key, endpoint, player_id, normalized_tag, last_response_hash,
            last_content_fingerprint, last_occurrence_key, last_applied_occurrence_key,
            last_seen_at, last_success_at, last_not_found_at, last_observation_id)
        VALUES ('player', %s, 'profile', %s, %s, repeat('a', 64), repeat('a', 64), %s, %s,
                %s, %s, %s, %s)
        """,
        (tag, player, tag, tag, tag, success, success, not_found, observation),
    )


def _week(connection, player, week_start, league, entry=None) -> None:
    connection.execute(
        """
        INSERT INTO player_league_weeks (player_id, week_start, league, entry, strength,
                                         proof_kind, proof_at, proof_source_id, rule_version)
        VALUES (%s, %s, %s, %s, 3, 'accepted_answer', %s, -1, 1)
        """,
        (player, week_start, league, entry, week_start),
    )


def _saved(connection, player, profile, season, sighted, name) -> None:
    connection.execute(
        """
        INSERT INTO live_unplayed_profiles (
            player_id, official_season_id, profile_version_id, observation_id, name,
            trophies, clan, sighted_at, sighting_observation_id)
        SELECT %s, %s, id, observation_id, %s, 5000, 'Saved clan', %s, NULL
        FROM player_profile_versions WHERE id = %s
        """,
        (player, season, name, sighted, profile),
    )


def _tally(connection, player, attacks, destruction) -> None:
    connection.execute(
        "INSERT INTO live_attack_tallies (player_id, official_season_id, attacks, destruction)"
        " VALUES (%s, %s, %s, %s)",
        (player, SEASON, attacks, destruction),
    )


def test_the_live_board_ranks_each_tracked_player_by_its_rules(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute("SET session_replication_role = replica")
            # Read this week; equal trophies order by average attack
            # destruction, then attacks, then the tag's hash.
            stay, _ = _player(connection, "#STAY", 5200, read=LAST_WEEK)
            _week(connection, stay, SEASON_START, "legend_i", "season_start")
            _week(connection, stay, WEEK_START, "legend_i", "stayed")
            for tag, tally in (("#TIEA", (10, 900)), ("#TIEB", (10, 950)),
                               ("#TIEC", None), ("#TIED", (0, 0)), ("#TIEE", (12, 1080))):
                player, _ = _player(connection, tag, 5100,
                                    confirmed=READ + timedelta(minutes=8))
                if tally:
                    _tally(connection, player, *tally)
            # Out of Legend I this week, and a profile no longer found.
            low, _ = _player(connection, "#LOW", 5300)
            _week(connection, low, WEEK_START, "below_legend_i")
            gone, _ = _player(connection, "#GONE", 5300)
            _check(connection, gone, "#GONE", success=READ, not_found=NOW)
            back, _ = _player(connection, "#BACK", 5050)
            _check(connection, back, "#BACK", success=READ, not_found=READ - timedelta(days=1))
            # Season 0 at 5,000: its saved profile ranks it, last confirmed
            # by an unchanged check.
            zero, zero_profile = _player(connection, "#ZERO", 5000, season="0",
                                         confirmed=READ + timedelta(minutes=2))
            _saved(connection, zero, zero_profile, SEASON, READ, "Saved zero")
            _check(connection, zero, "#ZERO", success=READ + timedelta(minutes=9),
                   observation=-zero * 10)
            # At 5,000 this Season with an older 5,000 profile saved, seen
            # again by a check whose response kept it.
            at_5000, _ = _player(connection, "#AT5K", 5000)
            older = _profile(connection, at_5000, "#AT5K", 5000, "0", READ - timedelta(hours=5))
            _saved(connection, at_5000, older, SEASON, READ - timedelta(hours=5), "Saved 5k")
            _check(connection, at_5000, "#AT5K", success=READ + timedelta(minutes=1),
                   observation=-1)
            connection.execute(
                """
                INSERT INTO player_profile_effects (
                    profile_version_id, observation_id, effect_kind, observed_at,
                    source_http_status, endpoint_version, schema_version, parser_version)
                VALUES (%s, -1, 'current_profile', %s, 200, 'v1', 'v1', 'p1')
                """,
                (older, READ),
            )
            _tally(connection, at_5000, 5, 400)
            # Waiting for their Season: a 5,000 profile saved last Season,
            # and a profile read before a promotion this week.
            old_saved, old_profile = _player(connection, "#OLDSAVE", 5400, season=OLD_SEASON)
            _saved(connection, old_saved, old_profile, OLD_SEASON, READ, "Saved old")
            stale, _ = _player(connection, "#STALE", 5400, read=LAST_WEEK)
            _week(connection, stale, WEEK_START, "legend_i", "promoted")
            # Not tracked, and tracked with no accepted profile.
            _player(connection, "#AWAY", 5500, active=False)
            _player(connection, "#NOPROF", 5500, state="conflict")
            connection.execute("ANALYZE")
            board = _read_live_board(connection, NOW)
            positions = live_positions(connection, [row[0] for row in board.rows], now=NOW)

    seen = READ + timedelta(minutes=8)
    assert board.rows == [
        ("#STAY", "Name #STAY", 5200, LAST_WEEK, "eligible", "Clan #STAY"),
        ("#TIEB", "Name #TIEB", 5100, seen, "eligible", "Clan #TIEB"),
        ("#TIEE", "Name #TIEE", 5100, seen, "eligible", "Clan #TIEE"),
        ("#TIEA", "Name #TIEA", 5100, seen, "eligible", "Clan #TIEA"),
        ("#TIEC", "Name #TIEC", 5100, seen, "eligible", "Clan #TIEC"),
        ("#TIED", "Name #TIED", 5100, seen, "eligible", "Clan #TIED"),
        ("#BACK", "Name #BACK", 5050, READ, "eligible", "Clan #BACK"),
        ("#AT5K", "Saved 5k", 5000, READ + timedelta(minutes=1), "eligible", "Saved clan"),
        ("#ZERO", "Saved zero", 5000, READ + timedelta(minutes=9), "eligible", "Saved clan"),
    ]
    assert positions == {row[0]: place for place, row in enumerate(board.rows, start=1)}
    # Every active player is tracked; two wait for their Season.
    assert (board.tracked_population, board.season_reset_pending) == (14, 2)
