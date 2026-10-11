"""How a Reset reading starts and ends Legend days: the Season its profile
names, a weekly drop and a Season's official total.

The profile read at the 05:00 UTC Reset can come before the game applies the
previous day's automatic defense loss, so a processed Reset pair proves the
responses arrived, not that trophies settled.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database, store_observation
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    _battle_log,
    _processor,
    _profile,
)

from clashlens import api_players, reconciliation_db, reset_baselines
from clashlens.api_db import ApiDatabase

TAG = "#2PP"
# The fixture profiles report the Season that ends at the August 10 Reset.
BOUNDARIES = {
    "ordinary": DAY_END,
    "monday": datetime(2026, 8, 3, 5, tzinfo=UTC),
    "season": datetime(2026, 8, 10, 5, tzinfo=UTC),
    "season_day_2": datetime(2026, 8, 11, 5, tzinfo=UTC),
}


def _eight_defenses() -> bytes:
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    return json.dumps({"items": [
        {**template, "attack": False, "opponentPlayerTag": f"#{tag}PP"}
        for tag in "89QGRJCU"
    ]}).encode()


def _reset_work(connection_info, archive_server, boundary, *, profile=None,
                log=None, profile_status=200, status="complete",
                profile_at=None, log_at=None) -> list[int]:
    """Save a Reset sweep's responses for one player; return their jobs."""
    ids, jobs = {}, []
    for endpoint, body, http_status, observed_at in (
        ("profile", profile, profile_status, profile_at or boundary),
        ("battle_log", log, 200, log_at or boundary),
    ):
        if body is not None:
            ids[endpoint], job = store_observation(
                connection_info, archive_server,
                occurrence_key=f"{boundary.isoformat()}-{endpoint}",
                endpoint=endpoint, body=body, observed_at=observed_at,
                normalized_tag=TAG, http_status=http_status,
            )
            jobs.append(job)
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO players (normalized_tag) VALUES (%s) ON CONFLICT DO NOTHING",
            (TAG,),
        )
        connection.execute(
            """
            WITH player AS (SELECT id FROM players WHERE normalized_tag = %(tag)s),
            sweep AS (
                INSERT INTO collector_reset_sweeps (
                    boundary_at, member_ids, membership_captured_at
                ) SELECT %(at)s, ARRAY[player.id], %(at)s FROM player RETURNING id
            )
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                profile_observation_id, battle_log_observation_id, completed_at
            ) SELECT 'reset_baseline', 'reset', 'player', player.id, %(tag)s,
                     sweep.id, %(at)s, 'reset-' || %(at)s::text, %(status)s,
                     %(profile_status)s, %(log_status)s, %(profile)s, %(log)s,
                     CASE WHEN %(status)s = 'complete' THEN %(at)s END
              FROM player, sweep
            """,
            {
                "tag": TAG, "at": boundary, "status": status,
                "profile": ids.get("profile"), "log": ids.get("battle_log"),
                "profile_status": "observed" if profile else "failed",
                "log_status": "observed" if log else "failed",
            },
        )
    return jobs


def _process(connection_info, archive_server, jobs) -> None:
    database, processor = _processor(connection_info, archive_server)
    try:
        for job_id in jobs:
            assert processor.process_job(job_id, owner=f"job-{job_id}") is not None
        if not jobs:
            reset_baselines.settle_failed_reset_work(database)
        while True:
            with database.pool.connection() as connection:
                pending = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                    " AND work_type = 'reconcile_ranked_day' ORDER BY id LIMIT 1"
                ).fetchone()
            if pending is None:
                return
            processor.process_job(int(pending[0]), owner="reconcile")
    finally:
        database.close()


def _rows(connection_info: str, query: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(query).fetchall()


def _season_profile(trophies: int, season_id: int) -> bytes:
    payload = json.loads(_profile(trophies))
    payload["currentLeagueSeasonId"] = season_id
    payload["previousLeagueSeasonId"] = season_id - 28 * 24 * 60 * 60
    return json.dumps(payload).encode()


OLD_SEASON, NEW_SEASON = 1783918800, 1786338000  # Seasons around August 10.


@pytest.mark.parametrize("kind,season_id,trophies,start", [
    # An old Season with no new-Season Legend I profile gives no start.
    ("season", OLD_SEASON, 6400, None),
    ("season", NEW_SEASON, 5000, 5000),
    # A new-Season Legend I reading that is not 5,000 starts at 5,000 by the
    # Season rule.
    ("season", NEW_SEASON, 6400, 5000),
    ("season", NEW_SEASON, 4999, 5000),
    ("season_day_2", OLD_SEASON, 6400, None),
    ("monday", OLD_SEASON, 6400, 6400),
])
def test_reset_start_needs_a_profile_naming_the_resets_season(
    database_url: str,
    archive_server,
    kind: str,
    season_id: int,
    trophies: int,
    start: int | None,
) -> None:
    boundary = BOUNDARIES[kind]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6400),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_season_profile(trophies, season_id),
                            log=_battle_log(empty=True))
        _process(connection_info, archive_server, jobs)
        # The opening day is otherwise reconciled when its battles arrive.
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary, now=boundary,
                request_key="opening",
            )
            assert processor.process_job(job, owner="opening") is not None
        finally:
            database.close()
        days = _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   official_season_id, season_day_number, start_trophies,
                   next_start_trophies
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")
        evidence = _rows(connection_info, """
            SELECT DISTINCT ON (boundary_at) state,
                   profile_observation_id IS NOT NULL
            FROM reset_baseline_evidence
            WHERE boundary_at = (SELECT max(boundary_at) FROM reset_baseline_evidence)
            ORDER BY boundary_at, version DESC""")
    previous_day = boundary - timedelta(days=1)
    by_start = {row[0]: row[1:] for row in days}
    assert by_start[previous_day][3] == start
    assert by_start[boundary][2] == start
    if kind == "season":
        # The calendar names both days even while the profile is old.
        assert by_start[previous_day][:2] == (str(OLD_SEASON), 28)
        assert by_start[boundary][:2] == (str(NEW_SEASON), 1)
    # The raw reading is kept.
    assert evidence == [("complete", True)]


def _conflicting_profile(kind: str) -> bytes:
    payload = json.loads(_profile(5000))
    if kind == "season_zero":  # As the game sometimes sends with 5,000.
        payload["currentLeagueSeasonId"] = 0
    else:  # A tier name we do not recognise, under a valid Season.
        payload["leagueTier"]["name"] = "Legend One"
    return json.dumps(payload).encode()


CURRENT_PROFILE = """
    SELECT profile.trophies, profile.source_contract_state
    FROM players AS player
    JOIN player_profile_versions AS profile
      ON profile.id = player.current_profile_version_id"""


@pytest.mark.parametrize("kind,conflict", [
    ("ordinary", "season_zero"),
    ("season", "season_zero"),
    ("ordinary", "tier_name"),
])
def test_rejected_reset_profile_gives_no_start(
    database_url: str, archive_server, kind: str, conflict: str
) -> None:
    boundary = BOUNDARIES[kind]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6400),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_conflicting_profile(conflict),
                            log=_battle_log(empty=True))
        _process(connection_info, archive_server, jobs)
        # Both days are otherwise reconciled when their battles arrive.
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (boundary - timedelta(days=1), boundary):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=boundary, request_key=day_start.isoformat(),
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        days = _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   start_trophies, next_start_trophies,
                   input_evidence -> 'start_baseline_evidence',
                   input_evidence -> 'end_baseline_evidence'
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")
        kept = _rows(connection_info, """
            SELECT profile.source_contract_state, profile.trophies
            FROM reset_baseline_evidence AS evidence
            JOIN player_profile_effects AS effect
              ON effect.observation_id = evidence.profile_observation_id
            JOIN player_profile_versions AS profile
              ON profile.id = effect.profile_version_id
            WHERE evidence.boundary_at = (
                SELECT max(boundary_at) FROM reset_baseline_evidence)""")
        current = _rows(connection_info, CURRENT_PROFILE)
    by_start = {row[0]: row[1:] for row in days}
    ended, opened = by_start[boundary - timedelta(days=1)], by_start[boundary]
    # Neither day uses the rejected 5,000, and its Season is unknown rather
    # than waiting for this player's Season reset.
    assert ended[1] is None and opened[0] is None
    assert "season_reset_pending" not in ended[3]
    assert "season_reset_pending" not in opened[2]
    assert opened[2]["profile"]["trophies"] == 5000
    # The rejected reading itself stays saved as evidence, but the player's
    # current profile is still the earlier trusted one.
    assert set(kept) == {("conflict", 5000)}
    assert current == [(6400, "accepted")]


@pytest.mark.parametrize("kind", ["ordinary", "season"])
def test_reset_profile_read_after_the_first_battle_gives_no_start(
    database_url: str, archive_server, kind: str
) -> None:
    # Eight attacks (+320) and eight defenses (-280) from 05:06 come before
    # a delayed 05:30 Reset profile of 6,040. At a Season-opening Reset that
    # profile names the new Season, so the start is 5,000 by the Season rule.
    boundary = BOUNDARIES[kind]
    reading = _profile(6040) if kind == "ordinary" else _season_profile(6040, NEW_SEASON)
    rule_start = None if kind == "ordinary" else 5000
    log = json.loads(_battle_log())
    template = log["items"][0]
    log["items"] = [
        {**template, "attack": index < 8,
         "stars": 0 if index == 15 else 3,
         "destructionPercentage": 0 if index == 15 else 100,
         "opponentPlayerTag": f"#{tag}P{'Y' if index < 8 else 'L'}",
         "battleTimestamp": (boundary + timedelta(minutes=6 + index)).strftime(
             "%Y%m%dT%H%M%S.000Z")}
        for index, tag in enumerate("89QGRJCU" * 2)
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6000),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=reading, log=json.dumps(log).encode(),
                            profile_at=boundary + timedelta(minutes=30),
                            log_at=boundary + timedelta(minutes=31))
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (boundary - timedelta(days=1), boundary):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=boundary + timedelta(hours=1),
                    request_key=day_start.isoformat(),
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        days = {row[0]: row[1:] for row in _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   start_trophies, next_start_trophies, attack_count,
                   defense_count, attack_gain, observed_defense_loss
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")}
        evidence = _rows(connection_info, f"""
            SELECT evidence.profile_valid, evidence.failure_reasons,
                   profile.source_contract_state, profile.trophies
            FROM reset_baseline_evidence AS evidence
            JOIN player_profile_effects AS effect
              ON effect.observation_id = evidence.profile_observation_id
            JOIN player_profile_versions AS profile
              ON profile.id = effect.profile_version_id
            WHERE evidence.boundary_at = '{boundary.isoformat()}'
            ORDER BY evidence.version DESC, evidence.id DESC LIMIT 1""")
        current = _rows(connection_info, CURRENT_PROFILE)
    # The accepted 6,040 is kept as evidence but starts neither day. It is
    # still the current profile, so the player page can calculate a 6,000
    # start from it and the sixteen recorded battles.
    assert evidence == [
        (False, ["profile_after_first_event"], "accepted", 6040)
    ]
    assert days[boundary - timedelta(days=1)][:2] == (6000, rule_start)
    assert days[boundary][0] == rule_start
    assert days[boundary][2:] == (8, 8, 320, 280)
    assert current == [(6040, "accepted")]


def test_legend_ii_reset_profile_is_current_but_gives_no_start(
    database_url: str, archive_server
) -> None:
    # A demoted player's Legend II profile, under a valid Season.
    payload = json.loads(_profile(4900))
    payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           DAY_END - timedelta(days=1), profile=_profile(6000),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, DAY_END,
                            profile=json.dumps(payload).encode(),
                            log=_battle_log(empty=True))
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (DAY_END - timedelta(days=1), DAY_END):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=DAY_END + timedelta(hours=1),
                    request_key=day_start.isoformat(),
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        days = {row[0]: row[1:] for row in _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   start_trophies, next_start_trophies
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")}
        current = _rows(connection_info, CURRENT_PROFILE)
    assert days[DAY_END - timedelta(days=1)] == (6000, None)
    assert days[DAY_END][0] is None
    assert current == [(4900, "accepted")]


DAY_ROWS = """
    SELECT DISTINCT ON (ranked_day_start) ranked_day_start, state, confidence,
           start_trophies, next_start_trophies,
           input_evidence -> 'start_baseline_evidence' ->> 'start_trophies_source'
    FROM ranked_day_versions ORDER BY ranked_day_start, version DESC"""


@pytest.mark.parametrize("later_tier,login_day", [
    ("Legend I", 3), ("Legend I", 10), ("Legend II", 3), (None, 3),
])
def test_flip_at_login_starts_the_season_at_5000_once_a_legend_i_profile_appears(
    database_url: str, archive_server, later_tier: str | None, login_day: int
) -> None:
    # The Season-opening Reset still reads the old Season's 6,400. On a later
    # day the player logs in: still in Legend I, demoted, or never seen again.
    boundary = BOUNDARIES["season"]
    old_reading = _season_profile(6400, OLD_SEASON)
    later = old_reading
    if later_tier is not None:
        payload = json.loads(_season_profile(5000, NEW_SEASON))
        payload["leagueTier"] = {
            "id": 105000036 if later_tier == "Legend I" else 105000035,
            "name": later_tier,
        }
        later = json.dumps(payload).encode()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6400),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=old_reading, log=_battle_log(empty=True))
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary,
                now=boundary + timedelta(days=1), request_key="day-1",
            )
            assert processor.process_job(job, owner="day-1") is not None
        finally:
            database.close()
        before = {row[0]: row[1:] for row in _rows(connection_info, DAY_ROWS)}
        login = boundary + timedelta(days=login_day - 1, hours=5)
        _, login_job = store_observation(
            connection_info, archive_server, occurrence_key="later-login",
            endpoint="profile", body=later, observed_at=login, normalized_tag=TAG,
        )
        # The Reset ending the login day finds the new profile.
        jobs = [login_job] + _reset_work(
            connection_info, archive_server, boundary + timedelta(days=login_day),
            profile=later, log=_battle_log(empty=True),
        )
        _process(connection_info, archive_server, jobs)
        after = {row[0]: row[1:] for row in _rows(connection_info, DAY_ROWS)}
        # The player page reads the start's source as the data-service role.
        api = ApiDatabase(as_api_role(connection_info))
        try:
            page = api_players.get_player_page(api, TAG, now=login, freshness_seconds=900)
        finally:
            api.close()
        opening_day = next(
            day for day in page["screen_ready"]["days"]
            if day["ranked_day_start"] == boundary.isoformat()
        )
        # Finished jobs are deleted after 48 hours; the next Reset still does
        # not rebuild the Season again.
        rebuilds = ("SELECT count(*) FROM python_processing_jobs"
                    " WHERE deduplication_key LIKE 'reconcile:season-rule:%'")
        built = _rows(connection_info, rebuilds)[0][0]
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM python_processing_jobs WHERE status = 'complete'"
            )
        _process(connection_info, archive_server, _reset_work(
            connection_info, archive_server,
            boundary + timedelta(days=login_day + 1),
            profile=later, log=_battle_log(empty=True),
        ))
        rebuilt_again = _rows(connection_info, rebuilds)[0][0]
    day_28, day_1 = boundary - timedelta(days=1), boundary
    # Until then September's last day has no end and Day 1 no start.
    assert before[day_28][0] == "Partial" and before[day_28][3] is None
    assert before[day_1][2] is None
    if later_tier == "Legend I":
        assert after[day_28][:4] == ("Complete", "inferred", 6400, 5000)
        assert after[day_1][2:] == (5000, None, "season_rule")
        assert (built, rebuilt_again) == (1, 0)
        assert (opening_day["start_trophies"], opening_day["start_trophies_source"]) == (
            5000, "season_rule"
        )
    else:
        assert after[day_28][3] is None and after[day_1][2] is None
        assert (built, rebuilt_again) == (0, 0)
        assert (opening_day["start_trophies"], opening_day["start_trophies_source"]) == (
            None, None
        )


@pytest.mark.parametrize("reading,login,log_ok", [
    # A rejected Season 0 or unrecognised-league reading stays unused, but
    # the Season rule still gives the start and counts the player Legend I.
    (_conflicting_profile("season_zero"), timedelta(hours=2), True),
    (_conflicting_profile("tier_name"), timedelta(hours=2), True),
    # So does a Legend I profile first seen after the next Monday's Reset.
    (_season_profile(6400, OLD_SEASON), timedelta(days=7, hours=1), True),
    # A failed Reset battle log still gives the start, but the ended day
    # stays incomplete.
    (_season_profile(6400, OLD_SEASON), timedelta(hours=2), False),
])
def test_season_rule_starts_day_1_at_5000_once_a_new_season_profile_exists(
    database_url: str, archive_server, reading: bytes, login: timedelta,
    log_ok: bool,
) -> None:
    boundary = BOUNDARIES["season"]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6400),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=reading,
                            log=_battle_log(empty=True) if log_ok else None)
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="login",
            endpoint="profile", body=_season_profile(5000, NEW_SEASON),
            observed_at=boundary + login, normalized_tag=TAG,
        )[1])
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (boundary - timedelta(days=1), boundary):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=boundary + login, request_key=f"{day_start.isoformat()}-late",
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        days = {row[0]: row[1:] for row in _rows(connection_info, DAY_ROWS)}
    ended = days[boundary - timedelta(days=1)]
    assert (ended[0], ended[3]) == ("Complete" if log_ok else "Partial", 5000)
    assert days[boundary][2:] == (5000, None, "season_rule")


def _dropped_profile(trophies: int) -> bytes:
    """As #QUR98JV2U's first profile after the 5 October 2026 Season end:
    Legend II, Season 0, still showing its final total."""
    payload = json.loads(_profile(trophies))
    payload["currentLeagueSeasonId"] = 0
    payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
    return json.dumps(payload).encode()


def _store_dropped_login(connection_info, archive_server, key, at, final) -> int:
    return store_observation(
        connection_info, archive_server, occurrence_key=key,
        endpoint="profile", body=_dropped_profile(final),
        observed_at=at, normalized_tag=TAG,
    )[1]


@pytest.mark.parametrize("reset_reading,login,official_gaps,state", [
    ("dropped", None, (0,), "Complete"),
    ("dropped", None, (-30,), "Inconsistent"),
    # The Reset still reads Legend I and the old Season; Legend II comes
    # later, before or after the official total.
    ("old_season", "before_history", (0,), "Complete"),
    ("old_season", "after_history", (0,), "Complete"),
    # The same Legend II profile was already saved before the Season.
    ("old_season", "seen_before_season", (0,), "Complete"),
    # A row without a total changes nothing; the next one with it still counts.
    ("dropped", None, (None, 0), "Complete"),
    # A newer official total replaces the one the day already used.
    ("dropped", None, (0, -30), "Inconsistent"),
    # A Legend I reading after the Reset matching the calculated end, before
    # the game applied what the official total counts, cannot overrule it.
    ("old_season", "later_reading", (-30,), "Inconsistent"),
])
def test_last_season_day_ends_at_the_official_total(
    database_url: str, archive_server, reset_reading: str, login: str | None,
    official_gaps: tuple[int | None, ...], state: str,
) -> None:
    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    final = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = []
        if login == "seen_before_season":
            jobs.append(_store_dropped_login(
                connection_info, archive_server, "early-login",
                boundary - timedelta(days=20), final,
            ))
        jobs += _reset_work(connection_info, archive_server, last_day,
                            profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(
            connection_info, archive_server, boundary,
            profile=(_dropped_profile(final) if reset_reading == "dropped"
                     else _season_profile(final, OLD_SEASON)),
            log=_log(*battles),
        )
        if login == "later_reading":
            jobs.append(store_observation(
                connection_info, archive_server, occurrence_key="later-reading",
                endpoint="profile", body=_season_profile(final, OLD_SEASON),
                observed_at=boundary + timedelta(minutes=5), normalized_tag=TAG,
            )[1])
        if login in {"before_history", "seen_before_season", "later_reading"}:
            jobs.append(_store_dropped_login(
                connection_info, archive_server, "dropped-login",
                boundary + timedelta(hours=2), final,
            ))
        _process(connection_info, archive_server, jobs)
        before = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]
        # The official Season-end placement arrives hours later.
        for hours, gap in enumerate(official_gaps, start=6):
            _, history_job = store_observation(
                connection_info, archive_server,
                occurrence_key=f"league-history-{hours}",
                endpoint="league_history", normalized_tag=TAG,
                observed_at=boundary + timedelta(hours=hours),
                parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                processing_version="clashlens-domain-processing-v1",
                domain_rule_version="clashlens-domain-rules-v1",
                body=json.dumps({"items": [{
                    "leagueSeasonId": str(int(boundary.timestamp())),
                    "leagueTrophies": None if gap is None else final + gap,
                    "leagueTierId": 105000036, "placement": 10568,
                    "attackWins": 1, "attackLosses": 0, "attackStars": 3,
                    "defenseWins": 0, "defenseLosses": 8, "defenseStars": 16,
                    "maxBattles": 8,
                }]}).encode(),
            )
            _process(connection_info, archive_server, [history_job])
        if login == "after_history":
            # The official total alone ends the day; a later profile showing
            # the drop changes nothing.
            assert {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[
                last_day][1] == state
            _process(connection_info, archive_server, [_store_dropped_login(
                connection_info, archive_server, "dropped-login",
                boundary + timedelta(hours=8), final,
            )])
        after = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]

    assert before[:2] == (last_day, "Partial")
    assert after[:2] == (last_day, state)
    if state == "Complete":
        assert after[2:5] == ("exact", 6000, final)


@pytest.mark.parametrize("reading,drop_at,ended,next_start,monday_eligible", [
    # Ranked below 10,000: the Reset still reads Legend I, Legend II about
    # 13 minutes later, processed after or before the Reset's own work.
    ("final", "after_reset", "Complete", "final", False),
    ("final", "before_reset", "Complete", "final", False),
    # Kept in Legend I: raised to 5,000 as before.
    (5000, None, "Complete", 5000, True),
    # Legend II only after the next Reset is not a drop at this one.
    ("final", "next_day", "Inconsistent", "final", True),
])
def test_player_dropped_at_a_weekly_reset_ends_at_the_reset_reading(
    database_url: str, archive_server, reading: object, drop_at: str | None,
    ended: str, next_start: object, monday_eligible: bool,
) -> None:
    from test_first_battle_log_postgres import LOSS, WIN, _log

    boundary = BOUNDARIES["monday"]
    last_day = boundary - timedelta(days=1)
    final = 4900 + WIN - 8 * LOSS
    assert final < 5000
    reading_trophies = final if reading == "final" else reading
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    login_at = {
        "after_reset": boundary + timedelta(minutes=13),
        "before_reset": boundary + timedelta(minutes=13),
        "next_day": boundary + timedelta(days=1, hours=1),
    }
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(4900), log=_battle_log(empty=True))
        reset_jobs = _reset_work(connection_info, archive_server, boundary,
                                 profile=_profile(reading_trophies),
                                 log=_log(*battles))
        login = [] if drop_at is None else [_store_dropped_login(
            connection_info, archive_server, "dropped-login",
            login_at[drop_at], final,
        )]
        if drop_at == "before_reset":
            jobs, login = jobs + login, []
        _process(connection_info, archive_server, jobs + reset_jobs)
        # The Monday's own day, saved before or after the drop is seen.
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary,
                now=boundary + timedelta(days=1), request_key="monday",
            )
            assert processor.process_job(job, owner="monday") is not None
        finally:
            database.close()
        _process(connection_info, archive_server, login)
        days = {row[0]: row[1:] for row in _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start, state,
                   next_start_trophies, failure_reasons
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")}
        # Off the live board and its count, and on the promotion list.
        tracked = _rows(connection_info, f"""
            SELECT player.active, candidate.league_tier_id
            FROM players AS player LEFT JOIN promotion_candidates AS candidate
              USING (normalized_tag) WHERE normalized_tag = '{TAG}'""")

    assert days[last_day][:2] == (
        ended, final if next_start == "final" else next_start
    )
    assert ("player_not_eligible" not in days[boundary][2]) == monday_eligible
    assert tracked == [(False, 105000035) if drop_at else (True, None)]


def test_weekly_drop_seen_before_the_last_day_is_saved_still_ends_it(
    database_url: str, archive_server, monkeypatch
) -> None:
    from test_first_battle_log_postgres import LOSS, WIN, _log

    boundary = BOUNDARIES["monday"]
    last_day = boundary - timedelta(days=1)
    final = 4900 + WIN - 8 * LOSS
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(4900), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_profile(final), log=_log(*battles))
        jobs.append(_store_dropped_login(
            connection_info, archive_server, "dropped-login",
            boundary + timedelta(minutes=13), final,
        ))
        database, processor = _processor(connection_info, archive_server)
        try:
            # The Legend II profile is processed before any calculation saves
            # the last day, and that calculation began before it, so reads no
            # drop.
            for job_id in jobs:
                assert processor.process_job(job_id, owner=f"job-{job_id}") is not None
            with monkeypatch.context() as patch:
                patch.setattr(reconciliation_db, "_dropped_after_reading",
                              lambda *args: False)
                for (job_id,) in _rows(connection_info, """
                        SELECT id FROM python_processing_jobs
                        WHERE status = 'pending' AND work_type = 'reconcile_ranked_day'
                          AND input_json->>'trigger' IS DISTINCT FROM 'weekly_drop'
                        ORDER BY id"""):
                    processor.process_job(int(job_id), owner="stale")
        finally:
            database.close()
        stale = _rows(connection_info, f"""
            SELECT DISTINCT ON (ranked_day_start) state FROM ranked_day_versions
            WHERE ranked_day_start = '{last_day.isoformat()}'
            ORDER BY ranked_day_start, version DESC""")
        due = _rows(connection_info, """
            SELECT due_at FROM python_processing_jobs
            WHERE input_json->>'trigger' = 'weekly_drop'""")
        _process(connection_info, archive_server, [])
        ended = _rows(connection_info, f"""
            SELECT DISTINCT ON (ranked_day_start) state, next_start_trophies
            FROM ranked_day_versions
            WHERE ranked_day_start = '{last_day.isoformat()}'
            ORDER BY ranked_day_start, version DESC""")

    assert stale == [("Inconsistent",)]
    # Due once the Reset's own calculations have long saved.
    assert len(due) == 1
    assert due[0][0] >= boundary + reconciliation_db.DAY_END_RECALCULATION_DELAY
    assert ended == [("Complete", final)]


def test_weekly_drop_without_a_reset_reading_gives_no_legend_i_monday(
    database_url: str, archive_server
) -> None:
    # The Monday Reset profile fails and its battle log arrives; Legend II
    # shows 13 minutes later. Sunday's end gets no raise to 5,000, and the
    # Monday is no Legend I day, so it does not start from Sunday's end.
    from test_first_battle_log_postgres import LOSS, WIN, _log

    boundary = BOUNDARIES["monday"]
    last_day = boundary - timedelta(days=1)
    final = 4900 + WIN - 8 * LOSS
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(4900), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            log=_log(*battles))
        jobs.append(_store_dropped_login(
            connection_info, archive_server, "dropped-login",
            boundary + timedelta(minutes=13), final,
        ))
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary,
                now=boundary + timedelta(days=1), request_key="monday",
            )
            assert processor.process_job(job, owner="monday") is not None
        finally:
            database.close()
        days = {row[0]: row[1:] for row in _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   expected_next_start_trophies, start_trophies, failure_reasons,
                   input_evidence -> 'start_baseline_evidence'
                       ->> 'start_trophies_source'
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")}

    assert days[last_day][0] == final
    assert days[boundary][1] is None and days[boundary][3] is None
    assert "player_not_eligible" in days[boundary][2]


def test_official_total_saved_while_the_last_day_is_calculated_is_not_missed(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The last day's first calculation reads no official total and is still
    saving when the player's league history saves one, 30 below the day's
    calculated end. The history waits for that calculation, then queues one
    more, so the day ends Inconsistent at the official total."""
    import time

    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.db import (
        ANALYTICS_RULE_VERSION,
        DEFAULT_PARSER_VERSION,
        DOMAIN_RULE_VERSION,
        PROCESSING_VERSION,
        Database,
    )
    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    final = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_dropped_profile(final), log=_log(*battles))
        # Its evidence is saved; its calculation has not run yet.
        monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", lambda *_, **__: False)
        _process(connection_info, archive_server, jobs)
        monkeypatch.undo()
        history_job = store_observation(
            connection_info, archive_server, occurrence_key="league-history",
            endpoint="league_history", normalized_tag=TAG,
            observed_at=boundary + timedelta(hours=6),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(boundary.timestamp())),
                "leagueTrophies": final - 30, "leagueTierId": 105000036,
                "placement": 10568, "attackWins": 1, "attackLosses": 0,
                "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                "defenseStars": 16, "maxBattles": 8,
            }]}).encode(),
        )[1]
        failures: list[BaseException] = []

        def history() -> None:
            try:
                _process(connection_info, archive_server, [history_job])
            except BaseException as error:  # noqa: BLE001
                failures.append(error)

        database = Database(connection_info)
        try:
            with database.pool.connection() as calculation, calculation.transaction():
                player_id = calculation.execute(
                    "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
                ).fetchone()[0]
                reconciliation_db.recalculate_ranked_day(
                    database, calculation, player_id=player_id, day_start=last_day,
                    parser_version=DEFAULT_PARSER_VERSION,
                    processing_version=PROCESSING_VERSION,
                    domain_rule_version=DOMAIN_RULE_VERSION,
                    analytics_rule_version=ANALYTICS_RULE_VERSION,
                )
                waiting = threading.Thread(target=history)
                waiting.start()
                deadline = time.monotonic() + 30
                with psycopg.connect(connection_info, autocommit=True) as observer:
                    while not observer.execute(
                        "SELECT count(*) FROM pg_locks"
                        " WHERE locktype = 'advisory' AND NOT granted"
                    ).fetchone()[0]:
                        assert time.monotonic() < deadline, "history never waited"
                        time.sleep(0.05)
            waiting.join(timeout=60)
        finally:
            database.close()
        after = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]
        queued = _rows(
            connection_info,
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:official-final:%'",
        )[0][0]

    assert failures == []
    assert queued == 1
    assert after[:2] == (last_day, "Inconsistent")


def test_official_total_saved_days_after_a_finished_repair_ends_the_last_day(
    database_url: str, archive_server
) -> None:
    """A survivor's last day is Complete at 5,020 and the Season repair has
    recalculated it. On Day 4 of the next Season, league history fetched
    again gives the official total, 4,980: the day is queued once, however
    often that total is read, and ends at it."""
    from domain_test_support import repair_season
    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.domain import ranked_day_for
    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    start = 5020 - WIN + 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(start), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_season_profile(5000, NEW_SEASON), log=_log(*battles))
        _process(connection_info, archive_server, jobs)
        repair_season(connection_info, ranked_day_for(last_day).official_season_id)
        _process(connection_info, archive_server, [])
        before = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]
        history_jobs = [
            store_observation(
                connection_info, archive_server, occurrence_key=f"league-history-{hours}",
                endpoint="league_history", normalized_tag=TAG,
                observed_at=boundary + timedelta(days=3, hours=hours),
                parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                processing_version="clashlens-domain-processing-v1",
                domain_rule_version="clashlens-domain-rules-v1",
                body=json.dumps({"items": [{
                    "leagueSeasonId": str(int(boundary.timestamp())),
                    "leagueTrophies": 4980, "leagueTierId": 105000036,
                    "placement": 10568, "attackWins": 1, "attackLosses": 0,
                    "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                    "defenseStars": 16, "maxBattles": 8,
                }]}).encode(),
            )[1]
            for hours in (2, 3)
        ]
        _process(connection_info, archive_server, history_jobs)
        after = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]
        queued = _rows(
            connection_info,
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:official-final:%'",
        )[0][0]
        repaired = _rows(
            connection_info,
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:season-repair:%'"
            " AND status::text = 'complete'",
        )[0][0]

    assert (before[1], before[4]) == ("Complete", 5000)
    assert repaired == 1
    assert queued == 1
    assert (after[1], after[4]) == ("Inconsistent", 4980)


@pytest.mark.parametrize("gap,state", [(0, "Partial"), (-40, "Inconsistent")])
@pytest.mark.parametrize("reset_profile_only", [False, True])
def test_official_total_ends_a_last_day_whose_season_end_reset_was_never_read(
    database_url: str, archive_server, gap: int, state: str,
    reset_profile_only: bool,
) -> None:
    """The Season-ending Reset was never collected for the player, or only
    its profile was, without its battle log, so their last day had no
    proven end. League history then gives the official total: the queued
    recalculation checks the day against it. Matching, the day stays
    Partial, as nothing shows the day's battles after its last battle log;
    40 below the calculated end, it is Inconsistent."""
    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    final = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="day-log",
            endpoint="battle_log", body=_log(*battles),
            observed_at=last_day + timedelta(hours=10), normalized_tag=TAG,
        )[1])
        if reset_profile_only:
            jobs += _reset_work(connection_info, archive_server, boundary,
                                profile=_season_profile(5000, NEW_SEASON), log=None)
        # Queued by the day's battle log while the day is live; this one is past.
        database, _ = _processor(connection_info, archive_server)
        jobs.append(reconciliation_db.enqueue_reconciliation(
            database, player_tag=TAG, day_start=last_day, now=boundary, request_key="live"))
        database.close()
        _process(connection_info, archive_server, jobs)
        before = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="league-history",
            endpoint="league_history", normalized_tag=TAG,
            observed_at=boundary + timedelta(hours=6),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(boundary.timestamp())),
                "leagueTrophies": final + gap, "leagueTierId": 105000036,
                "placement": 10568, "attackWins": 1, "attackLosses": 0,
                "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                "defenseStars": 16, "maxBattles": 8,
            }]}).encode(),
        )[1]])
        after = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day]
        recalculations = _rows(
            connection_info,
            "SELECT status::text FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:official-final:%'",
        )

    assert before[1] == "Partial"
    if not reset_profile_only:
        assert before[4] is None
    assert recalculations == [("complete",)]
    assert (after[1], after[4]) == (state, final + gap)


def test_official_total_recalculates_a_last_day_saved_under_older_rules(
    database_url: str, archive_server
) -> None:
    """The player's last day was saved only under an older calculation rule.
    League history read twice gives an official total 30 below its
    calculated end: the day is queued once, recalculated under the current
    rules and Inconsistent at that total."""
    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION
    from clashlens.reconciliation import RECONCILIATION_RULE_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    final = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_dropped_profile(final), log=_log(*battles))
        _process(connection_info, archive_server, jobs)
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                "UPDATE ranked_day_versions"
                " SET reconciliation_rule_version = 'legend-ranked-day-reconciliation-v2'"
            )
        _process(connection_info, archive_server, [
            store_observation(
                connection_info, archive_server, occurrence_key=f"league-history-{hours}",
                endpoint="league_history", normalized_tag=TAG,
                observed_at=boundary + timedelta(hours=hours),
                parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                processing_version="clashlens-domain-processing-v1",
                domain_rule_version="clashlens-domain-rules-v1",
                body=json.dumps({"items": [{
                    "leagueSeasonId": str(int(boundary.timestamp())),
                    "leagueTrophies": final - 30, "leagueTierId": 105000036,
                    "placement": 10568, "attackWins": 1, "attackLosses": 0,
                    "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                    "defenseStars": 16, "maxBattles": 8,
                }]}).encode(),
            )[1]
            for hours in (6, 7)
        ])
        queued = _rows(
            connection_info,
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:official-final:%'",
        )[0][0]
        with psycopg.connect(connection_info) as connection:
            current = connection.execute(
                "SELECT state, next_start_trophies FROM ranked_day_versions"
                " WHERE ranked_day_start = %s AND reconciliation_rule_version = %s"
                " ORDER BY version DESC LIMIT 1",
                (last_day, RECONCILIATION_RULE_VERSION),
            ).fetchone()

    assert queued == 1
    assert current == ("Inconsistent", final - 30)


def test_a_late_attack_never_explains_a_survivors_official_total(
    database_url: str, archive_server
) -> None:
    """A survivor's Season-ending Reset reading, 5,000, came 3 minutes after
    their last attack, on a day whose start the Complete day before proves.
    The official total is that attack's gain below the day's calculated end.
    The attack cannot explain the gap, as the official total counts every
    battle, so the day stays Inconsistent at the official total."""
    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=hour), False) for hour in range(1, 9)]
    battles.append((boundary - timedelta(minutes=3), True))
    final = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = []
        for reset in (last_day - timedelta(days=1), last_day):
            jobs += _reset_work(connection_info, archive_server, reset,
                                profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_season_profile(5000, NEW_SEASON), log=_log(*battles))
        _process(connection_info, archive_server, jobs)
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="league-history",
            endpoint="league_history", normalized_tag=TAG,
            observed_at=boundary + timedelta(hours=6),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(boundary.timestamp())),
                "leagueTrophies": final - WIN, "leagueTierId": 105000036,
                "placement": 10568, "attackWins": 1, "attackLosses": 0,
                "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                "defenseStars": 16, "maxBattles": 8,
            }]}).encode(),
        )[1]])
        days = {row[0]: row for row in _rows(connection_info, DAY_ROWS)}

    assert days[last_day - timedelta(days=1)][1] == "Complete"
    assert days[last_day][1:5] == ("Inconsistent", "uncertain", 6000, final - WIN)


def test_official_total_leaves_an_older_seasons_last_day_as_saved(
    database_url: str, archive_server
) -> None:
    """League history gives the official total for a Season two Seasons back,
    as one read on 9 October 2026 does for the last day of 6 September. The
    day's calculation accepts only the current and previous Seasons, so
    nothing is queued and the saved result stays as it was."""
    from test_first_battle_log_postgres import LOSS, WIN, _log

    from clashlens.domain import SEASON_ANCHOR_RULE_VERSION
    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    later = boundary + timedelta(days=28)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    final = 6000 + WIN - 8 * LOSS
    saved = (
        "SELECT id, state, next_start_trophies FROM ranked_day_versions"
        f" WHERE ranked_day_start = '{last_day.isoformat()}' ORDER BY id"
    )
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_dropped_profile(final), log=_log(*battles))
        _process(connection_info, archive_server, jobs)
        before = _rows(connection_info, saved)
        with psycopg.connect(connection_info, autocommit=True) as connection:
            # Two Seasons on, the latest confirmed Season starts 28 days later.
            connection.execute("SET session_replication_role = replica")
            connection.execute(
                "UPDATE legend_season_anchors SET state = 'superseded'"
                " WHERE state = 'confirmed'"
            )
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, %s, 1, 'confirmed')
                """,
                (str(int(later.timestamp())), str(int(boundary.timestamp())),
                 later, boundary, SEASON_ANCHOR_RULE_VERSION),
            )
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="league-history",
            endpoint="league_history", normalized_tag=TAG,
            observed_at=later + timedelta(days=4),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(boundary.timestamp())),
                "leagueTrophies": final - 30, "leagueTierId": 105000036,
                "placement": 10568, "attackWins": 1, "attackLosses": 0,
                "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                "defenseStars": 16, "maxBattles": 8,
            }]}).encode(),
        )[1]])
        after = _rows(connection_info, saved)
        queued = _rows(
            connection_info,
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:official-final:%'",
        )[0][0]

    assert before
    assert queued == 0
    assert after == before


def test_day_1_is_judged_with_the_official_total_of_the_season_before(
    database_url: str, archive_server,
) -> None:
    # A Day 1 profile read before the game's reset to 5,000 shows the
    # previous Season's official total, which Day 1 is judged with; it still
    # starts at 5,000.
    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, boundary - timedelta(days=1),
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_season_profile(5000, NEW_SEASON),
                            log=_battle_log(empty=True))
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="league-history",
            endpoint="league_history", normalized_tag=TAG,
            observed_at=boundary + timedelta(hours=5),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(boundary.timestamp())),
                "leagueTrophies": 5965, "leagueTierId": 105000036,
                "placement": 10568, "attackWins": 0, "attackLosses": 0,
                "attackStars": 0, "defenseWins": 0, "defenseLosses": 0,
                "defenseStars": 0, "maxBattles": 8,
            }]}).encode(),
        )[1])
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary, now=boundary,
                request_key="opening",
            )
            assert processor.process_job(job, owner="opening") is not None
        finally:
            database.close()
        day_1 = _rows(connection_info, f"""
            SELECT DISTINCT ON (ranked_day_start) start_trophies,
                   input_evidence -> 'start_baseline_evidence' ->> 'official_final_trophies'
            FROM ranked_day_versions WHERE ranked_day_start = '{boundary.isoformat()}'
            ORDER BY ranked_day_start, version DESC""")

    assert day_1 == [(5000, "5965")]
